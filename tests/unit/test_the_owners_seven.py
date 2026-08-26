r"""Six reports, in the owner's words, each asserted as the rule it stated.

Layer: L0, L4 and L5

> *"i rest the index the index size remained the same, look at alignment of
> things on settings page some of the check boxes are cut, there needs to be a
> button to clear logs, change model has only one model, the code tab is
> screwed it does not have a search box or nothing, when you close the app it
> lingers for a while because if i start it it says it is running"*

Six separate faults in one sentence. The alignment one is in
`test_settings_layout.py`, because it needs a laid-out page; the other five are
here.

**Each test asserts what the owner said, not what was changed.** That
distinction has cost this project real time: the column widths were fixed three
times, each fix shipped with a green test of the mechanism it had just altered,
and each time the report came back unchanged. A test that says "the guard I
added is present" proves the guard, not the behaviour. So these are phrased as
the sentences above: *the box is there*, *the space comes back*, *the message
names an action*.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# "when you close the app it lingers ... if i start it it says it is running"
# ---------------------------------------------------------------------------

def test_a_start_waits_for_a_copy_that_is_letting_go(tmp_path):
    r"""**The window is not lingering; it is closing, and that takes seconds.**

    Read from the run logs rather than assumed: pid 30200's event loop did not
    return until 21:20:59, and the two launches at 21:20:47 and 21:20:58 were
    refused inside that window while the third, at 21:21:00, opened normally.
    The lock is held for the whole of the shutdown - correctly, the stores are
    still open - and by then the window has already vanished from the screen, so
    trying again is the natural thing to do.

    Waiting is the fix, and this is the assertion that it waits: a lock released
    by another owner part-way through the window must be acquired rather than
    refused.
    """
    import threading
    import time

    from app.core.single_instance import SingleInstance

    holder = SingleInstance("handover-test", lock_dir=tmp_path).acquire()
    # Let go shortly after the second copy starts asking, the way a real close
    # finishes while somebody is double-clicking the shortcut.
    threading.Timer(0.6, holder.release).start()

    started = time.monotonic()
    second = SingleInstance("handover-test", lock_dir=tmp_path)
    second.acquire(wait_s=8.0)
    waited = time.monotonic() - started

    assert second.acquired, "a copy that let go during the wait was still refused"
    assert waited >= 0.4, "it cannot have waited for the other copy at all"
    second.release()


def test_a_start_still_refuses_a_window_that_is_genuinely_open(tmp_path):
    """The wait must not become an unlock. Two windows on one index is the
    corruption the whole mechanism exists to prevent, and a second copy that
    waits *forever* is a second copy that eventually gets in."""
    from app.core.errors import AppErrorException
    from app.core.single_instance import SingleInstance

    holder = SingleInstance("still-open-test", lock_dir=tmp_path).acquire()
    try:
        with pytest.raises(AppErrorException) as caught:
            SingleInstance("still-open-test", lock_dir=tmp_path).acquire(wait_s=0.5)
        assert caught.value.error.code == "ERR_DB_LOCKED"
    finally:
        holder.release()


def test_the_refusal_names_something_other_than_closing_the_window():
    r"""The project rule, applied to the one message this report produced.

    > *"even when printing errors it should suggest solutions"*

    The old suggestion was *"Close the other copy, then try again"*. After a
    twelve-second wait that is not a solution, it is the thing the person has
    already done - so the message has to cover the case where no window is
    visible at all.
    """
    from app.core.errors import make_error

    fix = make_error("ERR_DB_LOCKED", "core.single_instance").suggestion.lower()

    assert "task manager" in fix, "no route out when no window can be seen"
    assert "cannot see" in fix or "if you" in fix, "it assumes a visible window"


def test_closing_says_how_long_it_took():
    """A shutdown nobody can time is a shutdown nobody can fix.

    The run log went straight from "entering the event loop" to "the event loop
    returned" with nothing between, so a close taking twenty seconds and one
    taking two were indistinguishable - which is why this was guesswork for a
    week. Asserted on the source because the alternative is building a window
    and closing it, and closing a window is what crashes the test harness.
    """
    import ast
    import inspect
    import textwrap

    from app.ui import shell

    # `textwrap.dedent` because a method's source arrives indented and is not a
    # parsable module on its own.
    source = textwrap.dedent(inspect.getsource(shell.MainWindow.closeEvent))
    body = ast.unparse(ast.parse(source))
    assert "monotonic" in body, "closeEvent does not time itself"
    assert "closing:" in body, "closeEvent logs nothing a reader could search for"


# ---------------------------------------------------------------------------
# "the code tab is screwed it does not have a search box or nothing"
# ---------------------------------------------------------------------------

def test_the_code_tab_keeps_its_search_box_when_there_are_no_repositories():
    r"""**The box is never hidden. That is the whole rule.**

    `_show_state` used to call `self.input.setVisible(False)` whenever
    `repos_list` came back empty - which happens both when there are genuinely
    no repositories *and* when the query fails, because that worker's failure is
    swallowed deliberately. Either way the page lost the one control it exists
    for and said nothing.

    Driven through `paint_repo_state` with a stand-in rather than a real
    `CodeView`, so it is the decision under test and not Qt.
    """
    from app.ui.widgets.git_tree import paint_repo_state

    class Control:
        def __init__(self) -> None:
            self.visible = True
            self.enabled = True
            self.tip = ""
            self.text_value = ""

        def setVisible(self, on):    # noqa: N802 - Qt's name
            self.visible = bool(on)

        def setEnabled(self, on):    # noqa: N802
            self.enabled = bool(on)

        def setToolTip(self, text):  # noqa: N802
            self.tip = str(text)

        def setText(self, text):     # noqa: N802
            self.text_value = str(text)

    class Page:
        def __init__(self) -> None:
            self.input = Control()
            self.results = Control()
            self.git_split = Control()
            self.empty = Control()
            self.git_button = Control()
            self.run_button = Control()
            self.summary = Control()
            self._run_hint = "run it"

    page = Page()
    paint_repo_state(page, has_repos=False, message="Nothing is indexed yet.")

    assert page.input.visible, "the search box was hidden again"
    assert page.empty.visible and page.empty.text_value, "and nothing explains why"
    assert not page.git_button.enabled, "a tree button with no tree to show"
    assert "no repositories" in page.git_button.tip.lower(), (
        "disabled with no reason attached is the same silence in a new place")

    # And the ordinary case still shows the list.
    page = Page()
    paint_repo_state(page, has_repos=True, message="")
    assert page.input.visible and page.git_split.visible
    assert page.git_button.enabled and page.run_button.tip == "run it"


# ---------------------------------------------------------------------------
# "i rest the index the index size remained the same"
# ---------------------------------------------------------------------------

def test_clearing_the_index_gives_the_disk_space_back(tmp_path):
    r"""**Measured across a real reset, not asserted against the SQL.**

    A test checking that `clear_index` calls `VACUUM` would have passed against
    the broken version: it did call `VACUUM`. What it did not do was checkpoint
    the write-ahead log, so on a database of any size the deletions sat in
    `knowledge.db-wal` and the file group was no smaller afterwards - which is
    exactly what "the index size remained the same" describes.

    So this weighs the files.
    """
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        for number in range(400):
            file_id = store.upsert_file(
                rf"D:\corpus\file{number}.txt", size_bytes=4096, mtime_ns=number,
                ext="txt", parent_dir=r"D:\corpus")
            store.replace_chunks(file_id, [
                {"ordinal": ordinal, "text": f"passage {number}-{ordinal} " + "x" * 900,
                 "page": None, "char_start": 0, "char_end": 900}
                for ordinal in range(6)
            ])
            store.mark_indexed(file_id)

        before = store.file_bytes()
        assert before > 200_000, "the fixture is too small to prove anything"

        removed = store.clear_index()
        after = store.file_bytes()

    assert removed == 400
    assert after < before / 2, (
        f"the index went from {before:,} to {after:,} bytes after being cleared - "
        f"the disk space did not come back")


def test_a_reset_that_frees_nothing_says_so_rather_than_claiming_success():
    """Three outcomes, three sentences - and the one that matters is the middle.

    "Index cleared - 12,345 documents removed" while the folder is exactly as
    large as it was is the message that produced this report in the first place.
    """
    from app.ui.presenter import cleared_message

    empty = cleared_message({"removed": 0, "freed": 0})
    assert "already empty" in empty, f"a reset of nothing reads as a failure: {empty}"

    stuck = cleared_message({"removed": 1200, "freed": 0})
    assert "not come back" in stuck and "reset again" in stuck, (
        f"claims success while freeing nothing: {stuck}")

    ordinary = cleared_message({"removed": 1200, "freed": 3 * 1024 ** 3})
    assert "3.0 GB" in ordinary and "1,200" in ordinary, ordinary


# ---------------------------------------------------------------------------
# "there needs to be a button to clear logs"
# ---------------------------------------------------------------------------

def test_clearing_the_logs_removes_logs_and_nothing_else(tmp_path):
    """An allow-list, and the reason is that LOG_PATH is a folder somebody
    chooses. `README.txt` describes the layout and is not a log; anything else
    that has found its way in is not ours to delete."""
    from app.ui.presenter import clear_logs

    (tmp_path / "app").mkdir()
    (tmp_path / "runs").mkdir()
    (tmp_path / "app" / "app_2026-08-01.log").write_text("x" * 5000)
    (tmp_path / "runs" / "run-1-window.log").write_text("y" * 3000)
    (tmp_path / "README.txt").write_text("what each folder is for")
    (tmp_path / "notes.docx").write_bytes(b"not ours")

    outcome = clear_logs(tmp_path)

    assert outcome["removed"] == 2
    assert outcome["freed"] == 8000
    assert (tmp_path / "README.txt").exists(), "deleted the folder's own README"
    assert (tmp_path / "notes.docx").exists(), "deleted something that is not a log"


def test_the_file_this_session_is_writing_to_survives(tmp_path):
    r"""**Not caution: correctness.**

    On Windows the current run log cannot be unlinked at all, and on POSIX it
    can - leaving loguru writing into a file with no directory entry, so the
    session's own logging silently goes nowhere from the moment the button is
    pressed. Either way the file has to be named and kept.
    """
    from app.ui.presenter import clear_logs, logs_cleared_message

    (tmp_path / "app").mkdir()
    live = tmp_path / "app" / "app_today.log"
    live.write_text("still being written")
    (tmp_path / "app" / "app_old.log").write_text("finished")

    outcome = clear_logs(tmp_path, keep=[live])

    assert live.exists(), "deleted the log this session is writing to"
    assert outcome["removed"] == 1 and outcome["kept"] == 1
    assert "still writing to" in logs_cleared_message(outcome)


def test_a_log_that_will_not_delete_is_reported_with_a_way_out(tmp_path):
    """The project rule again: a failure names the action. A locked file is
    almost always another program holding it, which is fixable, and saying so
    beats a count that quietly does not add up."""
    from app.ui.presenter import logs_cleared_message

    said = logs_cleared_message(
        {"removed": 3, "freed": 100, "failed": ["app_today.log (in use)"], "kept": 0})

    assert "could not be removed" in said
    assert "close it and clear again" in said, f"no action offered: {said}"


# ---------------------------------------------------------------------------
# "change model has only one model"
# ---------------------------------------------------------------------------

def test_the_model_list_is_never_one_line_with_no_explanation():
    r"""The list was *correct* - one model was installed - and still wrong.

    A dropdown with a single row and nothing beside it cannot be told apart from
    a probe that failed. The suggestions are shown unselectable, carrying the
    command that would install them, which is how embedding models are already
    handled on this same control.
    """
    from app.llm.models import with_suggestions

    offered = with_suggestions(["qwen2.5:1.5b"])

    assert len(offered) > 1, "one row, still, and still nothing saying why"
    installed = [one for one in offered if one.selectable]
    assert [one.name for one in installed] == ["qwen2.5:1.5b"]

    for suggestion in offered[1:]:
        assert not suggestion.selectable, "offered a model that is not installed"
        assert "ollama pull" in suggestion.label, (
            f"named a model without saying how to get it: {suggestion.label}")


def test_a_model_already_installed_is_not_offered_again():
    """`qwen2.5:1.5b` pulled as `qwen2.5:1.5b-instruct-q4_0` is the same model.
    Telling somebody to install what they have is worse than saying nothing."""
    from app.llm.models import with_suggestions

    names = [one.name for one in with_suggestions(["qwen2.5:1.5b-instruct-q4_0"])]

    assert names.count("qwen2.5:1.5b") == 0, "offered to pull a model already there"
