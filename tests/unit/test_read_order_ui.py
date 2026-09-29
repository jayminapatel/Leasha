r""""Index this folder first" and the reading-order control, pressed.

Layer: L5

The folder list's row action marks folders in the order they are pressed, shows
each one's place, drops a folder that is removed, and says so through one
signal; the Tuning shelf's `INDEX_ORDER` control loads and saves both values.
See `tests/unit/test_read_order.py` for what the order does to a run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

pytestmark = pytest.mark.gui


def _box(qtbot, roots, first=None):
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    box.set_roots(list(roots), first=first)
    return box


def _item(box, root):
    for row in range(box.tree.topLevelItemCount()):
        item = box.tree.topLevelItem(row)
        if item.text(0) == root:
            return item
    raise AssertionError(root)


def test_marking_folders_keeps_the_order_they_were_marked_in(qtbot) -> None:
    box = _box(qtbot, ["/a", "/b", "/c"])
    heard: list[list[str]] = []
    box.first_changed.connect(heard.append)

    box.tree.setCurrentItem(_item(box, "/c"))
    box.first.click()
    box.tree.setCurrentItem(_item(box, "/a"))
    box.first.click()

    assert box.current_first_folders() == ["/c", "/a"]
    assert heard[-1] == ["/c", "/a"]
    assert [_item(box, r).text(3) for r in ("/a", "/b", "/c")] == ["2", "", "1"]


def test_pressing_again_takes_the_mark_off(qtbot) -> None:
    box = _box(qtbot, ["/a", "/b"], first=["/b", "/a"])
    assert [_item(box, r).text(3) for r in ("/a", "/b")] == ["2", "1"]
    box.toggle_first([_item(box, "/b")])
    assert box.current_first_folders() == ["/a"]
    assert _item(box, "/a").text(3) == "1"


def test_a_removed_folder_stops_being_first(qtbot) -> None:
    box = _box(qtbot, ["/a", "/b"], first=["/a", "/b"])
    heard: list[list[str]] = []
    box.first_changed.connect(heard.append)
    box.tree.setCurrentItem(_item(box, "/a"))
    box._remove_root()
    assert box.current_first_folders() == ["/b"]
    assert heard == [["/b"]]


def test_loading_the_list_says_nothing(qtbot) -> None:
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    heard: list = []
    box.first_changed.connect(heard.append)
    box.set_roots(["/a"], first=["/a"])
    assert heard == [] and box.current_first_folders() == ["/a"]


def test_the_settings_page_relays_the_marks(qtbot) -> None:
    """`first_folders_changed` is what the shell saves; `current_first_
    folders` is what the index controller reads at Start."""
    from app.ui.settings_view import SettingsView

    view = SettingsView(SimpleNamespace(), None)
    qtbot.addWidget(view)
    view.set_roots(["/a", "/b"], first=["/b"])
    heard: list = []
    view.first_folders_changed.connect(heard.append)
    view.roots_box.toggle_first([_item(view.roots_box, "/a")])
    assert heard == [["/b", "/a"]]
    assert view.current_first_folders() == ["/b", "/a"]


def test_the_reading_order_control_loads_and_saves(qtbot) -> None:
    from app.ui.widgets.tuning_groups import StrategyBox

    box = StrategyBox(SimpleNamespace(index_order="found"))
    qtbot.addWidget(box)
    control = box.findChild(type(box.read_order), "INDEX_ORDER")
    assert control is box.read_order
    assert box.values()["INDEX_ORDER"] == "found"
    box.read_order.setCurrentIndex(box.read_order.findData("newest"))
    assert box.values()["INDEX_ORDER"] == "newest"

    fresh = StrategyBox(SimpleNamespace())
    qtbot.addWidget(fresh)
    assert fresh.values()["INDEX_ORDER"] == "newest", "the default"


@pytest.mark.parametrize("separate", [False, True])
def test_start_hands_the_marks_and_the_order_to_the_run(tmp_path, separate) -> None:
    """The window's Start: in-process, the `Pipeline` gets the marks as
    `priority_roots` and `INDEX_ORDER` as `read_order`; in a separate process,
    the child is told `--first` for each mark, in order."""
    from pathlib import Path

    import app.index.resolve as resolve_module
    from app.index.resolve import Resolved
    from tests.unit.test_start_indexing_resolves_off_thread import _pump, _window

    app, built, store, vectors = _window(tmp_path)
    handed: list = []
    built.indexing_view.start = lambda run, **kw: handed.append(run)
    built._settings = built._settings.model_copy(
        update={"index_order": "found", "index_separate_process": separate})
    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = (
        lambda settings, store: Resolved(workers=1, onnx_threads=1, embed_batch=8))
    try:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        built.settings_view.set_roots([str(a), str(b)], first=[str(b), str(a)])
        built._start_indexing(roots=[str(a), str(b)])
        _pump(app)
        assert len(handed) == 1
        run = handed[0]
        if separate:
            pairs = [run.argv[i + 1] for i, word in enumerate(run.argv)
                     if word == "--first"]
            assert pairs == [str(Path(b)), str(Path(a))]
        else:
            assert list(run.config.walk.priority_roots) == [Path(b), Path(a)]
            assert run.config.read_order == "found"
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()
