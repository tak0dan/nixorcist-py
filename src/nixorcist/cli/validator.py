"""Semantic validation of a parsed :class:`Command`.

The parser guarantees a well-formed expression.  The validator checks that
the expression is *meaningful*: that scopes and operations agree, that
positional assignments are unambiguous, and that nothing silently discards
input (see ``TN102`` positional assignment overflow).
"""

from __future__ import annotations

from .ast import Broadcast, Command, Operation, PackageSet, Positional, Target
from .diagnostics import Diagnostic, ErrorCode, NixorcistError, Span


class ValidationError(NixorcistError):
    pass


def _err(d: Diagnostic) -> ValidationError:
    return ValidationError(d)


def _overflow_diagnostic(cmd: Command, group_count: int) -> Diagnostic:
    assert isinstance(cmd.assignment, Positional)
    slots = cmd.assignment.slots
    count = len(slots)
    start = cmd.assignment.slot_spans[group_count].start
    end = cmd.assignment.slot_spans[-1].end

    mapped: list[str] = []
    for i, g in enumerate(cmd.groups):
        pkgs = cmd.assign_packages_for(i)
        mapped.append(f"  {g.name} -> {{{','.join(p.name for p in pkgs)}}}")
    unassigned: list[str] = []
    for k in range(group_count, count):
        pkgs = slots[k]
        unassigned.append(f"  {{{','.join(p.name for p in pkgs)}}}")

    notes = [
        f"{group_count} profile groups were provided, but {count} package groups were specified.",
        "",
        "Resolved assignments:",
        *mapped,
    ]
    if unassigned:
        notes += ["", "Unassigned package groups:", *unassigned]

    suggestion = "If you intended to skip groups, use explicit empty slots:\n\n" + _suggest_expression(cmd)
    return Diagnostic(
        ErrorCode.POSITIONAL_OVERFLOW,
        "positional assignment overflow",
        expression=cmd.expression,
        spans=(Span(start, end),),
        notes=tuple(notes),
        suggestion=suggestion,
    )


def _suggest_expression(cmd: Command) -> str:
    assert isinstance(cmd.assignment, (Positional, Broadcast))
    if isinstance(cmd.assignment, Positional):
        slots = cmd.assignment.slots
        n = len(cmd.groups)
        if n >= len(slots):
            return cmd.expression
        groups = "{" + ",".join(g.name for g in cmd.groups) + "}"
        kept = slots[:n]
        slots_txt = "".join("{" + ",".join(p.name for p in s) + "}" for s in kept)
        op = "-I" if cmd.operation is Operation.INSTALL else "-R"
        return f"{op}{groups}##{slots_txt}"
    # Broadcast with overflow in ordered sections
    ordered = cmd.assignment.ordered
    n = len(cmd.groups)
    groups = "{" + ",".join(g.name for g in cmd.groups) + "}"
    kept = ordered[:n]
    slots_txt = "".join("{" + ",".join(p.name for p in s.packages) + "}" for s in kept)
    op = "-I" if cmd.operation is Operation.INSTALL else "-R"
    return f"{op}{groups}##{slots_txt}"


def _broadcast_overflow_diagnostic(cmd: Command, group_count: int) -> Diagnostic:
    assert isinstance(cmd.assignment, Broadcast)
    a = cmd.assignment
    ordered = a.ordered
    count = len(ordered)
    # Build span covering overflow section
    start = ordered[group_count].start
    end = ordered[-1].end

    mapped: list[str] = []
    for i, g in enumerate(cmd.groups):
        pkgs = cmd.assign_packages_for(i)
        mapped.append(f"  {g.name} -> {{{','.join(p.name for p in pkgs)}}}")
    unassigned: list[str] = []
    for k in range(group_count, count):
        pkgs = ordered[k].packages
        unassigned.append(f"  {{{','.join(p.name for p in pkgs)}}}")

    notes = [
        f"{group_count} profile groups were provided, but {count} ordered sections were specified.",
        "",
        "Resolved assignments:",
        *mapped,
    ]
    if unassigned:
        notes += ["", "Unassigned package groups:", *unassigned]

    return Diagnostic(
        ErrorCode.POSITIONAL_OVERFLOW,
        "ordered package assignment overflow",
        expression=cmd.expression,
        spans=(Span(start, end),),
        notes=tuple(notes),
        suggestion="reduce the number of ordered sections or add more groups",
    )


