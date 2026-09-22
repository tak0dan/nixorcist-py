"""Nixorcist command line entrypoint.

Supports both the compact DSL runner (``nixorcist -I pkgs.git``) and a set
of subcommands (``group``, ``status``, ``promote``, ``export``, ``import``,
``resolve``).

Global flags may appear anywhere before the DSL/subcommand body:

    --dry-run, --debug, --config-root DIR, --profile PATH
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

from .. import __version__
from ..cli.ast import Command, Operation, Target
from ..cli.diagnostics import Diagnostic, ErrorCode, NixorcistError, Reporter
from ..cli.parser import parse
from ..cli.validator import validate
from ..core.models import (
    Backend,
    DeclarativeMetadata,
    ResolvedPackage,
    dedupe_packages,
)
from ..core.planner import Plan, PlanError, plan
from ..core.resolver import PackageResolver
from ..groups.manager import GroupManager, validate_group_name
from ..groups.repository import GroupRepository
from ..logging import Logger
from ..storage.locking import FileLock
from ..storage.paths import Paths

_GLOBAL_FLAGS = {"--dry-run", "--debug", "--config-root", "--profile"}
_SUBCOMMANDS = {
    "group",
    "groups",
    "status",
    "promote",
    "demote",
    "export",
    "import",
    "resolve",
    "version",
    "help",
}


def _diag(code: ErrorCode, message: str) -> NixorcistError:
    return NixorcistError(Diagnostic(code, message))


class _Options:
    def __init__(self, argv: Sequence[str]) -> None:
        self.debug = False
        self.dry_run = False
        self.config_root: Path | None = None
        self.profile: str | None = None
        self.rest: list[str] = []
        i = 0
        argv = list(argv)
        while i < len(argv):
            arg = argv[i]
            if arg in ("--debug", "--dry-run"):
                setattr(self, arg.replace("--", "").replace("-", "_"), True)
            elif arg == "--config-root":
                i += 1
                if i >= len(argv):
                    raise _diag(ErrorCode.SYNTAX, "--config-root requires a value")
                self.config_root = Path(argv[i])
            elif arg == "--profile":
                i += 1
                if i >= len(argv):
                    raise _diag(ErrorCode.SYNTAX, "--profile requires a value")
                self.profile = argv[i]
            else:
                self.rest.append(arg)
            i += 1

    @property
    def logger(self) -> Logger:
        return Logger(debug=self.debug, dry_run=self.dry_run)


def _paths(opts: _Options) -> Paths:
    # ``--config-root`` identifies the NixOS tree to inspect/promote; it must
    # never redirect Nixorcist's own registry into that tree.  State remains
    # under ``NIXORCIST_HOME`` (or its normal XDG default).
    return Paths.from_env().ensure()


def _backend(opts: _Options, logger: Logger):
    from ..backends.profile import NixProfile

    return NixProfile(
        profile_path=opts.profile,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# Group subcommand
# ---------------------------------------------------------------------------


def _cmd_group(argv: Sequence[str], opts: _Options) -> int:
    args = list(argv)
    if not args or args[0] in ("--help", "-h"):
        print("usage: nixorcist group <create|list|show|remove|rename> [name ...]")
        return 0
    action, *rest = args
    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    if action == "create":
        if not rest:
            logger.error("create requires a group name")
            return 1
        name = rest[0]
        desc = rest[1] if len(rest) > 1 else ""
        if manager.canonical(name) is not None:
            logger.error(f"group '{name}' already exists")
            return 1
        if not opts.dry_run:
            manager.create(name, description=desc)
        logger.ok(f"created group '{name}'")
        return 0
    if action == "list":
        groups = manager.list_groups()
        if not groups:
            logger.info("no groups")
            return 0
        for gname in groups:
            print(gname)
        return 0
    if action == "show":
        if not rest:
            logger.error("show requires a group name")
            return 1
        name = manager.canonical(rest[0])
        if name is None:
            logger.error(f"group '{rest[0]}' not found")
            return 1
        group = manager.get(name)
        print(f"name: {name}")
        print(f"active: {group.state.active}")
        print(f"backend: {group.state.backend.value}")
        if group.declarative and group.declarative.module:
            print(f"declarative.module: {group.declarative.module}")
        print("packages:")
        for pkg in group.packages:
            print(f"  {pkg.requested}")
        return 0
    if action == "remove":
        if not rest:
            logger.error("remove requires a group name")
            return 1
        name = manager.canonical(rest[0])
        if name is None:
            logger.error(f"group '{rest[0]}' not found")
            return 1
        if not opts.dry_run:
            manager.delete(name)
        logger.ok(f"removed group '{name}'")
        return 0
    if action == "rename":
        if len(rest) < 2:
            logger.error("rename requires OLD NEW")
            return 1
        old = manager.canonical(rest[0])
        if old is None:
            logger.error(f"group '{rest[0]}' not found")
            return 1
        if not opts.dry_run:
            manager.rename(old, rest[1])
        logger.ok(f"renamed '{old}' to '{rest[1]}'")
        return 0
    logger.error(f"unknown group action '{action}'")
    return 1


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _cmd_status(argv: Sequence[str], opts: _Options) -> int:
    from ..profiles.manager import ProfileManager

    paths = _paths(opts)
    logger = opts.logger
    backend = _backend(opts, logger)
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    pman = ProfileManager(backend, logger=logger)
    groups = manager.all_groups()
    print(f"profile: {opts.profile or paths.profile_file}")
    print("groups:")
    for g in groups:
        state = g.state
        decl = g.declarative
        decl_info = f" declarative.module={decl.module}" if decl and decl.module else ""
        print(
            f"  {g.name}\tactive={state.active} backend={state.backend.value}"
            f"\tpackages={len(g.packages)}{decl_info}"
        )
    print("profile entries:")
    for entry in backend.list_entries():
        label = entry.attribute or entry.name
        print(f"  {entry.name}\t{label}")
    statuses = pman.status(groups)
    print("sync status:")
    for st in statuses:
        marker = "+" if st.profile else "-"
        grp = "+" if st.group else "-"
        print(f"  [{marker}] group={grp} requested={st.requested}")
    # Show installation method summary
    print("installation methods:")
    for g in groups:
        for pkg in g.packages:
            method = pkg.install_method.value
            print(f"  {g.name}/{pkg.requested}: {method}")
    return 0


# ---------------------------------------------------------------------------
# Promote / Demote subcommands
# ---------------------------------------------------------------------------


def _cmd_promote(argv: Sequence[str], opts: _Options) -> int:
    from ..promotion.discover import discover
    from ..promotion.history import PromotionHistory, log_promotion
    from ..promotion.lock import PromotionLock
    from ..promotion.planner import plan_promotion
    from ..promotion.transaction import Transaction
    from ..nix.config import discover_root

    paths = _paths(opts)
    logger = opts.logger
    check = "--check" in argv
    names = [a for a in argv if not a.startswith("--")]
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    resolver = PackageResolver(cache_dir=paths.cache_dir, logger=logger)
    targets = names or manager.list_groups()
    if not targets:
        logger.error("no groups to promote")
        return 1
    group_plan: list[tuple[str, list[ResolvedPackage]]] = []
    for raw in targets:
        canonical = manager.canonical(raw)
        if canonical is None:
            logger.error(f"group '{raw}' not found")
            return 1
        group = manager.get(canonical)
        group_plan.append((canonical, list(group.packages)))
    root = discover_root(opts.config_root)
    model = discover(root)
    history = PromotionHistory(paths.history_dir / "promotions.toml")

    if not opts.dry_run:
        # Acquire the configuration lock (§46); while held, other promotions
        # on the same target are routed to the queue (§44).
        lock = PromotionLock(paths.promotion_lock_file, timeout=0.5)
        try:
            lock.acquire(target=str(root.directory), operation_id=_candidate_id("all", "lock"))
        except NixorcistError as exc:
            logger.warn(str(exc))
            logger.info("queueing promotion instead")

    pplan = plan_promotion(model, group_plan)
    tx = Transaction(model, pplan, logger=logger)
    result = tx.run(dry_run=opts.dry_run, commit=not check)
    if opts.dry_run:
        return 0
    for rel in result.changed_files:
        logger.info(f"promoted: {rel}")
    if result.validated and not check:
        for canonical, pkgs in group_plan:
            group = manager.get(canonical)
            if group.state.backend is not Backend.DECLARATIVE:
                manager.promote(canonical)
            decl_path = pplan.new_module_path
            meta = DeclarativeMetadata(
                configuration_root=str(root.directory),
                module=str(decl_path.as_posix()) if decl_path else root.entry_file.name,
                declaration="module" if pplan.strategy == "CREATE_MODULE" else "inline",
                last_successful_candidate=_candidate_id(canonical, "promote"),
                last_promotion=_now_iso(),
            )
            manager.set_declarative_metadata(canonical, meta)
            log_promotion(
                history,
                operation_id=_candidate_id(canonical, "promote"),
                command=f"-P#{canonical}",
                target=canonical,
                previous_state="imperative",
                result_state="declarative",
                status="COMMITTED",
                candidate=",".join(result.changed_files),
            )
        logger.ok("promotion validated and committed")
        logger.info("run 'sudo nixos-rebuild switch' to apply the new configuration")
    elif result.validated and check:
        logger.info("--check: validation build passed; nothing committed")
    elif not result.validated:
        for canonical, _ in group_plan:
            log_promotion(
                history,
                operation_id=_candidate_id(canonical, "promote"),
                command=f"-P#{canonical}",
                target=canonical,
                previous_state="imperative",
                result_state="imperative",
                status="FAILED",
                candidate=",".join(result.changed_files),
            )
        logger.warn("promotion candidate did not validate; live configuration unchanged")
    lock.release()
    return 0


def _cmd_demote(argv: Sequence[str], opts: _Options) -> int:
    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    resolver = PackageResolver(cache_dir=paths.cache_dir, logger=logger)
    names = argv or manager.list_groups()
    for raw in names:
        canonical = manager.canonical(raw)
        if canonical is None:
            logger.error(f"group '{raw}' not found")
            return 1
        group = manager.get(canonical)
        if group.state.backend is not Backend.DECLARATIVE:
            logger.warn(f"[!] group '{canonical}' is not declarative; skipping demotion")
            continue
        if opts.dry_run:
            logger.info(f"would demote '{canonical}'")
            continue
        manager.demote(canonical)
        logger.ok(f"demoted '{canonical}' (backend -> imperative)")
    return 0


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


def _cmd_export(argv: Sequence[str], opts: _Options) -> int:
    fmt = "toml"
    names: list[str] = []
    i = 0
    argv = list(argv)
    while i < len(argv):
        if argv[i] == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
        else:
            names.append(argv[i])
            i += 1
    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    chosen = names or manager.list_groups()
    manifests = []
    for raw in chosen:
        canonical = manager.canonical(raw)
        if canonical is None:
            logger.error(f"group '{raw}' not found")
            return 1
        group = manager.get(canonical)
        from ..groups.manifest import GroupManifest, serialize
        from ..core.models import GroupState

        m = GroupManifest(
            name=canonical,
            packages=list(group.packages),
            state=group.state,
            declarative=group.declarative,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )
        manifests.append(m)
    if fmt in ("json", "JSON"):
        data = []
        for m in manifests:
            data.append({
                "version": 1,
                "group": {
                    "name": m.name,
                    "active": m.state.active,
                    "backend": m.state.backend.value,
                    "packages": [p.to_dict() for p in m.packages],
                },
            })
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        from ..groups.manifest import serialize
        for m in manifests:
            print(serialize(m).rstrip())
    return 0


def _cmd_import(argv: Sequence[str], opts: _Options) -> int:
    paths = _paths(opts)
    logger = opts.logger
    if not argv:
        logger.error("import requires a file path")
        return 1
    src = Path(argv[0])
    if not src.exists():
        logger.error(f"no such file: {src}")
        return 1
    from ..groups.manifest import parse_stream

    try:
        with src.open("rb") as handle:
            manifest = parse_stream(handle, source=str(src))
    except NixorcistError as exc:
        logger.error(str(exc))
        return 1
    repository = GroupRepository(paths)
    if not opts.dry_run:
        repository.save(manifest)
    logger.ok(f"imported group '{manifest.name}' ({len(manifest.packages)} packages)")
    return 0


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _cmd_resolve(argv: Sequence[str], opts: _Options) -> int:
    paths = _paths(opts)
    logger = opts.logger
    resolver = PackageResolver(cache_dir=paths.cache_dir, logger=logger)
    for name in argv:
        resolved = resolver.resolve(name)
        print(f"{resolved.requested}\t->\t{resolved.attribute}")
    return 0


# ---------------------------------------------------------------------------
# Query operations: -L list / -F find / -V validate (spec §22, §§13-16)
# ---------------------------------------------------------------------------


def _cmd_list(argv: Sequence[str], opts: _Options, head: str) -> int:
    from ..cli.query import (
        QueryError,
        discover_config,
        list_declarations,
        list_summary,
        list_tree,
        orphaned_names,
    )

    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)

    merged = head[2:] if head.startswith("-L") else ""
    detailed = "G" in merged
    config = "c" in merged
    orphans = "o" in merged

    for tok in argv:
        if tok in ("--groups", "-G"):
            detailed = True
        elif tok in ("--config", "-c"):
            config = True
        elif tok in ("--orphans", "-o"):
            orphans = True

    if config or orphans:
        model, decls = discover_config(opts.config_root)
        if orphans:
            decls = orphaned_names(manager, model, opts.config_root)
        print(list_declarations(decls))
        return 0

    if detailed:
        print(list_tree(manager.all_groups()))
    else:
        print(list_summary(manager.all_groups()))
    return 0


def _cmd_find(argv: Sequence[str], opts: _Options, head: str) -> int:
    from ..cli.query import (
        QueryError,
        discover_config,
        find_groups,
        orphaned_names,
        parse_find_tokens,
    )

    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)

    tokens = []
    if head.startswith("-F") and head not in ("-F", "--find", "--search"):
        suffix = head[2:]
        if suffix:
            tokens.append(suffix)
    for tok in argv:
        if tok.startswith("-F") and tok not in ("-F", "--find", "--search"):
            suffix = tok[2:]
            if suffix:
                tokens.append(suffix)
        else:
            tokens.append(tok)
    if not tokens and head not in ("-F", "--find", "--search"):
        tokens = body_run(head, argv, "name")

    query = parse_find_tokens(tokens)

    if query.source == "config":
        model, decls = discover_config(opts.config_root)
        for name, _pkgs in decls:
            if query.matches_group(name, _pkgs):
                print(name)
        return 0
    if query.source == "orphans":
        model, _decls = discover_config(opts.config_root)
        decls = orphaned_names(manager, model, opts.config_root)
        for name, _pkgs in decls:
            if query.matches_group(name, _pkgs):
                print(name)
        return 0

    matched = find_groups(query, manager, manager.all_groups())
    if not matched:
        print("(no matching groups)")
        return 0
    for group in matched:
        print(group.name)
    return 0


def body_run(head: str, argv: Sequence[str], mode: str) -> list[str]:
    out: list[str] = []
    for tok in (head, *argv):
        if tok.startswith("-F"):
            continue
        out.append(tok)
    return out


def _cmd_validate(argv: Sequence[str], opts: _Options, head: str) -> int:
    from ..cli.query import (
        QueryError,
        discover_config,
        split_group_refs,
        validate_group,
    )

    paths = _paths(opts)
    logger = opts.logger
    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    backend = _backend(opts, logger)

    installed = {e.attribute or e.package_key for e in backend.list_entries()}

    config_mode = any(tok in ("--config", "-c") for tok in argv)
    refs: list[str] = []
    for tok in argv:
        if tok in ("--config", "-c", "--"):
            continue
        refs.extend(split_group_refs(tok.lstrip("#")))
    if head.startswith("-V") and head != "-V":
        refs.extend(split_group_refs(head[2:].lstrip("#")))

    model = None
    if config_mode:
        model, _decls = discover_config(opts.config_root)
        root = model.root
        print(
            f"configuration root: {root.directory} "
            f"(flake={root.is_flake}, layout={model.configuration_kind.value})"
        )
        for group in manager.all_groups():
            print(validate_group(group, installed, model))
        return 0

    groups = [manager.get(r) for r in refs] if refs else manager.all_groups()
    for group in groups:
        if group is None:
            logger.error(f"group not found: {group}")
            return 1
        print(validate_group(group, installed, model))
    return 0


def _cmd_version() -> int:
    print(f"nixorcist {__version__}")
    return 0


# ---------------------------------------------------------------------------
# DSL runner
# ---------------------------------------------------------------------------


def _run_dsl(tokens_text: str, opts: _Options, reporter: Reporter) -> int:
    paths = _paths(opts)
    logger = opts.logger

    try:
        cmd = parse(tokens_text)
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1
    try:
        validate(cmd)
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1

    repository = GroupRepository(paths)
    manager = GroupManager(repository, logger=logger)
    resolver = PackageResolver(cache_dir=paths.cache_dir, logger=logger)
    backend = _backend(opts, logger)

    try:
        pl = plan(cmd, manager=manager, resolver=resolver)
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1

    for note in pl.notes:
        logger.info(note)
    for step in pl.steps():
        logger.info(f"  {step}")

    if opts.dry_run:
        return 0

    try:
        _execute_plan(pl, cmd, opts, reporter, manager, resolver, backend)
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1
    return 0


def _execute_plan(
    pl: Plan,
    cmd: Command,
    opts: _Options,
    reporter: Reporter,
    manager: GroupManager,
    resolver: PackageResolver,
    backend,
) -> None:
    logger = opts.logger
    paths = _paths(opts)

    # ---- group mutations ------------------------------------------------
    for name in pl.ensure_groups:
        manager.ensure(name)
        logger.info(f"  ensured group '{name}'")
    for name, pkgs in pl.add_to_groups.items():
        manager.add(name, dedupe_packages(pkgs))
        pkg_names = ", ".join(p.requested for p in pkgs)
        logger.info(f"  added to group '{name}': {pkg_names}")
    for name, requested in pl.remove_from_groups.items():
        manager.remove(name, requested)
        pkg_names = ", ".join(requested)
        logger.info(f"  removed from group '{name}': {pkg_names}")
    if pl.remove_from_all_groups is not None:
        for gname in manager.list_groups():
            manager.remove(gname, pl.remove_from_all_groups)

    # ---- profile ops ----------------------------------------------------
    if pl.wipe_profile:
        backend.remove_all()
        logger.info("  wiped profile")
    elif pl.remove_from_profile:
        removed = backend.remove([ResolvedPackage(requested=r, attribute=r) for r in pl.remove_from_profile])
        logger.info(f"  removed from profile: {', '.join(pl.remove_from_profile)}")
    if pl.install:
        backend.install(pl.install)
        pkg_names = ", ".join(p.requested for p in pl.install)
        logger.info(f"  installed into profile: {pkg_names}")

    # ---- state transitions (imperative) ---------------------------------
    for name in pl.deactivate:
        manager.deactivate(name)
        logger.ok(f"deactivated '{name}'")

    # ---- obliterate groups ----------------------------------------------
    for name in pl.obliterate:
        manager.delete(name)
        logger.ok(f"obliterated '{name}'")

    # ---- yield groups --------------------------------------------------
    for group_name, pkgs in pl.yield_groups:
        manager.ensure(group_name)
        logger.ok(f"yielded group '{group_name}'")

    # ---- declarative (promote / demote via promotion subsystem) ---------
    if pl.promote:
        _run_promotions(
            pl.promote,
            opts,
            manager,
            reporter,
        )
    for name in pl.demote:
        _run_demotion(name, opts, manager, reporter)

    for name, backend_name in pl.activate:
        if backend_name == "imperative":
            manager.activate(name, Backend.IMPERATIVE)
            logger.ok(f"activated '{name}' (imperative)")
        else:
            manager.activate(name, Backend.DECLARATIVE)
            logger.ok(f"activated '{name}' (declarative)")

    # ---- declarative install special case (-Id): activate declaratively --
    if cmd.is_declarative_install and cmd.groups:
        for ref in cmd.groups:
            canonical = manager.canonical(ref.name) or ref.name
            if not opts.dry_run:
                manager.activate(canonical, Backend.DECLARATIVE)

    # ---- activate groups imperatively if scope given (-Ii#G) ----------
    if (
        cmd.operation is Operation.INSTALL
        and cmd.target in (Target.NONE, Target.IMPERATIVE)
        and not cmd.is_declarative_install
        and cmd.has_scope
        and cmd.groups
    ):
        for ref in cmd.groups:
            canonical = manager.canonical(ref.name) or ref.name
            if manager.canonical(ref.name) is not None:
                manager.activate(canonical, Backend.IMPERATIVE)
                logger.ok(f"activated '{canonical}' (imperative)")


def _run_promotions(
    promote: list[tuple[str, list[ResolvedPackage]]],
    opts: _Options,
    manager: GroupManager,
    reporter: Reporter,
) -> None:
    from ..nix.config import discover_root
    from ..promotion.discover import discover
    from ..promotion.planner import plan_promotion
    from ..promotion.transaction import Transaction
    from ..logging import Logger as Lg

    root = discover_root(opts.config_root)
    model = discover(root)
    for group_name, pkgs in promote:
        plan = plan_promotion(model, [(group_name, pkgs)])
        tx = Transaction(model, plan, logger=opts.logger)
        result = tx.run(dry_run=opts.dry_run)
        if not opts.dry_run and result.validated:
            decl_path = plan.new_module_path
            meta = DeclarativeMetadata(
                configuration_root=str(root.directory),
                module=str(decl_path.as_posix()) if decl_path else root.entry_file.name,
                declaration="module" if plan.strategy == "CREATE_MODULE" else "inline",
                last_successful_candidate=_candidate_id(group_name, "promote"),
                last_promotion="",
            )
            manager.promote(group_name)
            manager.set_declarative_metadata(group_name, meta)
            logger = opts.logger
            logger.ok(f"promoted '{group_name}'")
    if opts.dry_run:
        opts.logger.info("(dry-run: promotion details printed above)")


def _run_demotion(
    group_name: str,
    opts: _Options,
    manager: GroupManager,
    reporter: Reporter,
) -> None:
    from ..nix.config import discover_root
    from ..promotion.discover import discover
    from ..promotion.planner import plan_demotion
    from ..promotion.transaction import Transaction

    root = discover_root(opts.config_root, dry_run=opts.dry_run)
    model = discover(root)
    group = manager.get(group_name)
    pkgs = group.packages if group else []
    dem_plan = plan_demotion(model, group_name, pkgs, group.declarative if group else None)
    tx = Transaction(model, dem_plan, logger=opts.logger)
    result = tx.run(dry_run=opts.dry_run)
    if not opts.dry_run and result.validated:
        manager.demote(group_name)
        opts.logger.ok(f"demoted '{group_name}'")


def _candidate_id(name: str, op: str) -> str:
    import time as _time

    stamp = f"{_time.time_ns():.0f}-{op}-{name}"
    return hashlib.sha1(stamp.encode()).hexdigest()[:10]


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reporter = Reporter()
    try:
        opts = _Options(argv)
    except NixorcistError as exc:
        reporter.report(exc.diagnostic)
        return 1

    body = opts.rest
    if not body:
        print("usage: nixorcist [DSL | <command>]", file=sys.stderr)
        return 0

    head = body[0]
    if head in ("version", "--version"):
        return _cmd_version()
    if head in ("help", "--help", "-h"):
        print(__doc__.strip())
        return 0
    if head in ("status",):
        return _cmd_status(body[1:], opts)
    if head in ("group", "groups"):
        return _cmd_group(body[1:], opts)
    if head == "promote":
        return _cmd_promote(body[1:], opts)
    if head == "demote":
        return _cmd_demote(body[1:], opts)
    if head == "export":
        return _cmd_export(body[1:], opts)
    if head == "import":
        return _cmd_import(body[1:], opts)
    if head == "resolve":
        return _cmd_resolve(body[1:], opts)
    if head in ("-L", "--list") or head.startswith("-L"):
        return _cmd_list(body[1:], opts, head)
    if head in ("-F", "--find", "--search") or head.startswith("-F"):
        return _cmd_find(body[1:], opts, head)
    if head in ("-V", "--validate") or head.startswith("-V"):
        return _cmd_validate(body[1:], opts, head)

    return _run_dsl(" ".join(body), opts, reporter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
