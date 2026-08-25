r"""Folders declared static, and the run that is allowed to skip them.

Layer: L3

From `docs/WORKORDER-terabyte-scale.md` §2a, which calls this *"the highest-value
item here"*: the corpus is historic, so the cost of indexing it is paid once and
the cost of **re-walking** it is paid for ever. Marking a root as an archive
turns an incremental pass over a settled 1.5TB from hours of fruitless `stat()`
calls into seconds.

**The rule is "skip cheaply, but never silently"**, and both halves are load
bearing. An archive that is skipped is an archive nobody is checking, so the
failure mode - a file that changed and was never re-read - is invisible by
construction. Every test below is either about a change that must *not* be
missed, or about the skip being visible when it happens.
"""

from __future__ import annotations

import json

import pytest

from app.index.archives import (
    ARCHIVE,
    DEFAULT_RECHECK_DAYS,
    LIVE,
    ArchiveRecord,
    RootPlan,
    directory_mtime,
    dump_modes,
    dump_records,
    files_under,
    load_modes,
    load_records,
    normalise,
    plan_roots,
    record_pass,
)

DAY = 86_400
NOW = 1_800_000_000.0


def _record(root=r"D:\Archive", *, age_days=1.0, files=4_000, mtime_ns=111):
    return ArchiveRecord(root=root, archived_at=int(NOW - age_days * DAY),
                         files=files, mtime_ns=mtime_ns)


def _stat(mtime_ns: int):
    class Stat:
        st_mtime_ns = mtime_ns

    return lambda _path: Stat()


# --- the decision -----------------------------------------------------------

def test_a_live_folder_is_always_walked():
    """The default, and the answer that is never wrong - only slow."""
    plans = plan_roots([r"D:\Current"], modes={}, records={}, now=NOW)

    assert [p.walk for p in plans] == [True]
    assert plans[0].mode == LIVE
    assert plans[0].reason


def test_an_archive_that_has_not_had_a_full_pass_is_walked():
    """**Marking a folder as an archive must not be a way to never index it.**

    The first pass always happens. Without this, somebody who marks a folder
    before the first run gets an empty index and a panel saying the folder was
    skipped because it is an archive - which is circular and permanent.
    """
    plans = plan_roots([r"D:\Archive"], modes={normalise(r"D:\Archive"): ARCHIVE},
                       records={}, now=NOW)

    assert plans[0].walk
    assert "has not had a full pass" in plans[0].reason


def test_a_settled_archive_is_skipped():
    plans = plan_roots(
        [r"D:\Archive"],
        modes={normalise(r"D:\Archive"): ARCHIVE},
        records={normalise(r"D:\Archive"): _record()},
        now=NOW, stat=_stat(111),
    )

    assert not plans[0].walk
    assert plans[0].files == 4_000


def test_the_folders_own_timestamp_is_the_tripwire():
    r"""**One `stat()` per root, and it catches the case that happens.**

    Creating, deleting or renaming an entry in a directory moves that
    directory's mtime. So somebody dropping `D:\Archive\2027` into an archive
    is caught for the cost of a single syscall, without walking a byte of the
    twelve years underneath it.
    """
    plans = plan_roots(
        [r"D:\Archive"],
        modes={normalise(r"D:\Archive"): ARCHIVE},
        records={normalise(r"D:\Archive"): _record(mtime_ns=111)},
        now=NOW, stat=_stat(222),                  # something was added
    )

    assert plans[0].walk
    assert "changed" in plans[0].reason


def test_an_unreadable_root_is_walked_rather_than_skipped():
    """`directory_mtime` returns 0 on failure and 0 never matches a stored
    mtime, so the failure direction is "do the work"."""
    def refuse(_path):
        raise PermissionError("no")

    plans = plan_roots(
        [r"D:\Archive"],
        modes={normalise(r"D:\Archive"): ARCHIVE},
        records={normalise(r"D:\Archive"): _record(mtime_ns=111)},
        now=NOW, stat=refuse,
    )

    assert plans[0].walk


