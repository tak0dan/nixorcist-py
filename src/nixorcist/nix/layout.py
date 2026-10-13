"""Configuration *architecture* analysis: how does this user lay out their
package groups, and how should a new group be written so it looks like theirs?

People declare packages in very different ways.  This module recognises four
group styles and the mechanisms behind them:

``inline``
    A literal list inside the entry file's ``environment.systemPackages``.
    Several inline groups are written as a ``++`` chain, each list optionally
    named by a ``# comment`` right above it (or right after its ``[``)::

        environment.systemPackages = with pkgs; [ vim git ]
          # office
          ++ [ libreoffice ];

``let``
    A named list in the entry file's ``let`` block, referenced from the
    package binding (``devTools = with pkgs; [ ... ]; ... ++ devTools``).

``module``
    A group in its own file, referenced straight from the entry file.  Either
    a NixOS module imported through ``imports`` that sets
    ``environment.systemPackages`` itself, or a *list file* pulled in with
    ``import ./x.nix { inherit pkgs; }`` inside the package binding.

``hub``
    A group in its own file that is aggregated by an intermediate *hub* file
    (for example ``./packages/default.nix`` importing ``dev.nix``,
    ``media.nix``, ...), rather than by the entry file directly.

Everything is derived from the structural parser in :mod:`.ast` and the
statically-resolved import graph; nothing is guessed from directory names.
Contributors that cannot be resolved statically (``lib.optionals ...``,
interpolated imports) are reported as *opaque* rather than mis-classified.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from .ast import Node, NixParseError, bindings, list_of, module_attrset, parse_module
from .config import NixOSRoot
from .imports import ImportGraph, build_import_graph

DEFAULT_TARGET: tuple[str, ...] = ("environment", "systemPackages")
PACKAGE_TARGETS: tuple[tuple[str, ...], ...] = (
    ("environment", "systemPackages"),
    ("home", "packages"),
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'-]*$")


class LayoutError(NixorcistError):
    def __init__(self, message: str, suggestion: str = ""):
        super().__init__(Diagnostic(ErrorCode.DISCOVERY, message, suggestion=suggestion))


class GroupStyle(str, Enum):
    INLINE = "inline"
    LET = "let"
    MODULE = "module"
    HUB = "hub"


class Mechanism(str, Enum):
    LITERAL = "literal"  # [ ... ] directly in a package binding
    LET_BINDING = "let"  # name = [ ... ]; in a let block
    NIXOS_MODULE = "nixos-module"  # file that sets the package option itself
    LIST_FILE = "list-file"  # file that evaluates to a list, via ``import``


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageRef:
    """One element of a package list, exactly as the user wrote it."""

    text: str
    attr: str | None  # ``kdePackages.kate`` for ``pkgs.kdePackages.kate``; None if opaque

    @property
    def is_opaque(self) -> bool:
        return self.attr is None


@dataclass
class DetectedGroup:
    name: str
    style: GroupStyle
    mechanism: Mechanism
    file: Path  # file holding the list itself
    target: tuple[str, ...]
    list_start: int
    list_end: int
    with_pkgs: bool
    packages: list[PackageRef] = field(default_factory=list)
    declared_in: Path | None = None  # file holding the package binding / imports entry
    hub: Path | None = None
    # How the aggregating file refers to this group (list-file mechanism):
    # the full reference expression and the span of the path inside it.
    reference_text: str | None = None
    reference_path_span: tuple[int, int] | None = None  # relative to reference_text

    @property
    def attrs(self) -> list[str]:
        return [p.attr for p in self.packages if p.attr]


@dataclass
class OpaqueContributor:
    file: Path
    target: tuple[str, ...]
    text: str


@dataclass
class ConfigLayout:
    root: NixOSRoot
    graph: ImportGraph
    groups: list[DetectedGroup]
    opaque: list[OpaqueContributor]
    hubs: list[Path]
    unparsed: list[tuple[Path, str]]

    # -- queries -----------------------------------------------------------
    def group(self, name: str) -> DetectedGroup | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None

    def style_counts(self, target: tuple[str, ...] = DEFAULT_TARGET) -> Counter:
        return Counter(g.style for g in self.groups if g.target == target)

    def where_declared(self, attr: str) -> list[DetectedGroup]:
        """Every group that already declares ``attr`` (exact attribute path)."""
        return [g for g in self.groups if attr in g.attrs]

    def declared_attrs(self) -> set[str]:
        return {a for g in self.groups for a in g.attrs}

    def to_dict(self) -> dict:
        def rel(p: Path | None) -> str | None:
            if p is None:
                return None
            try:
                return str(p.relative_to(self.root.directory))
            except ValueError:
                return str(p)

        decision = recommend_style(self)
        return {
            "root": str(self.root.directory),
            "entry": rel(self.root.entry_file),
            "hubs": [rel(h) for h in self.hubs],
            "style_counts": {s.value: n for s, n in self.style_counts().items()},
            "recommendation": {
                "style": decision.style.value if decision.style else None,
                "needs_choice": decision.needs_choice,
                "options": [s.value for s in decision.options],
                "reason": decision.reason,
            },
            "groups": [
                {
                    "name": g.name,
                    "style": g.style.value,
                    "mechanism": g.mechanism.value,
                    "file": rel(g.file),
                    "declared_in": rel(g.declared_in),
                    "hub": rel(g.hub),
                    "target": ".".join(g.target),
                    "with_pkgs": g.with_pkgs,
                    "packages": [p.text for p in g.packages],
                }
                for g in self.groups
            ],
            "opaque": [
                {"file": rel(o.file), "target": ".".join(o.target), "text": o.text}
                for o in self.opaque
            ],
            "unparsed": [{"file": rel(p), "error": e} for p, e in self.unparsed],
        }


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _unwrap(node: Node) -> tuple[Node, bool]:
    """Strip ``with``/``paren``/``assert`` wrappers; report ``with pkgs;``."""
    with_pkgs = False
    for _ in range(16):
        if node.kind == "with" and len(node.children) == 2:
            scope = node.children[0]
            if scope.kind in ("ident", "select") and scope.value in ("pkgs", "nixpkgs"):
                with_pkgs = True
            node = node.children[1]
        elif node.kind in ("paren", "assert") and node.children:
            node = node.children[-1]
        else:
            break
    return node, with_pkgs


def find_package_binding(ast: Node, target: tuple[str, ...]) -> Node | None:
    """Find ``a.b = ...`` or the nested ``a = { b = ...; }`` form."""
    top = bindings(ast)
    for child in top:
        if child.kind == "binding" and tuple(child.attrpath) == target:
            return child
    for child in top:
        if child.kind != "binding" or not child.children:
            continue
        prefix = tuple(child.attrpath)
        if not prefix or target[: len(prefix)] != prefix:
            continue
        inner, _ = _unwrap(child.children[0])
        if inner.kind != "attrset":
            continue
        rest = target[len(prefix):]
        for sub in inner.children:
            if sub.kind == "binding" and tuple(sub.attrpath) == rest:
                return sub
    return None


def _let_node(ast: Node) -> Node | None:
    current = ast
    for _ in range(8):
        if current.kind == "let":
            return current
        if current.kind in ("lambda", "with", "paren") and current.children:
            current = current.children[-1]
        else:
            return None
    return None


def _let_bindings(ast: Node) -> dict[str, Node]:
    let = _let_node(ast)
    if let is None:
        return {}
    out: dict[str, Node] = {}
    for child in let.children[:-1]:
        if child.kind == "binding" and len(child.attrpath) == 1:
            out[child.attrpath[0]] = child
    return out


def _import_path(node: Node) -> Node | None:
    """``import ./x.nix ...`` (any number of applied args) -> the path node."""
    node, _ = _unwrap(node)
    current = node
    while current.kind == "apply" and current.children:
        head = current.children[0]
        if head.kind == "ident" and head.value == "import":
            arg = current.children[1]
            return arg if arg.kind in ("path", "string") else None
        current = head
    return None


def _package_ref(node: Node, text: str, with_pkgs: bool) -> PackageRef:
    raw = text[node.start:node.end]
    if node.kind in ("ident", "select"):
        attr = raw.strip()
        if attr.startswith("pkgs."):
            attr = attr[len("pkgs."):]
        elif not with_pkgs:
            # Outside ``with pkgs;`` a bare name is a local binding, not a package.
            return PackageRef(text=raw, attr=None)
        if all(_IDENT_RE.match(part) for part in attr.split(".")):
            return PackageRef(text=raw, attr=attr)
    return PackageRef(text=raw, attr=None)


def _element_text(attr: str, with_pkgs: bool) -> str:
    return attr if with_pkgs else f"pkgs.{attr}"


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return value or "group"


def _comment_name(text: str, list_start: int) -> str | None:
    """Group name from a ``# name`` comment after ``[`` or on the lines above."""
    eol = text.find("\n", list_start)
    same_line = text[list_start + 1 : eol if eol >= 0 else len(text)]
    m = re.match(r"\s*#\s*(?:group:\s*)?(.+?)\s*$", same_line)
    if m:
        return _slug(m.group(1))

    before = text[:list_start]
    # Ignore the tail of the current line if it is only syntax (``++``, ``(``,
    # ``with pkgs;``, ``name =``) -- the comment, if any, sits above it.
    lines = before.split("\n")
    tail = lines.pop()
    if re.sub(r"(\+\+|\(|with\s+pkgs\s*;|[A-Za-z0-9_.'-]+\s*=)", "", tail).strip():
        return None
    while lines:
        line = lines.pop().strip()
        if not line:
            continue
        m = re.match(r"#\s*(?:group:\s*)?(.+?)\s*$", line)
        if m:
            return _slug(m.group(1))
        return None
    return None


