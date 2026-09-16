"""Home Manager declarative backend.

The spec calls for home-manager support (§36); full integration requires
a separate evaluation harness similar to ``nixos-rebuild``.  This stub
provides the interface so the CLI can route to it; install is explicitly
refused until a ``home-manager`` subprocess (or a ``nix build .#`` target)
is wired in, and the plan mode is documented.
"""

from __future__ import annotations

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import ResolvedPackage
from .base import DiscoveredTarget, PackageBackend


class HomeManagerError(NixorcistError):
    pass


class HomeManagerBackend(PackageBackend):
    name = "home-manager"

    def install(self, packages: list[ResolvedPackage], *, dry_run: bool = False) -> bool:
        return False

    def remove(self, packages: list[ResolvedPackage], *, dry_run: bool = False) -> bool:
        return False

    def remove_all(self, *, dry_run: bool = False) -> bool:
        return False

    def is_installed(self, pkg: ResolvedPackage, *, dry_run: bool = False) -> bool:
        return False

    def list_entries(self, *, dry_run: bool = False) -> list[dict[str, object]]:
        return []

    def installed_attributes(self, *, dry_run: bool = False) -> set[str]:
        return set()

    def discovered_targets(self, *, dry_run: bool = False) -> list[DiscoveredTarget]:
        return []