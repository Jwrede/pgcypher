"""Run the same SQL with cypher() calls against Apache AGE and against pgcypher on plain
PostgreSQL, and compare the rows after normalising graph ids (AGE ids are graphids)."""

import json
import re
import uuid

import psycopg
import pytest

from pgcypher.rewrite import rewrite_sql

DATA = """
CREATE (alice:Person {name: 'Alice', age: 30, city: 'Berlin'}),
       (bob:Person {name: 'Bob', age: 42, city: 'Hamburg'}),
       (carol:Person {name: 'Carol', age: 25, city: 'Berlin'}),
       (dave:Person {name: 'Dave', age: 51}),
       (acme:Company {name: 'Acme', founded: 1999}),
       (pizza:Food {name: 'Pizza', price: 9.5}),
       (alice)-[:KNOWS {since: 2015}]->(bob),
       (bob)-[:KNOWS {since: 2018}]->(carol),
       (carol)-[:KNOWS {since: 2020}]->(dave),
       (alice)-[:WORKS_AT {role: 'engineer'}]->(acme),
       (bob)-[:WORKS_AT {role: 'manager'}]->(acme),
       (carol)-[:LIKES]->(pizza),
       (dave)-[:LIKES]->(pizza)
"""

# (cypher, columns)
READS = [
    ("MATCH (n:Person) RETURN n ORDER BY n.name", ["n"]),
    ("MATCH (n) RETURN n.name, labels(n), label(n) ORDER BY n.name", ["name", "labels", "label"]),
    ("MATCH (n:Person {city: 'Berlin'}) RETURN n.name ORDER BY n.name", ["name"]),
    ("MATCH (a)-[r:KNOWS]->(b) RETURN a.name, r, b.name ORDER BY a.name", ["a", "r", "b"]),
    ("MATCH (a)<-[r:KNOWS]-(b) RETURN a.name, b.name, r.since ORDER BY a.name", ["a", "b", "s"]),
    ("MATCH (a:Person)-[:KNOWS]-(b:Person) RETURN a.name, b.name ORDER BY a.name, b.name", ["a", "b"]),
    ("MATCH (a {name: 'Alice'})-[:KNOWS*1..3]->(b) RETURN b.name ORDER BY b.name", ["b"]),
    ("MATCH (a {name: 'Alice'})-[:KNOWS*2]->(b) RETURN b.name", ["b"]),
    ("MATCH (a {name: 'Dave'})<-[:KNOWS*]-(b) RETURN b.name ORDER BY b.name", ["b"]),
    ("MATCH (a {name: 'Alice'})-[*]-(b) RETURN DISTINCT b.name ORDER BY b.name", ["b"]),
    ("MATCH p = (a:Person)-[:WORKS_AT]->(c) RETURN p, length(p) ORDER BY a.name", ["p", "l"]),
    ("MATCH (a)-[:KNOWS]->(b)-[:KNOWS]->(c) RETURN a.name, b.name, c.name ORDER BY a.name", ["a", "b", "c"]),
    ("MATCH (a)-[:KNOWS]->(b), (b)-[:WORKS_AT]->(c) RETURN a.name, c.name", ["a", "c"]),
    ("MATCH (n:Person) WHERE n.age > 28 AND n.age < 50 RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) WHERE n.city IS NULL OR n.name STARTS WITH 'C' RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) WHERE n.name IN ['Bob', 'Dave', 'Zed'] RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) WHERE n.name =~ 'A.*' OR n.name ENDS WITH 'e' RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) WHERE n.name CONTAINS 'a' AND NOT n.age = 51 RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) RETURN count(n), sum(n.age), min(n.age), max(n.age), avg(n.age)", ["c", "s", "mi", "ma", "av"]),
    ("MATCH (n:Person) RETURN n.city, count(*) AS c ORDER BY c DESC, n.city", ["city", "c"]),
    ("MATCH (n:Person)-[:KNOWS]->(m) RETURN n.name, collect(m.name) ORDER BY n.name", ["n", "friends"]),
    ("MATCH (n:Person) RETURN count(DISTINCT n.city)", ["c"]),
    ("MATCH (n:Person) RETURN n.name ORDER BY n.age DESC SKIP 1 LIMIT 2", ["n"]),
    ("MATCH (n:Person) WITH n WHERE n.age > 26 RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person) WITH n.city AS city, count(n) AS c WHERE c > 1 RETURN city, c", ["city", "c"]),
    ("MATCH (n:Person) WITH n ORDER BY n.age LIMIT 2 RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (a:Person) OPTIONAL MATCH (a)-[:LIKES]->(f) RETURN a.name, f.name ORDER BY a.name", ["a", "f"]),
    ("MATCH (a:Person) OPTIONAL MATCH (a)-[:WORKS_AT]->(c) WHERE c.founded > 2000 RETURN a.name, c ORDER BY a.name", ["a", "c"]),
    ("UNWIND [3, 1, 2] AS x RETURN x ORDER BY x", ["x"]),
    ("UNWIND [1, 2, 3] AS x RETURN x + 1, x - 1, x * 3, x / 2, x % 2, x / 2.0, -x", ["a", "b", "c", "d", "e", "f", "g"]),
    ("RETURN 'a' + 'b', 'n' + 1, [1, 2] + [3], [1] + 2, 1.5 + 1, 2 ^ 3", ["a", "b", "c", "d", "e", "f"]),
    ("RETURN toUpper('abc'), toLower('ABC'), trim('  x '), replace('aXa', 'X', 'b'), substring('hello', 1, 3), split('a,b', ',')", ["a", "b", "c", "d", "e", "f"]),
    ("RETURN size('hello'), size([1, 2, 3]), head([1, 2]), last([1, 2]), tail([1, 2, 3]), range(1, 4)", ["a", "b", "c", "d", "e", "f"]),
    ("RETURN toString(12), toInteger('42'), toInteger(3.7), toFloat('1.5'), coalesce(null, 'x'), abs(-3)", ["a", "b", "c", "d", "e", "f"]),
    ("RETURN {a: 1, b: 'x', c: [1, 2], d: {e: true}}, [1, 'two', null, 3.5], null, true, false", ["a", "b", "c", "d", "e"]),
    ("RETURN CASE WHEN 1 < 2 THEN 'yes' ELSE 'no' END, CASE 2 WHEN 1 THEN 'one' WHEN 2 THEN 'two' END", ["a", "b"]),
    ("MATCH (n:Person {name: 'Alice'}) RETURN properties(n), keys(n), n.missing, n['age']", ["p", "k", "m", "a"]),
    ("MATCH (a)-[r]->(b:Food) RETURN type(r), a.name ORDER BY a.name", ["t", "a"]),
    ("MATCH (n:Person) RETURN DISTINCT n.city ORDER BY n.city", ["city"]),
    ("MATCH (n:Person) WHERE n.age >= $min RETURN n.name ORDER BY n.name", ["n"]),
    ("MATCH (n:Person)-[r]->() RETURN n.name, count(r) AS c ORDER BY n.name", ["n", "c"]),
    ("MATCH (n:Person) RETURN collect(n.name)[0] IS NOT NULL, size(collect(n))", ["a", "b"]),
]