def test_the_interval_is_the_backstop_for_a_change_the_mtime_cannot_see():
    """A file edited in place, deep in the tree, leaves every directory above
    it untouched. Nothing cheap can see that, so time is the only guard."""
    settled = {normalise(r"D:\Archive"): ARCHIVE}
    records = {normalise(r"D:\Archive"): _record(age_days=31)}

    plans = plan_roots([r"D:\Archive"], modes=settled, records=records,
                       now=NOW, stat=_stat(111), recheck_days=30)

    assert plans[0].walk
    assert "31 days" in plans[0].reason


def test_zero_days_means_only_when_it_changes_or_when_asked():
    """The setting's "Only when it changes" position. Not "walk every time" -
    which is what an off-by-one here would silently produce."""
    plans = plan_roots(
        [r"D:\Archive"],
        modes={normalise(r"D:\Archive"): ARCHIVE},
        records={normalise(r"D:\Archive"): _record(age_days=9_999)},
        now=NOW, stat=_stat(111), recheck_days=0,
    )

    assert not plans[0].walk


def test_a_rescan_walks_it_whatever_everything_else_says():
    plans = plan_roots(
        [r"D:\Archive"],
        modes={normalise(r"D:\Archive"): ARCHIVE},
        records={normalise(r"D:\Archive"): _record()},
        now=NOW, stat=_stat(111), recheck=True,
    )

    assert plans[0].walk
    assert "rescan" in plans[0].reason


def test_one_corpus_can_be_both_at_once():
    """*"a corpus is nearly always both: the mail folder that changes hourly
    sits beside twelve years of project files that do not."*"""
    modes = {normalise(r"D:\Archive"): ARCHIVE}
    records = {normalise(r"D:\Archive"): _record()}

    plans = plan_roots([r"D:\Mail", r"D:\Archive", r"D:\Current"],
                       modes=modes, records=records, now=NOW, stat=_stat(111))

    assert [p.walk for p in plans] == [True, False, True]


def test_every_plan_carries_a_reason():
    """A skip with no reason is the thing this whole module exists not to be."""
    modes = {normalise(r"D:\A"): ARCHIVE}
    records = {normalise(r"D:\A"): _record(root=r"D:\A")}
    plans = plan_roots([r"D:\A", r"D:\B"], modes=modes, records=records,
                       now=NOW, stat=_stat(111))

    assert all(p.reason.strip() for p in plans)


# --- what the skip says -----------------------------------------------------

def test_a_skipped_root_says_how_many_files_and_when():
    r"""**The sentence that stops somebody deleting their index.**

    A folder that was deliberately not walked looks exactly like a folder that
    was never indexed - unless it says how much is in it and when it was last
    read. This is the difference between "left alone on purpose, 4,000 files,
    read in full three weeks ago" and a name with nothing beside it.
    """
    plan = RootPlan(r"D:\Archive", ARCHIVE, False, "nothing has changed", _record())

    line = plan.describe()

    assert "4,000 file(s)" in line
    assert "skipped" in line
    # A real date, not "unknown" - the record has one.
    assert "unknown date" not in line


def test_a_skip_with_no_record_still_says_so_rather_than_lying():
    plan = RootPlan(r"D:\Archive", ARCHIVE, False, "nothing has changed", None)

    assert "an unknown date" in plan.describe()
    assert "0 file(s)" in plan.describe()


# --- recording a pass -------------------------------------------------------

def test_a_walked_archive_records_its_count_and_the_folders_timestamp():
    plans = (RootPlan(r"D:\Archive", ARCHIVE, True, "first pass"),)
    counts = {normalise(r"D:\Archive"): 12_345}

    records = record_pass({}, plans, counts, now=NOW, stat=_stat(999))

    saved = records[normalise(r"D:\Archive")]
    assert saved.files == 12_345
    assert saved.mtime_ns == 999
    assert saved.archived_at == int(NOW)


def test_a_live_root_gets_no_record():
    """Its mode may change later, and a stale count from whenever it happened
    to be an archive is worse than none."""
    plans = (RootPlan(r"D:\Current", LIVE, True, "live"),)

    assert record_pass({}, plans, {}, now=NOW, stat=_stat(1)) == {}


