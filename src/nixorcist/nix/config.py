"""NixOS configuration root discovery.

The root is discovered from (in order): an explicit ``--root`` argument, the
``$NIXORCIST_CONFIG_ROOT`` environment variable, or ``/etc/nixos``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError


class ConfigDiscoveryError(NixorcistError):
    pass


@dataclass(frozen=True)
class NixOSRoot:
    directory: Path
    entry_file: Path
    is_flake: bool

    @property
    def configuration_file(self) -> Path:
        return self.directory / "configuration.nix"


def _entry_for_directory(directory: Path) -> Path | None:
    for candidate in ("configuration.nix", "flake.nix"):
        path = directory / candidate
        if path.is_file():
            return path
    return None


def discover_root(explicit: str | Path | None = None) -> NixOSRoot:
    candidate: Path | None = None

    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        env = os.environ.get("NIXORCIST_CONFIG_ROOT")
        if env:
            candidate = Path(env).expanduser()

    if candidate is not None:
        if candidate.is_dir():
            entry = _entry_for_directory(candidate)
            if entry is None:
                raise ConfigDiscoveryError(
                    Diagnostic(
                        ErrorCode.DISCOVERY,
                        f"{candidate} has no configuration.nix or flake.nix",
                    )
                )
            directory, entry_file = candidate, entry
        elif candidate.is_file():
            entry_file = candidate.resolve()
            directory = entry_file.parent
        else:
            raise ConfigDiscoveryError(
                Diagnostic(ErrorCode.DISCOVERY, f"configuration path {candidate} does not exist")
            )
    else:
        etc = Path("/etc/nixos")
        if not etc.is_dir():
            raise ConfigDiscoveryError(
                Diagnostic(
                    ErrorCode.DISCOVERY,
                    "no NixOS configuration root found",
                    suggestion="pass --root /path/to/config, set $NIXORCIST_CONFIG_ROOT, "
                    "or use a standard /etc/nixos configuration",
                )
            )
        entry = _entry_for_directory(etc)
        if entry is None:
            raise ConfigDiscoveryError(
                Diagnostic(ErrorCode.DISCOVERY, "/etc/nixos has no configuration.nix or flake.nix")
            )
        directory, entry_file = etc, entry

    return NixOSRoot(
        directory=directory.resolve(),
        entry_file=entry_file.resolve(),
        is_flake=entry_file.name == "flake.nix",
    )