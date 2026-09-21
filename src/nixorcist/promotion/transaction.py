"""Promotion transaction: a two-phase apply/validate/commit loop that
guarantees the original configuration is untouched unless the validation
build succeeds.

Phase outline:

1. Copy the configuration root into a temporary tree.
2. ``apply_plan`` edits the *temporary* tree only.
3. ``nixos-rebuild build`` validates the temporary tree.
4. Only on success are the changed files copied back into the real root.

On any failure the temporary tree is discarded and the real configuration is
left exactly as it was.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..logging import Logger
from .discover import ConfigurationModel
from .planner import PromotionPlan
from .transformer import apply_plan
from .validator import ValidationResult, validate as validate_tree


class PromotionError(NixorcistError):
    pass


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        name
        for name in names
        if name in (".git", ".hg", ".svn", "result", "result-system", ".nixorcist")
    }
    # A configuration root can contain a checkout of Nixorcist itself. Its
    # resolver cache is neither part of the NixOS module graph nor guaranteed
    # to be readable by the user performing a promotion. Never copy it into a
    # candidate tree.
    if Path(directory).name == "nixorcist" and "cache" in names:
        ignored.add("cache")
    return ignored


def copy_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=_ignore, symlinks=True)


@dataclass
class TransactionResult:
    strategy: str
    changed_files: list[str]
    validated: bool
    validation: ValidationResult | None
    dry_run: bool

    @property
    def would_change(self) -> bool:
        return bool(self.changed_files)


class Transaction:
    def __init__(
        self,
        model: ConfigurationModel,
        plan: PromotionPlan,
        logger: Logger | None = None,
    ) -> None:
        self.model = model
        self.plan = plan
        self.logger = logger or Logger()

    def run(
        self,
        dry_run: bool = False,
        commit: bool = True,
    ) -> TransactionResult:
        root = self.model.root.directory
        plan = self.plan
        self.logger.info(
            f"promotion strategy: {plan.strategy} -> {plan.target_module}"
        )
        if dry_run:
            return TransactionResult(
                strategy=plan.strategy,
                changed_files=plan.changed_relative_files,
                validated=False,
                validation=None,
                dry_run=True,
            )

        with tempfile.TemporaryDirectory(prefix="nixorcist-promote-") as tmp:
            tree = Path(tmp) / "config"
            self.logger.info(f"staging copy: {tmp}")
            try:
                copy_tree(root, tree)
            except OSError as exc:
                raise PromotionError(
                    Diagnostic(
                        ErrorCode.PROMOTION,
                        f"could not stage configuration from {root}: {exc}",
                    )
                ) from exc

            changed = apply_plan(tree, root, plan)
            self.logger.debug(f"changed in staging tree: {changed}")

            validation = validate_tree(tree, self.model, logger=self.logger)

            if commit:
                for rel in changed:
                    src = tree / rel
                    dst = root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                    except OSError as exc:
                        raise PromotionError(
                            Diagnostic(
                                ErrorCode.PROMOTION,
                                f"could not copy {src} back to {dst}: {exc}",
                            )
                        ) from exc
                self.logger.debug("staged changes committed to configuration root")
            else:
                self.logger.info("check mode: staged changes were NOT committed")

            return TransactionResult(
                strategy=plan.strategy,
                changed_files=changed,
                validated=validation.success,
                validation=validation,
                dry_run=False,
            )
