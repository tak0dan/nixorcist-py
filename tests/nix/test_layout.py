"""Configuration-architecture analysis and style-mirroring writer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nixorcist.nix.config import discover_root
from nixorcist.nix.layout import (
    GroupStyle,
    LayoutError,
    Mechanism,
    analyze,
    plan_extend_group,
    plan_new_group,
    recommend_style,
)
from nixorcist.nix.layout_apply import LayoutApplyError, apply_edits, render_diff

HARDWARE = "{ ... }:\n{\n  boot.loader.grub.enable = true;\n}\n"


def write_tree(base: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    return base


def nix_parses(path: Path) -> bool:
    if not shutil.which("nix-instantiate"):
        return True
    return subprocess.run(
        ["nix-instantiate", "--parse", str(path)], capture_output=True
    ).returncode == 0


# --------------------------------------------------------------------------
# Fixtures: the three layouts people actually use
# --------------------------------------------------------------------------

VANILLA = {
    "configuration.nix": """{ config, pkgs, ... }:

{
  imports = [ ./hardware-configuration.nix ];

  environment.systemPackages = with pkgs; [
    vim
    git
  ];
}
""",
    "hardware-configuration.nix": HARDWARE,
}

MIXED = {
    "configuration.nix": """{ config, pkgs, ... }:
let
  devTools = with pkgs; [ gcc gnumake ];
