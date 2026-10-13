"""uni -- one package-manager interface for every distribution.

Unified syntax:
    uni install <pkg>...      uni remove <pkg>...     uni search <query>
    uni update                uni upgrade             uni info <pkg>
    uni sources               uni system

Native syntax, translated to this system (names mapped via pkgmap if present):
    uni pacman -S bat         uni apt install bat     uni dnf install bat
    uni zypper in bat

Options:
    -s, --source NAME   use one source: native, pacman, aur, apt, dnf, zypper,
                        flatpak, snap, nix
    -p, --pick          choose packages interactively (fzf) across sources
    -y, --yes           don't ask the package manager for confirmation
    -n, --dry-run       print the commands, run nothing
    --json              machine-readable search output
"""

from __future__ import annotations

import json
import subprocess
import sys

from nixorcist.search.picker import pick

from .engine import Uni, UniError
from .translate import TranslateError, parse


def _pop_opt(argv: list[str], *names: str) -> str | None:
    for i, a in enumerate(argv):
        if a in names:
            if i + 1 >= len(argv):
                raise UniError(f"{a} requires a value")
            value = argv[i + 1]
            del argv[i : i + 2]
            return value
        for n in names:
            if n.startswith("--") and a.startswith(n + "="):
                del argv[i]
                return a.split("=", 1)[1]
    return None


def _pop_flag(argv: list[str], *names: str) -> bool:
    hit = False
    for n in names:
        while n in argv:
            argv.remove(n)
            hit = True
    return hit


def main(argv: list[str] | None = None, uni: Uni | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    try:
        only = _pop_opt(argv, "-s", "--source")
        picking = _pop_flag(argv, "-p", "--pick")
        dry = _pop_flag(argv, "-n", "--dry-run")
        as_json = _pop_flag(argv, "--json")
        yes = _pop_flag(argv, "-y", "--yes") if argv and argv[0] not in ("pacman", "paru", "yay", "apt", "apt-get", "dnf", "yum", "zypper") else False
        uni = uni or Uni()

        if argv[0] == "system":
            s = uni.system
            print(f"{s.pretty_name or s.id} (family {s.family}, repology {s.repology_repo})")
            print("managers: " + (", ".join(s.managers) or "none"))
            return 0
        if argv[0] == "sources":
            for src in uni.ordered():
                print(f"{uni.config.rank_of(src.name, src.kind):>4}  {src.name:<8} {src.kind:<8} {src.binary}")
            return 0

        op = parse(argv)
        op.yes = op.yes or yes

        if op.action == "search":
            hits = uni.search(op.query, only=only)
            if as_json:
                print(json.dumps([{"name": c.name, "source": c.source, "version": c.version,
                                   "description": c.description, "default": c.is_default}
                                  for c in hits], indent=2))
                return 0
            if picking:
                chosen = pick(hits, prompt=op.query)
                if chosen:
                    return _execute(uni.plan(_install(op), picked=chosen), dry)
                return 0
            if not hits:
                print(f"nothing found for {op.query!r}")
                return 1
            for c in hits:
                mark = "★" if c.is_default else " "
                print(f"{mark} {c.source:<8} {c.name:<40} {c.version}")
                if c.description:
                    print(f"           {c.description[:100]}")
            return 0

        if op.action in ("install", "remove") and picking:
            chosen = []
            for q in op.packages:
                chosen += pick(uni.search(q, only=only), prompt=q)
            if not chosen:
                return 0
            return _execute(uni.plan(op, picked=chosen), dry)

        if op.action == "list":
            src = uni.source(only or "native")
            listing = {"pacman": ["pacman", "-Q"], "aur": [src.binary, "-Qm"], "apt": ["apt", "list", "--installed"],
                       "dnf": ["dnf", "list", "--installed"], "zypper": ["zypper", "packages", "--installed-only"],
                       "flatpak": ["flatpak", "list", "--app"], "snap": ["snap", "list"], "nix": ["nix", "profile", "list"]}
            return _execute_raw([listing[src.name]], dry)

        return _execute(uni.plan(op, only=only), dry)
    except (UniError, TranslateError, RuntimeError) as exc:
        print(f"uni: {exc}", file=sys.stderr)
        return 1


def _install(op):
    op.action = "install"
    return op


def _execute(plans, dry: bool) -> int:
    for p in plans:
        if p.note:
            print(f"uni: {p.note}", file=sys.stderr)
    return _execute_raw([p.argv() for p in plans], dry)


def _execute_raw(cmds: list[list[str]], dry: bool) -> int:
    status = 0
    for cmd in cmds:
        print("$ " + " ".join(cmd), file=sys.stderr)
        if dry:
            continue
        status = subprocess.call(cmd) or status
    return status


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
