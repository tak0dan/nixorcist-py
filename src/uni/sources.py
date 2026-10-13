"""Package sources (backends).

Each source knows how to *build* the native command for an operation and how
to *parse* its search output into :class:`~nixorcist.search.core.Candidate`.
Building and running are kept apart so ``--dry-run`` and tests never touch
the system, and so the parsers can be tested against captured output.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from nixorcist.search.core import Candidate

Runner = Callable[[list[str]], str]


def run_capture(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return proc.stdout


@dataclass
class Source:
    name: str  # pacman, aur, apt, dnf, zypper, flatpak, snap, nix
    kind: str  # native | aur | flatpak | snap | nix
    binary: str
    root: bool = True  # needs sudo for changes

    # -- command construction ---------------------------------------------
    def install(self, pkgs: list[str], yes: bool = False) -> list[str]:
        raise NotImplementedError

    def remove(self, pkgs: list[str], yes: bool = False) -> list[str]:
        raise NotImplementedError

    def refresh(self) -> list[str] | None:
        return None

    def upgrade(self, yes: bool = False) -> list[str]:
        raise NotImplementedError

    def info(self, pkg: str) -> list[str]:
        raise NotImplementedError

    def search_cmd(self, query: str) -> list[str]:
        raise NotImplementedError

    # -- parsing ------------------------------------------------------------
    def parse_search(self, output: str) -> list[Candidate]:
        raise NotImplementedError

    def search(self, query: str, runner: Runner = run_capture) -> list[Candidate]:
        return self.parse_search(runner(self.search_cmd(query)))

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None


def _yes(flag: str, yes: bool) -> list[str]:
    return [flag] if yes else []


class Pacman(Source):
    def __init__(self, name="pacman", binary="pacman", root=True, kind="native"):
        super().__init__(name, kind, binary, root)

    def install(self, pkgs, yes=False):
        return [self.binary, "-S", "--needed", *_yes("--noconfirm", yes), *pkgs]

    def remove(self, pkgs, yes=False):
        return [self.binary, "-Rns", *_yes("--noconfirm", yes), *pkgs]

    def upgrade(self, yes=False):
        return [self.binary, "-Syu", *_yes("--noconfirm", yes)]

    def info(self, pkg):
        return [self.binary, "-Si", pkg]

    def search_cmd(self, query):
        return [self.binary, "-Ss", query]

    _HEAD = re.compile(r"^(\S+)/(\S+)\s+(\S+)(.*)$")

    def parse_search(self, output):
        out: list[Candidate] = []
        for line in output.splitlines():
            m = self._HEAD.match(line)
            if m:
                repo, name, version, rest = m.groups()
                flags = {"installed"} if "[installed" in rest else set()
                out.append(Candidate(name=name, source=self.name, pname=name, version=version,
                                     flags=flags, extra={"repo": repo}))
            elif out and line.startswith((" ", "\t")):
                out[-1].description = (out[-1].description + " " + line.strip()).strip()
        return out


class Aur(Pacman):
    """paru / yay: pacman syntax, run as the user."""

    def __init__(self, binary: str):
        super().__init__(name="aur", binary=binary, root=False, kind="aur")

    def search_cmd(self, query):
        return [self.binary, "-Ss", "--aur", query]


class Apt(Source):
    def __init__(self):
        super().__init__("apt", "native", "apt-get")

    def install(self, pkgs, yes=False):
        return ["apt-get", "install", *_yes("-y", yes), *pkgs]

    def remove(self, pkgs, yes=False):
        return ["apt-get", "remove", *_yes("-y", yes), *pkgs]

    def refresh(self):
        return ["apt-get", "update"]

    def upgrade(self, yes=False):
        return ["apt-get", "upgrade", *_yes("-y", yes)]

    def info(self, pkg):
        return ["apt-cache", "show", pkg]

    def search_cmd(self, query):
        return ["apt-cache", "search", query]

    def parse_search(self, output):
        out = []
        for line in output.splitlines():
            name, sep, desc = line.partition(" - ")
            if sep and name and " " not in name.strip():
                out.append(Candidate(name=name.strip(), source=self.name, pname=name.strip(),
                                     description=desc.strip()))
        return out


class Dnf(Source):
    def __init__(self):
        super().__init__("dnf", "native", "dnf")

    def install(self, pkgs, yes=False):
        return ["dnf", "install", *_yes("-y", yes), *pkgs]

    def remove(self, pkgs, yes=False):
        return ["dnf", "remove", *_yes("-y", yes), *pkgs]

    def refresh(self):
        return ["dnf", "makecache"]

    def upgrade(self, yes=False):
        return ["dnf", "upgrade", *_yes("-y", yes)]

    def info(self, pkg):
        return ["dnf", "info", pkg]

    def search_cmd(self, query):
        return ["dnf", "search", "-q", query]

    # dnf4: "bat.x86_64 : cat(1) clone"   dnf5: " bat.x86_64\tcat(1) clone"
    _LINE = re.compile(r"^\s*([A-Za-z0-9_.+-]+)\.(?:x86_64|aarch64|i686|noarch|src|armv7hl|ppc64le|s390x)\s*(?::\s*|\t+)(.*)$")

    def parse_search(self, output):
        out, seen = [], set()
        for line in output.splitlines():
            m = self._LINE.match(line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(Candidate(name=m.group(1), source=self.name, pname=m.group(1),
                                     description=m.group(2).strip()))
        return out


class Zypper(Source):
    def __init__(self):
        super().__init__("zypper", "native", "zypper")

    def install(self, pkgs, yes=False):
        return ["zypper", *_yes("--non-interactive", yes), "install", *pkgs]

    def remove(self, pkgs, yes=False):
        return ["zypper", *_yes("--non-interactive", yes), "remove", *pkgs]

    def refresh(self):
        return ["zypper", "refresh"]

    def upgrade(self, yes=False):
        return ["zypper", *_yes("--non-interactive", yes), "dist-upgrade"]

    def info(self, pkg):
        return ["zypper", "info", pkg]

    def search_cmd(self, query):
        return ["zypper", "--quiet", "search", "--type", "package", query]

    def parse_search(self, output):
        out = []
        for line in output.splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 3 or cols[1] in ("Name", "") or set(line) <= set("-+|"):
                continue
            status, name, summary = cols[0], cols[1], cols[2]
            flags = {"installed"} if status.startswith("i") else set()
            out.append(Candidate(name=name, source=self.name, pname=name,
                                 description=summary, flags=flags))
        return out


class Flatpak(Source):
    def __init__(self):
        super().__init__("flatpak", "flatpak", "flatpak", root=False)

    def install(self, pkgs, yes=False):
        return ["flatpak", "install", *_yes("-y", yes), "flathub", *pkgs]

    def remove(self, pkgs, yes=False):
        return ["flatpak", "uninstall", *_yes("-y", yes), *pkgs]

    def upgrade(self, yes=False):
        return ["flatpak", "update", *_yes("-y", yes)]

    def info(self, pkg):
        return ["flatpak", "remote-info", "flathub", pkg]

    def search_cmd(self, query):
        return ["flatpak", "search", "--columns=application,name,version,description", query]

    def parse_search(self, output):
        out = []
        for line in output.splitlines():
            cols = line.split("\t")
            if len(cols) < 2 or cols[0] in ("Application ID", "No matches found"):
                continue
            app, name = cols[0].strip(), cols[1].strip()
            out.append(Candidate(name=app, source=self.name, pname=name.lower().replace(" ", "-"),
                                 version=(cols[2].strip() if len(cols) > 2 else ""),
                                 description=(cols[3].strip() if len(cols) > 3 else ""),
                                 extra={"display_name": name}))
        return out


class Snap(Source):
    def __init__(self):
        super().__init__("snap", "snap", "snap")

    def install(self, pkgs, yes=False):
        return ["snap", "install", *pkgs]

    def remove(self, pkgs, yes=False):
        return ["snap", "remove", *pkgs]

    def upgrade(self, yes=False):
        return ["snap", "refresh"]

    def info(self, pkg):
        return ["snap", "info", pkg]

    def search_cmd(self, query):
        return ["snap", "find", query]

    def parse_search(self, output):
        out = []
        for line in output.splitlines()[1:]:
            parts = line.split(None, 4)
            if len(parts) >= 5:
                name, version, _publisher, _notes, summary = parts
                out.append(Candidate(name=name, source=self.name, pname=name,
                                     version=version, description=summary))
        return out


class NixProfile(Source):
    """Imperative ``nix profile`` (on NixOS, prefer nixorcist's declarative path)."""

    def __init__(self):
        super().__init__("nix", "nix", "nix", root=False)

    def install(self, pkgs, yes=False):
        return ["nix", "profile", "install", *(f"nixpkgs#{p}" for p in pkgs)]

    def remove(self, pkgs, yes=False):
        return ["nix", "profile", "remove", *pkgs]

    def upgrade(self, yes=False):
        return ["nix", "profile", "upgrade", "--all"]

    def info(self, pkg):
        return ["nix", "search", "nixpkgs", f"^{re.escape(pkg)}$", "--json"]

    def search_cmd(self, query):
        return ["nix", "search", "nixpkgs", query, "--json"]

    def parse_search(self, output):
        try:
            data = json.loads(output or "{}")
        except ValueError:
            return []
        out = []
        for key, info in data.items():
            attr = key.split(".", 2)[-1] if key.startswith("legacyPackages.") else key
            out.append(Candidate(name=attr, source=self.name, pname=info.get("pname", attr),
                                 version=info.get("version", ""),
                                 description=info.get("description", "")))
        return out


def all_sources(managers: tuple[str, ...]) -> dict[str, Source]:
    found: dict[str, Source] = {}
    if "pacman" in managers:
        found["pacman"] = Pacman()
    for helper in ("paru", "yay"):
        if helper in managers:
            found["aur"] = Aur(helper)
            break
    if "apt" in managers:
        found["apt"] = Apt()
    if "dnf" in managers:
        found["dnf"] = Dnf()
    if "zypper" in managers:
        found["zypper"] = Zypper()
    if "flatpak" in managers:
        found["flatpak"] = Flatpak()
    if "snap" in managers:
        found["snap"] = Snap()
    if "nix" in managers:
        found["nix"] = NixProfile()
    return found


SOURCE_CLASSES: dict[str, Callable[[], Source]] = {
    "pacman": Pacman, "apt": Apt, "dnf": Dnf, "zypper": Zypper,
    "flatpak": Flatpak, "snap": Snap, "nix": NixProfile, "aur": lambda: Aur("paru"),
}
