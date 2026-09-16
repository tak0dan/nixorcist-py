"""Group Manager tests for the corrected orthogonal state model.

The corrected model has two independent dimensions: ``active`` and
``backend``.  Promotion/demotion change ``backend`` only; -A/-E change
``active`` only; membership and profile installation are independent.
"""

from __future__ import annotations

import pytest

from nixorcist.core.models import Backend, GroupState
from nixorcist.groups.manager import GroupError


class TestGroupLifecycle:
    def test_create_returns_inactive_group(self, mgr):
        g = mgr.create("Programming")
        assert g.name == "Programming"
        assert g.state == GroupState(active=False, backend=Backend.NONE)

    def test_create_is_idempotent(self, mgr):
        mgr.create("Programming")
        again = mgr.create("Programming")
        assert again.name == "Programming"

    def test_create_then_get(self, mgr):
        mgr.create("Programming")
        assert mgr.get("Programming") is not None

    def test_get_missing(self, mgr):
        assert mgr.get("Nope") is None

    def test_list_groups_case_sorted(self, mgr):
        mgr.create("Art")
        mgr.create("music")
        assert mgr.list_groups() == ["Art", "music"]

    def test_add_packages_de_duplicates(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python"), package("python"), package("gcc")])
        g = mgr.get("Programming")
        assert {p.requested for p in g.packages} == {"python", "gcc"}

    def test_add_missing_group_raises(self, mgr, package):
        with pytest.raises(GroupError):
            mgr.add("Nope", [package("x")])

    def test_remove_packages(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python"), package("gcc")])
        g = mgr.remove("Programming", ["python"])
        assert {p.requested for p in g.packages} == {"gcc"}

    def test_delete(self, mgr):
        mgr.create("Programming")
        assert mgr.delete("Programming") is True
        assert mgr.delete("Programming") is False

    def test_rename(self, mgr):
        mgr.create("Programming")
        g = mgr.rename("Programming", "Development")
        assert g.name == "Development"
        assert mgr.get("Development") is not None

    def test_rename_invalid_name(self, mgr):
        mgr.create("Programming")
        with pytest.raises(GroupError):
            mgr.rename("Programming", "Bad{}Name")

    def test_invalid_group_name(self, mgr):
        with pytest.raises(GroupError):
            mgr.create("")

    def test_name_with_bad_chars(self, mgr):
        for bad in ("a{b", "a#b", "a,b", "a*b"):
            with pytest.raises(GroupError):
                mgr.create(bad)

    def test_name_with_bad_char_suggestion(self, mgr):
        with pytest.raises(GroupError) as exc:
            mgr.create("a#b")
        assert "invalid characters" in exc.value.diagnostic.message


class TestCaseInsensitiveLookup:
    def test_canonical_preserves_display_name(self, mgr):
        mgr.create("Programming")
        assert mgr.canonical("programming") == "Programming"
        assert mgr.canonical("PROGRAMMING") == "Programming"

    def test_get_case_insensitive(self, mgr):
        mgr.create("Programming")
        g = mgr.get("programming")
        assert g is not None and g.name == "Programming"

    def test_add_case_insensitive(self, mgr, package):
        mgr.create("Programming")
        g = mgr.add("PROGRAMMING", [package("java")])
        assert {p.requested for p in g.packages} == {"java"}


class TestOrthogonalStateMachine:
    """active x backend transitions (spec corrected model)."""

    def test_new_group_is_inactive_none(self, mgr):
        mgr.create("G")
        assert mgr.state_of("G") == GroupState(active=False, backend=Backend.NONE)

    def test_activate_sets_active_and_backend(self, mgr):
        mgr.create("G")
        g = mgr.activate("G", Backend.IMPERATIVE)
        assert g.state == GroupState(active=True, backend=Backend.IMPERATIVE)

    def test_activate_accepts_string(self, mgr):
        mgr.create("G")
        g = mgr.activate("G", "imperative")
        assert g.state.backend is Backend.IMPERATIVE

    def test_deactivate_sets_inactive_but_retains_backend(self, mgr):
        mgr.create("G")
        mgr.activate("G", Backend.DECLARATIVE)
        g = mgr.deactivate("G")
        assert g.state == GroupState(active=False, backend=Backend.DECLARATIVE)

    def test_promote_changes_backend_only(self, mgr):
        mgr.create("G")
        mgr.activate("G", Backend.IMPERATIVE)
        g = mgr.promote("G")
        assert g.state.active is True
        assert g.state.backend is Backend.DECLARATIVE

    def test_demote_changes_backend_only(self, mgr):
        mgr.create("G")
        mgr.activate("G", Backend.DECLARATIVE)
        g = mgr.demote("G")
        assert g.state.active is True
        assert g.state.backend is Backend.IMPERATIVE

    def test_promote_inactive_keeps_inactive(self, mgr):
        mgr.create("G")
        g = mgr.promote("G")
        assert g.state.active is False
        assert g.state.backend is Backend.DECLARATIVE

    def test_reactivate_after_deactivate_restores_backend(self, mgr):
        mgr.create("G")
        mgr.activate("G", Backend.DECLARATIVE)
        mgr.deactivate("G")
        g = mgr.activate("G", Backend.DECLARATIVE)
        assert g.state == GroupState(active=True, backend=Backend.DECLARATIVE)

    def test_membership_orthogonal_to_state(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("python")])
        mgr.deactivate("G")  # membership unaffected by state change
        g = mgr.get("G")
        assert {p.requested for p in g.packages} == {"python"}
        assert g.state.active is False

    def test_full_cycle_preserves_membership(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("python"), package("gcc")])
        mgr.activate("G", Backend.IMPERATIVE)
        mgr.promote("G")
        mgr.deactivate("G")
        mgr.demote("G")
        mgr.activate("G", Backend.IMPERATIVE)
        g = mgr.get("G")
        assert sorted(p.requested for p in g.packages) == ["gcc", "python"]
        assert g.state == GroupState(active=True, backend=Backend.IMPERATIVE)


