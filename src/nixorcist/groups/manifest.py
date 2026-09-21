"""Versioned TOML serialization of group manifests.

Schema v1 (see docs/manifest.md)::

    version = 1

    [group]
    name = "Programming"
    description = ""
    active = false
    backend = "imperative"
    created_at = "..."
    updated_at = "..."

    [group.declarative]
    module = "packages/programming.nix"
    configuration_root = "/etc/nixos"
    declaration = "module"
    last_successful_candidate = "a91f3c"

    [[group.packages]]
    name = "python"
    attribute = "python3"
    source = "nixpkgs"
    nixpkgs_revision = "..."
    resolution_timestamp = "2026-01-01T00:00:00+00:00"

The ``schema_version`` MUST be checked before any future migration.  The
attribute is a resolution result, not the package identity.  ``active`` and
``backend`` are the orthogonal activation dimensions; ``declarative``
metadata survives demotion.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import BinaryIO

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import Backend, DeclarativeMetadata, Group, GroupState, ResolvedPackage

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

SCHEMA_VERSION = 1

_PACKAGE_KEYS = ("name", "attribute", "source", "nixpkgs_revision", "resolution_timestamp")


@dataclass
class GroupManifest:
    schema_version: int = SCHEMA_VERSION
    name: str = ""
    description: str = ""
    packages: list[ResolvedPackage] = field(default_factory=list)
    state: GroupState = field(default_factory=GroupState)
    declarative: DeclarativeMetadata | None = None
    created_at: str = ""
    updated_at: str = ""

    def as_group(self) -> Group:
        return Group(
            name=self.name,
            packages=list(self.packages),
            state=self.state,
            declarative=self.declarative,
            created_at=self.created_at or _now(),
            updated_at=self.updated_at or _now(),
        )


def _now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class ManifestError(NixorcistError):
    pass


def serialize(manifest: GroupManifest) -> str:
    lines: list[str] = [f"version = {manifest.schema_version}", ""]
    lines.append("[group]")
    lines.append(f"name = {_toml_str(manifest.name)}")
    if manifest.description:
        lines.append(f"description = {_toml_str(manifest.description)}")
    lines.append(f"active = {str(bool(manifest.state.active)).lower()}")
    lines.append(f"backend = {_toml_str(manifest.state.backend.value)}")
    if manifest.created_at:
        lines.append(f"created_at = {_toml_str(manifest.created_at)}")
    if manifest.updated_at:
        lines.append(f"updated_at = {_toml_str(manifest.updated_at)}")
    decl = manifest.declarative
    if decl and (decl.module or decl.configuration_root or decl.last_successful_candidate):
        lines.append("")
        lines.append("[group.declarative]")
        if decl.configuration_root:
            lines.append(f"configuration_root = {_toml_str(decl.configuration_root)}")
        if decl.module:
            lines.append(f"module = {_toml_str(decl.module)}")
        if decl.declaration_path:
            lines.append(f"declaration_path = {_toml_str(decl.declaration_path)}")
        if decl.declaration:
            lines.append(f"declaration = {_toml_str(decl.declaration)}")
        if decl.last_successful_candidate:
            lines.append(f"last_successful_candidate = {_toml_str(decl.last_successful_candidate)}")
        if decl.last_promotion:
            lines.append(f"last_promotion = {_toml_str(decl.last_promotion)}")
    if manifest.packages:
        lines.append("")
        for pkg in manifest.packages:
            lines.append("[[group.packages]]")
            lines.append(f"name = {_toml_str(pkg.requested)}")
            lines.append(f"attribute = {_toml_str(pkg.attribute)}")
            lines.append(f"source = {_toml_str(pkg.source)}")
            if pkg.revision:
                lines.append(f"nixpkgs_revision = {_toml_str(pkg.revision)}")
            if pkg.resolution_timestamp:
                lines.append(f"resolution_timestamp = {_toml_str(pkg.resolution_timestamp)}")
            if pkg.install_method and pkg.install_method.value != "imperative":
                lines.append(f"install_method = {_toml_str(pkg.install_method.value)}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _toml_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def parse(text: str, source: str = "<manifest>") -> GroupManifest:
    try:
        data = tomllib.loads(text)
    except Exception as exc:  # tomllib raises TOMLDecodeError
        raise ManifestError(
            Diagnostic(
                ErrorCode.GROUP_STORAGE,
                f"could not parse manifest {source}: {exc}",
            )
        )

    version = int(data.get("version", 0))
    if version > SCHEMA_VERSION:
        raise ManifestError(
            Diagnostic(
                ErrorCode.GROUP_STORAGE,
                f"manifest {source} uses schema version {version}, but this "
                f"nixorcist only supports up to {SCHEMA_VERSION}",
                suggestion="upgrade nixorcist, or export the groups and re-import them",
            )
        )
    if version < 1:
        raise ManifestError(
            Diagnostic(
                ErrorCode.GROUP_STORAGE,
                f"manifest {source} is missing a schema version",
            )
        )

    group = data.get("group", {})
    if not isinstance(group, dict) or not group.get("name"):
        raise ManifestError(
            Diagnostic(ErrorCode.GROUP_STORAGE, f"manifest {source} has no group name")
        )

    packages: list[ResolvedPackage] = []
    raw_packages = group.get("packages", [])
    if isinstance(raw_packages, list):
        for entry in raw_packages:
            if not isinstance(entry, dict):
                continue
            requested = str(entry.get("name", ""))
            if not requested:
                raise ManifestError(
                    Diagnostic(
                        ErrorCode.GROUP_STORAGE,
                        f"manifest {source} contains a package entry without a name",
                    )
                )
            packages.append(ResolvedPackage.from_dict(entry))

    active = bool(group.get("active", False))
    backend_raw = str(group.get("backend", "none")).lower()
    try:
        backend = Backend(backend_raw)
    except ValueError:
        raise ManifestError(
            Diagnostic(
                ErrorCode.GROUP_STORAGE,
                f"manifest {source} has an unknown backend {backend_raw!r}",
                suggestion="expected one of: none, imperative, declarative",
            )
        )
    state = GroupState(active=active, backend=backend)
    declarative = DeclarativeMetadata.from_dict(group.get("declarative"))

    return GroupManifest(
        schema_version=version,
        name=str(group["name"]),
        description=str(group.get("description", "")),
        packages=packages,
        state=state,
        declarative=declarative if any(v for v in declarative.to_dict().values()) else None,
        created_at=str(group.get("created_at", "")),
        updated_at=str(group.get("updated_at", "")),
    )


def parse_stream(stream: BinaryIO, source: str = "<manifest>") -> GroupManifest:
    return parse(stream.read().decode("utf-8"), source=source)