"""Tests for the installation method feature (spec §90).

Installation method determines how a package is installed:
- ``imperative``: via ``nix profile install`` (default)
- ``declarative``: via NixOS configuration (promoted)
- ``auto``: decide based on group backend state

The ``-M`` flag sets the installation method for packages being added.
"""

from __future__ import annotations

import pytest

from nixorcist.cli.ast import InstallMethod
from nixorcist.cli.parser import parse
from nixorcist.core.models import ResolvedPackage
from nixorcist.core.planner import Plan, plan, _should_install_imperatively
from nixorcist.core.models import Backend


class TestInstallMethodParsing:
    """Test that -M flag is parsed correctly."""

    def test_method_imperative(self):
        cmd = parse("-IM imperative python gcc")
        assert cmd.install_method == InstallMethod.IMPERATIVE

    def test_method_declarative(self):
        cmd = parse("-IM declarative python gcc")
        assert cmd.install_method == InstallMethod.DECLARATIVE

    def test_method_auto(self):
        cmd = parse("-IM auto python gcc")
        assert cmd.install_method == InstallMethod.AUTO

    def test_method_long_flag(self):
        cmd = parse("--install --method declarative python")
        assert cmd.install_method == InstallMethod.DECLARATIVE

    def test_method_with_group(self):
        cmd = parse("-IG -M declarative #Dev#{python,gcc}")
        assert cmd.install_method == InstallMethod.DECLARATIVE

    def test_method_with_group_multiple(self):
        cmd = parse("-IG -M declarative #{Programming Gaming}#{git steam}")
        assert cmd.install_method == InstallMethod.DECLARATIVE
        assert [g.name for g in cmd.groups] == ["Programming", "Gaming"]
        assert [p.name for p in cmd.explicit_packages()] == ["git", "steam"]

    def test_method_invalid(self):
        from nixorcist.cli.diagnostics import NixorcistError
        with pytest.raises(NixorcistError):
            parse("-IM invalid python")


class TestInstallMethodInResolvedPackage:
    """Test that install_method is stored in ResolvedPackage."""

    def test_default_install_method(self):
        pkg = ResolvedPackage(requested="python", attribute="python3")
        assert pkg.install_method == InstallMethod.IMPERATIVE

    def test_declarative_install_method(self):
        pkg = ResolvedPackage(
            requested="python",
            attribute="python3",
            install_method=InstallMethod.DECLARATIVE,
        )
        assert pkg.install_method == InstallMethod.DECLARATIVE

    def test_to_dict_includes_install_method(self):
        pkg = ResolvedPackage(
            requested="steam",
            attribute="steam",
            install_method=InstallMethod.DECLARATIVE,
        )
        d = pkg.to_dict()
        assert d["install_method"] == "declarative"

    def test_from_dict_with_install_method(self):
        data = {
            "name": "steam",
            "attribute": "steam",
            "install_method": "declarative",
        }
        pkg = ResolvedPackage.from_dict(data)
        assert pkg.install_method == InstallMethod.DECLARATIVE

    def test_from_dict_default_install_method(self):
        data = {"name": "python", "attribute": "python3"}
        pkg = ResolvedPackage.from_dict(data)
        assert pkg.install_method == InstallMethod.IMPERATIVE


class TestShouldInstallImperatively:
    """Test the _should_install_imperatively helper function."""

    def test_imperative_method_always_installs(self):
        pkg = ResolvedPackage(
            requested="python", attribute="python3", install_method=InstallMethod.IMPERATIVE
        )
        assert _should_install_imperatively(pkg) == True
        assert _should_install_imperatively(pkg, Backend.DECLARATIVE) == True
        assert _should_install_imperatively(pkg, Backend.IMPERATIVE) == True

    def test_declarative_method_never_installs(self):
        pkg = ResolvedPackage(
            requested="steam", attribute="steam", install_method=InstallMethod.DECLARATIVE
        )
        assert _should_install_imperatively(pkg) == False
        assert _should_install_imperatively(pkg, Backend.DECLARATIVE) == False
        assert _should_install_imperatively(pkg, Backend.IMPERATIVE) == False

    def test_auto_method_installs_when_not_declarative(self):
        pkg = ResolvedPackage(
            requested="git", attribute="git", install_method=InstallMethod.AUTO
        )
        assert _should_install_imperatively(pkg) == True
        assert _should_install_imperatively(pkg, Backend.IMPERATIVE) == True
        assert _should_install_imperatively(pkg, Backend.NONE) == True

    def test_auto_method_skips_when_declarative(self):
        pkg = ResolvedPackage(
            requested="git", attribute="git", install_method=InstallMethod.AUTO
        )
        assert _should_install_imperatively(pkg, Backend.DECLARATIVE) == False


