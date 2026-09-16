"""Resolve human package names to Nix attributes against the active nixpkgs.

Nixorcist separates the human identifier (``python``) from the resolved Nix
attribute (``python3``).  Exact results depend on the selected nixpkgs
revision, so resolution always happens against the currently active Nix
environment and results are cached (see core.resolver) keyed by revision.
"""

from __future__ import annotations

import json
import re

from ..logging import Logger
from .command import NixCommand


def _best_match(requested: str, attributes: list[str]) -> str | None:
    """Pick the best attribute for the requested name.

    Preference order:
      1. attribute matches the requested name exactly;
      2. the final attribute segment matches exactly;
      3. an attribute segment equals the requested name;
      4. the attribute contains the requested name (earliest wins).
    """
    if not attributes:
        return None
    lower = requested.lower()
    exact = [a for a in attributes if a == requested or a.lower() == lower]
    if exact:
        exact.sort(key=len)
        return exact[0]
    last = [a for a in attributes if a.rsplit(".", 1)[-1].lower() == lower]
    if last:
        last.sort(key=len)
        return last[0]
    segments = [a for a in attributes if lower in [s.lower() for s in a.split(".")]]
    if segments:
        segments.sort(key=lambda a: (a.count("."), len(a)))
        return segments[0]
    contains = [a for a in attributes if lower in a.lower()]
    if contains:
        contains.sort(key=lambda a: (a.count("."), len(a)))
        return contains[0]
    return None


class NixResolver:
    """Resolves names using ``nix search`` against the nixpkgs flake."""

    def __init__(self, command: NixCommand | None = None, logger: Logger | None = None):
        self.command = command or NixCommand(logger or Logger())
        self.logger = logger or Logger()

    def resolve(self, requested: str) -> str | None:
        if not self.command.available:
            return None
        try:
            result = self.command.run(
                ["search", "--json", "--no-update-lock-file", "nixpkgs", requested], check=True
            )
        except Exception:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        attributes: list[str] = []
        for key in payload:
            if isinstance(key, str):
                attributes.append(key)
        self.logger.debug(
            f"nix search '{requested}': {len(attributes)} candidates "
            f"({', '.join(attributes[:5])}{'...' if len(attributes) > 5 else ''})"
        )
        return _best_match(requested, attributes)

    @staticmethod
    def sanitize_regex(requested: str) -> str:
        return re.escape(requested)