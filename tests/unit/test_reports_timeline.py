r"""Order 202626270602 (0n) section 4 - the Life Timeline's queries.

Layer: L4

Real `SqliteStore`, real rows. What is proved here is the part a person would
notice being subtly wrong: which date a thing is placed by, that nothing is
lost or repeated across pages, that a file on a drive in a drawer is still
there, and that it never writes anything.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from pathlib import Path

import pytest

from app.reports import timeline as T
from app.reports.timeline import (
    BRANCH_DATED, BRANCH_FILE, BRANCH_MAIL, Cursor, Period, date_of_file, kind_of,
    timeline_overview, timeline_page,
)
from app.storage.sqlite_store import SqliteStore
from tests.unit.timeline_env import NS, add_file, add_mail, june_2015, noon


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as opened:
        yield opened


def names(page) -> list[str]:
    return [entry.name for entry in page.entries]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def test_a_month_covers_the_whole_month_and_nothing_of_the_next():
    june = Period.month(2015, 6)
    assert june.start_ns <= noon(2015, 6, 1) < june.end_ns
    assert june.start_ns <= noon(2015, 6, 30) < june.end_ns
    assert not june.start_ns <= noon(2015, 5, 31) < june.end_ns
    assert not june.start_ns <= noon(2015, 7, 1) < june.end_ns
    assert june.is_month and (june.after, june.before) == (dt.date(2015, 6, 1), dt.date(2015, 6, 30))


def test_december_rolls_into_the_next_year_without_falling_over():
    december = Period.month(2015, 12)
    assert december.before == dt.date(2015, 12, 31)
    assert december.start_ns <= noon(2015, 12, 31) < december.end_ns


def test_a_period_typed_the_way_the_search_box_takes_dates():
    """`before:2015` means the *whole* of 2015 in the search box, and must here."""
    whole = Period.from_words("2015", "2015")
    assert (whole.after, whole.before) == (dt.date(2015, 1, 1), dt.date(2015, 12, 31))
    june = Period.from_words("2015-06", "2015-06")
    assert june.is_month
    days = Period.from_words("2015-06-05", "2015-06-07")
    assert (days.after, days.before) == (dt.date(2015, 6, 5), dt.date(2015, 6, 7))
    assert days.start_ns <= noon(2015, 6, 7) < days.end_ns


def test_an_open_end_is_open():
    later = Period.from_words("2015", "")
    assert later.before is None and later.end_ns > noon(2200, 1, 1)
    earlier = Period.from_words("", "2015")
    assert earlier.after is None and earlier.start_ns < Period.year(1850).start_ns


def test_a_range_typed_backwards_is_turned_round_not_refused():
    assert Period.from_words("2015-08", "2015-06") == Period.from_words("2015-06", "2015-08")


def test_the_range_boxes_take_a_time_of_day_as_that_moment():
    r"""Order "dates" §1b: the range boxes read dates through the search box's
    own parser, so they take its new time forms - and a time is the moment,
    not its whole day, or 09:00 to 17:00 would quietly be the entire day."""
    def at(hour: int, minute: int = 0) -> int:
        return int(dt.datetime(2015, 6, 1, hour, minute).timestamp()) * NS

    working = Period.from_words("2015-06-01T09:00", "2015-06-01 17:00")
    assert working.start_ns == at(9)
    assert working.start_ns <= at(17) < working.end_ns    # 17:00 itself is in
    assert working.end_ns == at(17, 1)                      # the minute, whole
    assert not working.start_ns <= at(8, 59) < working.end_ns
    # One side a time, the other a day - and typed backwards, turned round
    # rather than refused on comparing a datetime with a date.
    mixed = Period.from_words("2015-06-02", "2015-06-01T09:00")
    assert mixed is not None and mixed.start_ns == at(9)
    from app.reports.timeline_words import period_words
    assert period_words(working) == "1 Jun 2015 09:00 to 1 Jun 2015 17:00"


def test_something_that_is_not_a_date_is_not_guessed_at():
    assert Period.from_words("banana", "") is None
    assert Period.from_words("2015", "not-a-date") is None


def test_a_period_before_1970_does_not_crash_windows():
    r"""`datetime.timestamp()` raises OSError 22 there for anything before
    1970 - and a scanned print from 1965 is exactly what this exists for."""
    old = Period.year(1965)
    assert old.start_ns < 0 < old.end_ns - old.start_ns
    assert old.end_ns - old.start_ns == pytest.approx(365 * 86400 * NS, abs=2 * 3600 * NS)


def test_cursors_survive_the_command_line():
    cursor = Cursor(1_434_000_000_000_000_000, BRANCH_FILE, 42)
    assert Cursor.decode(cursor.encode()) == cursor
    assert Cursor.decode("nonsense") is None and Cursor.decode("1:2") is None


# ---------------------------------------------------------------------------
# The truthful-date rules
# ---------------------------------------------------------------------------

def test_the_camera_date_wins_over_the_date_the_file_was_copied(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        june = timeline_page(store, Period.month(2015, 6))
        by_id = {e.file_id: e for e in june.entries}
        assert ids["photo"] in by_id and by_id[ids["photo"]].basis == "taken"
        # ...and the same photograph is NOT in the month its file was copied.
        assert ids["photo"] not in {e.file_id for e in timeline_page(store, Period.month(2019, 3)).entries}
    finally:
        store.close()


def test_a_letter_with_no_camera_is_placed_by_its_file_date_and_says_so(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        entry = {e.file_id: e for e in timeline_page(store, Period.month(2015, 6)).entries}[ids["letter"]]
        assert entry.basis == "saved" and entry.kind == "document"
        assert entry.when.date() == dt.date(2015, 6, 20)
    finally:
        store.close()


def test_a_message_is_placed_by_when_it_was_sent_never_by_its_containers_date(tmp_path):
    r"""Every message in a `.pst` carries the archive's own modified time; taken
    at face value, ten thousand letters are all "the day the archive was
    opened"."""
    store, ids = june_2015(tmp_path)
    try:
        entry = {e.file_id: e for e in timeline_page(store, Period.month(2015, 6)).entries}[ids["mail"]]
        assert entry.basis == "sent" and entry.kind == "mail" and entry.name == "Wedding plans"
        assert entry.when.date() == dt.date(2015, 6, 25)
        assert ids["mail"] not in {e.file_id for e in timeline_page(store, Period.month(2021, 3)).entries}
    finally:
        store.close()


