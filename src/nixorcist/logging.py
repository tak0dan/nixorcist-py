"""Minimal console logging for Nixorcist.

Normal operation is concise; ``--debug`` exposes lexer tokens, ASTs, Nix
commands, filesystem operations and planning decisions.
"""

from __future__ import annotations

import sys


class Logger:
    def __init__(self, debug: bool = False, dry_run: bool = False, stream=sys.stderr):
        self.debug_enabled = debug
        self.dry_run = dry_run
        self.stream = stream

    def info(self, message: str) -> None:
        print(message, file=self.stream)

    def debug(self, message: str) -> None:
        if self.debug_enabled:
            print(f"[debug] {message}", file=self.stream)

    def dry(self, message: str) -> None:
        if self.dry_run:
            print(f"[dry-run] {message}", file=self.stream)

    def command(self, argv: list[str]) -> None:
        self.debug("$ " + " ".join(argv))

    def error(self, message: str) -> None:
        print(f"error: {message}", file=self.stream)

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=self.stream)

    def ok(self, message: str) -> None:
        print(f"[ok] {message}" if not self.dry_run else f"[dry-run] {message}", file=self.stream)


def ok_mark() -> str:
    return "[ok]"