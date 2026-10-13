"""Stage, validate and commit :class:`~.layout.FileEdit` lists.

Edits are never written straight into the user's configuration.  They are
applied to a temporary copy of the configuration root and checked there:

``parse``  (default) every edited file re-parses with Nixorcist's own parser
           and, when available, ``nix-instantiate --parse``.
``eval``   additionally evaluates ``config.<target>`` of the staged system and
           confirms the new packages show up in it.  Catches unknown attribute
           names (``pkgs.kate`` on a release where it moved to
           ``kdePackages.kate``) without building anything.
``build``  additionally runs ``nixos-rebuild build`` (or
           ``$NIXORCIST_REBUILD_CMD``) via the promotion validator.

Only if every requested check passes are the edited files copied back.
"""

from __future__ import annotations

import difflib
import json
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from .ast import NixParseError, parse_module
from .config import NixOSRoot
from .layout import DEFAULT_TARGET, FileEdit

CHECK_LEVELS = ("none", "parse", "eval", "build")


class LayoutApplyError(NixorcistError):
    def __init__(self, message: str, suggestion: str = ""):
        super().__init__(Diagnostic(ErrorCode.PROMOTION, message, suggestion=suggestion))


@dataclass
class ApplyResult:
    changed: list[str]
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    committed: bool = False
    evaluated_names: list[str] = field(default_factory=list)


def _rel(root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(root)
    except ValueError as exc:
        raise LayoutApplyError(f"{path} is outside the configuration root {root}") from exc


def render_diff(root: NixOSRoot, edits: list[FileEdit]) -> str:
    out: list[str] = []
    for edit in edits:
        rel = str(_rel(root.directory, edit.path))
        out.extend(
            difflib.unified_diff(
                edit.old_text.splitlines(keepends=True),
                edit.new_text.splitlines(keepends=True),
                fromfile="/dev/null" if edit.created else f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
    return "".join(out)


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_check(tree: Path, rels: list[Path], result: ApplyResult) -> None:
    for rel in rels:
        text = (tree / rel).read_text("utf-8")
        try:
            parse_module(text)
        except NixParseError as exc:
            raise LayoutApplyError(f"generated {rel} does not parse: {exc}") from exc
    result.checks.append("parse (nixorcist)")
    if shutil.which("nix-instantiate"):
        for rel in rels:
            proc = _run(["nix-instantiate", "--parse", str(tree / rel)], timeout=60)
            if proc.returncode != 0:
                raise LayoutApplyError(
                    f"nix-instantiate rejected {rel}: {proc.stderr.strip()}"
                )
        result.checks.append("parse (nix-instantiate)")
    else:
        result.warnings.append("nix-instantiate not found; skipped the Nix parser check")


_NAME_FN = "p: p.pname or (builtins.parseDrvName p.name).name"


def eval_command(tree: Path, root: NixOSRoot, target: tuple[str, ...]) -> list[str]:
    option = ".".join(target)
    if root.is_flake:
        host = socket.gethostname() or "nixos"
        return [
            "nix", "eval", "--json",
            f"path:{tree}#nixosConfigurations.{host}.config.{option}",
            "--apply", f"map ({_NAME_FN})",
        ]
    entry = tree / root.entry_file.relative_to(root.directory)
    expr = (
        f"map ({_NAME_FN}) "
        f"(import <nixpkgs/nixos> {{ configuration = {entry}; }}).config.{option}"
    )
    return ["nix-instantiate", "--eval", "--strict", "--json", "-E", expr]


def _eval_check(tree: Path, root: NixOSRoot, target: tuple[str, ...],
                new_attrs: list[str], result: ApplyResult) -> None:
    cmd = eval_command(tree, root, target)
    if not shutil.which(cmd[0]):
        raise LayoutApplyError(f"{cmd[0]} not found; cannot run the eval check")
    proc = _run(cmd)
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        tail = "\n".join(err[-12:])
        hint = ""
        if "undefined variable" in proc.stderr or "attribute" in proc.stderr and "missing" in proc.stderr:
            hint = "a package attribute may not exist on this nixpkgs release; check the name with the search"
        raise LayoutApplyError(f"evaluation of the staged configuration failed:\n{tail}", suggestion=hint)
    names = json.loads(proc.stdout or "[]")
    result.evaluated_names = names
    result.checks.append("eval (" + ".".join(target) + ")")
    present = set(names)
    for attr in new_attrs:
        leaf = attr.split(".")[-1]
        if leaf not in present:
            result.warnings.append(
                f"{attr}: evaluated fine, but no package named {leaf!r} found in "
                f"{'.'.join(target)} (its pname may differ)"
            )


def _build_check(tree: Path, root: NixOSRoot, result: ApplyResult) -> None:
    from ..promotion.validator import validate

    class _Model:  # the validator only needs ``.root``
        pass

    model = _Model()
    model.root = root
    validation = validate(tree, model)  # type: ignore[arg-type]
    if not validation.success:
        raise LayoutApplyError(f"build check failed: {validation.message}")
    result.checks.append("build (" + " ".join(validation.command) + ")")


def apply_edits(
    root: NixOSRoot,
    edits: list[FileEdit],
    *,
    check: str = "parse",
    target: tuple[str, ...] = DEFAULT_TARGET,
    new_attrs: list[str] | None = None,
    commit: bool = True,
) -> ApplyResult:
    if check not in CHECK_LEVELS:
        raise LayoutApplyError(f"unknown check level {check!r}", suggestion="/".join(CHECK_LEVELS))
    from ..promotion.transaction import copy_tree

    rels = [_rel(root.directory, e.path) for e in edits]
    result = ApplyResult(changed=[str(r) for r in rels])
    if not edits:
        return result

    with tempfile.TemporaryDirectory(prefix="nixorcist-layout-") as tmp:
        tree = Path(tmp) / "config"
        copy_tree(root.directory, tree)
        for rel, edit in zip(rels, edits):
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(edit.new_text, "utf-8")

        level = CHECK_LEVELS.index(check)
        if level >= 1:
            _parse_check(tree, rels, result)
        if level >= 2:
            _eval_check(tree, root, target, new_attrs or [], result)
        if level >= 3:
            _build_check(tree, root, result)

        if commit:
            for rel in rels:
                dst = root.directory / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tree / rel, dst)
            result.committed = True
    return result
