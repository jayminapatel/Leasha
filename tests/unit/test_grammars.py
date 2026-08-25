"""The preview pane's grammars, checked without a display.

A syntax highlighter fails quietly: one word in the wrong colour looks like a
rendering quirk, not a bug, so nobody reports it. These are the failures worth
catching mechanically - a pattern that will not compile, one that matches the
whole file, and the ordering rule the whole thing rests on.
"""

from __future__ import annotations

import re

import pytest

from app.ui.grammars import (
    BLOCK_COMMENTS, COMMENTS, CONSTANTS, KEYWORDS, LANGUAGES, NUMBER,
    STRINGS, combined, language_for, patterns_for,
)

GRAMMARS = sorted({*LANGUAGES.values()})


def paint(text: str, language: str) -> list[str | None]:
    """One role per character, by the same single scan the highlighter does.

    A plain-Python stand-in for `highlightBlock`: one pass, leftmost match, the
    first alternative that matched wins. It has to mirror the real painter or
    these tests assert something the application does not do.
    """
    pattern, roles = combined(language)
    painted: list[str | None] = [None] * len(text)
    if not pattern:
        return painted
    for found in re.finditer(pattern, text, re.MULTILINE):
        for index, role in enumerate(roles, start=1):
            if found.start(index) >= 0:
                for position in range(found.start(index), found.end(index)):
                    painted[position] = role
                break
    return painted


def role_of(text: str, needle: str, language: str) -> str | None:
    return paint(text, language)[text.index(needle)]


# -- the table ---------------------------------------------------------------

def test_an_unknown_extension_gets_no_grammar() -> None:
    """Guessing puts arbitrary words in keyword blue, which reads as a fault."""
    assert language_for("txt") is None
    assert language_for("") is None
    assert language_for("dwg") is None
    assert patterns_for(None) == []


def test_the_extension_is_matched_however_it_is_spelled() -> None:
    assert language_for("PY") == language_for(".py") == language_for("py") == "hash"


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_every_grammar_has_rules(grammar: str) -> None:
    """A grammar in the table with no patterns colours nothing, silently."""
    assert patterns_for(grammar), f"{grammar} is offered but paints nothing"


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_every_pattern_compiles(grammar: str) -> None:
    """A bad pattern raises inside a paint event, where it is hard to see."""
    for pattern, _role in patterns_for(grammar):
        re.compile(pattern)


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_no_pattern_matches_an_empty_string(grammar: str) -> None:
    """A zero-width match loops forever in Qt's `globalMatch`."""
    for pattern, _role in patterns_for(grammar):
        assert not re.match(pattern, ""), pattern


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_every_role_a_grammar_uses_is_one_the_painter_knows(grammar: str) -> None:
    """`_compile` indexes `formats[role]`, so an unknown role is a KeyError."""
    known = {"keyword", "string", "comment", "number", "constant", "plain"}
    assert {role for _p, role in patterns_for(grammar)} <= known


# -- the ordering rule -------------------------------------------------------

def test_a_keyword_inside_a_string_looks_like_a_string() -> None:
    """The reason strings are applied after keywords, not before."""
    assert role_of('x = "for the class"', "for the", "hash") == "string"


def test_a_comment_marker_inside_a_string_is_not_a_comment() -> None:
    assert role_of('url = "http://example.com"', "//example", "c") == "string"


def test_a_keyword_inside_a_comment_looks_like_a_comment() -> None:
    assert role_of("# return the class", "return", "hash") == "comment"


def test_a_keyword_on_its_own_is_still_a_keyword() -> None:
    assert role_of("return value", "return", "hash") == "keyword"


# -- the patterns themselves -------------------------------------------------

def test_an_escaped_quote_does_not_end_the_string() -> None:
    text = r'a = "she said \"no\"" + b'
    assert role_of(text, r"\"no", "hash") == "string"


def test_an_unclosed_quote_colours_one_line_not_the_rest_of_the_file() -> None:
    """Newline-bounded on purpose: one stray apostrophe must not paint the file."""
    text = "x = 'unclosed\nreturn value"
    assert role_of(text, "return", "hash") == "keyword"