# sequences of statements; each runs on a fresh copy of DATA
WRITES = [
    [
        ("MATCH (n:Person {name: 'Alice'}) SET n.age = n.age + 1, n.nick = 'Al' RETURN n", ["n"]),
        ("MATCH (n:Person {name: 'Alice'}) RETURN n.age, n.nick", ["a", "b"]),
    ],
    [
        ("MATCH (n:Person {name: 'Bob'}) SET n += {city: 'Kiel', age: null} RETURN n", ["n"]),
        ("MATCH (n:Person {name: 'Bob'}) REMOVE n.city RETURN n", ["n"]),
        ("MATCH (n:Person {name: 'Bob'}) RETURN properties(n)", ["p"]),
    ],
    [
        ("CREATE (x:Person {name: 'Eve', age: 33})-[:KNOWS]->(y:Person {name: 'Frank'}) RETURN x, y", ["x", "y"]),
        ("MATCH (a {name: 'Eve'})-[r:KNOWS]->(b) RETURN a.name, b.name", ["a", "b"]),
    ],
    [
        ("MATCH (a:Person {name: 'Dave'}), (c:Company) CREATE (a)-[r:WORKS_AT {role: 'intern'}]->(c) RETURN r.role", ["r"]),
        ("MATCH (a)-[r:WORKS_AT]->(c) RETURN a.name, r.role ORDER BY a.name", ["a", "r"]),
    ],
    [
        ("MERGE (n:Person {name: 'Alice'}) ON MATCH SET n.seen = true RETURN n.name, n.seen", ["a", "b"]),
        ("MERGE (n:Person {name: 'Zoe'}) ON CREATE SET n.new = true RETURN n.name, n.new", ["a", "b"]),
        ("MERGE (n:Person {name: 'Zoe'}) ON CREATE SET n.new = false RETURN n.name, n.new", ["a", "b"]),
        ("MATCH (n:Person) RETURN count(n)", ["c"]),
    ],
    [
        ("MATCH (a {name: 'Alice'}), (d {name: 'Dave'}) MERGE (a)-[r:KNOWS]->(d) RETURN type(r)", ["t"]),
        ("MATCH (a {name: 'Alice'}), (d {name: 'Dave'}) MERGE (a)-[r:KNOWS]->(d) RETURN type(r)", ["t"]),
        ("MATCH (a {name: 'Alice'})-[r:KNOWS]->(d) RETURN d.name ORDER BY d.name", ["d"]),
    ],
    [
        ("MATCH ()-[r:LIKES]->() DELETE r", ["a"]),
        ("MATCH (n:Food) DELETE n", ["a"]),
        ("MATCH (n) RETURN count(n)", ["c"]),
    ],
    [
        ("MATCH (n {name: 'Bob'}) DETACH DELETE n", ["a"]),
        ("MATCH (n) RETURN n.name ORDER BY n.name", ["n"]),
        ("MATCH ()-[r]->() RETURN count(r)", ["c"]),
    ],
    [
        ("UNWIND ['x', 'y'] AS name CREATE (n:Tag {name: name}) RETURN n.name ORDER BY n.name", ["n"]),
        ("MATCH (n:Tag) RETURN n.name ORDER BY n.name", ["n"]),
    ],
]

