"""Invariant/property tests for the corrected orthogonal state model and the
spec DSL round-trip pipeline (parse -> validate -> plan)."""

from __future__ import annotations

import pytest

from nixorcist.cli.parser import parse
from nixorcist.cli.validator import validate
from nixorcist.core.models import Backend
from nixorcist.core.planner import plan


def _name(state):
    return "A" if state.active else "I"


STATE_SPACE = [(active, backend) for active in (False, True) for backend in Backend]


class TestStateSpaceInvariant:
    """Every combination (active x backend) is representable."""

    @pytest.mark.parametrize("active", [False, True], ids=lambda v: f"act={v}")
    @pytest.mark.parametrize("backend", list(Backend), ids=lambda b: b.value)
    def test_manual_set(self, mgr, active, backend):
        from nixorcist.core.models import GroupState

        mgr.create("G")
        g = mgr._mutate_state("G", lambda m: setattr(m, "state", GroupState(active=active, backend=backend)))
        assert g.state.active is active
        assert g.state.backend is backend

    def test_create_then_activate_product(self, mgr):
        mgr.create("G")
        for backend in (Backend.NONE, Backend.IMPERATIVE, Backend.DECLARATIVE):
            mgr.activate("G", backend)
            assert mgr.state_of("G").active is True
            mgr.deactivate("G")
            assert mgr.state_of("G").active is False
            assert mgr.state_of("G").backend is backend


class TestOperationPairing:
    """-A/-E toggle active; -P/-D toggle backend.  Neither touches the other
    dimension; neither touches package membership."""

    def test_AE_only_touches_active(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("x")])

        for backend in (Backend.IMPERATIVE, Backend.DECLARATIVE):
            mgr.activate("G", backend)
            before_backend = mgr.state_of("G").backend
            mgr.deactivate("G")
            assert mgr.state_of("G").backend is before_backend  # unchanged
            assert mgr.state_of("G").active is False

        g = mgr.get("G")
        assert {p.requested for p in g.packages} == {"x"}

    def test_PD_only_touches_backend(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("x")])
        mgr.activate("G", Backend.IMPERATIVE)

        mgr.promote("G")
        assert mgr.state_of("G").active is True
        assert mgr.state_of("G").backend is Backend.DECLARATIVE

        mgr.demote("G")
        assert mgr.state_of("G").active is True
        assert mgr.state_of("G").backend is Backend.IMPERATIVE

        g = mgr.get("G")
        assert {p.requested for p in g.packages} == {"x"}


class TestGroupMembershipIdempotent:
    """Adding a package twice never duplicates it (dedupe on write)."""

    def test_add_twice(self, mgr, package):
        mgr.create("G")
        mgr.add("G", [package("a")])
        mgr.add("G", [package("a")])
        assert len(mgr.get("G").packages) == 1


class TestPipeline:
    """parse -> validate -> plan executes without raising for §57 forms."""

    @pytest.mark.parametrize(
        "expr",
        [
            "-IG#Programming#{java,javac}",
            "-IG#{Programming,Games}#{git}",
            "-IG{A,B}##{python}{gcc}",
            "-IG#{A,B}#{common}#{server}{workstation}",
            "-Id#Programming",
        ],
    )
    def test_end_to_end_plans(self, mgr, expr):
        cmd = parse(expr)
        validate(cmd)
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl is not None

    def test_pipeline_for_overflow_raises_validation(self, mgr):
        with pytest.raises(Exception) as exc:
            cmd = parse("-IG#{A,B}##{1}{2}{3}")
            validate(cmd)
        assert "overflow" in str(exc.value)

    def test_ensure_groups_created_after_plan(self, mgr):
        pl = plan(parse("-IG#{X,Y}##{a}{b}"), mgr, mgr.resolver)
        assert sorted(pl.ensure_groups) == ["X", "Y"]


class TestPipelineWithExistingGroups:
    """Pipeline tests that require pre-created groups."""

    def test_I_scope(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python")])
        cmd = parse("-I#Programming")
        validate(cmd)
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl is not None
        assert {p.requested for p in pl.install} == {"python"}

    def test_Ai(self, mgr):
        mgr.create("Programming")
        cmd = parse("-Ai#Programming")
        validate(cmd)
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl is not None

    def test_Ad(self, mgr):
        mgr.create("Programming")
        cmd = parse("-Ad#Programming")
        validate(cmd)
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl is not None

    def test_PS(self, mgr, package):
        mgr.create("Programming")
        mgr.add("Programming", [package("python")])
        cmd = parse("-PS#Programming")
        validate(cmd)
        pl = plan(cmd, mgr, mgr.resolver)
        assert pl is not None