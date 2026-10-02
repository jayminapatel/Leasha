r"""An index run that looked at nothing must say so.

On 2026-08-27 the owner started an index run from the window. It took six
minutes, reported success, and its stats read `seen: 0` - it walked no files
whatsoever. The six minutes were spent embedding a backlog of 256 vectors
left by an earlier run; the walk itself found nothing.

Nothing said so. `notices` was empty, the run log's only line was the summary
dictionary, and the Indexing page showed zeroes with no explanation. The
reasonable conclusion from the outside was "it did not index my mail", which
is the report that arrived.

Two things are fixed, and the second turned out to be the cause.

1. **Leasha now says which of the three situations it is in** when a run walks
   nothing, so nobody has to read a JSON-ish log line to find out.
2. **A folder that cannot be walked is named.** `walker.walk` skipped a root
   that does not exist with a bare `continue` - no log, no count, no notice.
   One disconnected drive or one renamed folder removed the whole corpus from
   the run, and the run still reported success. That is almost certainly what
   happened to the 30GB of mail.
"""

from __future__ import annotations

from app.index.pipeline import IndexStats, Pipeline


class _Log:
    """Records what the pipeline said, at what level."""

    def __init__(self) -> None:
        self.said: list = []

    def warning(self, template, *args) -> None:
        self.said.append(("warning", template.format(*args) if args else template))

    def debug(self, template, *args) -> None:
        self.said.append(("debug", template.format(*args) if args else template))


class _Pipeline:
    """Just enough of a pipeline for the method under test."""

    def __init__(self, roots) -> None:
        walk = type("Walk", (), {})()
        walk.roots = list(roots)
        self.config = type("Config", (), {})()
        self.config.walk = walk
        self._log = _Log()


def _run(roots, *, skipped: int = 0, seen: int = 0, indexed: int = 0,
         unchanged: int = 0) -> tuple:
    pipeline = _Pipeline(roots)
    stats = IndexStats()
    stats.seen, stats.indexed, stats.unchanged = seen, indexed, unchanged
    stats.skipped_roots = [{"root": f"r{n}"} for n in range(skipped)]
    Pipeline._say_if_nothing_was_walked(pipeline, stats)
    return stats.notices, pipeline._log.said


class TestTheThreeWaysNothingHappens:
    """Named separately, because the fix for each is different."""

    def test_no_folders_configured_points_at_settings(self) -> None:
        r"""Roots start empty since the privacy work, so this is a new machine.

        The fix is one trip to Settings, and the notice says so rather than
        leaving somebody to guess whether Leasha is broken.
        """
        notices, _said = _run([])
        assert len(notices) == 1
        assert "No folders are set up" in notices[0]
        assert "Settings" in notices[0]

    def test_every_folder_skipped_as_archival_says_that(self) -> None:
        """Deliberate, but worth repeating when the total is nothing."""
        notices, _said = _run([], skipped=2)
        assert "archives" in notices[0]
        assert "2 folder" in notices[0]

    def test_a_folder_that_held_nothing_names_it(self) -> None:
        r"""**The case that matters most.**

        A drive that did not mount reads as an empty folder rather than an
        error, so this is where a disconnected disk ends up - and naming the
        folder is what turns "nothing was indexed" into something actionable.
        """
        notices, _said = _run([r"D:\Mail"])
        assert r"D:\Mail" in notices[0]
        assert "connected" in notices[0]

    def test_many_folders_are_summarised_not_listed_in_full(self) -> None:
        r"""A notice is one line in a status bar, not a directory listing.

        The names are deliberately distinctive: single letters looked fine and
        the first version of this test asserted `"e" not in ...`, which was
        true only because "e" is in "index". A guard that can pass on the
        wrong substring is not a guard.
        """
        notices, _said = _run(["Alpha", "Bravo", "Charlie", "Delta", "Echo"])
        assert "Alpha, Bravo, Charlie..." in notices[0]
        assert "Delta" not in notices[0]
        assert "Echo" not in notices[0]


class TestARunStoppedBeforeItsWalkSaysThat:
    r"""2026-10-02. The fourth way, and the other three are untrue of it.

    On 2026-10-01 at 23:41 a run was started straight after a Stop. It spent
    its 47 seconds filling in 512 passages the Stop had left without vectors,
    was stopped again before the walk began, and then reported "Nothing was
    found to index in D:\OutlookArchive. The folder was read and held no
    files ... check it is connected" - of a folder holding twenty archives
    that it had never opened.
    """

    def _stopped(self, roots) -> tuple:
        import threading

        pipeline = _Pipeline(roots)
        pipeline._stop = threading.Event()
        pipeline._stop.set()
        stats = IndexStats()
        Pipeline._say_if_nothing_was_walked(pipeline, stats)
        return stats.notices, pipeline._log.said

    def test_it_does_not_blame_the_folder(self) -> None:
        notices, _said = self._stopped([r"D:\OutlookArchive"])
        assert len(notices) == 1
        assert "stopped" in notices[0]
        assert "connected" not in notices[0]
        assert "held no files" not in notices[0]

    def test_it_is_still_a_warning(self) -> None:
        _notices, said = self._stopped([r"D:\OutlookArchive"])
        assert any(level == "warning" for level, _text in said)

    def test_a_run_that_was_not_stopped_is_told_what_it_always_was(self) -> None:
        import threading

        pipeline = _Pipeline([r"D:\Mail"])
        pipeline._stop = threading.Event()
        stats = IndexStats()
        Pipeline._say_if_nothing_was_walked(pipeline, stats)
        assert "connected" in stats.notices[0]


