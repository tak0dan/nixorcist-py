"""Filesystem locations for Nixorcist state.

Default root: ``$NIXORCIST_HOME`` or ``~/.config/nixorcist``.
The exact layout may change; treat these locations as private to the tool.

    <root>/
    ├── config.toml
    ├── groups/
    │   ├── Programming.toml
    │   └── ...
    ├── cache/
    │   └── resolution/
    └── state/
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    base: Path

    @property
    def groups_dir(self) -> Path:
        return self.base / "groups"

    @property
    def cache_dir(self) -> Path:
        return self.base / "cache" / "resolution"

    @property
    def promotions_dir(self) -> Path:
        """Candidate trees and operation logs (spec §29)."""
        return self.base / "cache" / "promotions"

    @property
    def history_dir(self) -> Path:
        """Persisted operation history (spec §48)."""
        return self.base / "history"

    @property
    def queue_dir(self) -> Path:
        """Queued promotion operations (spec §44-45)."""
        return self.state_dir / "promotion" / "queue"

    @property
    def state_dir(self) -> Path:
        return self.base / "state"

    @property
    def config_file(self) -> Path:
        return self.base / "config.toml"

    @property
    def lock_file(self) -> Path:
        return self.base / "state" / "lock"

    @property
    def profile_file(self) -> Path:
        return self.base / "state" / "profile"

    @property
    def promotion_lock_file(self) -> Path:
        return self.base / "state" / "promotion.lock"

    def ensure(self) -> "Paths":
        for d in (self.groups_dir, self.cache_dir, self.state_dir, self.history_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def from_env(cls, override: str | Path | None = None) -> "Paths":
        if override:
            return cls(Path(override).expanduser())
        env = os.environ.get("NIXORCIST_HOME")
        if env:
            return cls(Path(env).expanduser())
        xdg = os.environ.get("XDG_CONFIG_HOME")
        home = Path(os.environ.get("HOME", str(Path.home())))
        base = Path(xdg).expanduser() if xdg else home / ".config"
        return cls(base / "nixorcist")