class TestPlannerInstallMethodFiltering:
    """Test that the planner filters packages by installation method."""

    def test_imperative_method_installs_all(self, mgr, package):
        """All packages with imperative method should be installed."""
        cmd = parse("-IM imperative python gcc")
        pl = plan(cmd, mgr, mgr.resolver)
        assert {p.requested for p in pl.install} == {"python", "gcc"}

    def test_declarative_method_installs_none(self, mgr, package):
        """No packages with declarative method should be installed."""
        cmd = parse("-IM declarative python gcc")
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl.install == []
        # Should have notes about deferred packages
        assert len(pl.notes) == 2
        assert all("[deferred]" in n for n in pl.notes)

    def test_auto_method_installs_when_no_group(self, mgr, package):
        """Auto method installs when there's no group context."""
        cmd = parse("-IM auto python gcc")
        pl = plan(cmd, mgr, mgr.resolver)
        assert {p.requested for p in pl.install} == {"python", "gcc"}

    def test_auto_method_skips_when_group_is_declarative(self, mgr, package):
        """Auto method skips when group backend is declarative."""
        mgr.create("Dev")
        mgr.activate("Dev", Backend.DECLARATIVE)
        cmd = parse("-IG -M auto #Dev#{python,gcc}")
        pl = plan(cmd, mgr, mgr.resolver)
        # Packages should be added to group but not installed
        assert "Dev" in pl.add_to_groups
        assert pl.install == []

    def test_mixed_methods_in_group(self, mgr, package):
        """Mix of imperative and declarative packages in same group."""
        mgr.create("Dev")
        # Add packages with different methods
        imperative_pkg = ResolvedPackage(
            requested="python", attribute="python3", install_method=InstallMethod.IMPERATIVE
        )
        declarative_pkg = ResolvedPackage(
            requested="steam", attribute="steam", install_method=InstallMethod.DECLARATIVE
        )
        mgr.add("Dev", [imperative_pkg, declarative_pkg])
        
        # Install from group - only imperative should be installed
        cmd = parse("-I#Dev")
        pl = plan(cmd, mgr, mgr.resolver)
        assert {p.requested for p in pl.install} == {"python"}


class TestManifestSerialization:
    """Test that install_method is serialized/deserialized correctly."""

    def test_roundtrip_imperative(self):
        from nixorcist.groups.manifest import GroupManifest, serialize, parse
        from nixorcist.core.models import GroupState, Backend
        
        manifest = GroupManifest(
            name="Dev",
            packages=[
                ResolvedPackage(
                    requested="python", attribute="python3", install_method=InstallMethod.IMPERATIVE
                )
            ],
            state=GroupState(active=False, backend=Backend.NONE),
        )
        text = serialize(manifest)
        parsed = parse(text)
        assert parsed.packages[0].install_method == InstallMethod.IMPERATIVE

    def test_roundtrip_declarative(self):
        from nixorcist.groups.manifest import GroupManifest, serialize, parse
        from nixorcist.core.models import GroupState, Backend
        
        manifest = GroupManifest(
            name="Dev",
            packages=[
                ResolvedPackage(
                    requested="steam", attribute="steam", install_method=InstallMethod.DECLARATIVE
                )
            ],
            state=GroupState(active=False, backend=Backend.NONE),
        )
        text = serialize(manifest)
        parsed = parse(text)
        assert parsed.packages[0].install_method == InstallMethod.DECLARATIVE
        # Verify it's in the TOML
        assert "install_method = \"declarative\"" in text

    def test_roundtrip_auto(self):
        from nixorcist.groups.manifest import GroupManifest, serialize, parse
        from nixorcist.core.models import GroupState, Backend
        
        manifest = GroupManifest(
            name="Dev",
            packages=[
                ResolvedPackage(
                    requested="git", attribute="git", install_method=InstallMethod.AUTO
                )
            ],
            state=GroupState(active=False, backend=Backend.NONE),
        )
        text = serialize(manifest)
        parsed = parse(text)
        assert parsed.packages[0].install_method == InstallMethod.AUTO
        # Verify it's in the TOML
        assert "install_method = \"auto\"" in text


class TestCLIIntegration:
    """Test the CLI with -M flag."""

    def test_cli_method_flag(self, cli_env):
        from nixorcist.cli.entry import main
        
        code = main(["--dry-run", "-IM", "declarative", "python", "gcc"])
        assert code == 0

    def test_cli_method_with_group(self, cli_env):
        from nixorcist.cli.entry import main
        
        code = main(["-IG", "-M", "declarative", "#Dev#{python,gcc}"])
        assert code == 0
        code, out = _run("-LG")
        assert "Dev" in out
        assert "python" in out
        assert "gcc" in out


def _run(*argv):
    from nixorcist.cli.entry import main
    from contextlib import redirect_stdout
    from io import StringIO
    
    out = StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


@pytest.fixture
def cli_env(tmp_path):
    import os
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