def _file_group_name(path: Path) -> str:
    stem = path.stem
    if stem == "default":
        stem = path.parent.name
    return _slug(stem)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class _Contribution:
    mechanism: Mechanism
    file: Path
    target: tuple[str, ...]
    declared_in: Path
    list_node: Node | None
    with_pkgs: bool
    name_hint: str | None
    text: str  # text of ``file``
    reference_text: str | None = None
    reference_path_span: tuple[int, int] | None = None


def _within(path: Path, root: NixOSRoot) -> bool:
    try:
        path.resolve().relative_to(root.directory)
        return True
    except ValueError:
        return False


def _parse_file(path: Path) -> tuple[str, Node]:
    text = path.read_text("utf-8")
    return text, parse_module(text)


def _collect(
    value: Node,
    module: Path,
    text: str,
    ast: Node,
    target: tuple[str, ...],
    inherited_with: bool,
    out: list[_Contribution],
    opaque: list[OpaqueContributor],
    unparsed: list[tuple[Path, str]],
) -> None:
    node, with_pkgs = _unwrap(value)
    with_pkgs = with_pkgs or inherited_with

    if node.kind == "op" and node.value == "++" and len(node.children) == 2:
        for child in node.children:
            _collect(child, module, text, ast, target, with_pkgs, out, opaque, unparsed)
        return

    if node.kind == "list":
        out.append(
            _Contribution(
                mechanism=Mechanism.LITERAL,
                file=module,
                target=target,
                declared_in=module,
                list_node=node,
                with_pkgs=with_pkgs,
                name_hint=_comment_name(text, node.start),
                text=text,
            )
        )
        return

    if node.kind == "ident":
        lets = _let_bindings(ast)
        if node.value in lets:
            binding = lets[node.value]
            inner, inner_with = _unwrap(binding.children[0])
            if inner.kind == "list":
                out.append(
                    _Contribution(
                        mechanism=Mechanism.LET_BINDING,
                        file=module,
                        target=target,
                        declared_in=module,
                        list_node=inner,
                        with_pkgs=inner_with,
                        name_hint=_slug(node.value),
                        text=text,
                    )
                )
                return
            # ``media = import ./media.nix { ... };`` -- follow the import.
            if _import_path(binding.children[0]) is not None:
                before = len(out)
                _collect(binding.children[0], module, text, ast, target, False, out, opaque, unparsed)
                for contribution in out[before:]:
                    contribution.name_hint = contribution.name_hint or _slug(node.value)
                return

    path_node = _import_path(node)
    if path_node is not None:
        raw = path_node.value.strip('"')
        if "${" not in raw and not raw.startswith("<"):
            target_file = (module.parent / raw).resolve()
            if target_file.is_dir():
                target_file = target_file / "default.nix"
            try:
                sub_text, sub_ast = _parse_file(target_file)
            except (OSError, NixParseError) as exc:
                unparsed.append((target_file, str(exc)))
                opaque.append(OpaqueContributor(module, target, text[node.start:node.end]))
                return
            body = sub_ast.children[-1] if sub_ast.kind == "lambda" and sub_ast.children else sub_ast
            inner, inner_with = _unwrap(body)
            if inner.kind == "list":
                ref_start = value.start
                ref_text = text[value.start:value.end]
                out.append(
                    _Contribution(
                        mechanism=Mechanism.LIST_FILE,
                        file=target_file,
                        target=target,
                        declared_in=module,
                        list_node=inner,
                        with_pkgs=inner_with,
                        name_hint=None,
                        text=sub_text,
                        reference_text=ref_text,
                        reference_path_span=(
                            path_node.start - ref_start,
                            path_node.end - ref_start,
                        ),
                    )
                )
                return

    opaque.append(OpaqueContributor(module, target, text[node.start:node.end]))


