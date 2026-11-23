-- pgcypher catalog: plain SQL and PL/pgSQL only, so it installs on managed PostgreSQL
-- (Amazon RDS, Aurora, Cloud SQL, Azure) without superuser rights or C extensions.
-- Values are jsonb. Vertices, edges and paths carry a "~t" marker key that the
-- output formatter turns into AGE's ::vertex, ::edge and ::path suffixes.

CREATE SCHEMA IF NOT EXISTS ag_catalog;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE n.nspname = 'ag_catalog' AND t.typname = 'agtype') THEN
        CREATE DOMAIN ag_catalog.agtype AS text;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS ag_catalog.ag_graph (
    name text PRIMARY KEY,
    namespace text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION ag_catalog.create_graph(graph_name text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = graph_name) THEN
        RAISE EXCEPTION 'graph "%" already exists', graph_name USING ERRCODE = 'invalid_schema_name';
    END IF;
    EXECUTE format('CREATE SCHEMA %I', graph_name);
    EXECUTE format('CREATE SEQUENCE %I._id_seq', graph_name);
    EXECUTE format('CREATE TABLE %I._vertex (id bigint PRIMARY KEY, label text NOT NULL, properties jsonb NOT NULL DEFAULT ''{}'')', graph_name);
    EXECUTE format('CREATE TABLE %I._edge (id bigint PRIMARY KEY, label text NOT NULL, start_id bigint NOT NULL, end_id bigint NOT NULL, properties jsonb NOT NULL DEFAULT ''{}'')', graph_name);
    EXECUTE format('CREATE INDEX ON %I._vertex (label)', graph_name);
    EXECUTE format('CREATE INDEX ON %I._vertex USING gin (properties jsonb_path_ops)', graph_name);
    EXECUTE format('CREATE INDEX ON %I._edge (start_id, label)', graph_name);
    EXECUTE format('CREATE INDEX ON %I._edge (end_id, label)', graph_name);
    EXECUTE format('CREATE INDEX ON %I._edge USING gin (properties jsonb_path_ops)', graph_name);
    INSERT INTO ag_catalog.ag_graph (name, namespace) VALUES (graph_name, graph_name);
    RAISE NOTICE 'graph "%" has been created', graph_name;
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.drop_graph(graph_name text, cascade boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = graph_name) THEN
        RAISE EXCEPTION 'graph "%" does not exist', graph_name USING ERRCODE = 'invalid_schema_name';
    END IF;
    IF cascade THEN
        EXECUTE format('DROP SCHEMA %I CASCADE', graph_name);
    ELSE
        EXECUTE format('DROP TABLE %I._vertex, %I._edge', graph_name, graph_name);
        EXECUTE format('DROP SEQUENCE %I._id_seq', graph_name);
        EXECUTE format('DROP SCHEMA %I', graph_name);
    END IF;
    DELETE FROM ag_catalog.ag_graph WHERE name = graph_name;
    RAISE NOTICE 'graph "%" has been dropped', graph_name;
END
$$;

-- Labels need no table of their own; these exist so AGE setup scripts keep working.
CREATE OR REPLACE FUNCTION ag_catalog.create_vlabel(graph_name text, label_name text) RETURNS void
LANGUAGE sql AS $$ SELECT NULL::void $$;
CREATE OR REPLACE FUNCTION ag_catalog.create_elabel(graph_name text, label_name text) RETURNS void
LANGUAGE sql AS $$ SELECT NULL::void $$;

-- ---------------------------------------------------------------- value helpers

CREATE OR REPLACE FUNCTION ag_catalog.pc_isint(j jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT jsonb_typeof(j) = 'number' AND j::text !~ '[.eE]' $$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_float(x double precision) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF x IS NULL THEN RETURN NULL; END IF;
    IF x = trunc(x) AND abs(x) < 1e15 THEN
        RETURN to_jsonb((trunc(x)::bigint::text || '.0')::numeric);
    END IF;
    RETURN to_jsonb(x::text::numeric);
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_str(j jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(j) WHEN 'string' THEN j #>> '{}' ELSE j::text END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_add(a jsonb, b jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF a IS NULL OR b IS NULL THEN RETURN NULL; END IF;
    IF jsonb_typeof(a) = 'array' THEN
        IF jsonb_typeof(b) = 'array' THEN RETURN a || b; END IF;
        RETURN a || jsonb_build_array(b);
    END IF;
    IF jsonb_typeof(b) = 'array' THEN RETURN jsonb_build_array(a) || b; END IF;
    IF jsonb_typeof(a) = 'string' OR jsonb_typeof(b) = 'string' THEN
        RETURN to_jsonb(ag_catalog.pc_str(a) || ag_catalog.pc_str(b));
    END IF;
    IF ag_catalog.pc_isint(a) AND ag_catalog.pc_isint(b) THEN
        RETURN to_jsonb(a::numeric + b::numeric);
    END IF;
    RETURN ag_catalog.pc_float(a::float8 + b::float8);
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_arith(op text, a jsonb, b jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    ints boolean;
BEGIN
    IF a IS NULL OR b IS NULL THEN RETURN NULL; END IF;
    IF jsonb_typeof(a) <> 'number' OR jsonb_typeof(b) <> 'number' THEN
        RAISE EXCEPTION 'operator % needs numbers, got % and %', op, a, b USING ERRCODE = 'invalid_parameter_value';
    END IF;
    ints := ag_catalog.pc_isint(a) AND ag_catalog.pc_isint(b);
    IF op = '^' THEN RETURN ag_catalog.pc_float(power(a::float8, b::float8)); END IF;
    IF ints THEN
        RETURN CASE op
            WHEN '-' THEN to_jsonb(a::numeric - b::numeric)
            WHEN '*' THEN to_jsonb(a::numeric * b::numeric)
            WHEN '/' THEN to_jsonb(trunc(a::numeric / b::numeric))
            WHEN '%' THEN to_jsonb(mod(a::numeric, b::numeric))
        END;
    END IF;
    RETURN CASE op
        WHEN '-' THEN ag_catalog.pc_float(a::float8 - b::float8)
        WHEN '*' THEN ag_catalog.pc_float(a::float8 * b::float8)
        WHEN '/' THEN ag_catalog.pc_float(a::float8 / b::float8)
        WHEN '%' THEN ag_catalog.pc_float(a::float8 - b::float8 * trunc(a::float8 / b::float8))
    END;
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_neg(a jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN ag_catalog.pc_isint(a) THEN to_jsonb(-(a::numeric)) ELSE ag_catalog.pc_float(-(a::float8)) END
$$;

-- property of a vertex or edge, or key of a map
CREATE OR REPLACE FUNCTION ag_catalog.pc_prop(j jsonb, key text) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN j ? '~t' THEN j -> 'properties' -> key ELSE j -> key END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_index(j jsonb, i jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN j IS NULL OR i IS NULL THEN NULL
        WHEN jsonb_typeof(j) = 'array' THEN j -> (i::int)
        ELSE ag_catalog.pc_prop(j, i #>> '{}')
    END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_size(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(j)
        WHEN 'array' THEN to_jsonb(jsonb_array_length(j))
        WHEN 'string' THEN to_jsonb(length(j #>> '{}'))
    END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_tostring(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(j)
        WHEN 'string' THEN j
        WHEN 'number' THEN to_jsonb(j::text)
        WHEN 'boolean' THEN to_jsonb(j::text)
    END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_tointeger(j jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF jsonb_typeof(j) = 'number' THEN RETURN to_jsonb(trunc(j::numeric)); END IF;
    IF jsonb_typeof(j) = 'string' THEN
        BEGIN
            RETURN to_jsonb(trunc((j #>> '{}')::numeric));
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_tofloat(j jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF jsonb_typeof(j) = 'number' THEN RETURN ag_catalog.pc_float(j::float8); END IF;
    IF jsonb_typeof(j) = 'string' THEN
        BEGIN
            RETURN ag_catalog.pc_float((j #>> '{}')::float8);
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_setprop(target jsonb, key text, value jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN target IS NULL THEN NULL
        WHEN value IS NULL OR value = 'null'::jsonb THEN jsonb_set(target, '{properties}', (target -> 'properties') - key)
        ELSE jsonb_set(target, ARRAY['properties', key], value)
    END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_props_of(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN j ? '~t' THEN j -> 'properties' ELSE j END $$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_setprops(target jsonb, props jsonb, merge boolean) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN target IS NULL THEN NULL
        WHEN merge THEN jsonb_set(target, '{properties}',
            jsonb_strip_nulls((target -> 'properties') || coalesce(ag_catalog.pc_props_of(props), '{}')))
        ELSE jsonb_set(target, '{properties}', jsonb_strip_nulls(coalesce(ag_catalog.pc_props_of(props), '{}')))
    END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_keys(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_agg(k ORDER BY k), '[]') FROM jsonb_object_keys(ag_catalog.pc_props_of(j)) AS k
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_range(a jsonb, b jsonb, step jsonb DEFAULT '1') RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_agg(to_jsonb(g)), '[]') FROM generate_series(a::bigint, b::bigint, step::bigint) AS g
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_least(a jsonb, b jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN a IS NULL THEN b WHEN b IS NULL THEN a WHEN b < a THEN b ELSE a END $$;
CREATE OR REPLACE FUNCTION ag_catalog.pc_greatest(a jsonb, b jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN a IS NULL THEN b WHEN b IS NULL THEN a WHEN b > a THEN b ELSE a END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'ag_catalog' AND p.proname = 'pc_min') THEN
        CREATE AGGREGATE ag_catalog.pc_min(jsonb) (SFUNC = ag_catalog.pc_least, STYPE = jsonb);
        CREATE AGGREGATE ag_catalog.pc_max(jsonb) (SFUNC = ag_catalog.pc_greatest, STYPE = jsonb);
    END IF;
END
$$;

-- AGE text output: vertices, edges and paths get their ::type suffix, everything else
-- prints like agtype (which follows the jsonb text format).
CREATE OR REPLACE FUNCTION ag_catalog.pc_format(j jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    t text;
BEGIN
    IF j IS NULL OR j = 'null'::jsonb THEN RETURN NULL; END IF;
    CASE jsonb_typeof(j)
    WHEN 'object' THEN
        t := j ->> '~t';
        IF t = 'v' THEN RETURN (j - '~t')::text || '::vertex'; END IF;
        IF t = 'e' THEN RETURN (j - '~t')::text || '::edge'; END IF;
        IF t = 'p' THEN
            RETURN '[' || (SELECT string_agg(ag_catalog.pc_format(x), ', ' ORDER BY n)
                           FROM jsonb_array_elements(j -> 'items') WITH ORDINALITY AS a(x, n)) || ']::path';
        END IF;
        IF strpos(j::text, '"~t"') = 0 THEN RETURN j::text; END IF;
        RETURN '{' || coalesce((SELECT string_agg(to_jsonb(k)::text || ': ' || coalesce(ag_catalog.pc_format(v), 'null'), ', ')
                                FROM jsonb_each(j) AS e(k, v)), '') || '}';
    WHEN 'array' THEN
        IF strpos(j::text, '"~t"') = 0 THEN RETURN j::text; END IF;
        RETURN '[' || coalesce((SELECT string_agg(coalesce(ag_catalog.pc_format(x), 'null'), ', ' ORDER BY n)
                                FROM jsonb_array_elements(j) WITH ORDINALITY AS a(x, n)), '') || ']';
    ELSE
        RETURN j::text;
    END CASE;
END
$$;

CREATE OR REPLACE FUNCTION ag_catalog.pc_assert_no_edges(graph_name text, vertex_id bigint, deleting bigint[]) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    found boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I._edge WHERE (start_id = $1 OR end_id = $1) AND NOT (id = ANY ($2)))', graph_name)
        INTO found USING vertex_id, coalesce(deleting, '{}');
    IF found THEN
        RAISE EXCEPTION 'Cannot delete a vertex that has edge(s). Delete the edge(s) first, or try DETACH DELETE.'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN true;
END
$$;
