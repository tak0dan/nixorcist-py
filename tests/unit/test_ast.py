"""AST unit tests: assignment semantics and Command helpers.

The AST models two assignment shapes (spec §§13-21):

* :class:`Broadcast` -- broadcast ``sections`` (applied to every group) plus
  optional positional ``ordered`` (slot ``i`` -> group ``i``, additively).
* :class:`Positional` -- the ``##`` ordered-only shorthand with preserved
  empty slots.
"""

from __future__ import annotations

import pytest

from nixorcist.cli.ast import (
    Broadcast,
    Command,
    Operation,
    PackageRef,
    PackageSet,
    Positional,
    Target,
)


def ref(name: str) -> PackageRef:
    return PackageRef(name, 0, len(name))


def set_(*names: str) -> PackageSet:
    return PackageSet(tuple(ref(n) for n in names), 0, 1)


class TestBroadcastAssignments:
    def test_pure_broadcast_every_group(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B")),
            assignment=Broadcast(sections=(set_("x", "y"),)),
        )
        assert cmd.assign_packages_for(0) == (ref("x"), ref("y"))
        assert cmd.assign_packages_for(1) == (ref("x"), ref("y"))

    def test_multi_section_broadcast_is_concatenated(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B")),
            assignment=Broadcast(sections=(set_("x"), set_("y"))),
        )
        assert {p.name for p in cmd.assign_packages_for(0)} == {"x", "y"}
        assert {p.name for p in cmd.assign_packages_for(1)} == {"x", "y"}

    def test_broadcast_plus_ordered_is_additive(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B")),
            assignment=Broadcast(sections=(set_("x"),), ordered=(set_("a"), set_("b"))),
        )
        assert {p.name for p in cmd.assign_packages_for(0)} == {"x", "a"}
        assert {p.name for p in cmd.assign_packages_for(1)} == {"x", "b"}

    def test_broadcast_fewer_ordered_than_groups(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B"), ref("C")),
            assignment=Broadcast(sections=(set_("x"),), ordered=(set_("a"),)),
        )
        assert {p.name for p in cmd.assign_packages_for(0)} == {"x", "a"}
        assert {p.name for p in cmd.assign_packages_for(1)} == {"x"}  # no slot
        assert {p.name for p in cmd.assign_packages_for(2)} == {"x"}


class TestPositionalAssignments:
    def test_positional_slot_mapping(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B")),
            assignment=Positional(slots=((ref("1"),), (ref("2"),))),
        )
        assert cmd.assign_packages_for(0) == (ref("1"),)
        assert cmd.assign_packages_for(1) == (ref("2"),)

    def test_positional_empty_slot(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B")),
            assignment=Positional(slots=((ref("1"),), ())),
        )
        assert cmd.assign_packages_for(0) == (ref("1"),)
        assert cmd.assign_packages_for(1) == ()

    def test_positional_out_of_range(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"), ref("B"), ref("C")),
            assignment=Positional(slots=((ref("1"),), (ref("2"),))),
        )
        assert cmd.assign_packages_for(2) == ()

    def test_slot_count_property(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"),),
            assignment=Positional(slots=((), ()), slot_spans=()),
        )
        assert cmd.assignment.slot_count == 2

    def test_is_positional(self):
        cmd = Command(
            operation=Operation.ADD,
            groups=(ref("A"),),
            assignment=Positional(slots=((ref("1"),),)),
        )
        assert cmd.is_positional is True


class TestExplicitPackages:
    def test_broadcast_bare(self):
        cmd = Command(
            operation=Operation.INSTALL,
            assignment=Broadcast(sections=(set_("python", "gcc"),)),
        )
        assert {p.name for p in cmd.explicit_packages()} == {"python", "gcc"}

    def test_broadcast_ordered_flattened(self):
        cmd = Command(
            operation=Operation.INSTALL,
            assignment=Broadcast(sections=(set_("x"),), ordered=(set_("y"),)),
        )
        assert {p.name for p in cmd.explicit_packages()} == {"x", "y"}

    def test_positional_flattened(self):
        cmd = Command(
            operation=Operation.INSTALL,
            assignment=Positional(slots=((ref("a"),), (ref("b"),))),
        )
        assert {p.name for p in cmd.explicit_packages()} == {"a", "b"}

    def test_none_assignment(self):
        cmd = Command(operation=Operation.INSTALL)
        assert cmd.explicit_packages() == ()


class TestCommandProperties:
    def test_is_declarative_install(self):
        cmd = Command(operation=Operation.INSTALL, target=Target.DECLARATIVE)
        assert cmd.is_declarative_install is True

    def test_declarative_install_requires_install_op(self):
        cmd = Command(operation=Operation.ACTIVATE, target=Target.DECLARATIVE)
        assert cmd.is_declarative_install is False

    def test_group_names(self):
        cmd = Command(operation=Operation.ADD, groups=(ref("A"), ref("B")))
        assert cmd.group_names == ("A", "B")

    def test_start_offset_empty(self):
        cmd = Command(operation=Operation.INSTALL)
        assert cmd.start_offset() == 0