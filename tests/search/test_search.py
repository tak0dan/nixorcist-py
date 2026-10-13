"""Version-aware search, defaults vs variants, and service linking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nixorcist.search.nixpkgs import (
    PackageIndex,
    Release,
    SearchError,
    detect_release,
    parse_aliases,
    release_from_string,
)
from nixorcist.search.resolve import attach_services, resolve_query
from nixorcist.search.services import services_for


def pkg(pname, version, desc, **meta):
    return {"pname": pname, "version": version, "meta": {"description": desc, **meta}}


COMMON = {
    "discord": pkg("discord", "0.0.80", "All-in-one cross-platform voice and text chat for gamers", license={"free": False}),
    "discord-ptb": pkg("discord-ptb", "0.0.120", "All-in-one cross-platform voice and text chat for gamers"),
    "discord-canary": pkg("discord-canary", "0.0.600", "All-in-one cross-platform voice and text chat for gamers"),
    "vesktop": pkg("vesktop", "1.5.5", "Alternate client for Discord with Vencord built-in"),
    "python312Packages.discordpy": pkg("discord.py", "2.4.0", "Python wrapper for the Discord API"),
    "steam": pkg("steam", "1.0.0.81", "Digital distribution platform", license={"free": False}),
    "virt-manager": pkg("virt-manager", "5.0.0", "Desktop user interface for managing virtual machines"),
    "libvirt": pkg("libvirt", "10.0.0", "Toolkit to interact with the virtualization capabilities"),
    "flatpak": pkg("flatpak", "1.16.0", "Linux application sandboxing and distribution framework"),
    "oldthing": pkg("oldthing", "1.0", "Something broken", broken=True),
}

R2405 = {**COMMON, "kate": pkg("kate", "23.08.5", "Advanced text editor"),
         "sddm": pkg("sddm", "0.21.0", "QML based X11 display manager")}
R2511 = {**COMMON, "kdePackages.kate": pkg("kate", "25.08.1", "Advanced text editor"),
         "kdePackages.sddm": pkg("sddm", "0.21.0", "QML based X11 display manager")}


def opt(type_, desc="", default=None):
    o = {"type": type_, "description": desc}
    if default is not None:
        o["default"] = {"_type": "literalExpression", "text": default}
    return o


OPTS_COMMON = {
    "programs.steam.enable": opt("boolean", "Whether to enable steam."),
    "programs.steam.package": opt("package", default="pkgs.steam"),
    "services.flatpak.enable": opt("boolean", "Whether to enable flatpak."),
    "programs.virt-manager.enable": opt("boolean"),
    "programs.virt-manager.package": opt("package", default="pkgs.virt-manager"),
    "virtualisation.libvirtd.enable": opt("boolean"),
    "virtualisation.libvirtd.package": opt("package", default="pkgs.libvirt"),
}
OPTS_2405 = {**OPTS_COMMON, "services.xserver.displayManager.sddm.enable": opt("boolean"),
             "services.xserver.displayManager.sddm.package": opt("package", default="pkgs.plasma5Packages.sddm")}
OPTS_2511 = {**OPTS_COMMON, "services.displayManager.sddm.enable": opt("boolean"),
             "services.displayManager.sddm.package": opt("package", default="pkgs.kdePackages.sddm")}


def index_for(pkgs, opts, release, aliases=None):
    idx = PackageIndex(":memory:", release_from_string(release))
    idx.load_packages({"version": 2, "packages": pkgs})
    idx.load_options(opts)
    if aliases:
        idx.load_aliases(aliases)
    return idx


# --------------------------------------------------------------------------
# Release detection
# --------------------------------------------------------------------------


def test_release_from_flake_lock(tmp_path):
    (tmp_path / "flake.lock").write_text(json.dumps({
        "root": "root", "version": 7,
        "nodes": {
            "root": {"inputs": {"nixpkgs": "nixpkgs_2"}},
            "nixpkgs_2": {"original": {"owner": "NixOS", "repo": "nixpkgs", "ref": "nixos-25.05", "type": "github"},
                          "locked": {"rev": "abcdef1234567890", "type": "github"}},
        },
    }))
    rel = detect_release(tmp_path)
    assert (rel.channel, rel.version, rel.rev, rel.source) == ("nixos-25.05", "25.05", "abcdef1234567890", "flake.lock")
    assert rel.key == "nixos-25.05-abcdef123456"


def test_release_unstable_flake_without_ref(tmp_path):
    (tmp_path / "flake.lock").write_text(json.dumps({
        "root": "root", "nodes": {"root": {"inputs": {"nixpkgs": "nixpkgs"}},
                                  "nixpkgs": {"original": {"type": "github"}, "locked": {"rev": "ff"}}}}))
    assert detect_release(tmp_path).channel == "nixos-unstable"


def test_release_override():
    assert detect_release(None, "24.11").channel == "nixos-24.11"
    assert detect_release(None, "unstable").channel == "nixos-unstable"
    with pytest.raises(SearchError):
        release_from_string("banana")


# --------------------------------------------------------------------------
# Same declaration, different releases
# --------------------------------------------------------------------------


def test_kate_on_2405_is_top_level():
    idx = index_for(R2405, OPTS_2405, "24.05")
    res = resolve_query(idx, "kate")
    assert res.attrs == ["kate"] and res.note == ""


def test_kate_on_2511_resolves_to_kdepackages_without_aliases():
    idx = index_for(R2511, OPTS_2511, "25.11")
    res = resolve_query(idx, "kate")
    assert res.attrs == ["kdePackages.kate"]
    assert "resolves to 'kdePackages.kate'" in res.note


def test_kate_on_2511_uses_rename_record_when_available():
    idx = index_for(R2511, OPTS_2511, "25.11", aliases={"kate": "kdePackages.kate"})
    res = resolve_query(idx, "kate")
    assert res.attrs == ["kdePackages.kate"]
    assert "renamed" in res.note


def test_pkgs_prefix_is_accepted():
    idx = index_for(R2511, OPTS_2511, "25.11")
    assert resolve_query(idx, "pkgs.discord").attrs == ["discord"]


# --------------------------------------------------------------------------
# Defaults, variants, ranking
# --------------------------------------------------------------------------


def test_discord_default_and_variants():
    idx = index_for(R2511, OPTS_2511, "25.11")
    ranked = idx.search("discord")
    assert ranked[0].name == "discord" and ranked[0].is_default
    assert sum(c.is_default for c in ranked) == 1
    names = [c.name for c in ranked]
    assert names.index("discord-ptb") < names.index("python312Packages.discordpy")
    assert names.index("discord-canary") < names.index("python312Packages.discordpy")
    assert "unfree" in ranked[0].flags
    assert ranked[0].family == next(c for c in ranked if c.name == "discord-ptb").family


def test_description_matches_are_found_but_ranked_low():
    idx = index_for(R2511, OPTS_2511, "25.11")
    ranked = idx.search("discord")
    assert "vesktop" in [c.name for c in ranked]
    assert [c.name for c in ranked].index("vesktop") > 2


def test_declared_packages_are_boosted_and_flagged():
    idx = index_for(R2511, OPTS_2511, "25.11")
    ranked = idx.search("discord", declared={"discord-canary"})
    canary = next(c for c in ranked if c.name == "discord-canary")
    assert "declared" in canary.flags
    assert [c.name for c in ranked][:2] == ["discord", "discord-canary"]


def test_broken_is_never_default():
    idx = index_for(R2511, OPTS_2511, "25.11")
    ranked = idx.search("oldthing")
    assert ranked and not ranked[0].is_default
    with pytest.raises(SearchError):
        resolve_query(idx, "oldthing")


def test_ambiguous_name_asks_for_pick():
    idx = index_for(R2511, OPTS_2511, "25.11")
    with pytest.raises(SearchError) as err:
        resolve_query(idx, "disc")
    assert "--pick" in err.value.diagnostic.suggestion


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------


def test_steam_needs_programs_steam():
    idx = index_for(R2511, OPTS_2511, "25.11")
    links = services_for(idx, "steam").recommended
    assert [l.option for l in links] == ["programs.steam.enable"]
    assert links[0].installs_package


def test_virt_manager_brings_libvirtd():
    idx = index_for(R2511, OPTS_2511, "25.11")
    options = {l.option for l in services_for(idx, "virt-manager").recommended}
    assert options == {"programs.virt-manager.enable", "virtualisation.libvirtd.enable"}


def test_flatpak_found_by_name():
    idx = index_for(R2511, OPTS_2511, "25.11")
    assert [l.option for l in services_for(idx, "flatpak").recommended] == ["services.flatpak.enable"]


def test_sddm_option_follows_the_release():
    old = index_for(R2405, OPTS_2405, "24.05")
    new = index_for(R2511, OPTS_2511, "25.11")
    assert {l.option for l in services_for(old, "sddm").recommended} == {"services.xserver.displayManager.sddm.enable"}
    assert {l.option for l in services_for(new, "kdePackages.sddm").recommended} == {"services.displayManager.sddm.enable"}


def test_plain_packages_have_no_services():
    idx = index_for(R2511, OPTS_2511, "25.11")
    assert services_for(idx, "discord").recommended == []
    res = attach_services(idx, resolve_query(idx, "steam"))
    assert list(res.services) == ["steam"]


# --------------------------------------------------------------------------
# aliases.nix
# --------------------------------------------------------------------------

ALIASES = '''
{ lib, config, overridden }:
self: super:
mapAliases {
  kate = throw "'kate' has been renamed to/replaced by 'kdePackages.kate'"; # Converted to throw 2024-11-01
  gnome_mplayer = throw "gnome_mplayer has been removed because it was unmaintained"; # Added 2023-01-01
  foo = bar; # Added 2020-01-01
  qt-video-wlr = lib.warnOnInstantiate "'qt-video-wlr' was renamed" kdePackages.qt-video-wlr;
}
'''


def test_parse_aliases():
    assert parse_aliases(ALIASES) == {
        "kate": "kdePackages.kate",
        "foo": "bar",
        "qt-video-wlr": "kdePackages.qt-video-wlr",
    }


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_index_persists_per_release(tmp_path):
    rel = Release("nixos-25.11", "25.11")
    idx = PackageIndex.for_release(tmp_path, rel)
    idx.load_packages({"packages": R2511})
    idx.set_meta(source="test")
    again = PackageIndex.for_release(tmp_path, rel)
    assert again.package_count == len(R2511) and again.meta("source") == "test"
    assert (tmp_path / "search" / "nixos-25.11.sqlite").is_file()
