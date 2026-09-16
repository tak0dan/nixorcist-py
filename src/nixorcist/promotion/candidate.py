"""Candidate configuration trees.

For every promotion operation Nixorcist stages a *candidate*: a copy of the
live configuration tree that the plan edits, before anything touches the real
configuration (§37).  The candidate is stored under
``cache/promotions/<operation-id>/candidate`` together with ``plan.toml``
(§29).

The live configuration is never modified before a candidate has passed
validation (§2.3, Rule 1, Rule 2, Rule 3).  A queued operation that is
canceled before execution must not leave a candidate on disk (§45).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger
from .discover import ConfigurationModel
from .planner import PromotionPlan
from .transaction import copy_tree
from .transformer import apply_plan


class CandidateError(NixorcistError):
    pass


def new_operation_id(tag: str = "promo") -> str:
    """Short, chronological, collision-resistant operation identifier."""
    stamp = f"{time.time_ns():.0f}-{tag}-{uuid.uuid4().hex[:8]}"
    return hashlib.sha1(stamp.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Candidate:
    operation_id: str
    root: Path
    entry_file: Path
    created_at: str
    strategy: str
    changed_files: tuple[str, ...] = field(default_factory=tuple)
    plan_toml: Path | None = None

    @property
    def exists(self) -> bool:
        return self.root.exists() and self.entry_file.exists()

    @property
    def candidate_dir(self) -> Path:
        return self.root


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _toml_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _plan_toml(operation_id: str, plan: PromotionPlan) -> str:
    groups = []
    for name, pkgs in plan.groups:
        groups.append(f"  {{ name = {_toml_str(name)}, packages = {_toml_str(','.join(p.attribute for p in pkgs))} }}")
    if groups:
        groups_txt = "[\n" + "\n".join(groups) + "\n]"
    else:
        groups_txt = "[]"
    imports = (
        ""
        if plan.strategy == "EXTEND"
        else f'new_module = {_toml_str(str(plan.new_module_path or ""))}'
    )
    return "\n".join(
        [
            "version = 1",
            f"operation_id = {_toml_str(operation_id)}",
            f"strategy = {_toml_str(plan.strategy)}",
            f"target_module = {_toml_str(str(plan.target_module))}",
            f"groups = {groups_txt}",
            imports,
        ]
    ).rstrip("\n") + "\n"


def create_candidate(
    model: ConfigurationModel,
    plan: PromotionPlan,
    promotions_dir: Path,
    operation_id: str | None = None,
    logger: Logger | None = None,
) -> Candidate:
    """Stage a candidate tree: copy the live configuration, then apply the
    plan inside the copy.  Nothing outside ``promotions_dir`` is modified."""
    logger = logger or Logger()
    op_id = operation_id or new_operation_id()
    candidate_root = promotions_dir / op_id / "candidate"
    try:
        candidate_root.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CandidateError(
            Diagnostic(
                ErrorCode.PROMOTION,
                f"could not create candidate directory {candidate_root}: {exc}",
            )
        ) from exc

    try:
        copy_tree(model.root.directory, candidate_root)
    except OSError as exc:
        raise CandidateError(
            Diagnostic(
                ErrorCode.PROMOTION,
                f"could not stage configuration into {candidate_root}: {exc}",
            )
        ) from exc

    try:
        changed = apply_plan(candidate_root, model.root.directory, plan)
    except Exception as exc:
        raise CandidateError(
            Diagnostic(
                ErrorCode.PROMOTION,
                f"could not apply promotion plan to candidate: {exc}",
            )
        ) from exc

    plan_toml = candidate_root.parent / "plan.toml"
    try:
        plan_toml.write_text(_plan_toml(op_id, plan), "utf-8")
    except OSError as exc:
        raise CandidateError(
            Diagnostic(
                ErrorCode.PROMOTION,
                f"could not write plan metadata {plan_toml}: {exc}",
            )
        ) from exc

    entry = candidate_root / model.root.entry_file.name
    logger.debug(
        f"candidate {op_id}: strategy={plan.strategy} changed={changed}"
    )
    return Candidate(
        operation_id=op_id,
        root=candidate_root,
        entry_file=entry,
        created_at=_now_iso(),
        strategy=plan.strategy,
        changed_files=tuple(changed),
        plan_toml=plan_toml,
    )


def discard_candidate(promotions_dir: Path, operation_id: str) -> bool:
    """Remove a staged candidate tree (and its operation directory)."""
    target = promotions_dir / operation_id
    if not target.exists():
        return False
    import shutil

    shutil.rmtree(target, ignore_errors=True)
    return True