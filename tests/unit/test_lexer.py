"""Lexer unit tests.

The lexer is a pure tokenizer over the CLI DSL (spec §§24-25).  It must
distinguish single-``#`` (BROADCAST) from double-``#`` (POSITIONAL), and
emit precise spans for every token so that parser diagnostics can point at
the offending text.
"""

from __future__ import annotations

import pytest

from nixorcist.cli.diagnostics import NixorcistError
from nixorcist.cli.lexer import Lexer, Token, TokenKind, lex


def tokenize(text: str) -> list[Token]:
    return Lexer(text).tokenize()


def kinds(text: str) -> list[str]:
    return [t.kind.value for t in tokenize(text)]


# ---------------------------------------------------------------------------
# Flag recognition
# ---------------------------------------------------------------------------


class TestFlags:
    def test_group_flag(self):
        toks = tokenize("-G#Programming")
        assert toks[0].kind is TokenKind.GROUP
        assert toks[0].text == "-G"

    def test_install_flag(self):
        toks = tokenize("-I#python")
        assert toks[0].kind is TokenKind.INSTALL

    def test_merged_flag_g_i(self):
        toks = tokenize("-IG#python")
        assert any(t.kind is TokenKind.INSTALL for t in toks)
        assert any(t.kind is TokenKind.GROUP for t in toks)

    def test_remove_flag(self):
        assert any(t.kind is TokenKind.REMOVE for t in tokenize("-R#."))

    def test_imperative_selector(self):
        assert any(t.kind is TokenKind.IMPERATIVE for t in tokenize("-Ii#python"))

    def test_declarative_selector(self):
        assert any(t.kind is TokenKind.DECLARATIVE for t in tokenize("-Id#python"))

    def test_activate_selector(self):
        assert any(t.kind is TokenKind.ACTIVATE for t in tokenize("-Ai#Programming"))

    def test_deactivate_selector(self):
        assert any(t.kind is TokenKind.DEACTIVATE for t in tokenize("-E#Programming"))

    def test_sequence_flag(self):
        assert any(t.kind is TokenKind.SEQUENCE for t in tokenize("-P-S#Programming"))

    def test_long_flags(self):
        assert any(t.kind is TokenKind.INSTALL for t in tokenize("--install"))
        assert any(t.kind is TokenKind.GROUP for t in tokenize("--group"))
        assert any(t.kind is TokenKind.OBLITERATE for t in tokenize("--obliterate"))
        assert any(t.kind is TokenKind.OBLITERATE for t in tokenize("--delete-group"))
        assert any(t.kind is TokenKind.YIELD for t in tokenize("--yield"))
        assert any(t.kind is TokenKind.SAVE for t in tokenize("--save"))


class TestObliterateYieldTokens:
    def test_short_obliterate(self):
        assert any(t.kind is TokenKind.OBLITERATE for t in tokenize("-O"))

    def test_short_yield(self):
        assert any(t.kind is TokenKind.YIELD for t in tokenize("-Y"))

    def test_short_save(self):
        assert any(t.kind is TokenKind.SAVE for t in tokenize("-s"))

    def test_os_pair_splits(self):
        kinds_list = [t.kind for t in tokenize("-Os") if t.kind is not TokenKind.EOF]
        assert kinds_list == [TokenKind.OBLITERATE, TokenKind.SAVE]

    def test_modifier_name_after_obliterate(self):
        toks = tokenize("-Ooo#Group")
        o_mods = [t for t in toks if t.kind is TokenKind.NAME and t.text.startswith("o")]
        assert o_mods[0].text == "oo"

    def test_modifier_name_after_yield(self):
        toks = tokenize("-Yyy#Group")
        y_mods = [t for t in toks if t.kind is TokenKind.NAME and t.text.startswith("y")]
        assert y_mods[0].text == "yy"

    def test_bare_save_standalone(self):
        toks = tokenize("--save")
        assert toks[0].kind is TokenKind.SAVE

    def test_long_flag_save(self):
        assert any(t.kind is TokenKind.SAVE for t in tokenize("-s"))