def analyze(root: NixOSRoot) -> ConfigLayout:
    graph = build_import_graph(root.entry_file)
    entry = root.entry_file.resolve()

    contributions: list[_Contribution] = []
    opaque: list[OpaqueContributor] = []
    unparsed: list[tuple[Path, str]] = []

    for key, mod in graph.modules.items():
        path = Path(key)
        if not mod.exists or not _within(path, root):
            continue
        if not mod.parsed or mod.ast is None:
            unparsed.append((path, mod.parse_error))
            continue
        for target in PACKAGE_TARGETS:
            binding = find_package_binding(mod.ast, target)
            if binding is None or not binding.children:
                continue
            _collect(
                binding.children[0], path, mod.text, mod.ast, target, False,
                contributions, opaque, unparsed,
            )

    # -- hubs: non-entry files that aggregate two or more groups -----------
    package_modules = {c.file for c in contributions if c.mechanism is Mechanism.LITERAL}
    hubs = sorted(
        Path(key)
        for key in graph.modules
        if Path(key) != entry and _aggregates(Path(key), contributions, graph, package_modules)
    )
    hub_set = set(hubs)

    # importer lookup for module files
    importers: dict[Path, list[Path]] = {}
    for key, mod in graph.modules.items():
        for imported in mod.imports:
            importers.setdefault(_resolve_import(imported), []).append(Path(key))

    groups: list[DetectedGroup] = []
    used_names: Counter = Counter()

    for c in contributions:
        hub: Path | None = None
        if c.mechanism is Mechanism.LITERAL and c.file == entry:
            style = GroupStyle.INLINE
            mechanism = Mechanism.LITERAL
            declared_in = entry
        elif c.mechanism is Mechanism.LITERAL:
            # The file itself is a NixOS module carrying the group.
            mechanism = Mechanism.NIXOS_MODULE
            parents = importers.get(c.file, [])
            if c.file in hub_set:
                # A hub's own literal list is a group living *in* the hub.
                style, hub, declared_in = GroupStyle.HUB, c.file, c.file
            else:
                hub_parents = [p for p in parents if p in hub_set]
                if hub_parents:
                    style, hub, declared_in = GroupStyle.HUB, hub_parents[0], hub_parents[0]
                else:
                    style = GroupStyle.MODULE
                    declared_in = parents[0] if parents else entry
        elif c.mechanism is Mechanism.LET_BINDING:
            mechanism = Mechanism.LET_BINDING
            declared_in = c.declared_in
            if c.declared_in == entry:
                style = GroupStyle.LET
            elif c.declared_in in hub_set:
                style, hub = GroupStyle.HUB, c.declared_in
            else:
                style = GroupStyle.MODULE
        else:  # LIST_FILE
            mechanism = Mechanism.LIST_FILE
            declared_in = c.declared_in
            if c.declared_in in hub_set:
                style, hub = GroupStyle.HUB, c.declared_in
            else:
                style = GroupStyle.MODULE

        if c.name_hint:
            name = c.name_hint
        elif mechanism is Mechanism.LITERAL:
            name = "main"
        else:
            name = _file_group_name(c.file)
        used_names[name] += 1
        if used_names[name] > 1:
            name = f"{name}-{used_names[name]}"

        assert c.list_node is not None
        packages = [_package_ref(el, c.text, c.with_pkgs) for el in c.list_node.children]
        groups.append(
            DetectedGroup(
                name=name,
                style=style,
                mechanism=mechanism,
                file=c.file,
                target=c.target,
                list_start=c.list_node.start,
                list_end=c.list_node.end,
                with_pkgs=c.with_pkgs,
                packages=packages,
                declared_in=declared_in,
                hub=hub,
                reference_text=c.reference_text,
                reference_path_span=c.reference_path_span,
            )
        )

    return ConfigLayout(
        root=root, graph=graph, groups=groups, opaque=opaque, hubs=hubs, unparsed=unparsed
    )


