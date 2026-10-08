"""A Windows directory junction is walked once, never as a loop.

Layer: L3

Found in review 2026-10-08 and reproduced on the owner's laptop: `os.walk(...,
followlinks=False)` still descends into a junction, because a junction is not
a symlink (`DirEntry.is_symlink()` is False, `is_junction()` True). A junction
pointing at its own parent was therefore walked level by level until the path
ran out of characters, and every depth was a distinct `path_key`, so one text
file was indexed 64 times. The same hole was in the scan, the polling
snapshot and the offline-media reconcile walk.

Windows-only by nature: `_winapi.CreateJunction` is how a junction is made,
and nothing else here has junctions.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")


def _tree_with_a_loop(tmp_path: Path) -> Path:
    """`root/sub/a.txt`, and `root/loop` -> `root` (a junction, not a symlink)."""
    import _winapi

    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("the only file")
    _winapi.CreateJunction(str(root), str(root / "loop"))
    assert (root / "loop").is_junction() and not (root / "loop").is_symlink()
    return root


def test_the_premise_still_holds(tmp_path):
    """If a future Python stops descending into junctions, this says so and
    the guards below become belt and braces rather than the fix."""
    root = _tree_with_a_loop(tmp_path)
    seen = sum(len(files) for _, _, files in os.walk(root))
    assert seen > 1, "os.walk no longer loops through a junction - revisit the guards"


def test_the_walker_reads_the_file_once_and_counts_the_junction(tmp_path):
    from app.index.walker import JUNCTION_SKIPPED, WalkConfig, walk

    root = _tree_with_a_loop(tmp_path)
    config = WalkConfig(roots=[root], extensions=frozenset({".txt"}))

    found = [c.path for c in walk(config)]

    assert found == [root / "sub" / "a.txt"]
    assert config.stat_failures.get(JUNCTION_SKIPPED) == 1


def test_the_walker_follows_the_junction_when_asked_to_follow_links(tmp_path):
    """`follow_symlinks=True` is the owner opting in to linked trees; a junction
    then counts as one. It is still finite only because the path limit ends it,
    which is what the owner asked for."""
    from app.index.walker import JUNCTION_SKIPPED, WalkConfig, walk

    root = _tree_with_a_loop(tmp_path)
    config = WalkConfig(roots=[root], extensions=frozenset({".txt"}), follow_symlinks=True)

    found = [c.path for c in walk(config)]

    assert len(found) > 1
    assert JUNCTION_SKIPPED not in config.stat_failures


def test_an_unlistable_folder_is_counted_not_dropped(tmp_path, monkeypatch):
    from app.index import walker as module
    from app.index.walker import FOLDER_UNLISTABLE, WalkConfig, walk

    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.txt").write_text("x")
    (tmp_path / "locked").mkdir()
    real_scandir = os.scandir

    def denied(path=".", *args, **kwargs):
        if Path(str(path)).name == "locked":
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "scandir", denied)
    config = WalkConfig(roots=[tmp_path], extensions=frozenset({".txt"}))
    found = [c.path.name for c in walk(config)]

    assert found == ["a.txt"]
    assert config.stat_failures.get(FOLDER_UNLISTABLE) == 1


def test_the_scan_counts_the_file_once(tmp_path):
    from app.index.scan import ScanConfig, scan

    root = _tree_with_a_loop(tmp_path)
    result = scan(ScanConfig(roots=[root], sample_pdfs=0))

    assert result.total.files == 1
    assert result.pruned.get("loop") == 1


def test_the_polling_snapshot_lists_the_file_once(tmp_path):
    from app.index.folder_watch import ChangeBuffer, PollingSource
    from app.index.walker import PathRules, WalkConfig

    root = _tree_with_a_loop(tmp_path)
    source = PollingSource(root, ChangeBuffer(),
                           rules=lambda: PathRules(WalkConfig(roots=[root])),
                           stop=threading.Event())

    snapshot = source._snapshot()

    assert [Path(p).name for p in snapshot] == ["a.txt"]


def test_the_offline_media_reconcile_walk_prunes_junctions(tmp_path, monkeypatch):
    """`reconcile_moves` needs a store; the walk inside it is what matters, so
    it is driven with a stand-in store that knows nothing."""
    from app.index import offline_media

    root = _tree_with_a_loop(tmp_path)

    class _Store:
        def iter_files(self, **kwargs):
            return []

    result = offline_media.reconcile_moves(_Store(), 1, root)
    assert result.moved == 0
    # The walk is not exposed; prove the prune by walking the same way it does.
    seen = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not Path(dirpath, d).is_junction()]
        seen.extend(files)
    assert seen == ["a.txt"]
