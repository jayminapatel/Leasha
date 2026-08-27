r"""A document pinned in its own window. Workspace §2.

Layer: L5.

**The regression this file exists for is 2a's whole point**: a new search in
the main window must never blank a pinned document. That is the opposite of
why it was pinned, and it is the kind of thing that works on the day it is
written and stops working the first time somebody shares a counter.

**View-only, and asserted rather than trusted.** Rotating a scan changes what
this window draws and nothing on disk — the bytes are compared before and
after. Rotation lives in the app's own state, keyed by a hash of the path.

**A copy, not a move.** The pane the button was pressed in keeps showing what
it showed, and closing the window returns nothing to re-wire.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt                                     # noqa: E402
from PyQt6.QtGui import QImage                                  # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

from app.ui.view_of_file import rotation_key                    # noqa: E402
from app.ui.widgets.preview_window import (                     # noqa: E402
    GEOMETRY_KEY, ON_TOP_KEY, TEXT_ONLY_NOTE, PreviewWindow,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def files():
    """A wide image and a text file, so both branches have something real."""
    folder = pathlib.Path(tempfile.mkdtemp())
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    png = folder / "wide.png"
    image.save(str(png))
    txt = folder / "notes.txt"
    txt.write_text("The safety report mentions safety twice.", encoding="utf-8")
    return png, txt


class Row:
    def __init__(self, path):
        self.path = str(path)
        self.name = pathlib.Path(path).name
        self.page = 0


def _settle(qapp, until, tries: int = 60) -> None:
    """Spin the loop until the worker has landed, or give up quietly."""
    for _ in range(tries):
        qapp.processEvents()
        if until():
            return
        time.sleep(0.02)


def _drawn(qapp, window):
    _settle(qapp, lambda: window.picture.pixmap() is not None
            and not window.picture.pixmap().isNull())
    return window.picture.pixmap()


# ---------------------------------------------------------------------------
# §2a — the window
# ---------------------------------------------------------------------------

def test_the_title_is_the_filename_and_the_tooltip_is_the_path(qapp, files):
    r"""A title bar cannot hold `C:\Users\...\2019\surveys\...`, and the one
    question somebody has about a pinned window is *which* copy it is."""
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    assert window.windowTitle() == "wide.png"
    assert str(png) in window.toolTip()


def test_it_is_not_parented_to_anything(qapp, files):
    r"""A parented widget with a window flag still minimises with its owner,
    and a pinned document that vanishes with the main window is not pinned."""
    png, _txt = files
    assert PreviewWindow(Row(png), state={}).parent() is None


def test_each_window_counts_its_own_renders(qapp, files):
    r"""**§2a's whole point.** A new search in the main window must never
    blank a pinned document. Two windows, two counters, and one moving does
    not move the other - a shared counter is exactly how this would break the
    first time somebody refactored it into "one place".
    """
    png, txt = files
    one = PreviewWindow(Row(png), state={})
    two = PreviewWindow(Row(txt), state={})
    before = two._generation
    one.reload()
    one.reload()
    assert two._generation == before


def test_a_stale_render_is_dropped_rather_than_drawn(qapp, files):
    """The mechanism behind that: a reply carrying an older number is one
    this window has already moved past — a second rotate pressed while the
    first was still rendering."""
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    _drawn(qapp, window)
    window._generation += 5
    window._show_card("this should be ignored", generation=1)
    assert window.card.text() != "this should be ignored"


def test_multiples_are_allowed(qapp, files):
    """Comparing two versions of a drawing falls out for free — the order
    says so, and nothing here holds a single instance."""
    png, txt = files
    windows = [PreviewWindow(Row(png), state={}),
               PreviewWindow(Row(png), state={}),
               PreviewWindow(Row(txt), state={})]
    assert len({id(window) for window in windows}) == 3


def test_a_second_window_does_not_land_exactly_on_the_first(qapp, files):
    """Pinning three documents on top of each other looks broken on the
    second use, whatever the code is doing."""
    png, _txt = files
    state = {GEOMETRY_KEY: "100,100,700,500"}
    first = PreviewWindow(Row(png), state=state)
    assert (first.geometry().x(), first.geometry().y()) != (100, 100)


def test_it_remembers_its_size_and_its_pin(qapp, files):
    png, _txt = files
    remembered: dict = {}
    window = PreviewWindow(Row(png), state={ON_TOP_KEY: "true"})
    window.remember.connect(remembered.update)
    assert window.on_top.isChecked()
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    window.on_top.setChecked(False)
    assert remembered[ON_TOP_KEY] == "false"


def test_closing_says_which_window_closed(qapp, files):
    """So the opener drops its reference rather than holding a dead widget —
    and, because multiples are allowed, it has to know *which*."""
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    closed: list = []
    window.closed.connect(closed.append)
    window.close()
    assert closed == [window]


# ---------------------------------------------------------------------------
# §2e — rotation, and the file it must not touch
# ---------------------------------------------------------------------------

def test_rotating_turns_the_picture(qapp, files):
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    window.show()
    first = _drawn(qapp, window)
    assert (first.width(), first.height()) == (200, 100)

    window._turn()
    _settle(qapp, lambda: window.picture.pixmap().width() == 100)
    assert (window.picture.pixmap().width(),
            window.picture.pixmap().height()) == (100, 200)
    window.hide()


def test_rotating_does_not_touch_the_file(qapp, files):
    r"""**§6's first rule, asserted on the bytes.** *No code path in this
    order writes to a user file.* The document is rotated in the window and
    the file on disk is compared byte for byte."""
    png, _txt = files
    before = png.read_bytes()
    window = PreviewWindow(Row(png), state={})
    window.show()
    _drawn(qapp, window)
    window._turn()
    _settle(qapp, lambda: window.picture.pixmap().width() == 100)
    window.hide()
    assert png.read_bytes() == before


def test_the_rotation_is_remembered_for_that_file(qapp, files):
    r"""*The sideways scan rotated once opens right-side-up forever* — and
    keyed by a hash, so `index_state` never holds a list of paths."""
    png, _txt = files
    remembered: dict = {}
    window = PreviewWindow(Row(png), state={})
    window.remember.connect(remembered.update)
    window._turn()
    assert remembered[rotation_key(str(png))] == "90"


def test_a_remembered_rotation_is_applied_when_it_opens(qapp, files):
    png, _txt = files
    window = PreviewWindow(Row(png), state={rotation_key(str(png)): "180"})
    assert window._view.turn == 180


# ---------------------------------------------------------------------------
# §2d — zoom
# ---------------------------------------------------------------------------

def test_zooming_a_picture_asks_for_a_bigger_render(qapp, files):
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    window.show()
    _drawn(qapp, window)
    window._zoom(out=False)
    _settle(qapp, lambda: window.picture.pixmap().width() > 200)
    assert window.picture.pixmap().width() > 200
    window.hide()


def test_zooming_text_moves_the_font_rather_than_scaling_it(qapp, files):
    r"""**§2d's own instruction**, and the reason: scaled text is blurry
    text, and what somebody wants from "bigger" in a document is more
    readable, not larger pixels."""
    _png, txt = files
    window = PreviewWindow(Row(txt), state={})
    _settle(qapp, lambda: bool(window.text.toPlainText()))
    before = window.text.font().pointSize()
    window._zoom(out=False)
    assert window.text.font().pointSize() > before
    window._zoom(out=True)
    assert window.text.font().pointSize() == before


def test_ctrl_wheel_zooms_and_a_plain_wheel_does_not(qapp, files):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent

    _png, txt = files
    window = PreviewWindow(Row(txt), state={})
    _settle(qapp, lambda: bool(window.text.toPlainText()))
    before = window.text.font().pointSize()

    def wheel(modifier):
        return QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, modifier, Qt.ScrollPhase.NoScrollPhase,
            False)

    window.wheelEvent(wheel(Qt.KeyboardModifier.NoModifier))
    assert window.text.font().pointSize() == before, "a plain wheel zoomed"
    window.wheelEvent(wheel(Qt.KeyboardModifier.ControlModifier))
    assert window.text.font().pointSize() > before


def test_rotate_and_fit_are_off_for_a_wall_of_text(qapp, files):
    """They mean nothing to it, and a control that does nothing is worse
    than one that is visibly unavailable."""
    _png, txt = files
    window = PreviewWindow(Row(txt), state={})
    _settle(qapp, lambda: bool(window.text.toPlainText()))
    assert not window.rotate.isEnabled()
    assert not window.fit.isEnabled()


# ---------------------------------------------------------------------------
# §2f / §2g — copying out, and the exits
# ---------------------------------------------------------------------------

def test_the_text_can_be_selected_and_copied(qapp, files):
    r"""§2f. **View-only still means copy-out works** - a preview you cannot
    quote from sends people to open the file for one sentence."""
    _png, txt = files
    window = PreviewWindow(Row(txt), state={})
    flags = window.text.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard


def test_the_exits_report_the_path(qapp, files):
    """§2g: "Open the real file" and "Show in folder", both routed through
    the window's existing worker-backed open paths."""
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    opened: list = []
    revealed: list = []
    window.open_requested.connect(opened.append)
    window.reveal_requested.connect(revealed.append)
    window.open_button.click()
    window.reveal_button.click()
    assert opened == [str(png)] and revealed == [str(png)]


