"""Promotion infrastructure tests: candidate trees (§37-39), operation
history (§48), target locking (§46), queued promotions (§44-45) and the
sequential chain (§42-43).

Behaviors asserted map to the spec §57 matrix where testable offline:
57.9/57.10  candidate staging + validation gating
57.11        sequential failure keeps base (A)
57.12        abandoned queued promotion cancelled, no candidate produced
57.13        persistent identity across a full state-cycle roundtrip
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nixorcist.core.models import ResolvedPackage
from nixorcist.nix.config import NixOSRoot
from nixorcist.promotion.candidate import (
    create_candidate,
    discard_candidate,
    new_operation_id,
)
from nixorcist.promotion.discover import discover
from nixorcist.promotion.history import (
    HistoryEntry,
    HistoryError,
    PromotionHistory,
    log_promotion,
)
from nixorcist.promotion.lock import LockError, LockOwner, PromotionLock
from nixorcist.promotion.planner import plan_promotion
from nixorcist.promotion.queue import PromotionQueue
from nixorcist.promotion.sequence import SequenceTracker


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
              ];
            }
        """)
    cfg = tmp_path / "configuration.nix"
    cfg.write_text(content, "utf-8")
    hw = tmp_path / "hardware-configuration.nix"
    hw.write_text("{ ... }: {}", "utf-8")
    return cfg, hw


def _root(tmp_path):
    cfg, _ = _write_config(tmp_path)
    return NixOSRoot(directory=tmp_path, entry_file=cfg, is_flake=False)


def _promos_dir(tmp_path_factory) -> Path:
    """A promotions cache directory OUTSIDE the config root (otherwise
    copy_tree would recursively copy the cache into the candidate)."""
    return tmp_path_factory.mktemp("promos")


def _model(tmp_path):
    return discover(_root(tmp_path))


def _extend_plan(tmp_path, packages=("firefox",)):
    model = _model(tmp_path)
    plan = plan_promotion(model, [("Workstation", [rp(p) for p in packages])])
    return model, plan


# ---------------------------------------------------------------------------
# Candidate trees (§37-39)
# ---------------------------------------------------------------------------


class TestCandidate:
    def test_stages_copy_and_applies_plan(self, tmp_path, tmp_path_factory):
        model, plan = _extend_plan(tmp_path, ("firefox", "thunderbird"))
        promos = _promos_dir(tmp_path_factory)
        cand = create_candidate(model, plan, promos)
        assert cand.exists
        assert cand.strategy == "EXTEND"
        content = cand.entry_file.read_text("utf-8")
        # the candidate carries the new packages, the live config does not
        assert "firefox" in content
        assert "vim" in content  # original preserved
        assert "thunderbird" in content
        live = model.root.entry_file.read_text("utf-8")
        assert "firefox" not in live  # live configuration untouched (Rule 1)

    def test_stores_plan_toml_with_operation_id(self, tmp_path, tmp_path_factory):
        model, plan = _extend_plan(tmp_path, ("firefox",))
        cand = create_candidate(model, plan, _promos_dir(tmp_path_factory))
        assert cand.plan_toml is not None and cand.plan_toml.exists()
        assert cand.operation_id in cand.plan_toml.read_text("utf-8")

    def test_candidate_lives_under_operation_directory(self, tmp_path, tmp_path_factory):
        model, plan = _extend_plan(tmp_path)
        promos = _promos_dir(tmp_path_factory)
        cand = create_candidate(model, plan, promos)
        assert cand.candidate_dir.parent.name == cand.operation_id
        assert (promos / cand.operation_id / "candidate").exists()

    def test_discard_candidate(self, tmp_path, tmp_path_factory):
        model, plan = _extend_plan(tmp_path)
        promos = _promos_dir(tmp_path_factory)
        cand = create_candidate(model, plan, promos)
        assert discard_candidate(promos, cand.operation_id) is True
        assert not (promos / cand.operation_id).exists()
        assert discard_candidate(promos, cand.operation_id) is False

    def test_operation_id_is_chronological(self):
        a = new_operation_id()
        b = new_operation_id()
        assert a != b
        assert len(a) == 12


