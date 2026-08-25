"""`ext` and `mtime_ns` must survive both retrievers into a result.

Layer: L4

They were being selected and discarded. `keyword.py` and `vector.py` have both
carried `f.ext` and `f.mtime_ns` since Layer 4; `SearchResult` had nowhere to put
them, so `_to_result` dropped them silently.

That mattered because a fifteen-year archive holds eight versions of everything
and the date is frequently the only thing telling two results apart. It cost one
line and no new query.

**Both paths are asserted separately**, because they hydrate differently - the
keyword path reads its columns in the same SELECT as the match, the vector path
hydrates from chunk ids afterwards. A change to either could drop a field
without the other noticing.
"""

from __future__ import annotations

import pytest

from app.search.engine import SearchEngine, SearchResult


@pytest.fixture
def engine():
    return SearchEngine.__new__(SearchEngine)


def hit(**overrides):
    """A row as either retriever hands it over."""
    base = {
        "chunk_id": 7, "file_id": 3, "path": r"D:\a\report.pdf",
        "text": "the pump station", "page": 4,
        "ext": "pdf", "mtime_ns": 1_700_000_000_000_000_000,
    }
    base.update(overrides)
    return base


def test_the_keyword_path_carries_both_fields(engine):
    """Its columns come from the same SELECT as the BM25 match."""
    result = engine._to_result(hit(), rank=1, sources=(0,), score=1.0)
    assert result.ext == "pdf"
    assert result.mtime_ns == 1_700_000_000_000_000_000


def test_the_vector_path_carries_both_fields(engine):
    """It hydrates from chunk ids in a second query, so it is a separate risk."""
    result = engine._to_result(
        hit(distance=0.21), rank=1, sources=(1,), score=0.8)
    assert result.ext == "pdf"
    assert result.mtime_ns == 1_700_000_000_000_000_000


def test_a_dotted_extension_is_normalised(engine):
    """`upsert_file` stores bare extensions - a dotted one here would make
    `type:pdf` and the row's own label disagree about the same file. This was a
    real bug once, in the store."""
    assert engine._to_result(hit(ext=".PDF"), rank=1, sources=(0,), score=1.0).ext == "pdf"


def test_a_missing_extension_is_empty_not_none(engine):
    """The view concatenates it into a label. None would render as "None"."""
    result = engine._to_result(hit(ext=None), rank=1, sources=(0,), score=1.0)
    assert result.ext == ""


def test_an_unknown_mtime_is_zero_not_none(engine):
    """Zero is the value `format_when` recognises as "do not claim a date".
    None would raise inside int arithmetic in the presenter."""
    result = engine._to_result(hit(mtime_ns=None), rank=1, sources=(0,), score=1.0)
    assert result.mtime_ns == 0


def test_a_row_without_the_columns_at_all_still_builds(engine):
    """A cached response from before this change, or a retriever that stops
    selecting them, must not take the whole search down."""
    bare = {"chunk_id": 1, "file_id": 1, "path": "x", "text": "y"}
    result = engine._to_result(bare, rank=1, sources=(0,), score=1.0)
    assert (result.ext, result.mtime_ns) == ("", 0)


def test_the_defaults_keep_existing_callers_working():
    """Both fields are optional, so every existing construction of a
    SearchResult - in tests, in the CLI, in the cache - stays valid."""
    result = SearchResult(chunk_id=1, file_id=1, path="p", text="t", score=1.0, rank=1)
    assert (result.ext, result.mtime_ns) == ("", 0)
