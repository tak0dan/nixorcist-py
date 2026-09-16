"""Recursive-descent parser building a :class:`Command` AST from DSL tokens.

The parser does not touch the filesystem, TOML stores or Nix.  Everything it
rejects is reported as a diagnostic (mostly ``TN001`` / ``TN003``).

Operations (spec §10 table + corrected state model):

    -I  install/activate    -P  promote (backend := declarative)
    -R  remove              -D  demote   (backend := imperative)
    -G  select/create group -E  deactivate (active := false)
    -A  activate            -S  sequence mode
    -i  imperative target   -d  declarative target
"""

from __future__ import annotations

from ..cli.ast import (
    Broadcast,
    Command,
    GroupRef,
    Operation,
    PackageRef,
    PackageSet,
    Positional,
    Target,
)
from .diagnostics import ErrorCode, NixorcistError, Span, syntax_error
from .lexer import Token, TokenKind, lex

_OP_KINDS = (
    TokenKind.INSTALL,
    TokenKind.REMOVE,
    TokenKind.PROMOTE,
    TokenKind.DEMOTE,
    TokenKind.DEACTIVATE,
    TokenKind.ACTIVATE,
    TokenKind.OBLITERATE,
    TokenKind.YIELD,
)
_CONFLICTING = {
    TokenKind.INSTALL: ("--install", "-R/-P/-D/-E/-A/-O/-Y"),
    TokenKind.REMOVE: ("--remove", "-I/-P/-D/-E/-A/-O/-Y"),
    TokenKind.PROMOTE: ("--promote", "-I/-R/-D/-E/-A/-O/-Y"),
    TokenKind.DEMOTE: ("--demote", "-I/-R/-P/-E/-A/-O/-Y"),
    TokenKind.DEACTIVATE: ("--deactivate", "-I/-R/-P/-D/-A/-O/-Y"),
    TokenKind.ACTIVATE: ("--activate", "-I/-R/-P/-D/-E/-O/-Y"),
    TokenKind.OBLITERATE: ("--obliterate", "-I/-R/-P/-D/-E/-A/-Y"),
    TokenKind.YIELD: ("--yield", "-I/-R/-P/-D/-E/-A/-O"),
}


