r"""Eight versions of the same letter, shown as one row — and never one row too
many.

Layer: L4. **The failure mode here is worse than a bad ranking**: a wrong fold
puts somebody's document behind a disclosure triangle, and somebody who does
not open it concludes the file is gone. So most of these tests are about what
must *not* fold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from app.search.folding import COPIES, VERSIONS, Fold, family, fold

_DAY = 86_400 * 1_000_000_000
_NOW = 1_700_000_000 * 1_000_000_000


@dataclass
class _Row:
    """Just enough of a `SearchResult` to fold."""

    path: str
    mtime_ns: int = _NOW
    content_hash: str = ""


def _row(path: str, *, days_old: float = 0, digest: str = "") -> _Row:
    return _Row(path=path, mtime_ns=int(_NOW - days_old * _DAY),
                content_hash=digest)


def _paths(folded):
    return [f.head.path for f in folded]


# --------------------------------------------------------------------------
# Finding the family
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    r"C:\Work\report v2.docx",
    r"C:\Work\report V2.docx",
    r"C:\Work\report_v04.docx",
    r"C:\Work\report v1.3.docx",
    r"C:\Work\report final.docx",
    r"C:\Work\report FINAL.docx",
    r"C:\Work\report draft.docx",
    r"C:\Work\report (1).docx",
    r"C:\Work\report - Copy.docx",
    r"C:\Work\report rev2.docx",
    r"C:\Work\report 2024-01-05.docx",
    r"C:\Work\report final v2 (1).docx",
])
def test_every_marker_leads_back_to_the_same_family(name):
    """Markers stack in real filenames - "report final v2 (1)" is one file
    somebody saved four times."""
    assert family(name) == family(r"C:\Work\report.docx")


def test_a_windows_path_is_split_by_hand():
    r"""**`pathlib` does not treat a backslash as a separator on Linux**, so
    `PurePath(r"C:\Work\report.docx").name` is the whole string and every
    family key would be unique - folding would silently never happen in CI.

    That is not hypothetical. The same mistake made a measurement earlier in
    this order read 20/20 when the true figure was 14/20.
    """
    folder, base, ext = family(r"C:\Work\Leeds\safety report v2.docx")
    assert folder == "c:/work/leeds"
    assert base == "safety report"
    assert ext == "docx"


def test_a_different_folder_is_a_different_family():
    """Two files with the same name in two projects are two documents. Only
    identical bytes may fold across folders."""
    assert family(r"C:\A\report.docx") != family(r"C:\B\report.docx")


def test_a_different_extension_is_a_different_family():
    """`report.docx` and `report.pdf` are usually the same content in two
    forms, and somebody searching wants whichever they can open."""
    assert family(r"C:\A\report.docx") != family(r"C:\A\report.pdf")


# --------------------------------------------------------------------------
# What must not fold
# --------------------------------------------------------------------------

def test_a_bare_trailing_number_is_not_a_version_marker():
    """**`chapter 1` and `chapter 2` are two documents.** Reading a trailing
    number as a version would hide half a book behind the other half - the
    worst thing this feature could do."""
    rows = [_row(r"C:\Book\chapter 1.docx", digest="a"),
            _row(r"C:\Book\chapter 2.docx", digest="b")]
    folded = fold(rows)
    assert len(folded) == 2
    assert all(not f.folded for f in folded)


def test_two_unrelated_documents_are_left_alone():
    rows = [_row(r"C:\W\invoice.pdf", digest="a"),
            _row(r"C:\W\holiday rota.xlsx", digest="b")]
    assert len(fold(rows)) == 2


def test_files_with_no_hash_never_fold_into_each_other():
    """**A shared blank is not a match.** Treating an empty hash as a key
    would fold together every document the index has not read yet."""
    rows = [_row(r"C:\W\one.pdf"), _row(r"C:\W\two.pdf"), _row(r"C:\W\three.pdf")]
    folded = fold(rows)
    assert len(folded) == 3 and all(not f.folded for f in folded)


def test_a_single_result_is_still_a_fold():
    """**Every row is a `Fold`**, so the view has one shape to draw rather
    than two - and an unfolded one says nothing."""
    folded = fold([_row(r"C:\W\one.pdf", digest="a")])
    assert len(folded) == 1 and not folded[0].folded and folded[0].label() == ""


def test_switched_off_means_every_result_stands_alone():
    rows = [_row(r"C:\W\report.docx", digest="same"),
            _row(r"C:\X\report.docx", digest="same")]
    folded = fold(rows, enabled=False)
    assert len(folded) == 2 and all(not f.folded for f in folded)


# --------------------------------------------------------------------------
# What does fold
# --------------------------------------------------------------------------

def test_identical_bytes_fold_wherever_they_sit():
    """The safe half of the feature: the same hash is the same document, and
    folding it can only help."""
    rows = [_row(r"C:\Work\report.docx", days_old=5, digest="same"),
            _row(r"C:\Backup\report.docx", days_old=900, digest="same")]
    folded = fold(rows)
    assert len(folded) == 1
    assert folded[0].reason == COPIES
    assert folded[0].label() == "1 identical copy elsewhere"
    assert folded[0].head.path == r"C:\Work\report.docx"


def test_versions_fold_and_the_newest_is_the_one_shown():
    """The corpus this exists for: the same report saved three times, and the
    person wants the last one."""
    rows = [_row(r"C:\W\safety report.docx", days_old=900, digest="a"),
            _row(r"C:\W\safety report v2.docx", days_old=400, digest="b"),
            _row(r"C:\W\safety report FINAL.docx", days_old=10, digest="c")]
    folded = fold(rows)
    assert len(folded) == 1
    assert folded[0].head.path == r"C:\W\safety report FINAL.docx"
    assert folded[0].reason == VERSIONS
    assert folded[0].label() == "2 older versions"


def test_the_older_ones_are_newest_first():
    """Expanding a fold should read like a history, not a shuffle."""
    rows = [_row(r"C:\W\r.docx", days_old=900, digest="a"),
            _row(r"C:\W\r v2.docx", days_old=400, digest="b"),
            _row(r"C:\W\r final.docx", days_old=10, digest="c")]
    older = [row.path for row in fold(rows)[0].older]
    assert older == [r"C:\W\r v2.docx", r"C:\W\r.docx"]


def test_copies_fold_first_then_versions():
    r"""**Two passes, because every indexed file has a hash.**

    Choosing one key or the other per row meant identical bytes always won and
    version folding never fired once: three drafts have three different
    hashes, so they were three groups of one. The copy in `Backup` reaches the
    version group through its twin in `Work`.
    """
    rows = [_row(r"C:\Work\report.docx", days_old=900, digest="a"),
            _row(r"C:\Work\report v2.docx", days_old=400, digest="b"),
            _row(r"C:\Work\report FINAL.docx", days_old=10, digest="c"),
            _row(r"C:\Backup\report FINAL.docx", days_old=10, digest="c")]
    folded = fold(rows)
    assert len(folded) == 1
    assert folded[0].head.path == r"C:\Work\report FINAL.docx"
    assert len(folded[0].older) == 3


def test_a_fold_keeps_its_groups_best_rank():
    """**Folding never demotes an answer.** If the best result is an older
    copy of a later one, the fold sits where the best result was - otherwise
    switching this on would push the right answer down the page."""
    rows = [_row(r"C:\W\report.docx", days_old=900, digest="a"),
            _row(r"C:\W\unrelated.pdf", days_old=1, digest="z"),
            _row(r"C:\W\report v2.docx", days_old=10, digest="b")]
    assert _paths(fold(rows)) == [r"C:\W\report v2.docx", r"C:\W\unrelated.pdf"]


def test_nothing_is_ever_dropped():
    """The invariant the whole design rests on: every result appears exactly
    once, as a head or behind one."""
    rows = [_row(r"C:\W\r.docx", days_old=900, digest="a"),
            _row(r"C:\W\r v2.docx", days_old=400, digest="b"),
            _row(r"C:\X\r.docx", days_old=5, digest="a"),
            _row(r"C:\W\chapter 1.docx", digest="c"),
            _row(r"C:\W\chapter 2.docx", digest="d"),
            _row(r"C:\W\unhashed.txt")]
    folded = fold(rows)
    seen = [f.head for f in folded] + [row for f in folded for row in f.older]
    assert len(seen) == len(rows)
    assert {id(row) for row in seen} == {id(row) for row in rows}


def test_the_label_counts_what_is_behind_it():
    """"1 more" tells nobody whether it is worth opening."""
    one = Fold(head=_row("a"), older=(_row("b"),), reason=VERSIONS)
    many = Fold(head=_row("a"), older=(_row("b"), _row("c")), reason=COPIES)
    assert one.label() == "1 older version"
    assert many.label() == "2 identical copies elsewhere"


# --------------------------------------------------------------------------
# Through the engine
# --------------------------------------------------------------------------

class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _NoModel:
    def embed(self, _text):
        raise RuntimeError("no embedding model in this test")

    def embed_all(self, _texts):
        raise RuntimeError("no embedding model in this test")


@pytest.fixture()
def engine(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    now = time.time_ns()
    for path, digest, days in (
        (r"C:\Work\safety report.docx", "aaa", 900),
        (r"C:\Work\safety report v2.docx", "bbb", 400),
        (r"C:\Work\safety report FINAL.docx", "ccc", 10),
        (r"C:\Backup\safety report FINAL.docx", "ccc", 10),
        (r"C:\Work\chapter 1.docx", "ddd", 100),
        (r"C:\Work\chapter 2.docx", "eee", 100),
    ):
        file_id = store.upsert_file(
            path, parent_dir=path.rsplit("\\", 1)[0], ext="docx",
            size_bytes=1, mtime_ns=int(now - days * _DAY), content_hash=digest,
            status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{
            "ordinal": 0,
            "text": "Leeds site safety report chapter contents"}])
    from app.search.engine import SearchEngine
    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def test_the_page_collapses_but_the_results_do_not(engine):
    """**`results` is never shortened.** Six results, three rows - a view that
    ignores `folds` draws exactly the page it drew before this existed."""
    from app.search.policy import SEARCH, for_surface

    response = engine.search("safety report chapter",
                             policy=for_surface(SEARCH), use_cache=False)
    assert len(response.results) == 6
    assert len(response.folds) == 3
    assert response.folds[0].head.path == r"C:\Work\safety report FINAL.docx"
    assert response.folds[0].label() == "3 older versions"


def test_the_code_tab_folds_nothing(engine):
    """Off for Code: two files that differ by one line are two files, and a
    developer comparing them needs both on screen."""
    from app.search.policy import CODE, for_surface

    response = engine.search("safety report chapter",
                             policy=for_surface(CODE), use_cache=False)
    assert len(response.folds) == len(response.results) == 6
    assert not any(f.folded for f in response.folds)


def test_the_hash_reaches_the_result_row(engine):
    """Both retrievers already joined `files`; this is one more column on a
    row that was being fetched anyway."""
    from app.search.policy import SEARCH, for_surface

    response = engine.search("chapter", policy=for_surface(SEARCH),
                             use_cache=False)
    assert all(result.content_hash for result in response.results)
