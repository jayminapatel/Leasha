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

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtGui import QImage                                  # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

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
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

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


def test_the_exits_report_the_row(qapp, files):
    """§2g: "Open the real file" and "Show in folder". 2026-10-04: they carry
    the row on show, so its drive, moment and line reach the open route."""
    png, _txt = files
    row = Row(png)
    window = PreviewWindow(row, state={})
    opened: list = []
    revealed: list = []
    window.open_requested.connect(opened.append)
    window.reveal_requested.connect(revealed.append)
    window.open_button.click()
    window.reveal_button.click()
    assert opened == [row] and revealed == [row]


def test_a_pinned_window_and_the_lightbox_open_the_row_by_the_one_route(qapp, files, monkeypatch):
    """Finding 4 (2026-10-04): both are built by `pop_out`, and both hand the
    ROW on show - after an arrow key, the sibling's - to `open_row_async`."""
    from app.ui import workers
    from app.ui.widgets import preview_window

    png, txt = files
    first, second = Row(png), Row(txt)
    asked: list = []
    monkeypatch.setattr(workers, "open_row_async",
                        lambda store, row, **kw: asked.append(
                            (store, row, kw.get("reveal", False))))
    errors: list = []
    window = preview_window.pop_out(first, store="the store", state={}, on_error=errors.append,
                                    remember=lambda _v: None, closed=lambda _w: None,
                                    siblings=[first, second], index=0)
    window.open_button.click()
    window._navigate(1)
    window.reveal_button.click()
    window.close()
    assert asked == [("the store", first, False), ("the store", second, True)]


def test_a_pinned_code_row_reads_and_opens_its_whole_path(qapp, files):
    """Finding 3: a Code row's `path` is shortened for its column; the window
    used it, and so read and opened a path that is not there."""
    from types import SimpleNamespace

    _png, txt = files
    row = SimpleNamespace(path="…" + str(txt)[-12:], full_path=str(txt), name=txt.name,
                          page=0, line_no=3)
    window = PreviewWindow(row, state={})
    assert window._path == str(txt) and window.toolTip() == str(txt)
    window.close()


def test_the_lightbox_describes_with_the_windows_settings(qapp, files, monkeypatch):
    from app.ui.widgets import preview_window

    monkeypatch.setattr(preview_window, "_DESCRIBE", dict(preview_window._DESCRIBE))
    preview_window.set_describe_options(ollama_url="http://box:1", ollama_vision_model="moondream",
                                        chat_engine="ollama")
    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    assert (window._ollama_url, window._ollama_vision_model, window._chat_engine) == (
        "http://box:1", "moondream", "ollama")
    window.close()


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


# ---------------------------------------------------------------------------
# Work order 0h §3b — the lightbox: next/previous through a result set
# ---------------------------------------------------------------------------

def test_with_no_siblings_the_arrow_keys_do_nothing(qapp, files):
    """Every pop-out before this order, and any pinned from somewhere that
    does not know its own result set - the keys must fall through unchanged,
    not raise and not silently "navigate" to nothing."""
    from PySide6.QtTest import QTest

    png, _txt = files
    window = PreviewWindow(Row(png), state={})
    title_before = window.windowTitle()
    QTest.keyClick(window, Qt.Key.Key_Right)
    assert window.windowTitle() == title_before


def test_the_right_arrow_moves_to_the_next_sibling(qapp, files):
    from PySide6.QtTest import QTest

    png, txt = files
    rows = [Row(png), Row(txt)]
    window = PreviewWindow(rows[0], state={}, siblings=rows, index=0)
    QTest.keyClick(window, Qt.Key.Key_Right)
    assert window._row is rows[1]
    assert window._path == str(txt)


def test_the_left_arrow_moves_to_the_previous_sibling_and_wraps(qapp, files):
    r"""Wrapping, not stopping - a lightbox that dead-ends at either photo
    makes somebody reach for the mouse, which is what arrow keys exist to
    save."""
    from PySide6.QtTest import QTest

    png, txt = files
    rows = [Row(png), Row(txt)]
    window = PreviewWindow(rows[0], state={}, siblings=rows, index=0)
    QTest.keyClick(window, Qt.Key.Key_Left)
    assert window._row is rows[1]                 # wrapped to the last one


