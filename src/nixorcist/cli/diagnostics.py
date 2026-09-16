"""Diagnostics, error codes, and rendering for Nixorcist.

Error codes (see docs/architecture.md):

    TN001  CLI syntax error
    TN002  invalid group
    TN003  invalid package expression
    TN004  package resolution failure
    TN005  group storage failure
    TN006  Nix command failure
    TN007  Nix configuration parse failure
    TN008  configuration discovery failure
    TN009  promotion failure
    TN010  validation failure
    TN102  positional assignment overflow
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class ErrorCode(str, Enum):
    SYNTAX = "TN001"
    INVALID_GROUP = "TN002"
    INVALID_PACKAGE = "TN003"
    RESOLUTION = "TN004"
    GROUP_STORAGE = "TN005"
    NIX_COMMAND = "TN006"
    NIX_PARSE = "TN007"
    DISCOVERY = "TN008"
    PROMOTION = "TN009"
    VALIDATION = "TN010"
    POSITIONAL_OVERFLOW = "TN102"


@dataclass(frozen=True)
class Span:
    """A source span, expressed as character offsets into an expression."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid span ({self.start}, {self.end})")

    @classmethod
    def empty(cls, at: int) -> "Span":
        return cls(at, at)


@dataclass(frozen=True)
class Diagnostic:
    code: ErrorCode
    message: str
    expression: str = ""
    spans: tuple[Span, ...] = ()
    notes: tuple[str, ...] = ()
    suggestion: str = ""

    def rendered(self) -> str:
        return render_diagnostic(self)


def _render_underlines(expression: str, spans: Iterable[Span]) -> str:
    lines: list[str] = []
    for span in spans:
        width = max(1, span.end - span.start)
        lines.append(" " * span.start + "^" + "~" * (width - 1))
    return "\n".join(lines)


def render_diagnostic(d: Diagnostic) -> str:
    severity = "error"
    lines: list[str] = []
    lines.append(f"{severity}[{d.code.value}]: {d.message}")
    if d.expression:
        lines.append("")
        lines.append("Expression:")
        lines.append(f"  {d.expression}")
        if d.spans:
            lines[-1] = lines[-1] + ""
            underline = _render_underlines(d.expression, d.spans)
            for line in underline.splitlines():
                lines.append("  " + line)
    if d.notes:
        lines.append("")
        for note in d.notes:
            lines.append(f"  {note}")
    if d.suggestion:
        lines.append("")
        lines.append(f"  hint: {d.suggestion}")
    return "\n".join(lines)


def syntax_error(expression: str, span: Span, message: str, suggestion: str = "") -> Diagnostic:
    return Diagnostic(
        ErrorCode.SYNTAX,
        message,
        expression=expression,
        spans=(span,),
        suggestion=suggestion,
    )


class NixorcistError(Exception):
    """An error carrying a structured diagnostic."""

    def __init__(self, diagnostic: Diagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    def rendered(self) -> str:
        return self.diagnostic.rendered()


@dataclass
class Reporter:
    """Collects diagnostics so the CLI can defer reporting until parsing is done."""

    diagnostics: list[Diagnostic] = field(default_factory=list)

    def emit(self, diagnostic: Diagnostic) -> NixorcistError:
        self.diagnostics.append(diagnostic)
        return NixorcistError(diagnostic)

    @property
    def has_errors(self) -> bool:
        return any(d.code is not None for d in self.diagnostics)

    def render_all(self) -> str:
        return "\n\n".join(render_diagnostic(d) for d in self.diagnostics)

    def report(self, diagnostics) -> None:
        """Render diagnostics to stderr (spec §54: clean, structured errors).

        Accepts a single :class:`Diagnostic` or any iterable of them.
        """
        items = list(diagnostics) if not isinstance(diagnostics, Diagnostic) else [diagnostics]
        self.diagnostics.extend(items)
        for d in items:
            print(render_diagnostic(d), file=sys.stderr)