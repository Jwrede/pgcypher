import os
import uuid

import psycopg
import pytest

from pgcypher.cli import schema_sql

PG_DSN = os.environ.get("PGCYPHER_TEST_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")
AGE_DSN = os.environ.get("PGCYPHER_AGE_DSN", "postgresql://postgres:postgres@127.0.0.1:55433/postgres")


def reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn, connect_timeout=3).close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def app_dsn():
    """A database owned by a role without superuser rights, as on Amazon RDS."""
    if not reachable(PG_DSN):
        pytest.skip("PostgreSQL not reachable (set PGCYPHER_TEST_DSN)")
    name = "pgc_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {name} LOGIN PASSWORD 'app' NOSUPERUSER NOCREATEDB NOCREATEROLE")
        admin.execute(f"CREATE DATABASE {name} OWNER {name}")
    info = psycopg.conninfo.conninfo_to_dict(PG_DSN)
    info.update(user=name, password="app", dbname=name)
    dsn = psycopg.conninfo.make_conninfo(**info)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0] is False
        conn.execute(schema_sql())
    yield dsn
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"DROP ROLE IF EXISTS {name}")


@pytest.fixture(scope="session")
def age_dsn():
    if not reachable(AGE_DSN):
        pytest.skip("Apache AGE not reachable (set PGCYPHER_AGE_DSN)")
    with psycopg.connect(AGE_DSN, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
    return AGE_DSN