def _resolve_import(path: Path) -> Path:
    if path.is_dir():
        return (path / "default.nix").resolve()
    return path.resolve()


def _aggregates(
    path: Path,
    contributions: list[_Contribution],
    graph: ImportGraph,
    package_modules: set[Path],
) -> bool:
    via_refs = sum(
        1
        for c in contributions
        if c.declared_in == path and c.mechanism in (Mechanism.LIST_FILE, Mechanism.LET_BINDING)
    )
    mod = graph.modules.get(str(path))
    via_imports = 0
    if mod is not None:
        via_imports = sum(1 for p in mod.imports if _resolve_import(p) in package_modules)
    own = sum(1 for c in contributions if c.file == path and c.mechanism is Mechanism.LITERAL)
    return via_refs + via_imports >= 2 or (via_refs + via_imports >= 1 and own >= 1)


# --------------------------------------------------------------------------
# Style decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleDecision:
    style: GroupStyle | None
    needs_choice: bool
    options: tuple[GroupStyle, ...]
    counts: dict[GroupStyle, int]
    reason: str


def recommend_style(
    layout: ConfigLayout,
    target: tuple[str, ...] = DEFAULT_TARGET,
    *,
    ask_when_mixed: bool = False,
) -> StyleDecision:
    """Pick the style a new group should use.

    * no groups yet           -> ``inline`` (the vanilla layout)
    * one style only          -> that style
    * a strict majority       -> the majority style (unless ``ask_when_mixed``)
    * tie / no majority       -> ``needs_choice`` with options ordered by use
    """
    counts = layout.style_counts(target)
    ordered = tuple(s for s, _ in counts.most_common())
    if not counts:
        return StyleDecision(
            GroupStyle.INLINE, False, (GroupStyle.INLINE,), {},
            "no package groups found; using the vanilla inline layout",
        )
    if len(counts) == 1:
        style = ordered[0]
        return StyleDecision(
            style, False, ordered, dict(counts),
            f"every existing group uses the {style.value} style",
        )
    total = sum(counts.values())
    top, top_n = counts.most_common(1)[0]
    summary = ", ".join(f"{n} {s.value}" for s, n in counts.most_common())
    if top_n * 2 > total and not ask_when_mixed:
        return StyleDecision(
            top, False, ordered, dict(counts),
            f"{top.value} is the majority style ({summary})",
        )
    return StyleDecision(
        None, True, ordered, dict(counts),
        f"groups are declared in mixed styles ({summary}); choose one",
    )


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


