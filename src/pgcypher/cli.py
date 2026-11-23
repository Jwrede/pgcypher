"""Command line: install the catalog, translate or run a query, start the proxy."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from importlib.resources import files

from .translate import translate


def schema_sql() -> str:
    return files("pgcypher").joinpath("schema.sql").read_text()


def install(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(schema_sql())
    print("installed ag_catalog (pgcypher)")


def run_query(dsn: str, graph: str, cypher: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(translate(graph, cypher))
        names = [d.name for d in cur.description or []]
        if names:
            print("\t".join(names))
        for row in cur.fetchall():
            print("\t".join("" if v is None else str(v) for v in row))


def hostport(text: str, default_port: int) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host:
        return text, default_port
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pgcypher", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="create the ag_catalog schema and helper functions")
    p.add_argument("dsn")
    p = sub.add_parser("schema", help="print the catalog SQL (to review it or run it with psql)")
    p = sub.add_parser("translate", help="print the SQL for a Cypher query")
    p.add_argument("-g", "--graph", required=True)
    p.add_argument("query")
    p = sub.add_parser("query", help="run a Cypher query and print the rows")
    p.add_argument("dsn")
    p.add_argument("-g", "--graph", required=True)
    p.add_argument("query")
    p = sub.add_parser("proxy", help="PostgreSQL wire proxy that rewrites cypher() calls")
    p.add_argument("--listen", default="127.0.0.1:5433", help="host:port to listen on (default 127.0.0.1:5433)")
    p.add_argument("--upstream", required=True, help="host:port of the PostgreSQL server")
    p.add_argument("--upstream-ssl", default="prefer", choices=["disable", "prefer", "require", "verify-full"])
    args = ap.parse_args(argv)
    try:
        if args.cmd == "install":
            install(args.dsn)
        elif args.cmd == "schema":
            sys.stdout.write(schema_sql())
        elif args.cmd == "translate":
            print(translate(args.graph, args.query))
        elif args.cmd == "query":
            run_query(args.dsn, args.graph, args.query)
        elif args.cmd == "proxy":
            from .proxy import serve

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            lh, lp = hostport(args.listen, 5433)
            uh, up = hostport(args.upstream, 5432)
            asyncio.run(serve(lh, lp, uh, up, args.upstream_ssl))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"pgcypher: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
