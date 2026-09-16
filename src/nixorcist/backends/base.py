"""Backend abstraction.

Nixorcist's group system is independent of the underlying installation
target.  Two kinds of backends exist:

* :class:`PackageBackend` — performs imperative package operations
  (e.g. ``nix profile``).
* :class:`DeclarativeBackend` — promotes groups into declarative
  configuration (NixOS today, Home Manager / nix-darwin later).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..core.models import InstalledProfileEntry, ResolvedPackage


class PackageBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def install(self, packages: list[ResolvedPackage]) -> None: ...

    @abstractmethod
    def remove(self, packages: list[ResolvedPackage]) -> None: ...

    @abstractmethod
    def remove_all(self) -> None: ...

    @abstractmethod
    def list_entries(self) -> list[InstalledProfileEntry]: ...

    def installed_attributes(self) -> set[str]:
        entries = self.list_entries()
        result: set[str] = set()
        for entry in entries:
            if entry.attribute:
                result.add(entry.attribute)
        return result

    def is_installed(self, package: ResolvedPackage) -> bool:
        attributes = self.installed_attributes()
        if package.attribute and package.attribute in attributes:
            return True
        return any(a.rsplit(".", 1)[-1] == package.attribute for a in attributes)


@dataclass
class DiscoveredTarget:
    """A discovered declarative configuration target."""

    root: Path
    kind: str  # "nixos" | "home-manager" | "flake" | ...


class DeclarativeBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def discover(self) -> DiscoveredTarget: ...

    @abstractmethod
    def plan(self, groups: list[tuple[str, list[ResolvedPackage]]]) -> object:
        ...

    @abstractmethod
    def validate(self, plan: object) -> object:
        ...

    @abstractmethod
    def apply(self, plan: object) -> None: ...