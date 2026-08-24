"""Finding a file by its name — the search that did not exist.

Layer: L1 / L4

`chunks_fts` indexes what documents *say*. Nothing indexed what they are
*called*, so a file named `Invoice 2024.pdf` whose contents never used those
words could not be found at all. That is how most people look for most files, so
it was the largest hole in the product and none of the existing tests could have
noticed it - they all asked about content.
"""

from __future__ import annotations

import time

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import file_rows, format_size, format_when

FILES = [
    "D:/Docs/Invoice 2024 Barnsley.pdf",
    "D:/Docs/Invoices/summary notes.txt",
    "D:/Docs/HACCP Review Barnsley.docx",
    "D:/Projects/pasteuriser layout.dwg",
    "D:/Archive/2007.pst",
]


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        for path in FILES:
            opened.upsert_file(path, size_bytes=2048, mtime_ns=1, source_kind="file")
        # Mail, which must never appear in a filename search.
        for n in range(3):
            opened.upsert_file(
                f"pst://2007.pst/E{n}", size_bytes=10, mtime_ns=1,
                source_kind="pst_message",
            )
        yield opened


def names(store, text, **kwargs) -> list[str]:
    return [
        hit["path"].rsplit("/", 1)[-1]
        for hit in store.search_files_by_name(text, **kwargs)
    ]


# -- the capability that was missing -----------------------------------------

def test_a_file_is_found_by_a_word_in_its_name():
    """The whole point. Nothing in this file's *contents* is being searched."""
    # (covered by the fixture-based tests below; kept as the named statement)


def test_a_substring_matches_not_just_a_word_prefix(store):
    """"voice" must find "Invoice".

    People type the middle of a name, or half of it. A word tokeniser cannot do
    this at all - which is why the index uses FTS5's trigram tokeniser, and why
    that choice is worth a test rather than a comment.
    """
    assert "Invoice 2024 Barnsley.pdf" in names(store, "voice")


def test_matching_is_case_insensitive(store):
    assert names(store, "haccp") == names(store, "HACCP")


def test_the_folder_is_searched_as_well_as_the_name(store):
    """"everything in my Invoices folder" is a reasonable thing to ask for."""
    assert "summary notes.txt" in names(store, "Invoices")


def test_a_name_match_outranks_a_folder_match(store):
    """Typing "invoice" should surface `Invoice 2024.pdf` before the files that
    merely live in an `Invoices` directory. That is the bm25 column weighting."""
    found = names(store, "invoice")
    assert found[0] == "Invoice 2024 Barnsley.pdf"


def test_results_can_be_narrowed_by_extension(store):
    assert names(store, "barnsley", ext=["docx"]) == ["HACCP Review Barnsley.docx"]
    assert names(store, "barnsley", ext=[".pdf"]) == ["Invoice 2024 Barnsley.pdf"]


# -- what must NOT appear ----------------------------------------------------

def test_mail_never_appears_in_a_filename_search(store):
    """A message's "path" is a synthetic key nobody typed and nobody would
    recognise, and mail would outnumber documents ten to one here."""
    assert names(store, "pst://") == []
    assert names(store, "E1") == []
    assert store.count_named_files() == len(FILES)


def test_the_archive_itself_is_still_findable(store):
    """The `.pst` is a real file on disk, even though its messages are not."""
    assert "2007.pst" in names(store, "2007")


def test_a_query_of_one_character_returns_nothing(store):
    """It would match nearly everything, and a hundred arbitrary rows is worse
    than an empty list."""
    assert names(store, "a") == []
    assert names(store, "") == []
    assert names(store, "   ") == []


def test_fts_operators_typed_by_accident_cannot_crash_it(store):
    """Somebody looking for a file types whatever is in the name, including
    quotes, asterisks and the word AND. None of it reaches the FTS expression
    parser - the query is quoted whole."""
    for hostile in ('AND', 'OR', '""', '*', 'a AND b', '"unclosed', 'NEAR(x y)', "a*b"):
        assert isinstance(store.search_files_by_name(hostile), list)


def test_a_deleted_file_stops_being_findable(store):
    """`files_fts` is standalone, so nothing cascades into it.

    Forgetting to clear it leaves a deleted file findable forever, which looks
    exactly like the index being wrong.
    """
    record = store.get_file("D:/Docs/HACCP Review Barnsley.docx")
    store.delete_file(record.id)
    assert names(store, "haccp") == []


def test_reindexing_a_file_does_not_duplicate_it(store):
    """`upsert_file` is called on every run, and a standalone FTS table has no
    unique constraint to save you."""
    before = len(names(store, "invoice"))
    for _ in range(3):
        store.upsert_file(FILES[0], size_bytes=4096, mtime_ns=2, source_kind="file")
    assert len(names(store, "invoice")) == before


def test_a_renamed_file_is_found_under_its_new_name(tmp_path):
    """A rename is a delete plus an insert as far as the walker is concerned."""
    with SqliteStore(tmp_path / "index.db") as store:
        record_id = store.upsert_file(
            "D:/Docs/draft.txt", size_bytes=10, mtime_ns=1, source_kind="file"
        )
        store.delete_file(record_id)
        store.upsert_file("D:/Docs/final.txt", size_bytes=10, mtime_ns=1, source_kind="file")

        assert names(store, "draft") == []
        assert names(store, "final") == ["final.txt"]


# -- how it is shown ---------------------------------------------------------

