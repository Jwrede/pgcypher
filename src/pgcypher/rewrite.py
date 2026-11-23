"""Rewrite AGE-style SQL (cypher(), LOAD 'age', CREATE EXTENSION age) into plain PostgreSQL."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import CypherSyntaxError
from .translate import TranslateError, Translator, qi
from . import parser as P


class RewriteError(Exception):
    pass


NOOP = "DO $pgcypher$ BEGIN END $pgcypher$"


@dataclass
class Tok:
    kind: str  # ws, str, dollar, qident, ident, num, param, op
    text: str
    start: int
    end: int


SQL_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--[^\n]*|/\*.*?\*/)
  | (?P<str>[eE]?'(?:[^']|'')*')
  | (?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$)
  | (?P<param>\$\d+)
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<num>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)
  | (?P<op>::|[^\s])
    """,
    re.VERBOSE | re.DOTALL,
)


def sql_tokens(text: str) -> list[Tok]:
    out: list[Tok] = []
    i = 0
    while i < len(text):
        m = SQL_TOKEN.match(text, i)
        if m is None:  # pragma: no cover, the op branch matches any character
            raise RewriteError(f"cannot read SQL at position {i}")
        kind = m.lastgroup if m.lastgroup != "tag" else "dollar"
        if kind == "dollar":
            tag = m.group()
            close = text.find(tag, m.end())
            if close < 0:
                raise RewriteError("unterminated dollar-quoted string")
            out.append(Tok("dollar", text[i : close + len(tag)], i, close + len(tag)))
            i = close + len(tag)
            continue
        out.append(Tok(kind, m.group(), i, m.end()))
        i = m.end()
    return out


def string_value(tok: Tok) -> str:
    if tok.kind == "dollar":
        tag_end = tok.text.index("$", 1) + 1
        return tok.text[tag_end : len(tok.text) - tag_end]
    if tok.kind == "str":
        body = tok.text[1:] if tok.text[0] in "eE" else tok.text
        return body[1:-1].replace("''", "'")
    raise RewriteError("expected a string constant")


def split_statements(toks: list[Tok]) -> list[list[Tok]]:
    stmts: list[list[Tok]] = [[]]
    for t in toks:
        if t.kind == "op" and t.text == ";":
            stmts.append([])
        else:
            stmts[-1].append(t)
    return stmts


def words(toks: list[Tok]) -> list[str]:
    return [t.text.lower() if t.kind != "qident" else t.text[1:-1].lower() for t in toks if t.kind != "ws"]


@dataclass
class CypherCall:
    start: int  # offset of "cypher" (or of the schema qualifier)
    end: int  # offset after the column definition list
    graph: str
    query: str
    params: str | None
    columns: list[str]
    alias: str = "_cypher"