@dataclass
class FileEdit:
    path: Path
    new_text: str
    created: bool = False
    old_text: str = ""


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    i = line_start
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[line_start:i]


def _render_list(attrs: Iterable[str], with_pkgs: bool, indent: str) -> str:
    inner = indent + "  "
    body = "".join(f"\n{inner}{_element_text(a, with_pkgs)}" for a in attrs)
    return f"[{body}\n{indent}]"


def _check_name(name: str, layout: ConfigLayout) -> None:
    if not _IDENT_RE.match(name):
        raise LayoutError(
            f"invalid group name {name!r}",
            suggestion="use letters, digits, '_' or '-', starting with a letter",
        )
    if layout.group(name) is not None:
        raise LayoutError(
            f"group {name!r} already exists in {layout.group(name).file}",
            suggestion=f"use `nixorcist layout extend {name} ...` to add packages to it",
        )


def _check_attrs(attrs: list[str]) -> None:
    if not attrs:
        raise LayoutError("no packages given")
    for a in attrs:
        a = a.removeprefix("pkgs.")
        if not all(_IDENT_RE.match(p) for p in a.split(".")):
            raise LayoutError(f"{a!r} is not a plain attribute path")


class _Editor:
    """Accumulates edits per file so several insertions compose correctly."""

    def __init__(self) -> None:
        self.files: dict[Path, FileEdit] = {}

    def text(self, path: Path) -> str:
        if path in self.files:
            return self.files[path].new_text
        return path.read_text("utf-8")

    def write(self, path: Path, new_text: str, *, created: bool = False) -> None:
        if path in self.files:
            self.files[path].new_text = new_text
        else:
            old = "" if created else path.read_text("utf-8")
            self.files[path] = FileEdit(path, new_text, created=created, old_text=old)

    def edits(self) -> list[FileEdit]:
        return list(self.files.values())


