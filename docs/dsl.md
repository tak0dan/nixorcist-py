# Nixorcist DSL reference

The compact Nixorcist DSL is whitespace-insensitive at the token level: a single
`argv` element such as

```
-IG#{Programming,Games}##{python,gcc}{steam}
```

and the space-separated form

```
-I -G # Programming , Games ## { python , gcc } { steam }
```

lex identically. The lexer and recursive-descent parser live in
`src/nixorcist/cli/{lexer,parser}.py`.

## Tokens

| Token        | Meaning                              |
|--------------|--------------------------------------|
| `-I` / `--install`  | install operation             |
| `-R` / `--remove`   | remove operation              |
| `-G` / `--group`    | group-scope operation         |
| `#`          | broadcast assignment / scope separator |
| `##`         | positional assignment                |
| `{` `}`      | collection brackets                  |
| `, | !`  | collection separator inside `{...}` |
| `*`          | "all groups" scope                   |
| `.`          | "profile only" scope                 |
| `-P`         | promote   (backend := declarative)   |
| `-D`         | demote    (backend := imperative)    |
| `-E`         | deactivate (active := false)         |
| `-A`         | activate                              |
| `-S`         | sequence mode                         |
| `-i`  / `-d` | imperative / declarative target       |
| `NAME`       | identifiers (packages / groups / values) |

## Operations

| Operation  | Effect                                                            |
|------------|-------------------------------------------------------------------|
| `-I`       | install/activate into the profile (imperative by default)         |
| `-R`       | remove from groups and/or the profile                             |
| `-G`       | select or create a group                                          |
| `-A`       | activate (flip to `active = true`)                                |
| `-P`       | promote — switch a group's backend to *declarative*               |
| `-D`       | demote — switch a group's backend to *imperative*                 |
| `-E`       | deactivate (flip to `active = false`)                             |
| `-O`       | obliterate a group's declarative representation                   |
| `-Y`       | yield a group to the registry (config untouched)                  |

## Scopes

| Scope      | Meaning                                        |
|------------|------------------------------------------------|
| `#G`       | group `G`                                     |
| `#*`       | all groups                                     |
| `#.`       | profile only                                   |
| (none)     | remembered target for `-A`; default imperative profile for `-I` |

## Zsh compatibility

Both zsh and bash perform **brace expansion** (`{a,b}` → `a b`) and
other special-character processing (`|` as pipe, `!` as history).
This silently breaks the DSL if collections are left unquoted.

**zsh** — disable both brace and history expansion by adding these to
`~/.zshrc`:

```zsh
unsetopt brace_expand
unsetopt histexpand
```

With those options set, comma- and exclamation-separated collections
work verbatim:

```zsh
nist -IG#{Programming,Gaming}#{firefox}#{python3}{git,steam}   # comma
nist -IG#{Programming,Gaming}#{firefox}#{python3}{git!steam}   # exclamation
```

The pipe separator `{git|steam}` is always a shell pipe operator
and must be quoted in zsh (or use `!` instead):

```zsh
nist -IG#{Programming,Gaming}#{firefox}#{python3}{git|steam}   # quote!
```

Space-separated names inside braces (e.g. `{git steam}`) work at
the DSL level but are rejected by zsh's parser; quote them or
use commas instead.

**bash** — bash also expands `{a,b}` and treats `|`/`!` as operators.
Single-quote the entire DSL argument there:

```bash
nist '-IG#{Programming,Gaming}#{firefox}#{python3}{git,steam}'
```

## Assignment

**Broadcast** — every braced section before the second `#` applies to every
selected group (`§13`):

```
-G{A,B}{python,gcc}     # A and B both get python, gcc
```

**Ordered / positional** — after `##`, slot *i* maps to group *i* additively
(`§17`). Empty slots are semantically meaningful:

```
-G{A,B}##{gcc}{git}     # A → gcc, B → git
-G{A,B}##{gcc}{}        # A → gcc, B → (nothing)
```

`-G{A,B}##{1}{2}{3}` is a validation error (positional assignment overflow).

## Modifiers

Modifier letters are idempotent past their meaningful threshold:

- `-O` — obliterate (registry-level cleanup by default)
- `-Oo` — also remove the group's declarative representation
- `-Ooo` (≥ 2 `o`) — also sweep *orphaned* declarative groups
- `-Os` / `-O-s` — save a copy of the removed declaration (§41)
- `-Y` — yield the group into the registry (config untouched)
- `-Yy` — yield the group's declarative representation
- `-Yyy` (≥ 2 `y`) — also yield all orphaned declarative groups

## Diagnostics

Invalid expressions are reported as structured diagnostics (`TNxxx`) against
the exact source span (spec §54) — never a traceback.

```
error[TN002]: group 'Nope' does not exist

  hint: create it with 'nist group create' or use -IG#{group}#{packages} to create it while installing
```