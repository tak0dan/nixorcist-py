"""Nix expression evaluation helpers (``nix eval``)."""

from __future__ import annotations

from .command import NixCommand


class NixEvaluator:
    def __init__(self, command: NixCommand | None = None):
        self.command = command or NixCommand()

    def eval_raw(self, expression: str) -> str | None:
        """Evaluate ``expression`` and return the raw (unquoted) value."""
        if not self.command.available:
            return None
        try:
            result = self.command.run(
                ["eval", "--raw", "--no-write-lock-file", expression], check=True
            )
        except Exception:
            return None
        return result.stdout.strip() or None

    def eval_json(self, expression: str) -> object | None:
        import json

        if not self.command.available:
            return None
        try:
            result = self.command.run(
                ["eval", "--json", "--no-write-lock-file", expression], check=True
            )
        except Exception:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def nixpkgs_revision(self) -> str:
        """Best-effort nixpkgs revision string, or ``""`` when unavailable.

        Resolution results are cached under this revision so the cache is
        invalidated whenever the relevant nixpkgs changes.
        """
        found = self.eval_raw("nixpkgs#lib.trivial.revision")
        if found:
            return found
        found = self.eval_raw("nixpkgs#lib.version")
        return found or ""