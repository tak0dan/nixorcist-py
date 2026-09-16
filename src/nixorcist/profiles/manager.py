"""Profile Manager: install/remove/status across a :class:`PackageBackend`.

The manifest (what groups say) and the profile (what is actually installed)
are independent states and may diverge; nothing here conflates them.
"""

from __future__ import annotations

from ..backends.base import PackageBackend
from ..core.models import ResolvedPackage, SyncStatus, dedupe_packages
from ..logging import Logger


class ProfileManager:
    def __init__(self, backend: PackageBackend, logger: Logger | None = None):
        self.backend = backend
        self.logger = logger or Logger()

    def install(self, packages: list[ResolvedPackage]) -> None:
        if not packages:
            self.logger.debug("no packages to install")
            return
        self.logger.info("Installing...")
        for pkg in packages:
            self.logger.info(f"  * {pkg.attribute}")
        self.backend.install(packages)

    def remove(self, packages: list[ResolvedPackage]) -> None:
        if not packages:
            return
        for pkg in packages:
            self.logger.info(f"Removing {pkg.attribute} from the profile...")
        self.backend.remove(packages)

    def remove_all(self) -> None:
        self.backend.remove_all()

    def installed_attributes(self) -> set[str]:
        return self.backend.installed_attributes()

    def installed_requested(self) -> set[str]:
        attrs = self.backend.installed_attributes()
        return {a.rsplit(".", 1)[-1] for a in attrs if a}

    def status(self, groups: list, requested_names: list[str] | None = None) -> list[SyncStatus]:
        """Compare desired group membership with the installed profile."""
        installed = self.installed_attributes()
        installed_keys = {a.rsplit(".", 1)[-1] for a in installed if a}
        requested = (
            sorted({p.requested for g in groups for p in g.packages})
            if requested_names is None
            else sorted(set(requested_names))
        )
        return [
            SyncStatus(
                requested=r,
                group=any(r in g.requested_names() for g in groups),
                profile=r in installed_keys,
            )
            for r in requested
        ]