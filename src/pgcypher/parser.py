"""Lexer and recursive descent parser for the openCypher subset pgcypher supports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class CypherSyntaxError(Exception):
    pass


# ---------------------------------------------------------------- AST


@dataclass
class Expr:
    pass


@dataclass
class Literal(Expr):
    value: Any


@dataclass
class Param(Expr):
    name: str


@dataclass
class Var(Expr):
    name: str


@dataclass
class Prop(Expr):
    base: Expr
    key: str


@dataclass
class Index(Expr):
    base: Expr
    index: Expr


@dataclass
class ListLit(Expr):
    items: list[Expr]


@dataclass
class MapLit(Expr):
    items: list[tuple[str, Expr]]


@dataclass
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass
class UnaryOp(Expr):
    op: str
    operand: Expr


@dataclass
class IsNull(Expr):
    operand: Expr
    negate: bool


@dataclass
class HasLabel(Expr):
    operand: Expr
    label: str


@dataclass
class Func(Expr):
    name: str
    args: list[Expr]
    distinct: bool = False


@dataclass
class CountStar(Expr):
    pass


@dataclass
class Case(Expr):
    subject: Expr | None
    whens: list[tuple[Expr, Expr]]
    default: Expr | None


@dataclass
class NodePat:
    var: str | None
    labels: list[str]
    props: MapLit | Param | None


@dataclass
class RelPat:
    var: str | None
    types: list[str]
    props: MapLit | Param | None
    direction: str  # "out", "in", "both"
    varlen: tuple[int, int | None] | None = None


@dataclass
class Pattern:
    path_var: str | None
    elements: list[NodePat | RelPat]  # node, rel, node, rel, node ...


@dataclass
class Clause:
    pass


@dataclass
class Match(Clause):
    patterns: list[Pattern]
    where: Expr | None
    optional: bool = False


@dataclass
class Unwind(Clause):
    expr: Expr
    var: str


@dataclass
class ReturnItem:
    expr: Expr
    alias: str | None
    text: str


@dataclass
class Projection(Clause):
    items: list[ReturnItem]
    star: bool
    distinct: bool
    order: list[tuple[Expr, bool]]  # (expr, descending)
    skip: Expr | None
    limit: Expr | None
    where: Expr | None = None  # WITH only
    is_return: bool = False


@dataclass
class Create(Clause):
    patterns: list[Pattern]


@dataclass
class SetItem:
    kind: str  # "prop", "replace", "merge"
    var: str
    key: str | None
    value: Expr | None


@dataclass
class SetClause(Clause):
    items: list[SetItem]


@dataclass
class Delete(Clause):
    exprs: list[Expr]
    detach: bool


@dataclass
class Merge(Clause):
    pattern: Pattern
    on_create: list[SetItem] = field(default_factory=list)
    on_match: list[SetItem] = field(default_factory=list)


@dataclass
class Query:
    clauses: list[Clause]


# ---------------------------------------------------------------- lexer

KEYWORDS = {
    "MATCH", "OPTIONAL", "WHERE", "RETURN", "WITH", "UNWIND", "AS", "CREATE", "MERGE", "SET",
    "REMOVE", "DELETE", "DETACH", "ON", "ORDER", "BY", "ASC", "ASCENDING", "DESC", "DESCENDING",
    "SKIP", "LIMIT", "DISTINCT", "AND", "OR", "XOR", "NOT", "IN", "STARTS", "ENDS", "CONTAINS",
    "IS", "NULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END", "UNION", "CALL", "YIELD",
}


@dataclass
class Tok:
    kind: str  # "kw", "ident", "str", "int", "float", "param", "op", "eof"
    value: Any
    pos: int


TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<float>(?:\d+\.\d+|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)
  | (?P<int>0x[0-9a-fA-F]+|\d+)
  | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<param>\$(?:[A-Za-z_][A-Za-z0-9_]*|\d+))
  | (?P<bq>`(?:[^`]|``)*`)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.|<>|!=|<=|>=|=~|\+=|[-+*/%^=<>(){}\[\]:,.|;])
    """,
    re.VERBOSE | re.DOTALL,
)

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "\\": "\\", "'": "'", '"': '"'}