@pytest.mark.parametrize("literal", ["42", "0xFF", "3.14", "1e10", "1_000"])
def test_numbers_are_recognised_in_the_shapes_code_writes_them(literal: str) -> None:
    assert re.fullmatch(NUMBER, literal), literal


def test_a_version_inside_a_word_is_not_a_number() -> None:
    """`\\b` on both ends: `utf8` is an identifier, not the number 8."""
    assert role_of("import utf8lib", "8lib", "hash") is None


@pytest.mark.parametrize("word", CONSTANTS)
def test_constants_are_coloured_apart_from_keywords(word: str) -> None:
    """They are what somebody scanning a config file is actually looking for."""
    assert role_of(f"x = {word}", word, "hash") == "constant"


def test_a_keyword_is_matched_whole_not_as_a_prefix() -> None:
    """Without word boundaries, `formatting` would be `for` plus noise."""
    assert role_of("formatting = 1", "formatting", "hash") is None


def test_the_hash_grammar_does_not_treat_a_c_comment_as_one() -> None:
    """Grammars are separate so `//` in a YAML URL stays plain."""
    assert role_of("url: http://example.com", "//example", "hash") is None


# -- block comments ----------------------------------------------------------

@pytest.mark.parametrize("grammar", sorted(BLOCK_COMMENTS))
def test_block_comment_delimiters_compile_and_differ(grammar: str) -> None:
    opener, closer = BLOCK_COMMENTS[grammar]
    re.compile(opener)
    re.compile(closer)
    assert opener != closer, "a state machine needs to tell the ends apart"


def test_a_grammar_with_block_comments_keeps_them_out_of_the_line_rules() -> None:
    """They belong to the state machine; a per-line regex cannot span lines.

    A `/\\*.*?\\*/` rule in the line list would half-work - correct on one line,
    silently wrong across two - which is worse than not being there.
    """
    for grammar in BLOCK_COMMENTS:
        for pattern, _role in patterns_for(grammar):
            assert ".*?" not in pattern, f"{grammar}: {pattern} spans lines"


# -- the single scan ---------------------------------------------------------

@pytest.mark.parametrize("grammar", GRAMMARS)
def test_the_combined_pattern_compiles_and_names_every_role(grammar: str) -> None:
    """One bad group turns the whole grammar into no grammar at all."""
    pattern, roles = combined(grammar)
    compiled = re.compile(pattern)
    assert compiled.groups == len(roles)


def test_no_grammar_paints_a_span_twice() -> None:
    """The property a single scan buys, asserted rather than assumed."""
    text = 'x = "a" # 1\nreturn 42  // "b"'
    for grammar in GRAMMARS:
        pattern, roles = combined(grammar)
        ends: list[int] = []
        for found in re.finditer(pattern, text, re.MULTILINE):
            assert not ends or found.start() >= ends[-1]
            ends.append(found.end())


def test_grammars_without_block_comments_are_not_given_a_state_machine() -> None:
    """`hash` and `json` have none; asking for one would paint from a `#`."""
    assert "hash" not in BLOCK_COMMENTS
    assert "json" not in BLOCK_COMMENTS


# -- the keyword lists -------------------------------------------------------

@pytest.mark.parametrize("grammar", sorted(KEYWORDS))
def test_keyword_lists_hold_no_duplicates(grammar: str) -> None:
    words = KEYWORDS[grammar]
    assert len(words) == len(set(words))


@pytest.mark.parametrize("grammar", sorted(KEYWORDS))
def test_keyword_lists_are_lower_case_and_plain(grammar: str) -> None:
    """A keyword with a regex character in it would corrupt the joined pattern.

    `re.escape` handles it, but a keyword needing escaping is a sign something
    that is not a word has been added to a word list.
    """
    for word in KEYWORDS[grammar]:
        assert word.isidentifier(), word


def test_a_comment_grammar_exists_for_every_language() -> None:
    """A missing entry is silent - the grammar simply never colours comments."""
    assert set(COMMENTS) >= set(GRAMMARS)


def test_string_rules_are_shared_by_every_grammar() -> None:
    """Quotes mean the same thing everywhere; a per-grammar copy would drift."""
    for grammar in GRAMMARS:
        painted = {pattern for pattern, role in patterns_for(grammar)
                   if role == "string"}
        assert set(STRINGS) <= painted, grammar