def validate(cmd: Command) -> None:
    """Raise :class:`ValidationError` when the command is not meaningful."""
    op = cmd.operation
    assignment = cmd.assignment
    groups = cmd.groups
    explicit = cmd.explicit_packages()

    if op is Operation.INSTALL and cmd.profile_only:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "the profile-only scope '.' is not valid for installation",
                expression=cmd.expression,
                suggestion="use -R#. to remove from the profile only, or drop the '.' for a plain install",
            )
        )

    if op is Operation.INSTALL and cmd.all_groups and explicit:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "cannot combine the '*' scope with explicit packages",
                expression=cmd.expression,
                suggestion="use '*' alone to install the contents of every group, or list packages without '*'",
            )
        )

    if op is Operation.INSTALL and groups and not cmd.groups_flag and explicit:
        raise _err(
            Diagnostic(
                ErrorCode.INVALID_PACKAGE,
                "cannot combine explicit packages with group references without -G",
                expression=cmd.expression,
                suggestion="use -IG#{group}#{packages} to add packages to a group while installing, "
                "or -I #{group} to install a group's existing packages",
            )
        )

    if op is Operation.ADD and cmd.profile_only:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "the profile-only scope '.' is not valid for group operations",
                expression=cmd.expression,
            )
        )

    if op is Operation.ADD and not groups and not cmd.all_groups:
        raise _err(
            Diagnostic(
                ErrorCode.INVALID_GROUP,
                "-G requires at least one group target",
                expression=cmd.expression,
                suggestion="write -G#GroupName {packages} or -G#{Group,Other}##{...}{...}",
            )
        )

    if op is Operation.ADD and cmd.all_groups and not explicit:
        raise _err(
            Diagnostic(
                ErrorCode.INVALID_PACKAGE,
                "no packages to add with the '*' scope",
                expression=cmd.expression,
                suggestion="write '-G* {packages}'",
            )
        )

    if isinstance(assignment, Positional):
        if cmd.all_groups or cmd.profile_only or not groups:
            raise _err(
                Diagnostic(
                    ErrorCode.SYNTAX,
                    "positional assignment '##' requires an explicit group list",
                    expression=cmd.expression,
                    suggestion="write -G#{GroupA,GroupB}##{packages}{morePackages}",
                )
            )
        if len(assignment.slots) > len(groups):
            raise _err(_overflow_diagnostic(cmd, len(groups)))

    # Ordered overflow for broadcast+ordered (spec §21/§57.8)
    if isinstance(assignment, Broadcast) and groups and len(assignment.ordered) > len(groups):
        raise _err(_broadcast_overflow_diagnostic(cmd, len(groups)))

    # ``-R#.{x}`` combined with -G is contradictory.
    if op is Operation.REMOVE and cmd.groups_flag and cmd.profile_only:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "-G contradicts the profile-only scope '.'",
                expression=cmd.expression,
                suggestion="use '-R#.' (profile only) or '-RG#{group}' (manifest only)", 
            )
        )

    if op is Operation.REMOVE:
        if cmd.profile_only and not explicit:
            raise _err(
                Diagnostic(
                    ErrorCode.INVALID_PACKAGE,
                    "no packages specified for profile-only removal",
                    expression=cmd.expression,
                    suggestion="write '-R#. {packages}'",
                )
            )
        if not explicit and not groups and not cmd.all_groups:
            raise _err(
                Diagnostic(
                    ErrorCode.INVALID_PACKAGE,
                    "no packages to remove",
                    expression=cmd.expression,
                    suggestion="write '-R {packages}' for a complete removal, "
                    "'-R#. {packages}' for the profile only, or '-R#{group} {packages}' for a group",
                )
            )

    _validate_state_operations(cmd)


def _validate_state_operations(cmd: Command) -> None:
    """Rules for the corrected-state-model operations -P/-D/-E/-A and the
    lifecycle operations -O/-Y (spec §§33-50)."""
    op = cmd.operation
    explicit = cmd.explicit_packages()

    if op in (
        Operation.PROMOTE,
        Operation.DEMOTE,
        Operation.DEACTIVATE,
        Operation.ACTIVATE,
        Operation.OBLITERATE,
        Operation.YIELD,
    ):
        if cmd.profile_only:
            raise _err(
                Diagnostic(
                    ErrorCode.SYNTAX,
                    "the profile-only scope '.' is not valid for "
                    f"{op.value}",
                    expression=cmd.expression,
                    suggestion="use '#' with a group collection, e.g. " + {
                        Operation.PROMOTE: "-P#Programming",
                        Operation.DEMOTE: "-D#Programming",
                        Operation.DEACTIVATE: "-E#{Programming,Games}",
                        Operation.ACTIVATE: "-A#Programming",
                        Operation.OBLITERATE: "-O#Programming",
                        Operation.YIELD: "-Y#Programming",
                    }[op],
                )
            )
        if explicit:
            raise _err(
                Diagnostic(
                    ErrorCode.INVALID_PACKAGE,
                    f"{op.value} does not take explicit packages",
                    expression=cmd.expression,
                    suggestion=(
                        "the group's stored package membership is used; "
                        "add packages with -G or -I first"
                        if op not in (Operation.OBLITERATE, Operation.YIELD)
                        else "select a group with -O#Group or -Y#Group"
                    ),
                )
            )
        if isinstance(cmd.assignment, Positional):
            raise _err(
                Diagnostic(
                    ErrorCode.SYNTAX,
                    f"positional assignment '##' is not valid for {op.value}",
                    expression=cmd.expression,
                )
            )

    if op is Operation.YIELD and cmd.all_groups:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "-Y requires an explicit group reference (the '*' scope is not valid)",
                expression=cmd.expression,
                suggestion="write -Y#Programming or -Yy#Programming",
            )
        )

    if op is Operation.ACTIVATE and cmd.target in (Target.NONE,):
        # plain -A is valid: remembered backend is used at execution time.
        pass

    if cmd.target is not Target.NONE and op not in (Operation.INSTALL, Operation.ACTIVATE):
        opaque = "-i" if cmd.target is Target.IMPERATIVE else "-d"
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                f"{opaque} can only be combined with -I or -A",
                expression=cmd.expression,
                suggestion="write -Ii (imperative install), -Id (declarative install), "
                "-Ai (imperative activate) or -Ad (declarative activate)",
            )
        )

    if cmd.sequence and op is not Operation.PROMOTE:
        raise _err(
            Diagnostic(
                ErrorCode.SYNTAX,
                "-S/--sequence is only meaningful with promote",
                expression=cmd.expression,
                suggestion="write -PS#Programming to enable sequential promotion",
            )
        )

    if op is Operation.INSTALL and cmd.target is Target.DECLARATIVE and cmd.all_groups:
        # -Id* would promote every group's contents; validate package presence
        # at planning time instead of silently declaring nothing.
        pass