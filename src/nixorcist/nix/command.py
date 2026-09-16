"""Thin wrapper around the ``nix`` executable.

Every external Nix invocation goes through here.  Commands are logged at
debug level and may be suppressed with ``--dry-run``.  Failures raise
``TN006`` diagnostics carrying the offending command's output.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def combined(self) -> str:
        parts = [self.stdout, self.stderr]
        return "\n".join(p for p in parts if p)


class NixError(NixorcistError):
    pass


class NixCommand:
    def __init__(self, logger: Logger | None = None, nix_bin: str = "nix"):
        self.logger = logger or Logger()
        self.nix_bin = nix_bin

    @property
    def available(self) -> bool:
        path = shutil.which(self.nix_bin)
        return path is not None

    def run(self, args: list[str], *, check: bool = True, timeout: int = 900) -> CommandResult:
        argv = [self.nix_bin, *args]
        self.logger.command(argv)
        if self.logger.dry_run:
            self.logger.dry(" ".join(argv))
            return CommandResult(tuple(argv), 0, "", "(dry run)")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise NixError(
                Diagnostic(
                    ErrorCode.NIX_COMMAND,
                    f"the '{self.nix_bin}' executable was not found on PATH",
                    suggestion="install Nix (https://nixos.org) or override the binary with $NIX_BIN",
                )
            )
        except subprocess.TimeoutExpired:
            raise NixError(
                Diagnostic(ErrorCode.NIX_COMMAND, f"'{self.nix_bin}' timed out after {timeout}s")
            )
        result = CommandResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
        if check and not result.ok:
            raise NixError(self._failure_diagnostic(result))
        return result

    def _failure_diagnostic(self, result: CommandResult) -> Diagnostic:
        detail = result.combined().strip()
        notes: tuple[str, ...] = ()
        if detail:
            lines = detail.splitlines()[:12]
            notes = ("command:"+ " " + " ".join(result.argv), *("  " + ln for ln in lines))
        else:
            notes = ("command:" + " " + " ".join(result.argv),)
        return Diagnostic(
            ErrorCode.NIX_COMMAND,
            f"nix command failed with exit code {result.returncode}",
            notes=notes,
        )