def _unescape(body: str) -> str:
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n == "u" and i + 5 < len(body):
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append(ESCAPES.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []
    pos = 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise CypherSyntaxError(f"unexpected character {text[pos]!r} at position {pos}")
        kind = m.lastgroup
        val = m.group()
        if kind == "ws":
            pass
        elif kind == "float":
            toks.append(Tok("float", float(val), pos))
        elif kind == "int":
            toks.append(Tok("int", int(val, 16) if val.startswith("0x") else int(val), pos))
        elif kind == "str":
            toks.append(Tok("str", _unescape(val[1:-1]), pos))
        elif kind == "param":
            toks.append(Tok("param", val[1:], pos))
        elif kind == "bq":
            toks.append(Tok("ident", val[1:-1].replace("``", "`"), pos))
        elif kind == "ident":
            if val.upper() in KEYWORDS:
                toks.append(Tok("kw", val.upper(), pos))
                toks[-1].raw = val  # type: ignore[attr-defined]
            else:
                toks.append(Tok("ident", val, pos))
        else:
            toks.append(Tok("op", val, pos))
        pos = m.end()
    toks.append(Tok("eof", None, pos))
    return toks


# ---------------------------------------------------------------- parser

AGGREGATES = {"count", "collect", "sum", "avg", "min", "max"}


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.anon = 0

    # helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def peek(self, n: int = 1) -> Tok:
        return self.toks[min(self.i + n, len(self.toks) - 1)]

    def error(self, msg: str) -> CypherSyntaxError:
        return CypherSyntaxError(f"{msg} at position {self.tok.pos}")

    def at_kw(self, *names: str) -> bool:
        return self.tok.kind == "kw" and self.tok.value in names

    def at_op(self, *ops: str) -> bool:
        return self.tok.kind == "op" and self.tok.value in ops

    def accept_kw(self, *names: str) -> bool:
        if self.at_kw(*names):
            self.i += 1
            return True
        return False

    def accept_op(self, *ops: str) -> bool:
        if self.at_op(*ops):
            self.i += 1
            return True
        return False

    def expect_kw(self, name: str) -> None:
        if not self.accept_kw(name):
            raise self.error(f"expected {name}")

    def expect_op(self, op: str) -> None:
        if not self.accept_op(op):
            raise self.error(f"expected {op!r}")

    def name(self) -> str:
        """Identifier; keywords are allowed as names (labels, keys)."""
        t = self.tok
        if t.kind == "ident":
            self.i += 1
            return t.value
        if t.kind == "kw":
            self.i += 1
            return getattr(t, "raw", t.value)
        raise self.error("expected a name")

    # query
    def parse(self) -> Query:
        clauses: list[Clause] = []
        while self.tok.kind != "eof":
            if self.accept_op(";"):
                continue
            clauses.append(self.clause())
        if not clauses:
            raise CypherSyntaxError("empty query")
        return Query(clauses)

    def clause(self) -> Clause:
        if self.at_kw("OPTIONAL"):
            self.i += 1
            self.expect_kw("MATCH")
            return self.match(optional=True)
        if self.accept_kw("MATCH"):
            return self.match()
        if self.accept_kw("UNWIND"):
            e = self.expr()
            self.expect_kw("AS")
            return Unwind(e, self.name())
        if self.accept_kw("WITH"):
            return self.projection(is_return=False)
        if self.accept_kw("RETURN"):
            return self.projection(is_return=True)
        if self.accept_kw("CREATE"):
            return Create(self.patterns())
        if self.accept_kw("MERGE"):
            m = Merge(self.pattern())
            while self.at_kw("ON"):
                self.i += 1
                if self.accept_kw("CREATE"):
                    self.expect_kw("SET")
                    m.on_create.extend(self.set_items())
                elif self.accept_kw("MATCH"):
                    self.expect_kw("SET")
                    m.on_match.extend(self.set_items())
                else:
                    raise self.error("expected CREATE or MATCH after ON")
            return m
        if self.accept_kw("SET"):
            return SetClause(self.set_items())
        if self.accept_kw("REMOVE"):
            items = []
            while True:
                var = self.name()
                if self.accept_op("."):
                    items.append(SetItem("prop", var, self.name(), None))
                else:
                    raise self.error("REMOVE supports properties only")
                if not self.accept_op(","):
                    break
            return SetClause(items)
        if self.at_kw("DETACH", "DELETE"):
            detach = self.accept_kw("DETACH")
            self.expect_kw("DELETE")
            exprs = [self.expr()]
            while self.accept_op(","):
                exprs.append(self.expr())
            return Delete(exprs, detach)
        if self.at_kw("UNION", "CALL"):
            raise self.error(f"{self.tok.value} is not supported")
        raise self.error("expected a clause")

    def match(self, optional: bool = False) -> Match:
        pats = self.patterns()
        where = self.expr() if self.accept_kw("WHERE") else None
        return Match(pats, where, optional)

    def set_items(self) -> list[SetItem]:
        items = []
        while True:
            var = self.name()
            if self.accept_op("."):
                key = self.name()
                self.expect_op("=")
                items.append(SetItem("prop", var, key, self.expr()))
            elif self.accept_op("+="):
                items.append(SetItem("merge", var, None, self.expr()))
            elif self.accept_op("="):
                items.append(SetItem("replace", var, None, self.expr()))
            elif self.at_op(":"):
                raise self.error("SET of labels is not supported")
            else:
                raise self.error("expected property assignment")
            if not self.accept_op(","):
                return items

    def projection(self, is_return: bool) -> Projection:
        distinct = self.accept_kw("DISTINCT")
        star = False
        items: list[ReturnItem] = []
        if self.accept_op("*"):
            star = True
            if not self.accept_op(","):
                return self.projection_tail(items, star, distinct, is_return)
        while True:
            start = self.tok.pos
            e = self.expr()
            end = self.tok.pos
            text = self.text[start:end].strip()
            alias = self.name() if self.accept_kw("AS") else None
            items.append(ReturnItem(e, alias, text))
            if not self.accept_op(","):
                break
        return self.projection_tail(items, star, distinct, is_return)

    def projection_tail(self, items, star, distinct, is_return) -> Projection:
        order: list[tuple[Expr, bool]] = []
        skip = limit = where = None
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            while True:
                e = self.expr()
                desc = False
                if self.accept_kw("DESC", "DESCENDING"):
                    desc = True
                else:
                    self.accept_kw("ASC", "ASCENDING")
                order.append((e, desc))
                if not self.accept_op(","):
                    break
        if self.accept_kw("SKIP"):
            skip = self.expr()
        if self.accept_kw("LIMIT"):
            limit = self.expr()
        if not is_return and self.accept_kw("WHERE"):
            where = self.expr()
        return Projection(items, star, distinct, order, skip, limit, where, is_return)

    # patterns
    def patterns(self) -> list[Pattern]:
        pats = [self.pattern()]
        while self.accept_op(","):
            pats.append(self.pattern())
        return pats

    def pattern(self) -> Pattern:
        path_var = None
        if self.tok.kind == "ident" and self.peek().kind == "op" and self.peek().value == "=":
            path_var = self.name()
            self.i += 1
        elems: list[NodePat | RelPat] = [self.node()]
        while self.at_op("-", "<"):
            elems.append(self.rel())
            elems.append(self.node())
        return Pattern(path_var, elems)

    def node(self) -> NodePat:
        self.expect_op("(")
        var = self.name() if self.tok.kind == "ident" else None
        labels = []
        while self.accept_op(":"):
            labels.append(self.name())
        props = self.props()
        self.expect_op(")")
        return NodePat(var, labels, props)

    def props(self) -> MapLit | Param | None:
        if self.at_op("{"):
            return self.map_lit()
        if self.tok.kind == "param":
            name = self.tok.value
            self.i += 1
            return Param(name)
        return None

    def rel(self) -> RelPat:
        left_arrow = False
        if self.accept_op("<"):
            left_arrow = True
        self.expect_op("-")
        var = None
        types: list[str] = []
        props = None
        varlen = None
        if self.accept_op("["):
            if self.tok.kind == "ident":
                var = self.name()
            if self.accept_op(":"):
                types.append(self.name())
                while self.accept_op("|"):
                    self.accept_op(":")
                    types.append(self.name())
            if self.accept_op("*"):
                lo, hi = 1, None
                if self.tok.kind == "int":
                    lo = self.tok.value
                    self.i += 1
                    hi = lo
                    if self.accept_op(".."):
                        hi = None
                        if self.tok.kind == "int":
                            hi = self.tok.value
                            self.i += 1
                elif self.accept_op(".."):
                    if self.tok.kind == "int":
                        hi = self.tok.value
                        self.i += 1
                varlen = (lo, hi)
            props = self.props()
            self.expect_op("]")
        self.expect_op("-")
        right_arrow = self.accept_op(">")
        if left_arrow and right_arrow:
            raise self.error("relationship cannot point both ways")
        direction = "in" if left_arrow else "out" if right_arrow else "both"
        return RelPat(var, types, props, direction, varlen)

    # expressions
    def expr(self) -> Expr:
        return self.or_expr()

    def or_expr(self) -> Expr:
        e = self.xor_expr()
        while self.accept_kw("OR"):
            e = BinOp("OR", e, self.xor_expr())
        return e

    def xor_expr(self) -> Expr:
        e = self.and_expr()
        while self.accept_kw("XOR"):
            e = BinOp("XOR", e, self.and_expr())
        return e

    def and_expr(self) -> Expr:
        e = self.not_expr()
        while self.accept_kw("AND"):
            e = BinOp("AND", e, self.not_expr())
        return e

    def not_expr(self) -> Expr:
        if self.accept_kw("NOT"):
            return UnaryOp("NOT", self.not_expr())
        return self.comparison()

    def comparison(self) -> Expr:
        e = self.additive()
        while True:
            if self.at_op("=", "<>", "!=", "<", "<=", ">", ">=", "=~"):
                op = self.tok.value
                self.i += 1
                e = BinOp("<>" if op == "!=" else op, e, self.additive())
            elif self.accept_kw("IN"):
                e = BinOp("IN", e, self.additive())
            elif self.at_kw("STARTS") and self.peek().value == "WITH":
                self.i += 2
                e = BinOp("STARTS WITH", e, self.additive())
            elif self.at_kw("ENDS") and self.peek().value == "WITH":
                self.i += 2
                e = BinOp("ENDS WITH", e, self.additive())
            elif self.accept_kw("CONTAINS"):
                e = BinOp("CONTAINS", e, self.additive())
            elif self.at_kw("IS"):
                self.i += 1
                neg = self.accept_kw("NOT")
                self.expect_kw("NULL")
                e = IsNull(e, neg)
            else:
                return e

    def additive(self) -> Expr:
        e = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.tok.value
            self.i += 1
            e = BinOp(op, e, self.multiplicative())
        return e

    def multiplicative(self) -> Expr:
        e = self.power()
        while self.at_op("*", "/", "%"):
            op = self.tok.value
            self.i += 1
            e = BinOp(op, e, self.power())
        return e

    def power(self) -> Expr:
        e = self.unary()
        while self.accept_op("^"):
            e = BinOp("^", e, self.unary())
        return e

    def unary(self) -> Expr:
        if self.accept_op("-"):
            return UnaryOp("-", self.unary())
        if self.accept_op("+"):
            return self.unary()
        return self.postfix()

    def postfix(self) -> Expr:
        e = self.atom()
        while True:
            if self.at_op(".") and self.peek().kind in ("ident", "kw"):
                self.i += 1
                e = Prop(e, self.name())
            elif self.accept_op("["):
                idx = self.expr()
                self.expect_op("]")
                e = Index(e, idx)
            elif self.at_op(":") and isinstance(e, Var):
                self.i += 1
                e = HasLabel(e, self.name())
            else:
                return e

    def atom(self) -> Expr:
        t = self.tok
        if t.kind in ("int", "float", "str"):
            self.i += 1
            return Literal(t.value)
        if t.kind == "param":
            self.i += 1
            return Param(t.value)
        if t.kind == "kw":
            if t.value in ("TRUE", "FALSE"):
                self.i += 1
                return Literal(t.value == "TRUE")
            if t.value == "NULL":
                self.i += 1
                return Literal(None)
            if t.value == "CASE":
                self.i += 1
                return self.case()
        if self.at_op("("):
            self.i += 1
            e = self.expr()
            self.expect_op(")")
            return e
        if self.at_op("["):
            self.i += 1
            items = []
            if not self.at_op("]"):
                items.append(self.expr())
                while self.accept_op(","):
                    items.append(self.expr())
            self.expect_op("]")
            return ListLit(items)
        if self.at_op("{"):
            return self.map_lit()
        if t.kind == "ident" or t.kind == "kw":
            name = self.name()
            if self.accept_op("("):
                if name.lower() == "count" and self.accept_op("*"):
                    self.expect_op(")")
                    return CountStar()
                distinct = self.accept_kw("DISTINCT")
                args = []
                if not self.at_op(")"):
                    args.append(self.expr())
                    while self.accept_op(","):
                        args.append(self.expr())
                self.expect_op(")")
                return Func(name.lower(), args, distinct)
            if t.kind == "kw":
                raise self.error(f"unexpected keyword {t.value}")
            return Var(name)
        raise self.error("expected an expression")

    def case(self) -> Case:
        subject = None if self.at_kw("WHEN") else self.expr()
        whens = []
        while self.accept_kw("WHEN"):
            cond = self.expr()
            self.expect_kw("THEN")
            whens.append((cond, self.expr()))
        default = self.expr() if self.accept_kw("ELSE") else None
        self.expect_kw("END")
        return Case(subject, whens, default)

    def map_lit(self) -> MapLit:
        self.expect_op("{")
        items = []
        if not self.at_op("}"):
            while True:
                key = self.tok.value if self.tok.kind == "str" else None
                if key is not None:
                    self.i += 1
                else:
                    key = self.name()
                self.expect_op(":")
                items.append((key, self.expr()))
                if not self.accept_op(","):
                    break
        self.expect_op("}")
        return MapLit(items)


def parse(text: str) -> Query:
    return Parser(text).parse()
