"""Execution Planner: convert a validated :class:`Command` into concrete
actions against the group manifest and the profile.

Planning is a read-only phase: nothing is mutated and no Nix command is run
here.  The resulting :class:`Plan` is executed afterwards by the CLI, which
applies manifest changes through the Group Manager and profile changes
through a backend.

The corrected state model means ``-P``/``-D`` change ``backend`` only, while
``-A``/``-E`` change ``active`` only; membership and profile installation are
separate.  Declarative *representation* changes (promote/demote bodies) are
carried out transactionally by the promotion subsystem after planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..cli.ast import Command, Operation, Target
from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError
from ..core.models import Backend, InstallMethod
from ..groups.manager import GroupError, GroupManager
from .models import ResolvedPackage, dedupe_packages

if TYPE_CHECKING:
    from ..core.resolver import PackageResolver


class PlanError(NixorcistError):
    pass


@dataclass
class Plan:
    add_to_groups: dict[str, list[ResolvedPackage]] = field(default_factory=dict)
    remove_from_groups: dict[str, list[str]] = field(default_factory=dict)
    remove_from_all_groups: list[str] | None = None
    install: list[ResolvedPackage] = field(default_factory=list)
    remove_from_profile: list[str] = field(default_factory=list)
    ensure_groups: list[str] = field(default_factory=list)
    wipe_profile: bool = False
    persist_manifest: bool = False
    notes: list[str] = field(default_factory=list)
    # -- corrected state model (spec state machine) ----------------------
    # (group_name, backend) to set active=True with
    activate: list[tuple[str, str]] = field(default_factory=list)
    deactivate: list[str] = field(default_factory=list)
    promote: list[tuple[str, list[ResolvedPackage]]] = field(default_factory=list)
    demote: list[str] = field(default_factory=list)
    # -- lifecycle operations (spec §§33-50) -----------------------------
    obliterate: list[str] = field(default_factory=list)
    yield_groups: list[tuple[str, list[ResolvedPackage]]] = field(default_factory=list)
    obliterate_declaration: bool = False
    obliterate_save: bool = False
    obliterate_orphan_sweep: bool = False
    yield_orphan_sweep: bool = False

    @property
    def is_noop(self) -> bool:
        return not (
            self.add_to_groups
            or self.remove_from_groups
            or self.remove_from_all_groups
            or self.install
            or self.remove_from_profile
            or self.wipe_profile
            or self.activate
            or self.deactivate
            or self.promote
            or self.demote
            or self.obliterate
            or self.yield_groups
        )

    def needs_declarative_work(self) -> bool:
        return bool(
            self.promote or self.demote or self.obliterate_declaration or self.yield_orphan_sweep
        )

    def steps(self) -> list[str]:
        """A human-readable ordered execution plan (spec section 18)."""
        steps: list[str] = []
        for name in self.ensure_groups:
            steps.append(f'ensure group "{name}"')
        if self.install:
            # Group packages by installation method for clearer output
            imperative_pkgs = [p for p in self.install if p.install_method is InstallMethod.IMPERATIVE]
            auto_pkgs = [p for p in self.install if p.install_method is InstallMethod.AUTO]
            deferred_pkgs = [p for p in self.install if p.install_method is InstallMethod.DECLARATIVE]
            
            if imperative_pkgs:
                names = ", ".join(f"{p.requested} -> {p.attribute}" for p in imperative_pkgs)
                steps.append(f"install into profile (nix profile): {names}")
            if auto_pkgs:
                names = ", ".join(f"{p.requested} -> {p.attribute}" for p in auto_pkgs)
                steps.append(f"install into profile (auto, nix profile): {names}")
            if deferred_pkgs:
                names = ", ".join(p.requested for p in deferred_pkgs)
                steps.append(f"deferred to declarative (NixOS config): {names}")
        if self.add_to_groups:
            for group, pkgs in self.add_to_groups.items():
                steps.append(f'add to group "{group}": {", ".join(p.requested for p in pkgs)}')
        if self.remove_from_groups:
            for group, names in self.remove_from_groups.items():
                steps.append(f'remove from group "{group}": {", ".join(names)}')
        if self.remove_from_all_groups is not None:
            steps.append(
                f'remove from all groups: {", ".join(self.remove_from_all_groups) or "(wipe)"}'
            )
        if self.remove_from_profile or self.wipe_profile:
            names = ", ".join(self.remove_from_profile) or "(wipe)"
            steps.append(f"remove from profile (nix profile): {names}")
        for backend, names in _group_by(("imperative", "declarative"), self.activate).items():
            steps.append(f"activate {' '.join(sorted(set(names)))} -> {backend}")
        if self.promote:
            for name, pkgs in self.promote:
                steps.append(
                    f"promote group \"{name}\" -> declarative "
                    f"({', '.join(p.requested for p in pkgs)})"
                )
        if self.demote:
            steps.append(f"demote groups -> imperative: {', '.join(sorted(set(self.demote)))}")
        if self.deactivate:
            steps.append(f"deactivate groups: {', '.join(sorted(set(self.deactivate)))}")
        if self.obliterate:
            steps.append(f"obliterate groups: {', '.join(sorted(set(self.obliterate)))}")
            if self.obliterate_declaration:
                steps.append("remove declarative representation of obliterated group(s)")
            if self.obliterate_orphan_sweep:
                steps.append("remove orphaned declarative group declarations")
            if self.obliterate_save:
                steps.append("save removed declarative section(s) as artifact")
        if self.yield_groups:
            steps.append(
                "yield groups: " + ", ".join(name for name, _ in self.yield_groups)
            )
            if self.yield_orphan_sweep:
                steps.append("yield orphaned declarative group(s)")
        if self.persist_manifest:
            steps.append("persist manifests")
        return steps


def _target_key(written: str, canonical: str | None) -> str:
    """Use the canonical display name when the group already exists, otherwise
    the written casing (the group will be created with that spelling)."""
    return canonical if canonical else written


def _should_install_imperatively(pkg: ResolvedPackage, group_backend: Backend | None = None) -> bool:
    """Determine if a package should be installed imperatively based on its
    installation method and the group's backend state.

    Per spec §90:
    - ``imperative``: always install via nix profile
    - ``declarative``: never install via nix profile (use NixOS config)
    - ``auto``: install imperatively only if the group is not declarative
    """
    if pkg.install_method == InstallMethod.IMPERATIVE:
        return True
    if pkg.install_method == InstallMethod.DECLARATIVE:
        return False
    # AUTO: install imperatively only if group is not declarative
    if group_backend is None or group_backend != Backend.DECLARATIVE:
        return True
    return False


def plan(cmd: Command, manager: GroupManager, resolver: "PackageResolver") -> Plan:
    op = cmd.operation
    explicit_names = [p.name for p in cmd.explicit_packages()]
    resolved_explicit = resolver.resolve_many(explicit_names) if explicit_names else []
    
    # Set install_method based on command's install_method for explicit packages
    if cmd.install_method is not InstallMethod.IMPERATIVE:
        from ..core.models import ResolvedPackage as RP
        resolved_explicit = [
            RP(
                requested=p.requested,
                attribute=p.attribute,
                source=p.source,
                revision=p.revision,
                resolution_timestamp=p.resolution_timestamp,
                install_method=cmd.install_method,
            )
            for p in resolved_explicit
        ]

    if cmd.is_declarative_install:
        return _plan_declarative_install(cmd, manager, resolved_explicit, resolver)

    add_to_groups: dict[str, list[ResolvedPackage]] = {}
    remove_from_groups: dict[str, list[str]] = {}
    remove_from_all: list[str] | None = None
    install_pkgs: list[ResolvedPackage] = []
    remove_from_profile: list[str] = []
    wipe_profile = False
    ensure_groups: list[str] = []
    # Initialize state lists for ALL operations (not just state ops)
    notes: list[str] = []
    state_activate: list[tuple[str, str]] = []
    state_deactivate: list[str] = []
    state_promote: list[tuple[str, list[ResolvedPackage]]] = []
    state_demote: list[str] = []
    # Lifecycle operation lists (-O/-Y, spec §§33-50)
    obliterate: list[str] = []
    yield_groups: list[tuple[str, list[ResolvedPackage]]] = []
    obliterate_declaration = False
    obliterate_save = False
    obliterate_orphan_sweep = False
    yield_orphan_sweep = False
    state_demote: list[str] = []

    def all_group_names() -> list[str]:
        return manager.list_groups()

    if op is Operation.INSTALL:
        if cmd.groups_flag and cmd.groups and not cmd.all_groups:
            for i, ref in enumerate(cmd.groups):
                key = _target_key(ref.name, manager.canonical(ref.name))
                pkgs = _resolve_slot(cmd, i, resolver)
                if pkgs:
                    add_to_groups[key] = add_to_groups.get(key, []) + pkgs
                    # Filter packages by installation method
                    group = manager.get(ref.name)
                    group_backend = group.state.backend if group else None
                    for pkg in pkgs:
                        if _should_install_imperatively(pkg, group_backend):
                            install_pkgs.append(pkg)
                        else:
                            notes.append(
                                f"[deferred] {pkg.requested} -> declarative (skipped imperative install)"
                            )
                else:
                    existing = manager.get(ref.name)
                    if existing:
                        group_backend = existing.state.backend
                        for pkg in existing.packages:
                            if _should_install_imperatively(pkg, group_backend):
                                install_pkgs.append(pkg)
                            else:
                                notes.append(
                                    f"[deferred] {pkg.requested} -> declarative (skipped imperative install)"
                                )
                ensure_groups.append(ref.name)
        elif cmd.groups_flag and cmd.all_groups:
            for name in all_group_names():
                add_to_groups[name] = list(resolved_explicit)
            install_pkgs.extend(resolved_explicit)
        elif cmd.groups:
            for ref in cmd.groups:
                existing = manager.get(ref.name)
                if existing is None:
                    raise GroupError(
                        Diagnostic(
                            ErrorCode.INVALID_GROUP,
                            f"group '{ref.name}' does not exist",
                            suggestion="create it with 'nist group create' "
                            "or use -IG#{group}#{packages} to create it while installing",
                        )
                    )
                group_backend = existing.state.backend
                for pkg in existing.packages:
                    if _should_install_imperatively(pkg, group_backend):
                        install_pkgs.append(pkg)
                    else:
                        notes.append(
                            f"[deferred] {pkg.requested} -> declarative (skipped imperative install)"
                        )
        elif cmd.all_groups:
            install_pkgs.extend(manager.union_contents(all_group_names()))
        else:
            # Bare install: filter by installation method
            for pkg in resolved_explicit:
                if _should_install_imperatively(pkg):
                    install_pkgs.append(pkg)
                else:
                    notes.append(
                        f"[deferred] {pkg.requested} -> declarative (skipped imperative install)"
                    )

    elif op is Operation.REMOVE:
        if cmd.groups_flag:
            if cmd.all_groups:
                if not explicit_names:
                    raise PlanError(
                        Diagnostic(
                            ErrorCode.INVALID_PACKAGE,
                            "no packages to remove from all groups",
                            suggestion="write '-RG* {packages}'",
                        )
                    )
                remove_from_all = list(explicit_names)
            else:
                for i, ref in enumerate(cmd.groups):
                    canonical = manager.canonical(ref.name)
                    if canonical is None:
                        raise GroupError(
                            Diagnostic(
                                ErrorCode.INVALID_GROUP,
                                f"group '{ref.name}' does not exist",
                                suggestion="cannot remove packages from a group that does not exist",
                            )
                        )
                    names = _slot_or_clear(cmd, i, resolver, manager, canonical)
                    if names:
                        remove_from_groups[canonical] = names
        elif cmd.profile_only:
            remove_from_profile = list(explicit_names)
        elif cmd.groups:
            for i, ref in enumerate(cmd.groups):
                canonical = manager.canonical(ref.name)
                if canonical is None:
                    raise GroupError(
                        Diagnostic(
                            ErrorCode.INVALID_GROUP,
                            f"group '{ref.name}' does not exist",
                        )
                    )
                names = _slot_or_clear(cmd, i, resolver, manager, canonical)
                if names:
                    remove_from_groups[canonical] = names
        elif cmd.all_groups:
            if explicit_names:
                remove_from_all = list(explicit_names)
                remove_from_profile = list(explicit_names)
            else:
                wipe_names: list[str] = []
                for name in all_group_names():
                    group = manager.get(name)
                    if group:
                        wipe_names.extend(p.requested for p in group.packages)
                remove_from_all = _dedupe_names(wipe_names)
                wipe_profile = True
        else:
            remove_from_all = list(explicit_names)
            remove_from_profile = list(explicit_names)

    elif op is Operation.ADD:
        if cmd.all_groups:
            for name in all_group_names():
                add_to_groups[name] = list(resolved_explicit)
        else:
            for i, ref in enumerate(cmd.groups):
                key = _target_key(ref.name, manager.canonical(ref.name))
                pkgs = _resolve_slot(cmd, i, resolver)
                if pkgs:
                    add_to_groups[key] = add_to_groups.get(key, []) + pkgs
                ensure_groups.append(ref.name)

    elif op in (Operation.PROMOTE, Operation.DEMOTE, Operation.DEACTIVATE, Operation.ACTIVATE):
        state_activate: list[tuple[str, str]] = []
        state_deactivate: list[str] = []
        state_promote: list[tuple[str, list[ResolvedPackage]]] = []
        state_demote: list[str] = []
        notes: list[str] = []
        _plan_state_operation(
            cmd,
            manager,
            add_to_groups,
            install_pkgs,
            notes,
            state_activate,
            state_deactivate,
            state_promote,
            state_demote,
        )
    elif op in (Operation.OBLITERATE, Operation.YIELD):
        state_activate = []
        state_deactivate = []
        state_promote = []
        state_demote = []
        notes = []
        _plan_lifecycle_operation(
            cmd,
            manager,
            notes,
            obliterate,
            yield_groups,
        )
        obliterate_declaration = cmd.obliterate_declaration_scope >= 1
        obliterate_orphan_sweep = cmd.obliterate_orphan_sweep
        obliterate_save = cmd.save
        yield_orphan_sweep = cmd.yield_orphan_sweep
    else:
        state_activate = []
        state_deactivate = []
        state_promote = []
        state_demote = []
        notes = []

    return Plan(
        add_to_groups=add_to_groups,
        remove_from_groups=remove_from_groups,
        remove_from_all_groups=remove_from_all,
        install=dedupe_packages(install_pkgs),
        remove_from_profile=_dedupe_names(remove_from_profile),
        ensure_groups=_dedupe_names(ensure_groups),
        wipe_profile=wipe_profile,
        persist_manifest=bool(
            add_to_groups
            or remove_from_groups
            or remove_from_all
            or ensure_groups
            or obliterate
            or yield_groups
        ),
        notes=notes,
        activate=state_activate,
        deactivate=state_deactivate,
        promote=state_promote,
        demote=state_demote,
        obliterate=obliterate,
        yield_groups=yield_groups,
        obliterate_declaration=obliterate_declaration,
        obliterate_save=obliterate_save,
        obliterate_orphan_sweep=obliterate_orphan_sweep,
        yield_orphan_sweep=yield_orphan_sweep,
    )


def _plan_lifecycle_operation(
    cmd: Command,
    manager: GroupManager,
    notes: list[str],
    obliterate: list[str],
    yield_groups: list[tuple[str, list[ResolvedPackage]]],
) -> None:
    """Populate registry-level plans for -O (obliterate) and -Y (yield).

    Declarative-configuration work is surfaced through the plan flags
    (``obliterate_declaration``/``obliterate_orphan_sweep`` and
    ``yield_orphan_sweep``); the promotional subsystem performs the actual
    configuration edits and must use the transactional candidate mechanism
    (spec §40).
    """
    op = cmd.operation
    if op is Operation.OBLITERATE:
        if cmd.all_groups:
            targets = manager.list_groups()
            if not targets:
                notes.append("[!] No groups exist to obliterate.")
        else:
            targets = [g.name for g in cmd.groups]
        obliterate.extend(targets)
        return

    # -Y yield: selected group plus (optionally) orphan adoption.
    for ref in cmd.groups:
        yield_groups.append((ref.name, []))


def _plan_state_operation(
    cmd: Command,
    manager: GroupManager,
    add_to_groups: dict[str, list[ResolvedPackage]],
    install_pkgs: list[ResolvedPackage],
    notes: list[str],
    state_activate: list[tuple[str, str]],
    state_deactivate: list[str],
    state_promote: list[tuple[str, list[ResolvedPackage]]],
    state_demote: list[str],
) -> None:
    """Populate the state-transition lists for -P/-D/-E/-A.

    Only read-only planning happens here; declarative representation work is
    performed later by the promotion/demotion transactions.
    """
    op = cmd.operation
    targets = _state_targets(cmd, manager)

    if op is Operation.PROMOTE:
        for name in targets:
            group = manager.get(name)
            if group is None:
                continue
            if group.state.backend is Backend.DECLARATIVE:
                notes.append(f"[!] Group '{name}' is already declarative; nothing to promote.")
                continue
            state_promote.append((name, list(group.packages)))

    elif op is Operation.DEMOTE:
        for name in targets:
            group = manager.get(name)
            if group is None:
                continue
            if group.state.backend is not Backend.DECLARATIVE:
                notes.append(f"[!] Group '{name}' has no declarative representation; nothing to demote.")
                continue
            state_demote.append(name)

    elif op is Operation.DEACTIVATE:
        for name in targets:
            group = manager.get(name)
            if group is None:
                continue
            if not group.state.active:
                notes.append(f"[!] Group '{name}' is already inactive.")
                continue
            state_deactivate.append(name)

    elif op is Operation.ACTIVATE:
        for name in targets:
            group = manager.get(name)
            if group is None:
                continue
            wanted = _wanted_backend(cmd, group)
            if wanted is Backend.DECLARATIVE:
                if group.state.active and group.state.backend is Backend.DECLARATIVE:
                    notes.append(f"[!] Group '{name}' is already active declaratively.")
                    continue
                if group.state.backend is not Backend.DECLARATIVE:
                    state_promote.append((name, list(group.packages)))
                state_activate.append((name, "declarative"))
            else:
                if group.state.active and group.state.backend is Backend.IMPERATIVE:
                    notes.append(f"[!] Group '{name}' is already active imperatively.")
                    continue
                state_activate.append((name, "imperative"))
                # Filter packages by installation method when activating imperatively
                for pkg in group.packages:
                    if _should_install_imperatively(pkg, Backend.IMPERATIVE):
                        install_pkgs.append(pkg)
                    else:
                        notes.append(
                            f"[deferred] {pkg.requested} -> declarative (skipped imperative install)"
                        )


def _state_targets(cmd: Command, manager: GroupManager) -> list[str]:
    if cmd.all_groups:
        names = manager.list_groups()
        if not names:
            raise PlanError(
                Diagnostic(ErrorCode.INVALID_GROUP, "no groups exist to operate on")
            )
        return names
    names: list[str] = []
    for ref in cmd.groups:
        canonical = manager.canonical(ref.name)
        if canonical is None:
            raise GroupError(
                Diagnostic(
                    ErrorCode.INVALID_GROUP,
                    f"group '{ref.name}' does not exist",
                    suggestion="create it with '-G#<name>' or 'nist group create <name>' first",
                )
            )
        names.append(canonical)
    return names


def _wanted_backend(cmd: Command, group) -> Backend:
    if cmd.target is Target.IMPERATIVE:
        return Backend.IMPERATIVE
    if cmd.target is Target.DECLARATIVE:
        return Backend.DECLARATIVE
    remembered = group.state.backend
    if remembered is Backend.NONE:
        if cmd.operation is Operation.ACTIVATE:
            return Backend.IMPERATIVE  # GS-01: no remembered backend -> imperative
        raise PlanError(
            Diagnostic(
                ErrorCode.INVALID_GROUP,
                f"group '{group.name}' has no remembered activation target",
                suggestion="use -Ai#%s or -Ad#%s to choose an explicit target" % (group.name, group.name),
            )
        )
    return remembered


def _group_by(backends, pairs):
    buckets: dict[str, set[str]] = {b: set() for b in backends}
    for name, backend in pairs:
        buckets.setdefault(backend, set()).add(name)
    return {k: sorted(v) for k, v in buckets.items() if v}


def _plan_declarative_install(
    cmd: Command,
    manager: GroupManager,
    resolved: list[ResolvedPackage],
    resolver: "PackageResolver | None" = None,
) -> Plan:
    """``-Id ...`` declares packages instead of installing them imperatively.

    Per spec §23/§86: with a group scope the packages are added to the group
    (created if needed) and the group is promoted; without a group a logical
    group named after the first package is created and promoted.
    """
    add_to_groups: dict[str, list[ResolvedPackage]] = {}
    ensure_groups: list[str] = []
    promote: list[tuple[str, list[ResolvedPackage]]] = []

    # Set install_method to DECLARATIVE for all packages in declarative install
    from ..core.models import ResolvedPackage as RP
    resolved = [
        RP(
            requested=p.requested,
            attribute=p.attribute,
            source=p.source,
            revision=p.revision,
            resolution_timestamp=p.resolution_timestamp,
            install_method=InstallMethod.DECLARATIVE,
        )
        for p in resolved
    ]

    if cmd.groups_flag and cmd.groups and not cmd.all_groups:
        for i, ref in enumerate(cmd.groups):
            key = _target_key(ref.name, manager.canonical(ref.name))
            slot = _resolve_slot(cmd, i, resolver)
            if slot:
                add_to_groups[key] = add_to_groups.get(key, []) + slot
            ensure_groups.append(ref.name)
        for ref in cmd.groups:
            key = _target_key(ref.name, manager.canonical(ref.name))
            group = manager.get(key)
            if group is not None and group.state.backend is Backend.DECLARATIVE:
                continue
            merged = list(add_to_groups.get(key, []))
            if group is not None:
                merged = dedupe_packages(list(group.packages) + merged)
            promote.append((key, merged))
    elif cmd.all_groups:
        for name in manager.list_groups():
            group = manager.get(name)
            if group and group.packages:
                promote.append((name, list(group.packages)))
    else:
        names = [p.requested for p in resolved]
        if names:
            group_name = _target_key(names[0], manager.canonical(names[0]))
            if manager.canonical(group_name) is None:
                ensure_groups.append(group_name)
                add_to_groups[group_name] = list(resolved)
            promote.append((group_name, list(resolved)))

    return Plan(
        add_to_groups=add_to_groups,
        ensure_groups=_dedupe_names(ensure_groups),
        persist_manifest=bool(add_to_groups or ensure_groups),
        promote=promote,
    )


def _resolve_slot(cmd: Command, index: int, resolver: "PackageResolver") -> list[ResolvedPackage]:
    refs = cmd.assign_packages_for(index)
    names = [p.name for p in refs]
    resolved = resolver.resolve_many(names) if names else []
    # Always set install_method based on command's install_method
    from ..core.models import ResolvedPackage as RP
    resolved = [
        RP(
            requested=p.requested,
            attribute=p.attribute,
            source=p.source,
            revision=p.revision,
            resolution_timestamp=p.resolution_timestamp,
            install_method=cmd.install_method,
        )
        for p in resolved
    ]
    return resolved


def _slot_or_clear(
    cmd: Command,
    index: int,
    resolver: "PackageResolver",
    manager: GroupManager,
    canonical: str,
) -> list[str]:
    """Packages for a positional/broadcast slot, or the group's contents when
    the command carries no explicit packages (``-R#Group`` clears the group)."""
    pkgs = _resolve_slot(cmd, index, resolver)
    if pkgs:
        return [p.requested for p in pkgs]
    existing = manager.get(canonical)
    if existing:
        return [p.requested for p in existing.packages]
    return []


def _dedupe_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out