def test_a_message_with_no_sent_date_is_not_placed_at_all(store):
    add_mail(store, "Mystery", sent=None, container_mtime=noon(2021, 3, 3))
    everything = timeline_page(store, Period.between(None, None))
    assert names(everything) == []


def test_a_year_guessed_from_a_folder_name_is_labelled_a_guess(store):
    add_file(store, r"D:\Scans\Summer 1999\print.jpg", mtime=noon(2019, 1, 1),
             taken=noon(1999, 1, 1), hint=True)
    entry = timeline_page(store, Period.year(1999)).entries[0]
    assert entry.basis == "guessed"


def test_a_file_with_no_usable_date_is_left_off_rather_than_put_in_1970(store):
    add_file(store, r"D:\odd.txt", mtime=0)
    assert timeline_page(store, Period.between(None, None)).entries == []


def test_date_of_file_uses_the_same_rules(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        assert date_of_file(store, ids["photo"]) == noon(2015, 6, 10)
        assert date_of_file(store, ids["letter"]) == noon(2015, 6, 20)
        assert date_of_file(store, ids["mail"]) == noon(2015, 6, 25)
        assert date_of_file(store, ids["undated"]) is None
        assert date_of_file(store, 99_999) is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Everything, across every source, in order
# ---------------------------------------------------------------------------

def test_june_2015_shows_the_photo_the_letter_the_drive_and_the_mail_oldest_first(tmp_path):
    r"""Section 5's own fixture test, and the order's acceptance sentence:
    "June 2015 is a place you can go - every photo, letter and file from that
    month, wherever it lives now"."""
    store, ids = june_2015(tmp_path)
    try:
        page = timeline_page(store, Period.month(2015, 6), connected={})
        assert [e.file_id for e in page.entries] == [
            ids["photo"], ids["offline"], ids["letter"], ids["mail"]]
        assert [e.when.day for e in page.entries] == [10, 15, 20, 25]
        assert page.done
        # what must not be there
        left_out = {ids["july"], ids["later_photo"], ids["code"], ids["undated"]}
        assert not left_out & {e.file_id for e in page.entries}
    finally:
        store.close()


def test_the_offline_drives_item_carries_its_source_and_cannot_be_opened(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        page = timeline_page(store, Period.month(2015, 6), connected={})
        offline = {e.file_id: e for e in page.entries}[ids["offline"]]
        assert offline.source_name == "Old WD" and offline.source_status == "offline"
        assert offline.on_a_source and not offline.reachable and offline.real_path is None
        assert offline.name == "beach.jpg"
        local = {e.file_id: e for e in page.entries}[ids["photo"]]
        assert local.source_name == "" and local.reachable
    finally:
        store.close()


def test_an_item_on_a_drive_that_is_plugged_in_resolves_to_its_real_path(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        volume = store.list_volumes()[0]["id"]
        page = timeline_page(store, Period.month(2015, 6),
                             connected={int(volume): Path("E:/")})
        online = {e.file_id: e for e in page.entries}[ids["offline"]]
        assert online.reachable
        assert online.real_path is not None and online.real_path.replace("\\", "/").endswith("Holiday/beach.jpg")
    finally:
        store.close()


def test_the_kind_narrows_the_month(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        def ids_of(kind):
            return {e.file_id for e in timeline_page(store, Period.month(2015, 6), kind=kind,
                                                     connected={}).entries}
        assert ids_of("photos") == {ids["photo"], ids["offline"]}
        assert ids_of("documents") == {ids["letter"]}
        assert ids_of("mail") == {ids["mail"]}
        assert ids_of("code") == {ids["code"]}
        assert ids_of("videos") == set()
        assert ids_of("no-such-kind") == ids_of("everything")      # never an error
    finally:
        store.close()


def test_program_code_is_left_out_of_everything_but_one_choice_away(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        everything = {e.file_id for e in timeline_page(store, Period.month(2015, 6), connected={}).entries}
        assert ids["code"] not in everything
    finally:
        store.close()


def test_videos_are_rows_of_their_own_kind(store):
    add_file(store, r"D:\Films\trip.mp4", mtime=noon(2015, 6, 3), taken=noon(2015, 6, 2))
    entry = timeline_page(store, Period.month(2015, 6)).entries[0]
    assert entry.kind == "video" and entry.basis == "taken"
    assert kind_of("mp4") == "video" and kind_of("JPG") == "photo" and kind_of(".docx") == "document"
    assert kind_of("", "pst_message") == "mail" and kind_of("py", "file", 3) == "code"


def test_files_inside_an_archive_are_not_the_timelines_business(store):
    add_file(store, r"D:\Old.zip!\inside.txt", mtime=noon(2015, 6, 3), source_kind="archive")
    assert timeline_page(store, Period.month(2015, 6)).entries == []


def test_the_photo_and_video_lists_match_what_the_indexer_reads():
    """The timeline's idea of a photograph must not drift from the indexer's."""
    from app.extract.media import VIDEO_EXTENSIONS
    from app.extract.ocr import OcrExtractor
    from app.extract.raw import RawExtractor

    indexer_photos = {e.lstrip(".") for e in OcrExtractor.extensions | RawExtractor.extensions} - {"svg"}
    assert T.PHOTO_EXTS == indexer_photos
    assert T.VIDEO_EXTS == {e.lstrip(".") for e in VIDEO_EXTENSIONS}


# ---------------------------------------------------------------------------
# Paging: a keyset, complete and without repeats
# ---------------------------------------------------------------------------

def _walk(store, period, limit, **options):
    seen, cursor, pages = [], None, 0
    while True:
        page = timeline_page(store, period, cursor=cursor, limit=limit, fold=False, **options)
        seen.extend(page.entries)
        pages += 1
        cursor = page.cursor
        assert pages < 500, "paging never ended"
        if cursor is None:
            return seen, pages


def test_paging_visits_every_item_once_in_order_across_all_three_sources(store):
    """Deliberately awkward: several items share one instant, on different
    sources, so a page boundary can fall between them."""
    rng = random.Random(7)
    instant = noon(2015, 6, 10)
    for i in range(40):
        when = instant if i % 3 == 0 else noon(2015, 6, rng.randint(1, 28), rng.randint(0, 5000))
        add_file(store, rf"D:\p\photo{i}.jpg", mtime=noon(2019, 1, 1), taken=when)
        add_file(store, rf"D:\l\doc{i}.docx", mtime=when)
        add_mail(store, f"m{i}", sent=when, container_mtime=noon(2021, 1, 1))
    period = Period.month(2015, 6)
    everything, _ = _walk(store, period, 1000)
    assert len(everything) == 120
    for limit in (1, 2, 3, 7, 50):
        paged, pages = _walk(store, period, limit)
        assert [e.file_id for e in paged] == [e.file_id for e in everything], limit
        assert pages == -(-120 // limit)
    keys = [(e.when_ns, e.file_id) for e in everything]
    assert [e.when_ns for e in everything] == sorted(e.when_ns for e in everything)
    assert len(set(keys)) == 120


def test_a_full_last_page_is_not_followed_by_an_empty_one(store):
    for i in range(6):
        add_file(store, rf"D:\l\d{i}.txt", mtime=noon(2015, 6, 1 + i))
    page = timeline_page(store, Period.month(2015, 6), limit=6)
    assert page.covered == 6 and page.done and page.cursor is None


def test_a_new_file_arriving_mid_scroll_does_not_shift_the_next_page(store):
    for i in range(10):
        add_file(store, rf"D:\l\d{i}.txt", mtime=noon(2015, 6, 10 + i))
    first = timeline_page(store, Period.month(2015, 6), limit=4, fold=False)
    add_file(store, r"D:\l\newcomer.txt", mtime=noon(2015, 6, 1))        # older than the cursor
    second = timeline_page(store, Period.month(2015, 6), limit=4, cursor=first.cursor, fold=False)
    assert names(second) == ["d4.txt", "d5.txt", "d6.txt", "d7.txt"]      # an OFFSET would repeat one


def test_an_empty_period_is_an_empty_done_page_not_an_error(store):
    page = timeline_page(store, Period.month(1901, 1))
    assert page.items == () and page.done and page.covered == 0


def test_a_wide_free_range_pages_like_a_narrow_one(store):
    add_file(store, r"D:\a.txt", mtime=noon(2005, 1, 1))
    add_file(store, r"D:\b.txt", mtime=noon(2019, 12, 31))
    page = timeline_page(store, Period.between(dt.date(2000, 1, 1), dt.date(2020, 12, 31)))
    assert names(page) == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# Density: a burst is one row that says how many are behind it
# ---------------------------------------------------------------------------

def test_a_burst_of_near_identical_photographs_folds_into_one_row(store):
    base = 0x00FF00FF00FF00FF
    for i in range(6):
        add_file(store, rf"D:\Pictures\burst{i}.jpg", mtime=noon(2019, 1, 1),
                 taken=noon(2015, 6, 10, i), phash=f"{base ^ (1 << i):016x}")
    add_file(store, r"D:\Pictures\other.jpg", mtime=noon(2019, 1, 1),
             taken=noon(2015, 6, 11), phash="ffffffff00000000")
    page = timeline_page(store, Period.month(2015, 6))
    assert len(page.items) == 2
    burst = page.items[0]
    assert burst.folded and len(burst.older) == 5 and "similar photos" in burst.label()
    assert page.covered == 7 and len(page.entries) == 7          # nothing lost behind the fold


def test_folding_can_be_turned_off_and_then_every_photograph_is_its_own_row(store):
    for i in range(4):
        add_file(store, rf"D:\P\b{i}.jpg", mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10, i),
                 phash=f"{0xABCDABCDABCDABCD ^ (1 << i):016x}")
    assert len(timeline_page(store, Period.month(2015, 6), fold=False).items) == 4
    assert len(timeline_page(store, Period.month(2015, 6), fold=True).items) == 1


def test_identical_copies_on_two_sources_fold_and_the_offline_copy_is_still_named(store):
    drive = store.upsert_volume("g", kind="drive", name="Old WD", status="OFFLINE")
    add_file(store, r"D:\Pictures\a.jpg", mtime=noon(2015, 6, 10), content_hash="samebytes")
    add_file(store, "leasha-volume://1/a.jpg", mtime=noon(2015, 6, 10, 5), content_hash="samebytes",
             volume_id=drive, relative_path="a.jpg")
    page = timeline_page(store, Period.month(2015, 6), connected={})
    assert len(page.items) == 1 and "identical copy" in page.items[0].label()
    assert {e.source_name for e in page.entries} == {"", "Old WD"}


# ---------------------------------------------------------------------------
# The overview: counts, and what it cannot say
# ---------------------------------------------------------------------------

def test_the_overview_counts_by_month_and_by_how_each_item_was_dated(tmp_path):
    store, _ids = june_2015(tmp_path)
    try:
        overview = timeline_overview(store)
        counts = {(y, m): n for y, m, n in overview.months}
        assert counts[(2015, 6)] == 4              # photo, drive photo, letter, message
        assert counts[(2015, 7)] == 1 and counts[(2019, 5)] == 1
        assert overview.total == 6                  # code and the undated file are not counted
        assert (overview.by_camera, overview.by_file_date, overview.by_sent_date) == (3, 2, 1)
        assert overview.undated == 1
        assert overview.years == ((2015, 5), (2019, 1))
        assert overview.months_of(2015)[6] == 4 and overview.months_of(2015)[1] == 0
        assert overview.first == (2015, 6) and overview.last == (2019, 5)
    finally:
        store.close()


def test_the_overview_of_an_empty_index_is_empty_and_says_nothing_untrue(store):
    overview = timeline_overview(store)
    assert overview.total == 0 and overview.months == () and overview.years == ()
    assert overview.first is None and overview.last is None


def test_the_overview_follows_the_kind(tmp_path):
    store, _ids = june_2015(tmp_path)
    try:
        assert timeline_overview(store, kind="mail").total == 1
        assert timeline_overview(store, kind="photos").total == 3
        assert timeline_overview(store, kind="code").total == 1
    finally:
        store.close()


def test_the_overview_agrees_with_what_paging_actually_returns(tmp_path):
    """A picker that promises 4 and a month that shows 5 is the bug this pins."""
    store, _ids = june_2015(tmp_path)
    try:
        overview = timeline_overview(store)
        for year, month, count in overview.months:
            shown, _ = _walk(store, Period.month(year, month), 3, connected={})
            assert len(shown) == count, (year, month)
    finally:
        store.close()


def test_photos_with_no_camera_date_are_counted_apart(store):
    add_file(store, r"D:\P\a.jpg", mtime=noon(2015, 6, 1))
    add_file(store, r"D:\P\b.jpg", mtime=noon(2015, 6, 2), taken=noon(2010, 1, 1))
    overview = timeline_overview(store)
    assert (overview.photos_with_camera_date, overview.photos_by_file_date_only) == (1, 1)


# ---------------------------------------------------------------------------
# Read-only, and never raises
# ---------------------------------------------------------------------------

def test_reading_the_timeline_changes_nothing(tmp_path):
    store, ids = june_2015(tmp_path)
    try:
        before = store.conn.total_changes
        rows_before = store.conn.execute("SELECT COUNT(*), MAX(mtime_ns), MAX(id) FROM files").fetchone()[:]
        timeline_page(store, Period.month(2015, 6), connected={})
        timeline_page(store, Period.between(None, None), limit=2, connected={})
        timeline_overview(store)
        date_of_file(store, ids["photo"])
        assert store.conn.total_changes == before
        assert store.conn.execute("SELECT COUNT(*), MAX(mtime_ns), MAX(id) FROM files").fetchone()[:] == rows_before
    finally:
        store.close()


def test_the_timeline_module_never_writes():
    """Source-scan, in the style of `test_the_inheritance_module_never_writes_to_disk`."""
    source = Path(T.__file__).read_text(encoding="utf-8")
    for pattern in (r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b", r"\bALTER\b",
                    r"\.write\(", r"\bopen\(", r"write_text", r"write_bytes", r"os\.remove",
                    r"shutil", r"\.unlink\(", r"\bcommit\("):
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "r\"")))
        assert not re.search(pattern, code), pattern


def test_a_branch_that_fails_costs_a_shorter_page_not_a_traceback(store):
    add_file(store, r"D:\a.txt", mtime=noon(2015, 6, 1))
    add_mail(store, "m", sent=noon(2015, 6, 2), container_mtime=noon(2021, 1, 1))

    class NoMailTable:
        """Delegates to the real connection, but the messages table 'is gone'."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if "messages" in sql:
                raise RuntimeError("no such table: messages")
            return self._real.execute(sql, *args)

    class Wrapped:
        conn = NoMailTable(store.conn)

    page = timeline_page(Wrapped(), Period.month(2015, 6))
    assert names(page) == ["a.txt"]                     # the files still came back
    assert timeline_overview(Wrapped()).total == 1      # and so did their count


def test_a_store_with_no_connection_at_all_gives_an_empty_page():
    class Broken:
        conn = None

    assert timeline_page(Broken(), Period.month(2015, 6)).items == ()
    assert timeline_overview(Broken()).total == 0


def test_the_branch_numbers_are_the_documented_order():
    assert (BRANCH_DATED, BRANCH_FILE, BRANCH_MAIL) == (0, 1, 2)
