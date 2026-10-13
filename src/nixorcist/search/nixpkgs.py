"""Version-aware nixpkgs index.

The same declaration means different things on different releases:
``pkgs.kate`` is the KDE editor on 24.05 and an error ("renamed to
kdePackages.kate") later on, and ``discord`` resolves to whatever version that
channel ships.  So every search is answered from an index built for *the
release the configuration actually uses*, never from "whatever nixpkgs is on
this machine".

Release detection (:func:`detect_release`), in order:
  explicit override  ->  ``flake.lock`` nixpkgs input  ->  ``<nixpkgs>`` channel
  ->  ``nixos-version``.

Index sources (:func:`build_index`), in order:
  channel metadata from ``channels.nixos.org/<channel>/{packages,options}.json.br``
  (fast, no evaluation) -> local evaluation of the release's nixpkgs
  (``nix-env -qaP --json --meta``) when offline or for a pinned flake revision.

``pkgs/top-level/aliases.nix`` of the release, when a local copy is
available, records renames (``kate -> kdePackages.kate``) so a stale name can be
redirected instead of failing.

Each release gets its own SQLite file under ``<cache>/search/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from .core import Candidate, rank

CHANNEL_URL = "https://channels.nixos.org/{channel}/{name}.json.br"


class SearchError(NixorcistError):
    def __init__(self, message: str, suggestion: str = ""):
        super().__init__(Diagnostic(ErrorCode.DISCOVERY, message, suggestion=suggestion))


# --------------------------------------------------------------------------
# Release detection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    channel: str  # nixos-25.05, nixos-unstable
    version: str  # 25.05, unstable
    rev: str = ""
    source: str = ""
    nixpkgs_path: str | None = None

    @property
    def key(self) -> str:
        return f"{self.channel}-{self.rev[:12]}" if self.rev else self.channel


_REL_RE = re.compile(r"^(\d{2}\.\d{2})")


def release_from_string(value: str, source: str = "override") -> Release:
    value = value.strip()
    if value in ("unstable", "nixos-unstable", "nixpkgs-unstable", "master"):
        return Release("nixos-unstable", "unstable", source=source)
    m = re.search(r"(\d{2}\.\d{2})", value)
    if not m:
        raise SearchError(f"cannot read a NixOS release from {value!r}",
                          suggestion="use e.g. 25.05 or unstable")
    return Release(f"nixos-{m.group(1)}", m.group(1), source=source)


def _from_flake_lock(lock_path: Path) -> Release | None:
    try:
        lock = json.loads(lock_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    nodes = lock.get("nodes", {})
    root = nodes.get(lock.get("root", "root"), {})
    ref = root.get("inputs", {}).get("nixpkgs")
    if isinstance(ref, list):  # follows
        ref = ref[-1]
    node = nodes.get(ref or "nixpkgs")
    if not node:
        return None
    original, locked = node.get("original", {}), node.get("locked", {})
    branch = original.get("ref") or ""
    rev = locked.get("rev", "")
    if branch:
        rel = release_from_string(branch, source="flake.lock")
    else:
        rel = Release("nixos-unstable", "unstable", source="flake.lock")
    return Release(rel.channel, rel.version, rev=rev, source="flake.lock")


def _run(cmd: list[str], timeout: int = 120, env: dict | None = None) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _version_to_release(version: str, source: str, path: str | None = None) -> Release:
    m = _REL_RE.match(version)
    if not m:
        raise SearchError(f"unrecognised nixpkgs version {version!r}")
    if "pre" in version:
        return Release("nixos-unstable", "unstable", source=source, nixpkgs_path=path)
    return Release(f"nixos-{m.group(1)}", m.group(1), source=source, nixpkgs_path=path)


def detect_release(config_dir: Path | None = None, override: str | None = None) -> Release:
    if override:
        return release_from_string(override)
    if config_dir is not None:
        rel = _from_flake_lock(config_dir / "flake.lock")
        if rel is not None:
            return rel
    out = _run(["nix-instantiate", "--eval", "--json", "-E",
                "(import <nixpkgs/lib>).version"], timeout=60)
    if out:
        path = (_run(["nix-instantiate", "--find-file", "nixpkgs"]) or "").strip() or None
        return _version_to_release(json.loads(out), "channel <nixpkgs>", path)
    out = _run(["nixos-version"])
    if out:
        return _version_to_release(out.strip(), "nixos-version")
    raise SearchError(
        "could not determine which NixOS release this configuration uses",
        suggestion="pass --release 25.05 (or unstable)",
    )


# --------------------------------------------------------------------------
# aliases.nix
# --------------------------------------------------------------------------

_ALIAS_LINE = re.compile(r"^\s{2}([A-Za-z_][A-Za-z0-9_'-]*)\s*=\s*(.+?);\s*(?:#.*)?$", re.M)
_QUOTED = re.compile(r"'([A-Za-z_][A-Za-z0-9_.'-]*)'")
_ATTR = re.compile(r"^[A-Za-z_][A-Za-z0-9_'-]*(\.[A-Za-z_][A-Za-z0-9_'-]*)*$")


def parse_aliases(text: str) -> dict[str, str]:
    """``old -> new`` from nixpkgs' ``pkgs/top-level/aliases.nix``.

    Handles plain aliases (``foo = bar;``), ``throw "'foo' has been renamed to
    'bar'"`` and ``warnAlias``/``warnOnInstantiate "..." bar`` forms.  Removals
    without a replacement are skipped.  This file is data, not code, so a
    line-oriented reading is reliable enough and far cheaper than evaluating.
    """
    out: dict[str, str] = {}
    for name, rhs in _ALIAS_LINE.findall(text):
        rhs = rhs.strip()
        if _ATTR.match(rhs) and rhs not in ("null", "true", "false"):
            out[name] = rhs
            continue
        m = re.search(r"(?:warnAlias|warnOnInstantiate)\s+\"[^\"]*\"\s+([A-Za-z_][\w.'-]*)\s*$", rhs)
        if m:
            out[name] = m.group(1)
            continue
        if "renamed" in rhs or "replaced" in rhs or "moved" in rhs:
            quoted = [q for q in _QUOTED.findall(rhs) if q != name]
            if quoted:
                out[name] = quoted[-1]
    return out


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
  attr TEXT PRIMARY KEY, pname TEXT, version TEXT, description TEXT,
  homepage TEXT, main_program TEXT, flags TEXT
);
CREATE TABLE IF NOT EXISTS options (
  name TEXT PRIMARY KEY, type TEXT, description TEXT, default_text TEXT
);
CREATE TABLE IF NOT EXISTS aliases (old TEXT PRIMARY KEY, new TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS packages_pname ON packages(pname);
"""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):  # {"_type": "literalExpression", "text": ...} / mdDoc
        return str(value.get("text", ""))
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return str(value)


