"""Transparent PostgreSQL wire protocol proxy that rewrites query text.

The proxy sits between PostgreSQL clients and an upstream server (for example
Amazon RDS). Startup, authentication and every result message pass through
unchanged. Only the query text of simple Query ('Q') and Parse ('P') messages
is passed through pgcypher.rewrite.rewrite_sql before it is forwarded.

TLS: clients talk to the proxy without TLS (SSLRequest and GSSENCRequest are
answered with 'N'). Towards the upstream the proxy negotiates TLS according to
upstream_ssl:

  disable      never use TLS
  prefer       use TLS if the server offers it, without certificate checks
  require      TLS is mandatory, without certificate checks
  verify-full  TLS is mandatory, the certificate and host name are verified
               against the system trust store

Because the client has no TLS session with the proxy, SCRAM channel binding
is not available to it. libpq refuses a server that offers
SCRAM-SHA-256-PLUS over a connection without TLS, so the proxy removes the
-PLUS mechanisms from the AuthenticationSASL list it relays. The client then
uses plain SCRAM-SHA-256, which the upstream accepts. md5 and password
authentication are relayed unchanged.

Rewrite errors: when rewrite_sql raises, the proxy does not inject a reply of
its own into the result stream. It forwards a query that is guaranteed to fail
during parse analysis on the upstream (a reference to a column with a unique
name) and replaces the text of the resulting ErrorResponse with the rewrite
error. The server therefore handles ordering, the skip until Sync in the
extended protocol and the transaction state (a failed statement inside a
transaction block leaves it in the failed state, as a real error would).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import uuid

from pgcypher import rewrite

log = logging.getLogger("pgcypher.proxy")

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102
MAX_STARTUP = 10_000
TOKEN_PREFIX = "pgcypher_rewrite_error_"
AUTH_SASL = struct.pack("!I", 10)


def message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def error_response(text: str, code: str) -> bytes:
    fields = b""
    for key, value in ((b"S", "ERROR"), (b"V", "ERROR"), (b"C", code), (b"M", text)):
        fields += key + value.encode("utf-8", "replace") + b"\0"
    return message(b"E", fields + b"\0")


def parse_error_fields(body: bytes) -> dict[bytes, bytes]:
    fields: dict[bytes, bytes] = {}
    pos = 0
    while pos < len(body) and body[pos] != 0:
        key = body[pos:pos + 1]
        end = body.index(b"\0", pos + 1)
        fields[key] = body[pos + 1:end]
        pos = end + 1
    return fields


def strip_channel_binding(body: bytes) -> bytes:
    """Remove SCRAM -PLUS mechanisms from an AuthenticationSASL body."""
    names = [n for n in body[4:].split(b"\0") if n]
    kept = [n for n in names if not n.endswith(b"-PLUS")]
    return body[:4] + b"".join(n + b"\0" for n in kept) + b"\0"


def ssl_context(mode: str) -> ssl.SSLContext:
    if mode == "verify-full":
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def read_packet(reader: asyncio.StreamReader) -> bytes:
    """Read one untyped startup phase packet (length included)."""
    head = await reader.readexactly(4)
    (length,) = struct.unpack("!I", head)
    if length < 8 or length > MAX_STARTUP:
        raise ConnectionError(f"invalid startup packet length {length}")
    return head + await reader.readexactly(length - 4)


async def read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    head = await reader.readexactly(5)
    kind = head[:1]
    (length,) = struct.unpack("!I", head[1:])
    if length < 4:
        raise ConnectionError(f"invalid message length {length}")
    return kind, await reader.readexactly(length - 4)


async def open_upstream(host: str, port: int, mode: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(host, port)
    if mode == "disable":
        return reader, writer
    writer.write(struct.pack("!II", 8, SSL_REQUEST))
    await writer.drain()
    answer = await reader.readexactly(1)
    if answer == b"S":
        await writer.start_tls(ssl_context(mode), server_hostname=host)
        return reader, writer
    if answer == b"N" and mode == "prefer":
        return reader, writer
    writer.close()
    raise ConnectionError(f"upstream {host}:{port} does not offer TLS (sslmode {mode})")


class Session:
    def __init__(self, proxy: "Proxy", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.proxy = proxy
        self.client_reader = reader
        self.client_writer = writer
        self.status = b"I"
        self.pending: dict[str, tuple[str, str]] = {}

    async def run(self) -> None:
        try:
            startup = await self.negotiate()
            if startup is None:
                return
            up_reader, up_writer = await open_upstream(
                self.proxy.upstream_host, self.proxy.upstream_port, self.proxy.upstream_ssl
            )
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ssl.SSLError) as exc:
            log.warning("connection setup failed: %s", exc)
            if not isinstance(exc, asyncio.IncompleteReadError):
                try:
                    self.client_writer.write(error_response(f"pgcypher proxy: {exc}", "08006"))
                    await self.client_writer.drain()
                except (ConnectionError, OSError):
                    pass
            self.client_writer.close()
            return
        up_writer.write(startup)
        await up_writer.drain()
        tasks = [
            asyncio.create_task(self.client_to_upstream(up_writer)),
            asyncio.create_task(self.upstream_to_client(up_reader)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (asyncio.IncompleteReadError, ConnectionError)):
                    log.error("session error: %r", exc)
        finally:
            for w in (up_writer, self.client_writer):
                w.close()

    async def negotiate(self) -> bytes | None:
        """Handle SSL, GSSENC and cancel requests; return the StartupMessage."""
        for _ in range(4):
            packet = await read_packet(self.client_reader)
            (code,) = struct.unpack("!I", packet[4:8])
            log.debug("startup phase packet code %d length %d", code, len(packet))
            if code in (SSL_REQUEST, GSSENC_REQUEST):
                self.client_writer.write(b"N")
                await self.client_writer.drain()
                continue
            if code == CANCEL_REQUEST:
                await self.forward_cancel(packet)
                self.client_writer.close()
                return None
            return packet
        raise ConnectionError("too many negotiation requests")

    async def forward_cancel(self, packet: bytes) -> None:
        try:
            reader, writer = await open_upstream(
                self.proxy.upstream_host, self.proxy.upstream_port, self.proxy.upstream_ssl
            )
            writer.write(packet)
            await writer.drain()
            log.debug("cancel request forwarded")
            await reader.read()  # the server closes the connection after a cancel
            writer.close()
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            log.warning("forwarding cancel request failed: %s", exc)

    def failing_query(self, exc: Exception) -> str:
        token = TOKEN_PREFIX + uuid.uuid4().hex
        code = "42601" if isinstance(exc, rewrite.RewriteError) else "XX000"
        self.pending[token] = (str(exc), code)
        return f'SELECT "{token}"'

    def rewrite(self, query: str) -> str:
        try:
            return rewrite.rewrite_sql(query)
        except Exception as exc:  # noqa: BLE001, any failure becomes a client error
            if not isinstance(exc, rewrite.RewriteError):
                log.exception("rewrite failed")
            return self.failing_query(exc)

    def transform(self, kind: bytes, body: bytes) -> bytes:
        if kind == b"Q":
            text = body[:-1].decode("utf-8", "surrogateescape")
            new = self.rewrite(text)
            if new == text:
                return message(kind, body)
            return message(kind, new.encode("utf-8", "surrogateescape") + b"\0")
        if kind == b"P":
            end_name = body.index(b"\0")
            end_query = body.index(b"\0", end_name + 1)
            text = body[end_name + 1:end_query].decode("utf-8", "surrogateescape")
            new = self.rewrite(text)
            if new == text:
                return message(kind, body)
            if new.startswith(f'SELECT "{TOKEN_PREFIX}'):
                rest = struct.pack("!H", 0)  # no parameter types for the failing query
            else:
                rest = body[end_query + 1:]
            payload = body[:end_name + 1] + new.encode("utf-8", "surrogateescape") + b"\0" + rest
            return message(kind, payload)
        return message(kind, body)

    async def client_to_upstream(self, up_writer: asyncio.StreamWriter) -> None:
        while True:
            kind, body = await read_message(self.client_reader)
            up_writer.write(self.transform(kind, body))
            await up_writer.drain()
            if kind == b"X":
                return

    async def upstream_to_client(self, up_reader: asyncio.StreamReader) -> None:
        while True:
            kind, body = await read_message(up_reader)
            if kind == b"Z" and body:
                self.status = body[:1]
            if kind == b"R" and body[:4] == AUTH_SASL:
                body = strip_channel_binding(body)
            out = message(kind, body)
            if kind == b"E" and self.pending:
                text = parse_error_fields(body).get(b"M", b"").decode("utf-8", "replace")
                for token in list(self.pending):
                    if token in text:
                        msg, code = self.pending.pop(token)
                        out = error_response(msg, code)
                        break
            self.client_writer.write(out)
            await self.client_writer.drain()


class Proxy:
    def __init__(self, upstream_host: str, upstream_port: int, upstream_ssl: str) -> None:
        if upstream_ssl not in ("disable", "prefer", "require", "verify-full"):
            raise ValueError(f"unknown upstream_ssl mode {upstream_ssl!r}")
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_ssl = upstream_ssl

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await Session(self, reader, writer).run()
        except Exception:  # noqa: BLE001, keep the listener alive
            log.exception("unhandled session error")
            writer.close()


async def serve(
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
    upstream_ssl: str = "prefer",
    ready: asyncio.Event | None = None,
) -> None:
    proxy = Proxy(upstream_host, upstream_port, upstream_ssl)
    server = await asyncio.start_server(proxy.handle, listen_host, listen_port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("listening on %s, upstream %s:%s (ssl %s)", addrs, upstream_host, upstream_port, upstream_ssl)
    if ready is not None:
        ready.set()
    async with server:
        await server.serve_forever()
