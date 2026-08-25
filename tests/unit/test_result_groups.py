"""One row per document, not one per chunk.

Layer: L5

**The problem.** Results are chunk-level and nothing grouped them, so a long PDF
matching in five places took five of the top ten rows. The person saw three
documents where they should have seen ten.

**Grouping is display-only, and that is load-bearing.** The measured baseline in
HANDOFF.md §3b - 75% at rank 1 after translation - was taken against chunk-level
ranking. Grouping inside the engine would change what "rank 1" means and make
every future measurement incomparable with that one, silently. `SearchEngine`
must keep returning exactly what it returns today.
"""

from __future__ import annotations

import time

import pytest

from app.ui.presenter import (
    GROUP_FETCH_MULTIPLIER,
    ResultRow,
    Snippet,
    breadcrumb,
    fetch_depth,
    group_results,
)


def row(file_id, score, *, chunk_id=0, path=None, page=None, ext="pdf",
        mtime_ns=1_700_000_000_000_000_000):
    return ResultRow(
        rank=0,
        chunk_id=chunk_id or int(score * 1000),
        file_id=file_id,
        path=path or rf"D:\Archive\2019\Leeds\file{file_id}.pdf",
        display_path="",
        snippet=Snippet(f"chunk {chunk_id}"),
        explain="keyword match",
        location=f"page {page}" if page else "",
        score=score,
        ext=ext,
        mtime_ns=mtime_ns,
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_five_chunks_from_two_files_produce_two_groups():
    """**The whole point.** Five of the top ten rows going to one PDF is the
    complaint this exists to answer."""
    rows = [row(1, 0.9), row(1, 0.8), row(2, 0.7), row(1, 0.6), row(2, 0.5)]
    groups = group_results(rows)
    assert len(groups) == 2
    assert [g.file_id for g in groups] == [1, 2]


def test_groups_are_ordered_by_their_best_chunk():
    """The engine's order is preserved rather than recomputed - first appearance
    decides position, which is what keeps this display-only."""
    rows = [row(2, 0.95), row(1, 0.9), row(2, 0.4)]
    assert [g.file_id for g in group_results(rows)] == [2, 1]


def test_a_groups_best_is_its_highest_scoring_chunk():
    rows = [row(1, 0.9, chunk_id=11), row(1, 0.5, chunk_id=12)]
    group = group_results(rows)[0]
    assert group.best.chunk_id == 11


def test_rows_are_ordered_best_first():
    rows = [row(1, 0.9, chunk_id=11), row(1, 0.7, chunk_id=12), row(1, 0.5, chunk_id=13)]
    group = group_results(rows)[0]
    assert [r.chunk_id for r in group.rows] == [11, 12, 13]


def test_the_group_score_is_the_best_chunk_not_the_mean():
    """**Averaging punishes a long document that matches strongly once** - which
    is the common case in an archive, and precisely the document somebody is
    looking for."""
    strong_once = group_results([row(1, 0.95), row(1, 0.1), row(1, 0.1)])[0]
    assert strong_once.score == 0.95, "the mean would be 0.38 and would sort it last"


def test_a_single_match_group_reports_no_match_count():
    """"1 match" is noise on every row of a list where one is the normal case."""
    assert group_results([row(1, 0.9)])[0].match_label == ""


def test_a_multi_match_group_says_how_many():
    assert group_results([row(1, 0.9), row(1, 0.8)])[0].match_label == "2 matches"


def test_every_matching_chunk_is_kept_for_expanding():
    """The chunks are the reason to expand a row - each opens the document at
    its own page."""
    groups = group_results([row(1, 0.9, page=4), row(1, 0.8, page=11)])
    assert [r.location for r in groups[0].rows] == ["page 4", "page 11"]


def test_the_limit_counts_groups_not_chunks():
    """A limit counting chunks would show two documents when asked for three,
    which is the original bug wearing a different hat."""
    rows = [row(1, 0.9), row(1, 0.8), row(2, 0.7), row(3, 0.6)]
    assert len(group_results(rows, limit=2)) == 2


def test_no_rows_is_no_groups_rather_than_an_error():
    assert group_results([]) == []


# ---------------------------------------------------------------------------
# Fetching deeper than the display
# ---------------------------------------------------------------------------

def test_the_fetch_is_deeper_than_the_display():
    """Grouping shrinks the list. A fetch sized for the display count leaves the
    page half empty on exactly the corpora this feature exists for - one long
    document owning thirty of fifty chunks."""
    assert fetch_depth(10) > 10
    assert fetch_depth(10) == 10 * GROUP_FETCH_MULTIPLIER


def test_the_multiplier_is_bounded():
    """It costs rerank time on every search, so it cannot be arbitrarily large."""
    assert 2 <= GROUP_FETCH_MULTIPLIER <= 8


def test_a_zero_display_count_still_fetches_something():
    assert fetch_depth(0) >= 1


# ---------------------------------------------------------------------------
# What the row says
# ---------------------------------------------------------------------------

def test_the_name_is_the_filename_not_the_path():
    """A person recognises `report_final_v3.pdf`. Nobody scans
    `D:\\Archive\\2019\\Projects\\...`."""
    group = group_results([row(1, 0.9, path=r"D:\Archive\2019\report_final_v3.pdf")])[0]
    assert group.name == "report_final_v3.pdf"


def test_the_full_path_survives_for_open_reveal_and_copy():
    """**The breadcrumb is a display choice and must never be the only copy of
    the truth** - opening a file needs the real path."""
    original = r"D:\Archive\2019\Projects\Leeds\report.pdf"
    group = group_results([row(1, 0.9, path=original)])[0]
    assert group.path == original
    assert group.rows[0].path == original


def test_the_folder_is_a_breadcrumb_keeping_the_end():
    """`shorten_path` elides the middle, which is exactly where the
    distinguishing part of a long archive path lives."""
    group = group_results([row(1, 0.9, path=r"D:\Archive\2019\Projects\Leeds\r.pdf")])[0]
    assert group.folder == "… > 2019 > Projects > Leeds"


def test_a_drive_letter_is_not_a_folder_anybody_thinks_in():
    assert "D:" not in breadcrumb(r"D:\Archive\Leeds")


def test_a_short_path_is_not_given_a_misleading_ellipsis():
    assert breadcrumb("D:/Archive/Leeds") == "Archive > Leeds"


def test_an_empty_path_is_empty_rather_than_an_error():
    assert breadcrumb("") == ""


def test_the_kind_comes_from_the_indexed_extension():
    assert group_results([row(1, 0.9, ext="xlsx")])[0].kind == "xlsx"


def test_the_kind_falls_back_to_the_filename_when_the_index_has_none():
    """A cached result from before `ext` existed still has a filename."""
    group = group_results([row(1, 0.9, ext="", path=r"D:\a\sheet.XLSX")])[0]
    assert group.kind == "xlsx"


def test_the_date_is_shown():
    """In a fifteen-year archive with eight versions of everything, the date is
    frequently the only thing that distinguishes two results."""
    assert group_results([row(1, 0.9)])[0].when != ""


def test_an_unknown_date_renders_empty_rather_than_1970():
    """`mtime_ns=0` means "not known". "1 Jan 1970" is a claim, and a false one."""
    group = group_results([row(1, 0.9, mtime_ns=0)])[0]
    assert group.when == ""
    assert "1970" not in group.when


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

MESSAGE = {
    "subject": "Licence renewal",
    "sender": "Chris Bell <chris@acme.com>",
    "sent_at": int(time.time()) - 86_400,
    "has_attach": 1,
}


def test_a_message_group_uses_its_subject_as_the_name():
    """A message's path is a synthetic key nobody typed and nobody would
    recognise."""
    group = group_results([row(9, 0.9, path="pst://x/0001")], details={9: MESSAGE})[0]
    assert group.name == "Licence renewal"


def test_a_message_group_shows_the_sender_instead_of_a_folder():
    group = group_results([row(9, 0.9)], details={9: MESSAGE})[0]
    assert "Chris Bell" in group.folder


def test_a_message_with_an_attachment_says_so():
    group = group_results([row(9, 0.9)], details={9: MESSAGE})[0]
    assert "attachment" in group.folder


def test_a_message_with_no_subject_says_so_rather_than_being_blank():
    """Blank looks like a rendering fault; an empty subject is common."""
    group = group_results([row(9, 0.9)], details={9: {"subject": ""}})[0]
    assert group.name == "(no subject)"


def test_a_message_group_is_kind_email():
    assert group_results([row(9, 0.9)], details={9: MESSAGE})[0].kind == "email"


def test_a_file_with_no_message_row_falls_back_without_raising():
    """Most results are files. A `details` map that simply has no entry for one
    must not be an error."""
    group = group_results([row(1, 0.9)], details={9: MESSAGE})[0]
    assert group.name == "file1.pdf"
    assert group.kind == "pdf"


def test_details_may_be_omitted_entirely():
    assert group_results([row(1, 0.9)], details=None)[0].name == "file1.pdf"


# ---------------------------------------------------------------------------
# The engine must be untouched
# ---------------------------------------------------------------------------

def test_grouping_does_not_reorder_the_rows_it_was_given():
    """Proof that this is display-only: the input list is untouched, so the
    §3b measurement still measures the same thing."""
    rows = [row(2, 0.95), row(1, 0.9), row(2, 0.4)]
    before = list(rows)
    group_results(rows)
    assert rows == before


def test_every_chunk_survives_grouping():
    """Nothing is dropped - a group holds every matching chunk, so expanding
    shows what the engine actually found."""
    rows = [row(1, 0.9), row(1, 0.8), row(2, 0.7)]
    groups = group_results(rows)
    assert sum(g.match_count for g in groups) == len(rows)