class TestARealRunStaysQuiet:
    """A notice on every successful run is a notice nobody reads."""

    def test_files_seen_means_no_notice(self) -> None:
        notices, _said = _run([r"D:\Mail"], seen=1200)
        assert notices == []

    def test_files_indexed_means_no_notice(self) -> None:
        notices, _said = _run([r"D:\Mail"], indexed=5)
        assert notices == []

    def test_everything_unchanged_means_no_notice(self) -> None:
        """A second run over a settled corpus is the normal case, not a fault."""
        notices, _said = _run([r"D:\Mail"], unchanged=1200)
        assert notices == []


class TestItIsLoudEnoughToFind:

    def test_it_is_logged_as_a_warning(self) -> None:
        r"""INFO would sit among a hundred other lines.

        A run that indexed nothing and said nothing is the report this exists
        to prevent, so it is a warning and appears in the log's error summary.
        """
        _notices, said = _run([])
        assert any(level == "warning" for level, _text in said)

    def test_a_broken_stats_object_does_not_take_the_run_down(self) -> None:
        """It runs at the end of a successful run; a notice may not undo one."""
        pipeline = _Pipeline([])
        broken = IndexStats()
        broken.seen = 0
        broken.skipped_roots = None                  # type: ignore[assignment]
        Pipeline._say_if_nothing_was_walked(pipeline, broken)   # must not raise


class TestAFolderThatCannotBeWalkedIsNamed:
    r"""The bare `continue` that hid 30GB of mail.

    Driven through the real `walk()` rather than a stub, because the whole
    defect was that a real walk said nothing - a test against a fake would
    have been written to the same silent contract.
    """

    def _walk(self, roots) -> tuple:
        from app.index.walker import WalkConfig, walk

        config = WalkConfig(roots=[str(root) for root in roots])
        found = [candidate.path for candidate in walk(config)]
        return found, config.root_problems

    def test_a_missing_root_is_recorded(self, tmp_path) -> None:
        _found, problems = self._walk([tmp_path / "not-here"])
        assert problems, "a missing folder vanished silently, exactly as before"
        assert "not found" in next(iter(problems.values()))

    def test_the_folders_that_do_exist_are_still_walked(self, tmp_path) -> None:
        """One bad folder must not cost the good ones."""
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.txt").write_text("hello", encoding="utf-8")

        found, problems = self._walk([good, tmp_path / "gone"])
        assert [path.name for path in found] == ["a.txt"]
        assert len(problems) == 1

    def test_a_healthy_walk_records_no_problems(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        _found, problems = self._walk([tmp_path])
        assert problems == {}

    def test_the_run_names_it_even_when_plenty_was_indexed(self) -> None:
        r"""**The case the empty-run notice cannot reach.**

        Three folders, one of them gone, thousands of files indexed from the
        other two: every number on the page looks healthy and the missing
        third is invisible. So this reports regardless of the counts.
        """
        pipeline = _Pipeline([])
        pipeline.config.walk.root_problems = {r"E:\Mail": "not found"}
        stats = IndexStats()
        stats.seen, stats.indexed = 9000, 9000

        Pipeline._report_root_problems(pipeline, stats)

        assert stats.notices, "a busy run hid a missing folder"
        assert r"E:\Mail" in stats.notices[0]
        assert "connect it" in stats.notices[0]
        assert stats.root_problems == {r"E:\Mail": "not found"}

    def test_an_excluded_root_is_named_differently(self) -> None:
        """A setting to change, not a folder to plug in. Different fix."""
        pipeline = _Pipeline([])
        pipeline.config.walk.root_problems = {r"D:\Logs": "excluded by a setting"}
        stats = IndexStats()

        Pipeline._report_root_problems(pipeline, stats)

        assert "excluded by a setting" in stats.notices[0]
        assert "connect it" not in stats.notices[0]

    def test_it_reaches_the_run_summary(self) -> None:
        """`as_dict` is what the run log and the Indexing page read."""
        stats = IndexStats()
        stats.root_problems = {r"E:\Mail": "not found"}
        assert stats.as_dict()["root_problems"] == {r"E:\Mail": "not found"}

    def test_a_missing_sink_does_not_raise(self) -> None:
        r"""A walk config without the new field at all.

        The stub here never sets `root_problems`, which is exactly the shape
        of an older caller - so this needs no arranging, only asserting that
        the absence is read as "nothing to report" rather than as an error.
        """
        pipeline = _Pipeline([])
        assert not hasattr(pipeline.config.walk, "root_problems")
        stats = IndexStats()
        Pipeline._report_root_problems(pipeline, stats)   # must not raise
        assert stats.notices == []
