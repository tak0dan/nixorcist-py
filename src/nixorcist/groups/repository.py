"""Filesystem-backed group manifest repository.

One TOML file per group under ``<root>/groups/``.  Group lookup is
case-insensitive in line with the spec, while the canonical/display name
recorded inside each manifest is preserved.
"""

from __future__ import annotations

from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger
from ..storage.paths import Paths
from .manifest import GroupManifest, ManifestError, parse


class RepositoryError(NixorcistError):
    pass


class GroupRepository:
    def __init__(self, paths: Paths, logger: Logger | None = None):
        self.paths = paths
        self.logger = logger or Logger()

    def _ensure(self) -> None:
        self.paths.groups_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, canonical_name: str) -> Path:
        safe = "".join(ch for ch in canonical_name if ch.isalnum() or ch in "._-")
        safe = safe or "unnamed"
        return self.paths.groups_dir / f"{safe}.toml"

    def _candidates(self) -> list[Path]:
        self._ensure()
        return sorted(self.paths.groups_dir.glob("*.toml"))

    def exists(self, name: str) -> bool:
        return self.load(name) is not None

    def load(self, name: str) -> GroupManifest | None:
        lower = name.lower()
        for candidate in self._candidates():
            try:
                manifest = parse(candidate.read_text("utf-8"), source=str(candidate))
            except ManifestError as exc:
                raise RepositoryError(exc.diagnostic) from exc
            except OSError as exc:
                raise RepositoryError(
                    Diagnostic(ErrorCode.GROUP_STORAGE, f"could not read {candidate}: {exc}")
                ) from exc
            if manifest.name.lower() == lower:
                return manifest
        return None

    def load_all(self) -> list[GroupManifest]:
        manifests: list[GroupManifest] = []
        for candidate in self._candidates():
            try:
                manifests.append(parse(candidate.read_text("utf-8"), source=str(candidate)))
            except ManifestError as exc:
                raise RepositoryError(exc.diagnostic) from exc
            except OSError as exc:
                raise RepositoryError(
                    Diagnostic(ErrorCode.GROUP_STORAGE, f"could not read {candidate}: {exc}")
                ) from exc
        manifests.sort(key=lambda m: m.name.lower())
        return manifests

    def save(self, manifest: GroupManifest) -> None:
        self._ensure()
        path = self._path_for(manifest.name)
        try:
            path.write_text(serialize_safe(manifest), "utf-8")
        except OSError as exc:
            raise RepositoryError(
                Diagnostic(ErrorCode.GROUP_STORAGE, f"could not write {path}: {exc}")
            ) from exc
        self.logger.debug(f"persisted manifest {path}")
        # Remove a stale differently-cased file if it exists.
        for candidate in self._candidates():
            if candidate == path:
                continue
            try:
                existing = parse(candidate.read_text("utf-8"), source=str(candidate))
            except Exception:
                continue
            if existing.name.lower() == manifest.name.lower() and existing.name != manifest.name:
                try:
                    candidate.unlink()
                    self.logger.debug(f"removed stale manifest {candidate}")
                except OSError:
                    pass

    def delete(self, name: str) -> bool:
        manifest = self.load(name)
        if manifest is None:
            return False
        path = self._path_for(manifest.name)
        try:
            path.unlink()
        except OSError as exc:
            raise RepositoryError(
                Diagnostic(ErrorCode.GROUP_STORAGE, f"could not delete {path}: {exc}")
            ) from exc
        return True


def serialize_safe(manifest: GroupManifest) -> str:
    from .manifest import serialize

    return serialize(manifest)