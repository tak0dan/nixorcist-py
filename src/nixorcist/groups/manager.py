"""Group Manager: high-level operations over persistent group manifests.

The Group Manager never executes ``nix profile`` or any other Nix command;
that responsibility belongs to the Profile Manager / backend.  It only reads
and mutates manifest state.
"""

from __future__ import annotations

import datetime as _dt

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import (
    Backend,
    DeclarativeMetadata,
    Group,
    GroupState,
    ResolvedPackage,
    dedupe_packages,
)
from ..core.resolver import PackageResolver
from ..logging import Logger
from .manifest import GroupManifest
from .repository import GroupRepository


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

_GROUP_NAME_BAD = set("{}#,.*")


class GroupError(NixorcistError):
    pass


def validate_group_name(name: str) -> None:
    if not name:
        raise GroupError(Diagnostic(ErrorCode.INVALID_GROUP, "group names cannot be empty"))
    if any(ch in _GROUP_NAME_BAD for ch in name):
        raise GroupError(
            Diagnostic(
                ErrorCode.INVALID_GROUP,
                f"group name {name!r} contains invalid characters",
                suggestion="group names may contain letters, digits, '_', '.', '-' and '+', but not {{}}#,.*",
            )
        )


class GroupManager:
    def __init__(
        self,
        repository: GroupRepository,
        resolver: PackageResolver | None = None,
        logger: Logger | None = None,
    ):
        self.repository = repository
        self.resolver = resolver
        self.logger = logger or Logger()

    # -- name resolution -------------------------------------------------
    def canonical(self, name: str) -> str | None:
        """Resolve a name (case-insensitively) to its canonical/display name."""
        manifest = self.repository.load(name)
        return manifest.name if manifest else None

    def get(self, name: str) -> Group | None:
        manifest = self.repository.load(name)
        return manifest.as_group() if manifest else None

    # -- mutations --------------------------------------------------------
    def create(self, name: str, description: str = "") -> Group:
        validate_group_name(name)
        existing = self.repository.load(name)
        if existing is not None:
            return existing.as_group()
        manifest = GroupManifest(
            name=name,
            description=description,
            packages=[],
            state=GroupState(active=False, backend=Backend.NONE),
            created_at=_now(),
            updated_at=_now(),
        )
        self.repository.save(manifest)
        self.logger.debug(f"created group {name}")
        return manifest.as_group()

    def ensure(self, name: str) -> Group:
        return self.create(name)

    def add(self, name: str, packages: list[ResolvedPackage]) -> Group:
        manifest = self.repository.load(name)
        if manifest is None:
            raise GroupError(
                Diagnostic(
                    ErrorCode.INVALID_GROUP,
                    f"group '{name}' does not exist",
                    suggestion="create it first with 'nist group create <name>' or use -G#<name>",
                )
            )
        existing: dict[str, ResolvedPackage] = {}
        for pkg in manifest.packages:
            existing.setdefault(pkg.requested, pkg)
        changed = False
        for pkg in packages:
            if pkg.requested not in existing:
                existing[pkg.requested] = pkg
                changed = True
        if changed:
            manifest.packages = list(existing.values())
            self.repository.save(manifest)
        return manifest.as_group()

    def remove(self, name: str, requested: list[str]) -> Group:
        manifest = self.repository.load(name)
        if manifest is None:
            raise GroupError(
                Diagnostic(ErrorCode.INVALID_GROUP, f"group '{name}' does not exist")
            )
        wanted = set(requested)
        manifest.packages = [p for p in manifest.packages if p.requested not in wanted]
        self.repository.save(manifest)
        return manifest.as_group()

    def delete(self, name: str) -> bool:
        return self.repository.delete(name)

    def obliterate(self, name: str) -> bool:
        """``-O``: permanently remove a group from the registry (spec §35).

        Destruction of the metadata object is separate from configurament
        deactivation (-E) and from declarative cleanup (-Oo...o).
        """
        existed = self.repository.load(name) is not None
        self.repository.delete(name)
        if existed:
            self.logger.debug(f"obliterated group {name}")
        return existed

    def yield_group(
        self, name: str, packages: list[ResolvedPackage], description: str = ""
    ) -> Group:
        """``-Y``: adopt/import a group from the NixOS configuration (spec §44).

        Creates the registry entry if absent, or refreshes its membership if
        already known (orphan adoption).  The configuration itself is never
        modified by this operation.
        """
        validate_group_name(name)
        existing = self.repository.load(name)
        if existing is None:
            manifest = GroupManifest(
                name=name,
                description=description,
                packages=list(dedupe_packages(packages)),
                state=GroupState(active=False, backend=Backend.NONE),
                created_at=_now(),
                updated_at=_now(),
            )
            self.repository.save(manifest)
            self.logger.debug(f"yielded group {name} from configuration")
            return manifest.as_group()
        existing.packages = list(dedupe_packages(packages))
        existing.updated_at = _now()
        self.repository.save(existing)
        self.logger.debug(f"refreshed yielded group {name}")
        return existing.as_group()

    def rename(self, old: str, new: str) -> Group:
        validate_group_name(new)
        manifest = self.repository.load(old)
        if manifest is None:
            raise GroupError(
                Diagnostic(ErrorCode.INVALID_GROUP, f"group '{old}' does not exist")
            )
        if self.repository.load(new) is not None and new.lower() != old.lower():
            raise GroupError(
                Diagnostic(ErrorCode.INVALID_GROUP, f"group '{new}' already exists")
            )
        manifest.name = new
        self.repository.save(manifest)
        self.logger.debug(f"renamed group {old} -> {new}")
        return manifest.as_group()

    # -- activation state (orthogonal to membership) -----------------------
    def _mutate_state(self, name: str, fn) -> Group:
        """Load a group, apply ``fn(manifest)``, persist, return the new Group."""
        manifest = self.repository.load(name)
        if manifest is None:
            raise GroupError(
                Diagnostic(ErrorCode.INVALID_GROUP, f"group '{name}' does not exist")
            )
        fn(manifest)
        manifest.updated_at = _now()
        self.repository.save(manifest)
        return manifest.as_group()

    def state_of(self, name: str) -> GroupState | None:
        manifest = self.repository.load(name)
        return manifest.state if manifest else None

    def activate(self, name: str, backend: Backend | str) -> Group:
        """``-A``: set ``active = True`` and the activation target."""
        target = backend if isinstance(backend, Backend) else Backend(str(backend).lower())
        return self._mutate_state(
            name, lambda m: setattr(m, "state", GroupState(active=True, backend=target))
        )

    def deactivate(self, name: str) -> Group:
        """``-E``: set ``active = False``, *retaining* the backend."""
        return self._mutate_state(
            name,
            lambda m: setattr(m, "state", GroupState(active=False, backend=m.state.backend)),
        )

    def promote(self, name: str) -> Group:
        """``-P``: backend := declarative; activation is unchanged."""
        return self._mutate_state(
            name,
            lambda m: setattr(m, "state", GroupState(active=m.state.active, backend=Backend.DECLARATIVE)),
        )

    def demote(self, name: str) -> Group:
        """``-D``: backend := imperative; activation is unchanged."""
        return self._mutate_state(
            name,
            lambda m: setattr(m, "state", GroupState(active=m.state.active, backend=Backend.IMPERATIVE)),
        )

    def set_declarative_metadata(self, name: str, metadata: DeclarativeMetadata) -> Group:
        return self._mutate_state(name, lambda m: setattr(m, "declarative", metadata))

    # -- queries ----------------------------------------------------------
    def list_groups(self) -> list[str]:
        return [m.name for m in self.repository.load_all()]

    def all_groups(self) -> list[Group]:
        return [m.as_group() for m in self.repository.load_all()]

    def group_packages(self, name: str) -> list[ResolvedPackage]:
        group = self.get(name)
        return list(group.packages) if group else []

    def union_contents(self, names: list[str]) -> list[ResolvedPackage]:
        merged: list[ResolvedPackage] = []
        for name in names:
            group = self.get(name)
            if group:
                merged.extend(group.packages)
        return dedupe_packages(merged)