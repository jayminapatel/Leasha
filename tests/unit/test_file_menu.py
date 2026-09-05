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
