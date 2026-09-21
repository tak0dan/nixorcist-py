"""Promotion transformer: apply a :class:`PromotionPlan` to a *copied*
configuration tree by editing parsed Nix structure (never regex)."""

from __future__ import annotations

from pathlib import Path

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..nix.ast import find_binding, list_of, parse_module, module_attrset

from .planner import PromotionPlan


class TransformError(NixorcistError):
    pass


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    i = line_start
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[line_start:i]


def insert_into_list(text: str, list_node, snippets: list[str]) -> str:
    close = list_node.end - 1
    # Prefer the indentation of an existing element; an empty list gets one
    # level deeper than the list's own line.  Keeping the closing bracket on
    # its original line is important both for readable candidates and for
    # minimal, reviewable configuration diffs.
    if list_node.children:
        indent = _line_indent(text, list_node.children[0].start)
    else:
        indent = _line_indent(text, list_node.start) + "  "

    line_start = text.rfind("\n", list_node.start, close)
    closing_indent = _line_indent(text, close)
    if line_start >= 0 and text[line_start + 1 : close].strip() == "":
        insertion = "".join(f"\n{indent}{snippet}" for snippet in snippets)
        return text[:line_start] + insertion + f"\n{closing_indent}" + text[close:]

    insertion = "".join(f"\n{indent}{snippet}" for snippet in snippets)
    # Inline lists have no dedicated closing-bracket line. Convert just that
    # local fragment to multiline form rather than attaching ``]`` to the
    # final generated element.
    return text[:close] + insertion + f"\n{closing_indent}" + text[close:]


def _normalise_element(value: str) -> str:
    value = value.strip()
    if value.startswith("pkgs."):
        value = value[len("pkgs."):].strip()
    return value.strip("\"'")


def remove_from_list(text: str, list_node, targets: set[str]) -> tuple[str, int]:
    """Return ``(new_text, count)`` with list elements matching ``targets``
    removed.  Elements match by attribute name (optional ``pkgs.`` prefix)."""
    children = list(list_node.children)
    segments: list[str] = []
    start = list_node.start + 1
    removed = 0
    for child in children:
        if _normalise_element(child.value) in targets:
            removed += 1
            continue
        segments.append(text[start:child.end])
        start = child.end
    segments.append(text[start:list_node.end - 1])
    return text[: list_node.start + 1] + "".join(segments) + text[list_node.end - 1:], removed


def insert_binding_before_close(text: str, attrset_node, bind_text: str) -> str:
    close = attrset_node.end - 1
    indent = _line_indent(text, close)
    insertion = f"\n{indent}{bind_text}"
    return text[:close] + insertion + text[close:]


def _rel_path(root_dir: Path, path: Path) -> Path:
    try:
        return path.relative_to(root_dir)
    except ValueError:
        return path


