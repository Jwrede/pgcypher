"""Translate a parsed Cypher query into one PostgreSQL statement.

Every Cypher value is a jsonb value in SQL. A query becomes a chain of CTE stages; each
stage has one column per bound variable. Writes become data-modifying CTEs that read the
stages, so the whole query runs as a single statement (and a single snapshot).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import parser as P


class TranslateError(Exception):
    pass


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def jlit(value: object) -> str:
    return lit(json.dumps(value)) + "::jsonb"


def truth(sql: str) -> str:
    """SQL boolean from a jsonb expression (NULL stays NULL, non-booleans are false)."""
    return f"(({sql}) = 'true'::jsonb)"


def vertex_json(a: str) -> str:
    return f"jsonb_build_object('~t', 'v', 'id', {a}.id, 'label', {a}.label, 'properties', {a}.properties)"


def edge_json(a: str) -> str:
    return (
        f"jsonb_build_object('~t', 'e', 'id', {a}.id, 'label', {a}.label, 'start_id', {a}.start_id, "
        f"'end_id', {a}.end_id, 'properties', {a}.properties)"
    )


@dataclass
class Binding:
    sql: str
    kind: str  # "v" vertex, "e" edge, "p" path, "x" any value
    alias: str | None = None  # table alias when the variable is a row of _vertex/_edge


Env = dict[str, Binding]


@dataclass
class Written:
    kind: str  # "v" or "e"
    origin: str  # stage where the write happened (decides which entities exist)
    latest: str  # latest stage that still carries the variable (decides the value)
    action: str  # "create", "update", "merge"
    flag: str | None = None  # merge: column that is true when the entity was created


@dataclass
class Stage:
    name: str
    cols: list[str]
    kinds: dict[str, str]


@dataclass
class Translator:
    graph: str
    params_sql: str | None = None
    ctes: list[tuple[str, str, bool]] = field(default_factory=list)
    n: int = 0
    written: dict[str, Written] = field(default_factory=dict)
    deletes: list[tuple[str, P.Expr, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        g = qi(self.graph)
        self.V = f"{g}._vertex"
        self.E = f"{g}._edge"
        self.nextid = f"nextval({lit(g + '._id_seq')})"
        self.ctes.append(("s0", "SELECT", False))
        self.stage = Stage("s0", [], {})
        self.kinds_at: dict[str, dict[str, str]] = {"s0": {}}

    # ------------------------------------------------------------ helpers

    def fresh(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def push(self, sql: str, cols: list[str], kinds: dict[str, str], carried: set[str], materialized: bool = False) -> None:
        name = self.fresh("s")
        self.ctes.append((name, sql, materialized))
        self.stage = Stage(name, cols, kinds)
        self.kinds_at[name] = dict(kinds)
        for var, w in self.written.items():
            if var in carried and var in cols:
                w.latest = name

    def env(self, alias: str = "p") -> Env:
        return {c: Binding(f"{alias}.{qi(c)}", self.stage.kinds[c]) for c in self.stage.cols}

    def prev_cols(self, alias: str = "p") -> str:
        return ", ".join(f"{alias}.{qi(c)}" for c in self.stage.cols)

    # ------------------------------------------------------------ expressions

    def ex(self, e: P.Expr, env: Env) -> str:
        if isinstance(e, P.Literal):
            v = e.value
            if v is None:
                return "NULL::jsonb"
            if isinstance(v, str):
                return f"to_jsonb({lit(v)}::text)"
            if isinstance(v, bool):
                return "'true'::jsonb" if v else "'false'::jsonb"
            return f"{lit(repr(v))}::jsonb"
        if isinstance(e, P.Param):
            if self.params_sql is None:
                raise TranslateError(f"parameter ${e.name} used but no parameter map was passed to cypher()")
            return f"(({self.params_sql})::text::jsonb -> {lit(e.name)})"
        if isinstance(e, P.Var):
            if e.name not in env:
                raise TranslateError(f"variable `{e.name}` does not exist")
            return env[e.name].sql
        if isinstance(e, P.Prop):
            if isinstance(e.base, P.Var) and e.base.name in env:
                b = env[e.base.name]
                if b.alias:
                    return f"({b.alias}.properties -> {lit(e.key)})"
                if b.kind in ("v", "e"):
                    return f"({b.sql} -> 'properties' -> {lit(e.key)})"
            return f"ag_catalog.pc_prop({self.ex(e.base, env)}, {lit(e.key)})"
        if isinstance(e, P.Index):
            return f"ag_catalog.pc_index({self.ex(e.base, env)}, {self.ex(e.index, env)})"
        if isinstance(e, P.ListLit):
            if not e.items:
                return "'[]'::jsonb"
            return "jsonb_build_array(" + ", ".join(self.ex(i, env) for i in e.items) + ")"
        if isinstance(e, P.MapLit):
            return self.map_sql(e, env)
        if isinstance(e, P.UnaryOp):
            x = self.ex(e.operand, env)
            if e.op == "NOT":
                return f"to_jsonb(NOT {truth(x)})"
            return f"ag_catalog.pc_neg({x})"
        if isinstance(e, P.IsNull):
            x = self.ex(e.operand, env)
            return f"to_jsonb(({x}) IS {'NOT ' if e.negate else ''}NULL)"
        if isinstance(e, P.HasLabel):
            return f"to_jsonb(({self.ex(e.operand, env)}) ->> 'label' = {lit(e.label)})"
        if isinstance(e, P.BinOp):
            return self.binop(e, env)
        if isinstance(e, P.CountStar):
            return "to_jsonb(count(*))"
        if isinstance(e, P.Func):
            return self.func(e, env)
        if isinstance(e, P.Case):
            parts = ["CASE"]
            for cond, val in e.whens:
                if e.subject is not None:
                    c = f"(({self.ex(e.subject, env)}) = ({self.ex(cond, env)}))"
                else:
                    c = truth(self.ex(cond, env))
                parts.append(f"WHEN {c} THEN {self.ex(val, env)}")
            if e.default is not None:
                parts.append(f"ELSE {self.ex(e.default, env)}")
            parts.append("END")
            return "(" + " ".join(parts) + ")"
        raise TranslateError(f"unsupported expression {type(e).__name__}")

    def map_sql(self, m: P.MapLit, env: Env) -> str:
        if not m.items:
            return "'{}'::jsonb"
        return "jsonb_build_object(" + ", ".join(f"{lit(k)}, {self.ex(v, env)}" for k, v in m.items) + ")"

    def props_sql(self, props: P.MapLit | P.Param | None, env: Env) -> str | None:
        if props is None:
            return None
        if isinstance(props, P.Param):
            return self.ex(props, env)
        return self.map_sql(props, env)

    def binop(self, e: P.BinOp, env: Env) -> str:
        a = self.ex(e.left, env)
        b = self.ex(e.right, env)
        op = e.op
        if op in ("=", "<>", "<", "<=", ">", ">="):
            return f"to_jsonb(({a}) {op} ({b}))"
        if op == "AND":
            return f"to_jsonb({truth(a)} AND {truth(b)})"
        if op == "OR":
            return f"to_jsonb({truth(a)} OR {truth(b)})"
        if op == "XOR":
            return f"to_jsonb({truth(a)} <> {truth(b)})"
        if op == "+":
            return f"ag_catalog.pc_add({a}, {b})"
        if op in ("-", "*", "/", "%", "^"):
            return f"ag_catalog.pc_arith({lit(op)}, {a}, {b})"
        if op == "IN":
            return f"to_jsonb(({a}) IN (SELECT jsonb_array_elements({b})))"
        sa, sb = f"(({a}) #>> '{{}}')", f"(({b}) #>> '{{}}')"
        if op == "STARTS WITH":
            return f"to_jsonb(starts_with({sa}, {sb}))"
        if op == "ENDS WITH":
            return f"to_jsonb(right({sa}, length({sb})) = {sb})"
        if op == "CONTAINS":
            return f"to_jsonb(strpos({sa}, {sb}) > 0)"
        if op == "=~":
            return f"to_jsonb({sa} ~ ('^(?:' || {sb} || ')$'))"
        raise TranslateError(f"unsupported operator {op}")

    def func(self, f: P.Func, env: Env) -> str:
        name = f.name
        args = [self.ex(a, env) for a in f.args]
        d = "DISTINCT " if f.distinct else ""

        def need(n: int) -> None:
            if len(args) != n:
                raise TranslateError(f"{name}() takes {n} argument(s)")

        def s(i: int) -> str:
            return f"(({args[i]}) #>> '{{}}')"

        if name == "count":
            need(1)
            return f"to_jsonb(count({d}{args[0]}))"
        if name == "collect":
            need(1)
            return f"coalesce(jsonb_agg({d}{args[0]}) FILTER (WHERE ({args[0]}) IS NOT NULL), '[]'::jsonb)"
        if name == "sum":
            need(1)
            return f"coalesce(to_jsonb(sum({d}({args[0]})::numeric)), '0'::jsonb)"
        if name == "avg":
            need(1)
            return f"ag_catalog.pc_float(avg({d}({args[0]})::numeric)::float8)"
        if name in ("min", "max"):
            need(1)
            return f"ag_catalog.pc_{name}({args[0]})"
        if name == "id":
            need(1)
            return f"(({args[0]}) -> 'id')"
        if name in ("label", "type"):
            need(1)
            return f"(({args[0]}) -> 'label')"
        if name == "labels":
            need(1)
            return f"CASE WHEN ({args[0]}) IS NULL THEN NULL ELSE jsonb_build_array(({args[0]}) -> 'label') END"
        if name == "properties":
            need(1)
            return f"ag_catalog.pc_props_of({args[0]})"
        if name == "keys":
            need(1)
            return f"ag_catalog.pc_keys({args[0]})"
        if name == "size":
            need(1)
            return f"ag_catalog.pc_size({args[0]})"
        if name == "length":
            need(1)
            return (
                f"CASE WHEN ({args[0]}) ->> '~t' = 'p' THEN to_jsonb((jsonb_array_length(({args[0]}) -> 'items') - 1) / 2) "
                f"ELSE ag_catalog.pc_size({args[0]}) END"
            )
        if name in ("nodes", "relationships"):
            need(1)
            rem = 1 if name == "nodes" else 0
            return (
                f"(SELECT coalesce(jsonb_agg(x ORDER BY n), '[]'::jsonb) FROM jsonb_array_elements(({args[0]}) -> 'items') "
                f"WITH ORDINALITY AS a(x, n) WHERE n % 2 = {rem})"
            )
        if name in ("startnode", "endnode"):
            need(1)
            col = "start_id" if name == "startnode" else "end_id"
            return f"(SELECT {vertex_json('sv')} FROM {self.V} sv WHERE sv.id = (({args[0]}) ->> {lit(col)})::bigint)"
        if name in ("toupper", "tolower", "trim", "ltrim", "rtrim", "reverse"):
            need(1)
            fn = {"toupper": "upper", "tolower": "lower"}.get(name, name)
            return f"to_jsonb({fn}({s(0)}))"
        if name == "replace":
            need(3)
            return f"to_jsonb(replace({s(0)}, {s(1)}, {s(2)}))"
        if name == "substring":
            if len(args) == 2:
                return f"to_jsonb(substr({s(0)}, ({args[1]})::int + 1))"
            need(3)
            return f"to_jsonb(substr({s(0)}, ({args[1]})::int + 1, ({args[2]})::int))"
        if name in ("left", "right"):
            need(2)
            return f"to_jsonb({name}({s(0)}, ({args[1]})::int))"
        if name == "split":
            need(2)
            return f"to_jsonb(string_to_array({s(0)}, {s(1)}))"
        if name == "tostring":
            need(1)
            return f"ag_catalog.pc_tostring({args[0]})"
        if name == "tointeger":
            need(1)
            return f"ag_catalog.pc_tointeger({args[0]})"
        if name == "tofloat":
            need(1)
            return f"ag_catalog.pc_tofloat({args[0]})"
        if name == "toboolean":
            need(1)
            return f"(CASE lower({s(0)}) WHEN 'true' THEN 'true'::jsonb WHEN 'false' THEN 'false'::jsonb END)"
        if name == "coalesce":
            return "coalesce(" + ", ".join(args) + ")"
        if name == "abs":
            need(1)
            return f"(CASE WHEN ag_catalog.pc_isint({args[0]}) THEN to_jsonb(abs(({args[0]})::numeric)) ELSE ag_catalog.pc_float(abs(({args[0]})::float8)) END)"
        if name in ("ceil", "floor", "round", "sqrt", "exp", "log", "log10", "sin", "cos", "tan"):
            need(1)
            fn = {"log": "ln"}.get(name, name)
            return f"ag_catalog.pc_float({fn}(({args[0]})::float8))"
        if name == "sign":
            need(1)
            return f"to_jsonb(sign(({args[0]})::numeric)::int)"
        if name == "head":
            need(1)
            return f"(({args[0]}) -> 0)"
        if name == "last":
            need(1)
            return f"(({args[0]}) -> -1)"
        if name == "tail":
            need(1)
            return f"(({args[0]}) - 0)"
        if name == "range":
            if len(args) not in (2, 3):
                raise TranslateError("range() takes 2 or 3 arguments")
            return f"ag_catalog.pc_range({', '.join(args)})"
        if name == "exists":
            need(1)
            return f"to_jsonb(({args[0]}) IS NOT NULL)"
        if name == "rand":
            return "ag_catalog.pc_float(random())"
        if name == "timestamp":
            return "to_jsonb((extract(epoch FROM clock_timestamp()) * 1000)::bigint)"
        raise TranslateError(f"function {name}() is not supported")

    # ------------------------------------------------------------ clauses

    def run(self, q: P.Query, columns: list[str] | None) -> str:
        clauses = q.clauses
        final: P.Projection | None = None
        if clauses and isinstance(clauses[-1], P.Projection) and clauses[-1].is_return:
            final = clauses[-1]
            clauses = clauses[:-1]
        for c in clauses:
            if isinstance(c, P.Match):
                self.match(c)
            elif isinstance(c, P.Unwind):
                self.unwind(c)
            elif isinstance(c, P.Projection):
                if c.is_return:
                    raise TranslateError("RETURN must be the last clause")
                self.projection(c)
            elif isinstance(c, P.Create):
                self.create(c)
            elif isinstance(c, P.SetClause):
                self.set_clause(c)
            elif isinstance(c, P.Delete):
                self.delete(c)
            elif isinstance(c, P.Merge):
                self.merge(c)
            else:
                raise TranslateError(f"unsupported clause {type(c).__name__}")
        writes = self.write_ctes()
        if final is None:
            cols = columns or ["a"]
            body = "SELECT " + ", ".join(f"NULL::ag_catalog.agtype AS {qi(c)}" for c in cols) + " WHERE false"
        else:
            body = self.final_return(final, columns)
        parts = [f"{name} AS {'MATERIALIZED ' if mat else ''}({sql})" for name, sql, mat in self.ctes + writes]
        return "WITH " + ",\n".join(parts) + "\n" + body

    # MATCH -----------------------------------------------------------------

    def match(self, c: P.Match) -> None:
        env = self.env("p")
        new: Env = {}
        froms: list[str] = []
        conds: list[str] = []
        uniq: list[tuple[str, str]] = []  # ("id", sql) or ("arr", sql) per relationship

        def scope() -> Env:
            return {**env, **new}

        def node(np: P.NodePat) -> tuple[str, str]:
            """Returns (id sql, json sql) of the node."""
            if len(np.labels) > 1:
                raise TranslateError("a vertex has exactly one label in AGE; multiple labels are not supported")
            var = np.var
            if var and var in new:
                b = new[var]
            elif var and var in env:
                b = env[var]
                if b.kind != "v":
                    raise TranslateError(f"variable `{var}` is not a vertex")
            else:
                a = self.fresh("v")
                froms.append(f"{self.V} AS {a}")
                b = Binding(vertex_json(a), "v", a)
                if var:
                    new[var] = b
            if b.alias:
                idsql, labsql, propsql = f"{b.alias}.id", f"{b.alias}.label", f"{b.alias}.properties"
            else:
                idsql, labsql, propsql = f"(({b.sql}) ->> 'id')::bigint", f"(({b.sql}) ->> 'label')", f"(({b.sql}) -> 'properties')"
            if np.labels:
                conds.append(f"{labsql} = {lit(np.labels[0])}")
            p = self.props_sql(np.props, scope())
            if p:
                conds.append(f"{propsql} @> {p}")
            return idsql, b.sql

        for pat in c.patterns:
            elems = pat.elements
            ids_json = [node(elems[0])]
            path_items = [ids_json[0][1]]
            for i in range(1, len(elems), 2):
                rel: P.RelPat = elems[i]  # type: ignore[assignment]
                left = ids_json[-1][0]
                right, right_json = node(elems[i + 1])  # type: ignore[arg-type]
                ids_json.append((right, right_json))
                if rel.varlen is None:
                    rel_json = self.fixed_rel(rel, left, right, env, new, froms, conds, uniq, scope)
                    path_items += [rel_json, right_json]
                else:
                    if pat.path_var:
                        raise TranslateError("named paths with variable-length relationships are not supported")
                    self.varlen_rel(rel, left, right, env, new, froms, conds, uniq, scope)
            if pat.path_var:
                new[pat.path_var] = Binding(
                    "jsonb_build_object('~t', 'p', 'items', jsonb_build_array(" + ", ".join(path_items) + "))", "p"
                )
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                (ka, a), (kb, b) = uniq[i], uniq[j]
                if ka == "id" and kb == "id":
                    conds.append(f"{a} <> {b}")
                elif ka == "id":
                    conds.append(f"NOT ({a} = ANY ({b}))")
                elif kb == "id":
                    conds.append(f"NOT ({b} = ANY ({a}))")
                else:
                    conds.append(f"NOT ({a} && {b})")
        if c.where is not None:
            conds.append(truth(self.ex(c.where, scope())))
        sel = ", ".join(f"{b.sql} AS {qi(v)}" for v, b in new.items())
        inner = "SELECT " + sel
        if froms:
            inner += " FROM " + ", ".join(froms)
        if conds:
            inner += " WHERE " + " AND ".join(conds)
        join = "LEFT JOIN LATERAL" if c.optional else "CROSS JOIN LATERAL"
        cols_sql = ", ".join(x for x in [self.prev_cols("p"), ", ".join(f"m.{qi(v)}" for v in new)] if x)
        sql = f"SELECT {cols_sql} FROM {self.stage.name} p {join} ({inner}) m" + (" ON true" if c.optional else "")
        kinds = {**self.stage.kinds, **{v: b.kind for v, b in new.items()}}
        self.push(sql, self.stage.cols + list(new), kinds, set(self.stage.cols))

    def rel_conds(self, rel: P.RelPat, a: str, scope) -> list[str]:
        out = []
        if rel.types:
            if len(rel.types) == 1:
                out.append(f"{a}.label = {lit(rel.types[0])}")
            else:
                out.append(f"{a}.label IN ({', '.join(lit(t) for t in rel.types)})")
        p = self.props_sql(rel.props, scope())
        if p:
            out.append(f"{a}.properties @> {p}")
        return out

    def fixed_rel(self, rel, left, right, env, new, froms, conds, uniq, scope) -> str:
        var = rel.var
        if var and var in env:
            b = env[var]
            if b.kind != "e":
                raise TranslateError(f"variable `{var}` is not an edge")
            a = self.fresh("e")
            froms.append(f"{self.E} AS {a}")
            conds.append(f"{a}.id = (({b.sql}) ->> 'id')::bigint")
        elif var and var in new:
            raise TranslateError(f"edge variable `{var}` used twice in one pattern")
        else:
            a = self.fresh("e")
            froms.append(f"{self.E} AS {a}")
        if rel.direction == "out":
            conds += [f"{a}.start_id = {left}", f"{a}.end_id = {right}"]
        elif rel.direction == "in":
            conds += [f"{a}.start_id = {right}", f"{a}.end_id = {left}"]
        else:
            conds.append(f"(({a}.start_id = {left} AND {a}.end_id = {right}) OR ({a}.start_id = {right} AND {a}.end_id = {left}))")
        conds += self.rel_conds(rel, a, scope)
        uniq.append(("id", f"{a}.id"))
        b = Binding(edge_json(a), "e", a)
        if var and var not in env:
            new[var] = b
        return b.sql

    def varlen_rel(self, rel, left, right, env, new, froms, conds, uniq, scope) -> None:
        lo, hi = rel.varlen
        w = self.fresh("w")
        if rel.direction == "out":
            on, nxt = "e.start_id = w.node", "e.end_id"
        elif rel.direction == "in":
            on, nxt = "e.end_id = w.node", "e.start_id"
        else:
            on, nxt = "(e.start_id = w.node OR e.end_id = w.node)", "CASE WHEN e.start_id = w.node THEN e.end_id ELSE e.start_id END"
        extra = self.rel_conds(rel, "e", scope)
        if hi is not None:
            extra.append(f"cardinality(w.edges) < {int(hi)}")
        where = " AND ".join(["NOT (e.id = ANY (w.edges))"] + extra)
        froms.append(
            f"LATERAL (WITH RECURSIVE w(node, edges) AS (SELECT {left}, ARRAY[]::bigint[] UNION ALL "
            f"SELECT {nxt}, w.edges || e.id FROM w JOIN {self.E} e ON {on} WHERE {where}) "
            f"SELECT node, edges FROM w WHERE cardinality(edges) >= {int(lo)}) AS {w}"
        )
        conds.append(f"{w}.node = {right}")
        uniq.append(("arr", f"{w}.edges"))
        if rel.var:
            if rel.var in env or rel.var in new:
                raise TranslateError(f"variable `{rel.var}` already bound")
            new[rel.var] = Binding(
                f"(SELECT coalesce(jsonb_agg({edge_json('x')} ORDER BY u.n), '[]'::jsonb) "
                f"FROM unnest({w}.edges) WITH ORDINALITY AS u(id, n) JOIN {self.E} x ON x.id = u.id)",
                "x",
            )

    # UNWIND ----------------------------------------------------------------

    def unwind(self, c: P.Unwind) -> None:
        x = self.ex(c.expr, self.env("p"))
        cols = ", ".join(v for v in [self.prev_cols("p"), f"u.{qi(c.var)}"] if v)
        sql = (
            f"SELECT {cols} FROM {self.stage.name} p CROSS JOIN LATERAL "
            f"jsonb_array_elements(CASE WHEN jsonb_typeof({x}) = 'array' THEN {x} WHEN ({x}) IS NULL THEN '[]'::jsonb "
            f"ELSE jsonb_build_array({x}) END) AS u({qi(c.var)})"
        )
        cols_list = [col for col in self.stage.cols if col != c.var] + [c.var]
        if c.var in self.stage.cols:
            raise TranslateError(f"variable `{c.var}` already declared")
        self.push(sql, cols_list, {**self.stage.kinds, c.var: "x"}, set(self.stage.cols))

    # WITH / RETURN ------------------------------------------------------------

    def items_of(self, c: P.Projection) -> list[P.ReturnItem]:
        items = []
        if c.star:
            items += [P.ReturnItem(P.Var(v), v, v) for v in self.stage.cols if not v.startswith("__")]
        items += c.items
        if not items:
            raise TranslateError("nothing to project")
        return items

    @staticmethod
    def item_name(it: P.ReturnItem) -> str:
        if it.alias:
            return it.alias
        if isinstance(it.expr, P.Var):
            return it.expr.name
        return it.text

    def has_agg(self, e) -> bool:
        if isinstance(e, P.CountStar):
            return True
        if isinstance(e, P.Func) and e.name in P.AGGREGATES:
            return True
        if isinstance(e, P.Expr):
            for v in vars(e).values():
                if isinstance(v, P.Expr) and self.has_agg(v):
                    return True
                if isinstance(v, list):
                    for x in v:
                        if isinstance(x, P.Expr) and self.has_agg(x):
                            return True
                        if isinstance(x, tuple) and any(isinstance(y, P.Expr) and self.has_agg(y) for y in x):
                            return True
        return False

    def kind_of(self, e: P.Expr, env: Env) -> str:
        if isinstance(e, P.Var) and e.name in env:
            return env[e.name].kind
        if isinstance(e, P.Func) and e.name in ("startnode", "endnode"):
            return "v"
        return "x"

    def project_sql(self, c: P.Projection) -> tuple[str, list[str], dict[str, str], int, set[str]]:
        """SQL of a projection with its order keys as trailing __o columns."""
        env = self.env("p")
        items = self.items_of(c)
        names = [self.item_name(it) for it in items]
        if len(set(names)) != len(names):
            raise TranslateError("duplicate column names in projection")
        sqls = [self.ex(it.expr, env) for it in items]
        kinds = {n: self.kind_of(it.expr, env) for n, it in zip(names, items)}
        carried = {n for n, it in zip(names, items) if isinstance(it.expr, P.Var) and it.expr.name == n}
        agg = any(self.has_agg(it.expr) for it in items)
        sel = ", ".join(f"{s} AS {qi(n)}" for s, n in zip(sqls, names))
        tail = ""
        if c.skip is not None:
            tail += f" OFFSET ({self.ex(c.skip, {})})::numeric::bigint"
        if c.limit is not None:
            tail += f" LIMIT ({self.ex(c.limit, {})})::numeric::bigint"
        if not agg and not c.distinct:
            oenv = {**env, **{n: Binding(s, kinds[n]) for n, s in zip(names, sqls)}}
            okeys = [self.ex(e, oenv) for e, _ in c.order]
            osel = "".join(f", {k} AS __o{i}" for i, k in enumerate(okeys))
            order = ", ".join(f"__o{i}{' DESC' if desc else ''}" for i, (_, desc) in enumerate(c.order))
            sql = f"SELECT {sel}{osel} FROM {self.stage.name} p"
            if order:
                sql += " ORDER BY " + order
            return sql + tail, names, kinds, len(okeys), carried
        group = ""
        if agg:
            keys = [s for s, it in zip(sqls, items) if not self.has_agg(it.expr)]
            if keys:
                group = " GROUP BY " + ", ".join(keys)
        inner = f"SELECT {'DISTINCT ' if c.distinct else ''}{sel} FROM {self.stage.name} p{group}"
        qenv = {n: Binding(f"q.{qi(n)}", kinds[n]) for n in names}
        okeys = []
        for e, _ in c.order:
            match = [n for n, it in zip(names, items) if it.expr == e]
            okeys.append(f"q.{qi(match[0])}" if match else self.ex(e, qenv))
        osel = "".join(f", {k} AS __o{i}" for i, k in enumerate(okeys))
        order = ", ".join(f"__o{i}{' DESC' if desc else ''}" for i, (_, desc) in enumerate(c.order))
        sql = f"SELECT q.*{osel} FROM ({inner}) q"
        if order:
            sql += " ORDER BY " + order
        return sql + tail, names, kinds, len(okeys), carried

    def projection(self, c: P.Projection) -> None:
        sql, names, kinds, _, carried = self.project_sql(c)
        self.push(sql, names, kinds, carried)
        if c.where is not None:
            cond = truth(self.ex(c.where, self.env("p")))
            self.push(f"SELECT {self.prev_cols('p')} FROM {self.stage.name} p WHERE {cond}", names, kinds, set(names))
        else:
            # drop the order key columns
            self.push(f"SELECT {self.prev_cols('p')} FROM {self.stage.name} p", names, kinds, set(names))

    def final_return(self, c: P.Projection, columns: list[str] | None) -> str:
        sql, names, kinds, nkeys, _ = self.project_sql(c)
        cols = columns if columns is not None else names
        if len(cols) != len(names):
            raise TranslateError("return row and column definition list do not match")
        sel = ", ".join(f"ag_catalog.pc_format(r.{qi(n)})::ag_catalog.agtype AS {qi(col)}" for n, col in zip(names, cols))
        order = ", ".join(f"r.__o{i}{' DESC' if desc else ''}" for i, (_, desc) in enumerate(c.order))
        out = f"SELECT {sel} FROM ({sql}) r"
        if order:
            out += " ORDER BY " + order
        return out

    # writes ------------------------------------------------------------------

    def new_vertex(self, np: P.NodePat, env: Env) -> str:
        if len(np.labels) > 1:
            raise TranslateError("a vertex has exactly one label in AGE; multiple labels are not supported")
        label = np.labels[0] if np.labels else ""
        props = self.props_sql(np.props, env)
        props = f"jsonb_strip_nulls({props})" if props else "'{}'::jsonb"
        return f"jsonb_build_object('~t', 'v', 'id', {self.nextid}, 'label', {lit(label)}, 'properties', {props})"

    def create(self, c: P.Create) -> None:
        env = self.env("p")
        node_cols: dict[str, str] = {}
        refs: list[list[str]] = []  # per pattern: column name per element (nodes) or None
        for pat in c.patterns:
            names = []
            for i, el in enumerate(pat.elements):
                if i % 2:
                    names.append("")
                    continue
                np: P.NodePat = el  # type: ignore[assignment]
                if np.var and (np.var in env or np.var in node_cols):
                    if np.labels or np.props:
                        raise TranslateError(f"variable `{np.var}` already exists")
                    if np.var in env and env[np.var].kind != "v":
                        raise TranslateError(f"variable `{np.var}` is not a vertex")
                    names.append(np.var)
                    continue
                col = np.var or self.fresh("__c")
                node_cols[col] = self.new_vertex(np, env)
                names.append(col)
            refs.append(names)
        prev = self.stage.cols
        sel = ", ".join(x for x in [self.prev_cols("p"), ", ".join(f"{s} AS {qi(n)}" for n, s in node_cols.items())] if x)
        kinds = {**self.stage.kinds, **{n: "v" for n in node_cols}}
        self.push(f"SELECT {sel} FROM {self.stage.name} p", prev + list(node_cols), kinds, set(prev), materialized=True)
        for n in node_cols:
            self.written[n] = Written("v", self.stage.name, self.stage.name, "create")
        # edges
        env = self.env("p")
        edge_cols: dict[str, str] = {}
        for pat, names in zip(c.patterns, refs):
            for i in range(1, len(pat.elements), 2):
                rel: P.RelPat = pat.elements[i]  # type: ignore[assignment]
                if len(rel.types) != 1:
                    raise TranslateError("CREATE needs exactly one relationship type")
                if rel.varlen is not None:
                    raise TranslateError("variable-length relationships cannot be created")
                if rel.direction == "both":
                    raise TranslateError("only directed relationships can be created")
                a, b = env[names[i - 1]].sql, env[names[i + 1]].sql
                if rel.direction == "in":
                    a, b = b, a
                if rel.var and (rel.var in env or rel.var in edge_cols):
                    raise TranslateError(f"variable `{rel.var}` already exists")
                col = rel.var or self.fresh("__r")
                props = self.props_sql(rel.props, env)
                props = f"jsonb_strip_nulls({props})" if props else "'{}'::jsonb"
                edge_cols[col] = (
                    f"jsonb_build_object('~t', 'e', 'id', {self.nextid}, 'label', {lit(rel.types[0])}, "
                    f"'start_id', ({a}) -> 'id', 'end_id', ({b}) -> 'id', 'properties', {props})"
                )
                names[i] = col
        if edge_cols:
            prev = self.stage.cols
            sel = self.prev_cols("p") + ", " + ", ".join(f"{s} AS {qi(n)}" for n, s in edge_cols.items())
            kinds = {**self.stage.kinds, **{n: "e" for n in edge_cols}}
            self.push(f"SELECT {sel} FROM {self.stage.name} p", prev + list(edge_cols), kinds, set(prev), materialized=True)
            for n in edge_cols:
                self.written[n] = Written("e", self.stage.name, self.stage.name, "create")
        paths = {pat.path_var: names for pat, names in zip(c.patterns, refs) if pat.path_var}
        if paths:
            env = self.env("p")
            prev = self.stage.cols
            sel = self.prev_cols("p") + ", " + ", ".join(
                "jsonb_build_object('~t', 'p', 'items', jsonb_build_array(" + ", ".join(env[n].sql for n in names) + f")) AS {qi(pv)}"
                for pv, names in paths.items()
            )
            self.push(f"SELECT {sel} FROM {self.stage.name} p", prev + list(paths), {**self.stage.kinds, **{p: "p" for p in paths}}, set(prev))

    def apply_sets(self, items: list[P.SetItem], exprs: dict[str, str], env: Env, guard: str | None = None) -> None:
        for it in items:
            if it.var not in env:
                raise TranslateError(f"variable `{it.var}` does not exist")
            kind = env[it.var].kind
            if kind not in ("v", "e"):
                raise TranslateError(f"SET needs a vertex or edge, `{it.var}` is neither")
            cur = exprs.get(it.var, env[it.var].sql)
            scope = {**env, it.var: Binding(cur, kind)}
            val = self.ex(it.value, scope) if it.value is not None else "NULL::jsonb"
            if it.kind == "prop":
                new = f"ag_catalog.pc_setprop({cur}, {lit(it.key)}, {val})"
            else:
                new = f"ag_catalog.pc_setprops({cur}, {val}, {'true' if it.kind == 'merge' else 'false'})"
            exprs[it.var] = f"(CASE WHEN {guard} THEN {new} ELSE {cur} END)" if guard else new

    def push_sets(self, exprs: dict[str, str]) -> None:
        sel = ", ".join(f"{exprs[c]} AS {qi(c)}" if c in exprs else f"p.{qi(c)}" for c in self.stage.cols)
        self.push(f"SELECT {sel} FROM {self.stage.name} p", list(self.stage.cols), dict(self.stage.kinds), set(self.stage.cols))
        for var in exprs:
            w = self.written.get(var)
            if w is None:
                self.written[var] = Written(self.stage.kinds[var], self.stage.name, self.stage.name, "update")
            elif w.action == "merge":
                w.action = "merge+update"

    def set_clause(self, c: P.SetClause) -> None:
        exprs: dict[str, str] = {}
        self.apply_sets(c.items, exprs, self.env("p"))
        self.push_sets(exprs)

    def delete(self, c: P.Delete) -> None:
        env = self.env("p")
        for e in c.exprs:
            if not isinstance(e, P.Var) or e.name not in env or env[e.name].kind not in ("v", "e"):
                raise TranslateError("DELETE supports vertex and edge variables only")
            self.deletes.append((self.stage.name, e, c.detach))
            self.ex(e, env)

    def merge(self, c: P.Merge) -> None:
        pat = c.pattern
        env = self.env("p")
        if pat.path_var:
            raise TranslateError("named paths in MERGE are not supported")
        if len(pat.elements) == 1:
            np: P.NodePat = pat.elements[0]  # type: ignore[assignment]
            if np.var and np.var in env:
                raise TranslateError(f"variable `{np.var}` already exists")
            if len(np.labels) > 1:
                raise TranslateError("a vertex has exactly one label in AGE; multiple labels are not supported")
            conds = []
            if np.labels:
                conds.append(f"m.label = {lit(np.labels[0])}")
            props = self.props_sql(np.props, env)
            if props:
                conds.append(f"m.properties @> jsonb_strip_nulls({props})")
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            lookup = f"(SELECT {vertex_json('m')} FROM {self.V} m{where} ORDER BY m.id LIMIT 1)"
            build = self.new_vertex(np, env)
            kind = "v"
            var = np.var or self.fresh("__m")
        elif len(pat.elements) == 3:
            a, rel, b = pat.elements
            for np in (a, b):
                if not (np.var and np.var in env and not np.labels and not np.props):  # type: ignore[union-attr]
                    raise TranslateError("MERGE of a relationship needs both end vertices bound by an earlier clause")
            if len(rel.types) != 1 or rel.varlen is not None or rel.direction == "both":  # type: ignore[union-attr]
                raise TranslateError("MERGE needs one directed relationship with exactly one type")
            sa, sb = env[a.var].sql, env[b.var].sql  # type: ignore[union-attr]
            if rel.direction == "in":  # type: ignore[union-attr]
                sa, sb = sb, sa
            if rel.var and rel.var in env:  # type: ignore[union-attr]
                raise TranslateError(f"variable `{rel.var}` already exists")
            props = self.props_sql(rel.props, env)  # type: ignore[union-attr]
            conds = [
                f"m.label = {lit(rel.types[0])}",  # type: ignore[union-attr]
                f"m.start_id = (({sa}) ->> 'id')::bigint",
                f"m.end_id = (({sb}) ->> 'id')::bigint",
            ]
            if props:
                conds.append(f"m.properties @> jsonb_strip_nulls({props})")
            lookup = f"(SELECT {edge_json('m')} FROM {self.E} m WHERE {' AND '.join(conds)} ORDER BY m.id LIMIT 1)"
            pj = f"jsonb_strip_nulls({props})" if props else "'{}'::jsonb"
            build = (
                f"jsonb_build_object('~t', 'e', 'id', {self.nextid}, 'label', {lit(rel.types[0])}, "  # type: ignore[union-attr]
                f"'start_id', ({sa}) -> 'id', 'end_id', ({sb}) -> 'id', 'properties', {pj})"
            )
            kind = "e"
            var = rel.var or self.fresh("__m")  # type: ignore[union-attr]
        else:
            raise TranslateError("MERGE supports a single vertex or a single relationship between bound vertices")
        found = self.fresh("__f")
        flag = self.fresh("__new")
        prev = self.stage.cols
        self.push(
            f"SELECT {', '.join(x for x in [self.prev_cols('p'), f'{lookup} AS {found}'] if x)} FROM {self.stage.name} p",
            prev + [found], {**self.stage.kinds, found: kind}, set(prev), materialized=True,
        )
        cols = [x for x in self.stage.cols if x != found]
        sel = ", ".join([f"p.{qi(x)}" for x in cols] + [
            f"CASE WHEN p.{found} IS NULL THEN {build} ELSE p.{found} END AS {qi(var)}",
            f"(p.{found} IS NULL) AS {flag}",
        ])
        self.push(f"SELECT {sel} FROM {self.stage.name} p", cols + [var, flag],
                  {**{k: self.stage.kinds[k] for k in cols}, var: kind, flag: "x"}, set(cols), materialized=True)
        self.written[var] = Written(kind, self.stage.name, self.stage.name, "merge", flag)
        env = self.env("p")
        exprs: dict[str, str] = {}
        if c.on_create:
            self.apply_sets(c.on_create, exprs, env, guard=f"p.{flag}")
        if c.on_match:
            self.apply_sets(c.on_match, exprs, env, guard=f"NOT p.{flag}")
        if exprs:
            self.push_sets(exprs)

    def write_ctes(self) -> list[tuple[str, str, bool]]:
        out: list[tuple[str, str, bool]] = []
        for var, w in self.written.items():
            q = qi(var)
            if w.flag:
                flag_create, flag_update = f" AND o.{w.flag}", f" AND NOT o.{w.flag}"
            else:
                flag_create = flag_update = ""
            if w.latest == w.origin:
                def src(f: str) -> str:
                    return f"(SELECT DISTINCT ON (o.{q} ->> 'id') o.{q} AS val FROM {w.origin} o WHERE o.{q} IS NOT NULL{f})"
            else:
                def src(f: str) -> str:
                    return (
                        f"(SELECT DISTINCT ON (o.{q} ->> 'id') coalesce(l.val, o.{q}) AS val FROM {w.origin} o "
                        f"LEFT JOIN (SELECT DISTINCT ON (x.{q} ->> 'id') x.{q} ->> 'id' AS id, x.{q} AS val FROM {w.latest} x "
                        f"WHERE x.{q} IS NOT NULL) l ON l.id = o.{q} ->> 'id' WHERE o.{q} IS NOT NULL{f})"
                    )
            table = self.V if w.kind == "v" else self.E
            if w.action in ("create", "merge", "merge+update"):
                f = flag_create
                if w.kind == "v":
                    ins = f"INSERT INTO {table} (id, label, properties) SELECT (val ->> 'id')::bigint, val ->> 'label', val -> 'properties' FROM {src(f)} s"
                else:
                    ins = (
                        f"INSERT INTO {table} (id, label, start_id, end_id, properties) SELECT (val ->> 'id')::bigint, val ->> 'label', "
                        f"(val ->> 'start_id')::bigint, (val ->> 'end_id')::bigint, val -> 'properties' FROM {src(f)} s"
                    )
                out.append((self.fresh("w_ins"), ins, False))
            if w.action in ("update", "merge+update"):
                f = flag_update
                upd = f"UPDATE {table} t SET properties = s.val -> 'properties' FROM {src(f)} s WHERE t.id = (s.val ->> 'id')::bigint"
                out.append((self.fresh("w_upd"), upd, False))
        if self.deletes:
            def ids(kind: str, detach: bool | None) -> str:
                parts = []
                for stage, e, det in self.deletes:
                    k = self.kind_in(stage, e)
                    if k != kind or (detach is not None and det != detach):
                        continue
                    x = f"s.{qi(e.name)}"  # type: ignore[attr-defined]
                    parts.append(f"SELECT (({x}) ->> 'id')::bigint AS id FROM {stage} s WHERE {x} IS NOT NULL")
                return " UNION ".join(parts) if parts else "SELECT NULL::bigint AS id WHERE false"
            de, dvd, dvn = self.fresh("d_e"), self.fresh("d_vd"), self.fresh("d_vn")
            out.append((de, ids("e", None), False))
            out.append((dvd, ids("v", True), False))
            out.append((dvn, ids("v", False), False))
            out.append((self.fresh("w_del"),
                        f"DELETE FROM {self.E} WHERE id IN (SELECT id FROM {de}) OR start_id IN (SELECT id FROM {dvd}) "
                        f"OR end_id IN (SELECT id FROM {dvd})", False))
            out.append((self.fresh("w_del"),
                        f"DELETE FROM {self.V} WHERE id IN (SELECT id FROM {dvd}) OR (id IN (SELECT id FROM {dvn}) AND "
                        f"ag_catalog.pc_assert_no_edges({lit(self.graph)}, id, (SELECT array_agg(id) FROM {de})))", False))
        return out

    def kind_in(self, stage: str, e: P.Expr) -> str:
        return self.kinds_at[stage][e.name]  # type: ignore[attr-defined]


def translate(graph: str, cypher: str, columns: list[str] | None = None, params_sql: str | None = None) -> str:
    """Translate one Cypher query for `graph` into a PostgreSQL statement.

    `columns` are the names of the `AS (...)` column definition list of the cypher() call;
    `params_sql` is the SQL expression of its third argument (the parameter map).
    """
    return Translator(graph, params_sql).run(P.parse(cypher), columns)
