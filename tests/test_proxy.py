"""Tests for the wire protocol proxy against a real PostgreSQL server."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import psycopg
import pytest
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict

from pgcypher import proxy, rewrite

DSN = os.environ.get("PGCYPHER_TEST_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")
UP = conninfo_to_dict(DSN)
UP_HOST = str(UP.get("host", "127.0.0.1"))
UP_PORT = int(UP.get("port", 5432))


def upstream_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not upstream_available(), reason="test PostgreSQL not reachable")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_proxy(ssl_mode: str = "prefer") -> int:
    port = free_port()
    started = threading.Event()

    def run() -> None:
        async def main() -> None:
            ready = asyncio.Event()
            task = asyncio.create_task(proxy.serve("127.0.0.1", port, UP_HOST, UP_PORT, ssl_mode, ready))
            await ready.wait()
            started.set()
            await task

        asyncio.run(main())

    threading.Thread(target=run, daemon=True).start()
    assert started.wait(5)
    return port


def stub(text: str) -> str:
    if "boom" in text:
        raise rewrite.RewriteError(f"cannot translate: {text}")
    return text.replace("select 'cypher-marker'", "select 'rewritten'")


@pytest.fixture(scope="module")
def proxy_port() -> int:
    return start_proxy()


@pytest.fixture(autouse=True)
def patch_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rewrite, "rewrite_sql", stub)


def dsn_via(port: int, **extra: str) -> str:
    params = {**UP, "host": "127.0.0.1", "port": str(port), **extra}
    return " ".join(f"{k}='{v}'" for k, v in params.items())


def connect(port: int, **extra: str) -> psycopg.Connection:
    return psycopg.connect(dsn_via(port, **extra), connect_timeout=5)


def test_simple_query_is_rewritten(proxy_port: int) -> None:
    with connect(proxy_port, sslmode="prefer") as conn:
        res = conn.pgconn.exec_(b"select 'cypher-marker'")
        assert res.status == pq.ExecStatus.TUPLES_OK
        assert res.get_value(0, 0) == b"rewritten"
        res = conn.pgconn.exec_(b"select 'untouched'")
        assert res.get_value(0, 0) == b"untouched"


def test_extended_protocol_is_rewritten(proxy_port: int) -> None:
    with connect(proxy_port) as conn:
        row = conn.execute("select 'cypher-marker', %s::int + 1", [41]).fetchone()
        assert row == ("rewritten", 42)
        row = conn.execute("select 'cypher-marker'", prepare=True).fetchone()
        assert row == ("rewritten",)


def test_error_on_simple_query_keeps_connection(proxy_port: int) -> None:
    with connect(proxy_port) as conn:
        res = conn.pgconn.exec_(b"select 'boom'")
        assert res.status == pq.ExecStatus.FATAL_ERROR
        assert res.error_field(pq.DiagnosticField.SQLSTATE) == b"42601"
        assert res.error_field(pq.DiagnosticField.MESSAGE_PRIMARY) == b"cannot translate: select 'boom'"
        assert conn.pgconn.exec_(b"select 1").get_value(0, 0) == b"1"


def test_error_on_parse_keeps_connection(proxy_port: int) -> None:
    with connect(proxy_port) as conn:
        conn.autocommit = True
        with pytest.raises(psycopg.errors.SyntaxError) as info:
            conn.execute("select %s::text, 'boom'", ["x"])
        assert "cannot translate" in str(info.value)
        assert conn.execute("select %s::int", [7]).fetchone() == (7,)


def test_error_inside_transaction_fails_the_transaction(proxy_port: int) -> None:
    with connect(proxy_port) as conn:
        conn.execute("select 1")
        with pytest.raises(psycopg.errors.SyntaxError):
            conn.execute("select 'boom'")
        assert conn.info.transaction_status == pq.TransactionStatus.INERROR
        conn.rollback()
        assert conn.execute("select 2").fetchone() == (2,)


def test_password_auth_through_proxy(proxy_port: int) -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("drop role if exists pgcypher_proxy_user")
        admin.execute("set password_encryption = 'scram-sha-256'")
        admin.execute("create role pgcypher_proxy_user login password 'right pw'")
    try:
        with connect(proxy_port, user="pgcypher_proxy_user", password="right pw") as conn:
            assert conn.execute("select current_user").fetchone() == ("pgcypher_proxy_user",)
        with pytest.raises(psycopg.OperationalError):
            connect(proxy_port, user="pgcypher_proxy_user", password="wrong pw")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute("drop role if exists pgcypher_proxy_user")


def test_transaction_rollback(proxy_port: int) -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("create table if not exists pgcypher_proxy_t (x int)")
        admin.execute("truncate pgcypher_proxy_t")
    try:
        with connect(proxy_port) as conn:
            conn.autocommit = True
            conn.execute("begin")
            conn.execute("insert into pgcypher_proxy_t values (1)")
            assert conn.info.transaction_status == pq.TransactionStatus.INTRANS
            conn.execute("rollback")
            assert conn.execute("select count(*) from pgcypher_proxy_t").fetchone() == (0,)
            with conn.transaction():
                conn.execute("insert into pgcypher_proxy_t values (2)")
            assert conn.execute("select count(*) from pgcypher_proxy_t").fetchone() == (1,)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute("drop table if exists pgcypher_proxy_t")


def test_cancel(proxy_port: int) -> None:
    with connect(proxy_port) as conn:
        # cancel_safe() waits in Python; the blocking cancel() would hold the GIL
        # and starve the in-process proxy thread.
        timer = threading.Timer(0.5, conn.cancel_safe)
        start = time.monotonic()
        timer.start()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("select pg_sleep(5)")
        assert time.monotonic() - start < 2.5
        conn.rollback()
        assert conn.execute("select 1").fetchone() == (1,)


def upstream_ssl_enabled() -> bool:
    with psycopg.connect(DSN) as conn:
        return conn.execute("show ssl").fetchone() == ("on",)


def test_require_tls_upstream() -> None:
    port = start_proxy("require")
    if upstream_ssl_enabled():
        with connect(port) as conn:
            row = conn.execute("select ssl from pg_stat_ssl where pid = pg_backend_pid()").fetchone()
            assert row == (True,)
    else:
        with pytest.raises(psycopg.OperationalError) as info:
            connect(port)
        assert "does not offer TLS" in str(info.value)


def test_strip_channel_binding() -> None:
    body = proxy.AUTH_SASL + b"SCRAM-SHA-256-PLUS\0SCRAM-SHA-256\0\0"
    assert proxy.strip_channel_binding(body) == proxy.AUTH_SASL + b"SCRAM-SHA-256\0\0"
