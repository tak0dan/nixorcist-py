"""Promotion queue.

Without ``-S``, if a promotion against the same configuration target is
already active, another promotion is queued (§44).  Queued operations are
associated with their originating session/process.  If that session
disappears before the queued operation begins, the operation is cancelled
(§45).  A cancelled queued intent must not produce a candidate configuration.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

QUEUE_STATES = ("QUEUED", "CANCELLED")


class QueueError(NixorcistError):
    pass


@dataclass(frozen=True)
class QueuedOperation:
    operation_id: str
    target: str
    command: str
    session: str
    created_at: str
    state: str = "QUEUED"

    def cancel(self) -> "QueuedOperation":
        return _replace_state(self, "CANCELLED")


def _replace_state(op: QueuedOperation, state: str) -> QueuedOperation:
    return QueuedOperation(
        operation_id=op.operation_id,
        target=op.target,
        command=op.command,
        session=op.session,
        created_at=op.created_at,
        state=state,
    )


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _toml_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _serialize(op: QueuedOperation) -> str:
    return "\n".join(
        [
            "version = 1",
            "",
            "[queued]",
            f"operation_id = {_toml_str(op.operation_id)}",
            f"target = {_toml_str(op.target)}",
            f"command = {_toml_str(op.command)}",
            f"session = {_toml_str(op.session)}",
            f"created_at = {_toml_str(op.created_at)}",
            f"state = {_toml_str(op.state)}",
        ]
    ).rstrip("\n") + "\n"


class PromotionQueue:
    """Persistent queue of pending promotions for a configuration target."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, operation_id: str) -> Path:
        return self.directory / f"{operation_id}.toml"

    def enqueue(
        self,
        *,
        target: str,
        command: str,
        session: str,
        operation_id: str = "",
    ) -> QueuedOperation:
        if not session:
            raise QueueError(
                Diagnostic(ErrorCode.PROMOTION, "cannot queue without an owning session")
            )
        op = QueuedOperation(
            operation_id=operation_id or _new_operation_id(),
            target=target,
            command=command,
            session=session,
            created_at=_now_iso(),
            state="QUEUED",
        )
        self._write(op)
        return op

    def _write(self, op: QueuedOperation) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self._path(op.operation_id).write_text(_serialize(op), "utf-8")
        except OSError as exc:
            raise QueueError(
                Diagnostic(
                    ErrorCode.PROMOTION,
                    f"could not write queued operation {op.operation_id}: {exc}",
                )
            ) from exc

    def list(self) -> list[QueuedOperation]:
        if not self.directory.exists():
            return []
        ops: list[QueuedOperation] = []
        for f in sorted(self.directory.glob("*.toml")):
            try:
                mtime = f.stat().st_mtime_ns
            except OSError:
                mtime = 0
            try:
                text = f.read_text("utf-8")
            except OSError:
                continue
            data = tomllib.loads(text)
            row = data.get("queued", {})
            ops.append(
                (
                    mtime,
                    QueuedOperation(
                        operation_id=str(row.get("operation_id", f.stem)),
                        target=str(row.get("target", "")),
                        command=str(row.get("command", "")),
                        session=str(row.get("session", "")),
                        created_at=str(row.get("created_at", "")),
                        state=str(row.get("state", "QUEUED")),
                    ),
                )
            )
        ops.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].operation_id))
        return [o for _, o in ops]

    def pending(self) -> list[QueuedOperation]:
        return [o for o in self.list() if o.state == "QUEUED"]

    def get(self, operation_id: str) -> QueuedOperation | None:
        for o in self.list():
            if o.operation_id == operation_id:
                return o
        return None

    def cancel(self, operation_id: str) -> QueuedOperation | None:
        op = self.get(operation_id)
        if op is None:
            return None
        if op.state != "QUEUED":
            return op
        updated = op.cancel()
        self._write(updated)
        return updated

    def cancel_abandoned(self, live_sessions: set[str]) -> list[QueuedOperation]:
        """Cancel queued operations whose originating session is gone (§45)."""
        cancelled: list[QueuedOperation] = []
        for op in self.pending():
            if op.session not in live_sessions:
                updated = self.cancel(op.operation_id)
                if updated is not None:
                    cancelled.append(updated)
        return cancelled

    def pop_next(self) -> QueuedOperation | None:
        """Dequeue the oldest pending operation and delete its record."""
        pending = self.pending()
        if not pending:
            return None
        next_op = pending[0]
        try:
            self._path(next_op.operation_id).unlink()
        except OSError:
            pass
        return next_op


def _new_operation_id() -> str:
    import hashlib
    import time
    import uuid

    stamp = f"{time.time_ns():.0f}-queue-{uuid.uuid4().hex[:8]}"
    return hashlib.sha1(stamp.encode()).hexdigest()[:12]