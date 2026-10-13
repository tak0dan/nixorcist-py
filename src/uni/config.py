"""User configuration: which sources are used, in which order.

``~/.config/uni/config.toml``::

    # lower number = tried first
    [priority]
    native  = 10
    aur     = 20
    flatpak = 30
    nix     = 40
    snap    = 50

    [disabled]
    sources = ["snap"]

``native`` stands for the distribution's own manager (pacman/apt/dnf/zypper).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRIORITY = {"native": 10, "aur": 20, "flatpak": 30, "nix": 40, "snap": 50}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(os.environ.get("UNI_CONFIG", Path(base) / "uni" / "config.toml"))


@dataclass
class UniConfig:
    priority: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PRIORITY))
    disabled: set[str] = field(default_factory=set)

    def rank_of(self, source_name: str, source_kind: str) -> int:
        for key in (source_name, source_kind):
            if key in self.priority:
                return self.priority[key]
        return 100

    @classmethod
    def load(cls, path: Path | None = None) -> "UniConfig":
        path = path or config_path()
        cfg = cls()
        try:
            text = path.read_text("utf-8")
        except OSError:
            return cfg
        try:
            import tomllib  # py3.11+
        except ImportError:  # pragma: no cover
            return cfg
        data = tomllib.loads(text)
        cfg.priority.update({k: int(v) for k, v in data.get("priority", {}).items()})
        cfg.disabled = set(data.get("disabled", {}).get("sources", []))
        return cfg