def _chain_indent(text: str, binding: Node) -> str:
    """Indentation for a new ``++`` line: reuse the chain's own, if any."""
    value = binding.children[0]
    m = re.search(r"\n([ \t]*)\+\+", text[value.start:value.end])
    if m:
        return m.group(1)
    if "\n" in text[binding.start:value.start]:
        # value starts on its own line (``x =\n    (import ...)``)
        return _line_indent(text, value.start)
    return _line_indent(text, binding.start)


def _append_to_binding(editor: _Editor, path: Path, target: tuple[str, ...], expr: str,
                       comment: str | None = None) -> None:
    text = editor.text(path)
    ast = parse_module(text)
    binding = find_package_binding(ast, target)
    if binding is None:
        _insert_binding(editor, path, target, expr, comment)
        return
    value = binding.children[0]
    indent = _chain_indent(text, binding)
    head = f"\n{indent}# {comment}" if comment else ""
    insertion = f"{head}\n{indent}++ {expr}"
    editor.write(path, text[: value.end] + insertion + text[value.end:])


def _insert_binding(editor: _Editor, path: Path, target: tuple[str, ...], expr: str,
                    comment: str | None = None) -> None:
    text = editor.text(path)
    ast = parse_module(text)
    attrs = module_attrset(ast)
    attrs, _ = _unwrap(attrs)
    if attrs.kind != "attrset":
        raise LayoutError(f"{path} is not a plain attribute set; refusing to guess where to write")
    close = attrs.end - 1
    indent = _line_indent(text, attrs.children[0].start) if attrs.children else _line_indent(text, close) + "  "
    head = f"\n{indent}# {comment}" if comment else ""
    insertion = f"{head}\n{indent}{'.'.join(target)} = {expr};\n"
    # keep the closing brace on its own line
    line_start = text.rfind("\n", attrs.start, close)
    if line_start >= 0 and not text[line_start + 1 : close].strip():
        new_text = text[:line_start] + insertion.rstrip("\n") + text[line_start:]
    else:
        new_text = text[:close] + insertion + text[close:]
    editor.write(path, new_text)


def _add_import(editor: _Editor, path: Path, rel: str) -> None:
    from ..promotion.transformer import insert_into_list

    text = editor.text(path)
    ast = parse_module(text)
    imports = find_package_binding(ast, ("imports",))
    if imports is None:
        _insert_binding(editor, path, ("imports",), f"[ {rel} ]")
        return
    lst = list_of(imports.children[0])
    if lst is None:
        raise LayoutError(f"`imports` in {path} is not a plain list; refusing to guess")
    editor.write(path, insert_into_list(text, lst, [rel]))


def _relpath(from_dir: Path, to: Path) -> str:
    import os

    rel = os.path.relpath(to, from_dir)
    return rel if rel.startswith("..") else f"./{rel}"


def _majority(values: Iterable, default):
    counts = Counter(values)
    return counts.most_common(1)[0][0] if counts else default


def _module_template(attrs: list[str], target: tuple[str, ...]) -> str:
    return (
        "{ pkgs, ... }:\n\n{\n"
        f"  {'.'.join(target)} = with pkgs; {_render_list(attrs, True, '  ')};\n"
        "}\n"
    )


def _strip_leading_comments(text: str) -> str:
    """Drop the sibling's header comment -- it describes the sibling."""
    lines = text.split("\n")
    while lines and (lines[0].strip().startswith("#") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines)


def _clone_with_list(sibling: DetectedGroup, attrs: list[str]) -> str:
    text = sibling.file.read_text("utf-8")
    indent = _line_indent(text, sibling.list_start)
    cloned = text[: sibling.list_start] + _render_list(attrs, sibling.with_pkgs, indent) + text[sibling.list_end:]
    return _strip_leading_comments(cloned)


def _list_file_from_sibling(sibling: DetectedGroup, attrs: list[str]) -> str:
    return _clone_with_list(sibling, attrs)


