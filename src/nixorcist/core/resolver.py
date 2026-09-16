"""High-level package resolution with caching and identity fallback.

A :class:`PackageResolver` wraps the Nix-backed resolver, a per-revision
on-disk cache, and a deterministic fallback so the DSL remains usable without
Nix (and is fully testable).
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger
from ..nix.evaluator import NixEvaluator
from ..nix.resolver import NixResolver
from .models import ResolvedPackage


class ResolutionMode(str, Enum):
    AUTO = "auto"
    NIX = "nix"
    IDENTITY = "identity"

    @classmethod
    def from_env(cls) -> "ResolutionMode":
        value = os.environ.get("NIXORCIST_RESOLVE", "").strip().lower()
        if value in ("nix", "search"):
            return cls.NIX
        if value in ("identity", "off", "none", "no"):
            return cls.IDENTITY
        return cls.AUTO


class ResolutionError(NixorcistError):
    pass


class PackageResolver:
    def __init__(
        self,
        nix_resolver: NixResolver | None = None,
        evaluator: NixEvaluator | None = None,
        mode: ResolutionMode | str | None = None,
        cache_dir: Path | None = None,
        logger: Logger | None = None,
        strict: bool = False,
    ):
        self.logger = logger or Logger()
        self.mode = ResolutionMode(mode) if mode else ResolutionMode.from_env()
        self.nix_resolver = nix_resolver or NixResolver(logger=self.logger)
        self.evaluator = evaluator or NixEvaluator(self.nix_resolver.command)
        self._cache_dir = cache_dir
        self._cache: dict[str, dict[str, str]] | None = None
        self._revision: str | None = None
        self.strict = strict

    # -- cache ----------------------------------------------------------
    @property
    def cache_dir(self) -> Path | None:
        return self._cache_dir

    def _load_cache(self) -> None:
        if self._cache is not None:
            return
        self._cache = {}
        if self._cache_dir is None:
            return
        cache_file = self._cache_dir / "resolution.json"
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text())
                self._cache = data.get("entries", {})
                if "revision" in data:
                    self._revision = data["revision"]
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def _save_cache(self) -> None:
        if self._cache is None or self._cache_dir is None:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {"revision": self._revision or "", "entries": self._cache}
            (self._cache_dir / "resolution.json").write_text(
                json.dumps(payload, indent=2) + "\n"
            )
        except OSError:
            pass

    def nixpkgs_revision(self) -> str:
        if self._revision is None:
            self._revision = self.evaluator.nixpkgs_revision()
        return self._revision

    # -- resolution -----------------------------------------------------
    def resolve(self, requested: str, source: str = "nixpkgs") -> ResolvedPackage:
        if self.mode is ResolutionMode.IDENTITY:
            return ResolvedPackage(requested=requested, attribute=requested, source=source)

        if self.mode is ResolutionMode.NIX and not self.nix_resolver.command.available:
            if self.strict:
                raise ResolutionError(
                    Diagnostic(
                        ErrorCode.RESOLUTION,
                        f"cannot resolve '{requested}': Nix is not available",
                    )
                )
            return ResolvedPackage(requested=requested, attribute=requested, source=source)

        revision = self.nixpkgs_revision()
        self._load_cache()
        key = f"{source}|{requested}"
        if revision:
            cached = self._cache.get(key) if self._cache else None
            if cached:
                self.logger.debug(f"resolution cache hit: {requested} -> {cached}")
                return ResolvedPackage(
                    requested=requested,
                    attribute=cached,
                    source=source,
                    revision=revision,
                )

        attribute = self.nix_resolver.resolve(requested)
        if attribute:
            if revision and self._cache is not None:
                self._cache[key] = attribute
                self._save_cache()
            self.logger.debug(f"resolved {requested} -> {attribute}")
            return ResolvedPackage(
                requested=requested,
                attribute=attribute,
                source=source,
                revision=revision,
            )

        self.logger.debug(f"failed to resolve '{requested}'; falling back to identity attribute")
        if self.strict:
            raise ResolutionError(
                Diagnostic(
                    ErrorCode.RESOLUTION,
                    f"could not resolve package name '{requested}' against nixpkgs",
                    suggestion="check the spelling, or install with NIXORCIST_RESOLVE=identity",
                )
            )
        return ResolvedPackage(requested=requested, attribute=requested, source=source)

    def resolve_many(self, requested: list[str], source: str = "nixpkgs") -> list[ResolvedPackage]:
        resolved: dict[str, ResolvedPackage] = {}
        for name in requested:
            if name not in resolved:
                resolved[name] = self.resolve(name, source=source)
        return list(resolved.values())