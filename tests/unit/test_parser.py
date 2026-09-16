"""Parser unit tests: the complete §57 (corrected) parsing semantics.

These assert the spec-faithful DSL grammar — broadcast sections continue
across ``{}`` until a second ``#`` (spec §13), the second ``#`` (or ``##``)
starts positional/broadcast-ordered assignment (§14, §16), empty ``{}`` slots
are preserved (§20), and the ``##`` shorthand is a separate ordered-only form.

Reference matrix (verified against the implementation):

    -G#{A,B}#{1}#{2}{3}   -> groups A,B; broadcast {1}; ordered [{2},{3}]
    -G#{A,B}#{1}          -> groups A,B; broadcast sections [{1}]
    -G#{A,B}##{1}{2}      -> groups A,B; Positional slots [(1),(2)]
    -G{A,B}##{1}{2}       -> groups A,B; Positional slots [(1),(2)]
    -G#Programming        -> groups [Programming]; no package assignment
"""

from __future__ import annotations

import pytest

from nixorcist.cli.ast import Broadcast, Operation, PackageRef, Positional, Target
from nixorcist.cli.diagnostics import NixorcistError
from nixorcist.cli.parser import parse
from nixorcist.cli.validator import validate


def names(*refs) -> list[str]:
    """Package names from a tuple of PackageRef (ignores spans)."""
    return [r.name for r in refs]


def pkg_names(iterable) -> list[str]:
    """Names out of any iterable of PackageRef."""
    return [r.name for r in iterable]


# ---------------------------------------------------------------------------
# Operations and flags
# ---------------------------------------------------------------------------


class TestOperations:
    def test_install(self):
        assert parse("-I python").operation is Operation.INSTALL

    def test_remove(self):
        assert parse("-R {python}").operation is Operation.REMOVE

    def test_add_via_group_flag(self):
        cmd = parse("-G#Programming#{python}")
        assert cmd.operation is Operation.ADD

    @pytest.mark.parametrize(
        "expr,op",
        [
            ("-P#Programming", Operation.PROMOTE),
            ("-D#Programming", Operation.DEMOTE),
            ("-E#Programming", Operation.DEACTIVATE),
            ("-A#Programming", Operation.ACTIVATE),
        ],
    )
    def test_state_operations(self, expr, op):
        assert parse(expr).operation is op

    def test_merged_groups_install(self):
        cmd = parse("-IG#Programming#{python}")
        assert cmd.operation is Operation.INSTALL
        assert cmd.groups_flag is True
        assert cmd.group_names == ("Programming",)

    def test_merged_sequence_promote(self):
        cmd = parse("-PS#Programming")
        assert cmd.operation is Operation.PROMOTE
        assert cmd.sequence is True

    def test_activation_target_imperative(self):
        cmd = parse("-Ai#Programming")
        assert cmd.operation is Operation.ACTIVATE
        assert cmd.target is Target.IMPERATIVE

    def test_activation_target_declarative(self):
        cmd = parse("-Ad#Programming")
        assert cmd.target is Target.DECLARATIVE

    def test_declarative_install(self):
        cmd = parse("-Id#Programming")
        assert cmd.operation is Operation.INSTALL
        assert cmd.target is Target.DECLARATIVE
        assert cmd.is_declarative_install is True

    def test_dot_profile_only(self):
        cmd = parse("-R#. {python}")
        assert cmd.profile_only is True
        assert cmd.operation is Operation.REMOVE

    def test_star_all_groups(self):
        cmd = parse("-R#* python")
        assert cmd.all_groups is True

    def test_empty_command_raises(self):
        with pytest.raises(NixorcistError):
            parse("")

    def test_content_without_operation_raises(self):
        with pytest.raises(NixorcistError):
            parse("python gcc")


# ---------------------------------------------------------------------------
# The core §57 matrix
# ---------------------------------------------------------------------------


