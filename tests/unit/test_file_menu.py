"""Every context-menu action must actually run when clicked.

Layer: L5

`QAction.triggered` emits a checked boolean, and each menu item's callback is
connected through a lambda. Construction alone proves nothing: a signature
mismatch between the signal and the callback only surfaces at *trigger* time,
when a user clicks the item - the one moment no constructor test covers. So
these tests trigger every action through Qt's real signal dispatch and assert
the callback ran.

(For the record: PyQt truncates signal arguments to the callable's arity, so a
zero-argument lambda on `triggered` is legal. These tests pin that behaviour
so a future port - PySide, a Qt major bump, a refactor to bound methods -
cannot silently break every menu item at once.)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# exc_type=ImportError: pytest 8 only auto-skips ModuleNotFoundError, but a
# PyQt6 that is installed yet cannot load (a headless machine missing libEGL)
# raises plain ImportError - and that environment cannot run these tests
# either, so it must skip, not error.
pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.ui.widgets import file_menu  # noqa: E402
from app.ui.widgets.file_menu import FileActions, build_menu  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def copied(monkeypatch):
    """Capture clipboard writes; the offscreen platform's clipboard is not
    what these tests are about."""
    texts: list[str] = []
    monkeypatch.setattr(file_menu, "_copy", texts.append)
    return texts


def _action(menu, label: str):
    for action in menu.actions():
        if action.text() == label:
            return action
    raise AssertionError(
        f"no action labelled {label!r}; menu has {[a.text() for a in menu.actions()]}"
    )


def test_every_action_fires_its_callback_when_triggered(qapp, tmp_path: Path, copied):
    target = tmp_path / "report.docx"
    target.write_text("x", encoding="utf-8")

    fired: list[str] = []
    # **The parent must be held in a local.** `build_menu(QWidget(), ...)`
    # passes a temporary with no Python reference: it is collected as soon as
    # the call returns, Qt deletes its children with it, and the menu becomes
    # "wrapped C/C++ object of type QMenu has been deleted" on the next line.
    #
    # A test artefact, not a product bug - in the window the parent is a
    # long-lived table - but it is the same ownership trap that produced a real
    # one in `view_options`, and a test that cannot run is a guard that is not
    # guarding.
    parent = QWidget()
    menu = build_menu(
        parent,
        str(target),
        FileActions(
            open_file=lambda: fired.append("open"),
            reveal=lambda: fired.append("reveal"),
            search_inside=lambda: fired.append("search_inside"),
            reindex=lambda: fired.append("reindex"),
        ),
    )

    for label in ("Open", "Show in folder", "Search inside this file"):
        action = _action(menu, label)
        assert action.isEnabled(), f"{label} should be enabled for an existing file"
        action.trigger()          # real signal dispatch: triggered(bool) -> lambda

    assert fired == ["open", "reveal", "search_inside"]

    _action(menu, "Copy path").trigger()
    _action(menu, "Copy file name").trigger()
    assert copied == [str(target), "report.docx"]


def test_pin_action_fires_whatever_the_files_state(qapp, tmp_path: Path, copied):
    """Workspace §3c: offered whatever the file's own state - even a missing
    file is worth keeping track of, which "Open" is not."""
    gone = tmp_path / "vanished.pdf"

    fired: list[str] = []
    parent = QWidget()
    menu = build_menu(parent, str(gone), FileActions(pin=lambda: fired.append("pin")))

    pin = _action(menu, "Pin")
    assert pin.isEnabled(), "pinning a result does not depend on the file existing"
    pin.trigger()
    assert fired == ["pin"]


def test_no_pin_action_when_not_offered(qapp, tmp_path: Path):
    parent = QWidget()
    menu = build_menu(parent, str(tmp_path / "a.pdf"), FileActions())
    assert all(action.text() != "Pin" for action in menu.actions())


def test_more_like_this_fires_whatever_the_files_state(qapp, tmp_path: Path):
    r"""Work order 0h §2d. A vector neighbour search does not care whether
    the file is still on disk - same reasoning as "Pin", offered whatever
    `_exists` says."""
    gone = tmp_path / "vanished.jpg"

    fired: list[str] = []
    parent = QWidget()
    menu = build_menu(
        parent, str(gone), FileActions(similar=lambda: fired.append("similar")))

    action = _action(menu, "More like this")
    assert action.isEnabled()
    action.trigger()
    assert fired == ["similar"]


def test_no_more_like_this_action_when_not_offered(qapp, tmp_path: Path):
    parent = QWidget()
    menu = build_menu(parent, str(tmp_path / "a.pdf"), FileActions())
    assert all(action.text() != "More like this" for action in menu.actions())


def test_missing_file_offers_reindex_and_it_fires(qapp, tmp_path: Path, copied):
    gone = tmp_path / "vanished.pdf"

    fired: list[str] = []
    # **The parent must be held in a local.** `build_menu(QWidget(), ...)`
    # passes a temporary with no Python reference: it is collected as soon as
    # the call returns, Qt deletes its children with it, and the menu becomes
    # "wrapped C/C++ object of type QMenu has been deleted" on the next line.
    #
    # A test artefact, not a product bug - in the window the parent is a
    # long-lived table - but it is the same ownership trap that produced a real
    # one in `view_options`, and a test that cannot run is a guard that is not
    # guarding.
    parent = QWidget()
    menu = build_menu(
        parent,
        str(gone),
        FileActions(
            open_file=lambda: fired.append("open"),
            reindex=lambda: fired.append("reindex"),
            # 2026-10-04: the list says so (decided on a worker); the menu never stats.
            missing=True,
        ),
    )

    assert not _action(menu, "Open").isEnabled(), "cannot open a missing file"

    reindex = _action(menu, "File is missing - re-index this folder")
    reindex.trigger()
    assert fired == ["reindex"], "the recovery action is the one that must work"

    # "Copy path" stays enabled for missing files - the path is the clue to
    # where the file went - and must still deliver.
    _action(menu, "Copy path").trigger()
    assert copied == [str(gone)]


def test_explicit_copy_pairs_fire_with_their_own_values(qapp, tmp_path: Path, copied):
    """The mail case: several copy actions built in a loop. The loop variable
    must be captured per-action, not shared - the classic late-binding bug
    would make every item copy the last pair's text."""
    target = tmp_path / "msg.eml"
    target.write_text("x", encoding="utf-8")

    # **The parent must be held in a local.** `build_menu(QWidget(), ...)`
    # passes a temporary with no Python reference: it is collected as soon as
    # the call returns, Qt deletes its children with it, and the menu becomes
    # "wrapped C/C++ object of type QMenu has been deleted" on the next line.
    #
    # A test artefact, not a product bug - in the window the parent is a
    # long-lived table - but it is the same ownership trap that produced a real
    # one in `view_options`, and a test that cannot run is a guard that is not
    # guarding.
    parent = QWidget()
    menu = build_menu(
        parent,
        str(target),
        FileActions(copy=[("Copy subject", "HACCP review"), ("Copy sender", "a@b.c")]),
    )

    _action(menu, "Copy subject").trigger()
    _action(menu, "Copy sender").trigger()
    assert copied == ["HACCP review", "a@b.c"]


