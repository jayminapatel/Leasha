r"""L8b §1d: "AGGREGATE answers are computed, not generated."

Layer: L4 / L8b

`count_matching` is the query behind that promise - the model may phrase a
sentence around the number, but the number itself has to come from here,
never from the model. These tests are all real `SqliteStore`, no fakes: this
is SQL correctness, and the property that matters most - a file with three
matching passages counts once - can only be proved against a real database.
"""

from __future__ import annotations

from app.search.keyword import count_matching
from app.search.query import parse_query
from app.storage.sqlite_store import SqliteStore


def _add(store, *, path, mtime=0, ext="txt", sender="", chunks=("body text",)):
    file_id = store.upsert_file(
        path=path, size_bytes=1000, mtime_ns=mtime, ext=ext,
        parent_dir=str(path).rsplit("\\", 1)[0] if "\\" in str(path) else "",
    )
    store.replace_chunks(
        file_id, [{"ordinal": n, "text": text} for n, text in enumerate(chunks)])
    store.mark_indexed(file_id)
    if sender:
        store.conn.execute(
            "INSERT INTO messages (file_id, subject, sender, recipients, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_id, "", sender, "[]", mtime // 1_000_000_000))
    return file_id


def test_a_file_with_several_matching_passages_counts_once(tmp_path):
    """The property that matters most. `search()`'s own rows are one per
    *chunk* - right for showing three snippets, wrong for counting one
    document three times."""
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\work\invoice.pdf", chunks=(
            "invoice for the March order", "invoice total due",
            "invoice payment terms",
        ))
        assert count_matching(store, parse_query("invoice")) == 1


def test_documents_with_none_of_the_words_are_not_counted(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\work\invoice.pdf", chunks=("an invoice",))
        _add(store, path=r"C:\work\unrelated.pdf", chunks=("holiday photos",))
        assert count_matching(store, parse_query("invoice")) == 1


def test_zero_matches_is_zero_not_an_error(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\work\a.pdf", chunks=("nothing relevant",))
        assert count_matching(store, parse_query("nonexistentword")) == 0


def test_a_filter_only_query_counts_by_filter_alone(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\work\a.pdf", ext="pdf")
        _add(store, path=r"C:\work\b.pdf", ext="pdf")
        _add(store, path=r"C:\work\c.docx", ext="docx")
        assert count_matching(store, parse_query("type:pdf")) == 2


def test_a_term_and_a_filter_combine(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\work\a.pdf", ext="pdf", chunks=("the invoice",))
        _add(store, path=r"C:\work\b.docx", ext="docx", chunks=("the invoice",))
        _add(store, path=r"C:\work\c.pdf", ext="pdf", chunks=("unrelated text",))
        assert count_matching(store, parse_query("invoice type:pdf")) == 1


def test_a_sender_filter_counts_from_the_messages_table(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\mail\1.eml", sender="dave@example.com")
        _add(store, path=r"C:\mail\2.eml", sender="chris@example.com")
        _add(store, path=r"C:\mail\3.eml", sender="dave@example.com")
        assert count_matching(store, parse_query("from:dave")) == 2


def test_no_terms_and_no_filters_counts_every_file(tmp_path):
    """The edge case: an aggregate question with nothing to narrow by is
    "how many files are there at all", not zero and not an error."""
    with SqliteStore(tmp_path / "index.db") as store:
        _add(store, path=r"C:\a.pdf")
        _add(store, path=r"C:\b.pdf")
        _add(store, path=r"C:\c.pdf")
        assert count_matching(store, parse_query("")) == 3


def test_the_count_is_not_capped_at_the_search_page_size(tmp_path):
    r"""**The one property this whole function exists for.** `search()`'s
    `limit` (`KEYWORD_LIMIT = 100`) bounds a ranked page; a count silently
    truncated at that page size would be exactly the wrong number reported
    with total confidence."""
    with SqliteStore(tmp_path / "index.db") as store:
        for n in range(150):
            _add(store, path=rf"C:\work\invoice{n}.pdf", chunks=("an invoice",))
        assert count_matching(store, parse_query("invoice")) == 150


def test_a_wide_or_query_counts_every_file_with_any_term():
    r"""**Not `search()`'s narrow-first optimisation.** A multi-word query
    whose terms do not all co-occur must count every file matching *any* of
    them - the same OR form `search()` itself falls back to when the AND
    form comes up short, not the narrower AND form it tries first for
    ranking speed."""
    import tempfile
    from pathlib import Path

    with SqliteStore(Path(tempfile.mkdtemp()) / "index.db") as store:
        _add(store, path=r"C:\work\pump.txt", chunks=("pump station notes",))
        _add(store, path=r"C:\work\valve.txt", chunks=("valve replacement",))
        _add(store, path=r"C:\work\both.txt", chunks=("pump and valve schedule",))
        _add(store, path=r"C:\work\neither.txt", chunks=("unrelated budget prose",))

        assert count_matching(store, parse_query("pump valve")) == 3