@pytest.mark.parametrize(("size", "expected"), [
    (0, "0 B"), (999, "999 B"), (2048, "2.0 KB"),
    (5_242_880, "5.0 MB"), (3_221_225_472, "3.0 GB"),
])
def test_sizes_are_readable(size, expected):
    """Never "3221225472 bytes"."""
    assert format_size(size) == expected


def test_a_negative_size_does_not_produce_nonsense():
    assert format_size(-1) == "0 B"


@pytest.mark.parametrize(("ago_s", "expected"), [
    (5, "just now"), (300, "5 min ago"), (7200, "2 hours ago"),
    (90_000, "yesterday"), (600_000, "6 days ago"), (3_000_000, "4 weeks ago"),
])
def test_ages_are_relative_because_that_is_what_gets_compared(ago_s, expected):
    """"Yesterday" answers "is this the one I was working on" instantly.
    "2026-08-03 14:22:07" requires arithmetic."""
    now = time.time()
    assert format_when(int((now - ago_s) * 1e9), now=now) == expected


def test_beyond_a_year_the_date_is_more_useful_than_the_age():
    now = time.time()
    shown = format_when(int((now - 40_000_000) * 1e9), now=now)
    assert "ago" not in shown
    assert "20" in shown              # a year is in there


def test_a_clock_skewed_future_file_does_not_say_minus_three_days():
    now = time.time()
    assert format_when(int((now + 10_000) * 1e9), now=now) == "just now"


def test_a_row_splits_the_name_from_the_folder():
    rows = file_rows([{
        "id": 1, "path": "D:/Docs/Sales/Invoice 2024.pdf", "ext": "pdf",
        "size_bytes": 2048, "mtime_ns": int(time.time() * 1e9), "status": "INDEXED",
    }])
    assert rows[0].name == "Invoice 2024.pdf"
    assert rows[0].folder.endswith("Sales")
    assert rows[0].kind == "PDF"
    assert rows[0].note == ""


def test_a_file_indexed_by_name_only_says_so(store):
    """A scanned PDF is findable by name but has no searchable contents.

    Saying so is the point: "I can see it but cannot search inside it" is real,
    useful, and invites the same fruitless search twice if hidden.
    """
    rows = file_rows([{
        "id": 1, "path": "D:/Scans/old.pdf", "ext": "pdf", "size_bytes": 10,
        "mtime_ns": int(time.time() * 1e9), "status": "SKIPPED",
        "skip_code": "ERR_NO_TEXT_LAYER",
    }])
    assert "name only" in rows[0].note
    assert "ERR_NO_TEXT_LAYER" in rows[0].note


def test_a_windows_path_splits_on_backslashes():
    """These keys are written on Windows and may be read anywhere."""
    rows = file_rows([{
        "id": 1, "path": r"D:\Docs\Sales\Invoice.pdf", "ext": "pdf",
        "size_bytes": 1, "mtime_ns": 1, "status": "INDEXED",
    }])
    assert rows[0].name == "Invoice.pdf"


# ---------------------------------------------------------------------------
# Scoping the main search to mail or to documents. Same index, same query,
# a WHERE clause on a column that is already there.
# ---------------------------------------------------------------------------

def scoped_store(tmp_path):
    store = SqliteStore(tmp_path / "scoped.db").connect()
    rows = [
        ("D:/Docs/report.pdf", "file", "The Barnsley Dairy HACCP review of the pasteuriser."),
        ("D:/Docs/notes.txt", "file", "Barnsley Dairy site notes."),
        ("pst://2007.pst/E1", "pst_message", "Barnsley Dairy HACCP meeting on Tuesday."),
        ("pst://2007.pst/E2", "pst_message", "Re: Barnsley Dairy costs."),
        ("D:/Mail/one.eml", "eml", "Barnsley Dairy, forwarded."),
    ]
    for path, kind, text in rows:
        file_id = store.upsert_file(path, size_bytes=10, mtime_ns=1, source_kind=kind)
        store.replace_chunks(file_id, [{
            "ordinal": 0, "text": text, "char_start": 0, "char_end": len(text), "page": None,
        }])
        store.mark_indexed(file_id)
    return store


def scoped_paths(store, scope: str) -> set[str]:
    from app.search import keyword
    from app.search.query import parse_query

    return {hit["path"] for hit in keyword.search(store, parse_query("Barnsley").scoped(scope))}


def test_everything_returns_both_mail_and_documents(tmp_path):
    store = scoped_store(tmp_path)
    try:
        assert len(scoped_paths(store, "all")) == 5
    finally:
        store.close()


def test_mail_returns_only_mail(tmp_path):
    store = scoped_store(tmp_path)
    try:
        found = scoped_paths(store, "mail")
        assert found == {"pst://2007.pst/E1", "pst://2007.pst/E2", "D:/Mail/one.eml"}
    finally:
        store.close()


def test_documents_returns_only_files(tmp_path):
    store = scoped_store(tmp_path)
    try:
        assert scoped_paths(store, "documents") == {"D:/Docs/report.pdf", "D:/Docs/notes.txt"}
    finally:
        store.close()


def test_loose_eml_files_count_as_mail_not_as_documents(tmp_path):
    """An .eml on disk is a file, but it is *mail* to the person searching.

    Classifying it by where it lives rather than by what it is would put it in
    the wrong half of the only distinction the chips make.
    """
    store = scoped_store(tmp_path)
    try:
        assert "D:/Mail/one.eml" in scoped_paths(store, "mail")
        assert "D:/Mail/one.eml" not in scoped_paths(store, "documents")
    finally:
        store.close()