class TestBroadcastAssignment:
    def test_broadcast_single_group(self):
        cmd = parse("-G#Programming#{java,javac}")
        assert cmd.group_names == ("Programming",)
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert len(a.sections) == 1
        assert {p.name for p in a.sections[0].packages} == {"java", "javac"}
        assert a.ordered == ()
        assert pkg_names(cmd.assign_packages_for(0)) == ["java", "javac"]

    def test_broadcast_multiple_groups_every_group_gets_every_section(self):
        cmd = parse("-G#{Programming,Games}#{git}")
        assert cmd.group_names == ("Programming", "Games")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert len(a.sections) == 1
        assert pkg_names(cmd.assign_packages_for(0)) == ["git"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["git"]

    def test_broadcast_continues_across_brace_sections(self):
        # spec §13: no second '#' -> both sections are broadcast
        cmd = parse("-G#{A,B}#{1}{2}")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert [tuple(p.name for p in s.packages) for s in a.sections] == [("1",), ("2",)]
        assert a.ordered == ()
        assert pkg_names(cmd.assign_packages_for(0)) == ["1", "2"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["1", "2"]


class TestBroadcastOrderedTransition:
    def test_second_hash_starts_ordered(self):
        cmd = parse("-G#{A,B}#{1}#{2}")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert [tuple(p.name for p in s.packages) for s in a.sections] == [("1",)]
        assert [tuple(p.name for p in s.packages) for s in a.ordered] == [("2",)]
        assert pkg_names(cmd.assign_packages_for(0)) == ["1", "2"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["1"]

    def test_spec_case_57_6(self):
        # -G#{A,B}#{1}#{2}{3}: broadcast {1} + ordered [{2},{3}]
        cmd = parse("-G#{A,B}#{1}#{2}{3}")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert len(a.sections) == 1
        assert [tuple(p.name for p in s.packages) for s in a.ordered] == [("2",), ("3",)]
        assert pkg_names(cmd.assign_packages_for(0)) == ["1", "2"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["1", "3"]


class TestPositionalShorthand:
    def test_double_hash_after_hash_scope(self):
        cmd = parse("-G#{A,B}##{1}{2}")
        a = cmd.assignment
        assert isinstance(a, Positional)
        assert pkg_names(cmd.assign_packages_for(0)) == ["1"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["2"]

    def test_double_hash_after_brace_scope(self):
        cmd = parse("-G{A,B}##{1}{2}")
        a = cmd.assignment
        assert isinstance(a, Positional)
        assert pkg_names(cmd.assign_packages_for(0)) == ["1"]
        assert pkg_names(cmd.assign_packages_for(1)) == ["2"]

    def test_double_hash_multi_package_inside_one_slot(self):
        cmd = parse("-G#{A,B}##{python,gcc}{steam}")
        sl = cmd.assignment.slots
        assert {p.name for p in sl[0]} == {"python", "gcc"}
        assert {p.name for p in sl[1]} == {"steam"}

    def test_empty_slots_preserved_middle(self):
        cmd = parse("-G#{A,B}##{1}{}{2}")
        slots = cmd.assignment.slots
        assert list(list(p.name for p in s) for s in slots) == [["1"], [], ["2"]]

    def test_empty_slots_preserved_first(self):
        cmd = parse("-G#{A,B}##{}{2}")
        slots = cmd.assignment.slots
        assert list(list(p.name for p in s) for s in slots) == [[], ["2"]]

    def test_empty_slots_preserved_last(self):
        cmd = parse("-G#{A,B}##{1}{}")
        slots = cmd.assignment.slots
        assert list(list(p.name for p in s) for s in slots) == [["1"], []]

    def test_positional_requires_at_least_one_slot(self):
        with pytest.raises(NixorcistError):
            parse("-G#{A,B}## ")


class TestObliterateParsing:
    def test_bare_obliterate(self):
        cmd = parse("-O#Programming")
        assert cmd.operation is Operation.OBLITERATE
        assert cmd.group_names == ("Programming",)
        assert cmd.obliterate_count == 0
        assert cmd.save is False

    def test_o_count_one(self):
        cmd = parse("-Oo#Programming")
        assert cmd.obliterate_count == 1
        assert cmd.obliterate_declaration_scope == 1

    def test_o_count_two_triggers_orphan_sweep(self):
        cmd = parse("-Ooo#Programming")
        assert cmd.obliterate_count == 2
        assert cmd.obliterate_orphan_sweep is True

    def test_extra_o_modifiers_idempotent(self):
        cmd = parse("-Ooooo#Programming")
        assert cmd.obliterate_count == 4
        assert cmd.obliterate_orphan_sweep is True

    def test_save_flag(self):
        cmd = parse("-Os#Programming")
        assert cmd.save is True
        assert cmd.obliterate_count == 0

    def test_save_with_modifiers(self):
        cmd = parse("-Oos#Programming")
        assert cmd.save is True
        assert cmd.obliterate_count == 1

    def test_save_with_long_flag(self):
        cmd = parse("-O -s#Programming")
        assert cmd.save is True
        assert cmd.obliterate_count == 0

    def test_obliterate_all_groups(self):
        cmd = parse("-O#*")
        assert cmd.all_groups is True
        assert cmd.obliterate_count == 0

    def test_long_flag(self):
        cmd = parse("--obliterate#Programming")
        assert cmd.operation is Operation.OBLITERATE
        assert cmd.group_names == ("Programming",)

    def test_long_flag_delete_group_alias(self):
        cmd = parse("--delete-group#Programming")
        assert cmd.operation is Operation.OBLITERATE

    def test_obliterate_requires_scope(self):
        with pytest.raises(NixorcistError):
            parse("-O")

    def test_obliterate_no_packages(self):
        with pytest.raises(NixorcistError):
            parse("-O##{x}")  # positional not valid


class TestYieldParsing:
    def test_bare_yield(self):
        cmd = parse("-Y#Programming")
        assert cmd.operation is Operation.YIELD
        assert cmd.group_names == ("Programming",)
        assert cmd.yield_count == 0

    def test_y_count_one(self):
        cmd = parse("-Yy#Programming")
        assert cmd.yield_count == 1

    def test_y_count_two_triggers_orphan_sweep(self):
        cmd = parse("-Yyy#Programming")
        assert cmd.yield_count == 2
        assert cmd.yield_orphan_sweep is True

    def test_extra_y_modifiers_idempotent(self):
        cmd = parse("-Yyyyy#Programming")
        assert cmd.yield_count == 4
        assert cmd.yield_orphan_sweep is True

    def test_long_flag(self):
        cmd = parse("--yield#Programming")
        assert cmd.operation is Operation.YIELD

    def test_yield_requires_scope(self):
        with pytest.raises(NixorcistError):
            parse("-Y")

    def test_yield_rejects_star(self):
        with pytest.raises(NixorcistError):
            validate(parse("-Y#*"))

    def test_yield_extra_y_are_idempotent(self):
        base = parse("-Yyy#Games")
        more = parse("-Yyyyyyy#Games")
        assert base.yield_count == 2
        assert more.yield_count == 6
        assert base.yield_orphan_sweep == more.yield_orphan_sweep is True

    def test_obliterate_extra_o_are_idempotent(self):
        base = parse("-Ooo#Programming")
        more = parse("-Oooooooo#Programming")
        assert base.obliterate_count == 2
        assert more.obliterate_count == 7
        assert base.obliterate_orphan_sweep == more.obliterate_orphan_sweep is True


class TestScopeAndBarePackages:
    def test_bare_names_single_broadcast_section(self):
        cmd = parse("-I python gcc")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert {p.name for p in a.sections[0].packages} == {"python", "gcc"}
        assert pkg_names(cmd.assign_packages_for(0)) == ["python", "gcc"]

    def test_bare_braced_for_install(self):
        cmd = parse("-I {python,gcc}")
        a = cmd.assignment
        assert isinstance(a, Broadcast)
        assert {p.name for p in a.sections[0].packages} == {"python", "gcc"}

    def test_group_only_scope_no_assignment(self):
        cmd = parse("-G#Programming")
        assert cmd.group_names == ("Programming",)
        assert cmd.assignment is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_conflicting_operations(self):
        with pytest.raises(NixorcistError):
            parse("-I-R python")

    def test_conflicting_targets(self):
        with pytest.raises(NixorcistError):
            parse("-Ii -Id python")

    def test_unterminated_collection(self):
        with pytest.raises(NixorcistError):
            parse("-G#Programming#{python")

    def test_unexpected_comma(self):
        with pytest.raises(NixorcistError):
            parse("-G#{A,,B}#{python}")

    def test_state_op_requires_scope(self):
        with pytest.raises(NixorcistError):
            parse("-E#. {x}")  # '#' scope cannot consume '.', etc.

    def test_expected_group_after_hash(self):
        with pytest.raises(NixorcistError):
            parse("-I# ")

    def test_trailing_junk(self):
        with pytest.raises(NixorcistError):
            parse("-I python gcc {vim}")