import pytest

from pgcypher.parser import CypherSyntaxError, parse
from pgcypher.rewrite import NOOP, RewriteError, rewrite_sql
from pgcypher.translate import TranslateError, translate


def test_noop_statements():
    assert rewrite_sql("LOAD 'age'") == NOOP
    assert rewrite_sql("load '$libdir/plugins/age';").startswith("DO")
    assert rewrite_sql("CREATE EXTENSION IF NOT EXISTS age") == NOOP
    assert rewrite_sql("SELECT 1") == "SELECT 1"
    assert rewrite_sql("SELECT 'cypher(' AS x") == "SELECT 'cypher(' AS x"


def test_whole_statement_is_replaced():
    sql = rewrite_sql("SELECT * FROM cypher('g', $$ MATCH (n) RETURN n $$) AS (n agtype)")
    assert sql.startswith("WITH s0 AS")
    assert '"g"._vertex' in sql


def test_embedded_call_keeps_alias():
    sql = rewrite_sql("SELECT t.n FROM ag_catalog.cypher('g', 'MATCH (n) RETURN n.name') AS t(n agtype) WHERE 1 = 1")
    assert sql.startswith("SELECT t.n FROM (WITH")
    assert sql.endswith(') AS t("n") WHERE 1 = 1')


def test_embedded_write_is_rejected():
    with pytest.raises(RewriteError, match="whole statement"):
        rewrite_sql("SELECT count(*) FROM cypher('g', $$ CREATE (n) RETURN n $$) AS (n agtype)")


def test_column_count_must_match():
    with pytest.raises(RewriteError, match="column definition list"):
        rewrite_sql("SELECT * FROM cypher('g', $$ MATCH (n) RETURN n, n.x $$) AS (n agtype)")


def test_params_need_a_map():
    with pytest.raises(TranslateError, match="parameter"):
        translate("g", "MATCH (n) WHERE n.x = $x RETURN n")
    assert "$1" in translate("g", "MATCH (n) WHERE n.x = $x RETURN n", params_sql="$1")


@pytest.mark.parametrize("q", ["MATCH (n RETURN n", "RETURN", "MATCH (a)<-[]->(b) RETURN a", "MATCH (n) RETURN n ORDER"])
def test_syntax_errors(q):
    with pytest.raises(CypherSyntaxError):
        parse(q)


@pytest.mark.parametrize(
    "q,msg",
    [
        ("MATCH (n:A:B) RETURN n", "one label"),
        ("RETURN x", "does not exist"),
        ("CREATE (a)-[:T]-(b)", "directed"),
        ("CREATE (a)-[r]->(b)", "exactly one relationship type"),
        ("MATCH (n) RETURN foo(n)", "not supported"),
        ("MATCH p = (a)-[*]->(b) RETURN p", "variable-length"),
    ],
)
def test_unsupported(q, msg):
    with pytest.raises(TranslateError, match=msg):
        translate("g", q)
