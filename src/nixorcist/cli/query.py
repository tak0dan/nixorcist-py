"""Query operations for Nixorcist: ``-L`` (list), ``-F`` (find/search) and
``-V`` (validate/inspect).

These are information/query operations (spec §22).  They never mutate the
registry, the profile or the NixOS configuration.  ``-L`` and ``-F`` are
handled outside the mutation DSL: a query "command family" is dispatched from
the CLI entry point before plan execution.

Supported forms (spec §§13-16, 19, 22):

    -L                 list registry groups (summary: GROUP STATE PACKAGES)
    -LG / -L --groups  detailed tree listing of each group's packages
    -Lc                list configuration declarations
    -Lo                list orphaned configuration declarations
    -F                 search registry groups
    -Fname=%sub%       name contains
    -Fname=gaming%     name starts with
    -Fname=%amin       name ends with
    -Fname=Programming name equal
    -F-git-            groups containing a package whose name matches git
    -F-{git,gcc}-      groups containing BOTH git and gcc
    -F-{git|gcc}-      groups containing git OR gcc
    -Fc                search the NixOS configuration instead of the registry
    -Fo                search orphaned configuration declarations
    -V                 validate all registry groups
    -V#Name[,Name...]  validate specific groups
    -V --config        validate the discovered configuration mapping

Multiple ``-F`` predicates combine with AND semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..core.models import Backend, Group, ResolvedPackage
from ..groups.manager import GroupManager

_OPERATION_CHARS = set("IRPDEAOGY")


class QueryError(ValueError):
    pass


# ---------------------------------------------------------------------------
# -L layout
# ---------------------------------------------------------------------------


def _display_state(group: Group) -> str:
    if not group.state.active:
        return "inactive"
    if group.state.backend is Backend.DECLARATIVE:
        return "declarative"
    return "imperative"


def list_summary(groups: Iterable[Group]) -> str:
    rows = [(g.name, _display_state(g), len(g.packages)) for g in groups]
    if not rows:
        return "(no groups)"
    name_w = max(len(r[0]) for r in rows)
    state_w = max(len(r[1]) for r in rows)
    lines = [f"{'GROUP':<{name_w}}  {'STATE':<{state_w}}  PACKAGES"]
    for name, state, count in rows:
        lines.append(f"{name:<{name_w}}  {state:<{state_w}}  {count}")
    return "\n".join(lines)


def list_tree(groups: Iterable[Group]) -> str:
    lines: list[str] = []
    for group in groups:
        lines.append(group.name)
        pkgs = group.packages
        if not pkgs:
            lines.append("└── (no packages)")
            continue
        for i, pkg in enumerate(pkgs):
            prefix = "└── " if i == len(pkgs) - 1 else "├── "
            lines.append(f"{prefix}{pkg.requested}")
    return "\n".join(lines) if lines else "(no groups)"


def list_declarations(groups: Iterable[tuple[str, list[ResolvedPackage]]]) -> str:
    lines: list[str] = []
    for name, pkgs in groups:
        lines.append(f"{name} ({', '.join(p.requested for p in pkgs) or 'empty'})")
    return "\n".join(lines) if lines else "(no configuration declarations)"


# ---------------------------------------------------------------------------
# -F predicate parsing / matching
# ---------------------------------------------------------------------------


@dataclass
class Predicate:
    field: str  # "name" | "content"
    mode: str  # "contains" | "startswith" | "endswith" | "exact" | "all" | "any" | "compound"
    value: str = ""
    values: list[str] = ()
    has_pattern: bool = False
    # compound name parts: ordered list of (mode, substring), ANDed together.
    # e.g. `%am%in%` -> contains(am) AND contains(in).
    name_parts: tuple = ()

    def matches_name(self, name: str) -> bool:
        if self.name_parts:
            return all(_match_seg(mode, sub, name) for mode, sub in self.name_parts)
        if self.mode == "contains":
            return self.value.lower() in name.lower()
        if self.mode == "startswith":
            return name.lower().startswith(self.value.lower())
        if self.mode == "endswith":
            return name.lower().endswith(self.value.lower())
        return name == self.value

    def matches_package(self, pkg_name: str) -> bool:
        if self.mode == "all":
            return all(v.lower() in pkg_name.lower() for v in self.values)
        if self.mode == "any":
            return any(v.lower() in pkg_name.lower() for v in self.values)
        return self.value.lower() in pkg_name.lower()


def _match_seg(mode: str, sub: str, name: str) -> bool:
    if mode == "startswith":
        return name.lower().startswith(sub.lower())
    if mode == "endswith":
        return name.lower().endswith(sub.lower())
    return sub.lower() in name.lower()


@dataclass
class FindQuery:
    source: str = "registry"  # "registry" | "config" | "orphans"
    name_preds: tuple = ()
    content_preds: tuple = ()

    @property
    def has_predicates(self) -> bool:
        return bool(self.name_preds or self.content_preds)

    def matches_group(self, name: str, packages: Sequence[ResolvedPackage]) -> bool:
        if not self.has_predicates:
            return True
        for pred in self.name_preds:
            if not pred.matches_name(name):
                return False
        if self.content_preds:
            names = [p.requested for p in packages]
            for pred in self.content_preds:
                if pred.mode == "all":
                    if not all(
                        any(v.lower() in n.lower() for n in names) for v in pred.values
                    ):
                        return False
                elif pred.mode == "any":
                    if not any(
                        any(v.lower() in n.lower() for n in names) for v in pred.values
                    ):
                        return False
                elif not any(pred.matches_package(n) for n in names):
                    return False
        return True


def _parse_pattern(pattern: str) -> Predicate:
    """Turn ``%sub%``/``sub%``/``%sub``/``sub``/``%a%b%`` into a name predicate.

    Compound patterns such as ``%am%in%`` split into contains(am) AND
    contains(in); ``a%b%c`` -> startswith(a) AND contains(b) AND endswith(c).
    """
    count = pattern.count("%")
    if count == 2 and pattern.startswith("%") and pattern.endswith("%"):
        return Predicate("name", "contains", pattern[1:-1], has_pattern=True)
    if count >= 2:
        segs = pattern.split("%")
        parts: list[tuple[str, str]] = []
        for idx, sub in enumerate(segs):
            if not sub:
                continue
            if not pattern.startswith("%") and idx == 0:
                parts.append(("startswith", sub))
            elif not pattern.endswith("%") and idx == len(segs) - 1:
                parts.append(("endswith", sub))
            else:
                parts.append(("contains", sub))
        return Predicate("name", "compound", has_pattern=True, name_parts=tuple(parts))
    if pattern.startswith("%") and len(pattern) >= 2:
        return Predicate("name", "endswith", pattern[1:], has_pattern=True)
    if pattern.endswith("%") and len(pattern) >= 2:
        return Predicate("name", "startswith", pattern[:-1], has_pattern=True)
    return Predicate("name", "exact", pattern)


def parse_find_tokens(tokens: Sequence[str]) -> FindQuery:
    """Parse find tokens such as ``name=%dev%``, ``-git-``, ``c``.

    A leading ``-F`` prefix on a token is tolerated (``-Fname=%dev%``);
    combined tokens like ``-Fc``/``-Fo`` (config/orphans source) are also
    recognized.
    """
    query = FindQuery()
    tokens = list(tokens)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-F", "--find", "--search"):
            i += 1
            continue
        if tok.startswith("-F") and len(tok) > 2:
            body_tok = tok[2:]
            if body_tok in ("c", "o", "reg"):
                tok = body_tok
            elif body_tok:
                tokens[i:i + 1] = [body_tok]
                tok = body_tok
        if tok in ("c", "config", "--config"):
            query.source = "config"
        elif tok in ("o", "orphans", "--orphans"):
            query.source = "orphans"
        elif tok in ("reg", "--registry"):
            query.source = "registry"
        elif tok.startswith("name="):
            query.name_preds = query.name_preds + (_parse_pattern(tok[len("name="):]),)
        elif tok == "--name" and i + 1 < len(tokens):
            query.name_preds = query.name_preds + (_parse_pattern(tokens[i + 1]),)
            i += 1
        elif tok.startswith("-") and tok.endswith("-") and len(tok) > 2:
            inner = tok[1:-1]
            pred: Predicate
            if inner.startswith("{") and inner.endswith("}"):
                raw = inner[1:-1]
                if "|" in raw:
                    pred = Predicate("content", "any", values=[p for p in raw.split("|") if p])
                else:
                    pred = Predicate("content", "all", values=[p for p in raw.split(",") if p])
            else:
                pred = Predicate("content", "contains", inner)
            query.content_preds = query.content_preds + (pred,)
        elif tok == "--content" and i + 1 < len(tokens):
            val = tokens[i + 1]
            if val.startswith("{") and val.endswith("}"):
                raw = val[1:-1]
                pred = (
                    Predicate("content", "any", values=[p for p in raw.split("|") if p])
                    if "|" in raw
                    else Predicate("content", "all", values=[p for p in raw.split(",") if p])
                )
            else:
                pred = Predicate("content", "contains", val)
            query.content_preds = query.content_preds + (pred,)
            i += 1
        elif tok in ("--",):
            pass
        elif tok and "=" in tok:
            raise QueryError(f"unsupported find predicate {tok!r}")
        else:
            query.name_preds = query.name_preds + (Predicate("name", "contains", tok),)
        i += 1
    return query


# ---------------------------------------------------------------------------
# -L / -F rendering helpers used by the CLI dispatch layer
# ---------------------------------------------------------------------------


def registry_manager(cls: type, paths) -> GroupManager:
    from ..groups.repository import GroupRepository

    return cls(GroupRepository(paths))


def discover_config(config_root: Path | None):
    """Return (model, declarations [(name, pkgs), ...])."""
    from ..nix.config import discover_root
    from ..promotion.discover import discover

    root = discover_root(config_root)
    model = discover(root)
    decls: list[tuple[str, list[ResolvedPackage]]] = []
    for attrpath, pkgs in model.discover_existing_packages().items():
        name = attrpath[-1] if attrpath else ""
        decls.append((name, pkgs))
    return model, decls


def orphaned_names(
    manager: GroupManager, model, config_root: Path | None
) -> list[tuple[str, list[ResolvedPackage]]]:
    """Declarations no registry group claims (module, attrpath) for."""
    from ..nix.config import discover_root

    root = discover_root(config_root)
    owned: set[tuple[Path, tuple[str, ...]]] = set()
    for group in manager.all_groups():
        decl = group.declarative
        if decl and decl.module and decl.configuration_root:
            owned.add((Path(decl.configuration_root) / decl.module, ()))
    orphans: list[tuple[str, list[ResolvedPackage]]] = []
    for decl in model.declarations:
        key = (decl.module, decl.attrpath)
        if key in owned:
            continue
        pkgs = _decl_packages(decl)
        orphans.append((decl.attrpath[-1], pkgs))
    return orphans


def _decl_packages(decl) -> list[ResolvedPackage]:
    pkgs: list[ResolvedPackage] = []
    for child in decl.list_node.children:
        for node in child.walk():
            if node.kind != "ident" or not node.value:
                continue
            raw = node.value.strip()
            if "${" in raw or "}" in raw:
                continue
            attr = raw.removeprefix("pkgs.").strip() if decl.with_pkgs else raw
            if attr and (attr.isidentifier() or any(ch.isalpha() for ch in attr)):
                pkgs.append(ResolvedPackage(requested=attr, attribute=attr))
    return pkgs


class _VResult:
    def __init__(self, name: str):
        self.name = name
        self.lines: list[str] = []

    def ok(self, value) -> None:
        self.lines.append(f"    OK    {value}")

    def missing(self, value) -> None:
        self.lines.append(f"    MISS  {value}")

    def bad(self, value) -> None:
        self.lines.append(f"    BAD   {value}")

    def render(self) -> str:
        return (self.name + "\n" + "\n".join(self.lines)).rstrip()


def validate_group(
    group: Group,
    installed_attributes: set[str],
    model=None,
) -> str:
    out = _VResult(group.name)
    member_names = {p.requested for p in group.packages}
    attr_names = {p.attribute for p in group.packages}
    out.lines.append(f"    Registry:        {'OK' if group.state.active else 'OK'}")

    if group.packages:
        missing = sorted(a for a in attr_names - installed_attributes)
        active = group.state.active
        profile_ok = (not active) or not missing
        out.lines.append(f"    Packages:        {len(group.packages)}")
        out.lines.append(f"    Profile:         {'OK' if profile_ok else 'BAD'}")
        if not profile_ok and missing:
            out.lines.append(f"    missing:         {', '.join(missing)}")
    else:
        out.lines.append(f"    Packages:        0")
        out.lines.append(f"    Profile:         OK")

    if group.declarative and group.declarative.declaration:
        out.lines.append(
            f"    NixOS decl:      PRESENT ({group.declarative.module or '?'})"
        )
    elif model is not None:
        present = _declaration_present(model, group.name)
        out.lines.append(
            f"    NixOS decl:      {'PRESENT' if present else 'NOT PRESENT'}"
        )
    else:
        out.lines.append(f"    NixOS decl:      NOT PRESENT")

    out.lines.append(f"    State:           {_display_state(group)}")
    return out.render()


def find_groups(
    query: FindQuery,
    manager: GroupManager,
    groups: Sequence[Group],
) -> list[Group]:
    matched: list[Group] = []
    for group in groups:
        if query.matches_group(group.name, group.packages):
            matched.append(group)
    return matched


def _declaration_present(model, name: str) -> bool:
    try:
        decls = model.discover_existing_packages()
    except Exception:
        return False
    return any(attrpath and attrpath[-1] == name for attrpath in decls)


def is_operation_char(ch: str) -> bool:
    return ch in _OPERATION_CHARS


def split_group_refs(merged: str) -> list[str]:
    """Split ``#a,b`` or repeated ``#a#b`` into group names."""
    refs: list[str] = []
    for piece in re.split(r"[#,]+", merged):
        piece = piece.strip()
        if piece:
            refs.append(piece)
    return refs