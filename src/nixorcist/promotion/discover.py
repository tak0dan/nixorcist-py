"""Configuration discovery: find declarative package declarations across
NixOS modules and classify them for promotion planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..core.models import ResolvedPackage
from ..nix.ast import Node, find_binding, is_with_pkgs, list_of, module_attrset
from ..nix.config import NixOSRoot
from ..nix.imports import ImportGraph, build_import_graph
from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError


_PACKAGE_TARGETS: list[tuple[str, ...]] = [
    ("environment", "systemPackages"),
    ("home", "packages"),
]

_FREQUENCY_PACKAGE_TARGETS = {
    ("environment", "systemPackages"): 0,
    ("home", "packages"): 1,
}


class DiscoveryError(NixorcistError):
    pass


class ConfigurationKind(str, Enum):
    """Configuration layout determined from the statically-resolved import graph.

    ``PLAIN`` means a package declaration is owned by the entry configuration
    itself (or no package declaration exists yet). ``MODULAR`` means the
    package declaration Nixorcist would manage lives in an imported module.
    A standard ``configuration.nix`` that imports only
    ``hardware-configuration.nix`` is therefore still plain. Dynamic imports
    are deliberately not guessed: they remain visible on ``ImportGraph`` and
    the planner only edits bindings it parsed structurally.
    """

    PLAIN = "plain"
    MODULAR = "modular"


@dataclass
class PackageDeclaration:
    module: Path
    attrpath: tuple[str, ...]
    binding: Node
    list_node: Node
    with_pkgs: bool
    module_roots: list[str] = ()

    def __hash__(self) -> int:
        return hash((self.module, self.attrpath))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PackageDeclaration):
            return NotImplemented
        return self.module == other.module and self.attrpath == other.attrpath


@dataclass
class ConfigurationModel:
    root: NixOSRoot
    graph: ImportGraph
    declarations: list[PackageDeclaration]

    @property
    def configuration_kind(self) -> ConfigurationKind:
        """Classify the configuration without relying on directory names.

        The classification is based on real, parsed ``imports`` edges, rather
        than heuristics such as the presence of a ``modules/`` directory.
        """
        if any(declaration.module != self.root.entry_file for declaration in self.declarations):
            return ConfigurationKind.MODULAR
        return ConfigurationKind.PLAIN

    @property
    def is_modular(self) -> bool:
        return self.configuration_kind is ConfigurationKind.MODULAR

    @property
    def preferred_declaration(self) -> PackageDeclaration | None:
        """The declaration that should be edited first during promotion."""
        candidates = [d for d in self.declarations if d.module == self.root.entry_file]
        if candidates:
            candidates.sort(key=lambda d: _FREQUENCY_PACKAGE_TARGETS.get(d.attrpath, 99))
            return candidates[0]
        visible = [d for d in self.declarations if self.root.directory in d.module.parents or d.module == self.root.entry_file]
        if visible:
            visible.sort(key=lambda d: (
                _FREQUENCY_PACKAGE_TARGETS.get(d.attrpath, 99),
                str(d.module),
            ))
            return visible[0]
        if self.declarations:
            return sorted(self.declarations, key=lambda d: _FREQUENCY_PACKAGE_TARGETS.get(d.attrpath, 99))[0]
        return None

    @property
    def can_write_root(self) -> bool:
        root = self.graph.root_module
        return root is not None and root.parsed and root.ast is not None

    def discover_existing_packages(self) -> dict[tuple[str,...], list[ResolvedPackage]]:
        """Present a first pass view of what each declaration already contains."""
        result: dict[tuple[str,...], list[ResolvedPackage]] = {}
        for decl in self.declarations:
            pkgs: list[ResolvedPackage] = []
            for child in decl.list_node.children:
                for node in child.walk():
                    if node.kind != "ident" or not node.value:
                        continue
                    raw = node.value.strip()
                    if "${" in raw or "}" in raw:
                        continue
                    attr = raw.removeprefix("pkgs.").strip() if decl.with_pkgs else raw
                    if attr and attr.isidentifier() or any(ch.isalpha() for ch in attr):
                        pkgs.append(ResolvedPackage(requested=attr, attribute=attr))
            result[decl.attrpath] = pkgs
        return result

    def orphan_declarations(
        self,
        owned: set[tuple[Path, tuple[str, ...]]],
    ) -> list[PackageDeclaration]:
        """Declarations present in the configuration that no group owns.

        ``owned`` is the set of ``(module, attrpath)`` tuples that live
        registry groups claim (from ``DeclarativeMetadata``).  Everything else
        discovered in the configuration is an *orphan* — the ``-Ooo`` orphan
        sweep target (spec §38).
        """
        return [d for d in self.declarations if (d.module, d.attrpath) not in owned]


def _is_within_root(path: Path, root: NixOSRoot) -> bool:
    try:
        path.resolve().relative_to(root.directory)
        return True
    except ValueError:
        return False


def discover(model_root: NixOSRoot) -> ConfigurationModel:
    graph = build_import_graph(model_root.entry_file)
    declarations: list[PackageDeclaration] = []
    for module_path, mod in graph.modules.items():
        if not mod.parsed or mod.ast is None:
            continue
        if not _is_within_root(Path(module_path), model_root):
            continue
        for target in _PACKAGE_TARGETS:
            binding = find_binding(mod.ast, target)
            if binding is None:
                continue
            pkg_list = list_of(binding.children[0])
            if pkg_list is None:
                continue
            declarations.append(
                PackageDeclaration(
                    module=Path(module_path),
                    attrpath=target,
                    binding=binding,
                    list_node=pkg_list,
                    with_pkgs=is_with_pkgs(binding.children[0]),
                )
            )
    return ConfigurationModel(root=model_root, graph=graph, declarations=declarations)
