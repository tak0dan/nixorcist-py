"""Native-syntax translation: ``uni pacman -S bat`` works on Fedora.

A foreign command is parsed into a neutral :class:`Op`, its package names are
mapped from the foreign distribution's naming to the local one (pkgmap /
Repology when available), and the op is executed by the local source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIONS = ("install", "remove", "search", "update", "upgrade", "info", "list")


class TranslateError(ValueError):
    pass


@dataclass
class Op:
    action: str
    packages: list[str] = field(default_factory=list)
    yes: bool = False
    dialect: str = "uni"  # which syntax it was written in

    @property
    def query(self) -> str:
        return " ".join(self.packages)


def _split(args: list[str]) -> tuple[list[str], list[str]]:
    flags = [a for a in args if a.startswith("-")]
    words = [a for a in args if not a.startswith("-")]
    return flags, words


def parse_pacman(args: list[str]) -> Op:
    flags, words = _split(args)
    if not flags:
        raise TranslateError("pacman needs an operation flag like -S, -R, -Ss, -Syu")
    op = flags[0]
    yes = "--noconfirm" in flags
    letters = op.lstrip("-")
    if letters.startswith("S"):
        rest = letters[1:]
        if "s" in rest:
            return Op("search", words, dialect="pacman")
        if "i" in rest:
            return Op("info", words, dialect="pacman")
        if "u" in rest and not words:
            return Op("upgrade", yes=yes, dialect="pacman")
        if "y" in rest and not words:
            return Op("update", dialect="pacman")
        return Op("install", words, yes=yes, dialect="pacman")
    if letters.startswith("R"):
        return Op("remove", words, yes=yes, dialect="pacman")
    if letters.startswith("Q"):
        if "i" in letters[1:] and words:
            return Op("info", words, dialect="pacman")
        if "s" in letters[1:]:
            return Op("list", words, dialect="pacman")
        return Op("list", words, dialect="pacman")
    raise TranslateError(f"unsupported pacman operation {op}")


_VERBS = {
    "apt": {"install": "install", "remove": "remove", "purge": "remove", "search": "search",
            "update": "update", "upgrade": "upgrade", "full-upgrade": "upgrade",
            "dist-upgrade": "upgrade", "show": "info", "list": "list"},
    "dnf": {"install": "install", "in": "install", "remove": "remove", "rm": "remove",
            "erase": "remove", "search": "search", "makecache": "update", "check-update": "update",
            "upgrade": "upgrade", "update": "upgrade", "distro-sync": "upgrade", "info": "info",
            "list": "list"},
    "zypper": {"install": "install", "in": "install", "remove": "remove", "rm": "remove",
               "search": "search", "se": "search", "refresh": "update", "ref": "update",
               "update": "upgrade", "up": "upgrade", "dist-upgrade": "upgrade", "dup": "upgrade",
               "info": "info", "if": "info", "packages": "list"},
    "uni": {a: a for a in ACTIONS} | {"in": "install", "rm": "remove", "s": "search",
                                       "up": "upgrade", "show": "info"},
}
_YES = {"-y", "--yes", "--assumeyes", "-n", "--non-interactive", "--noconfirm"}


def parse_verb(dialect: str, args: list[str]) -> Op:
    flags, words = _split(args)
    if not words:
        raise TranslateError(f"{dialect}: missing command")
    verb, packages = words[0], words[1:]
    action = _VERBS[dialect].get(verb)
    if action is None:
        raise TranslateError(f"unsupported {dialect} command {verb!r}")
    return Op(action, packages, yes=bool(_YES & set(flags)), dialect=dialect)


DIALECTS = {
    "pacman": parse_pacman, "paru": parse_pacman, "yay": parse_pacman,
    "apt": lambda a: parse_verb("apt", a), "apt-get": lambda a: parse_verb("apt", a),
    "dnf": lambda a: parse_verb("dnf", a), "yum": lambda a: parse_verb("dnf", a),
    "zypper": lambda a: parse_verb("zypper", a),
}

# naming family each dialect implies, for cross-distro name mapping
DIALECT_REPO = {"pacman": "arch", "paru": "aur", "yay": "aur", "apt": "debian_12",
                "apt-get": "debian_12", "dnf": "fedora_rawhide", "yum": "fedora_rawhide",
                "zypper": "opensuse_tumbleweed"}


def parse(argv: list[str]) -> Op:
    if argv and argv[0] in DIALECTS:
        op = DIALECTS[argv[0]](argv[1:])
        op.dialect = argv[0]
        return op
    return parse_verb("uni", argv)