in
{
  imports = [
    ./hardware-configuration.nix
    ./groups/games.nix
    ./groups/office.nix
  ];

  # base
  environment.systemPackages = with pkgs; [
    vim
    kdePackages.kate
    (discord.override { withVencord = true; })
  ]
  # media
  ++ [ mpv ]
  ++ devTools
  ++ (import ./lists/net.nix { inherit pkgs; });
}
""",
    "hardware-configuration.nix": HARDWARE,
    "groups/games.nix": """{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    steam
  ];
}
""",
    "groups/office.nix": """{ pkgs, ... }:
{
  programs.firefox.enable = true;
  environment.systemPackages = [
    pkgs.libreoffice
  ];
}
""",
    "lists/net.nix": "{ pkgs }:\n\nwith pkgs; [\n  curl\n  wget\n]\n",
}

HUB_LISTS = {
    "configuration.nix": """{ config, pkgs, ... }:
{
  imports = [ ./hardware-configuration.nix ./packages ];
}
""",
    "hardware-configuration.nix": HARDWARE,
    "packages/default.nix": """{ pkgs, ... }:
{
  environment.systemPackages =
    (import ./groups/dev.nix { inherit pkgs; })
    ++ (import ./groups/media.nix { inherit pkgs; });
}
""",
    "packages/groups/dev.nix": "# development tools\n{ pkgs }:\n\nwith pkgs; [\n  gcc\n  gdb\n]\n",
    "packages/groups/media.nix": "# media\n{ pkgs }:\n\nwith pkgs; [\n  mpv\n]\n",
}

HUB_MODULES = {
    "configuration.nix": """{ config, pkgs, ... }:
{
  imports = [ ./hardware-configuration.nix ./packages/hub.nix ];
}
""",
    "hardware-configuration.nix": HARDWARE,
    "packages/hub.nix": "{\n  imports = [\n    ./dev.nix\n    ./media.nix\n  ];\n}\n",
    "packages/dev.nix": "{ pkgs, ... }:\n{\n  environment.systemPackages = with pkgs; [ gcc ];\n}\n",
    "packages/media.nix": "{ pkgs, ... }:\n{\n  environment.systemPackages = with pkgs; [ mpv ];\n}\n",
}


def load(tmp_path: Path, files: dict[str, str]):
    write_tree(tmp_path, files)
    root = discover_root(tmp_path)
    return root, analyze(root)


def by_name(layout):
    return {g.name: g for g in layout.groups}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_vanilla_single_inline_group(tmp_path):
    _, layout = load(tmp_path, VANILLA)
    assert [(g.name, g.style) for g in layout.groups] == [("main", GroupStyle.INLINE)]
    assert layout.groups[0].attrs == ["vim", "git"]
    assert layout.hubs == []
    decision = recommend_style(layout)
    assert decision.style is GroupStyle.INLINE and not decision.needs_choice


def test_mixed_layout_classifies_every_group(tmp_path):
    _, layout = load(tmp_path, MIXED)
    groups = by_name(layout)
    assert groups["base"].style is GroupStyle.INLINE
    assert groups["media"].style is GroupStyle.INLINE
    assert groups["devtools"].style is GroupStyle.LET
    assert groups["games"].style is GroupStyle.MODULE
    assert groups["games"].mechanism is Mechanism.NIXOS_MODULE
    assert groups["office"].style is GroupStyle.MODULE
    assert groups["net"].style is GroupStyle.MODULE
    assert groups["net"].mechanism is Mechanism.LIST_FILE
    assert layout.hubs == []


def test_attribute_paths_are_kept_whole(tmp_path):
    """``kdePackages.kate`` must not collapse to ``kate``."""
    _, layout = load(tmp_path, MIXED)
    base = by_name(layout)["base"]
    assert base.attrs == ["vim", "kdePackages.kate"]
    opaque = [p for p in base.packages if p.is_opaque]
    assert len(opaque) == 1 and "discord.override" in opaque[0].text
    assert [g.name for g in layout.where_declared("kdePackages.kate")] == ["base"]
    assert layout.where_declared("kate") == []


def test_mixed_without_majority_asks(tmp_path):
    _, layout = load(tmp_path, MIXED)
    decision = recommend_style(layout)
    # 2 inline + 1 let + 3 module: no strict majority
    assert decision.needs_choice and decision.style is None
    assert decision.options[0] is GroupStyle.MODULE


def test_majority_wins(tmp_path):
    files = dict(MIXED)
    files["configuration.nix"] = files["configuration.nix"].replace(
        "  ++ devTools\n", ""
    ).replace("  # media\n  ++ [ mpv ]\n", "")
    _, layout = load(tmp_path, files)
    decision = recommend_style(layout)
    assert decision.style is GroupStyle.MODULE  # 3 module vs 1 inline
    assert recommend_style(layout, ask_when_mixed=True).needs_choice


def test_hub_of_list_files(tmp_path):
    _, layout = load(tmp_path, HUB_LISTS)
    assert [h.name for h in layout.hubs] == ["default.nix"]
    groups = by_name(layout)
    assert set(groups) == {"dev", "media"}
    assert all(g.style is GroupStyle.HUB for g in groups.values())
    assert all(g.mechanism is Mechanism.LIST_FILE for g in groups.values())
    assert recommend_style(layout).style is GroupStyle.HUB


def test_hub_of_modules(tmp_path):
    _, layout = load(tmp_path, HUB_MODULES)
    assert [h.name for h in layout.hubs] == ["hub.nix"]
    groups = by_name(layout)
    assert {g.style for g in groups.values()} == {GroupStyle.HUB}
    assert {g.mechanism for g in groups.values()} == {Mechanism.NIXOS_MODULE}


def test_nested_attrset_form_is_detected(tmp_path):
    files = {
        "configuration.nix": "{ pkgs, ... }:\n{\n  environment = {\n    systemPackages = with pkgs; [ htop ];\n  };\n}\n",
    }
    _, layout = load(tmp_path, files)
    assert layout.groups[0].attrs == ["htop"]


# --------------------------------------------------------------------------
# Writing: every style, mirrored from the user's own files
# --------------------------------------------------------------------------


def apply_and_reload(root, edits, attrs=None):
    result = apply_edits(root, edits, check="parse", new_attrs=attrs)
    assert result.committed
    for rel in result.changed:
        assert nix_parses(root.directory / rel), rel
    return analyze(root)


def test_new_inline_group(tmp_path):
    root, layout = load(tmp_path, VANILLA)
    edits = plan_new_group(layout, "office", ["libreoffice", "pkgs.kdePackages.okular"], GroupStyle.INLINE)
    after = apply_and_reload(root, edits)
    office = by_name(after)["office"]
    assert office.style is GroupStyle.INLINE
    assert office.attrs == ["libreoffice", "kdePackages.okular"]
    assert by_name(after)["main"].attrs == ["vim", "git"]


def test_new_inline_group_without_existing_binding(tmp_path):
    files = {"configuration.nix": "{ pkgs, ... }:\n{\n  networking.hostName = \"box\";\n}\n"}
    root, layout = load(tmp_path, files)
    edits = plan_new_group(layout, "base", ["vim"], GroupStyle.INLINE)
    after = apply_and_reload(root, edits)
    assert by_name(after)["base"].attrs == ["vim"]


def test_new_let_group(tmp_path):
    root, layout = load(tmp_path, MIXED)
    edits = plan_new_group(layout, "rust", ["cargo", "rustc"], GroupStyle.LET)
    after = apply_and_reload(root, edits)
    rust = by_name(after)["rust"]
    assert rust.style is GroupStyle.LET and rust.attrs == ["cargo", "rustc"]


def test_new_module_group_mirrors_majority_mechanism(tmp_path):
    root, layout = load(tmp_path, MIXED)
    edits = plan_new_group(layout, "chat", ["discord"], GroupStyle.MODULE)
    created = [e for e in edits if e.created]
    assert [e.path.relative_to(root.directory).as_posix() for e in created] == ["groups/chat.nix"]
    # cloned from games.nix (the clean sibling), not office.nix (has extra options)
    assert "programs.firefox" not in created[0].new_text
    after = apply_and_reload(root, edits)
    chat = by_name(after)["chat"]
    assert chat.style is GroupStyle.MODULE and chat.mechanism is Mechanism.NIXOS_MODULE
    assert "./groups/chat.nix" in (root.directory / "configuration.nix").read_text()


def test_new_hub_list_file_group(tmp_path):
    root, layout = load(tmp_path, HUB_LISTS)
    edits = plan_new_group(layout, "games", ["steam", "lutris"], GroupStyle.HUB)
    after = apply_and_reload(root, edits)
    games = by_name(after)["games"]
    assert games.style is GroupStyle.HUB and games.mechanism is Mechanism.LIST_FILE
    assert games.file == (root.directory / "packages/groups/games.nix").resolve()
    hub_text = (root.directory / "packages/default.nix").read_text()
    assert "(import ./groups/games.nix { inherit pkgs; })" in hub_text
    # sibling's shape copied (args, with pkgs), its header comment and list not
    assert (root.directory / "packages/groups/games.nix").read_text() == (
        "{ pkgs }:\n\nwith pkgs; [\n  steam\n  lutris\n]\n"
    )


def test_new_hub_module_group(tmp_path):
    root, layout = load(tmp_path, HUB_MODULES)
    edits = plan_new_group(layout, "games", ["steam"], GroupStyle.HUB)
    after = apply_and_reload(root, edits)
    games = by_name(after)["games"]
    assert games.style is GroupStyle.HUB and games.mechanism is Mechanism.NIXOS_MODULE
    assert "./games.nix" in (root.directory / "packages/hub.nix").read_text()


def test_extend_group_skips_duplicates_and_keeps_style(tmp_path):
    root, layout = load(tmp_path, MIXED)
    edits = plan_extend_group(layout, "office", ["libreoffice", "kdePackages.okular"])
    after = apply_and_reload(root, edits)
    assert by_name(after)["office"].attrs == ["libreoffice", "kdePackages.okular"]
    # office.nix has no ``with pkgs;`` so the element must be qualified
    assert "pkgs.kdePackages.okular" in (root.directory / "groups/office.nix").read_text()
    assert plan_extend_group(after, "office", ["libreoffice"]) == []


def test_extend_inline_one_line_list(tmp_path):
    root, layout = load(tmp_path, MIXED)
    edits = plan_extend_group(layout, "devtools", ["cmake"])
    after = apply_and_reload(root, edits)
    assert by_name(after)["devtools"].attrs == ["gcc", "gnumake", "cmake"]


def test_rejects_duplicates_and_bad_input(tmp_path):
    _, layout = load(tmp_path, MIXED)
    with pytest.raises(LayoutError):
        plan_new_group(layout, "games", ["x"], GroupStyle.MODULE)
    with pytest.raises(LayoutError):
        plan_new_group(layout, "new", ["foo bar"], GroupStyle.MODULE)
    with pytest.raises(LayoutError):
        plan_new_group(layout, "new", ["x"], GroupStyle.HUB)  # no hub here


def test_dry_run_diff_and_no_commit(tmp_path):
    root, layout = load(tmp_path, VANILLA)
    before = (root.directory / "configuration.nix").read_text()
    edits = plan_new_group(layout, "office", ["libreoffice"], GroupStyle.INLINE)
    diff = render_diff(root, edits)
    assert "+    libreoffice" in diff and "# office" in diff
    result = apply_edits(root, edits, check="parse", commit=False)
    assert not result.committed
    assert (root.directory / "configuration.nix").read_text() == before


def test_broken_output_is_never_committed(tmp_path):
    root, layout = load(tmp_path, VANILLA)
    edits = plan_new_group(layout, "office", ["libreoffice"], GroupStyle.INLINE)
    edits[0].new_text = edits[0].new_text.replace("];", "", 1)
    before = (root.directory / "configuration.nix").read_text()
    with pytest.raises(LayoutApplyError):
        apply_edits(root, edits, check="parse")
    assert (root.directory / "configuration.nix").read_text() == before
