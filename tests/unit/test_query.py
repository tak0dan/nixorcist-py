"""Tests for the query operations: ``-L`` (list), ``-F`` (find), ``-V``
(validate) per spec §§13-16 and §22.

Query functions are pure (string rendering / matching), so we test the logic
directly through :mod:`nixorcist.cli.query` and the thin CLI entrypoints
exercised via ``main()`` with an isolated ``NIXORCIST_HOME``.
"""

from __future__ import annotations

import os
from contextlib import redirect_stdout
from io import StringIO

import pytest

from nixorcist.cli.entry import main
from nixorcist.cli.query import (
    FindQuery,
    Predicate,
    find_groups,
    list_declarations,
    list_summary,
    list_tree,
    parse_find_tokens,
)
from nixorcist.core.models import Backend, Group, GroupState, ResolvedPackage


def _pkg(name, attr=None):
    return ResolvedPackage(requested=name, attribute=attr or name)


def _group(name, packages, active=False, backend=Backend.NONE):
    return Group(
        name=name,
        packages=packages,
        state=GroupState(active=active, backend=backend),
    )


# ---------------------------------------------------------------------------
# predicate parsing
# ---------------------------------------------------------------------------


class TestParsePatterns:
    def test_contains(self):
        q = parse_find_tokens(["name=%dev%"])
        p = q.name_preds[0]
        assert p.mode == "contains"
        assert p.value == "dev"
        assert p.matches_name("DevTools")
        assert not p.matches_name("Testing")

    def test_startswith(self):
        q = parse_find_tokens(["name=gaming%"])
        p = q.name_preds[0]
        assert p.mode == "startswith"
        assert p.matches_name("Gaming")
        assert not p.matches_name("NonGaming")

    def test_endswith(self):
        q = parse_find_tokens(["-Fname=%amin"])
        p = q.name_preds[0]
        assert p.mode == "endswith"
        assert p.matches_name("Benjamin")
        assert not p.matches_name("Programming")

    def test_exact(self):
        q = parse_find_tokens(["name=Programming"])
        p = q.name_preds[0]
        assert p.mode == "exact"
        assert p.matches_name("Programming")
        assert not p.matches_name("Prog")

    def test_bare_token_is_contains(self):
        q = parse_find_tokens(["dev"])
        assert len(q.name_preds) == 1
        assert q.name_preds[0].mode == "contains"

    def test_content_single(self):
        q = parse_find_tokens(["-git-"])
        assert len(q.content_preds) == 1
        p = q.content_preds[0]
        assert p.mode == "contains"
        assert p.matches_package("git")


class TestParseContentSets:
    def test_content_all(self):
        q = parse_find_tokens(["-{git,gcc}-"])
        p = q.content_preds[0]
        assert p.mode == "all"
        assert sorted(p.values) == ["gcc", "git"]

    def test_content_all_matches_group_having_both(self):
        q = parse_find_tokens(["-{git,gcc}-"])
        groups = [
            _group("DevTools", [_pkg("git"), _pkg("gcc")]),
            _group("GitOnly", [_pkg("git")]),
        ]
        assert [g.name for g in find_groups(q, None, groups)] == ["DevTools"]

    def test_content_any(self):
        q = parse_find_tokens(["-{git|gcc}-"])
        p = q.content_preds[0]
        assert p.mode == "any"
        assert set(p.values) == {"gcc", "git"}

    def test_content_any_matches_group_having_one(self):
        q = parse_find_tokens(["-{git|gcc}-"])
        groups = [
            _group("DevTools", [_pkg("git")]),
            _group("Media", [_pkg("ffmpeg")]),
        ]
        assert [g.name for g in find_groups(q, None, groups)] == ["DevTools"]

    def test_compound_contains(self):
        q = parse_find_tokens(["name=%am%in%"])
        p = q.name_preds[0]
        assert p.mode == "compound"
        assert p.matches_name("Benjamin")
        assert p.matches_name("Programming")
        assert not p.matches_name("Gaming")

    def test_compound_mixed(self):
        q = parse_find_tokens(["--name", "a%b%c"])
        p = q.name_preds[0]
        assert p.mode == "compound"
        assert p.matches_name("alpha-beta-char")
        assert not p.matches_name("alpha-bet")
        assert not p.matches_name("char-beta-alpha")

    def test_source_selectors(self):
        assert parse_find_tokens(["c"]).source == "config"
        assert parse_find_tokens(["--config"]).source == "config"
        assert parse_find_tokens(["o"]).source == "orphans"
        assert parse_find_tokens(["reg"]).source == "registry"


