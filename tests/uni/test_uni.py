"""uni: parsers, translation, source priority, name mapping."""

from __future__ import annotations

import sys
import types

import pytest

from uni.cli import main
from uni.config import UniConfig
from uni.engine import Uni, UniError
from uni.sources import Apt, Aur, Dnf, Flatpak, NixProfile, Pacman, Snap, Zypper
from uni.system import SystemInfo, parse_os_release
from uni.translate import TranslateError, parse

# --------------------------------------------------------------------------
# Captured outputs
# --------------------------------------------------------------------------

PACMAN = """extra/bat 0.24.0-2 [installed]
    Cat clone with syntax highlighting and git integration
extra/bat-extras 2024.08.24-1
    Bash scripts that integrate bat with various command line tools
"""
APT = "bat - cat(1) clone with syntax highlighting and git integration\nbatcat-extras - extras\n"
DNF4 = """======================== Name Exactly Matched: bat ========================
bat.x86_64 : cat(1) clone with wings
bat.src : cat(1) clone with wings
bat-extras.noarch : Bash scripts that integrate bat
"""
DNF5 = """Matched fields: name (exact)
 bat.x86_64\tcat(1) clone with wings
Matched fields: name
 bat-extras.noarch\tBash scripts that integrate bat
"""
ZYPPER = """S  | Name       | Summary                                   | Type
---+------------+-------------------------------------------+--------
i+ | bat        | A cat(1) clone with syntax highlighting   | package
   | bat-extras | Bash scripts that integrate bat           | package
"""
FLATPAK = "com.discordapp.Discord\tDiscord\t0.0.80\tMessaging, Voice and Video Client\ndev.vencord.Vesktop\tVesktop\t1.5.5\tDiscord client\n"
SNAP = """Name     Version  Publisher  Notes  Summary
discord  0.0.80   snapcrafters*  -   All-in-one voice and text chat
"""
NIX = '{"legacyPackages.x86_64-linux.bat":{"pname":"bat","version":"0.24.0","description":"Cat clone"}}'


def test_pacman_parser():
    hits = Pacman().parse_search(PACMAN)
    assert [(c.name, c.version) for c in hits] == [("bat", "0.24.0-2"), ("bat-extras", "2024.08.24-1")]
    assert "installed" in hits[0].flags and hits[0].description.startswith("Cat clone")


def test_apt_parser():
    assert [c.name for c in Apt().parse_search(APT)] == ["bat", "batcat-extras"]


@pytest.mark.parametrize("output", [DNF4, DNF5])
def test_dnf_parsers(output):
    assert [c.name for c in Dnf().parse_search(output)] == ["bat", "bat-extras"]


def test_zypper_parser():
    hits = Zypper().parse_search(ZYPPER)
    assert [c.name for c in hits] == ["bat", "bat-extras"] and "installed" in hits[0].flags


def test_flatpak_snap_nix_parsers():
    assert [c.name for c in Flatpak().parse_search(FLATPAK)] == ["com.discordapp.Discord", "dev.vencord.Vesktop"]
    assert Flatpak().parse_search(FLATPAK)[0].pname == "discord"
    assert [c.name for c in Snap().parse_search(SNAP)] == ["discord"]
    assert [(c.name, c.version) for c in NixProfile().parse_search(NIX)] == [("bat", "0.24.0")]


def test_install_commands():
    assert Pacman().install(["bat"], yes=True) == ["pacman", "-S", "--needed", "--noconfirm", "bat"]
    assert Aur("paru").install(["bat"]) == ["paru", "-S", "--needed", "bat"]
    assert Zypper().install(["bat"], yes=True) == ["zypper", "--non-interactive", "install", "bat"]
    assert NixProfile().install(["bat"]) == ["nix", "profile", "install", "nixpkgs#bat"]


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv,action,pkgs,yes", [
    (["pacman", "-S", "bat"], "install", ["bat"], False),
    (["pacman", "-S", "--noconfirm", "bat", "fd"], "install", ["bat", "fd"], True),
    (["pacman", "-Ss", "bat"], "search", ["bat"], False),
    (["pacman", "-Syu"], "upgrade", [], False),
    (["pacman", "-Sy"], "update", [], False),
    (["pacman", "-Rns", "bat"], "remove", ["bat"], False),
    (["pacman", "-Si", "bat"], "info", ["bat"], False),
    (["apt", "install", "-y", "bat"], "install", ["bat"], True),
    (["apt", "full-upgrade"], "upgrade", [], False),
    (["dnf", "in", "bat"], "install", ["bat"], False),
    (["zypper", "se", "bat"], "search", ["bat"], False),
    (["zypper", "dup"], "upgrade", [], False),
    (["install", "bat"], "install", ["bat"], False),
    (["rm", "bat"], "remove", ["bat"], False),
])
def test_translation(argv, action, pkgs, yes):
    op = parse(argv)
    assert (op.action, op.packages, op.yes) == (action, pkgs, yes)


def test_translation_errors():
    with pytest.raises(TranslateError):
        parse(["pacman", "bat"])
    with pytest.raises(TranslateError):
        parse(["apt", "frobnicate", "bat"])


