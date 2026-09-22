"""Nix profile backend: wraps ``nix profile``.

Installs use ``nixpkgs#<attribute>`` references.  Removals resolve the
requested/attribute names against ``nix profile list`` to find the correct
profile elements, so ``-R python`` (which resolves to ``python3``) finds a
profile entry named ``nixpkgs-python3-...``.
"""

from __future__ import annotations

import json
from typing import Iterable

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import InstalledProfileEntry, ResolvedPackage
from ..logging import Logger
from ..nix.command import NixCommand
from .base import PackageBackend

_FLAKE_PREFIX = "nixpkgs"


class ProfileError(NixorcistError):
    pass


def _attr_bucket(attr_path: str) -> str:
    return attr_path.rsplit(".", 1)[-1]


class NixProfile(PackageBackend):
    def __init__(
        self,
        command: NixCommand | None = None,
        profile_path: str | None = None,
        logger: Logger | None = None,
    ):
        self.logger = logger or Logger()
        self.command = command or NixCommand(logger=self.logger)
        self.profile_path = profile_path

    @property
    def name(self) -> str:
        return "nix-profile"

    def _base_args(self) -> list[str]:
        if self.profile_path:
            return ["--profile", self.profile_path]
        return []

    def install(self, packages: list[ResolvedPackage]) -> None:
        if not packages:
            return
        if not self.command.available:
            raise ProfileError(
                Diagnostic(
                    ErrorCode.NIX_COMMAND,
                    "'nix' is not available; cannot install into the profile",
                )
            )
        refs = [f"{_FLAKE_PREFIX}#{p.attribute}" for p in packages]
        self.command.run(["profile", "add", *self._base_args(), "--no-update-lock-file", *refs], check=True)

    def remove(self, packages: list[ResolvedPackage]) -> None:
        if not packages:
            return
        entries = self.list_entries()
        wanted: set[str] = {p.attribute for p in packages}
        buckets: set[str] = {_attr_bucket(p.attribute) for p in packages}
        matched: list[str] = []
        for e in entries:
            if e.attribute in wanted or _attr_bucket(e.attribute) in buckets:
                if e.store_path and e.store_path not in matched:
                    matched.append(e.store_path)
        if not matched:
            self.logger.debug(f"nothing to remove: no profile element matches {sorted(wanted)}")
            self.logger.info(
                "Nothing to remove from the profile "
                f"({', '.join(sorted(wanted))} not installed)."
            )
            return
        self.command.run(["profile", "remove", *self._base_args(), *matched], check=True)

    def remove_all(self) -> None:
        entries = self.list_entries()
        if not entries:
            return
        paths = [e.store_path for e in entries if e.store_path]
        if not paths:
            return
        self.command.run(["profile", "remove", *self._base_args(), *paths], check=True)

    def list_entries(self) -> list[InstalledProfileEntry]:
        if not self.command.available:
            return []
        result = self.command.run(["profile", "list", *self._base_args(), "--json"], check=True)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        elements = payload.get("elements", []) if isinstance(payload, dict) else []
        if isinstance(elements, dict):
            elements = list(elements.values())
        entries: list[InstalledProfileEntry] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            attr_path = element.get("attrPath") or element.get("attr", "") or ""
            name = element.get("name") or ""
            if isinstance(attr_path, list):
                attr_path = ".".join(a for a in attr_path if isinstance(a, str))
            paths = element.get("storePaths") or []
            entries.append(
                InstalledProfileEntry(
                    store_path=paths[0] if paths else "",
                    name=str(name),
                    attribute=str(attr_path),
                )
            )
        return entries