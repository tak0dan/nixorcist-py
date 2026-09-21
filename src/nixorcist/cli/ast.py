"""Internal AST for the Nixorcist DSL.

The AST is independent of Nix.  It only describes *intent*: which groups are
involved, how packages are assigned to them, and whether any profile
operation is requested.  Transformations into concrete actions happen in the
execution planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .diagnostics import Span


class Operation(str, Enum):
    INSTALL = "install"
    REMOVE = "remove"
    ADD = "add"
    PROMOTE = "promote"
    DEMOTE = "demote"
    DEACTIVATE = "deactivate"
    ACTIVATE = "activate"
    OBLITERATE = "obliterate"
    YIELD = "yield"


class Target(str, Enum):
    NONE = "none"
    IMPERATIVE = "imperative"
    DECLARATIVE = "declarative"


class InstallMethod(str, Enum):
    """How a package should be installed into the system.

    Declarative packages are higher in hierarchy than imperative ones.
    When a group is promoted to declarative, its packages should be installed
    via NixOS configuration rather than ``nix profile install``.
    """

    IMPERATIVE = "imperative"
    DECLARATIVE = "declarative"
    AUTO = "auto"


@dataclass(frozen=True)
class PackageRef:
    name: str
    start: int = 0
    end: int = 0

    def span(self) -> Span:
        return Span(self.start, self.end)


@dataclass(frozen=True)
class GroupRef:
    name: str
    start: int = 0
    end: int = 0

    def span(self) -> Span:
        return Span(self.start, self.end)


@dataclass(frozen=True)
class PackageSet:
    """A single braced package collection: ``{a,b}``."""

    packages: tuple[PackageRef, ...] = ()
    start: int = 0
    end: int = 0

    def span(self) -> Span:
        return Span(self.start, self.end)


@dataclass(frozen=True)
class Broadcast:
    """Broadcast + ordered assignment (spec §§10-15).

    ``sections`` are braced broadcast sections: every section's packages
    apply to every selected group, in order (§13: broadcast continues across
    ``{}`` until a second ``#``).

    ``ordered`` are the positional slots introduced by the second ``#``
    (or ``##``); slot ``i`` maps to group ``i`` additively (§17).

    Pure-broadcast (no second ``#``) has ``ordered=()``.
    The ``##`` shorthand is represented by :class:`Positional` instead.
    """

    sections: tuple[PackageSet, ...] = ()
    ordered: tuple[PackageSet, ...] = ()


@dataclass(frozen=True)
class Positional:
    """Ordered-only (``##``) assignment: empty broadcast + positional slots.

    Empty slots (``{}``) are semantically meaningful and are preserved as
    empty tuple entries.
    """

    slots: tuple[tuple[PackageRef, ...], ...]
    slot_spans: tuple[Span, ...] = ()

    @property
    def slot_count(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class Command:
    """A fully parsed DSL command.

    ``target`` captures ``-i``/``-d`` selection for ``-I`` / ``-A``:
    ``Target.NONE`` means "remembered target" for ``-A`` and the default
    (imperative) profile for ``-I``.

    ``install_method`` captures ``-M`` selection for package installation:
    ``InstallMethod.IMPERATIVE`` is the default (install via nix profile),
    ``InstallMethod.DECLARATIVE`` means install via NixOS config,
    ``InstallMethod.AUTO`` means decide based on group backend state.
    """

    operation: Operation
    groups: tuple[GroupRef, ...] = ()
    all_groups: bool = False
    profile_only: bool = False
    has_scope: bool = False
    groups_flag: bool = False
    assignment: Broadcast | Positional | None = None
    expression: str = ""
    target: Target = Target.NONE
    sequence: bool = False
    install_method: InstallMethod = InstallMethod.IMPERATIVE
    # Obliteration / yielding modifiers (spec §§33-63)
    obliterate_count: int = 0
    yield_count: int = 0
    save: bool = False

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(g.name for g in self.groups)

    @property
    def is_positional(self) -> bool:
        return isinstance(self.assignment, Positional)

    @property
    def is_declarative_install(self) -> bool:
        """``-Id ...`` -- install then activate declaratively."""
        return self.operation is Operation.INSTALL and self.target is Target.DECLARATIVE

    @property
    def obliterate_declaration_scope(self) -> int:
        """Number of 'o' modifiers: 0=registry only, 1=selected decl, 2+=orphan sweep."""
        return self.obliterate_count

    @property
    def yield_orphan_sweep(self) -> bool:
        """True when y_count >= 2: also yield all orphaned declarative groups."""
        return self.yield_count >= 2

    @property
    def obliterate_orphan_sweep(self) -> bool:
        """True when o_count >= 2: also remove all orphaned declarative declarations."""
        return self.obliterate_count >= 2

    def assign_packages_for(self, index: int) -> tuple[PackageRef, ...]:
        """Return the package refs assigned to the ``index``-th target group.

        For broadcast, every group receives the concatenation of all broadcast
        sections plus its positional ordered slot (spec §14-15).
        For positional (##), group ``i`` receives slot ``i`` only.
        """
        if self.assignment is None:
            return ()
        if isinstance(self.assignment, Positional):
            if index < len(self.assignment.slots):
                return self.assignment.slots[index]
            return ()
        # Broadcast: broadcast-sections *all groups* + ordered[i]
        pkgs = [p for sec in self.assignment.sections for p in sec.packages]
        if index < len(self.assignment.ordered):
            pkgs.extend(self.assignment.ordered[index].packages)
        return tuple(pkgs)

    def explicit_packages(self) -> tuple[PackageRef, ...]:
        if self.assignment is None:
            return ()
        if isinstance(self.assignment, Positional):
            return tuple(p for slot in self.assignment.slots for p in slot)
        # Broadcast: all broadcast + all ordered
        return (
            tuple(p for sec in self.assignment.sections for p in sec.packages)
            + tuple(p for sec in self.assignment.ordered for p in sec.packages)
        )

    def spans_for_group(self, index: int) -> Span:
        if isinstance(self.assignment, Positional) and index < len(self.assignment.slot_spans):
            return self.assignment.slot_spans[index]
        return Span.empty(self.start_offset() if hasattr(self, "start_offset") else 0)

    def start_offset(self) -> int:
        offsets = [g.start for g in self.groups]
        return min(offsets) if offsets else 0