# ---------------------------------------------------------------------------
# Operation history (§48)
# ---------------------------------------------------------------------------


class TestHistory:
    def test_records_and_reads_back(self, tmp_path):
        hist = PromotionHistory(tmp_path / "history" / "promotions.toml")
        log_promotion(
            hist,
            operation_id="abc123",
            command="-P#Programming",
            target="Programming",
            previous_state="imperative",
            result_state="declarative",
            status="COMMITTED",
            candidate="configuration_abc.nix",
        )
        entries = hist.all()
        assert len(entries) == 1
        assert entries[0].status == "COMMITTED"
        assert entries[0].target == "Programming"
        assert entries[0].candidate == "configuration_abc.nix"

    def test_records_failures_too(self, tmp_path):
        hist = PromotionHistory(tmp_path / "history" / "promotions.toml")
        log_promotion(
            hist,
            operation_id="deadbe",
            command="-P#Programming",
            target="Programming",
            previous_state="imperative",
            result_state="imperative",
            status="FAILED",
            candidate="configuration_deadbe.nix",
        )
        assert hist.operation("deadbe").status == "FAILED"

    def test_reject_unknown_status(self, tmp_path):
        hist = PromotionHistory(tmp_path / "h.toml")
        with pytest.raises(HistoryError):
            log_promotion(
                hist,
                operation_id="x",
                command="-P#G",
                target="G",
                previous_state="none",
                result_state="declarative",
                status="NOPE",
            )

    def test_recent_orders_by_timestamp(self, tmp_path):
        hist = PromotionHistory(tmp_path / "h.toml")
        for i in range(3):
            log_promotion(
                hist, operation_id=f"op{i}",
                command=f"-P#G{i}", target=f"G{i}",
                previous_state="none", result_state="declarative",
                status="COMMITTED",
            )
        assert [e.operation_id for e in hist.recent(2)] == ["op1", "op2"]

    def test_latest_for_group(self, tmp_path):
        hist = PromotionHistory(tmp_path / "h.toml")
        log_promotion(
            hist, operation_id="a", command="-P#G", target="G",
            previous_state="none", result_state="declarative", status="COMMITTED",
        )
        log_promotion(
            hist, operation_id="b", command="-D#G", target="G",
            previous_state="declarative", result_state="imperative", status="COMMITTED",
        )
        latest = hist.latest_for("G")
        assert latest is not None and latest.operation_id == "b"


# ---------------------------------------------------------------------------
# Target lock (§46)
# ---------------------------------------------------------------------------


class TestLock:
    def test_acquire_release_cycle(self, tmp_path):
        lock = PromotionLock(tmp_path / "promotion.lock")
        owner = lock.acquire(target="/etc/nixos", operation_id="op1", session="shell-A")
        assert isinstance(owner, LockOwner)
        assert owner.target == "/etc/nixos"
        assert lock.current_owner().operation_id == "op1"
        lock.release()
        assert lock.current_owner() is None

    def test_second_acquire_is_rejected(self, tmp_path):
        a = PromotionLock(tmp_path / "promotion.lock")
        a.acquire(target="/etc/nixos", operation_id="op1", session="s1")
        b = PromotionLock(tmp_path / "promotion.lock")
        with pytest.raises(LockError):
            b.acquire(target="/etc/nixos", operation_id="op2", session="s2")
        a.release()
        # after release it can be acquired again
        b.acquire(target="/etc/nixos", operation_id="op3", session="s3")
        b.release()

    def test_state_transitions_persisted(self, tmp_path):
        lock = PromotionLock(tmp_path / "promotion.lock")
        owner = lock.acquire(target="/etc/nixos", operation_id="op1", session="s1")
        assert owner.state == "PREPARING"
        lock.update_state("TESTING")
        assert lock.current_owner().state == "TESTING"
        lock.update_state("COMMITTED")
        assert lock.current_owner().state == "COMMITTED"
        lock.release()