def apply_plan(tree: Path, root_dir: Path, plan: PromotionPlan) -> list[str]:
    """Mutate the copied tree; return relative paths of changed files."""
    changed: list[str] = []

    def edit_file(rel: Path, edit_fn) -> None:
        target = tree / rel
        try:
            text = target.read_text("utf-8")
        except OSError as exc:
            raise TransformError(
                Diagnostic(ErrorCode.PROMOTION, f"could not read {target}: {exc}")
            ) from exc
        new_text = edit_fn(text)
        try:
            target.write_text(new_text, "utf-8")
        except OSError as exc:
            raise TransformError(
                Diagnostic(ErrorCode.PROMOTION, f"could not write {target}: {exc}")
            ) from exc
        changed.append(str(rel))

    if plan.strategy in ("EXTEND",):
        rel = _rel_path(root_dir, plan.target_module)
        attrpath = plan.attrpath

        def extend(text: str) -> str:
            ast = parse_module(text)
            binding = find_binding(ast, attrpath)
            if binding is None:
                raise TransformError(
                    Diagnostic(
                        ErrorCode.PROMOTION,
                        f"binding {'.'.join(attrpath)} disappeared while planning",
                    )
                )
            pkg_list = list_of(binding.children[0])
            if pkg_list is None:
                raise TransformError(
                    Diagnostic(
                        ErrorCode.PROMOTION,
                        f"value of {'.'.join(attrpath)} is not a plain list; refusing to guess",
                    )
                )
            return insert_into_list(text, pkg_list, plan.element_snippets)

        edit_file(rel, extend)

    elif plan.strategy == "EMBED":
        rel = _rel_path(root_dir, plan.target_module)
        snippets = "\n".join(f"    {s}" for s in plan.element_snippets)
        bind_text = (
            "environment.systemPackages = with pkgs; [\n"
            f"{snippets}\n"
            "  ];"
        )

        def embed(text: str) -> str:
            ast = parse_module(text)
            root_attrs = module_attrset(ast)
            if root_attrs.kind != "attrset":
                raise TransformError(
                    Diagnostic(ErrorCode.PROMOTION, f"{rel} is not a plain attribute set")
                )
            return insert_binding_before_close(text, root_attrs, bind_text)

        edit_file(rel, embed)

    elif plan.strategy == "CREATE_MODULE":
        new_path = tree / plan.new_module_path
        new_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            new_path.write_text(plan.new_module_content or "", "utf-8")
        except OSError as exc:
            raise TransformError(
                Diagnostic(ErrorCode.PROMOTION, f"could not write {new_path}: {exc}")
            ) from exc
        changed.append(str(plan.new_module_path))

        rel = _rel_path(root_dir, plan.target_module)

        def add_import(text: str) -> str:
            ast = parse_module(text)
            imports_binding = find_binding(ast, ["imports"])
            if imports_binding is None:
                raise TransformError(
                    Diagnostic(ErrorCode.PROMOTION, "root configuration has no imports list")
                )
            imports_list = list_of(imports_binding.children[0])
            if imports_list is None:
                raise TransformError(
                    Diagnostic(ErrorCode.PROMOTION, "root imports is not a plain list")
                )
            snippet = "./" + str(plan.new_module_path)
            return insert_into_list(text, imports_list, [snippet])

        edit_file(rel, add_import)

    elif plan.strategy == "REMOVE_ELEMENTS":
        for rel_str, attrpath in plan.removal_targets:
            rel = Path(rel_str)

            def strip_list(text: str, attrpath=attrpath) -> str:
                ast = parse_module(text)
                binding = find_binding(ast, attrpath)
                if binding is None:
                    return text
                pkg_list = list_of(binding.children[0])
                if pkg_list is None:
                    return text
                new_text, count = remove_from_list(text, pkg_list, set(plan.remove_elements))
                if count == 0:
                    return text
                return new_text

            try:
                original = (tree / rel).read_text("utf-8")
            except OSError:
                continue
            updated = strip_list(original)
            if updated != original:
                (tree / rel).write_text(updated, "utf-8")
                changed.append(str(rel))

    elif plan.strategy == "REMOVE_MODULE":
        module_path = tree / plan.remove_module_rel
        try:
            module_path.unlink()
            changed.append(str(plan.remove_module_rel))
        except FileNotFoundError:
            changed.append(str(plan.remove_module_rel))

        rel = _rel_path(root_dir, plan.target_module)

        def drop_import(text: str) -> str:
            ast = parse_module(text)
            imports_binding = find_binding(ast, ["imports"])
            if imports_binding is None:
                return text
            imports_list = list_of(imports_binding.children[0])
            if imports_list is None:
                return text
            target_text = "./" + plan.remove_module_rel.as_posix()
            candidates = {target_text, target_text.rsplit("/", 1)[-1] if "/" in target_text else target_text}
            new_text, _ = remove_from_list(text, imports_list, candidates)
            return new_text

        try:
            original = (tree / rel).read_text("utf-8")
        except OSError:
            original = None
        if original is not None:
            updated = drop_import(original)
            if updated != original:
                (tree / rel).write_text(updated, "utf-8")
                changed.append(str(rel))

    else:  # pragma: no cover
        raise TransformError(
            Diagnostic(ErrorCode.PROMOTION, f"unknown promotion strategy {plan.strategy!r}")
        )

    return changed
