"""Promotion tests: discovery, planning, and transaction dry-run.

All tests use a synthetic NixOS configuration root in tmp_path.
No Nix evaluation happens; ``nixos-rebuild`` is never called.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nixorcist.core.models import ResolvedPackage
from nixorcist.logging import Logger
from nixorcist.nix.config import NixOSRoot
from nixorcist.promotion.discover import PackageDeclaration, ConfigurationModel, discover
from nixorcist.promotion.planner import PromotionPlan, plan_demotion, plan_promotion
from nixorcist.promotion.transaction import Transaction, TransactionResult, copy_tree


def rp(name, attr=None):
    return ResolvedPackage(requested=name, attribute=attr or name)


def _write_config(tmp_path, content=None):
    if content is None:
        content = textwrap.dedent("""\
            { pkgs, ... }:
            {
              imports = [ ./hardware-configuration.nix ];

              environment.systemPackages = with pkgs; [
                vim
                git
              ];
            }
        """)
    cfg = tmp_path / "configuration.nix"
    cfg.write_text(content, "utf-8")
    hw = tmp_path / "hardware-configuration.nix"
    hw.write_text("{ ... }: {}", "utf-8")
    return cfg, hw


def _root(tmp_path, content=None):
    cfg, _ = _write_config(tmp_path, content)
    return NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)


class TestDiscover:
    def test_finds_system_packages(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        assert len(model.declarations) >= 1
        assert model.declarations[0].attrpath == ("environment", "systemPackages")

    def test_empty_config_returns_no_declarations(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text("{ pkgs, ... }: { }", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        assert model.declarations == []

    def test_existing_packages(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        decl = model.preferred_declaration
        assert decl is not None
        existing = model.discover_existing_packages()
        pkg_names = {p.requested for p in existing.get(decl.attrpath, [])}
        assert "vim" in pkg_names

    def test_preferred_declaration_is_in_entry_file(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        assert model.preferred_declaration.module == root.entry_file


class TestPromotionPlan:
    def test_extend_strategy(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        pp = plan_promotion(model, [("Workstation", [rp("firefox"), rp("thunderbird")])])
        assert pp.strategy == "EXTEND"
        assert len(pp.element_snippets) == 2
        assert "firefox" in pp.element_snippets

    def test_create_module_strategy_when_no_existing_list(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text(
            '{ pkgs, ... }: { imports = [ ./hardware-configuration.nix ]; }\n',
            "utf-8",
        )
        hw = tmp_path / "hardware-configuration.nix"
        hw.write_text("{ ... }: {}", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        assert model.declarations == []
        pp = plan_promotion(model, [("A", [rp("python")])])
        assert pp.strategy == "CREATE_MODULE"
        assert pp.new_module_content is not None

    def test_demotion_module_strategy(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        from nixorcist.core.models import DeclarativeMetadata

        meta = DeclarativeMetadata(declaration="module", module="nixorcist/a.nix")
        module_abs = tmp_path / "nixorcist" / "a.nix"
        module_abs.parent.mkdir(exist_ok=True)
        module_abs.write_text("{}", "utf-8")
        pp = plan_demotion(model, "A", [rp("python")], meta)
        assert pp.strategy == "REMOVE_MODULE"


class TestTransactionDryRun:
    def test_dry_run_returns_changed_files_without_mutating(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        pp = plan_promotion(model, [("Workstation", [rp("firefox")])])
        txn = Transaction(model, pp)
        result = txn.run(dry_run=True)
        assert result.dry_run is True
        assert isinstance(result.changed_files, list)
        # config.nix must not have changed
        content = root.entry_file.read_text("utf-8")
        assert "vim" in content

    def test_result_is_transaction_result(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        pp = plan_promotion(model, [("A", [rp("python")])])
        result = Transaction(model, pp).run(dry_run=True)
        assert isinstance(result, TransactionResult)
        assert result.would_change is True


class TestOrphanSweep:
    """The -Ooo orphan sweep (spec §38): declarations no group owns are
    removed from the configuration by the promotion subsystem."""

    def _owned_decl(self, model):
        decl = model.preferred_declaration
        assert decl is not None
        return decl, {(decl.module, decl.attrpath)}

    def test_owned_declaration_is_not_orphan(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        decl, owned = self._owned_decl(model)
        assert model.orphan_declarations(owned) == []

    def test_unowned_declaration_is_orphan(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        orphans = model.orphan_declarations(set())
        assert len(orphans) == 1
        assert isinstance(orphans[0], PackageDeclaration)
        assert orphans[0].attrpath == ("environment", "systemPackages")

    def test_two_declarations_one_orphan(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text(
            textwrap.dedent("""\
                { pkgs, ... }:
                { imports = [ ./hardware-configuration.nix ];
                  environment.systemPackages = with pkgs; [ vim ];
                  home.packages = with pkgs; [ firefox ];
                }
            """),
            "utf-8",
        )
        hw = tmp_path / "hardware-configuration.nix"
        hw.write_text("{ ... }: {}", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        owned = {("(module)", d.attrpath) for d in model.declarations}
        owned.clear()
        # Claim only the systemPackages declaration -> home.packages is orphan.
        sysp = ("environment", "systemPackages")
        claim = next(
            (d.module, d.attrpath)
            for d in model.declarations
            if d.attrpath == sysp
        )
        orphans = model.orphan_declarations({claim})
        assert [o.attrpath for o in orphans] == [("home", "packages")]

    def test_orphan_demotion_plans_removal(self, tmp_path):
        """An orphaned declaration demotes as REMOVE_ELEMENTS on the entry."""
        root = _root(tmp_path)
        model = discover(root)
        orphans = model.orphan_declarations(set())
        assert len(orphans) == 1
        pp = plan_demotion(model, "Orphan", [rp("vim")], {"declaration": "inline"})
        assert pp.strategy == "REMOVE_ELEMENTS"
        assert "vim" in pp.remove_elements

    def test_orphan_plan_works_in_dry_run_transaction(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        meta = {
            "declaration": "inline",
            "module": "nixorcist/orphan.nix",
        }
        pp = plan_demotion(model, "Orphan", [rp("vim")], meta)
        result = Transaction(model, pp).run(dry_run=True)
        assert result.dry_run is True
        assert result.would_change is True
        # live configuration is untouched
        content = root.entry_file.read_text("utf-8")
        assert "vim" in content


class TestCopyTree:
    def test_copy_tree_matches(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "sub").mkdir(parents=True)
        (src / "configuration.nix").write_text("{}", "utf-8")
        (src / "sub" / "mod.nix").write_text("{ ... }: {}", "utf-8")
        copy_tree(src, dst)
        assert (dst / "configuration.nix").read_text() == "{}"
        assert (dst / "sub" / "mod.nix").exists()

    def test_git_dir_is_ignored(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / ".git").mkdir(parents=True)
        (src / ".git" / "config").write_text("ignored", "utf-8")
        (src / "configuration.nix").write_text("ok", "utf-8")
        copy_tree(src, dst)
        assert not (dst / ".git").exists()
        assert (dst / "configuration.nix").exists()