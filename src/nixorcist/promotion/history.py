"""Operation history.

Nixorcist records every operation — successful and failed — so the user can
answer *what happened / when / why / which configuration was tested / which
candidate failed* (§48).  History is append-only TOML under
``<root>/history/promotions.toml``.
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

PROMOTION_STATES = (
    "QUEUED",
    "PREPARING",
    "TESTING",
    "FAILED",
    "VALIDATED",
    "COMMITTING",
    "COMMITTED",
    "CANCELLED",
)


class HistoryError(NixorcistError):
    pass


@dataclass(frozen=True)
class HistoryEntry:
    operation_id: str
    timestamp: str
    command: str
    action: str
    previous_state: str
    result_state: str
    status: str
    candidate: str = ""
    target: str = ""

    def validate_status(self) -> None:
        if self.status not in PROMOTION_STATES:
            raise HistoryError(
                Diagnostic(
                    ErrorCode.PROMOTION,
                    f"unknown promotion status {self.status!r}",
                    suggestion="expected one of: " + ", ".join(PROMOTION_STATES),
                )
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


def _serialize(entries: list[HistoryEntry]) -> str:
    lines = ["version = 1", ""]
    for e in entries:
        lines.append("[[entry]]")
        lines.append(f"operation_id = {_toml_str(e.operation_id)}")
        lines.append(f"timestamp = {_toml_str(e.timestamp)}")
        lines.append(f"command = {_toml_str(e.command)}")
        lines.append(f"action = {_toml_str(e.action)}")
        lines.append(f"previous_state = {_toml_str(e.previous_state)}")
        lines.append(f"result_state = {_toml_str(e.result_state)}")
        lines.append(f"status = {_toml_str(e.status)}")
        if e.candidate:
            lines.append(f"candidate = {_toml_str(e.candidate)}")
        if e.target:
            lines.append(f"target = {_toml_str(e.target)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _parse(text: str) -> list[HistoryEntry]:
    data = tomllib.loads(text)
    raw = data.get("entry", [])
    entries: list[HistoryEntry] = []
    for row in raw if isinstance(raw, list) else []:
        entries.append(
            HistoryEntry(
                operation_id=str(row.get("operation_id", "")),
                timestamp=str(row.get("timestamp", "")),
                command=str(row.get("command", "")),
                action=str(row.get("action", "")),
                previous_state=str(row.get("previous_state", "")),
                result_state=str(row.get("result_state", "")),
                status=str(row.get("status", "")),
                candidate=str(row.get("candidate", "")),
                target=str(row.get("target", "")),
            )
        )
    return entries


class PromotionHistory:
    """Append-only log of promotion operations."""

    def __init__(self, file_path: Path):
        self.path = file_path

    def _read(self) -> list[HistoryEntry]:
        if not self.path.exists():
            return []
        try:
            return _parse(self.path.read_text("utf-8"))
        except Exception as exc:
            raise HistoryError(
                Diagnostic(
                    ErrorCode.PROMOTION,
                    f"could not read history {self.path}: {exc}",
                )
            ) from exc

    def record(self, entry: HistoryEntry) -> None:
        entry.validate_status()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._read()
        entries.append(entry)
        try:
            self.path.write_text(_serialize(entries), "utf-8")
        except OSError as exc:
            raise HistoryError(
                Diagnostic(
                    ErrorCode.PROMOTION,
                    f"could not write history {self.path}: {exc}",
                )
            ) from exc

    def all(self) -> list[HistoryEntry]:
        return sorted(self._read(), key=lambda e: e.timestamp)

    def recent(self, limit: int = 10) -> list[HistoryEntry]:
        return self.all()[-limit:]

    def for_group(self, target: str) -> list[HistoryEntry]:
        return [e for e in self.all() if e.target == target]

    def latest_for(self, target: str) -> HistoryEntry | None:
        matches = self.for_group(target)
        return matches[-1] if matches else None

    def operation(self, operation_id: str) -> HistoryEntry | None:
        for e in self.all():
            if e.operation_id == operation_id:
                return e
        return None


def log_promotion(
    history: PromotionHistory,
    *,
    operation_id: str,
    command: str,
    target: str,
    previous_state: str,
    result_state: str,
    status: str,
    timestamp: str = "",
    candidate: str = "",
) -> HistoryEntry:
    entry = HistoryEntry(
        operation_id=operation_id,
        timestamp=timestamp or _now_iso(),
        command=command,
        action="promote",
        previous_state=previous_state,
        result_state=result_state,
        status=status,
        candidate=candidate,
        target=target,
    )
    history.record(entry)
    return entry