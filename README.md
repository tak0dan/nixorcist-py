# nixorcist

Higher-level Nix package/profile management and configuration promotion.

Nixorcist sits on top of the `nix` CLI and gives you a compact **DSL** for
operations on packages and groups, an **orthogonal group state model**
(`active` × backend), and a **promotion workflow** that turns imperative
profile state into declarative NixOS configuration.

```
[ ] install packages & group them     [X] track what the profile actually has
[ ] remove / rollback per group       [X] sync status: desired vs actual
[ ] promote profile state → NixOS     [X] imperative ↔ declarative round-trip
```

## Features

- **Compact DSL** — one expression can create groups, assign packages, and act
  on a profile: `nixorcist -IG'#Dev{python,nodejs}'`
- **Broadcast & positional assignment** — `-G{A,B}##{gcc}{git}` or
  `-G{A,B}##{gcc}{}` (explicit empty slots are honored)
- **Orthogonal state model** — every group has `active` (`true`/`false`) and a
  backend (`imperative` / `declarative`), so "installed" and "declared" are
  tracked independently
- **Promotion workflow** — stage imperative profile state as a candidate,
  validate it, and commit it into your NixOS config (`promote`); roll back with
  `demote`. Backed by operation IDs, history, a queue, and a lock
- **Lifecycle operations** — `-O` obliterate a group's declarations and
  `-Y` yield a group to the registry while keeping the config untouched
- **Full offline testability** — `--dry-run` resolves and plans without
  touching the profile or the config

## Install

Nixorcist can be installed any of several ways, without losing any
functionality:

1. **Nix profile (flake)**
   ```sh
   nix profile install "path:$PWD#default" --replace nixorcist
   ```

2. **Plain Python**
   ```sh
   pip install .
   # or, for development
   pip install -e .[test]
   ```
   Python 3.10+; the only runtime requirement is the `nix` executable on PATH.

3. **configuration.nix (plain package, no flake input)**
   ```nix
   # In your NixOS configuration:
   nixpkgs.overlays = [
     (import /path/to/nixorcist/overlay.nix)
   ];
   environment.systemPackages = [ pkgs.nixorcist ];
   ```
   Both `nixorcist` and the shorter alias `nist` are exposed.

## Usage

```
nixorcist <expression>              # run the compact DSL
nixorcist <subcommand> [...]        # group | status | promote | export | import | resolve
```

### DSL at a glance

| Token | Meaning                                    |
|-------|--------------------------------------------|
| `-I`  | install / activate                          |
| `-R`  | remove                                      |
| `-G`  | select / create group                       |
| `-A`  | activate                                    |
| `-P`  | promote → declarative backend               |
| `-D`  | demote → imperative backend                 |
| `-E`  | deactivate (`active := false`)              |
| `-O`  | obliterate (`-Ooo` also sweeps orphans)     |
| `-Y`  | yield (`-Yyy` also sweeps orphans)          |
| `-S`  | sequence mode (last-successful-base chain)  |
| `-M`  | method casting (per-group install method)   |
| `-i` / `-d` | imperative / declarative target       |
| `#`   | scope separator / broadcast assignment      |
| `##`  | positional assignment                       |
| `{ }` | collection, `,` separator                   |
| `*`   | "all groups" scope                          |
| `.`   | "profile only" scope                        |

### Examples

```sh
# Create/install group Dev with python and nodejs, and install them
nixorcist -IG'#Dev{python,nodejs}'

# Broadcast packages to several groups
nixorcist -G'{Work,Labs}{rust,cargo}'

# Positional assignment: Work gets gcc, Labs gets git (explicit empty ok)
nixorcist -G'{Work,Labs}##{gcc}{git}'
nixorcist -G'{Work,Labs}##{gcc}{}'

# Install everything a group already declares into the profile
nixorcist -I'#Dev'

# Remove from the actual profile only
nixorcist -R'#.{cowsay,hello}'

# Deactivate a group (state change only)
nixorcist -E'#Dev'

# Promote/profile state as a candidate, then commit it to declarative NixOS
nixorcist -P'#Dev'
nixorcist promote Dev
```

### Subcommands

```sh
nixorcist group create Work --packages git cargo
nixorcist group list
nixorcist group show Work
nixorcist group remove Work
nixorcist status                  # desired vs actual sync report
nixorcist promote [--dry-run]     # candidate-based promotion
nixorcist export  [--format json] # backup group manifests
nixorcist import  <file.toml>     # restore a manifest
nixorcist resolve <pkg>           # print the resolved nixpkgs attribute
```

Global flags (can appear anywhere before the DSL/subcommand body):

- `--dry-run` — plan + resolve, execute nothing
- `--debug` — verbatim Nix commands, tokens, AST, plan
- `--config-root DIR` — use a different config root (for tests / CI)
- `--profile PATH` — operate on a non-default nix profile

## State & storage

- **`NIXORCIST_HOME`** (default `~/.config/nixorcist/`) holds:
  - `groups/*.toml` — group manifests
  - `cache/` — resolved package cache
  - `state/` — profile pointer, promotion queue/lock
  - `history/` — promotion history (`promotions.toml`, `sequences.toml`)
- **Config root** — the NixOS source tree that promotions write into
  (default: discover `configuration.nix` upward from CWD, or `--config-root`)

## Development

```sh
# nix shell / nix develop
nix develop

# or a plain venv
python -m venv .venv && source .venv/bin/activate
pip install -e .[test]

# run the test suite
pytest
```

```
nix profile list        # what the profile has
nixorcist status        # what groups declare vs the profile
```

## Documentation

- `README.md` — this file
- Named DSL reference: `docs/dsl.md` (single source of truth for the token
  grammar used by the lexer)

## License

MIT — see `LICENSE`.