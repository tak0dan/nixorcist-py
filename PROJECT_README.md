# Nixorcist developer handoff

This document records the current verification state, the source of truth for
the design, and the commands a developer should run before changing the
project.

## Design contract

The authoritative design is
`/home/tak_1/Downloads/Nixorcist-plan.pdf` (September 2026, v4.0).  Nixorcist
is a persistent group-management layer above Nix; it does not replace Nix's
package evaluation, store, profiles, or NixOS activation.

The central contract is:

```
CLI input -> lexer -> parser -> AST -> validation -> plan -> backend -> Nix
```

Parsing and planning must be read-only.  In particular, a promotion must
create and validate a candidate configuration before the live NixOS
configuration or group state is changed.

### DSL invariants

- The first `#` starts broadcast assignment; braced sections remain broadcast
  until another `#` is encountered.
- The next `#` enters ordered assignment permanently. `##` is ordered-only
  shorthand with an intentionally empty broadcast section.
- `{}` is a meaningful empty ordered slot. Missing trailing slots are empty,
  but missing intermediate slots must be written explicitly.
- More ordered sections than selected groups is an error; no section may be
  silently discarded.
- Group identity and package membership survive promote, demote, deactivate,
  and activate.

### Configuration architecture and safe promotion

Configuration discovery parses Nix tokens into a structural AST before it
follows static `imports` edges or changes a declaration. It classifies a tree
as `plain` when the package declaration belongs to the entry configuration
(including the normal `hardware-configuration.nix` import) and `modular` when
the declaration belongs to an imported module. Directory names are not used
as a proxy for architecture.

For promotion, Nixorcist chooses a parsed `environment.systemPackages` or
`home.packages` list in the entry or an imported module and extends that exact
list. If a modular root has an imports list but no package declaration, it
creates a dedicated module and inserts its import structurally. It refuses to
guess when the relevant expression cannot be parsed as a list. Every edit is
made in a candidate tree, re-parsed, validated, and only then committed.

See [docs/dsl.md](docs/dsl.md) for the user-facing grammar and shell quoting
guidance.

## Validation performed (2026-09-17)

1. Read the project sources, tests, README, DSL documentation, and the plan
   PDF.
2. Entered the Nix development environment and ran the full test suite:

   ```sh
   nix develop --command pytest -q -ra
   ```

   Result: passed. The suite currently collects **359 tests**.

3. Built the installable flake package:

   ```sh
   nix build -L .#default
   ```

   Result: passed.

4. Ran source-level DSL smoke tests for broadcast, ordered-only with an
   explicit empty slot, mixed broadcast/ordered syntax, and ordered overflow.
   Result: valid expressions passed validation; overflow was rejected.
5. Exercised configuration discovery and promotion planning against synthetic
   plain and modular NixOS trees. The tests verify import-graph layout
   classification, imported package-list detection, generated-module insertion,
   preservation of comments, and that the edited files parse after insertion.
6. Inspected the supplied `/etc/nixos/plain_configuration.nix` without
   modifying it. It is classified as `plain`; its `with pkgs; [ ... ]`
   `environment.systemPackages` list is found structurally, and a promotion
   plans `EXTEND` against that exact file and declaration.

The `--config-root` option selects only the NixOS source tree. It never
changes where Nixorcist stores group manifests; use `NIXORCIST_HOME` to select
an isolated registry for integration tests. Candidate validation also preserves
the selected entry filename, including non-standard names such as
`plain_configuration.nix`.

Candidate copying excludes VCS/build artifacts and the resolver cache inside
an embedded `nixorcist/` checkout. Those files are not NixOS modules and can
have restrictive permissions; excluding them keeps validation focused on the
actual configuration graph.

List insertion preserves structural syntax and the original closing-bracket
line: inserted packages receive the list's element indentation, while `]`
remains on its own existing indentation level. Candidate files are re-parsed
after the edit in the promotion test suite.

### Plain-configuration three-group integration fixture

For a non-destructive integration run, the following groups were created in
an isolated registry at `/tmp/nixorcist-plain-test.pE2fAK`:

- `PlainDev` → `hello`
- `PlainTools` → `jq`
- `PlainShell` → `ripgrep`

Their promotion plan was applied to a temporary copy of
`/etc/nixos/plain_configuration.nix`. The result was structurally re-parsed
and contains the three entries in `environment.systemPackages`, preserving
the list's indentation and closing bracket. The live `/etc/nixos` file was
not changed during this run. A full `nixos-rebuild build` candidate validation
must complete successfully before committing this fixture to the live file.

## Errors encountered and repairs

| Observation | Cause | Resolution |
|---|---|---|
| `pdftotext: command not found` | The base environment does not include PDF utilities. | Used `nix shell nixpkgs#poppler-utils --command pdftotext …` to read the supplied plan. |
| `pytest: command not found` | The base environment is intentionally minimal. | Ran tests through `nix develop`. |
| Nix daemon socket denied in the filesystem sandbox | Nix commands require access to the local daemon. | Re-ran the validation commands with the required daemon permission. |
| `nix develop --command python -c 'import nixorcist'` failed | The development shell supplied Python and pytest but did not expose the checkout's `src/` directory. | The flake shell hook now exports `PYTHONPATH=$PWD/src…`, so direct source imports and `python -m nixorcist` work in `nix develop`. |
| `nix build .#default` could not copy `src/nixorcist/cli/_nist` | The completion file was untracked; Nix flake source snapshots omit untracked files. | Added the required completion asset to Git's index. The package now builds and `nix run .#default -- --help` succeeds. |

The working tree was already dirty when this review began. Those existing
changes were preserved; do not use a reset/checkout to discard them.

## Developer workflow

```sh
nix develop
pytest -q -ra
python -m nixorcist --help
nix build -L .#default
```

For a source-only invocation outside the development shell:

```sh
PYTHONPATH=src python3 -m nixorcist --help
```

Use quoted DSL arguments in normal shells. In zsh, disable brace expansion as
described in `docs/dsl.md`, or quote the entire expression. For example:

```sh
nixorcist '-G#{Work,Labs}#{rust,cargo}'
nixorcist '-G{Work,Labs}##{gcc}{}'
```

## Next steps

1. Keep every new DSL rule covered at lexer, parser, validator, and planner
   levels; include empty slots and overflow cases.
2. Add end-to-end tests that run the installed package executable, not only
   imports from `src/`.
3. Exercise promotion tests against temporary NixOS configuration trees,
   verifying that invalid candidates leave the live tree and group backend
   unchanged.
4. Before merging, run `git diff --check`, the full pytest suite, and
   `nix build -L .#default`.
