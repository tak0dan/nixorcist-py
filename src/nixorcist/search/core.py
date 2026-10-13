"""Distribution-agnostic search core.

Every backend (nixpkgs, pacman, apt, dnf, flatpak, ...) turns its own data into
:class:`Candidate` objects; this module ranks them, groups variants into
families and picks the *default* -- the thing a new user gets when they type
just ``discord``.  Nothing here knows about Nix; that is what lets ``uni``
reuse the exact same engine on imperative distributions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Candidate:
    name: str  # install name: nix attr path, pacman/apt package name, flatpak app id
    source: str  # "nixpkgs", "pacman", "apt", "flatpak", ...
    pname: str = ""  # upstream-ish name used for family grouping
    version: str = ""
    description: str = ""
    homepage: str = ""
    flags: set[str] = field(default_factory=set)  # broken, insecure, unfree, installed, declared, renamed
    note: str = ""  # e.g. "renamed from kate in 25.11"
    score: float = 0.0
    is_default: bool = False
    family: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def leaf(self) -> str:
        return self.name.rsplit(".", 1)[-1]


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def words(value: str) -> set[str]:
    return set(_WORD.findall(value.lower()))


# Scopes whose members are rarely what someone typing a bare name wants.
_NOISY_SCOPES = (
    "python", "perl", "haskell", "ocaml", "lua", "nodePackages", "rPackages",
    "emacsPackages", "vimPlugins", "texlive", "chickenPackages", "coqPackages",
    "beamPackages", "idrisPackages", "akkuPackages", "rubyPackages", "php",
    "elmPackages", "linuxKernel", "linuxPackages", "tests",
)


def score(c: Candidate, query: str) -> float:
    q = query.strip()
    qn = norm(q)
    name_n, leaf_n, pname_n = norm(c.name), norm(c.leaf), norm(c.pname or c.leaf)
    s = 0.0
    if c.name == q:
        s = 100
    elif "." not in q and c.leaf == q:
        s = 88  # kdePackages.kate for "kate"
    elif pname_n == qn:
        s = 80
    elif leaf_n.startswith(qn) or name_n.startswith(qn):
        s = 62 - min(len(leaf_n) - len(qn), 20) * 0.5  # discord-ptb, discord-canary
    elif qn and qn in name_n:
        s = 45
    elif qn and qn in pname_n:
        s = 42
    else:
        qw = words(q)
        dw = words(c.description)
        if qw and qw <= dw:
            s = 25
        elif qw & dw:
            s = 12
    if s <= 0:
        return 0.0
    if "." in c.name and "." not in q and c.name.split(".", 1)[0].startswith(_NOISY_SCOPES):
        s -= 25
    if "broken" in c.flags:
        s -= 40
    if "insecure" in c.flags:
        s -= 30
    if "declared" in c.flags:
        s += 15
    if "installed" in c.flags:
        s += 5
    return max(s, 0.1)


def family_of(c: Candidate) -> str:
    """``discord-ptb`` and ``discord-canary`` belong to the ``discord`` family."""
    base = norm((c.pname or c.leaf).split("-", 1)[0])
    return base or norm(c.leaf)


def rank(candidates: Iterable[Candidate], query: str, limit: int | None = 50) -> list[Candidate]:
    """Score, mark the default and order: default first, then by score."""
    hits: list[Candidate] = []
    for c in candidates:
        c.score = score(c, query)
        if c.score > 0:
            c.family = family_of(c)
            hits.append(c)
    hits.sort(key=lambda c: (-c.score, len(c.name), c.name))
    mark_default(hits, query)
    hits.sort(key=lambda c: (not c.is_default, -c.score, len(c.name), c.name))
    return hits[:limit] if limit else hits


def mark_default(ranked: list[Candidate], query: str) -> Candidate | None:
    """What a plain ``query`` would get you.

    Exact name first; otherwise an unambiguous nested match (``kdePackages.kate``
    when the release has no top-level ``kate``); otherwise the backend's own
    redirect (a rename recorded as ``extra["redirect_from"]``).  Ambiguous
    matches get no default -- the user must pick.
    """
    for c in ranked:
        c.is_default = False
    exact = [c for c in ranked if c.name == query and "broken" not in c.flags]
    if exact:
        exact[0].is_default = True
        return exact[0]
    redirected = [c for c in ranked if c.extra.get("redirect_from") == query]
    if len(redirected) == 1:
        redirected[0].is_default = True
        return redirected[0]
    leaf = [
        c for c in ranked
        if c.leaf == query and "broken" not in c.flags and c.score >= 70
    ]
    if len(leaf) == 1:
        leaf[0].is_default = True
        return leaf[0]
    # Sources whose install name is an ID (flatpak: com.discordapp.Discord):
    # one unambiguous match on the package's own name.
    qn = norm(query)
    named = [c for c in ranked if qn and norm(c.pname) == qn and "broken" not in c.flags]
    if len(named) == 1:
        named[0].is_default = True
        return named[0]
    return None


def variants(ranked: list[Candidate], of: Candidate) -> list[Candidate]:
    return [c for c in ranked if c is not of and c.family == of.family]
