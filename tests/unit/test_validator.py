"""Validator unit tests (TN101/TN102/TN003 rules).

The validator rejects commands that are unambiguously wrong: profile-only
installs, ``*`` combined with explicit packages, positional overflows
(spec §21 / TN102), and mixed broadcast+ordered overflows (§57.8), while
accepting spec-legal broadcast, positional and state commands.
"""

from __future__ import annotations

import pytest

from nixorcist.cli.diagnostics import ErrorCode
from nixorcist.cli.parser import parse
from nixorcist.cli.validator import ValidationError, validate


def error_of(expr) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        validate(parse(expr))
    return exc.value


class TestValidCommands:
    @pytest.mark.parametrize(
        "expr",
        [
            "-I python gcc",
            "-I {python,gcc}",
            "-G#Programming#{python,gcc}",  # broadcast to a group
            "-G#{Programming,Games}#{git}",  # broadcast to many groups
            "-G#{Programming,Games}##{python}{gcc}",  # positional shorthand
            "-G{A,B}##{1}{2}",  # positional shorthand, brace scope
            "-G#{A,B}#{1}#{2}{3}",  # broadcast + 2 ordered for 2 groups (§57.6)
            "-G#Programming",  # create-only, no packages
            "-R#. {python}",
            "-R#* python",
            "-PS#Programming",
            "-P#Programming",
            "-D#Programming",
            "-E#Programming",
            "-A#Programming",
            "-Ai#Programming",
            "-Ad#Programming",
            "-Id#Programming",
        ],
    )
    def test_accepts(self, expr):
        validate(parse(expr))


class TestPositionalOverflow:
    def test_hash_scope_overflow(self):
        err = error_of("-G#{A,B}##{1}{2}{3}")
        assert err.diagnostic.code is ErrorCode.POSITIONAL_OVERFLOW
        assert "overflow" in err.diagnostic.message

    def test_brace_scope_overflow(self):
        error_of("-G{A,B}##{1}{2}{3}")

    def test_double_hash_needs_explicit_groups(self):
        err = error_of("-I##{python}{gcc}")
        assert "requires an explicit group list" in err.diagnostic.message

    def test_overflow_diagnostic_carries_expression(self):
        err = error_of("-G#{A,B}##{1}{2}{3}")
        assert err.diagnostic.expression == "-G#{A,B}##{1}{2}{3}"


class TestBroadcastOrderedOverflow:
    def test_3_ordered_for_2_groups(self):
        err = error_of("-G#{A,B}#{1}#{2}{3}{4}")
        assert "ordered package assignment overflow" in err.diagnostic.message

    def test_2_ordered_for_2_groups_is_valid(self):
        # §57.8 boundary: N ordered for N groups is allowed
        validate(parse("-G#{A,B}#{1}#{2}{3}"))

    def test_1_ordered_for_3_groups_is_valid(self):
        validate(parse("-G#{A,B,C}#{1}#{2}"))


class TestInstallRules:
    def test_profile_only_install_rejected(self):
        err = error_of("-I#. {python}")
        assert "not valid for installation" in err.diagnostic.message

    def test_star_with_explicit_rejected(self):
        err = error_of("-I#* {python}")
        assert "cannot combine" in err.diagnostic.message

    def test_group_ref_without_G_rejected(self):
        # a group reference without -G carries plain packages
        err = error_of("-I#Programming#{vim}")
        assert err.diagnostic.code is ErrorCode.INVALID_PACKAGE

    def test_plain_install_of_existing_packages_ok(self):
        validate(parse("-I#Programming"))


class TestGroupAddRules:
    def test_require_group_target(self):
        # "-G {python}" with no scope and no '#' would be a package broadcast
        # addressed to groups -- the validator demands a group target.
        error_of("-G")

    def test_star_needs_packages(self):
        err = error_of("-G*")
        assert "no packages" in err.diagnostic.message.lower()

    def test_add_all_groups_with_packages_ok(self):
        validate(parse("-G* {python,gcc}"))


class TestStateOperationRules:
    @pytest.mark.parametrize("expr", ["-P#Programming {python}", "-E#{A,B}#{x}"])
    def test_state_ops_do_not_take_packages(self, expr):
        err = error_of(expr)
        assert "does not take explicit packages" in err.diagnostic.message

    def test_positional_shorthand_rejected_for_state_ops(self):
        err = error_of("-P#{A,B}##{}{}")
        assert "not valid for promote" in err.diagnostic.message

    def test_target_with_non_install_non_activate_rejected(self):
        err = error_of("-R -i #python")
        assert "-i" in err.diagnostic.message

    def test_sequence_only_with_promote(self):
        err = error_of("-DS#Programming")
        assert "sequence" in err.diagnostic.message


class TestRemoveRules:
    def test_profile_only_remove_needs_packages(self):
        err = error_of("-R#.")
        assert "no packages" in err.diagnostic.message.lower()

    def test_profile_only_remove_with_packages_ok(self):
        validate(parse("-R#. {python}"))

    def test_profile_only_contradicts_group_flag(self):
        err = error_of("-RG#. {python}")
        assert "-G contradicts" in err.diagnostic.message


class TestObliterateRules:
    def test_obliterate_profile_only_rejected(self):
        err = error_of("-O#.")
        assert "not valid for obliterate" in err.diagnostic.message

    def test_obliterate_positional_rejected(self):
        err = error_of("-O#G##{}")
        assert "not valid for obliterate" in err.diagnostic.message

    def test_obliterate_all_groups_ok(self):
        validate(parse("-O#*"))

    def test_obliterate_with_save_ok(self):
        validate(parse("-Os#Programming"))


class TestYieldRules:
    def test_yield_profile_only_rejected(self):
        err = error_of("-Y#.")
        assert "not valid for yield" in err.diagnostic.message

    def test_yield_positional_rejected(self):
        err = error_of("-Y#G##{}")
        assert "not valid for yield" in err.diagnostic.message

    def test_yield_star_rejected(self):
        err = error_of("-Y#*")
        assert "not valid" in err.diagnostic.message.lower()

    def test_yield_group_ok(self):
        validate(parse("-Y#Programming"))