class TestUnionContents:
    def test_union_dedupes_across_groups(self, mgr, package):
        mgr.create("A")
        mgr.create("B")
        mgr.add("A", [package("python")])
        mgr.add("B", [package("python"), package("gcc")])
        merged = mgr.union_contents(["A", "b"])
        assert {p.requested for p in merged} == {"python", "gcc"}

    def test_group_packages(self, mgr, package):
        mgr.create("A")
        mgr.add("A", [package("vim")])
        assert [p.requested for p in mgr.group_packages("a")] == ["vim"]


class TestObliterate:
    def test_obliterate_existing(self, mgr):
        mgr.create("Programming")
        assert mgr.obliterate("Programming") is True
        assert mgr.get("Programming") is None

    def test_obliterate_missing(self, mgr):
        assert mgr.obliterate("Nope") is False

    def test_obliterate_removes_manifest(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("python")])
        mgr.obliterate("G")
        assert mgr.list_groups() == []


class TestYieldGroup:
    def test_yield_creates_new_group(self, mgr, package):
        g = mgr.yield_group("Programming", [package("python"), package("gcc")])
        assert g.name == "Programming"
        assert sorted(p.requested for p in g.packages) == ["gcc", "python"]
        assert g.state.active is False

    def test_yield_refreshes_existing(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("old_pkg")])
        g = mgr.yield_group("Programming", [package("new_pkg")])
        assert {p.requested for p in g.packages} == {"new_pkg"}

    def test_yield_deduplicates(self, mgr, package):
        g = mgr.yield_group("G", [package("x"), package("x")])
        assert len(g.packages) == 1

    def test_yield_is_readonly_for_config(self, mgr, package):
        """Yield adds to registry only; configuration is never touched."""
        g = mgr.yield_group("G", [package("python")])
        assert g.name == "G"
        assert mgr.get("G") is not None