"""An AGE client session through the proxy against plain PostgreSQL."""

import json

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from test_proxy import start_proxy


def via_proxy(dsn: str, port: int) -> str:
    info = conninfo_to_dict(dsn)
    info.update(host="127.0.0.1", port=port)
    return make_conninfo(**info)


def test_age_client_session_through_proxy(app_dsn):
    port = start_proxy("prefer")
    with psycopg.connect(via_proxy(app_dsn, port), autocommit=True) as conn:
        # what AGE setup code usually runs first
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        conn.execute("LOAD 'age'")
        conn.execute('SET search_path = ag_catalog, "$user", public')
        conn.execute("SELECT create_graph('shop')")
        assert conn.execute("SELECT count(*) FROM ag_graph WHERE name = 'shop'").fetchone()[0] == 1
        conn.execute(
            "SELECT * FROM cypher('shop', $$ CREATE (:Customer {name: 'Ann'})-[:BOUGHT {qty: 2}]->(:Product {sku: 'A1'}) $$) AS (a agtype)"
        )
        # parameters arrive as an agtype map in the third argument (extended protocol, $1)
        rows = conn.execute(
            "SELECT * FROM cypher('shop', $$ MATCH (c:Customer {name: $name})-[b:BOUGHT]->(p) RETURN p.sku, b.qty $$, %s) AS (sku agtype, qty agtype)",
            (json.dumps({"name": "Ann"}),),
        ).fetchall()
        assert rows == [('"A1"', "2")]
        # prepared and reused
        q = "SELECT * FROM cypher('shop', $$ MATCH (c:Customer) WHERE c.name = $name RETURN c $$, %s) AS (c agtype)"
        for _ in range(3):
            row = conn.execute(q, (json.dumps({"name": "Ann"}),), prepare=True).fetchone()
            assert row[0].endswith("::vertex") and '"name": "Ann"' in row[0]
        # cypher() inside a larger read query
        n = conn.execute(
            "SELECT count(*) FROM cypher('shop', $$ MATCH (n) RETURN n $$) AS t(n agtype) WHERE t.n IS NOT NULL"
        ).fetchone()[0]
        assert n == 2
        # a Cypher syntax error is reported and the session stays usable
        try:
            conn.execute("SELECT * FROM cypher('shop', $$ MATCH (n RETURN n $$) AS (n agtype)")
            raise AssertionError("expected an error")
        except psycopg.errors.SyntaxError as exc:
            assert "expected" in str(exc)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        # writes inside an explicit transaction roll back
        with conn.transaction():
            conn.execute("SELECT * FROM cypher('shop', $$ CREATE (:Customer {name: 'Tmp'}) $$) AS (a agtype)")
            conn.execute("ROLLBACK")
        rows = conn.execute("SELECT * FROM cypher('shop', $$ MATCH (c:Customer) RETURN c.name $$) AS (n agtype)").fetchall()
        assert rows == [('"Ann"',)]
        conn.execute("SELECT drop_graph('shop', true)")
