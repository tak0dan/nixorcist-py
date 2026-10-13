"""``nixorcist layout`` -- inspect and write package groups in the user's own style.

    nixorcist layout [show] [--json]
    nixorcist layout where <attr>...
    nixorcist layout add <group> <name>... [--style S] [--ask] [--check LEVEL]
                                         [--pick] [--with-services] [--raw]
                                         [--release R] [--source S]
    nixorcist layout extend <group> <name>... [same flags, minus --style/--ask]

Names are resolved against the package index of the release the configuration
uses: ``kate`` becomes ``kdePackages.kate`` where it moved, and a name that does
not exist without a single obvious replacement is an error.

``--pick``           choose from the ranked candidates (fzf) instead of the default
``--with-services``  also enable the NixOS options a package needs
                     (programs.steam.enable, virtualisation.libvirtd.enable, ...);
                     without it you are asked when a terminal is attached
``--raw``            use the names exactly as typed (no index)

``--style``  inline | let | module | hub (default: the configuration's majority
             style; when there is no clear majority you are asked)
``--ask``    always ask when the configuration mixes styles
``--check``  none | parse | eval | build (default: parse)

With the global ``--dry-run`` the diff is printed and nothing is written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Sequence

from ..nix.config import discover_root
from ..nix.layout import (
    GroupStyle,
    StyleDecision,
    analyze,
    plan_extend_group,
    plan_new_group,
    recommend_style,
)
from ..nix.layout_apply import CHECK_LEVELS, apply_edits, render_diff
from .diagnostics import Diagnostic, ErrorCode, NixorcistError, Reporter
from ..nix.layout import plan_enable_options

_STYLE_HELP = {
    GroupStyle.INLINE: "another list in configuration.nix's systemPackages",
    GroupStyle.LET: "a named list in configuration.nix's let block",
    GroupStyle.MODULE: "its own file, imported from configuration.nix",
    GroupStyle.HUB: "its own file, registered in the hub file",
}


def _err(message: str, suggestion: str = "") -> NixorcistError:
    return NixorcistError(Diagnostic(ErrorCode.SYNTAX, message, suggestion=suggestion))


def _take(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 >= len(argv):
            raise _err(f"{flag} requires a value")
        value = argv[i + 1]
        del argv[i : i + 2]
        return value
    return None


def _flag(argv: list[str], flag: str) -> bool:
    if flag in argv:
        argv.remove(flag)
        return True
    return False


def choose_style(decision: StyleDecision, *, has_hub: bool = True) -> GroupStyle:
    """Ask which style to use: fzf when available, a numbered prompt otherwise."""
    options = list(decision.options) + [s for s in GroupStyle if s not in decision.options]
    if not has_hub:
        options = [s for s in options if s is not GroupStyle.HUB]
    lines = [
        f"{s.value:<7} {decision.counts.get(s, 0)} group(s) — {_STYLE_HELP[s]}" for s in options
    ]
    if not sys.stdin.isatty():
        raise _err(
            decision.reason,
            suggestion="pass --style " + "|".join(s.value for s in decision.options),
        )
    if shutil.which("fzf"):
        proc = subprocess.run(
            ["fzf", "--prompt", "group style> ", "--header", decision.reason, "--height", "40%", "--reverse"],
            input="\n".join(lines), capture_output=False, stdout=subprocess.PIPE, text=True,
        )
        picked = proc.stdout.strip().split(" ", 1)[0]
        if proc.returncode != 0 or not picked:
            raise _err("no style selected")
        return GroupStyle(picked)
    print(decision.reason, file=sys.stderr)
    for i, line in enumerate(lines, 1):
        print(f"  {i}) {line}", file=sys.stderr)
    answer = input("style [1]: ").strip() or "1"
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError):
        try:
            return GroupStyle(answer)
        except ValueError:
            raise _err(f"unknown choice {answer!r}") from None


def _print_layout(layout) -> None:
    root = layout.root
    print(f"configuration root: {root.directory} (entry {root.entry_file.name}, flake={root.is_flake})")
    if layout.hubs:
        print("hubs: " + ", ".join(str(h.relative_to(root.directory)) for h in layout.hubs))
    decision = recommend_style(layout)
    counts = ", ".join(f"{n} {s.value}" for s, n in layout.style_counts().most_common()) or "none"
    print(f"groups: {counts}")
    print(f"new groups: {decision.style.value if decision.style else 'ask'} — {decision.reason}")
    print()
    width = max((len(g.name) for g in layout.groups), default=4)
    for g in layout.groups:
        rel = g.file.relative_to(root.directory) if root.directory in g.file.parents else g.file
        print(f"  {g.name:<{width}}  {g.style.value:<6} {g.mechanism.value:<12} {rel}")
        shown = ", ".join(p.text.strip() for p in g.packages)
        print(f"  {'':<{width}}  {shown or '(empty)'}")
    for o in layout.opaque:
        print(f"  (opaque) {o.file.name}: {o.text.strip()[:60]}")
    for path, error in layout.unparsed:
        print(f"  (unparsed) {path}: {error}")


def _resolve(queries, layout, config_root, *, pick, with_services, release, source):
    """Resolve typed names to attributes of the configured release, and collect
    the NixOS options to enable alongside them."""
    from ..search.core import Candidate
    from ..search.picker import pick as interactive_pick
    from ..search.resolve import attach_services, resolve_query
    from .search_cmd import open_index

    try:
        index = open_index(config_root, release=release, source=source)
    except NixorcistError as exc:
        print(f"warning: package index unavailable ({exc.diagnostic.message}); "
              "using names as typed", file=sys.stderr)
        return [q.removeprefix("pkgs.") for q in queries], []

    attrs: list[str] = []
    options: list[str] = []
    declared = layout.declared_attrs()
    for query in queries:
        res = attach_services(index, resolve_query(index, query, pick=pick, declared=declared))
        if res.note:
            print(f"note: {res.note}", file=sys.stderr)
        attrs.extend(a for a in res.attrs if a not in attrs)
        for attr, links in res.services.items():
            if with_services:
                options.extend(l.option for l in links if l.option not in options)
                continue
            if sys.stdin.isatty():
                choices = [Candidate(name=l.option, source="nixos option", description=l.reason)
                           for l in links]
                header = f"{attr} has NixOS options; select the ones to enable (Esc = none)"
                for c in interactive_pick(choices, prompt="enable", header=header,
                                              empty_means_none=True):
                    if c.name not in options:
                        options.append(c.name)
            else:
                hint = ", ".join(l.option for l in links)
                print(f"hint: {attr} has NixOS options: {hint} (use --with-services to enable)",
                      file=sys.stderr)
    return attrs, options


def cmd_layout(argv: Sequence[str], config_root, dry_run: bool) -> int:
    reporter = Reporter()
    argv = list(argv)
    try:
        as_json = _flag(argv, "--json")
        ask = _flag(argv, "--ask")
        style_arg = _take(argv, "--style")
        check = _take(argv, "--check") or "parse"
        raw = _flag(argv, "--raw")
        pick_mode = _flag(argv, "--pick")
        with_services = _flag(argv, "--with-services")
        release = _take(argv, "--release")
        source = _take(argv, "--source") or "auto"
        if check not in CHECK_LEVELS:
            raise _err(f"unknown --check level {check!r}", suggestion="|".join(CHECK_LEVELS))

        root = discover_root(config_root, dry_run=dry_run)
        layout = analyze(root)
        action = argv.pop(0) if argv else "show"

        if action == "show":
            if as_json:
                print(json.dumps(layout.to_dict(), indent=2))
            else:
                _print_layout(layout)
            return 0

        if action == "where":
            if not argv:
                raise _err("usage: nixorcist layout where <attr>...")
            status = 0
            for attr in argv:
                attr = attr.removeprefix("pkgs.")
                hits = layout.where_declared(attr)
                if hits:
                    for g in hits:
                        print(f"{attr}: group {g.name} ({g.style.value}) in {g.file}")
                else:
                    print(f"{attr}: not declared")
                    status = 1
            return status

        if action in ("add", "extend"):
            if len(argv) < 2:
                raise _err(f"usage: nixorcist layout {action} <group> <attr>...")
            name, attrs = argv[0], argv[1:]
            options: list[str] = []
            if not raw:
                attrs, options = _resolve(
                    attrs, layout, config_root,
                    pick=pick_mode, with_services=with_services, release=release, source=source,
                )
            if action == "add":
                if style_arg:
                    try:
                        style = GroupStyle(style_arg)
                    except ValueError:
                        raise _err(
                            f"unknown style {style_arg!r}",
                            suggestion="|".join(s.value for s in GroupStyle),
                        ) from None
                else:
                    decision = recommend_style(layout, ask_when_mixed=ask)
                    if decision.style:
                        style = decision.style
                        print(f"style: {style.value} ({decision.reason})", file=sys.stderr)
                    else:
                        style = choose_style(decision, has_hub=bool(layout.hubs))
                        print(f"style: {style.value}", file=sys.stderr)
                edits = plan_new_group(layout, name, attrs, style)
            else:
                edits = plan_extend_group(layout, name, attrs)
            if options:
                edits = plan_enable_options(layout, options, near_group=name, edits=edits)
            if not edits:
                print(f"group {name} already declares all of: {' '.join(attrs)}")
                return 0

            print(render_diff(root, edits), end="")
            result = apply_edits(
                root, edits, check="none" if dry_run else check,
                new_attrs=[a.removeprefix("pkgs.") for a in attrs], commit=not dry_run,
            )
            for warning in result.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if result.committed:
                checks = ", ".join(result.checks) or "none"
                print(f"wrote {', '.join(result.changed)} (checks: {checks})", file=sys.stderr)
            else:
                print("dry run: nothing written", file=sys.stderr)
            return 0

        raise _err(f"unknown layout action {action!r}", suggestion="show | where | add | extend")
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1
