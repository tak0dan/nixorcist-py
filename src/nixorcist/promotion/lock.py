"""Configuration target lock.

A configuration target requires a lock before any candidate is staged (§46).
The lock carries ownership metadata — operation id, PID, originating session,
creation time, current state and the target configuration — and is held for
the duration of one promotion so concurrent operations on the same tree are
prevented.  A second promotion against the same target while a lock is held
is queued (§44), never started concurrently.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class LockError(NixorcistError):
    pass


@dataclass(frozen=True)
class LockOwner:
    operation_id: str
    pid: int
    session: str
    created_at: str
    state: str
    target: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "operation_id": self.operation_id,
            "pid": self.pid,
            "session": self.session,
            "created_at": self.created_at,
            "state": self.state,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LockOwner":
        return cls(
            operation_id=str(data.get("operation_id", "")),
            pid=int(data.get("pid", 0)),
            session=str(data.get("session", "")),
            created_at=str(data.get("created_at", "")),
            state=str(data.get("state", "")),
            target=str(data.get("target", "")),
        )


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _session_default() -> str:
    node = os.uname().nodename if hasattr(os, "uname") else "host"
    return f"{os.getpid()}@{node}"


def _toml_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _serialize_owner(owner: LockOwner) -> str:
    d = owner.to_dict()
    return "\n".join(
        [
            "version = 1",
            "",
            "[lock]",
            f"operation_id = {_toml_str(d['operation_id'])}",
            f"pid = {d['pid']}",
            f"session = {_toml_str(d['session'])}",
            f"created_at = {_toml_str(d['created_at'])}",
            f"state = {_toml_str(d['state'])}",
            f"target = {_toml_str(d['target'])}",
        ]
    ).rstrip("\n") + "\n"


def _parse_owner(text: str) -> LockOwner | None:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib  # type: ignore[import-not-found]
    try:
        data = tomllib.loads(text)
    except Exception:
        return None
    lock = data.get("lock", {})
    if not lock:
        return None
    return LockOwner.from_dict(lock)


class PromotionLock:
    """Advisory flock-based configuration lock with ownership metadata.

    The lock file holds the TOML metadata; an ``fcntl.flock(LOCK_EX)`` on the
    open file description provides mutual exclusion.  A busy lock fails fast
    so the caller can route the operation to the promotion queue (§44).
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._owner: LockOwner | None = None
        self._fd = None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a+")

    def acquire(
        self,
        *,
        target: str,
        operation_id: str,
        session: str = "",
        timeout: float | None = None,
    ) -> LockOwner:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            try:
                fd = self._open()
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise LockError(
                        Diagnostic(
                            ErrorCode.PROMOTION,
                            f"promotion lock on {self.path} is already held",
                            suggestion="queue this promotion and retry once the "
                            "active promotion finishes",
                        )
                    )
                time.sleep(self.poll)

        # We hold the flock; the previous holder is gone (or never existed).
        # Truncate in place and write our ownership metadata.
        fd.seek(0)
        fd.truncate(0)
        owner = LockOwner(
            operation_id=operation_id,
            pid=os.getpid(),
            session=session or _session_default(),
            created_at=_now_iso(),
            state="PREPARING",
            target=target,
        )
        fd.write(_serialize_owner(owner))
        fd.flush()
        try:
            os.fsync(fd.fileno())
        except OSError:
            pass
        self._fd = fd
        self._owner = owner
        return owner

    def current_owner(self) -> LockOwner | None:
        """Read the lock metadata (without acquiring the mutex)."""
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text("utf-8")
        except OSError:
            return None
        return _parse_owner(text)

    def update_state(self, state: str) -> LockOwner:
        if self._owner is None:
            raise LockError(
                Diagnostic(ErrorCode.PROMOTION, "lock is not held; cannot update state")
            )
        owner = LockOwner(
            operation_id=self._owner.operation_id,
            pid=self._owner.pid,
            session=self._owner.session,
            created_at=self._owner.created_at,
            state=state,
            target=self._owner.target,
        )
        try:
            self.path.write_text(_serialize_owner(owner), "utf-8")
        except OSError as exc:
            raise LockError(
                Diagnostic(ErrorCode.PROMOTION, f"could not update lock: {exc}")
            ) from exc
        self._owner = owner
        return owner

    def release(self) -> None:
        if self._fd is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._owner = None

    def __enter__(self) -> "PromotionLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()