"""End-to-end CLI tests exercising the full pipeline through main().

Every test uses an isolated ``NIXORCIST_HOME`` so no real state is touched.
Operations that would trigger ``nix profile install`` use ``--dry-run``.
Group-only operations (``-G``) modify manifests without touching the profile.
"""

from __future__ import annotations

import os
import textwrap
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from nixorcist.cli.entry import main


@pytest.fixture
def cli_env(tmp_path):
    """Run the CLI under an isolated home with identity resolution."""
    old_home = os.environ.get("NIXORCIST_HOME")
    old_resolve = os.environ.get("NIXORCIST_RESOLVE")
    os.environ["NIXORCIST_HOME"] = str(tmp_path)
    os.environ["NIXORCIST_RESOLVE"] = "identity"
    yield tmp_path
    if old_home is None:
        os.environ.pop("NIXORCIST_HOME", None)
    else:
        os.environ["NIXORCIST_HOME"] = old_home
    if old_resolve is None:
        os.environ.pop("NIXORCIST_RESOLVE", None)
    else:
        os.environ["NIXORCIST_RESOLVE"] = old_resolve


def _run(*argv):
    out = StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


def _write_config(tmp_path, content=None):
    content = content or textwrap.dedent("""\
        { pkgs, ... }:
        {
          imports = [ ./hardware-configuration.nix ];
          environment.systemPackages = with pkgs; [ vim ];
        }
    """)
    cfg = tmp_path / "configuration.nix"
    cfg.write_text(content, "utf-8")
    hw = tmp_path / "hardware-configuration.nix"
    hw.write_text("{ ... }: {}", "utf-8")
    return cfg


def _ensure_groups(*names):
    """Create groups and add packages via -G (manifest-only, no profile)."""
    for name in names:
        main(["-G", f"#{{{name}}}"])


# ---------------------------------------------------------------------------
# Group lifecycle via CLI
# ---------------------------------------------------------------------------


class TestGroupSubcommand:
    def test_create_list_show_remove(self, cli_env):
        code, _ = _run("group", "create", "Programming")
        assert code == 0

        code, out = _run("group", "list")
        assert code == 0
        assert "Programming" in out

        code, out = _run("group", "show", "Programming")
        assert code == 0
        assert "name: Programming" in out

        code, _ = _run("group", "remove", "Programming")
        assert code == 0

        code, out = _run("group", "list")
        assert code == 0
        assert "Programming" not in out

    def test_rename(self, cli_env):
        _run("group", "create", "OldName")
        code, _ = _run("group", "rename", "OldName", "NewName")
        assert code == 0
        code, out = _run("group", "list")
        assert "NewName" in out
        assert "OldName" not in out

    def test_create_empty_group(self, cli_env):
        code, _ = _run("group", "create", "Empty")
        assert code == 0
        code, out = _run("group", "show", "Empty")
        assert "name: Empty" in out
        assert "packages:" in out


# ---------------------------------------------------------------------------
# DSL operations via CLI (group-only: no profile install)
# ---------------------------------------------------------------------------


class TestDslGroupOnly:
    """Tests that use -G (add to group manifest) without -I (profile install)."""

    def test_add_broadcast(self, cli_env):
        code, _ = _run("-G#Programming#{python,gcc}")
        assert code == 0
        code, out = _run("-L")
        assert "Programming" in out

    def test_add_positional(self, cli_env):
        code, _ = _run("-G{A,B}##{python}{gcc}")
        assert code == 0
        code, out = _run("-LG")
        assert "python" in out
        assert "gcc" in out

    def test_add_pipe_separator(self, cli_env):
        code, _ = _run("-G#Dev#{git|vim}")
        assert code == 0
        code, out = _run("-LG")
        assert "git" in out
        assert "vim" in out

    def test_add_exclamation_separator(self, cli_env):
        code, _ = _run("-G#Dev#{git!vim}")
        assert code == 0
        code, out = _run("-LG")
        assert "git" in out
        assert "vim" in out

    def test_deactivate_activate_dry_run(self, cli_env):
        _run("-G#Dev#{git}")
        code, _ = _run("--dry-run", "-A#Dev")
        assert code == 0
        code, _ = _run("--dry-run", "-E#Dev")
        assert code == 0
        code, _ = _run("--dry-run", "-A#Dev")
        assert code == 0

    def test_obliterate_group(self, cli_env):
        _run("-G#Dev#{git}")
        code, _ = _run("-O#Dev")
        assert code == 0
        code, out = _run("-LG")
        assert "Dev" not in out or "(no groups)" in out

    def test_yield_group(self, cli_env):
        code, _ = _run("-Y#Remote")
        assert code == 0
        code, out = _run("-LG")
        assert "Remote" in out

    def test_remove_group_packages(self, cli_env):
        _run("-G#Dev#{python,gcc,git}")
        code, _ = _run("-RG#Dev python")
        assert code == 0
        code, out = _run("-LG")
        assert "python" not in out

    def test_add_empty_slot_in_ordered(self, cli_env):
        code, _ = _run("-G{A,B}##{python}{}")
        assert code == 0
        code, out = _run("-LG")
        assert "python" in out

    def test_add_broadcast_plus_ordered(self, cli_env):
        code, _ = _run("-G#{A,B}#{common}#{server}{workstation}")
        assert code == 0
        code, out = _run("-LG")
        assert "common" in out
        assert "server" in out
        assert "workstation" in out

    def test_overflow_rejected(self, cli_env):
        code, _ = _run("-G#{A,B}##{1}{2}{3}")
        assert code == 1