# ---------------------------------------------------------------------------
# name + content AND semantics
# ---------------------------------------------------------------------------


class TestFindMatching:
    def test_name_and_content_intersection(self):
        q = parse_find_tokens(["name=%dev%", "-git-"])
        groups = [
            _group("DevTools", [_pkg("git"), _pkg("nix")]),
            _group("Development", [_pkg("python")]),
            _group("DevNull", [_pkg("python")]),
        ]
        assert [g.name for g in find_groups(q, None, groups)] == ["DevTools"]

    def test_name_contains_many(self):
        q = parse_find_tokens(["name=%amin%"])
        groups = [
            _group("Gaming", [_pkg("steam")]),
            _group("GamingTools", [_pkg("lutris")]),
            _group("Gaming-Utils", [_pkg("mangohud")]),
            _group("Media", [_pkg("ffmpeg")]),
        ]
        assert [g.name for g in find_groups(q, None, groups)] == [
            "Gaming",
            "GamingTools",
            "Gaming-Utils",
        ]

    def test_no_predicates_matches_all(self):
        q = FindQuery()
        groups = [_group("A", []), _group("B", [])]
        assert len(find_groups(q, None, groups)) == 2


# ---------------------------------------------------------------------------
# -L rendering
# ---------------------------------------------------------------------------


class TestListRendering:
    def test_summary_layout(self):
        groups = [
            _group("Programming", [_pkg("python"), _pkg("gcc"), _pkg("git")], active=True),
            _group("Games", [_pkg("steam")], active=False, backend=Backend.DECLARATIVE),
            _group("Media", [_pkg("ffmpeg")] * 8),
        ]
        text = list_summary(groups)
        assert "Programming" in text
        assert "imperative" in text
        assert "inactive" in text
        assert text.index("Programming") < text.index("Games")

    def test_summary_empty(self):
        assert list_summary([]) == "(no groups)"

    def test_tree(self):
        groups = [_group("Programming", [_pkg("python"), _pkg("git")])]
        text = list_tree(groups)
        assert "Programming" in text
        assert "├── python" in text
        assert "└── git" in text

    def test_tree_empty_groups(self):
        text = list_tree([_group("Empty", [])])
        assert "└── (no packages)" in text

    def test_declarations(self):
        text = list_declarations([("Dev", [_pkg("git")])])
        assert "Dev (git)" in text


# ---------------------------------------------------------------------------
# -V rendering
# ---------------------------------------------------------------------------


class TestValidate:
    def test_inactive_group_profile_ok(self):
        group = _group("Programming", [_pkg("python", "legacyPackages.x86_64-linux.python3")])
        installed = {"legacyPackages.x86_64-linux.python3"}
        from nixorcist.cli.query import validate_group

        text = validate_group(group, installed)
        assert "Packages:        1" in text
        assert "Profile:         OK" in text

    def test_active_group_missing_package_bad(self):
        group = _group(
            "Programming",
            [_pkg("git", "legacyPackages.x86_64-linux.git")],
            active=True,
        )
        from nixorcist.cli.query import validate_group

        text = validate_group(group, set())
        assert "Profile:         BAD" in text
        assert "legacyPackages.x86_64-linux.git" in text


# ---------------------------------------------------------------------------
# CLI-level dispatch (isolated NIXORCIST_HOME)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path):
    """Run the CLI under an isolated home; Nix operations are stubbed out by
    pointing at a profile-less backend cache dir and using --config-root."""
    old = os.environ.get("NIXORCIST_HOME")
    os.environ["NIXORCIST_HOME"] = str(tmp_path)
    yield tmp_path
    if old is None:
        os.environ.pop("NIXORCIST_HOME", None)
    else:
        os.environ["NIXORCIST_HOME"] = old


def _run(*argv, env_home=None):
    out = StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class TestCliQueryDispatch:
    def test_list_summary_via_cli(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-L")
        assert code == 0
        assert "Programming" in out
        assert "PACKAGES" in out

    def test_list_tree_via_cli(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-LG")
        assert code == 0
        assert "├── python3" in out

    def test_find_by_name_contains(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-Fname=%pro%")
        assert code == 0
        assert out.strip() == "Programming"

    def test_find_by_content(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-F-git-")
        assert code == 0
        assert out.strip() == "Programming"

    def test_find_no_match(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-F-gcc-")
        assert code == 0
        assert "(no matching groups)" in out

    def test_validate_no_crash(self, cli_env):
        main(["-G#Programming#{python3,git}"])
        code, out = _run("-V#Programming")
        assert code == 0
        assert "Programming" in out
        assert "State:" in out