"""Planner unit tests: plan() against all AST operations.

The planner runs fully offline via identity resolver; it never invokes Nix.
The ``notes`` list (UnboundLocalError fix) and ``ensure_groups`` must be
populated in every code path through the if/elif chain.
"""

from __future__ import annotations

import pytest

from nixorcist.cli.parser import parse
from nixorcist.core.planner import Plan, plan


def _run(mgr, expr):
    """Parse expr, plan, return Plan."""
    return plan(parse(expr), mgr, mgr.resolver)


class TestInstallPlanner:
    def test_bare_names_plan_install(self, mgr, package):
        cmd = parse("-I python gcc")
        pl = plan(cmd, mgr, mgr.resolver)
        assert {p.requested for p in pl.install} == {"python", "gcc"}

    def test_group_scope_installs_existing_packages(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python"), package("gcc")])
        pl = _run(mgr, "-I#Programming")
        assert {p.requested for p in pl.install} == {"python", "gcc"}

    def test_IG_broadcast_adds_packages_to_group(self, mgr, package):
        pl = _run(mgr, "-IG#Programming#{java,javac}")
        assert "Programming" in pl.add_to_groups
        assert {p.requested for p in pl.add_to_groups["Programming"]} == {"java", "javac"}

    def test_IG_broadcast_creates_missing_group(self, mgr, package):
        pl = _run(mgr, "-IG#NewGroup#{python}")
        assert "NewGroup" in pl.add_to_groups
        assert pl.ensure_groups == ["NewGroup"]

    def test_IG_ordered_adds_per_group(self, mgr, package):
        pl = _run(mgr, "-IG{A,B}##{python}{gcc}")
        assert "A" in pl.add_to_groups
        assert "B" in pl.add_to_groups
        assert {p.requested for p in pl.add_to_groups["A"]} == {"python"}
        assert {p.requested for p in pl.add_to_groups["B"]} == {"gcc"}

    def test_notes_populated(self, mgr, package):
        # The notes list must be initialized even for install path
        pl = _run(mgr, "-I python")
        assert isinstance(pl.notes, list)


class TestAddPlanner:
    def test_add_broadcast_to_existing(self, mgr, package):
        mgr.create("Programming")
        pl = _run(mgr, "-G#Programming#{vim}")
        assert {p.requested for p in pl.add_to_groups["Programming"]} == {"vim"}

    def test_add_all_groups_broadcast(self, mgr, package):
        mgr.create("A")
        mgr.create("B")
        pl = _run(mgr, "-G* {git}")
        assert "A" in pl.add_to_groups and "B" in pl.add_to_groups
        assert {p.requested for p in pl.add_to_groups["A"]} == {"git"}

    def test_add_creates_multiple_missing_groups(self, mgr, package):
        pl = _run(mgr, "-G#{X,Y}#{git}")
        assert pl.ensure_groups == ["X", "Y"]


class TestRemovePlanner:
    def test_remove_profile_packages(self, mgr, package):
        pl = _run(mgr, "-R#.{python,gcc}")
        assert sorted(pl.remove_from_profile) == ["gcc", "python"]

    def test_remove_group_packages(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python"), package("gcc")])
        pl = _run(mgr, "-RG#Programming python")
        assert pl.remove_from_groups["Programming"] == ["python"]


class TestPlannerConsistency:
    def test_install_noop_on_empty_package_list(self, mgr, package):
        # A bare -G#G creates an empty group without installing anything
        pl = _run(mgr, "-G#G")
        assert pl.add_to_groups == {}
        assert pl.install == []

    def test_plan_result_is_plan_instance(self, mgr, package):
        assert isinstance(_run(mgr, "-I python"), Plan)

    def test_persist_manifest_on_add(self, mgr, package):
        pl = _run(mgr, "-IG#G#{python}")
        assert pl.persist_manifest is True

    def test_unknown_group_scope_raises_group_error(self, mgr, package):
        from nixorcist.groups.manager import GroupError

        with pytest.raises(GroupError):
            _run(mgr, "-I#NoSuchGroup")


class TestLifecyclePlanner:
    def test_obliterate_existing(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-O#Programming")
        assert pl.obliterate == ["Programming"]
        assert pl.obliterate_declaration is False

    def test_obliterate_o_count_1_declaration(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-Oo#Programming")
        assert pl.obliterate_declaration is True
        assert pl.obliterate_orphan_sweep is False

    def test_obliterate_o_count_2_orphan_sweep(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-Ooo#Programming")
        assert pl.obliterate_declaration is True
        assert pl.obliterate_orphan_sweep is True

    def test_obliterate_save(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-Os#Programming")
        assert pl.obliterate_save is True

    def test_obliterate_all_groups(self, mgr):
        mgr.create("A")
        mgr.create("B")
        pl = _run(mgr, "-O#*")
        assert sorted(pl.obliterate) == ["A", "B"]

    def test_obliterate_all_groups_empty(self, mgr):
        pl = _run(mgr, "-O#*")
        assert pl.obliterate == []

    def test_yield_group(self, mgr):
        pl = _run(mgr, "-Y#Games")
        assert pl.yield_groups == [("Games", [])]

    def test_yield_orphan_sweep_flag(self, mgr):
        pl = _run(mgr, "-Yyy#Games")
        assert pl.yield_orphan_sweep is True

    def test_obliterate_needs_declarative_work(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-Oo#Programming")
        assert pl.needs_declarative_work() is True

    def test_obliterate_registry_only_no_declarative(self, mgr):
        mgr.create("Programming")
        pl = _run(mgr, "-O#Programming")
        assert pl.needs_declarative_work() is False

    def test_yield_all_orphans_needs_declarative(self, mgr):
        pl = _run(mgr, "-Yyy#Games")
        assert pl.needs_declarative_work() is True

    def test_steps_include_obliterate(self, mgr):
        mgr.create("G")
        pl = _run(mgr, "-O#G")
        steps = pl.steps()
        assert any("obliterate" in s for s in steps)

    def test_steps_include_yield(self, mgr):
        pl = _run(mgr, "-Y#G")
        steps = pl.steps()
        assert any("yield" in s for s in steps)