ERRORS = [
    "MATCH (n {name: 'Alice'}) DELETE n",  # vertex with edges
]


def normalise(rows):
    ids: dict = {}

    def canon(v):
        return ids.setdefault(v, len(ids) + 1)

    def walk(x):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                out[k] = canon(v) if k in ("id", "start_id", "end_id") and "~t" in x else walk(v)
            return out
        if isinstance(x, list):
            return [walk(i) for i in x]
        return x

    def cell(text):
        if text is None:
            return None
        s = re.sub(r"\}::vertex", ', "~t": "v"}', text)
        s = re.sub(r"\}::edge", ', "~t": "e"}', s)
        s = re.sub(r"\]::path", "]", s)
        return json.loads(s)

    return [tuple(walk(cell(c)) for c in row) for row in rows]


def sql_for(graph, cypher, columns, params):
    cols = ", ".join(f"{c} agtype" for c in columns)
    if params is None:
        return f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS ({cols})"
    return f"SELECT * FROM cypher('{graph}', $$ {cypher} $$, %s) AS ({cols})"


def run_age(conn, graph, cypher, columns, params=None):
    sql = sql_for(graph, cypher, columns, params)
    cur = conn.execute(sql, (json.dumps(params),) if params is not None else None)
    return cur.fetchall() if cur.description else []


def run_pgc(conn, graph, cypher, columns, params=None):
    sql = rewrite_sql(sql_for(graph, cypher, columns, params))
    cur = conn.execute(sql, (json.dumps(params),) if params is not None else None)
    return cur.fetchall() if cur.description else []


@pytest.fixture()
def pair(app_dsn, age_dsn):
    graph = "g" + uuid.uuid4().hex[:8]
    age = psycopg.connect(age_dsn, autocommit=True)
    age.execute("LOAD 'age'")
    age.execute('SET search_path = ag_catalog, "$user", public')
    age.execute("SELECT create_graph(%s)", (graph,))
    pgc = psycopg.connect(app_dsn, autocommit=True)
    pgc.execute("SELECT ag_catalog.create_graph(%s)", (graph,))
    run_age(age, graph, DATA, ["a"])
    run_pgc(pgc, graph, DATA, ["a"])
    yield graph, age, pgc
    age.execute("SELECT drop_graph(%s, true)", (graph,))
    pgc.execute("SELECT ag_catalog.drop_graph(%s, true)", (graph,))
    age.close()
    pgc.close()


PARAMS = {"min": 30}


@pytest.mark.parametrize("cypher,columns", READS, ids=[q for q, _ in READS])
def test_read_matches_age(pair, cypher, columns):
    graph, age, pgc = pair
    params = PARAMS if "$" in cypher else None
    expected = run_age(age, graph, cypher, columns, params)
    got = run_pgc(pgc, graph, cypher, columns, params)
    assert normalise(got) == normalise(expected)


@pytest.mark.parametrize("steps", WRITES, ids=[s[0][0] for s in WRITES])
def test_writes_match_age(pair, steps):
    graph, age, pgc = pair
    for cypher, columns in steps:
        expected = run_age(age, graph, cypher, columns)
        got = run_pgc(pgc, graph, cypher, columns)
        assert normalise(got) == normalise(expected), cypher


@pytest.mark.parametrize("cypher", ERRORS)
def test_errors_match_age(pair, cypher):
    graph, age, pgc = pair
    with pytest.raises(psycopg.Error) as age_err:
        run_age(age, graph, cypher, ["a"])
    with pytest.raises(psycopg.Error) as pgc_err:
        run_pgc(pgc, graph, cypher, ["a"])
    assert "Cannot delete a vertex that has edge(s)" in str(age_err.value)
    assert "Cannot delete a vertex that has edge(s)" in str(pgc_err.value)
