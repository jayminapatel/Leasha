"""The preview pane can be built, and cannot stop the window being built.

Layer: L5

Written after the pane crashed the application on launch. `QPdfView()` needs a
parent in PySide6; the missing argument raised inside `MainWindow.__init__`, so an
**optional preview that is off by default** turned into a program that would not
start at all.

Two failures, and the second is the one worth a test. The typo is trivial. The
design fault is that a component nobody had switched on was able to take the
window down - and the module's own docstring already promised the opposite, that
a PDF view which cannot be built falls back to the file card.

Nothing here needs the pane to *work*. These tests only assert it can be
constructed and survives being handed nonsense, which is precisely the gap that
let a `TypeError` reach the person running it.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QListWidget  # noqa: E402

from app.ui.widgets.preview import PreviewPane, attach_preview  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_the_pane_can_be_constructed(qapp):
    """The whole of the bug: this raised, during window construction."""
    assert PreviewPane() is not None


def test_construction_survives_a_pdf_view_that_will_not_build(qapp, monkeypatch):
    """Whatever goes wrong in the optional half, the pane still exists.

    Simulated by making the import itself fail, which is the same path any
    other failure now takes.
    """
    import builtins

    real_import = builtins.__import__

    def no_pdf(name, *args, **kwargs):
        if name.startswith("PySide6.QtPdf"):
            raise RuntimeError("simulated: this Qt build has no PDF module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pdf)

    pane = PreviewPane()

    assert pane is not None
    assert pane._pdf is None, "it must degrade to the card, not to an exception"


def test_a_pdf_with_no_viewer_shows_the_card_rather_than_failing(qapp, monkeypatch):
    from app.ui.preview_loader import KIND_PDF, Preview

    pane = PreviewPane()
    monkeypatch.setattr(pane, "_pdf", None)

    pane._rendered(
        Preview(kind=KIND_PDF, path="D:/x/report.pdf", title="report.pdf",
                subtitle="1 MB"),
        pane._generation,
    )

    assert pane.stack.currentWidget() is pane.card
    assert "report.pdf" in pane.card.text()


def test_attaching_to_a_list_returns_a_hidden_pane(qapp):
    """Off until asked for, and the splitter exists either way so the toggle is
    a repaint rather than a relayout."""
    results = QListWidget()
    results.selected = _Signal()

    pane, split = attach_preview(results, lambda _row: None, lambda _e: None)

    assert not pane.isVisible()
    assert split.count() == 2


def test_showing_a_row_does_not_read_the_file_immediately(qapp):
    """Debounced: arrowing through fifty results must queue one render, not
    fifty. The timer is the mechanism, so the timer is what is asserted."""
    pane = PreviewPane()
    pane.show_row(_Row("D:/nowhere/thing.txt"))

    assert pane._timer.isActive()
    assert pane.title.text() == "thing.txt", "the heading follows at once"


def test_clearing_stops_a_queued_render(qapp):
    pane = PreviewPane()
    pane.show_row(_Row("D:/nowhere/thing.txt"))
    pane.clear()

    assert not pane._timer.isActive()


def test_a_stale_render_is_dropped(qapp):
    """A result that lands after the selection moved on must not be painted -
    the wrong document beside the right row is worse than nothing, because it
    looks right."""
    from app.ui.preview_loader import KIND_TEXT, Preview

    pane = PreviewPane()
    pane.show_row(_Row("D:/a.txt"))
    stale = pane._generation
    pane.show_row(_Row("D:/b.txt"))

    pane._rendered(Preview(kind=KIND_TEXT, body="from the old row"), stale)

    assert "from the old row" not in pane.text.toPlainText()


def test_shutdown_is_safe_before_anything_was_shown(qapp):
    PreviewPane().shutdown()


class _Row:
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.page = 0


class _Signal:
    """The one method `attach_preview` calls on `results.selected`."""

    def connect(self, _slot) -> None:
        return None
