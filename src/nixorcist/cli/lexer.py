"""Lexer for the compact Nixorcist DSL.

The DSL is whitespace-insensitive at the token level, so a single argv
element such as ``-IG#{Programming,Games}##{python,gcc}{steam}`` and a
space-separated form ``-I -G # Programming ...`` lex identically.

Token forms (see docs/dsl.md):

    -I  / --install      install operation
    -R  / --remove       remove operation
    -G  / --group        group-scope operation
    #                    broadcast assignment / scope separator
    ##                   positional assignment
    { }                  collection brackets
    ,                    collection separator
    *                    "all groups" scope
    .                    "profile only" scope
    NAME                 identifiers (packages / groups / values)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .diagnostics import ErrorCode, NixorcistError, Span, syntax_error


class TokenKind(str, Enum):
    INSTALL = "INSTALL"
    REMOVE = "REMOVE"
    GROUP = "GROUP"
    BROADCAST = "BROADCAST"
    POSITIONAL = "POSITIONAL"
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    COMMA = "COMMA"
    STAR = "STAR"
    DOT = "DOT"
    NAME = "NAME"
    EOF = "EOF"
    # Corrected-state-model operations / modifiers (spec §§10–12, 25–28)
    PROMOTE = "PROMOTE"
    DEMOTE = "DEMOTE"
    DEACTIVATE = "DEACTIVATE"
    ACTIVATE = "ACTIVATE"
    SEQUENCE = "SEQUENCE"
    IMPERATIVE = "IMPERATIVE"
    DECLARATIVE = "DECLARATIVE"
    # Obliteration / yielding (spec §§33-63)
    OBLITERATE = "OBLITERATE"
    YIELD = "YIELD"
    SAVE = "SAVE"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    offset: int
    end: int

    @property
    def span(self) -> Span:
        return Span(self.offset, self.end)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Token({self.kind.value}, {self.text!r})"


_LONG_FLAGS = {
    "--install": TokenKind.INSTALL,
    "--remove": TokenKind.REMOVE,
    "--group": TokenKind.GROUP,
    "--promote": TokenKind.PROMOTE,
    "--declare": TokenKind.PROMOTE,
    "--demote": TokenKind.DEMOTE,
    "--deactivate": TokenKind.DEACTIVATE,
    "--sync": TokenKind.ACTIVATE,
    "--activate": TokenKind.ACTIVATE,
    "--sequence": TokenKind.SEQUENCE,
    "--imperative": TokenKind.IMPERATIVE,
    "--declarative": TokenKind.DECLARATIVE,
    "--obliterate": TokenKind.OBLITERATE,
    "--delete-group": TokenKind.OBLITERATE,
    "--yield": TokenKind.YIELD,
    "--save": TokenKind.SAVE,
}

_SHORT_PREFIX = "-"
_PREFIX_KIND = {
    "I": TokenKind.INSTALL,
    "R": TokenKind.REMOVE,
    "G": TokenKind.GROUP,
    "P": TokenKind.PROMOTE,
    "D": TokenKind.DEMOTE,
    "E": TokenKind.DEACTIVATE,
    "A": TokenKind.ACTIVATE,
    "S": TokenKind.SEQUENCE,
    "i": TokenKind.IMPERATIVE,
    "d": TokenKind.DECLARATIVE,
    "O": TokenKind.OBLITERATE,
    "Y": TokenKind.YIELD,
    "s": TokenKind.SAVE,
}

_NAME_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
# Subsequent characters may include `.`, `-` and `+` (attribute paths such as
# ``kdePackages.kate`` or versioned names).
_NAME_BODY = _NAME_START | set(".-+")

_WHITESPACE = set(" \t\r\n")


def _is_merged_flag(tok: Token) -> bool:
    """True when a short-prefix token texts looks like ``-XY`` (two prefix
    characters merged, e.g. ``-IG``, ``-PS``, ``-Ai``)."""
    return (
        tok.text.startswith("-")
        and len(tok.text) == 3
        and tok.text[1] in _PREFIX_KIND
        and tok.text[2] in _PREFIX_KIND
    )


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self._tokens: list[Token] | None = None

    def _error(self, at: int, message: str, suggestion: str = "") -> NixorcistError:
        raise NixorcistError(syntax_error(self.text, Span(at, at + 1), message, suggestion))

    def _start_name(self, offset: int) -> Token:
        pos = offset
        while pos < len(self.text) and self.text[pos] in _NAME_BODY:
            pos += 1
        return Token(TokenKind.NAME, self.text[offset:pos], offset, pos)

    def next_token(self, start: int) -> Token:
        pos = start
        text = self.text
        n = len(text)
        while pos < n and text[pos] in _WHITESPACE:
            pos += 1
        if pos >= n:
            return Token(TokenKind.EOF, "<eof>", pos, pos)

        ch = text[pos]

        if ch == "-":
            for flag, kind in _LONG_FLAGS.items():
                if text.startswith(flag, pos):
                    return Token(kind, flag, pos, pos + len(flag))
            # Short prefixes may be merged: ``-IG`` -> -I -G, ``-Ai`` -> -A -i.
            if pos + 1 < n and text[pos + 1] in _PREFIX_KIND:
                second = text[pos + 2] if pos + 2 < n else ""
                if second in _PREFIX_KIND:
                    # Merged pair; tokenize() splits it into two tokens.
                    first_kind = _PREFIX_KIND[text[pos + 1]]
                    return Token(first_kind, text[pos : pos + 3], pos, pos + 3)
                return Token(
                    _PREFIX_KIND[text[pos + 1]], text[pos : pos + 2], pos, pos + 2
                )
            self._error(pos, f"unknown flag {ch!r}")
            raise AssertionError("unreachable")

        if ch == "#":
            if pos + 1 < n and text[pos + 1] == "#":
                return Token(TokenKind.POSITIONAL, "##", pos, pos + 2)
            return Token(TokenKind.BROADCAST, "#", pos, pos + 1)

        if ch == "{":
            return Token(TokenKind.LBRACE, ch, pos, pos + 1)
        if ch == "}":
            return Token(TokenKind.RBRACE, ch, pos, pos + 1)
        if ch == ",":
            return Token(TokenKind.COMMA, ch, pos, pos + 1)
        if ch == "*":
            return Token(TokenKind.STAR, ch, pos, pos + 1)
        if ch == ".":
            return Token(TokenKind.DOT, ch, pos, pos + 1)

        if ch in _NAME_START:
            return self._start_name(pos)

        self._error(pos, f"unexpected character {ch!r}", "expected an operator, a group, or a package name")
        raise AssertionError("unreachable")

    def tokenize(self) -> list[Token]:
        if self._tokens is not None:
            return self._tokens
        tokens: list[Token] = []
        pos = 0
        while True:
            tok = self.next_token(pos)
            if not _is_merged_flag(tok):
                tokens.append(tok)
                pos = tok.end
            else:
                # "-XY" merges into two tokens: -X (op) -Y (modifier/op).
                first = Token(tok.kind, "-" + tok.text[1], tok.offset, tok.offset + 2)
                second_kind = _PREFIX_KIND[tok.text[2]]
                second = Token(second_kind, "-" + tok.text[2], tok.offset + 1, tok.offset + 3)
                tokens.extend([first, second])
                pos = tok.end
            if tok.kind is TokenKind.EOF:
                break
        self._tokens = tokens
        return tokens


def lex(text: str) -> list[Token]:
    return Lexer(text).tokenize()