"""Sequential promotion chain (-S).

Sequential promotion builds a chronological chain of candidate
configurations: each step is validated against the *last successful* base,
never against a failed candidate (§42-43).  A failed candidate must not
become the base for subsequent sequential operations (Rule 4).

The tracker records each test against a base in TOML:
``<root>/history/sequences.toml``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


@dataclass(frozen=True)
class SequenceStep:
    candidate_id: str
    tested_against: str
    passed: bool
    timestamp: str


@dataclass
class SequenceTracker:
    path: Path
    steps: list[SequenceStep] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SequenceTracker":
        tracker = cls(path=path)
        if path.exists():
            try:
                data = tomllib.loads(path.read_text("utf-8"))
            except Exception:
                return tracker
            raw = data.get("step", [])
            steps: list[SequenceStep] = []
            for row in raw if isinstance(raw, list) else []:
                steps.append(
                    SequenceStep(
                        candidate_id=str(row.get("candidate_id", "")),
                        tested_against=str(row.get("tested_against", "")),
                        passed=bool(row.get("passed", False)),
                        timestamp=str(row.get("timestamp", "")),
                    )
                )
            tracker.steps = steps
        return tracker

    @property
    def last_successful_base(self) -> str | None:
        """The candidate id of the most recent PASS — the base for -S."""
        for step in reversed(self.steps):
            if step.passed:
                return step.candidate_id
        return None

    def record_test(self, candidate_id: str, tested_against: str, passed: bool) -> None:
        import datetime as _dt

        step = SequenceStep(
            candidate_id=candidate_id,
            tested_against=tested_against,
            passed=passed,
            timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        )
        self.steps.append(step)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["version = 1", ""]
        for s in self.steps:
            lines.append("[[step]]")
            lines.append(f"candidate_id = {_quote(s.candidate_id)}")
            lines.append(f"tested_against = {_quote(s.tested_against)}")
            lines.append(f"passed = {str(s.passed).lower()}")
            lines.append(f"timestamp = {_quote(s.timestamp)}")
            lines.append("")
        self.path.write_text("\n".join(lines).rstrip("\n") + "\n", "utf-8")


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'