# --------------------------------------------------------------------------
# Engine: priority walk, explicit source, name mapping
# --------------------------------------------------------------------------

ARCH = SystemInfo(id="arch", managers=("pacman", "flatpak"))
FEDORA = SystemInfo(id="fedora", version_id="41", managers=("dnf", "flatpak"))


def fake_runner(table):
    def run(cmd):
        return table.get(cmd[0], {}).get(cmd[-1], "")
    return run


def test_install_prefers_native_then_falls_back_by_priority():
    runner = fake_runner({
        "pacman": {"bat": PACMAN, "discord": ""},
        "flatpak": {"discord": FLATPAK, "bat": ""},
    })
    uni = Uni(ARCH, UniConfig(), runner=runner, sources={"pacman": Pacman(), "flatpak": Flatpak()})
    plans = uni.plan(parse(["install", "bat", "discord"]))
    assert [p.argv() for p in plans] == [
        ["sudo", "pacman", "-S", "--needed", "bat"],
        ["flatpak", "install", "flathub", "com.discordapp.Discord"],
    ]


def test_priority_is_configurable():
    runner = fake_runner({"pacman": {"bat": PACMAN}, "flatpak": {"bat": "org.bat.Bat\tbat\t1\tdesc\n"}})
    cfg = UniConfig(priority={"flatpak": 1, "native": 50})
    uni = Uni(ARCH, cfg, runner=runner, sources={"pacman": Pacman(), "flatpak": Flatpak()})
    assert [s.name for s in uni.ordered()] == ["flatpak", "pacman"]
    assert uni.plan(parse(["install", "bat"]))[0].source.name == "flatpak"


def test_disabled_and_explicit_source():
    runner = fake_runner({"pacman": {"bat": PACMAN}})
    uni = Uni(ARCH, UniConfig(disabled={"flatpak"}), runner=runner,
              sources={"pacman": Pacman(), "flatpak": Flatpak()})
    assert list(uni.sources) == ["pacman"]
    with pytest.raises(UniError):
        uni.plan(parse(["install", "bat"]), only="flatpak")
    assert uni.plan(parse(["install", "bat"]), only="native")[0].source.name == "pacman"


def test_unknown_package_is_an_error():
    uni = Uni(ARCH, UniConfig(), runner=fake_runner({}), sources={"pacman": Pacman()})
    with pytest.raises(UniError):
        uni.plan(parse(["install", "nope"]))


def test_search_merges_sources_defaults_first():
    runner = fake_runner({"pacman": {"bat": PACMAN}, "flatpak": {"bat": ""}})
    uni = Uni(ARCH, UniConfig(), runner=runner, sources={"pacman": Pacman(), "flatpak": Flatpak()})
    hits = uni.search("bat")
    assert hits[0].name == "bat" and hits[0].is_default and hits[0].source == "pacman"


def test_foreign_syntax_maps_names_via_pkgmap(monkeypatch):
    calls = []

    def resolve(name, from_repo=None, to_repo=None):
        calls.append((name, from_repo, to_repo))
        return types.SimpleNamespace(install_name="rust-bat") if name == "bat" else None

    monkeypatch_module = types.ModuleType("pkgmap")
    monkeypatch_module.resolve = resolve
    monkeypatch.setitem(sys.modules, "pkgmap", monkeypatch_module)
    dnf_out = "rust-bat.x86_64 : cat(1) clone\n"
    uni = Uni(FEDORA, UniConfig(), runner=fake_runner({"dnf": {"rust-bat": dnf_out}}),
              sources={"dnf": Dnf()})
    plans = uni.plan(parse(["pacman", "-S", "bat"]))
    assert plans[0].packages == ["rust-bat"]
    assert "via pkgmap" in plans[0].note
    assert calls == [("bat", "arch", "fedora_41")]


def test_update_and_upgrade_cover_every_source():
    uni = Uni(ARCH, UniConfig(), runner=fake_runner({}), sources={"pacman": Pacman(), "flatpak": Flatpak()})
    assert [p.argv() for p in uni.plan(parse(["upgrade", "-y"]))] == [
        ["sudo", "pacman", "-Syu", "--noconfirm"], ["flatpak", "update", "-y"]]


# --------------------------------------------------------------------------
# System detection and CLI
# --------------------------------------------------------------------------


def test_os_release_and_families():
    data = parse_os_release('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n')
    info = SystemInfo(id=data["ID"], id_like=(data["ID_LIKE"],), version_id=data["VERSION_ID"])
    assert info.family == "debian" and info.repology_repo == "ubuntu_24_04"
    assert SystemInfo(id="endeavouros", id_like=("arch",)).family == "arch"
    assert SystemInfo(id="opensuse-tumbleweed", id_like=("opensuse", "suse")).repology_repo == "opensuse_tumbleweed"


def test_cli_dry_run(capsys=None):
    runner = fake_runner({"pacman": {"bat": PACMAN}})
    uni = Uni(ARCH, UniConfig(), runner=runner, sources={"pacman": Pacman()})
    assert main(["-n", "install", "bat"], uni=uni) == 0
    assert main(["-n", "apt", "install", "bat"], uni=uni) == 0
    assert main(["-n", "install", "nope"], uni=uni) == 1