def test_a_skipped_archive_keeps_the_record_it_already_had():
    """It was not walked, so this run learned nothing about it. Overwriting
    the date would extend the trust without any evidence for it - which is
    exactly how an archive stops being checked for ever."""
    existing = {normalise(r"D:\Archive"): _record(age_days=20)}
    plans = (RootPlan(r"D:\Archive", ARCHIVE, False, "nothing changed",
                      existing[normalise(r"D:\Archive")]),)

    after = record_pass(existing, plans, {}, now=NOW, stat=_stat(1))

    assert after == existing


# --- the two state keys -----------------------------------------------------

def test_only_archives_are_written_down():
    """`live` is the default, so writing it would leave a file full of entries
    that mean "no change", and a removed folder would keep a mode for ever."""
    raw = dump_modes({r"D:\A": ARCHIVE, r"D:\B": LIVE})

    assert json.loads(raw) == {normalise(r"D:\A"): ARCHIVE}


def test_modes_survive_a_round_trip_including_the_windows_path_forms():
    r"""These are written by the window and read back by the command line, and
    the same folder appears as `D:\Archive`, `D:\Archive\` and `d:\archive` in
    the two. A mode keyed under one and looked up under another is a setting
    that silently does nothing."""
    stored = load_modes(dump_modes({"D:\\Archive\\": ARCHIVE}))

    assert stored[normalise(r"d:\ARCHIVE")] == ARCHIVE


@pytest.mark.parametrize("raw", ["", "not json", "[]", "3", '{"D:\\\\A": "banana"}'])
def test_an_unreadable_mode_record_means_everything_is_live(raw):
    """The slow answer, and never the wrong one. Failing a run because a
    preference could not be parsed would be a poor trade."""
    assert load_modes(raw) == {}


def test_records_survive_a_round_trip():
    records = {normalise(r"D:\A"): _record(root=r"D:\A", files=7, mtime_ns=42)}

    back = load_records(dump_records(records))

    assert back[normalise(r"D:\A")].files == 7
    assert back[normalise(r"D:\A")].mtime_ns == 42


@pytest.mark.parametrize("raw", ["", "{", "[]", '{"a": 3}', '{"a": {"files": "x"}}'])
def test_an_unreadable_archive_record_is_no_record(raw):
    """Which means the archive is walked in full - the safe direction."""
    load_records(raw)                              # must not raise


def test_a_real_directory_mtime_is_read(tmp_path):
    """The seam is tested everywhere else; this proves the default reads
    something real, so a refactor cannot leave the tripwire always returning
    0 and every archive walked for ever."""
    assert directory_mtime(tmp_path) > 0
    assert directory_mtime(tmp_path / "nope") == 0


# --- attributing a file to a root ------------------------------------------

def test_a_file_is_attributed_to_the_longest_matching_root():
    r"""One indexed root routinely sits inside another. A first match over an
    unordered list attributes files to whichever was seen first."""
    roots = [r"D:\Archive", r"D:\Archive\2019"]

    assert files_under(r"D:\Archive\2019\a.txt", roots) == normalise(r"D:\Archive\2019")
    assert files_under(r"D:\Archive\2020\a.txt", roots) == normalise(r"D:\Archive")
    assert files_under(r"D:\Elsewhere\a.txt", roots) is None


def test_attribution_does_not_use_path_parent():
    r"""`PurePosixPath(r"D:\Archive\a.txt").parent` is `.` off Windows - the
    whole path is one filename - and this project has been caught by that four
    separate times. A prefix test on the normalised string works on both."""
    assert files_under(r"D:\Archive\deep\nested\a.txt", [r"D:\Archive"]) is not None


def test_a_root_is_not_its_own_prefix_by_accident():
    r"""`D:\Arch` must not claim files under `D:\Archive`."""
    assert files_under(r"D:\Archive\a.txt", [r"D:\Arch"]) is None


def test_the_default_interval_is_a_month_not_a_week():
    """Documented so a change is deliberate: the mtime tripwire is what
    actually catches change, and the interval is only the backstop."""
    assert DEFAULT_RECHECK_DAYS == 30
