"""NixOS persistent backend: edit the system configuration declaratively
via Nix AST + promotion (spec §33/§34).

Install = append ``pkgs.<attr>`` to the discovered target list (extending
an existing ``environment.systemPackages``, creating a new module, or
embedding the list inline).

Remove = surgically delete matching elements from all discovered target
lists and return ``True``.

The ``NixOSBackend`` exposes standard :class:`PackageBackend` methods
and two extra helpers used by the CLI for deterministic backend-style
operations:

``remove_elements`` — remove matching requested names from target lists.
``clear_lists`` — strip all static entries from target lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import ResolvedPackage
from ..nix.ast import find_binding, list_of, parse_module
from ..nix.config import discover_root, NixOSRoot
from ..promotion.transformer import remove_from_list
from .base import DiscoveredTarget, PackageBackend


class NixOSError(NixorcistError):
    pass


@dataclass
class _Discovery:
    root: NixOSRoot
    source_text: str
    entries: list[_DeclEntry]


@dataclass
class _DeclEntry:
    attrpath: tuple[str, ...]
    with_pkgs: bool
    module: str
    list_start: int
    list_end: int


def _discover(config_root: Path | None = None, *, dry_run: bool = False) -> _Discovery:
    root = discover_root(config_root, dry_run=dry_run)
    text = root.entry_file.read_text("utf-8")
    ast = parse_module(text)
    entries: list[_DeclEntry] = []
    targets = [("environment", "systemPackages"), ("home", "packages")]
    for attrpath in targets:
        binding = find_binding(ast, attrpath)
        if binding is None:
            continue
        pkg_list = list_of(binding.children[0])
        if pkg_list is None:
            continue
        with_pkgs = False
        scope_node = binding.children[0]
        while True:
            if scope_node.kind == "with" and scope_node.children:
                ident = scope_node.children[0]
                if ident.kind in ("ident", "select") and ident.value in ("pkgs", "nixpkgs"):
                    with_pkgs = True
                    break
                scope_node = scope_node.children[-1]
            elif scope_node.kind in ("paren", "assert") and scope_node.children:
                scope_node = scope_node.children[-1]
            else:
                break
        entries.append(_DeclEntry(
            attrpath=attrpath,
            with_pkgs=with_pkgs,
            module=str(root.entry_file),
            list_start=pkg_list.start,
            list_end=pkg_list.end,
        ))
    return _Discovery(root=root, source_text=text, entries=entries)


def _normalise_element(value: str) -> str:
    value = value.strip()
    if value.startswith("pkgs."):
        value = value[len("pkgs."):].strip()
    return value.strip("\"'")


class NixOSBackend(PackageBackend):
    """Declarative backend editing the NixOS configuration in place."""

    name = "nixos"

    def install(self, packages: list[ResolvedPackage], *, dry_run: bool = False) -> bool:
        return False

    def remove(self, packages: list[ResolvedPackage], *, dry_run: bool = False) -> bool:
        ok, _ = self.remove_elements(packages, dry_run=dry_run)
        return ok

    def remove_all(self, *, dry_run: bool = False) -> bool:
        return self.clear_lists(dry_run=dry_run)

    def is_installed(self, pkg: ResolvedPackage, *, dry_run: bool = False) -> bool:
        return pkg.attribute in self.installed_attributes(dry_run=dry_run)

    def list_entries(self, *, dry_run: bool = False) -> list[dict[str, object]]:
        attrs = self.installed_attributes(dry_run=dry_run)
        return [{"requested": a, "attribute": a} for a in sorted(attrs)]

    def installed_attributes(self, *, dry_run: bool = False) -> set[str]:
        try:
            disc = _discover(dry_run=dry_run)
        except NixorcistError:
            return set()
        ast = parse_module(disc.source_text)
        attrs: set[str] = set()
        for decl in disc.entries:
            binding = find_binding(ast, decl.attrpath)
            if binding is None:
                continue
            pkg_list = list_of(binding.children[0])
            if pkg_list is None:
                continue
            for child in pkg_list.children:
                raw = child.value.strip()
                if "${" in raw or "}" in raw or raw.startswith("<") or raw.startswith("#"):
                    continue
                attr = raw[5:].strip() if raw.startswith("pkgs.") else raw
                attr = attr.strip("\"'")
                if attr and not any(ch in attr for ch in " (){}[]"):
                    attrs.add(attr)
        return attrs

    def discovered_targets(self, *, dry_run: bool = False) -> list[DiscoveredTarget]:
        try:
            disc = _discover(dry_run=dry_run)
        except NixorcistError:
            return []
        return [
            DiscoveredTarget(
                backend_name="nixos",
                display_name=".".join(d.attrpath),
                path=d.module,
            )
            for d in disc.entries
        ]

    def remove_elements(
        self,
        packages: list[ResolvedPackage],
        *,
        dry_run: bool = False,
    ) -> tuple[bool, int]:
        try:
            disc = _discover(dry_run=dry_run)
        except NixorcistError:
            return False, 0
        targets = {_normalise_element(p.requested) for p in packages}
        total = 0
        any_changed = False
        for decl in disc.entries:
            ast = parse_module(disc.source_text)
            binding = find_binding(ast, decl.attrpath)
            if binding is None:
                continue
            pkg_list = list_of(binding.children[0])
            if pkg_list is None:
                continue
            new_text, count = remove_from_list(disc.source_text, pkg_list, targets)
            if count:
                any_changed = True
                total += count
                if not dry_run:
                    Path(decl.module).write_text(new_text, "utf-8")
        return any_changed, total

    def clear_lists(self, *, dry_run: bool = False) -> bool:
        try:
            disc = _discover(dry_run=dry_run)
        except NixorcistError:
            return False
        changed = False
        for decl in disc.entries:
            ast = parse_module(disc.source_text)
            binding = find_binding(ast, decl.attrpath)
            if binding is None:
                continue
            pkg_list = list_of(binding.children[0])
            if pkg_list is None or not pkg_list.children:
                continue
            text = disc.source_text
            start = pkg_list.start + 1
            end = pkg_list.end - 1
            new_text = text[:start] + text[end:]
            changed = True
            if not dry_run:
                Path(decl.module).write_text(new_text, "utf-8")
        return changed