class PackageIndex:
    def __init__(self, path: Path | str = ":memory:", release: Release | None = None):
        self.path = path
        self.release = release
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)

    @classmethod
    def for_release(cls, cache_dir: Path, release: Release) -> "PackageIndex":
        return cls(Path(cache_dir) / "search" / f"{release.key}.sqlite", release)

    # -- loading -------------------------------------------------------------
    def load_packages(self, data: dict) -> int:
        """Accepts channel ``packages.json`` (``{"packages": {...}}``) and
        ``nix-env -qaP --json --meta`` output (``{attr: {...}}``)."""
        pkgs = data.get("packages", data) if isinstance(data, dict) else {}
        rows = []
        for attr, info in pkgs.items():
            if attr.startswith("nixos.") or attr.startswith("nixpkgs."):
                attr = attr.split(".", 1)[1]  # nix-env channel prefix
            meta = info.get("meta", {}) or {}
            flags = []
            if meta.get("broken"):
                flags.append("broken")
            if meta.get("insecure") or meta.get("knownVulnerabilities"):
                flags.append("insecure")
            lic = meta.get("license")
            lics = lic if isinstance(lic, list) else [lic]
            if any(isinstance(item, dict) and item.get("free") is False for item in lics):
                flags.append("unfree")
            rows.append((
                attr,
                info.get("pname") or re.sub(r"-[0-9].*$", "", info.get("name", attr)),
                info.get("version", ""),
                _text(meta.get("description")),
                _text(meta.get("homepage")).split(" ")[0],
                meta.get("mainProgram") or "",
                ",".join(flags),
            ))
        self.db.executemany("INSERT OR REPLACE INTO packages VALUES (?,?,?,?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    def load_options(self, data: dict) -> int:
        rows = [
            (name, _text(o.get("type")), _text(o.get("description")), _text(o.get("default")))
            for name, o in data.items()
            if isinstance(o, dict)
        ]
        self.db.executemany("INSERT OR REPLACE INTO options VALUES (?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    def load_aliases(self, mapping: dict[str, str]) -> int:
        self.db.executemany("INSERT OR REPLACE INTO aliases VALUES (?,?)", mapping.items())
        self.db.commit()
        return len(mapping)

    def set_meta(self, **values: str) -> None:
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", values.items())
        self.db.commit()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @property
    def package_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM packages").fetchone()[0]

    @property
    def option_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM options").fetchone()[0]

    # -- queries -------------------------------------------------------------
    def _candidate(self, row) -> Candidate:
        attr, pname, version, desc, home, main_program, flags = row
        return Candidate(
            name=attr, source="nixpkgs", pname=pname or "", version=version or "",
            description=desc or "", homepage=home or "",
            flags=set(filter(None, (flags or "").split(","))),
            extra={"main_program": main_program} if main_program else {},
        )

    def get(self, attr: str) -> Candidate | None:
        row = self.db.execute("SELECT * FROM packages WHERE attr=?", (attr,)).fetchone()
        return self._candidate(row) if row else None

    def alias(self, name: str) -> str | None:
        row = self.db.execute("SELECT new FROM aliases WHERE old=?", (name,)).fetchone()
        return row[0] if row else None

    def resolve_alias(self, name: str, depth: int = 5) -> str | None:
        target = None
        while depth and (nxt := self.alias(target or name)):
            target, depth = nxt, depth - 1
        return target

    def candidates(self, query: str, limit: int = 3000) -> list[Candidate]:
        q = f"%{query.lower()}%"
        rows = self.db.execute(
            "SELECT * FROM packages WHERE lower(attr) LIKE ? OR lower(pname) LIKE ? "
            "OR lower(description) LIKE ? LIMIT ?",
            (q, q, q, limit),
        ).fetchall()
        found = {r[0]: self._candidate(r) for r in rows}
        target = self.resolve_alias(query)
        if target:
            c = found.get(target) or self.get(target)
            if c is not None:
                c.extra["redirect_from"] = query
                c.flags.add("renamed")
                c.note = f"'{query}' was renamed to '{target}' on this release"
                found[target] = c
        return list(found.values())

    def search(self, query: str, *, declared: Iterable[str] = (), limit: int | None = 50) -> list[Candidate]:
        declared = set(declared)
        cands = self.candidates(query)
        for c in cands:
            if c.name in declared:
                c.flags.add("declared")
        return rank(cands, query, limit=limit)

    def options(self, pattern: str = "%") -> list[tuple[str, str, str, str]]:
        return self.db.execute(
            "SELECT name, type, description, default_text FROM options WHERE name LIKE ?", (pattern,)
        ).fetchall()


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def _brotli_decode(blob: bytes) -> bytes:
    try:
        import brotli  # type: ignore

        return brotli.decompress(blob)
    except ImportError:
        pass
    if shutil.which("brotli"):
        proc = subprocess.run(["brotli", "-d", "-c"], input=blob, capture_output=True)
        if proc.returncode == 0:
            return proc.stdout
    raise SearchError("channel metadata is brotli-compressed and no brotli decoder is available",
                      suggestion="install python3Packages.brotli or brotli, or use --source local")


def fetch_channel_json(channel: str, name: str, timeout: int = 120) -> dict:
    url = CHANNEL_URL.format(channel=channel, name=name)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            blob = resp.read()
    except OSError as exc:
        raise SearchError(f"could not download {url}: {exc}") from exc
    return json.loads(_brotli_decode(blob))


def _flake_nixpkgs_path(config_dir: Path) -> str | None:
    out = _run(["nix", "eval", "--raw", "--impure", "--expr",
                f'(builtins.getFlake "path:{config_dir}").inputs.nixpkgs.outPath'], timeout=300)
    return out.strip() if out else None


def eval_local_packages(nixpkgs_path: str) -> dict:
    out = _run(
        ["nix-env", "-f", nixpkgs_path, "-qaP", "--json", "--meta",
         "--arg", "config", "{ allowAliases = false; }"],
        timeout=1800,
        env={"NIXPKGS_ALLOW_UNFREE": "1", "NIXPKGS_ALLOW_INSECURE": "1"},
    )
    if out is None:
        raise SearchError(f"nix-env could not list packages from {nixpkgs_path}")
    return json.loads(out)


def build_index(
    index: PackageIndex,
    release: Release,
    *,
    source: str = "auto",
    config_dir: Path | None = None,
) -> str:
    """Fill ``index`` for ``release``; returns a short description of the source used."""
    nixpkgs_path = release.nixpkgs_path
    if nixpkgs_path is None and release.source == "flake.lock" and config_dir is not None:
        nixpkgs_path = _flake_nixpkgs_path(config_dir)

    used = None
    # A pinned flake rev is matched exactly by a local eval; channel metadata
    # tracks the channel head, which is close but not identical.
    order = {"auto": ["channel", "local"], "channel": ["channel"], "local": ["local"]}[source]
    if release.rev and nixpkgs_path and source == "auto":
        order = ["local", "channel"]
    errors = []
    for kind in order:
        try:
            if kind == "channel":
                index.load_packages(fetch_channel_json(release.channel, "packages"))
                try:
                    index.load_options(fetch_channel_json(release.channel, "options"))
                except SearchError as exc:
                    errors.append(str(exc))
                used = f"channel metadata ({release.channel})"
            else:
                if not nixpkgs_path:
                    raise SearchError("no local nixpkgs for this release")
                index.load_packages(eval_local_packages(nixpkgs_path))
                opts = Path(nixpkgs_path)  # options come from the NixOS manual build
                built = _run(["nix-build", str(opts / "nixos/release.nix"), "-A", "options",
                              "--no-out-link"], timeout=1800)
                if built:
                    path = Path(built.strip()) / "share/doc/nixos/options.json"
                    if path.is_file():
                        index.load_options(json.loads(path.read_text("utf-8")))
                used = f"local evaluation ({nixpkgs_path})"
            break
        except SearchError as exc:
            errors.append(str(exc))
    if used is None:
        raise SearchError("could not build the package index: " + "; ".join(errors),
                          suggestion="check network access or pass --source local")
    if nixpkgs_path:
        aliases = Path(nixpkgs_path) / "pkgs/top-level/aliases.nix"
        if aliases.is_file():
            index.load_aliases(parse_aliases(aliases.read_text("utf-8")))
    index.set_meta(release=release.channel, rev=release.rev, source=used)
    return used
