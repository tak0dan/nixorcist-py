"""fzf-style selection, shared by nixorcist and uni.

Uses ``fzf`` when it is installed (multi-select, description preview) and a
numbered prompt otherwise.  Non-interactive callers never reach here: they get
the default candidate or an error asking them to be explicit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .core import Candidate


def format_line(c: Candidate) -> str:
    mark = "★" if c.is_default else " "
    flags = ",".join(sorted(f for f in c.flags if f != "declared"))
    declared = " [declared]" if "declared" in c.flags else ""
    version = c.version or "-"
    tail = f" ({flags})" if flags else ""
    note = f" — {c.note}" if c.note else ""
    return f"{mark} {c.name}\t{version}\t{c.source}{declared}{tail}\t{c.description}{note}"


def describe(c: Candidate) -> str:
    parts = [f"{c.name} {c.version}".strip(), c.description]
    if c.homepage:
        parts.append(c.homepage)
    if c.note:
        parts.append(c.note)
    if c.flags:
        parts.append("flags: " + ", ".join(sorted(c.flags)))
    return "\n".join(p for p in parts if p)


def pick(candidates: list[Candidate], *, prompt: str = "package", multi: bool = True,
         header: str = "", empty_means_none: bool = False) -> list[Candidate]:
    if not candidates:
        return []
    if not sys.stdin.isatty():
        raise RuntimeError("interactive selection needs a terminal")
    lines = [format_line(c) for c in candidates]
    by_name = {c.name: c for c in candidates}
    if shutil.which("fzf"):
        cmd = ["fzf", "--delimiter", "\t", "--prompt", f"{prompt}> ", "--height", "60%",
               "--reverse", "--header", header or "★ = what a plain declaration gets you",
               "--preview", "echo {4}", "--preview-window", "down,3,wrap"]
        if multi:
            cmd.append("--multi")
        proc = subprocess.run(cmd, input="\n".join(lines), stdout=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return []
        picked = []
        for line in proc.stdout.splitlines():
            name = line[2:].split("\t", 1)[0]
            if name in by_name:
                picked.append(by_name[name])
        return picked
    if header:
        print(header, file=sys.stderr)
    for i, line in enumerate(lines, 1):
        name, version, source, desc = (line.split("\t") + ["", "", ""])[:4]
        print(f"{i:>3}) {name}  {version}  {source}\n       {desc[:100]}", file=sys.stderr)
    default = next((i for i, c in enumerate(candidates, 1) if c.is_default), 1)
    hint = "numbers separated by spaces" if multi else "a number"
    if empty_means_none:
        answer = input(f"{prompt} ({hint}, empty = none): ").strip()
        if not answer:
            return []
    else:
        answer = input(f"{prompt} [{default}] ({hint}, empty = default): ").strip()
        if not answer:
            return [candidates[default - 1]]
    picked = []
    for token in answer.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(candidates):
            picked.append(candidates[int(token) - 1])
    return picked if multi else picked[:1]