def find_calls(text: str, toks: list[Tok]) -> list[CypherCall]:
    sig = [t for t in toks if t.kind != "ws"]
    calls = []
    i = 0
    while i < len(sig):
        t = sig[i]
        if t.kind == "ident" and t.text.lower() == "cypher" and i + 1 < len(sig) and sig[i + 1].text == "(":
            start = t.start
            if i >= 2 and sig[i - 1].text == "." and sig[i - 2].text.lower() in ("ag_catalog", '"ag_catalog"'):
                start = sig[i - 2].start
            j = i + 2
            graph = string_value(sig[j]) if sig[j].kind in ("str", "dollar") else None
            if graph is None:
                raise RewriteError("cypher() needs the graph name as a string constant")
            j += 1
            if sig[j].text != ",":
                raise RewriteError("cypher() needs a query string as its second argument")
            j += 1
            if sig[j].kind not in ("str", "dollar"):
                raise RewriteError("cypher() needs the query as a string constant")
            query = string_value(sig[j])
            j += 1
            params = None
            if sig[j].text == ",":
                j += 1
                depth = 0
                pstart = sig[j].start
                while j < len(sig) and not (depth == 0 and sig[j].text == ")"):
                    if sig[j].text == "(":
                        depth += 1
                    elif sig[j].text == ")":
                        depth -= 1
                    j += 1
                params = text[pstart : sig[j - 1].end].strip()
            if j >= len(sig) or sig[j].text != ")":
                raise RewriteError("unterminated cypher() call")
            j += 1
            if j < len(sig) and sig[j].text.lower() == "as":
                j += 1
            alias = "_cypher"
            if j < len(sig) and sig[j].kind in ("ident", "qident") and j + 1 < len(sig) and sig[j + 1].text == "(":
                alias = sig[j].text  # table alias, AS t(a agtype)
                j += 1
            if j >= len(sig) or sig[j].text != "(":
                raise RewriteError("a column definition list is required for cypher(), e.g. AS (a agtype)")
            j += 1
            columns = []
            expect_name = True
            depth = 0
            while j < len(sig) and not (depth == 0 and sig[j].text == ")"):
                s = sig[j]
                if s.text == "(":
                    depth += 1
                elif s.text == ")":
                    depth -= 1
                elif s.text == "," and depth == 0:
                    expect_name = True
                elif expect_name:
                    columns.append(s.text[1:-1].replace('""', '"') if s.kind == "qident" else s.text.lower())
                    expect_name = False
                j += 1
            if j >= len(sig):
                raise RewriteError("unterminated column definition list")
            calls.append(CypherCall(start, sig[j].end, graph, query, params, columns, alias))
            i = j + 1
            continue
        i += 1
    return calls


def translate_call(call: CypherCall) -> tuple[str, bool]:
    t = Translator(call.graph, call.params)
    try:
        sql = t.run(P.parse(call.query), call.columns)
    except (CypherSyntaxError, TranslateError) as exc:
        raise RewriteError(str(exc)) from exc
    return sql, bool(t.written or t.deletes)


def rewrite_statement(text: str, toks: list[Tok]) -> str:
    w = words(toks)
    if not w:
        return text
    if w[0] == "load" and len(w) == 2 and "age" in w[1]:
        return NOOP
    if w[:2] == ["create", "extension"]:
        rest = w[2:]
        if rest[:3] == ["if", "not", "exists"]:
            rest = rest[3:]
        if rest and rest[0] in ("age", "'age'"):
            return NOOP
    if "cypher" not in w:
        return text
    calls = find_calls(text, toks)
    if not calls:
        return text
    simple = re.fullmatch(r"select\s+\*\s+from\s+", text[: calls[0].start].strip().lower() + " ") is not None
    out = []
    pos = 0
    for call in calls:
        sql, writes = translate_call(call)
        if len(calls) == 1 and simple and text[call.end :].strip() == "":
            return sql
        if writes:
            raise RewriteError("a cypher() query that writes must be the whole statement: SELECT * FROM cypher(...) AS (...)")
        cols = ", ".join(qi(c) for c in call.columns)
        out.append(text[pos : call.start])
        out.append(f"({sql}) AS {call.alias}({cols})")
        pos = call.end
    out.append(text[pos:])
    return "".join(out)


def rewrite_sql(text: str) -> str:
    """Return the SQL to send upstream for one simple-query string or one Parse message."""
    if "cypher" not in text.lower() and "age" not in text.lower():
        return text
    toks = sql_tokens(text)
    stmts = split_statements(toks)
    if len(stmts) == 1:
        return rewrite_statement(text, toks)
    pieces = []
    for st in stmts:
        if not st:
            continue
        seg = text[st[0].start : st[-1].end]
        shifted = [Tok(t.kind, t.text, t.start - st[0].start, t.end - st[0].start) for t in st]
        pieces.append(rewrite_statement(seg, shifted))
    return ";\n".join(p for p in pieces if p.strip()) + (";" if text.rstrip().endswith(";") else "")
