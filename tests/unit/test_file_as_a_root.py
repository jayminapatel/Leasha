r"""One file as an entry in Folders to index. 2026-10-03.

Layer: L3 (the walk, the scan, the archive record, the watch) and L5 (the list).

The owner, of the folder list: "can it be file to index" - one archive out of
a folder of them, without taking the folder. A file entry is walked as a
listing of its own folder that names only it, so every rule a file meets
inside a folder applies to it exactly: the exclusions, the extension table,
the size ceiling, name-only, placeholders. What is checked here:

* the walk yields that file and nothing else from its folder, and a missing
  file is "not found" like a missing folder;
* the scan estimate counts it as one file, not as an unreadable folder;
* an archive record's "files under" test says the file is under itself;
* the folder watch leaves it out, saying so;
* the list's "Add file…" button adds it as a line like any other, with its
  own "Index now".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.index.walker import WalkConfig, walk

TEXT = frozenset({".txt", ".md", ".pst"})


def _tree(tmp_path: Path) -> Path:
    folder = tmp_path / "archive"
    folder.mkdir()
    for name in ("2009.pst", "2010.pst", "notes.txt"):
        (folder / name).write_text(f"the contents of {name}", encoding="utf-8")
    (folder / "deeper").mkdir()
    (folder / "deeper" / "more.txt").write_text("deeper", encoding="utf-8")
    return folder


class TestTheWalk:

    def test_a_file_root_yields_that_file_and_nothing_else(self, tmp_path):
        folder = _tree(tmp_path)
        found = list(walk(WalkConfig(roots=[folder / "2009.pst"], extensions=TEXT)))
        assert [c.path for c in found] == [folder / "2009.pst"]
        assert found[0].readable and found[0].size_bytes > 0

    def test_a_file_root_beside_its_folder_is_counted_once(self, tmp_path):
        folder = _tree(tmp_path)
        found = list(walk(WalkConfig(roots=[folder / "2009.pst", folder], extensions=TEXT)))
        assert sorted(c.path.name for c in found) == ["2009.pst", "2010.pst", "more.txt", "notes.txt"]

    def test_a_missing_file_root_is_not_found_like_a_missing_folder(self, tmp_path):
        folder = _tree(tmp_path)
        config = WalkConfig(roots=[folder / "gone.pst", folder / "2009.pst"], extensions=TEXT)
        assert [c.path.name for c in walk(config)] == ["2009.pst"]
        assert config.root_problems == {str(folder / "gone.pst"): "not found"}

    def test_the_same_rules_apply_to_it_as_inside_a_folder(self, tmp_path):
        folder = _tree(tmp_path)
        (folder / "~$lock.txt").write_text("a lock file", encoding="utf-8")
        locked = WalkConfig(roots=[folder / "~$lock.txt"], extensions=TEXT, name_only=False)
        assert list(walk(locked)) == [], "an excluded name was let in as a root"
        other = WalkConfig(roots=[folder / "2009.pst"], extensions=frozenset({".txt"}),
                           name_only=False)
        assert list(walk(other)) == [], "an extension nothing reads was let in as a root"

    def test_a_file_root_marked_first_comes_first(self, tmp_path):
        folder = _tree(tmp_path)
        config = WalkConfig(roots=[folder], extensions=TEXT,
                            priority_roots=[folder / "2010.pst"])
        by_name = {c.path.name: c.priority for c in walk(config)}
        assert by_name["2010.pst"] == 0 and by_name["2009.pst"] == 100


class TestWhatElseReadsARoot:

    def test_the_scan_counts_a_file_root_as_one_file(self, tmp_path):
        from app.index.scan import ScanConfig, scan

        folder = _tree(tmp_path)
        result = scan(ScanConfig(roots=[folder / "2009.pst"], sample_pdfs=0, sample_archives=0))
        assert result.total.files == 1
        assert result.unreadable_dirs == 0, "the file was taken for a folder that could not be read"
        assert result.missing_roots == ()

    def test_a_file_is_under_itself_for_the_archive_record(self, tmp_path):
        from app.index.archives import files_under, normalise

        folder = _tree(tmp_path)
        archive = folder / "2009.pst"
        assert files_under(archive, [archive]) == normalise(archive)
        assert files_under(folder / "2010.pst", [archive]) is None
        assert files_under(folder / "deeper" / "more.txt", [folder]) == normalise(folder)

    def test_the_folder_watch_leaves_a_file_out(self, tmp_path):
        from app.index.folder_watch import live_roots

        folder = _tree(tmp_path)
        assert live_roots([folder, folder / "2009.pst"]) == [folder]
        assert live_roots([folder / "gone.pst"]) == [folder / "gone.pst"], (
            "a file that is not there is treated as the missing folder it may be")


pytest.importorskip("PySide6")


@pytest.mark.gui
class TestTheList:

    def test_add_file_puts_the_file_on_its_own_line(self, qtbot, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QPushButton

        from app.ui.widgets import roots_box as module

        folder = _tree(tmp_path)
        chosen = str(folder / "2009.pst")
        monkeypatch.setattr(module.QFileDialog, "getOpenFileName",
                            classmethod(lambda cls, *a, **k: (chosen, "")))
        box = module.RootsBox()
        qtbot.addWidget(box)
        box.set_roots([str(folder)])
        heard: list = []
        box.roots_changed.connect(heard.append)

        box._add_file()
        assert box.current_roots() == [str(folder), chosen]
        assert heard == [[str(folder), chosen]]
        line = box.tree.topLevelItem(1)
        assert box.tree.itemWidget(line, 4).findChild(QPushButton) is not None, "no Index now"

    def test_add_file_is_a_system_button_and_cancelling_adds_nothing(self, qtbot, monkeypatch):
        from app.ui.widgets import roots_box as module
        from app.ui.widgets.buttons import BUTTONS

        assert "Add file…" in BUTTONS
        monkeypatch.setattr(module.QFileDialog, "getOpenFileName",
                            classmethod(lambda cls, *a, **k: ("", "")))
        box = module.RootsBox()
        qtbot.addWidget(box)
        box._add_file()
        assert box.current_roots() == []