def test_the_title_shows_position_only_when_it_can_navigate(qapp, files):
    png, txt = files
    alone = PreviewWindow(Row(png), state={})
    assert "of" not in alone.windowTitle()

    rows = [Row(png), Row(txt)]
    together = PreviewWindow(rows[0], state={}, siblings=rows, index=0)
    assert "(1 of 2)" in together.windowTitle()
    from PySide6.QtTest import QTest

    QTest.keyClick(together, Qt.Key.Key_Right)
    assert "(2 of 2)" in together.windowTitle()


def test_navigating_re_reads_that_sibling_own_remembered_rotation(qapp, files):
    r"""Rotation is per-file (`view_of_file.read_turn`, keyed by a hash of
    the path) - moving to a different photo must not carry the first one's
    rotation across, and must pick up whatever was remembered for the new
    one."""
    png, txt = files
    state = {rotation_key(str(txt)): "90"}
    rows = [Row(png), Row(txt)]
    window = PreviewWindow(rows[0], state=state, siblings=rows, index=0)
    assert window._view.turn == 0                 # nothing remembered for png

    from PySide6.QtTest import QTest

    QTest.keyClick(window, Qt.Key.Key_Right)
    assert window._view.turn == 90                 # picked up txt's own turn


# ---------------------------------------------------------------------------
# Describe - work order 0i section 3a. The order's own test list: "absent
# Ollama -> button greyed with reason (never an error); present -> caption
# cached, second click instant."
# ---------------------------------------------------------------------------

class _FakeStoreForDescribe:
    """`has_ai_caption`/`add_caption_chunk` only - the two calls the
    Describe button actually makes."""

    def __init__(self, *, already_described=False):
        self._described = already_described
        self.added = []

    def has_ai_caption(self, file_id, **_kwargs):
        return self._described

    def add_caption_chunk(self, file_id, caption, **_kwargs):
        self._described = True
        self.added.append((file_id, caption))


def test_describe_button_greys_with_a_reason_when_ollama_is_unreachable(qapp, files):
    r"""Never an error - a disabled button with a plain-words tooltip."""
    png, _txt = files
    row = Row(png)
    row.file_id = 42
    store = _FakeStoreForDescribe()
    window = PreviewWindow(
        row, state={}, store=store,
        ollama_url="http://127.0.0.1:1",   # nothing listens here - fast refusal
        ollama_vision_model="llava", chat_engine="ollama")

    _settle(qapp, lambda: "not running" in window.describe_button.toolTip(), tries=200)

    assert not window.describe_button.isEnabled()
    assert "not running" in window.describe_button.toolTip()


def test_describe_button_is_disabled_when_already_described(qapp, files):
    """Second click instant: the button already knows and never re-asks."""
    png, _txt = files
    row = Row(png)
    row.file_id = 43
    store = _FakeStoreForDescribe(already_described=True)
    window = PreviewWindow(
        row, state={}, store=store,
        ollama_url="http://127.0.0.1:1", ollama_vision_model="llava", chat_engine="ollama")

    _settle(qapp,
            lambda: "Already described" in window.describe_button.toolTip(),
            tries=200)

    assert not window.describe_button.isEnabled()
    assert "Already described" in window.describe_button.toolTip()


def test_describe_click_caches_the_caption_and_disables_the_button(qapp, files, monkeypatch):
    """A successful Describe writes the caption once and does not ask again."""
    from app.extract import vision_caption

    png, _txt = files
    row = Row(png)
    row.file_id = 44
    store = _FakeStoreForDescribe()

    monkeypatch.setattr(vision_caption, "available", lambda client: True)
    monkeypatch.setattr(
        vision_caption, "describe_image",
        lambda path, client, **_kw: vision_caption.VisionCaptionResult(
            caption="A dog on a beach.", model=client.model, elapsed_s=0.01))

    window = PreviewWindow(
        row, state={}, store=store,
        ollama_url="http://127.0.0.1:1", ollama_vision_model="llava", chat_engine="ollama")
    _settle(qapp, lambda: window.describe_button.isEnabled(), tries=200)
    assert window.describe_button.isEnabled()      # available, not yet described

    window._describe()
    _settle(qapp, lambda: bool(store.added), tries=200)

    assert store.added == [(44, "A dog on a beach.")]
    _settle(qapp,
            lambda: "Already described" in window.describe_button.toolTip(),
            tries=200)
    assert not window.describe_button.isEnabled()
    assert "Already described" in window.describe_button.toolTip()
