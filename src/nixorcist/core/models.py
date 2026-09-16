"""Core data models shared across Nixorcist subsytems.

These models are backend-independent: they describe *intent* (what the user
wants) and *manifest state* (what the manifests claim), distinct from what is
actually installed or declared in a NixOS configuration.

Group state uses two orthogonal dimensions (see the corrected state model):

* ``active`` -- whether the group is activated
* ``backend`` -- *where* it is (or was) activated: ``imperative`` (Nix
  profile), ``declarative`` (NixOS configuration) or ``none``.

``promote``/``demote`` change the backend dimension; ``-A``/``-E`` change the
activation dimension; package membership and profile installation are fully
independent properties.
"""

from __future__ import annotations

import datetime as _dt
import uuid as _uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ResolvedPackage:
    """A package name resolved to a concrete Nix attribute.

    ``requested`` is the human identifier the user typed; ``attribute`` is the
    resolution result.  The conceptual identity of a package is
    ``(source, requested)``; the attribute is a resolution result and may be
    re-resolved when nixpkgs reorganizes attributes.
    """

    requested: str
    attribute: str
    source: str = "nixpkgs"
    revision: str = ""
    resolution_timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.requested,
            "attribute": self.attribute,
            "source": self.source,
            "nixpkgs_revision": self.revision,
            "resolution_timestamp": self.resolution_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResolvedPackage":
        return cls(
            requested=data.get("name", ""),
            attribute=data.get("attribute", data.get("name", "")),
            source=data.get("source", "nixpkgs"),
            revision=data.get("nixpkgs_revision", ""),
            resolution_timestamp=data.get("resolution_timestamp", ""),
        )


@dataclass
class Group:
    """A named logical collection of packages (independent of install state).

    The group's activation state is orthogonal to its membership: a group can
    be inactive yet still carry packages, or declaratively *known* without
    being active.
    """

    name: str
    packages: list[ResolvedPackage] = field(default_factory=list)
    state: "GroupState" = field(default_factory=lambda: GroupState())
    declarative: Optional["DeclarativeMetadata"] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def requested_names(self) -> set[str]:
        return {p.requested for p in self.packages}


class Backend(str, Enum):
    NONE = "none"
    IMPERATIVE = "imperative"
    DECLARATIVE = "declarative"


@dataclass(frozen=True)
class GroupState:
    """Activation state: ``active`` (bool) x ``backend`` (Backend).

    For an inactive group the backend is *retained* so that a later ``-A``
    can restore the remembered target::

        active=False, backend=IMPERATIVE  -> "deactivated imperative group"
        active=False, backend=DECLARATIVE -> "deactivated declarative group"
        active=False, backend=NONE        -> "newly created / never activated"
    """

    active: bool = False
    backend: Backend = Backend.NONE


@dataclass(frozen=True)
class DeclarativeMetadata:
    """Known declarative representation/history of a group.

    This information survives demotion: it describes *where* the group has a
    declarative representation, which is distinct from whether the group is
    currently declaratively active.
    """

    configuration_root: str = ""
    module: str = ""
    declaration_path: str = ""
    declaration: str = ""  # "inline" | "module" | ""
    last_successful_candidate: str = ""
    last_promotion: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "configuration_root": self.configuration_root,
            "module": self.module,
            "declaration_path": self.declaration_path,
            "declaration": self.declaration,
            "last_successful_candidate": self.last_successful_candidate,
            "last_promotion": self.last_promotion,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DeclarativeMetadata":
        if not data:
            return cls()
        return cls(
            configuration_root=str(data.get("configuration_root", "")),
            module=str(data.get("module", "")),
            declaration_path=str(data.get("declaration_path", "")),
            declaration=str(data.get("declaration", "")),
            last_successful_candidate=str(data.get("last_successful_candidate", "")),
            last_promotion=str(data.get("last_promotion", "")),
        )


@dataclass(frozen=True)
class InstalledProfileEntry:
    """One entry reported by ``nix profile list``."""

    store_path: str
    name: str
    attribute: str = ""

    @property
    def package_key(self) -> str:
        """A coarse human-ish identifier for display/matching."""
        # nix profile names look like "nixpkgs-python3-3.11.1" or the attr
        # when referenced.  Strip common ``nixpkgs-`` prefixes.
        if self.attribute:
            return self.attribute
        base = self.name
        for prefix in ("nixpkgs-",):
            if base.startswith(prefix):
                base = base[len(prefix):]
        return base.split("-")[0]


@dataclass(frozen=True)
class SyncStatus:
    """Per-package membership across (desired) groups and (actual) profile."""

    requested: str
    group: bool
    profile: bool


def dedupe_packages(packages: list[ResolvedPackage]) -> list[ResolvedPackage]:
    """Order-preserving dedup on ``(source, requested)``."""
    seen: set[tuple[str, str]] = set()
    out: list[ResolvedPackage] = []
    for p in packages:
        key = (p.source, p.requested)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def new_uid() -> str:
    return _uuid.uuid4().hex[:12]