def _module_from_sibling(sibling: DetectedGroup, attrs: list[str]) -> str | None:
    """Clone a sibling module only if it holds nothing but the package list."""
    text = sibling.file.read_text("utf-8")
    ast = parse_module(text)
    own = [b for b in bindings(ast)]
    if len(own) != 1:
        return None
    return _clone_with_list(sibling, attrs)


def plan_new_group(
    layout: ConfigLayout,
    name: str,
    attrs: list[str],
    style: GroupStyle,
    target: tuple[str, ...] = DEFAULT_TARGET,
) -> list[FileEdit]:
    """Return the file edits that declare a new group in ``style``,
    mirroring how the user's existing groups of that style are written."""
    _check_name(name, layout)
    attrs = [a.removeprefix("pkgs.") for a in attrs]
    _check_attrs(attrs)
    entry = layout.root.entry_file.resolve()
    editor = _Editor()
    siblings = [g for g in layout.groups if g.style is style and g.target == target]

    if style is GroupStyle.INLINE:
        text = editor.text(entry)
        binding = find_package_binding(parse_module(text), target)
        with_pkgs = binding is not None and _unwrap(binding.children[0])[1]
        if binding is None:
            _insert_binding(editor, entry, target, "with pkgs; " + _render_list(attrs, True, "  "), comment=name)
        else:
            indent = _chain_indent(text, binding)
            _append_to_binding(editor, entry, target, _render_list(attrs, with_pkgs, indent), comment=name)

    elif style is GroupStyle.LET:
        _add_let_group(editor, entry, name, attrs, target)

    elif style is GroupStyle.MODULE:
        mechanism = _majority((g.mechanism for g in siblings), Mechanism.NIXOS_MODULE)
        if mechanism is Mechanism.LET_BINDING:
            mechanism = Mechanism.NIXOS_MODULE
        same = [g for g in siblings if g.mechanism is mechanism]
        directory = _majority((g.file.parent for g in same), layout.root.directory / "nixorcist")
        new_file = directory / f"{name}.nix"
        _write_group_file(editor, new_file, same, mechanism, attrs, target)
        _register(editor, entry, new_file, mechanism, same, target)

    elif style is GroupStyle.HUB:
        if not layout.hubs:
            raise LayoutError(
                "no hub file found in this configuration",
                suggestion="pick the module or inline style, or create a hub first",
            )
        hub = _majority((g.hub for g in siblings if g.hub), layout.hubs[0])
        in_hub = [g for g in siblings if g.hub == hub]
        mechanism = _majority((g.mechanism for g in in_hub), Mechanism.NIXOS_MODULE)
        if mechanism is Mechanism.LET_BINDING:
            _add_let_group(editor, hub, name, attrs, target)
        else:
            same = [g for g in in_hub if g.mechanism is mechanism]
            directory = _majority((g.file.parent for g in same if g.file != hub), hub.parent)
            new_file = directory / f"{name}.nix"
            _write_group_file(editor, new_file, same, mechanism, attrs, target)
            _register(editor, hub, new_file, mechanism, same, target)
    else:  # pragma: no cover
        raise LayoutError(f"unknown style {style!r}")

    return editor.edits()


def _write_group_file(editor, new_file: Path, same: list[DetectedGroup], mechanism: Mechanism,
                      attrs: list[str], target: tuple[str, ...]) -> None:
    if new_file.exists():
        raise LayoutError(f"{new_file} already exists")
    if mechanism is Mechanism.LIST_FILE:
        if not same:
            content = "{ pkgs }:\n\nwith pkgs; " + _render_list(attrs, True, "") + "\n"
        else:
            content = _list_file_from_sibling(same[0], attrs)
    else:
        content = None
        for sibling in same:
            content = _module_from_sibling(sibling, attrs)
            if content:
                break
        content = content or _module_template(attrs, target)
    editor.write(new_file, content, created=True)


def _register(editor, aggregator: Path, new_file: Path, mechanism: Mechanism,
              same: list[DetectedGroup], target: tuple[str, ...]) -> None:
    rel = _relpath(aggregator.parent, new_file)
    if mechanism is Mechanism.LIST_FILE:
        ref = next((g for g in same if g.reference_text and g.reference_path_span), None)
        if ref is not None:
            a, b = ref.reference_path_span
            expr = ref.reference_text[:a] + rel + ref.reference_text[b:]
        else:
            expr = f"(import {rel} {{ inherit pkgs; }})"
        _append_to_binding(editor, aggregator, target, expr)
    else:
        _add_import(editor, aggregator, rel)


