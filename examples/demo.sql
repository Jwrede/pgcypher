-- Run with: psql "postgresql://demo:demo@127.0.0.1:5433/demo" -f examples/demo.sql
-- (port 5433 is the pgcypher proxy in front of a plain PostgreSQL)
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('demo');

SELECT * FROM cypher('demo', $$
  CREATE (a:Person {name: 'Alice'})-[:KNOWS {since: 2015}]->(b:Person {name: 'Bob'}),
         (b)-[:KNOWS {since: 2018}]->(:Person {name: 'Carol'})
$$) AS (a agtype);

SELECT * FROM cypher('demo', $$
  MATCH (a:Person {name: 'Alice'})-[:KNOWS*1..2]->(f)
  RETURN f.name, f
  ORDER BY f.name
$$) AS (name agtype, friend agtype);

SELECT * FROM cypher('demo', $$
  MATCH (p:Person)-[k:KNOWS]->(q)
  RETURN p.name, count(k) AS knows, collect(q.name) AS names
  ORDER BY p.name
$$) AS (person agtype, knows agtype, names agtype);

SELECT drop_graph('demo', true);
