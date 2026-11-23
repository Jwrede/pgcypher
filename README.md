# pgcypher

Run Apache AGE style Cypher queries on managed PostgreSQL (Amazon RDS, Aurora, Google Cloud SQL)
where the AGE extension cannot be installed.

pgcypher has three parts:

- **A catalog in plain SQL and PL/pgSQL** (`pgcypher install`). It creates the `ag_catalog` schema
  with `create_graph()`, `drop_graph()`, an `agtype` domain and helper functions. Every graph is a
  schema with a `_vertex` and an `_edge` table (jsonb properties, GIN and btree indexes). Nothing
  needs superuser rights or a C extension, so it installs with the normal database owner role.
- **A Cypher to SQL translator.** A Cypher query becomes one PostgreSQL statement: a chain of CTEs
  for `MATCH`, `WITH` and `UNWIND`, recursive CTEs for variable-length paths, data-modifying CTEs
  for `CREATE`, `MERGE`, `SET`, `REMOVE` and `DELETE`. Results are printed in AGE's text format
  (`{...}::vertex`, `{...}::edge`, `[...]::path`).
- **A PostgreSQL wire protocol proxy** (`pgcypher proxy`). Applications keep their AGE SQL
  (`LOAD 'age'`, `SELECT * FROM cypher('graph', $$ ... $$) AS (a agtype)`) and connect to the proxy
  instead of the database. The proxy rewrites `cypher()` calls (simple and extended protocol,
  including `$1` parameter maps), turns `LOAD 'age'` and `CREATE EXTENSION age` into no-ops, and
  passes everything else through unchanged. Authentication is relayed to the server (SCRAM works;
  the proxy speaks TLS to the server and plain TCP to the client).

## Why

Apache AGE adds Cypher to PostgreSQL, but it is a C extension, and the large managed PostgreSQL
services do not offer it:

- [apache/age#998](https://github.com/apache/age/issues/998) "Add Support for Apache AGE on Amazon
  RDS": 119 reactions, 54 comments, open since June 2023. A member of the AGE project explains the blocker
  there: RDS only accepts extensions in trusted languages, and AGE is written in C; the suggested
  workaround is to run PostgreSQL yourself on EC2 instead of RDS
  ([comment](https://github.com/apache/age/issues/998#issuecomment-3289999747)). In August 2026 a
  user wrote that their migration to AWS means rewriting their code for Neptune.
- Google Cloud SQL has the same gap ([issue tracker 288350484](https://issuetracker.google.com/issues/288350484),
  listed in [apache/age discussion #2224](https://github.com/apache/age/discussions/2224)). According to
  that discussion, Azure Database for PostgreSQL and Alibaba Cloud ship AGE.
- The existing Cypher to SQL translators are archived or unmaintained and target other databases
  ([microsoft/openCypherTranspiler](https://github.com/microsoft/openCypherTranspiler), T-SQL, read
  only, last change 2022; [cytosm](https://github.com/cytosm/cytosm), 2018). Commercial graph query
  layers over relational databases exist; I did not find an open source one for PostgreSQL that
  keeps AGE's SQL interface.

## Install

```sh
pip install git+https://github.com/Jwrede/pgcypher
pgcypher install "postgresql://app:secret@mydb.xxxx.eu-central-1.rds.amazonaws.com/app"
pgcypher proxy --listen 127.0.0.1:5433 --upstream mydb.xxxx.eu-central-1.rds.amazonaws.com:5432
```

`pgcypher schema` prints the catalog SQL if you prefer to review it and run it with `psql`.
`--upstream-ssl` is `prefer` by default (TLS without certificate check); use `verify-full` to check
the server certificate against the system trust store, or `require` to refuse plain connections.

## Example

`examples/demo.sql`, run with `psql` through the proxy against a plain PostgreSQL 16 server
(output copied from a local run; CI runs the same file):

```
$ psql "postgresql://demo@127.0.0.1:5433/demo" -f examples/demo.sql
...
  name   |                                friend
---------+-----------------------------------------------------------------------
 "Bob"   | {"id": 2, "label": "Person", "properties": {"name": "Bob"}}::vertex
 "Carol" | {"id": 3, "label": "Person", "properties": {"name": "Carol"}}::vertex
(2 rows)

 person  | knows |   names
---------+-------+-----------
 "Alice" | 1     | ["Bob"]
 "Bob"   | 1     | ["Carol"]
(2 rows)
```

Without the proxy, `pgcypher translate -g demo "MATCH (n) RETURN n"` prints the SQL and
`pgcypher query <dsn> -g demo "MATCH (n) RETURN n"` runs a query directly.

## Supported Cypher

- `MATCH`, `OPTIONAL MATCH`, `WHERE`, `WITH` (with `WHERE`, `ORDER BY`, `SKIP`, `LIMIT`), `UNWIND`,
  `RETURN` (`DISTINCT`, `ORDER BY`, `SKIP`, `LIMIT`, `*`)
- patterns with labels and property maps, directed and undirected relationships, alternative
  types (`[:A|B]`), variable-length relationships (`*`, `*2`, `*1..3`, `*..3`), named paths of
  fixed length, relationship uniqueness within a `MATCH`
- `CREATE`, `MERGE` (one vertex, or one relationship between bound vertices) with `ON CREATE SET`
  and `ON MATCH SET`, `SET n.p = ...`, `SET n = {...}`, `SET n += {...}`, `REMOVE n.p`, `DELETE`,
  `DETACH DELETE` (deleting a vertex that still has edges fails, with AGE's error message)
- operators: comparison, `AND`/`OR`/`XOR`/`NOT`, `IS [NOT] NULL`, `IN`, `STARTS WITH`, `ENDS WITH`,
  `CONTAINS`, `=~`, `+ - * / % ^` (integer and float arithmetic kept apart as in AGE), list and map
  literals, indexing, `CASE`, parameters (`$name` from the third argument of `cypher()`)
- functions: `count` (also `count(*)`, `DISTINCT`), `collect`, `sum`, `avg`, `min`, `max`, `id`,
  `label`, `labels`, `type`, `properties`, `keys`, `size`, `length`, `nodes`, `relationships`,
  `startNode`, `endNode`, `toUpper`, `toLower`, `trim`, `ltrim`, `rtrim`, `replace`, `substring`,
  `left`, `right`, `split`, `reverse`, `toString`, `toInteger`, `toFloat`, `toBoolean`, `coalesce`,
  `abs`, `ceil`, `floor`, `round`, `sqrt`, `sign`, `exp`, `log`, `head`, `last`, `tail`, `range`,
  `exists`, `rand`, `timestamp`

## Tests

The main test is differential: `tests/test_differential.py` loads the same graph into Apache AGE
1.8.0 (PostgreSQL 18, Docker image `apache/age:release_PG18_1.8.0`) and into pgcypher on a plain
PostgreSQL, runs 42 read queries, 9 write sequences and one error case as AGE SQL on both, and
compares the rows after mapping graph ids (AGE ids are graphids, pgcypher ids are sequence
numbers). pgcypher runs as a role without superuser rights, as on RDS. Further tests cover the
proxy (password login, TLS to the server, prepared statements, errors in the simple and extended
protocol, transactions, cancel), an AGE client session through the proxy, and the SQL rewriting.
CI runs everything with pgcypher on PostgreSQL 14, 16 and 18, plus `examples/demo.sql` through the
proxy with `psql`.

## Limitations

- Not tested against RDS, Aurora or Cloud SQL themselves. The tests use plain PostgreSQL with a
  non-superuser role, which is the restriction that matters there.
- No performance comparison with AGE has been done. Property filters use jsonb containment with a
  GIN index, and variable-length paths are recursive CTEs that enumerate edge-unique paths, which
  gets expensive on dense graphs with unbounded `*`.
- Storage is not AGE's: there are no per-label tables and ids are plain bigints. pgcypher is meant
  for applications that talk to AGE through `cypher()`; SQL that reads AGE's label tables directly
  does not work. Existing AGE data has to be exported and loaded again.
- A query sees the database as it was when the query started. A `MATCH` after a write clause in the
  same query does not see that query's own writes, two `MERGE`s of the same new entity in one query
  create two, and the same row updated through two variables in one query keeps one update.
- Not supported: multiple labels per vertex (AGE has one label too), label changes with `SET`,
  `UNION`, `CALL`, list comprehensions and pattern predicates, `MERGE` of longer patterns, named
  paths with variable-length parts, AGE's `agtype` operators and functions in surrounding SQL.
- Values come back as text. `agtype` is a domain over `text` (a real base type would need C), so
  drivers that parse `agtype` by type OID see strings. The official `apache-age-python` driver also
  calls `age_prepare_cypher()`, which pgcypher does not provide.
- The proxy has no TLS towards the client; run it on localhost or a private network next to the
  application.
- Number formatting follows AGE for the tested cases; floats outside the 1e15 range and NaN or
  infinity are not handled like AGE.

## License

MIT
