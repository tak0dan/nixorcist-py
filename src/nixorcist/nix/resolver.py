"""Resolve human package names to Nix attributes against the active nixpkgs.

Nixorcist separates the human identifier (``python``) from the resolved Nix
attribute (``python3``).  Exact results depend on the selected nixpkgs
revision, so resolution always happens against the currently active Nix
environment and results are cached (see core.resolver) keyed by revision.
"""

from __future__ import annotations

import json
import platform
import re

from ..logging import Logger
from .command import NixCommand


def _nix_system() -> str:
    """Detect the Nix system string dynamically.

    Returns the current system in Nix format (e.g., ``x86_64-linux``,
    ``aarch64-darwin``).  Falls back to ``x86_64-linux`` if detection fails.
    """
    machine = platform.machine()
    system = platform.system().lower()
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    os_map = {
        "linux": "linux",
        "darwin": "darwin",
        "freebsd": "freebsd",
    }
    nix_arch = arch_map.get(machine, machine)
    nix_os = os_map.get(system, system)
    return f"{nix_arch}-{nix_os}"


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
    """Resolves names using ``nix search`` against the nixpkgs flake.

    Resolution uses a two-phase approach:
      1. Fast path: try the direct attribute path ``legacyPackages.<system>.<name>``
      2. Full search: fall back to ``nix search`` if the direct path fails
    """

    def __init__(self, command: NixCommand | None = None, logger: Logger | None = None):
        self.command = command or NixCommand(logger or Logger())
        self.logger = logger or Logger()
        self._system: str | None = None

    @property
    def system(self) -> str:
        if self._system is None:
            self._system = _nix_system()
        return self._system

    def _try_direct(self, requested: str) -> str | None:
        """Try to evaluate the direct attribute path.

        Attempts ``legacyPackages.<system>.<requested>`` directly via
        ``nix eval``.  Returns the full attribute path on success, None
        if the attribute doesn't exist.
        """
        attr = f"legacyPackages.{self.system}.{requested}"
        try:
            result = self.command.run(
                ["eval", "--json", "--no-write-lock-file", f"nixpkgs#{attr}"],
                check=True,
            )
            if result.stdout.strip():
                return attr
        except Exception:
            pass
        return None

    def _search(self, requested: str) -> list[str]:
        """Run ``nix search`` and return all matching attribute paths."""
        try:
            result = self.command.run(
                ["search", "--json", "--no-update-lock-file", "nixpkgs", requested],
                check=True,
            )
        except Exception:
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        return [k for k in payload if isinstance(k, str)]

    def resolve(self, requested: str) -> str | None:
        if not self.command.available:
            return None

        # Phase 1: fast path — try direct attribute
        direct = self._try_direct(requested)
        if direct:
            self.logger.debug(f"direct resolve '{requested}' -> {direct}")
            return direct

        # Phase 2: full search
        attributes = self._search(requested)
        if attributes:
            self.logger.debug(
                f"nix search '{requested}': {len(attributes)} candidates "
                f"({', '.join(a.rsplit('.', 1)[-1] for a in attributes[:5])}"
                f"{'...' if len(attributes) > 5 else ''})"
            )
            match = _best_match(requested, attributes)
            if match:
                return match

        self.logger.debug(f"failed to resolve '{requested}'")
        return None

    @staticmethod
    def sanitize_regex(requested: str) -> str:
        return re.escape(requested)