def _add_let_group(editor: _Editor, path: Path, name: str, attrs: list[str],
                   target: tuple[str, ...]) -> None:
    text = editor.text(path)
    ast = parse_module(text)
    let = _let_node(ast)
    if let is None or len(let.children) < 2:
        raise LayoutError(
            f"{path.name} has no top-level `let` block to add a named group to",
            suggestion="choose the inline or module style instead",
        )
    last = let.children[-2]
    semi = text.find(";", last.end)
    indent = _line_indent(text, last.start)
    binding = f"\n{indent}{name} = with pkgs; {_render_list(attrs, True, indent)};"
    text = text[: semi + 1] + binding + text[semi + 1:]
    editor.write(path, text)
    _append_to_binding(editor, path, target, name)


def plan_extend_group(layout: ConfigLayout, name: str, attrs: list[str]) -> list[FileEdit]:
    """Add packages to an existing group, skipping exact duplicates."""
    group = layout.group(name)
    if group is None:
        known = ", ".join(g.name for g in layout.groups) or "none"
        raise LayoutError(f"group {name!r} not found", suggestion=f"known groups: {known}")
    attrs = [a.removeprefix("pkgs.") for a in attrs]
    _check_attrs(attrs)
    existing = set(group.attrs)
    new = [a for a in attrs if a not in existing]
    if not new:
        return []
    text = group.file.read_text("utf-8")
    # re-parse to get the live list node
    lst = _find_list_at(parse_module(text), group.list_start)
    if lst is None:
        raise LayoutError(f"could not re-locate group {name!r} in {group.file}")
    if lst.children:
        indent = _line_indent(text, lst.children[0].start)
    else:
        indent = _line_indent(text, lst.start) + "  "
    close = lst.end - 1
    insertion = "".join(f"\n{indent}{_element_text(a, group.with_pkgs)}" for a in new)
    line_start = text.rfind("\n", lst.start, close)
    if line_start >= 0 and not text[line_start + 1 : close].strip():
        new_text = text[:line_start] + insertion + text[line_start:]
    else:
        new_text = text[:close].rstrip() + insertion + "\n" + _line_indent(text, lst.start) + text[close:]
    return [FileEdit(group.file, new_text, old_text=text)]


def plan_enable_options(
    layout: ConfigLayout,
    options: list[str],
    *,
    near_group: str | None = None,
    edits: list[FileEdit] | None = None,
) -> list[FileEdit]:
    """Set ``<option> = true;`` for each option not already defined.

    Options go next to the group they belong to when that group is its own
    NixOS module (so ``games.nix`` carries both ``steam`` and
    ``programs.steam.enable``); otherwise into the entry configuration.
    ``edits`` lets this compose with edits planned for the same files.
    """
    editor = _Editor()
    for e in edits or []:
        editor.files[e.path] = e
    entry = layout.root.entry_file.resolve()
    target_file = entry
    if near_group:
        group = layout.group(near_group)
        if group is not None and group.mechanism is Mechanism.NIXOS_MODULE:
            target_file = group.file
        elif group is None:
            # a group being created in this same plan: find its new module file
            for e in edits or []:
                if e.created and e.path.stem == near_group and "systemPackages" in e.new_text:
                    target_file = e.path
    for option in options:
        path = tuple(option.split("."))
        if not all(_IDENT_RE.match(p) for p in path):
            raise LayoutError(f"{option!r} is not a plain option path")
        if _option_defined(layout, path, editor):
            continue
        _insert_binding(editor, target_file, path, "true")
    return editor.edits()


def _option_defined(layout: ConfigLayout, path: tuple[str, ...], editor: _Editor) -> bool:
    files = {Path(k) for k, m in layout.graph.modules.items() if m.parsed}
    files |= set(editor.files)
    for f in files:
        try:
            ast = parse_module(editor.text(f))
        except (OSError, NixParseError):
            continue
        if find_package_binding(ast, path) is not None:
            return True
    return False


def _find_list_at(ast: Node, start: int) -> Node | None:
    for node in ast.walk():
        if node.kind == "list" and node.start == start:
            return node
    return None
