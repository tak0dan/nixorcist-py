"""NixOS import graph construction.

Nixorcist must not assume that every package lives in the root
``configuration.nix``.  This module resolves ``imports = [ ... ];`` chains to
produce a graph of module files, recording importers and (where parseable)
package declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from .ast import Node, find_binding, list_of, parse_module


class ImportGraphError(NixorcistError):
    pass


def _has_interpolation(string_node: Node) -> bool:
    raw = string_node.value
    return "${" in raw


def _element_to_path(element: Node, base_dir: Path) -> Path | None:
    """Return a resolvable absolute path for an ``imports`` list element, or
    ``None`` when the element is dynamic (cannot be resolved statically)."""
    if element.kind == "path":
        raw = element.value
        if raw.startswith("<") or raw.startswith("~"):
            return None
        return (base_dir / raw).resolve()
    if element.kind == "string" and not _has_interpolation(element):
        raw = element.value.strip('"')
        if not raw or raw.startswith(("<", "http://", "https://", "github:")):
            return None
        return (base_dir / raw).resolve()
    return None


@dataclass
class ModuleNode:
    path: Path
    exists: bool = False
    parsed: bool = False
    imports: list[Path] = field(default_factory=list)
    dynamic_imports: list[str] = field(default_factory=list)
    importers: list[Path] = field(default_factory=list)
    parse_error: str = ""
    text: str = ""
    ast: Node | None = None

    @property
    def key(self) -> str:
        return str(self.path)


@dataclass
class ImportGraph:
    root: Path
    entry_exists: bool = False
    modules: dict[str, ModuleNode] = field(default_factory=dict)

    @property
    def root_module(self) -> ModuleNode | None:
        return self.modules.get(str(self.root))

    def all_modules(self) -> list[ModuleNode]:
        return [self.modules[k] for k in sorted(self.modules)]


def _load_module(path: Path) -> ModuleNode:
    node = ModuleNode(path=path)
    if not path.exists():
        return node
    node.exists = True
    try:
        node.text = path.read_text("utf-8")
    except OSError as exc:
        node.parse_error = f"could not read: {exc}"
        return node
    try:
        ast = parse_module(node.text)
        node.ast = ast
        node.parsed = True
    except Exception as exc:
        node.parse_error = str(exc)
        return node

    imports_binding = find_binding(ast, ["imports"])
    if imports_binding is None:
        return node
    import_list = list_of(imports_binding.children[0])
    if import_list is None:
        return node
    for element in import_list.children:
        resolved = _element_to_path(element, path.parent)
        if resolved is None:
            node.dynamic_imports.append(element.value)
            continue
        node.imports.append(resolved)
    return node


def build_import_graph(entry_file: Path) -> ImportGraph:
    entry = entry_file.resolve()
    graph = ImportGraph(root=entry, entry_exists=entry.exists())
    seen: dict[str, ModuleNode] = {}

    def visit(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        node = _load_module(path)
        seen[key] = node
        for imported in node.imports:
            target = imported if imported.is_file() else imported
            if not target.is_file() and imported.is_dir():
                target = imported / "default.nix"
            visit(target)

    visit(entry)
    graph.modules = seen

    for key, node in seen.items():
        for imported in node.imports:
            target = imported if imported.is_file() else imported
            if not target.is_file() and imported.is_dir():
                target = imported / "default.nix"
            child = seen.get(str(target))
            if child is not None:
                child.importers.append(node.path)

    return graph