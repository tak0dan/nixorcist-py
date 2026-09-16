"""A real, structural parser for (a practical subset of) Nix expressions.

Nixorcist deliberately does NOT use regular expressions to edit Nix
configuration files.  This module tokenizes and parses the subset of Nix
needed for configuration discovery and promotion -- attribute sets, lists,
``with``, ``let``, ``if``, ``inherit``, application, attribute selection and
module lambdas (including ``{ config, pkgs, ... }:`` patterns) -- and records
precise source spans so transformations can be applied textually while
preserving all other formatting.

Anything that cannot be parsed in a given region raises :class:`NixParseError`;
callers fall back to treating that file (or region) as opaque rather than
guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError


class NixParseError(NixorcistError):
    def __init__(self, message: str, source: str = ""):
        super().__init__(
            Diagnostic(ErrorCode.NIX_PARSE, message, expression=source)
        )


@dataclass(frozen=True)
class Token:
    kind: str  # IDENT INT STRING PATH OP DOT KEYWORD EOF
    value: str
    start: int
    end: int


@dataclass
class Node:
    kind: str
    start: int
    end: int
    value: str = ""
    children: list["Node"] = field(default_factory=list)
    attrpath: list[str] = field(default_factory=list)

    def slice(self, text: str) -> str:
        return text[self.start : self.end]

    def __repr__(self) -> str:  # pragma: no cover
        return f"Node({self.kind}, {self.value!r}, {self.start}..{self.end})"

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            yield from child.walk()


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

_KEYWORDS = {"with", "rec", "let", "in", "if", "then", "else", "assert", "inherit", "or"}

_MULTI_OPS = {
    "//": "OP",
    "++": "OP",
    "==": "OP",
    "!=": "OP",
    "<=": "OP",
    ">=": "OP",
    "&&": "OP",
    "||": "OP",
    "->": "OP",
}

_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_'-]*")
_PATH_END = set(" \t\r\n;,]})")
_WS = set(" \t\r\n")


def _scan_ind_string(text: str, i: int) -> int:
    n = len(text)
    j = i + 2
    while j < n:
        if text.startswith("''", j):
            nxt = text[j + 2 : j + 3]
            if nxt in ("'", "$", "\\"):
                j += 3
                continue
            return j + 2
        if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
            j = _scan_interpolation(text, j + 2)
            continue
        j += 1
    raise NixParseError("unterminated indented string")


def _scan_interpolation(text: str, j: int) -> int:
    n = len(text)
    depth = 1
    k = j
    while k < n:
        c = text[k]
        if c == '"':
            k = _scan_string(text, k)
            continue
        if text.startswith("''", k):
            k = _scan_ind_string(text, k)
            continue
        if c == "$" and k + 1 < n and text[k + 1] == "{":
            depth += 1
            k += 2
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    raise NixParseError("unterminated string interpolation")


def _scan_string(text: str, i: int) -> int:
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "$" and j + 1 < n and text[j + 1] == "{":
            j = _scan_interpolation(text, j + 2)
            continue
        j += 1
    raise NixParseError("unterminated string")


def _scan_path(text: str, i: int) -> int:
    n = len(text)
    j = i
    while j < n and text[j] not in _PATH_END:
        j += 1
    return j


def lex(text: str) -> list[Token]:
    tokens: list[Token] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c in _WS:
            i += 1
            continue
        if c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise NixParseError("unterminated block comment", text)
            i = end + 2
            continue
        if c == '"':
            end = _scan_string(text, i)
            tokens.append(Token("STRING", text[i:end], i, end))
            i = end
            continue
        if text.startswith("''", i):
            end = _scan_ind_string(text, i)
            tokens.append(Token("STRING", text[i:end], i, end))
            i = end
            continue
        if c == "<":
            end = text.find(">", i + 1)
            if end == -1:
                raise NixParseError("unterminated angle path", text)
            tokens.append(Token("PATH", text[i : end + 1], i, end + 1))
            i = end + 1
            continue
        for op in _MULTI_OPS:
            if text.startswith(op, i):
                tokens.append(Token("OP", op, i, i + len(op)))
                i += len(op)
                break
        else:
            if text.startswith("./", i) or text.startswith("../", i):
                end = _scan_path(text, i)
                tokens.append(Token("PATH", text[i:end], i, end))
                i = end
            elif c == "/":
                nxt = text[i + 1] if i + 1 < n else ""
                if nxt and (nxt.isalpha() or nxt == "/" or nxt == "."):
                    end = _scan_path(text, i)
                    tokens.append(Token("PATH", text[i:end], i, end))
                    i = end
                else:
                    tokens.append(Token("OP", "/", i, i + 1))
                    i += 1
            elif c.isdigit():
                end = i
                while end < n and text[end].isdigit():
                    end += 1
                tokens.append(Token("INT", text[i:end], i, end))
                i = end
            elif c in "{}[],;:@":
                tokens.append(Token(c, c, i, i + 1))
                i += 1
            elif c in "()":
                tokens.append(Token(c, c, i, i + 1))
                i += 1
            elif c == ".":
                tokens.append(Token("DOT", c, i, i + 1))
                i += 1
            elif c in "+-*!<>=":
                tokens.append(Token("OP", c, i, i + 1))
                i += 1
            elif c == ":":
                tokens.append(Token(":", c, i, i + 1))
                i += 1
            elif c == "?":
                tokens.append(Token("?", c, i, i + 1))
                i += 1
            else:
                m = _IDENT_RE.match(text, i)
                if m:
                    value = m.group(0)
                    kind = "KEYWORD" if value in _KEYWORDS else "IDENT"
                    tokens.append(Token(kind, value, i, m.end()))
                    i = m.end()
                else:
                    raise NixParseError(f"unexpected character {c!r}", text)
            continue
    tokens.append(Token("EOF", "", n, n))
    return tokens


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str, tokens: list[Token]):
        self.text = text
        self.tokens = tokens
        self.i = 0

    def peek(self, ahead: int = 0) -> Token:
        j = min(self.i + ahead, len(self.tokens) - 1)
        return self.tokens[j]

    def advance(self) -> Token:
        tok = self.peek()
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def fail(self, message: str) -> NixParseError:
        return NixParseError(message, self.text)

    def expect(self, kind: str, value: str | None = None) -> Token:
        tok = self.peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise self.fail(f"expected {value or kind}, found {tok.value!r} at {tok.start}")
        return self.advance()

    def node(self, kind: str, start: int, end: int, value: str = "") -> Node:
        return Node(kind, start, end, value=value)

    def _keyword_start(self, tok: Token) -> bool:
        return tok.kind == "KEYWORD" and tok.value in ("with", "let", "if", "assert")

    # -- expression levels ---------------------------------------------------
    def parse_expr(self) -> Node:
        tok = self.peek()
        if tok.kind == "KEYWORD":
            if tok.value == "with":
                return self.parse_with()
            if tok.value == "let":
                return self.parse_let()
            if tok.value == "if":
                return self.parse_if()
            if tok.value == "assert":
                return self.parse_assert()
        return self.parse_or()

    def _keyword_expr(self) -> Node:
        return self.parse_expr()

    def parse_with(self) -> Node:
        start = self.peek().start
        self.expect("KEYWORD", "with")
        scope = self.parse_expr()
        self.expect(";")
        body = self.parse_expr()
        node = self.node("with", start, body.end)
        node.children = [scope, body]
        return node

    def parse_let(self) -> Node:
        start = self.peek().start
        self.expect("KEYWORD", "let")
        bindings: list[Node] = []
        while self.peek().kind != "KEYWORD" or self.peek().value != "in":
            if self.peek().kind == "KEYWORD" and self.peek().value == "inherit":
                bindings.append(self.parse_inherit())
                continue
            lhs = self.parse_attrpath_ident()
            self.expect("OP", "=")
            value = self.parse_expr()
            self.expect(";")
            node = self.node("binding", lhs.start, value.end, value=lhs.value)
            node.attrpath = lhs.attrpath
            node.children = [value]
            bindings.append(node)
        self.expect("KEYWORD", "in")
        body = self.parse_expr()
        node = self.node("let", start, body.end)
        node.children = bindings + [body]
        return node

    def parse_if(self) -> Node:
        start = self.peek().start
        self.expect("KEYWORD", "if")
        cond = self.parse_expr()
        self.expect("KEYWORD", "then")
        then_branch = self.parse_expr()
        self.expect("KEYWORD", "else")
        else_branch = self.parse_expr()
        node = self.node("if", start, else_branch.end)
        node.children = [cond, then_branch, else_branch]
        return node

    def parse_assert(self) -> Node:
        start = self.peek().start
        self.expect("KEYWORD", "assert")
        cond = self.parse_expr()
        self.expect(";")
        body = self.parse_expr()
        node = self.node("assert", start, body.end)
        node.children = [cond, body]
        return node

    def _binary(self, level: str, ops: set[str], next_fn) -> Node:
        node = next_fn()
        while self.peek().kind == "OP" and self.peek().value in ops:
            op = self.advance()
            rhs = next_fn()
            n = self.node("op", node.start, rhs.end, value=op.value)
            n.children = [node, rhs]
            node = n
        return node

    def parse_or(self) -> Node:
        return self._binary("or", {"||"}, self.parse_and)

    def parse_and(self) -> Node:
        return self._binary("and", {"&&"}, self.parse_eq)

    def parse_eq(self) -> Node:
        return self._binary("eq", {"==", "!="}, self.parse_rel)

    def parse_rel(self) -> Node:
        return self._binary("rel", {"<", ">", "<=", ">="}, self.parse_update)

    def parse_update(self) -> Node:
        return self._binary("update", {"//"}, self.parse_concat)

    def parse_concat(self) -> Node:
        return self._binary("concat", {"++"}, self.parse_add)

    def parse_add(self) -> Node:
        return self._binary("add", {"+", "-"}, self.parse_mul)

    def parse_mul(self) -> Node:
        return self._binary("mul", {"*", "/"}, self.parse_unary)

    def parse_unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "OP" and tok.value in ("-", "!"):
            op = self.advance()
            operand = self.parse_unary()
            node = self.node("op", op.start, operand.end, value=op.value)
            node.children = [operand]
            return node
        return self.parse_apply()

    def is_atom_start(self, tok: Token) -> bool:
        if self._keyword_start(tok):
            return True
        if tok.kind in ("IDENT", "STRING", "PATH", "INT", "{", "[", "("):
            return True
        return False

    def parse_apply(self) -> Node:
        node = self.parse_select()
        while self.is_atom_start(self.peek()):
            nxt = self.parse_select()
            n = self.node("apply", node.start, nxt.end)
            n.children = [node, nxt]
            node = n
        return node

    def parse_select(self) -> Node:
        if self._keyword_start(self.peek()):
            node = self._keyword_expr()
        else:
            node = self.parse_atom()
        while self.peek().kind == "DOT":
            self.advance()
            attr = self.peek()
            if attr.kind not in ("IDENT", "INT"):
                raise self.fail(f"expected attribute name after '.', found {attr.value!r}")
            self.advance()
            n = self.node("select", node.start, attr.end, value=attr.value)
            n.children = [node]
            node = n
        return node

    def parse_attrpath_ident(self) -> Node:
        start = self.peek().start
        parts = [self.advance()]
        while self.peek().kind == "DOT":
            self.advance()
            parts.append(self.advance())
        for tok in parts:
            if tok.kind not in ("IDENT", "INT"):
                raise self.fail(f"invalid attribute path segment {tok.value!r}")
        node = self.node("attrpath", start, parts[-1].end, value=".".join(t.value for t in parts))
        node.attrpath = [t.value for t in parts]
        return node

    def parse_atom(self) -> Node:
        tok = self.peek()
        if tok.kind == "IDENT":
            self.advance()
            return self.node("ident", tok.start, tok.end, value=tok.value)
        if tok.kind == "INT":
            self.advance()
            return self.node("int", tok.start, tok.end, value=tok.value)
        if tok.kind == "STRING":
            self.advance()
            return self.node("string", tok.start, tok.end, value=tok.value)
        if tok.kind == "PATH":
            self.advance()
            return self.node("path", tok.start, tok.end, value=tok.value)
        if tok.kind == "{":
            return self.parse_attrset_or_lambda()
        if tok.kind == "[":
            return self.parse_list()
        if tok.kind == "(":
            start = tok.start
            self.advance()
            inner = self.parse_expr()
            end = self.expect(")").end
            node = self.node("paren", start, end)
            node.children = [inner]
            return node
        if tok.kind == "KEYWORD" and tok.value == "rec":
            start = tok.start
            self.advance()
            attrs = self.parse_attrset(rec=True)
            node = self.node("rec", start, attrs.end)
            node.children = [attrs]
            return node
        raise self.fail(f"unexpected token {tok.value!r}")

    def parse_list(self) -> Node:
        start = self.peek().start
        self.expect("[")
        elements: list[Node] = []
        while self.peek().kind != "]":
            if self.peek().kind == "EOF":
                raise self.fail("unterminated list")
            elements.append(self.parse_expr())
        end = self.advance().end
        node = self.node("list", start, end)
        node.children = elements
        return node

    # -- attribute sets ------------------------------------------------------
    def _region_is_formal(self, brace_index: int) -> bool:
        """A ``{...}`` region is a function argument pattern (formal) when it
        contains a top-level comma, ``...``, or is directly followed by ``:``,
        and contains neither a top-level ``=`` nor ``;``."""
        tokens = self.tokens
        depth = 1
        comma = eq = semi = 0
        dots = 0
        j = brace_index + 1
        followed_by_colon = False
        while j < len(tokens):
            tok = tokens[j]
            if tok.kind == "EOF":
                break
            if tok.kind == "STRING":
                j += 1
                continue
            if tok.kind in ("{", "[", "("):
                depth += 1
            elif tok.kind in ("}", "]", ")"):
                depth -= 1
                if depth == 0:
                    if j + 1 < len(tokens) and tokens[j + 1].kind == ":":
                        followed_by_colon = True
                    break
            elif tok.kind == "DOT":
                dots += 1
            elif tok.kind == "," and depth == 1:
                comma += 1
            elif tok.kind == "OP" and tok.value == "=" and depth == 1:
                eq += 1
            elif tok.value == ";" and depth == 1:
                semi += 1
            j += 1
        has_ellipsis = dots >= 3
        has_comma = comma > 0
        if eq > 0 or semi > 0:
            return False
        return has_comma or has_ellipsis or followed_by_colon

    def parse_attrset_or_lambda(self) -> Node:
        brace_index = self.i
        start = self.peek().start
        if self._region_is_formal(brace_index):
            args = self.parse_formal()
            self.expect(":")
            body = self.parse_expr()
            node = self.node("lambda", start, body.end)
            node.attrpath = ["lambda"]
            node.children = [args, body]
            return node
        attrs = self.parse_attrset(rec=False)
        if self.peek().kind == ":":
            # ``{ pkgs }: ...`` typed lambda with a single named argument
            self.advance()
            body = self.parse_expr()
            node = self.node("lambda", start, body.end)
            node.attrpath = ["lambda"]
            node.children = [attrs, body]
            return node
        return attrs

    def parse_formal(self) -> Node:
        start = self.peek().start
        self.expect("{")
        entries: list[Node] = []
        while self.peek().kind != "}":
            if self.peek().kind == "EOF":
                raise self.fail("unterminated argument pattern")
            tok = self.peek()
            if tok.kind == "DOT":
                # ``...`` ellipsis
                while self.peek().kind == "DOT":
                    self.advance()
                break
            if tok.kind == "IDENT":
                name_tok = self.advance()
                entry = self.node("arg", name_tok.start, name_tok.end, value=name_tok.value)
                entry.children = []
                if self.peek().kind == "?":
                    self.advance()
                    default = self.parse_expr()
                    entry.end = default.end
                    entry.children = [default]
                entries.append(entry)
                if self.peek().kind == ",":
                    self.advance()
                    continue
                break
            raise self.fail(f"unexpected token {tok.value!r} in argument pattern")
        end = self.expect("}").end
        node = self.node("argset", start, end)
        node.attrpath = [e.value for e in entries]
        node.children = entries
        return node

    def parse_attrset(self, rec: bool) -> Node:
        start = self.peek().start
        self.expect("{")
        entries: list[Node] = []
        while self.peek().kind != "}":
            if self.peek().kind == "EOF":
                raise self.fail("unterminated attribute set")
            tok = self.peek()
            if tok.kind == "KEYWORD" and tok.value == "inherit":
                entries.append(self.parse_inherit())
                continue
            if tok.kind == "IDENT":
                entry = self.parse_binding()
                if entry.attrpath:
                    entries.append(entry)
                continue
            raise self.fail(f"unexpected token {tok.value!r} in attribute set")
        end = self.advance().end
        node = self.node("attrset", start, end)
        node.attrpath = ["attrset"]
        node.children = [c for c in entries if c.kind in ("binding", "inherit")]
        return node

    def parse_binding(self) -> Node:
        lhs = self.parse_attrpath_ident()
        self.expect("OP", "=")
        value = self.parse_expr()
        if self.peek().kind == ";":
            self.advance()
        node = self.node("binding", lhs.start, value.end, value=lhs.value)
        node.attrpath = lhs.attrpath
        node.children = [value]
        return node

    def parse_inherit(self) -> Node:
        start = self.peek().start
        self.expect("KEYWORD", "inherit")
        inherit_from: Node | None = None
        if self.peek().kind == "(":
            self.advance()
            inherit_from = self.parse_expr()
            self.expect(")")
        names: list[str] = []
        while self.peek().kind == "IDENT":
            names.append(self.advance().value)
        if self.peek().kind == ";":
            self.advance()
        node = self.node("inherit", start, (self.tokens[self.i - 1].end if names else start))
        node.attrpath = list(names)
        node.value = " ".join(names)
        node.children = [inherit_from] if inherit_from else []
        return node


def parse_module(text: str) -> Node:
    """Parse a Nix module / configuration expression."""
    parser = _Parser(text, lex(text))
    node = parser.parse_expr()
    if parser.peek().kind != "EOF":
        raise NixParseError(f"unexpected trailing tokens starting at {parser.peek().value!r}", text)
    return node


# --------------------------------------------------------------------------
# Discovery helpers
# --------------------------------------------------------------------------


def module_attrset(node: Node) -> Node:
    """Return the attribute set of interest after unwrapping lambdas/with."""
    current = node
    if current.kind == "lambda" and len(current.children) >= 2:
        current = current.children[-1]
    if current.kind == "with" and current.children:
        current = current.children[-1]
    return current


def attrset_text(node: Node) -> str:
    return node.value


def bindings(node: Node) -> list[Node]:
    root = module_attrset(node)
    if root.kind != "attrset":
        return []
    return [c for c in root.children if c.kind in ("binding", "inherit")]


def find_binding(node: Node, attrpath: Iterable[str]) -> Node | None:
    path = list(attrpath)
    for child in bindings(node):
        if child.kind == "binding" and child.attrpath == path:
            return child
    return None


def list_of(node: Node) -> Node | None:
    """Unwrap ``with pkgs; [ ... ]`` / ``( [ ... ] )`` and return a list node."""
    current = node
    seen = 0
    while seen < 8:
        if current.kind in ("with", "assert", "paren") and current.children:
            current = current.children[-1]
        else:
            break
        seen += 1
    return current if current.kind == "list" else None


def is_with_pkgs(node: Node) -> bool:
    """True when ``node`` is ``with pkgs; <body>`` (so bare names resolve)."""
    depth = 0
    current = node
    while depth < 8:
        if current.kind == "with" and current.children:
            scope = current.children[0]
            if scope.kind in ("ident", "select") and scope.value in ("pkgs", "nixpkgs"):
                return True
            current = current.children[-1]
        elif current.kind == "paren" and current.children:
            current = current.children[-1]
        else:
            return False
        depth += 1
    return False