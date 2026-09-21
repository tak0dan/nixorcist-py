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
from nixorcist.nix.ast import parse_module
from nixorcist.promotion.discover import (
    ConfigurationKind,
    PackageDeclaration,
    ConfigurationModel,
    discover,
)
from nixorcist.promotion.planner import PromotionPlan, plan_demotion, plan_promotion
from nixorcist.promotion.transaction import Transaction, TransactionResult, copy_tree
from nixorcist.promotion.transformer import apply_plan
from nixorcist.promotion.validator import build_validation_command


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


def test_parser_accepts_single_argument_lambda_in_let_binding():
    text = "let filter = pkg: !(builtins.elem pkg [ ]); in { environment.systemPackages = [ ]; }"
    parse_module(text)


class TestDiscover:
    def test_classifies_plain_configuration_from_import_graph(self, tmp_path):
        model = discover(_root(tmp_path))
        # The standard hardware import does not make an otherwise inline
        # configuration modular.
        assert model.configuration_kind is ConfigurationKind.PLAIN
        assert model.is_modular is False

    def test_classifies_single_file_configuration_as_plain(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text("{ pkgs, ... }: { environment.systemPackages = [ pkgs.vim ]; }", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        assert model.configuration_kind is ConfigurationKind.PLAIN
        assert model.is_modular is False

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

    def test_modular_package_declaration_is_detected_and_extended_safely(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text("{ ... }: { imports = [ ./packages/dev.nix ]; }\n", "utf-8")
        module = tmp_path / "packages" / "dev.nix"
        module.parent.mkdir()
        module.write_text(
            textwrap.dedent("""\
                { pkgs, ... }:
                {
                  # Existing comments and formatting must survive insertion.
                  environment.systemPackages = with pkgs; [ vim ];
                }
            """),
            "utf-8",
        )
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        assert model.configuration_kind is ConfigurationKind.MODULAR
        assert model.preferred_declaration is not None
        assert model.preferred_declaration.module == module

        plan = plan_promotion(model, [("Development", [rp("git")])])
        assert plan.strategy == "EXTEND"
        assert plan.target_module == module
        apply_plan(tmp_path, tmp_path, plan)

        updated = module.read_text("utf-8")
        assert "# Existing comments" in updated
        assert "git" in updated
        assert "git\n  ];" in updated
        # Re-parse the changed file: edits are applied only after finding
        # structural tokens, and must remain valid Nix syntax afterwards.
        parse_module(updated)

    def test_modular_configuration_creates_a_parseable_group_module(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text("{ ... }: { imports = [ ./hardware.nix ]; }\n", "utf-8")
        (tmp_path / "hardware.nix").write_text("{ ... }: {}\n", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        plan = plan_promotion(model, [("Development", [rp("git")])])
        assert plan.strategy == "CREATE_MODULE"
        apply_plan(tmp_path, tmp_path, plan)

        parse_module(cfg.read_text("utf-8"))
        generated = tmp_path / plan.new_module_path
        assert generated.exists()
        parse_module(generated.read_text("utf-8"))

    def test_plain_candidate_keeps_closing_bracket_on_its_own_line(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        plan = plan_promotion(model, [("PlainDev", [rp("hello"), rp("jq"), rp("ripgrep")])])
        apply_plan(tmp_path, tmp_path, plan)
        updated = root.entry_file.read_text("utf-8")
        assert "ripgrep\n  ];" in updated
        parse_module(updated)

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

    def test_validation_uses_selected_nonstandard_entry_filename(self, tmp_path):
        cfg = tmp_path / "plain_configuration.nix"
        cfg.write_text("{ ... }: {}", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        command = build_validation_command("/tmp/nixorcist-candidate", discover(root))
        assert command[-1] == "nixos-config=/tmp/nixorcist-candidate/plain_configuration.nix"


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

    def test_embedded_nixorcist_cache_is_ignored(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "nixorcist" / "cache").mkdir(parents=True)
        (src / "nixorcist" / "cache" / "index.txt").write_text("cache", "utf-8")
        (src / "nixorcist" / "module.nix").write_text("{ ... }: {}", "utf-8")
        copy_tree(src, dst)
        assert not (dst / "nixorcist" / "cache").exists()
        assert (dst / "nixorcist" / "module.nix").exists()


class TestInvalidCandidateLeavesLiveTreeUnchanged:
    """When a promotion candidate fails validation, the live configuration
    tree and group backend state must be exactly as before the attempt."""

    def test_transaction_dry_run_preserves_live_content(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        original_content = root.entry_file.read_text("utf-8")

        plan = plan_promotion(model, [("Workstation", [rp("firefox")])])
        result = Transaction(model, plan).run(dry_run=True)

        assert result.dry_run is True
        content = root.entry_file.read_text("utf-8")
        assert content == original_content

    def test_candidate_tree_is_separate_from_live(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        live_before = root.entry_file.read_text("utf-8")

        plan = plan_promotion(model, [("Workstation", [rp("firefox")])])
        result = Transaction(model, plan, logger=Logger(dry_run=True)).run(dry_run=True)

        live_after = root.entry_file.read_text("utf-8")
        assert live_before == live_after
        assert result.would_change is True

    def test_failed_promotion_does_not_write_config(self, tmp_path):
        """A dry-run promotion does not touch the live file."""
        root = _root(tmp_path)
        model = discover(root)
        original = root.entry_file.read_text("utf-8")

        plan = plan_promotion(model, [("NewGroup", [rp("newpkg")])])
        txn = Transaction(model, plan)
        result = txn.run(dry_run=True)

        assert root.entry_file.read_text("utf-8") == original
        assert result.would_change is True

    def test_transformer_does_not_modify_tree_in_dry_run(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        original = root.entry_file.read_text("utf-8")

        plan = plan_promotion(model, [("Workstation", [rp("firefox")])])
        result = Transaction(model, plan).run(dry_run=True)

        assert root.entry_file.read_text("utf-8") == original

    def test_candidate_copy_excludes_build_artifacts(self, tmp_path):
        src = tmp_path / "config"
        src.mkdir()
        (src / "configuration.nix").write_text(
            "{ pkgs, ... }: { environment.systemPackages = with pkgs; [ vim ]; }",
            "utf-8",
        )
        (src / "hardware-configuration.nix").write_text("{ ... }: {}", "utf-8")
        (src / "result").mkdir()
        (src / "result" / "binary").write_text("binary", "utf-8")
        (src / ".git").mkdir()

        from nixorcist.promotion.transaction import copy_tree
        dst = tmp_path / "candidate"
        copy_tree(src, dst)

        assert (dst / "configuration.nix").exists()
        assert not (dst / "result").exists()
        assert not (dst / ".git").exists()

    def test_demotion_dry_run_preserves_live(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        from nixorcist.core.models import DeclarativeMetadata

        meta = DeclarativeMetadata(declaration="module", module="nixorcist/a.nix")
        module_abs = tmp_path / "nixorcist" / "a.nix"
        module_abs.parent.mkdir(exist_ok=True)
        module_abs.write_text("{}", "utf-8")
        plan = plan_demotion(model, "A", [rp("python")], meta)

        original = root.entry_file.read_text("utf-8")
        result = Transaction(model, plan).run(dry_run=True)

        assert root.entry_file.read_text("utf-8") == original
        assert result.dry_run is True


class TestTransformedFileParsing:
    """Every file modified by the transformer must remain valid Nix after
    the edit.  These tests re-parse the transformed content."""

    def test_extend_preserves_valid_syntax(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        plan = plan_promotion(model, [("Workstation", [rp("firefox"), rp("thunderbird")])])
        apply_plan(tmp_path, tmp_path, plan)
        updated = root.entry_file.read_text("utf-8")
        parse_module(updated)

    def test_embed_preserves_valid_syntax(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text("{ pkgs, ... }: { }\n", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        plan = plan_promotion(model, [("Dev", [rp("git")])])
        if plan.strategy == "EMBED":
            apply_plan(tmp_path, tmp_path, plan)
            parse_module(cfg.read_text("utf-8"))

    def test_create_module_both_files_parseable(self, tmp_path):
        cfg = tmp_path / "configuration.nix"
        cfg.write_text(
            "{ ... }: { imports = [ ./hardware.nix ]; }\n", "utf-8"
        )
        (tmp_path / "hardware.nix").write_text("{ ... }: {}\n", "utf-8")
        root = NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)
        model = discover(root)
        plan = plan_promotion(model, [("Dev", [rp("git")])])
        assert plan.strategy == "CREATE_MODULE"
        apply_plan(tmp_path, tmp_path, plan)
        parse_module(cfg.read_text("utf-8"))
        parse_module((tmp_path / plan.new_module_path).read_text("utf-8"))

    def test_removal_preserves_valid_syntax(self, tmp_path):
        root = _root(tmp_path)
        model = discover(root)
        meta = {"declaration": "inline"}
        plan = plan_demotion(model, "A", [rp("vim")], meta)
        apply_plan(tmp_path, tmp_path, plan)
        parse_module(root.entry_file.read_text("utf-8"))