class Parser:
    def __init__(self, text: str, tokens: list[Token]):
        self.text = text
        self.tokens = tokens
        self.i = 0
        self._last_rbrace_end = 0

    # -- token stream helpers -------------------------------------------
    def peek(self, ahead: int = 0) -> Token:
        i = min(self.i + ahead, len(self.tokens) - 1)
        return self.tokens[i]

    def advance(self) -> Token:
        tok = self.peek()
        if tok.kind is not TokenKind.EOF:
            self.i += 1
        return tok

    def _syntax_error(self, span: Span, message: str, suggestion: str = "") -> NixorcistError:
        raise NixorcistError(syntax_error(self.text, span, message, suggestion))

    # ------------------------------------------------------------------
    def parse(self) -> Command:
        flags: dict[TokenKind, bool] = {k: False for k in _OP_KINDS}
        groups_flag = False
        sequence = False
        target = Target.NONE
        obliterate_count = 0
        yield_count = 0
        save = False

        while (
            self.peek().kind in _OP_KINDS
            or self.peek().kind
            in (
                TokenKind.GROUP,
                TokenKind.SEQUENCE,
                TokenKind.IMPERATIVE,
                TokenKind.DECLARATIVE,
            )
        ):
            kind = self.peek().kind
            if kind in (TokenKind.OBLITERATE, TokenKind.YIELD):
                long, others = _CONFLICTING[kind]
                for other in _OP_KINDS:
                    if flags[other]:
                        raise self._syntax_error(
                            self.peek().span,
                            f"conflicting operations: cannot combine {long} with {others}",
                        )
                flags[kind] = True
                self.advance()
                if kind is TokenKind.OBLITERATE:
                    obliterate_count, save = self._parse_obliterate_modifiers(save)
                else:
                    yield_count = self._parse_yield_modifiers(yield_count)
                continue
            if kind in _OP_KINDS:
                long, others = _CONFLICTING[kind]
                for other in _OP_KINDS:
                    if flags[other]:
                        raise self._syntax_error(
                            self.peek().span,
                            f"conflicting operations: cannot combine {long} with {others}",
                        )
                flags[kind] = True
            elif kind is TokenKind.GROUP:
                groups_flag = True
            elif kind is TokenKind.SEQUENCE:
                sequence = True
            elif kind in (TokenKind.IMPERATIVE, TokenKind.DECLARATIVE):
                wanted = (
                    Target.IMPERATIVE if kind is TokenKind.IMPERATIVE else Target.DECLARATIVE
                )
                if target is not Target.NONE and target is not wanted:
                    raise self._syntax_error(
                        self.peek().span, "conflicting activation targets: -i and -d together"
                    )
                target = wanted
            self.advance()

        groups, all_groups, profile_only, has_scope = self._parse_scope(groups_flag)
        assignment = self._parse_assignment()

        if self.peek().kind is not TokenKind.EOF:
            tok = self.peek()
            raise self._syntax_error(
                tok.span,
                f"unexpected token {tok.text!r}",
                "the expression ended early; check braces and separators",
            )

        operation = self._decide_operation(flags, groups_flag, groups, assignment)

        if operation in (
            Operation.PROMOTE,
            Operation.DEMOTE,
            Operation.DEACTIVATE,
            Operation.ACTIVATE,
        ):
            if not groups and not all_groups:
                raise self._syntax_error(
                    self.text_span(len(self.text)),
                    f"{operation.value} requires a group scope",
                    "write e.g. -P#Programming, -E#{Games,Media} or -A#Programming",
                )

        if operation in (Operation.OBLITERATE, Operation.YIELD) and not groups and not all_groups and not profile_only:
            raise self._syntax_error(
                self.text_span(len(self.text)),
                f"{operation.value} requires a group scope",
                "write e.g. -O#Programming, -Oo#Programming or -Y#Programming",
            )

        return Command(
            operation=operation,
            groups=tuple(groups),
            all_groups=all_groups,
            profile_only=profile_only,
            has_scope=has_scope,
            groups_flag=groups_flag,
            assignment=assignment,
            expression=self.text,
            target=target,
            sequence=sequence,
            obliterate_count=obliterate_count,
            yield_count=yield_count,
            save=save,
        )

    def _decide_operation(
        self,
        flags: dict[TokenKind, bool],
        groups_flag: bool,
        groups: list[GroupRef],
        assignment: Broadcast | Positional | None,
    ) -> Operation:
        if flags[TokenKind.INSTALL]:
            return Operation.INSTALL
        if flags[TokenKind.REMOVE]:
            return Operation.REMOVE
        if flags[TokenKind.PROMOTE]:
            return Operation.PROMOTE
        if flags[TokenKind.DEMOTE]:
            return Operation.DEMOTE
        if flags[TokenKind.DEACTIVATE]:
            return Operation.DEACTIVATE
        if flags[TokenKind.ACTIVATE]:
            return Operation.ACTIVATE
        if flags[TokenKind.OBLITERATE]:
            return Operation.OBLITERATE
        if flags[TokenKind.YIELD]:
            return Operation.YIELD
        if groups_flag:
            return Operation.ADD
        has_content = bool(groups) or assignment is not None
        if has_content:
            raise self._syntax_error(
                self.text_span(0),
                "no operation specified",
                "prefix the expression with -I (--install), -R (--remove), "
                "-P (--promote), -D (--demote), -E (--deactivate) or -A (--activate)",
            )
        raise self._syntax_error(self.text_span(0), "empty command", "e.g. nist -I python gcc")

    # -- obliteration / yielding modifiers (spec §§33-41, 44-49) ----------
    def _modifier_text(self) -> str | None:
        """Return the text of an adjacent lowercase modifier NAME token, or None."""
        tok = self.peek()
        if tok.kind is TokenKind.NAME and tok.text.islower():
            return tok.text
        return None

    def _parse_obliterate_modifiers(self, save: bool) -> tuple[int, bool]:
        """Consume ``-Oo...o`` / ``-Os`` / ``-O-s`` modifier runs.

        Returns ``(o_count, save)``.  ``o`` count 0/1/2+ determines the
        declarative cleanup scope (§37-38); ``s`` enables save mode (§41).
        """
        count = 0
        while True:
            if self.peek().kind is TokenKind.SAVE:
                save = True
                self.advance()
                continue
            text = self._modifier_text()
            if text is None:
                break
            if not set(text) <= set("os"):
                raise self._syntax_error(
                    self.peek().span,
                    f"unknown -O modifier {text!r}",
                    "valid -O modifiers are lowercase 'o' (declaration scope) "
                    "and 's' (save removed declaration)",
                )
            count += text.count("o")
            if "s" in text:
                save = True
            self.advance()
        return count, save

    def _parse_yield_modifiers(self, current: int) -> int:
        """Consume ``-Yy...y`` runs; returns the cumulative ``y`` count."""
        count = current
        while True:
            text = self._modifier_text()
            if text is None:
                break
            if not set(text) <= set("y"):
                raise self._syntax_error(
                    self.peek().span,
                    f"unknown -Y modifier {text!r}",
                    "valid -Y modifiers are lowercase 'y' (adoption scope)",
                )
            count += text.count("y")
            self.advance()
        return count

    # -- scope parsing --------------------------------------------------
    def _parse_scope(
        self, groups_flag: bool
    ) -> tuple[list[GroupRef], bool, bool, bool]:
        groups: list[GroupRef] = []
        all_groups = False
        profile_only = False
        has_scope = False
        tok = self.peek()

        if tok.kind is TokenKind.BROADCAST:
            has_scope = True
            self.advance()
            nxt = self.peek()
            if nxt.kind is TokenKind.DOT:
                profile_only = True
                self.advance()
            elif nxt.kind is TokenKind.STAR:
                all_groups = True
                self.advance()
            elif nxt.kind is TokenKind.LBRACE:
                groups = self._parse_name_collection(GroupRef)
                if not groups:
                    raise self._syntax_error(
                        tok.span, "empty group collection", "name at least one group, e.g. #{Programming,Games}"
                    )
            elif nxt.kind is TokenKind.NAME:
                groups = [self._parse_group_ref(self.advance())]
            elif nxt.kind in (TokenKind.EOF, TokenKind.POSITIONAL, TokenKind.BROADCAST):
                raise self._syntax_error(
                    tok.span,
                    "expected a group target after '#'",
                    "use #[group], #{group,...}, #* or #.",
                )
            else:
                raise self._syntax_error(nxt.span, f"unexpected token {nxt.text!r} after '#'")
        elif tok.kind is TokenKind.STAR:
            has_scope = True
            all_groups = True
            self.advance()
        elif tok.kind is TokenKind.DOT:
            has_scope = True
            profile_only = True
            self.advance()
        elif tok.kind is TokenKind.LBRACE and groups_flag:
            has_scope = True
            groups = self._parse_name_collection(GroupRef)
            if not groups:
                raise self._syntax_error(tok.span, "empty group collection")
        return groups, all_groups, profile_only, has_scope

    def _parse_assignment(self) -> Broadcast | Positional | None:
        tok = self.peek()

        # ── Positional (## shorthand): ordered-only ────────────────────
        if tok.kind is TokenKind.POSITIONAL:
            self.advance()
            slots: list[tuple[PackageRef, ...]] = []
            spans: list[Span] = []
            while self.peek().kind is TokenKind.LBRACE:
                start = self.peek().offset
                items = self._parse_name_collection(PackageRef)
                spans.append(Span(start, self._last_rbrace_end))
                slots.append(tuple(items))
            if not slots:
                raise self._syntax_error(tok.span, "positional assignment requires at least one package collection")
            return Positional(tuple(slots), tuple(spans))

        # ── Broadcast (#): sections → optional ordered transition ──────
        if tok.kind is TokenKind.BROADCAST:
            self.advance()  # consume first '#'
            sections, ordered = self._parse_broadcast_sections()
            if not sections:
                raise self._syntax_error(
                    tok.span,
                    "expected a package collection after '#'",
                    "e.g. -I#{python,gcc}",
                )
            return Broadcast(tuple(sections), tuple(ordered))

        # ── Bare braced section (no leading #) ────────────────────────
        if tok.kind is TokenKind.LBRACE:
            start = tok.offset
            items = self._parse_name_collection(PackageRef)
            sec = PackageSet(tuple(items), start, self._last_rbrace_end)
            return Broadcast((sec,), ())

        # ── Bare names (no braces, no #) ──────────────────────────────
        if tok.kind is TokenKind.NAME:
            start = tok.offset
            names = self._collect_names()
            end = names[-1].end if names else start
            sec = PackageSet(tuple(names), start, end)
            return Broadcast((sec,), ())

        return None

    def _parse_broadcast_sections(
        self,
    ) -> tuple[list[PackageSet], list[PackageSet]]:
        """Parse broadcast sections followed by optional ordered sections.

        Broadcast: ``{...}`` sections (and bare-name runs) continue until
        a second ``#`` (broadcast→ordered transition) or end-of-stream.

        Ordered: ``{...}`` sections after the transition, consumed
        positionally (§17).
        """
        broadcast: list[PackageSet] = []

        # Collect broadcast sections (spec §13: broadcast continues across {})
        while True:
            if self.peek().kind is TokenKind.LBRACE:
                start = self.peek().offset
                items = self._parse_name_collection(PackageRef)
                broadcast.append(PackageSet(tuple(items), start, self._last_rbrace_end))
            elif self.peek().kind is TokenKind.NAME:
                start = self.peek().offset
                names = self._collect_names()
                end = names[-1].end if names else start
                broadcast.append(PackageSet(tuple(names), start, end))
            else:
                break

        # Transition to ordered mode on second '#' or '##' (spec §14, §16)
        ordered: list[PackageSet] = []
        if self.peek().kind in (TokenKind.BROADCAST, TokenKind.POSITIONAL):
            self.advance()  # consume the transition token
            while self.peek().kind is TokenKind.LBRACE:
                start = self.peek().offset
                items = self._parse_name_collection(PackageRef)
                ordered.append(PackageSet(tuple(items), start, self._last_rbrace_end))

        return broadcast, ordered

    # -- primitives -----------------------------------------------------
    def _collect_names(self) -> list[PackageRef]:
        refs: list[PackageRef] = []
        while self.peek().kind is TokenKind.NAME:
            refs.append(self._parse_package_ref(self.advance()))
        return refs

    def _parse_name_collection(self, cls):
        lbrace = self.advance()
        if lbrace.kind is not TokenKind.LBRACE:
            raise self._syntax_error(lbrace.span, "expected '{'")
        items = []
        while self.peek().kind is not TokenKind.RBRACE:
            tok = self.peek()
            if tok.kind is TokenKind.EOF:
                raise self._syntax_error(lbrace.span, "unterminated collection: missing '}'")
            if tok.kind is TokenKind.NAME:
                items.append(self._ref_from_token(tok, cls))
                self.advance()
                if self.peek().kind is TokenKind.COMMA:
                    self.advance()
            elif tok.kind is TokenKind.COMMA:
                raise self._syntax_error(tok.span, "unexpected ','", "write {a,b}, not {,a} or {a,,b}")
            else:
                raise self._syntax_error(tok.span, f"expected a name or '}}', found {tok.text!r}")
        self._last_rbrace_end = self.peek().end
        self.advance()
        return items

    def _ref_from_token(self, tok: Token, cls):
        if cls is PackageRef:
            return PackageRef(tok.text, tok.offset, tok.end)
        return GroupRef(tok.text, tok.offset, tok.end)

    def _parse_group_ref(self, tok: Token) -> GroupRef:
        return GroupRef(tok.text, tok.offset, tok.end)

    def _parse_package_ref(self, tok: Token) -> PackageRef:
        return PackageRef(tok.text, tok.offset, tok.end)

    # -- helpers --------------------------------------------------------
    def text_span(self, length: int) -> Span:
        return Span(0, min(length, len(self.text)))


def parse(text: str) -> Command:
    tokens = lex(text)
    return Parser(text, tokens).parse()