def test_a_document_shown_as_text_says_so(qapp):
    r"""§2g's sentence. Somebody looking at a Word document with no layout
    needs to know it is this window's limitation and not the document's -
    otherwise the natural conclusion is that the file is damaged."""
    assert "open the file" in TEXT_ONLY_NOTE.lower()
    assert "layout" in TEXT_ONLY_NOTE.lower()


def test_the_sentence_is_flagged_not_parsed_out_of_a_notice():
    r"""**The rule this codebase set for notices**: nothing reads a message
    string to decide anything. `_extracted` sets `meta["extracted"]`, and the
    window reads the flag."""
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "preview_loader.py").read_text(encoding="utf-8")
    assert '"extracted": True' in source


def test_a_plain_text_file_is_not_told_it_is_missing_a_layout(qapp, files):
    """A `.txt` shown as text has no layout to be missing."""
    _png, txt = files
    window = PreviewWindow(Row(txt), state={})
    _settle(qapp, lambda: bool(window.text.toPlainText()))
    assert not window.note.isVisible()


def test_every_control_states_its_effect(qapp, files):
    r"""§6's rule for this whole order, and §6a's test: *every new control's
    tooltip states its effect.*"""
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    for control in (window.rotate, window.zoom_in, window.zoom_out,
                    window.fit, window.print_button, window.open_button,
                    window.reveal_button, window.on_top):
        assert control.toolTip().strip(), f"{control.text()} has no tooltip"
        assert "_" not in control.toolTip(), "jargon in a tooltip"


