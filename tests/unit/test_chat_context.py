r"""L8b §1c: context economy for small models.

Layer: L8b

Pure arithmetic and a thin call into `app.search.window`, already tested on
its own terms in that module's own suite - these tests are about the
budget, not about re-proving `rerank_window`'s cutting behaviour.
"""

from __future__ import annotations

import pytest

from app.chat.context import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_LENGTH,
    OUTPUT_RESERVE_TOKENS,
    PROMPT_OVERHEAD_TOKENS,
    build_context,
    context_budget_chars,
    per_document_width,
)
from app.search.window import RERANK_WINDOW_CHARS


# ---------------------------------------------------------------------------
# context_budget_chars
# ---------------------------------------------------------------------------

def test_the_budget_reserves_output_and_prompt_overhead():
    length = 32_768
    expected = (length - OUTPUT_RESERVE_TOKENS - PROMPT_OVERHEAD_TOKENS) * CHARS_PER_TOKEN
    assert context_budget_chars(length) == expected


def test_none_falls_back_to_the_default_context_length():
    assert context_budget_chars(None) == context_budget_chars(DEFAULT_CONTEXT_LENGTH)


def test_zero_or_negative_also_falls_back_to_the_default():
    """`0` and a negative number are both "not a real answer", the same as
    `None` - a caller should not have to special-case which falsy shape a
    broken context-length probe returned."""
    assert context_budget_chars(0) == context_budget_chars(DEFAULT_CONTEXT_LENGTH)
    assert context_budget_chars(-1) == context_budget_chars(DEFAULT_CONTEXT_LENGTH)


def test_a_window_too_small_for_both_reserves_is_zero_not_negative():
    small = OUTPUT_RESERVE_TOKENS + PROMPT_OVERHEAD_TOKENS - 100
    assert context_budget_chars(small) == 0


def test_a_bigger_model_gives_a_bigger_budget():
    assert context_budget_chars(131_072) > context_budget_chars(8_192)


# ---------------------------------------------------------------------------
# per_document_width
# ---------------------------------------------------------------------------

def test_the_budget_divides_evenly_across_documents():
    assert per_document_width(10_000, 5) == 2_000


def test_a_thin_budget_is_floored_rather_than_shrunk_further():
    r"""**The floor matters more than the division.** Below
    `RERANK_WINDOW_CHARS` a passage starts losing the context that makes a
    match meaningful - the same floor `app.search.window` names for the
    identical reason."""
    assert per_document_width(1_000, 5) == RERANK_WINDOW_CHARS


def test_a_custom_minimum_is_honoured():
    assert per_document_width(100, 5, minimum=50) == 50


def test_zero_documents_returns_the_whole_budget_unchanged():
    assert per_document_width(5_000, 0) == 5_000


def test_one_document_gets_the_whole_budget_too():
    assert per_document_width(5_000, 1) == 5_000


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------

LONG_PASSAGE = (
    "Introductory padding about nothing in particular. " * 20
    + "The pump station commissioning report is attached for review. "
    + "Trailing padding that follows the match. " * 20
)


def test_one_window_per_passage():
    windows = build_context([LONG_PASSAGE, LONG_PASSAGE, LONG_PASSAGE],
                            ["pump", "station"], context_length=8_192)
    assert len(windows) == 3


def test_each_window_is_centred_on_the_matching_terms():
    windows = build_context([LONG_PASSAGE], ["pump", "station"], context_length=8_192)
    assert "pump station commissioning" in windows[0]


def test_more_documents_means_a_narrower_window_each():
    few = build_context([LONG_PASSAGE] * 2, ["pump"], context_length=8_192)
    many = build_context([LONG_PASSAGE] * 40, ["pump"], context_length=8_192)
    assert len(few[0]) >= len(many[0])


def test_no_passages_is_not_an_error():
    assert build_context([], ["pump"], context_length=8_192) == []


def test_a_missing_context_length_still_produces_usable_windows():
    windows = build_context([LONG_PASSAGE], ["pump"], context_length=None)
    assert windows and "pump" in windows[0].lower()