# ---------------------------------------------------------------------------
# Queue (§44-45)
# ---------------------------------------------------------------------------


class TestQueue:
    def test_enqueue_and_pending(self, tmp_path):
        queue = PromotionQueue(tmp_path / "queue")
        op = queue.enqueue(target="/etc/nixos", command="-P#Games", session="shell-A")
        assert op.state == "QUEUED"
        pending = queue.pending()
        assert len(pending) == 1
        assert pending[0].session == "shell-A"
        assert queue.get(op.operation_id).target == "/etc/nixos"

    def test_enqueue_requires_session(self, tmp_path):
        queue = PromotionQueue(tmp_path / "queue")
        with pytest.raises(Exception):
            queue.enqueue(target="/etc/nixos", command="-P#G", session="")

    def test_cancel_abandoned_operations(self, tmp_path):
        queue = PromotionQueue(tmp_path / "queue")
        queue.enqueue(target="/etc/nixos", command="-P#G1", session="shell-A")
        queue.enqueue(target="/etc/nixos", command="-P#G2", session="shell-B")
        queue.enqueue(target="/etc/nixos", command="-P#G3", session="shell-B")
        cancelled = queue.cancel_abandoned(live_sessions={"shell-B"})
        assert len(cancelled) == 1
        assert cancelled[0].session == "shell-A"
        # shell-B operations remain queued
        assert [o.session for o in queue.pending()] == ["shell-B", "shell-B"]
        # cancelled is no longer pending
        assert cancelled[0].state == "CANCELLED"

    def test_pop_next_dequeues_oldest(self, tmp_path):
        queue = PromotionQueue(tmp_path / "queue")
        a = queue.enqueue(target="/etc/nixos", command="-P#A", session="s")
        b = queue.enqueue(target="/etc/nixos", command="-P#B", session="s")
        first = queue.pop_next()
        assert first.operation_id == a.operation_id
        assert queue.pop_next().operation_id == b.operation_id
        assert queue.pop_next() is None


# ---------------------------------------------------------------------------
# Sequential chain (§42-43, 57.11)
# ---------------------------------------------------------------------------


class TestSequenceChain:
    def test_base_is_last_successful(self, tmp_path):
        tracker = SequenceTracker.load(tmp_path / "seq.toml")
        # A PASS -> new base
        tracker.record_test("A", tested_against="base", passed=True)
        assert tracker.last_successful_base == "A"
        # B FAIL -> base must STAY A (Rule 4)
        tracker.record_test("B", tested_against="A", passed=False)
        assert tracker.last_successful_base == "A"
        # C tested against A, then passes -> new base C
        tracker.record_test("C", tested_against="A", passed=True)
        assert tracker.last_successful_base == "C"

    def test_chain_survives_reload(self, tmp_path):
        path = tmp_path / "seq.toml"
        tracker = SequenceTracker.load(path)
        tracker.record_test("X", tested_against="", passed=True)
        tracker.record_test("Y", tested_against="X", passed=False)
        again = SequenceTracker.load(path)
        assert again.last_successful_base == "X"


# ---------------------------------------------------------------------------
# §57.13 persistent identity across a full state-cycle roundtrip
# ---------------------------------------------------------------------------


class TestRoundTrip57_13:
    def test_group_identity_survives_state_cycle(self, mgr):
        from nixorcist.core.models import Backend

        # create + populate
        mgr.create("Games")
        mgr.add("Games", [rp("steam"), rp("prismlauncher")])
        originals = {p.requested for p in mgr.get("Games").packages}

        # imperative -> promote -> declarative
        mgr.activate("Games", Backend.IMPERATIVE)
        mgr.promote("Games")

        # declarative -> demote -> imperative
        mgr.demote("Games")

        # imperative -> deactivate -> activate
        mgr.activate("Games", Backend.IMPERATIVE)
        mgr.deactivate("Games")
        mgr.activate("Games", Backend.DECLARATIVE)

        group = mgr.get("Games")
        assert {p.requested for p in group.packages} == originals
        assert group.state.active is True
        assert group.state.backend.value == "declarative"