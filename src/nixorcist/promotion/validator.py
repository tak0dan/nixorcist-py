"""Promotion validation: run ``nixos-rebuild build`` against the temporary
configuration tree to verify evaluation/build correctness without switching
the running system.

If this fails the temporary tree is discarded and the original configuration
is left untouched.  The rebuild command can be overridden with
``$NIXORCIST_REBUILD_CMD`` (``{root}`` is substituted with the tree path).
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger
from .discover import ConfigurationModel


class ValidationError(NixorcistError):
    pass


@dataclass(frozen=True)
class ValidationResult:
    command: tuple[str, ...]
    success: bool
    message: str
    duration_s: float


def _default_command(tree: str, model: ConfigurationModel) -> list[str]:
    if model.root.is_flake:
        hostname = socket.gethostname() or "localhost"
        return ["nixos-rebuild", "build", "--flake", f"{tree}#{hostname}"]
    # A caller may deliberately promote a non-standard entry file such as
    # ``plain_configuration.nix``. Preserve its path relative to the selected
    # configuration root in the candidate tree rather than assuming the
    # conventional filename.
    try:
        entry = model.root.entry_file.relative_to(model.root.directory)
    except ValueError:
        entry = Path(model.root.entry_file.name)
    config = str(Path(tree) / entry)
    return ["nixos-rebuild", "build", "-I", f"nixos-config={config}"]


def build_validation_command(tree: str | os.PathLike, model: ConfigurationModel) -> list[str]:
    raw = os.environ.get("NIXORCIST_REBUILD_CMD")
    if raw:
        parts = [p.replace("{root}", str(tree)) for p in shlex.split(raw)]
        return parts
    return _default_command(str(tree), model)


def validate(
    tree: str | os.PathLike,
    model: ConfigurationModel,
    logger: Logger | None = None,
    dry_run: bool = False,
    check_rebuild_command_only: bool = False,
) -> ValidationResult:
    logger = logger or Logger()
    path = str(tree)
    command = build_validation_command(path, model)
    command_text = " ".join(command)
    logger.command(list(command))
    if dry_run or check_rebuild_command_only:
        logger.info(f"would run: {command_text}")
        return ValidationResult(tuple(command), True, "(dry run; build skipped)", 0.0)

    if not shutil.which("nixos-rebuild"):
        raise ValidationError(
            Diagnostic(
                ErrorCode.VALIDATION,
                "nixos-rebuild was not found on PATH",
                suggestion="run promotion on a NixOS system, or set "
                "$NIXORCIST_REBUILD_CMD to a suitable build command",
            )
        )

    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3600,
            cwd=path,
        )
    except subprocess.TimeoutExpired:
        raise ValidationError(
            Diagnostic(ErrorCode.VALIDATION, f"validation build timed out after 60 minutes")
        )
    duration = time.monotonic() - started
    combined = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
    )
    tail = "\n".join(combined.splitlines()[-40:])
    ok = proc.returncode == 0
    if not ok:
        raise ValidationError(
            Diagnostic(
                ErrorCode.VALIDATION,
                f"nixos-rebuild build failed (exit {proc.returncode}); the temporary "
                "configuration tree was discarded and the original configuration is unchanged",
                notes=("build output:" + ("\n" + indent(tail) if tail else " (no output)")),
            )
        )
    logger.debug(f"validation build OK in {duration:.1f}s")
    return ValidationResult(tuple(command), True, tail, duration)


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
