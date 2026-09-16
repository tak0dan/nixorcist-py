"""Simple advisory file lock protecting manifest mutations.

Batch operations must not interleave with other Nixorcist processes.  This
uses ``fcntl.flock`` on POSIX and degrades to a no-op elsewhere.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError


class FileLock:
    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd = None
        self._blocking = True
        try:
            import fcntl  # noqa: F401

            self._fcntl = __import__("fcntl")
        except ImportError:  # pragma: no cover - non-POSIX
            self._fcntl = None

    def acquire(self) -> None:
        if self._fcntl is None:
            self._blocking = False
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = self.path.open("w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                self._fd.write(str(time.time_ns()))
                self._fd.flush()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._fd.close()
                    self._fd = None
                    raise NixorcistError(
                        Diagnostic(
                            ErrorCode.GROUP_STORAGE,
                            f"could not acquire lock {self.path}",
                            suggestion="another nixorcist process is running; retry shortly",
                        )
                    )
                time.sleep(self.poll)

    def release(self) -> None:
        if self._fd is not None:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()