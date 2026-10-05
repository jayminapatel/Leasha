r"""The pinned working-set panel. Workspace §3c.

Layer: L5 widget. Adapted from `docs/_superseded/working_set.py`, swapped from
`QListWidget` (whose own drag is Qt's internal item format, not a file) to a
`QListView` over `DraggableResultsModel` - see `pinned_panel.py`'s own
docstring for why that swap, not the original, is what makes "drag the lot
into an email" actually true.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                     # noqa: E402

from app.ui import pinned                                     # noqa: E402
from app.ui.widgets.pinned_panel import PinnedPanel, enabled_checkbox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def row(path: str, name: str = ""):
    return SimpleNamespace(path=path, name=name)


def test_pinning_a_row_shows_up_in_the_list_and_the_summary(qapp):
    panel = PinnedPanel()
    panel.pin(row(r"D:\a.pdf"))
    assert panel.pins == (pinned.Pin(r"D:\a.pdf"),)
    assert panel.list.model().rowCount() == 1
    assert "1 document" in panel.summary.text()


def test_pinning_the_same_row_twice_does_not_duplicate_it(qapp):
    panel = PinnedPanel()
    panel.pin(row(r"D:\a.pdf"))
    panel.pin(row(r"D:\a.pdf"))
    assert len(panel.pins) == 1


def test_clear_is_the_only_way_the_set_empties(qapp):
    """§3c is emphatic: never by a new search. A panel has no notion of a
    search at all, which is the property under test - clear is explicit."""
    panel = PinnedPanel()
    panel.pin(row(r"D:\a.pdf"))
    panel.pin(row(r"D:\b.pdf"))
    panel.clear()
    assert panel.pins == ()
    assert panel.list.model().rowCount() == 0


def test_restore_reads_back_what_pin_persisted(qapp):
    saver = PinnedPanel()
    saver.pin(row(r"D:\a.pdf", "A"))
    remembered = {}
    saver.remember.connect(lambda values: remembered.update(values))
    saver.pin(row(r"D:\b.pdf"))

    loader = PinnedPanel()
    loader.restore(remembered)
    assert loader.pins == saver.pins


def test_restore_of_garbage_state_is_empty_not_a_crash(qapp):
    panel = PinnedPanel()
    panel.restore({"ui:pinned_results": "not json"})
    assert panel.pins == ()


def test_open_all_emits_every_path(qapp):
    panel = PinnedPanel()
    panel.pin(row(r"D:\a.pdf"))
    panel.pin(row(r"D:\b.pdf"))
    opened: list = []
    panel.open_all.connect(opened.append)
    panel.open_button.click()
    assert opened == [[r"D:\a.pdf", r"D:\b.pdf"]]


def test_open_all_does_nothing_when_the_set_is_empty(qapp):
    panel = PinnedPanel()
    opened: list = []
    panel.open_all.connect(opened.append)
    panel.open_button.click()
    assert opened == []


def test_copy_puts_one_path_per_line_on_the_clipboard(qapp, monkeypatch):
    panel = PinnedPanel()
    panel.pin(row(r"D:\a.pdf"))
    panel.pin(row(r"D:\b.pdf"))

    copied: list = []
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: SimpleNamespace(
        setText=copied.append)))
    panel.copy_button.click()
    assert copied == ["D:\\a.pdf\nD:\\b.pdf"]


def test_buttons_disable_when_the_set_is_empty(qapp):
    panel = PinnedPanel()
    assert not panel.open_button.isEnabled()
    panel.pin(row(r"D:\a.pdf"))
    assert panel.open_button.isEnabled()
    panel.clear()
    assert not panel.open_button.isEnabled()


def test_the_list_offers_real_file_urls_not_qts_own_format(qapp):
    """The point of swapping `QListWidget` for `DraggableResultsModel`."""
    # 2026-10-05: a path this system can hold in a file link. `D:\a.pdf` is
    # not one on a Mac - Qt reads it there as a file called that in "/".
    import os
    from pathlib import Path

    native = r"D:\a.pdf" if os.name == "nt" else "/a.pdf"
    panel = PinnedPanel()
    panel.pin(row(native))
    assert panel.list.dragEnabled()
    model = panel.list.model()
    data = model.mimeData([model.index(0, 0)])
    assert data.hasUrls()
    assert Path(data.urls()[0].toLocalFile()) == Path(native)


def test_enabled_checkbox_defaults_on_with_no_store(qapp):
    box = enabled_checkbox(None, on_toggle=lambda _checked: None)
    assert box.isChecked()


def test_enabled_checkbox_reads_a_stored_off_state(qapp):
    store = SimpleNamespace(get_state=lambda key, default=None: "off")
    box = enabled_checkbox(store, on_toggle=lambda _checked: None)
    assert not box.isChecked()