# ---------------------------------------------------------------------------
# # vs ## distinction (spec §24)
# ---------------------------------------------------------------------------


class TestHashVsDoubleHash:
    def test_single_hash_is_broadcast(self):
        toks = tokenize("-G#{A,B}#{1}")
        broadcasts = [t for t in toks if t.kind is TokenKind.BROADCAST]
        assert len(broadcasts) == 2  # scope # + package #

    def test_double_hash_is_positional(self):
        toks = tokenize("-G{A,B}##{1}{2}")
        assert any(t.kind is TokenKind.POSITIONAL for t in toks)

    def test_double_hash_is_single_positional_token(self):
        toks = tokenize("##")
        assert len([t for t in toks if t.kind is TokenKind.POSITIONAL]) == 1
        assert toks[0].text == "##"

    def test_hash_and_double_hash_different_tokens(self):
        toks = tokenize("# ##")
        kinds_list = [t.kind for t in toks if t.kind not in (TokenKind.EOF,)]
        assert kinds_list[0] is TokenKind.BROADCAST
        assert kinds_list[1] is TokenKind.POSITIONAL


# ---------------------------------------------------------------------------
# Braces, commas, name tokens
# ---------------------------------------------------------------------------


class TestBracesAndSeparators:
    def test_lbrace_rbrace(self):
        toks = tokenize("{git}")
        assert toks[0].kind is TokenKind.LBRACE
        assert toks[2].kind is TokenKind.RBRACE

    def test_comma(self):
        toks = tokenize("{A,B}")
        assert toks[2].kind is TokenKind.COMMA

    def test_name(self):
        toks = tokenize("{git}")
        assert toks[1].kind is TokenKind.NAME
        assert toks[1].text == "git"

    def test_multiple_names(self):
        text = "{git,vim,python}"
        names = [t.text for t in tokenize(text) if t.kind is TokenKind.NAME]
        assert names == ["git", "vim", "python"]

    def test_dot_token(self):
        # Standalone '.' (profile-only scope) produces DOT; dots inside
        # names (e.g. pkgs.git) are consumed by the name scanner.
        toks = tokenize("#.")
        assert any(t.kind is TokenKind.DOT for t in toks)

    def test_star_token(self):
        toks = tokenize("{*}")
        assert any(t.kind is TokenKind.STAR for t in toks)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


class TestSpans:
    def test_token_spans_are_increasing(self):
        toks = tokenize("-G#{A,B}#{1}{2}")
        prev_end = 0
        for t in toks:
            assert t.offset >= 0
            assert t.end >= t.offset, f"span went backwards for {t.text!r}"
            assert t.offset >= prev_end, f"overlapping/backwards span for {t.text!r}"
            prev_end = t.end

    def test_span_covers_consumed_text(self):
        text = "-G#{A,B}#{1}"
        toks = tokenize(text)
        assert toks[-1].kind is TokenKind.EOF
        covered = max(t.end for t in toks)
        assert covered <= len(text) + 1  # EOF may point just past the end

    def test_eof_token(self):
        toks = tokenize("{git}")
        assert toks[-1].kind is TokenKind.EOF


# ---------------------------------------------------------------------------
# Whitespace and errors
# ---------------------------------------------------------------------------


class TestWhitespaceAndErrors:
    def test_whitespace_between_tokens_is_skipped(self):
        toks = tokenize("-G#{A, B}  #  {1}")
        names = [t.text for t in toks if t.kind is TokenKind.NAME]
        assert "A" in names and "B" in names

    def test_unconsumable_character_raises(self):
        with pytest.raises(NixorcistError):
            lex("???")

    def test_lex_free_function(self):
        assert isinstance(lex(""), list)

    def test_lexer_tokenize_returns_tokens(self):
        toks = tokenize("")
        assert all(isinstance(t, Token) for t in toks)
