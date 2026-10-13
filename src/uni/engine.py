"""Uni's decision layer: which source handles a package, under which name.

Search fans out to every enabled source and is ranked by the shared nixorcist
search core.  Install without ``-s`` walks sources in priority order and uses
the first one where the name is an exact (default) match; ``--pick`` shows
everything in the fzf picker instead.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from nixorcist.search.core import Candidate, rank

from .config import UniConfig
from .sources import Runner, Source, run_capture, all_sources
from .system import SystemInfo, detect
from .translate import DIALECT_REPO, Op


class UniError(RuntimeError):
    pass


@dataclass
class Planned:
    source: Source
    cmd: list[str]
    packages: list[str]
    note: str = ""

    def argv(self, sudo: bool = True) -> list[str]:
        if sudo and self.source.root and self.cmd[0] not in ("sudo", "doas"):
            return ["sudo", *self.cmd]
        return list(self.cmd)


def map_name(name: str, from_repo: str | None, to_repo: str | None) -> tuple[str, str]:
    """Map a package name between distributions via pkgmap, when installed.

    Returns ``(name, note)``; unchanged when pkgmap is unavailable or has no
    mapping.
    """
    if not from_repo or not to_repo or from_repo == to_repo:
        return name, ""
    try:
        from pkgmap import resolve  # Levizor/pkgmap
    except ImportError:
        return name, ""
    try:
        res = resolve(name, from_repo=from_repo, to_repo=to_repo)
    except Exception:  # offline database, unknown repo slug ...
        return name, ""
    if res and getattr(res, "install_name", None) and res.install_name != name:
        return res.install_name, f"{name} ({from_repo}) -> {res.install_name} ({to_repo}) via pkgmap"
    return name, ""


class Uni:
    def __init__(self, system: SystemInfo | None = None, config: UniConfig | None = None,
                 sources: dict[str, Source] | None = None, runner: Runner = run_capture):
        self.system = system or detect()
        self.config = config or UniConfig.load()
        self.runner = runner
        found = sources if sources is not None else all_sources(self.system.managers)
        self.sources = {
            n: s for n, s in found.items()
            if n not in self.config.disabled and s.kind not in self.config.disabled
        }

    # -- sources --------------------------------------------------------------
    def ordered(self) -> list[Source]:
        return sorted(self.sources.values(), key=lambda s: (self.config.rank_of(s.name, s.kind), s.name))

    def source(self, name: str) -> Source:
        if name == "native":
            native = [s for s in self.ordered() if s.kind == "native"]
            if not native:
                raise UniError("no native package manager detected")
            return native[0]
        if name not in self.sources:
            known = ", ".join(self.sources) or "none"
            raise UniError(f"source {name!r} is not available here (available: {known})")
        return self.sources[name]

    def native(self) -> Source:
        return self.source("native")

    # -- search -----------------------------------------------------------------
    def search(self, query: str, only: str | None = None, limit: int = 60) -> list[Candidate]:
        targets = [self.source(only)] if only else self.ordered()

        def one(src: Source) -> list[Candidate]:
            try:
                return rank(src.search(query, self.runner), query, limit=limit)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            per_source = list(pool.map(one, targets))
        merged: list[Candidate] = []
        for src, hits in zip(targets, per_source):
            prio = self.config.rank_of(src.name, src.kind)
            for c in hits:
                c.extra["priority"] = prio
                merged.append(c)
        # defaults first (in priority order), then by score, priority breaking ties
        merged.sort(key=lambda c: (not c.is_default, c.extra["priority"] if c.is_default else 0,
                                   -c.score, c.extra["priority"], c.name))
        return merged[:limit]

    # -- planning -----------------------------------------------------------------
    def plan(self, op: Op, only: str | None = None, picked: list[Candidate] | None = None) -> list[Planned]:
        if op.action in ("update", "upgrade"):
            targets = [self.source(only)] if only else self.ordered()
            plans = []
            for src in targets:
                cmd = src.refresh() if op.action == "update" else src.upgrade(op.yes)
                if cmd:
                    plans.append(Planned(src, cmd, []))
            return plans

        if op.action in ("install", "remove", "info"):
            if not op.packages and not picked:
                raise UniError(f"{op.action}: no packages given")
            assignments: dict[str, tuple[Source, list[str], list[str]]] = {}

            def assign(src: Source, name: str, note: str = "") -> None:
                entry = assignments.setdefault(src.name, (src, [], []))
                entry[1].append(name)
                if note:
                    entry[2].append(note)

            if picked:
                for c in picked:
                    assign(self.sources[c.source], c.name)
            else:
                for pkg in op.packages:
                    src, name, note = self._locate(pkg, op, only)
                    assign(src, name, note)

            plans = []
            for src, names, notes in assignments.values():
                if op.action == "install":
                    cmd = src.install(names, op.yes)
                elif op.action == "remove":
                    cmd = src.remove(names, op.yes)
                else:
                    for n in names:
                        plans.append(Planned(src, src.info(n), [n], "; ".join(notes)))
                    continue
                plans.append(Planned(src, cmd, names, "; ".join(notes)))
            return plans
        raise UniError(f"cannot plan {op.action!r}")

    def _locate(self, pkg: str, op: Op, only: str | None) -> tuple[Source, str, str]:
        """Where does ``pkg`` come from, and under which name?"""
        foreign = DIALECT_REPO.get(op.dialect)
        local = self.system.repology_repo

        if only:
            src = self.source(only)
            name, note = (map_name(pkg, foreign, local) if src.kind == "native" else (pkg, ""))
            return src, name, note

        if op.action == "remove":
            # removing: the native manager unless told otherwise
            src = self.native()
            name, note = map_name(pkg, foreign, local)
            return src, name, note

        # install: native name mapping first, then priority walk
        mapped, note = map_name(pkg, foreign, local)
        for src in self.ordered():
            query = mapped if src.kind == "native" else pkg
            try:
                hits = rank(src.search(query, self.runner), query, limit=20)
            except Exception:
                continue
            default = next((c for c in hits if c.is_default), None)
            if default is not None:
                return src, default.name, note
        raise UniError(
            f"no source has a package named {pkg!r}",
            )