# -- 2026-10-04: one rule with the open route, and never a stat on this thread --

@pytest.mark.parametrize("path", [
    "pst://2024/2097188/attachments/Model CED.xlsm",     # an email attachment
    "D:/Docs/backup.zip/q3/report.docx",                 # a file inside a zip
    "pst://2024/2097188",                                # a message (Outlook)
    "leasha-volume://3/Holiday/beach.jpg",               # a catalogued drive's file
])
def test_open_and_show_in_folder_are_offered_for_everything_the_route_opens(
        qapp, path, monkeypatch):
    """Finding 1: each of these was greyed out because the menu statted the
    path - and Search offered "File is missing" for a file that opens."""
    def no_stat(*_a, **_k):
        raise AssertionError("the menu statted a path on the interface thread")

    monkeypatch.setattr(Path, "exists", no_stat)
    parent = QWidget()
    menu = build_menu(parent, path, FileActions(
        open_file=lambda: None, reveal=lambda: None, reindex=lambda: None))
    assert _action(menu, "Open").isEnabled() and _action(menu, "Show in folder").isEnabled()
    assert all("missing" not in action.text() for action in menu.actions())


def test_a_drive_that_is_out_greys_open_and_offers_no_reindex(qapp):
    from types import SimpleNamespace

    parent = QWidget()
    for actions in (
        FileActions(open_file=lambda: None, reveal=lambda: None, reindex=lambda: None,
                    offline=True),                                      # Search's mark
        FileActions(open_file=lambda: None, reveal=lambda: None, reindex=lambda: None,
                    row=SimpleNamespace(path="leasha-volume://3/a.jpg", status="Offline")),
    ):
        menu = build_menu(parent, "leasha-volume://3/a.jpg", actions)
        assert not _action(menu, "Open").isEnabled()
        assert not _action(menu, "Show in folder").isEnabled()
        assert all("missing" not in action.text() for action in menu.actions())