# ---------------------------------------------------------------------------
# §2h — mail
# ---------------------------------------------------------------------------

def test_a_message_pops_out_like_a_file(qapp):
    r"""§2h: *the same window, no special casing beyond the synthetic-path
    load that already exists.* A mail row has no file on disk, so the pane's
    `body_provider` travels with it - which is why the signal carries two
    things rather than one."""
    class MailRow:
        path = "C:/archive/mail.pst/12345"
        name = "Re: the site survey"
        page = 0

    window = PreviewWindow(MailRow(), state={},
                           body_provider=lambda _row: "The survey is attached.")
    _settle(qapp, lambda: bool(window.text.toPlainText()))
    assert "survey" in window.text.toPlainText()
    assert window.windowTitle() == "Re: the site survey"


def test_the_pane_hands_its_provider_over_with_the_row(qapp):
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.body_provider = lambda _row: "body"
    seen: list = []
    pane.pop_out_requested.connect(lambda row, provider: seen.append(provider))
    pane.show_row(Row("C:/x/a.txt"))
    pane.pop_button.click()
    assert seen and seen[0] is pane.body_provider


def test_the_pane_keeps_showing_what_it_was_showing(qapp, files):
    r"""**A copy, not a move.** Pressing the button must not empty the pane
    it was pressed in - that is the design the order sets out and the reason
    closing a pop-out returns nothing to re-wire."""
    from app.ui.widgets.preview import PreviewPane

    _png, txt = files
    pane = PreviewPane()
    pane.show_row(Row(txt))
    title_before = pane.title.text()
    pane.pop_button.click()
    assert pane.title.text() == title_before
    assert pane._row is not None


def test_nothing_in_the_window_opens_a_file_for_writing():
    r"""§6, as a guard rather than as a promise. The window reads, renders and
    prints; it never writes."""
    source = (pathlib.Path(__file__).resolve().parents[2] / "app" / "ui"
              / "widgets" / "preview_window.py").read_text(encoding="utf-8")
    for writing in ("write_text(", "write_bytes(", "shutil.", "os.remove",
                    "unlink(", '"w"', "'w'"):
        assert writing not in source, f"preview_window.py has {writing}"