# ---------------------------------------------------------------------------
# DSL install with --dry-run (does not touch profile)
# ---------------------------------------------------------------------------


class TestDslInstallDryRun:
    def test_install_dry_run(self, cli_env):
        code, _ = _run("--dry-run", "-IG#Dev#{python,gcc}")
        assert code == 0

    def test_install_positional_dry_run(self, cli_env):
        code, _ = _run("--dry-run", "-IG{A,B}##{python}{gcc}")
        assert code == 0

    def test_install_pipe_dry_run(self, cli_env):
        code, _ = _run("--dry-run", "-IG#Dev#{git|vim}")
        assert code == 0

    def test_install_exclamation_dry_run(self, cli_env):
        code, _ = _run("--dry-run", "-IG#Dev#{git!vim}")
        assert code == 0


# ---------------------------------------------------------------------------
# Query operations via CLI
# ---------------------------------------------------------------------------


class TestQueryCli:
    def test_list_summary(self, cli_env):
        _run("-G#Dev#{git}")
        code, out = _run("-L")
        assert code == 0
        assert "GROUP" in out
        assert "Dev" in out

    def test_list_tree(self, cli_env):
        _run("-G#Dev#{git,vim}")
        code, out = _run("-LG")
        assert code == 0
        assert "git" in out
        assert "vim" in out

    def test_find_by_name(self, cli_env):
        _run("-G#Programming#{python}")
        code, out = _run("-Fname=%prog%")
        assert code == 0
        assert "Programming" in out

    def test_find_by_content(self, cli_env):
        _run("-G#Programming#{python}")
        code, out = _run("-F-python-")
        assert code == 0
        assert "Programming" in out

    def test_find_no_match(self, cli_env):
        _run("-G#Programming#{python}")
        code, out = _run("-F-gcc-")
        assert code == 0
        assert "(no matching groups)" in out

    def test_validate(self, cli_env):
        _run("-G#Dev#{git}")
        code, out = _run("-V#Dev")
        assert code == 0
        assert "Dev" in out

    def test_version(self, cli_env):
        code, out = _run("version")
        assert code == 0
        assert "nixorcist" in out

    def test_help(self, cli_env):
        code, out = _run("--help")
        assert code == 0

    def test_empty_command(self, cli_env):
        code, _ = _run()
        assert code == 0


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_mutate_manifest(self, cli_env):
        code, _ = _run("--dry-run", "-G#Dev#{python}")
        assert code == 0
        code, out = _run("-L")
        assert "Dev" not in out

    def test_dry_run_does_not_mutate_groups(self, cli_env):
        _run("-G#Existing#{git}")
        code, _ = _run("--dry-run", "-G#New#{python}")
        assert code == 0
        code, out = _run("-LG")
        assert "Existing" in out
        assert "New" not in out

    def test_dry_run_promote(self, cli_env):
        _write_config(cli_env)
        os.environ["NIXORCIST_CONFIG_ROOT"] = str(cli_env)
        try:
            _run("-G#Dev#{git}")
            code, _ = _run("--dry-run", "-P#Dev")
            assert code == 0
        finally:
            os.environ.pop("NIXORCIST_CONFIG_ROOT", None)


# ---------------------------------------------------------------------------
# Export / Import round-trip
# ---------------------------------------------------------------------------


class TestExportImport:
    def test_export_toml(self, cli_env):
        _run("-G#Dev#{python,gcc}")
        code, out = _run("export", "Dev")
        assert code == 0
        assert "python" in out

    def test_export_json(self, cli_env):
        _run("-G#Dev#{python}")
        code, out = _run("export", "--format", "json", "Dev")
        assert code == 0
        assert '"python"' in out

    def test_import_roundtrip(self, cli_env):
        _run("-G#Dev#{python,gcc}")
        export_out = StringIO()
        with redirect_stdout(export_out):
            main(["export", "Dev"])
        toml_content = export_out.getvalue()

        import_file = cli_env / "exported.toml"
        import_file.write_text(toml_content, "utf-8")
        _run("group", "remove", "Dev")
        code, _ = _run("import", str(import_file))
        assert code == 0
        code, out = _run("-LG")
        assert "python" in out

    def test_export_all(self, cli_env):
        _run("-G#A#{x}")
        _run("-G#B#{y}")
        code, out = _run("export")
        assert code == 0
        assert "A" in out
        assert "B" in out


# ---------------------------------------------------------------------------
# Promote/demote with config root (dry-run)
# ---------------------------------------------------------------------------


class TestPromoteDemote:
    def test_promote_demote_cycle_dry_run(self, cli_env):
        _write_config(cli_env)
        os.environ["NIXORCIST_CONFIG_ROOT"] = str(cli_env)
        try:
            _run("-G#Dev#{git}")
            code, _ = _run("--dry-run", "-P#Dev")
            assert code == 0

            code, _ = _run("--dry-run", "-D#Dev")
            assert code == 0
        finally:
            os.environ.pop("NIXORCIST_CONFIG_ROOT", None)

    def test_promote_dry_run(self, cli_env):
        _write_config(cli_env)
        os.environ["NIXORCIST_CONFIG_ROOT"] = str(cli_env)
        try:
            _run("-G#Dev#{git}")
            code, _ = _run("--dry-run", "-P#Dev")
            assert code == 0
        finally:
            os.environ.pop("NIXORCIST_CONFIG_ROOT", None)