def test_copy_path_on_a_catalogued_drive_copies_its_real_path(qapp, monkeypatch):
    """Finding 9: Search and Files copied the internal `leasha-volume://` key."""
    from types import SimpleNamespace

    from app.ui import workers

    asked = []
    monkeypatch.setattr(workers, "copy_path_async", lambda row, **_k: asked.append(row))
    row = SimpleNamespace(path="leasha-volume://3/a.jpg", volume_id=3)
    parent = QWidget()
    menu = build_menu(parent, row.path, FileActions(row=row))
    _action(menu, "Copy path").trigger()
    assert asked == [row]


def test_copy_path_resolves_on_a_worker_and_copies_the_key_when_the_drive_is_out(
        qapp, qtbot, monkeypatch):
    from types import SimpleNamespace

    from app.ui import tasks, workers

    row = SimpleNamespace(path="leasha-volume://3/a.jpg", volume_id=3)
    monkeypatch.setattr(tasks, "resolve_open_path", lambda store, r: r"E:\a.jpg")
    workers.copy_path_async(row, store=object())
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == r"E:\a.jpg", timeout=5000)

    def offline(store, r):
        raise RuntimeError("not plugged in")

    monkeypatch.setattr(tasks, "resolve_open_path", offline)
    workers.copy_path_async(row, store=object())
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == row.path, timeout=5000)


def test_the_photo_grids_open_opens_the_file_and_view_opens_the_lightbox(qapp, monkeypatch):
    """Finding 8: the grid's "Open" opened the lightbox, unlike every other list."""
    from types import SimpleNamespace

    from app.ui.widgets import thumbnail_grid

    menus = []
    monkeypatch.setattr(thumbnail_grid, "show_for", lambda *a: menus.append(a[3]))
    grid = thumbnail_grid.ThumbnailGrid()
    row = SimpleNamespace(path="D:/p/beach.jpg", ext="jpg", chunk_id=1, file_id=1)
    grid._rows = [row]
    monkeypatch.setattr(grid._list, "itemAt", lambda _p: None)
    monkeypatch.setattr(grid._list, "selectedItems", lambda: [object()])
    monkeypatch.setattr(grid, "_row_at", lambda _item: row)
    files, lightbox = [], []
    grid.file_requested.connect(files.append)
    grid.opened.connect(lambda r, siblings: lightbox.append(r))
    from PyQt6.QtCore import QPoint

    grid._on_context_menu(QPoint(1, 1))
    menus[0].open_file()
    menus[0].view()
    assert files == [row] and lightbox == [row]
