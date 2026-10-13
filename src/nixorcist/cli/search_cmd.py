"""``nixorcist search`` -- version-aware package search.

    nixorcist search <query> [--release R] [--source auto|channel|local]
                             [--limit N] [--json] [--services] [--update]

Results come from an index of the release *your configuration* uses (see
:mod:`nixorcist.search.nixpkgs`).  ★ marks what a plain ``<query>``
declaration gets on that release.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

from ..search.nixpkgs import PackageIndex, Release, build_index, detect_release
from ..search.services import services_for
from ..storage.paths import Paths
from .diagnostics import Diagnostic, ErrorCode, NixorcistError, Reporter


def _err(message: str, suggestion: str = "") -> NixorcistError:
    return NixorcistError(Diagnostic(ErrorCode.SYNTAX, message, suggestion=suggestion))


def take(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 >= len(argv):
            raise _err(f"{flag} requires a value")
        value = argv[i + 1]
        del argv[i : i + 2]
        return value
    return None


def flag(argv: list[str], name: str) -> bool:
    if name in argv:
        argv.remove(name)
        return True
    return False


def config_dir(config_root) -> Path | None:
    from ..nix.config import discover_root

    try:
        return discover_root(config_root).directory
    except NixorcistError:
        return None


def open_index(
    config_root=None,
    *,
    release: str | None = None,
    source: str = "auto",
    update: bool = False,
    log=lambda msg: print(msg, file=sys.stderr),
) -> PackageIndex:
    directory = config_dir(config_root)
    rel: Release = detect_release(directory, release)
    index = PackageIndex.for_release(Paths.from_env().ensure().cache_dir, rel)
    if update or index.package_count == 0:
        log(f"building package index for {rel.channel} (from {rel.source}) ...")
        used = build_index(index, rel, source=source, config_dir=directory)
        log(f"indexed {index.package_count} packages, {index.option_count} options via {used}")
    return index


def cmd_search(argv: Sequence[str], config_root) -> int:
    reporter = Reporter()
    argv = list(argv)
    try:
        release = take(argv, "--release")
        source = take(argv, "--source") or "auto"
        limit = int(take(argv, "--limit") or 25)
        as_json = flag(argv, "--json")
        show_services = flag(argv, "--services")
        update = flag(argv, "--update")
        if not argv and not update:
            raise _err("usage: nixorcist search <query>")
        index = open_index(config_root, release=release, source=source, update=update)
        if not argv:
            return 0
        query = " ".join(argv)

        declared: set[str] = set()
        try:
            from ..nix.config import discover_root
            from ..nix.layout import analyze

            declared = analyze(discover_root(config_root)).declared_attrs()
        except NixorcistError:
            pass

        ranked = index.search(query, declared=declared, limit=limit)
        if as_json:
            out = []
            for c in ranked:
                item = {"attr": c.name, "pname": c.pname, "version": c.version,
                        "description": c.description, "default": c.is_default,
                        "flags": sorted(c.flags), "note": c.note, "family": c.family}
                if show_services:
                    item["services"] = [
                        {"option": l.option, "reason": l.reason, "installs_package": l.installs_package}
                        for l in services_for(index, c).recommended
                    ]
                out.append(item)
            print(json.dumps({"release": index.release.channel, "results": out}, indent=2))
            return 0

        print(f"# {index.release.channel}  ({index.meta('source') or 'index'})")
        if not ranked:
            print(f"no package matches {query!r}")
            return 1
        for c in ranked:
            mark = "★" if c.is_default else " "
            flags = ",".join(sorted(c.flags))
            print(f"{mark} {c.name:<36} {c.version:<14} {flags}")
            if c.description:
                print(f"    {c.description[:110]}")
            if c.note:
                print(f"    note: {c.note}")
            if show_services:
                for link in services_for(index, c).recommended:
                    extra = " (also installs it)" if link.installs_package else ""
                    print(f"    service: {link.option}{extra} — {link.reason}")
        if not any(c.is_default for c in ranked):
            print(f"\nno default: {query!r} is not a package name on {index.release.channel}")
        return 0
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1
    except ValueError as exc:
        reporter.report(Diagnostic(ErrorCode.SYNTAX, str(exc)))
        return 1
