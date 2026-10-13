"""Detect the running distribution and the package managers it has."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SystemInfo:
    id: str  # arch, debian, ubuntu, fedora, opensuse-tumbleweed, nixos ...
    id_like: tuple[str, ...] = ()
    version_id: str = ""
    pretty_name: str = ""
    managers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def family(self) -> str:
        """arch | debian | fedora | suse | nixos | unknown"""
        ids = (self.id, *self.id_like)
        for fam, keys in (
            ("arch", ("arch", "manjaro", "endeavouros", "cachyos")),
            ("debian", ("debian", "ubuntu", "linuxmint", "pop")),
            ("fedora", ("fedora", "rhel", "centos", "rocky", "almalinux")),
            ("suse", ("opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap", "sles")),
            ("nixos", ("nixos",)),
        ):
            if any(i in keys or i.startswith(keys) for i in ids):
                return fam
        return "unknown"

    @property
    def repology_repo(self) -> str | None:
        """Repository slug used by pkgmap/Repology for this system."""
        major = self.version_id.split(".")[0]
        if self.id == "ubuntu" and self.version_id:
            return "ubuntu_" + self.version_id.replace(".", "_")
        if self.id == "debian" and major:
            return f"debian_{major}"
        if self.id == "fedora" and major:
            return f"fedora_{major}"
        if self.family == "suse":
            return "opensuse_tumbleweed" if "tumbleweed" in self.id else (
                f"opensuse_leap_{self.version_id.replace('.', '_')}" if self.version_id else None)
        return {"arch": "arch", "nixos": "nix_unstable"}.get(self.family)


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


_MANAGERS = ("pacman", "paru", "yay", "apt", "dnf", "zypper", "flatpak", "snap", "nix")


def detect(os_release: Path = Path("/etc/os-release"), which=shutil.which) -> SystemInfo:
    data: dict[str, str] = {}
    for candidate in (os_release, Path("/usr/lib/os-release")):
        try:
            data = parse_os_release(candidate.read_text("utf-8"))
            break
        except OSError:
            continue
    return SystemInfo(
        id=data.get("ID", "unknown").lower(),
        id_like=tuple(data.get("ID_LIKE", "").lower().split()),
        version_id=data.get("VERSION_ID", ""),
        pretty_name=data.get("PRETTY_NAME", ""),
        managers=tuple(m for m in _MANAGERS if which(m)),
    )
