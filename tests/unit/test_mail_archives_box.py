r"""The Mail archives box: each archive read its own way, and read again.

Layer: L5

2026-10-07, the owner: "need a way for each pst file it can be configured how
to index outlook or direct ... there should be a reindex button on those files
... dont forget we need icons everywhere". Both ways of reading one again were
asked for: "Read again" over the top, and "Clear and read again", which asks,
with the count, before anything is removed.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QWidget                           # noqa: E402

from app.index.archives import normalise                        # noqa: E402
from app.storage.sqlite_store import SqliteStore                # noqa: E402
from app.ui import presenter                                    # noqa: E402
from app.ui.widgets.mail_archives_box import (                  # noqa: E402
    COL_CLEAR, COL_HOW, COL_READ, MailArchivesBox,
)

A = r"D:\Mail\2019.pst"
B = r"D:\Mail\Old\work.pst"
OST = r"C:\Users\me\AppData\Local\Microsoft\Outlook\me.ost"


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _file(store, path, kind="file", status="INDEXED", ext=None):
    return store.upsert_file(path, size_bytes=10, mtime_ns=1, source_kind=kind,
                             status=status, ext=ext)


def _message(store, mailbox, entry, *, store_path):
    file_id = _file(store, f"pst://{mailbox}/{entry}", "pst_message", ext="")
    with store.write() as conn:
        conn.execute(
            "INSERT INTO messages (file_id, store_path, entry_id, subject, sender, "
            "recipients, sent_at, has_attach) VALUES (?, ?, ?, 's', 'a@b', '[]', 1, 1)",
            (file_id, store_path, entry))
    return file_id


def _fill(store):
    _file(store, A, "archive")
    _file(store, B, "file", status="PENDING")
    _file(store, OST, "file", status="SKIPPED")
    _file(store, r"D:\Docs\notes.txt")
    for entry in ("e1", "e2", "e3"):
        _message(store, "2019", entry, store_path=A)
    _file(store, "pst://2019/e1/attachments/inner.pst", "archive")   # not on disk
    _message(store, "work", "w1", store_path=B)


# -- the store ------------------------------------------------------------------

def test_the_store_lists_every_archive_with_its_message_count(store):
    _fill(store)
    rows = store.mail_archives()
    assert [row["path"] for row in rows] == sorted([A, B, OST])
    by_path = {row["path"]: row for row in rows}
    assert by_path[A]["messages"] == 3
    assert by_path[B]["messages"] == 1
    assert by_path[OST]["messages"] == 0
    assert by_path[A]["status"] == "INDEXED"
    assert by_path[B]["status"] == "PENDING"


def test_an_empty_index_has_no_archives(store):
    assert store.mail_archives() == []


# -- the words --------------------------------------------------------------------

def test_the_words():
    assert presenter.messages_words(0) == "No messages"
    assert presenter.messages_words(1) == "1 message"
    assert presenter.messages_words(12345) == "12,345 messages"
    assert presenter.archive_status_words("INDEXED") == "Read"
    assert presenter.archive_status_words("PENDING") == "Not read yet"
    assert presenter.archive_status_words("PENDING", None, 4).startswith("Partly read")
    assert presenter.archive_status_words("SKIPPED", "ERR_SOMETHING_NEW") == \
        "Skipped: ERR_SOMETHING_NEW"
    assert presenter.archive_status_words("FAILED", None).startswith("Skipped: ")
    labels = [label for label, _value in presenter.ARCHIVE_CHOICES]
    assert labels == ["Automatic - direct if possible (as set above)",
                      "Direct file reading (no Outlook needed)", "Through Outlook (MAPI)"]


def test_the_first_choice_names_the_way_the_setting_above_reads():
    # 2026-10-08, the owner: "the text use the setting above makes no sense it
    # should display the actual option".
    assert presenter.archive_default_label("auto") == \
        "Automatic - direct if possible (as set above)"
    assert presenter.archive_default_label("libpff") == "Direct file reading (as set above)"
    assert presenter.archive_default_label("outlook") == "Through Outlook (as set above)"
    assert presenter.archive_default_label("nonsense") == presenter.archive_default_label("auto")
    for backend in ("auto", "libpff", "outlook"):
        choices = presenter.archive_choices(backend)
        assert [value for _label, value in choices] == ["auto", "libpff", "outlook"]
        assert choices[0][0] == presenter.archive_default_label(backend)
    assert "Use the setting above" not in presenter.ARCHIVE_CHOICE_TIP
    # Put back to follow the setting, a line says which way that is.
    said = presenter.archive_choice_saved_message(A, "auto", {}, "outlook")
    assert said.startswith("2019.pst: Through Outlook (as set above).")


def test_the_choice_labels_are_the_drop_down_above_word_for_word():
    from pathlib import Path

    source = Path("app/ui/settings_view.py").read_text(encoding="utf-8")
    for label, value in presenter.ARCHIVE_CHOICES[1:]:
        assert f'addItem("{label}", "{value}")' in source


def test_the_clear_question_names_the_count_and_spares_the_file():
    title, body = presenter.clear_archive_confirmation(A, 1234)
    assert "2019.pst" in title
    assert "1,234 items" in body
    assert "Your file is not touched." in body


def test_every_status_line_names_an_amount():
    assert "3 messages" in presenter.read_again_message(A, 3)
    assert "12 items" in presenter.clearing_archive_message(A, 12)
    assert "12 items" in presenter.archive_cleared_message(A, 12)
    assert "1 archive" in presenter.archive_choice_saved_message(A, "libpff", {"a": "libpff"})


# -- the box ------------------------------------------------------------------------

ROWS = [
    {"path": A, "status": "INDEXED", "skip_code": None, "messages": 3},
    {"path": B, "status": "PENDING", "skip_code": None, "messages": 0},
    {"path": OST, "status": "SKIPPED", "skip_code": "ERR_OUTLOOK_MISSING", "messages": 0},
]


def test_an_empty_box_says_so_instead_of_an_empty_table(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives([], {})
    assert box.empty.isVisibleTo(box)
    assert not box.tree.isVisibleTo(box)
    assert "No mail archives in the index yet" in box.empty.text()


def test_filling_the_box_emits_nothing_and_shows_the_saved_choice(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    seen: list = []
    box.choice_changed.connect(lambda *a: seen.append(a))
    box.set_archives(ROWS, {normalise(A): "outlook"})
    assert seen == []
    assert box.archives() == [A, B, OST]
    assert not box.empty.isVisibleTo(box)
    combo = box.tree.itemWidget(box.item_for(A), COL_HOW)
    assert combo.currentData() == "outlook"
    assert box.tree.itemWidget(box.item_for(B), COL_HOW).currentData() == "auto"
    assert box.current_choices() == {normalise(A): "outlook"}
    assert box.messages_in(A) == 3


def test_a_choice_emits_with_its_archive(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives(ROWS, {})
    seen: list = []
    box.choice_changed.connect(lambda *a: seen.append(a))
    combo = box.tree.itemWidget(box.item_for(B), COL_HOW)
    combo.setCurrentIndex(combo.findData("libpff"))
    assert seen == [(B, "libpff")]


def test_an_ost_is_read_through_outlook_and_cannot_be_changed(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives(ROWS, {normalise(OST): "libpff"})
    combo = box.tree.itemWidget(box.item_for(OST), COL_HOW)
    assert combo.currentData() == "outlook"
    assert not combo.isEnabled()
    assert "Only Outlook can read it" in combo.toolTip()
    assert normalise(OST) not in box.current_choices()


def _button(box, path, column):
    from PySide6.QtWidgets import QPushButton

    return box.tree.itemWidget(box.item_for(path), column).findChild(QPushButton)


def test_both_buttons_carry_icons_and_emit_their_archive(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives(ROWS, {})
    again, clear = _button(box, B, COL_READ), _button(box, B, COL_CLEAR)
    for button in (again, clear):
        assert not button.icon().isNull()
        assert button.toolTip()
    assert again.property("buttonRole") == "secondary"
    assert clear.property("buttonRole") == "danger"
    read, cleared = [], []
    box.read_again.connect(read.append)
    box.clear_and_read.connect(cleared.append)
    again.click()
    clear.click()
    assert read == [B] and cleared == [B]


def test_the_first_choice_follows_the_setting_above_and_emits_nothing(qtbot):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives(ROWS, {normalise(A): "outlook"}, default_backend="libpff")
    first = lambda path: box.tree.itemWidget(box.item_for(path), COL_HOW).itemText(0)  # noqa: E731
    assert first(B) == "Direct file reading (as set above)"
    assert box.tree.itemWidget(box.item_for(B), COL_HOW).currentText() == \
        "Direct file reading (as set above)"
    seen: list = []
    box.choice_changed.connect(lambda *a: seen.append(a))
    box.set_default_backend("outlook")
    assert [first(path) for path in (A, B, OST)] == ["Through Outlook (as set above)"] * 3
    assert box.tree.itemWidget(box.item_for(B), COL_HOW).currentData() == "auto"
    assert box.tree.itemWidget(box.item_for(A), COL_HOW).currentData() == "outlook"
    assert seen == []
    assert box.current_choices() == {normalise(A): "outlook"}
    box.set_default_backend("auto")
    assert first(B) == "Automatic - direct if possible (as set above)"
    # A reload that does not say keeps what it was told last.
    box.set_default_backend("libpff")
    box.set_archives(ROWS, {})
    assert first(B) == "Direct file reading (as set above)"


def _shown(qtbot, width=900):
    box = MailArchivesBox()
    qtbot.addWidget(box)
    box.set_archives(ROWS, {})
    box.resize(width, 400)
    box.show()
    qtbot.waitExposed(box)
    box.columns.fit()
    qtbot.wait(20)
    return box


def test_every_column_can_be_dragged_and_is_fitted(qtbot):
    from PySide6.QtWidgets import QHeaderView

    box = _shown(qtbot)
    header = box.tree.header()
    for column in range(header.count()):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
        assert header.sectionSize(column) >= header.sectionSizeHint(column)
    assert sum(header.sectionSize(c) for c in range(header.count())) == \
        box.tree.viewport().width()
    # The drop-downs and both buttons are all on the page.
    for column in (COL_HOW, COL_READ, COL_CLEAR):
        widest = max(box.tree.itemWidget(box.item_for(p), column).sizeHint().width()
                     for p in (A, B, OST))
        assert header.sectionSize(column) >= widest


def test_the_first_choice_changing_refits_its_column(qtbot):
    box = _shown(qtbot)
    before = box.tree.header().sectionSize(COL_HOW)
    box.set_default_backend("outlook")          # shorter than "Automatic - ..."
    qtbot.waitUntil(lambda: box.tree.header().sectionSize(COL_HOW) != before, timeout=3_000)


def test_no_button_or_drop_down_is_cut_off_by_its_line(qtbot):
    from PySide6.QtWidgets import QPushButton

    box = _shown(qtbot)
    tree = box.tree
    for path in (A, B, OST):
        item = box.item_for(path)
        line = tree.visualItemRect(item)
        for column in (COL_HOW, COL_READ, COL_CLEAR):
            cell = tree.itemWidget(item, column)
            place = cell.geometry()
            assert line.top() <= place.top() and place.bottom() <= line.bottom()
            assert cell.height() >= cell.sizeHint().height()
        for column in (COL_READ, COL_CLEAR):
            button = tree.itemWidget(item, column).findChild(QPushButton)
            assert button.height() <= line.height()
            assert button.text() == "" and not button.icon().isNull()
            assert button.toolTip() and button.accessibleName()


def test_the_archive_column_elides_in_the_middle_with_the_whole_path_in_the_tooltip(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QStyleOptionViewItem

    box = _shown(qtbot)
    item = box.item_for(B)
    index = box.tree.indexFromItem(item, 0)
    option = QStyleOptionViewItem()
    box.tree.itemDelegate().initStyleOption(option, index)
    assert option.textElideMode == Qt.TextElideMode.ElideMiddle
    assert item.toolTip(0) == B


# -- the controller -------------------------------------------------------------------

class _Window(QWidget):
    """What the controller reaches through `self._w`, and nothing else."""

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._vectors = None
        self._image_vectors = None
        self.indexed: list[str] = []
        self.notices: list[str] = []
        self.errors: list = []
        self.settings_view = QWidget()
        self.settings_view.mail_archives = MailArchivesBox()

    def _index_file_now(self, path):
        self.indexed.append(path)

    def notify(self, text, timeout_ms=None):
        self.notices.append(text)

    def _show_error(self, error):
        self.errors.append(error)


@pytest.fixture()
def window(qtbot, store):
    shown = _Window(store)
    qtbot.addWidget(shown)
    return shown


@pytest.fixture()
def own_lock(monkeypatch, tmp_path):
    """Every forget_ids takes a run lock of this test's own, not the machine's."""
    import app.index.forget_folder as forget

    real = forget.forget_ids
    options = {"name": f"Leasha.Test.MailArchives.{uuid.uuid4().hex}",
               "lock_dir": tmp_path / "locks"}

    def forget_ids(*args, **kwargs):
        return real(*args, **kwargs, **options)

    monkeypatch.setattr(forget, "forget_ids", forget_ids)


def _controller(window):
    from app.ui.controllers.settings_controller import SettingsController

    return SettingsController(window)


def test_the_list_and_the_choices_load_on_a_worker(qtbot, window, store):
    from app.index.run_setup import PST_BACKENDS_STATE_KEY, dump_pst_backends

    _fill(store)
    store.set_state(PST_BACKENDS_STATE_KEY, dump_pst_backends({normalise(A): "outlook"}))
    ctl = _controller(window)
    ctl._load_mail_archives()
    box = window.settings_view.mail_archives
    qtbot.waitUntil(lambda: box.archives() == sorted([A, B, OST]), timeout=5_000)
    assert box.current_choices() == {normalise(A): "outlook"}


def test_the_setting_above_is_read_with_the_list_and_followed_when_it_changes(
        qtbot, window, store):
    from app.index.run_setup import PST_BACKEND_STATE_KEY

    _fill(store)
    store.set_state(PST_BACKEND_STATE_KEY, "libpff")
    window._apply_pst_backend = lambda backend: None
    ctl = _controller(window)
    ctl._load_mail_archives()
    box = window.settings_view.mail_archives
    qtbot.waitUntil(lambda: box.archives() == sorted([A, B, OST]), timeout=5_000)
    first = box.tree.itemWidget(box.item_for(B), COL_HOW).itemText(0)
    assert first == "Direct file reading (as set above)"
    # "How to read archives" changed in the same window.
    ctl._save_pst_backend("outlook")
    assert box.tree.itemWidget(box.item_for(B), COL_HOW).itemText(0) == \
        "Through Outlook (as set above)"
    # A reload straight after, before the write may have landed, keeps it.
    ctl._load_mail_archives()
    qtbot.wait(200)
    assert box.default_backend() == "outlook"


def test_a_choice_is_saved_where_the_reader_reads_it(qtbot, window, store):
    from app.index.run_setup import PST_BACKENDS_STATE_KEY, load_pst_backends

    ctl = _controller(window)
    ctl._save_archive_choice(B, "libpff")
    qtbot.waitUntil(lambda: load_pst_backends(
        store.get_state(PST_BACKENDS_STATE_KEY, "") or "") == {normalise(B): "libpff"},
        timeout=5_000)
    ctl._save_archive_choice(B, "auto")
    qtbot.waitUntil(lambda: load_pst_backends(
        store.get_state(PST_BACKENDS_STATE_KEY, "") or "") == {}, timeout=5_000)
    assert window.notices and "work.pst" in window.notices[-1]


def test_read_again_starts_a_run_of_that_archive_and_removes_nothing(window, store):
    _fill(store)
    before = len(store.mail_archives())
    ctl = _controller(window)
    ctl._read_archive_again(A)
    assert window.indexed == [A]
    assert len(store.mail_archives()) == before
    assert window.notices


def test_clear_removes_nothing_until_it_is_confirmed(qtbot, window, store, own_lock):
    _fill(store)
    ctl = _controller(window)
    asked: list = []
    ctl.ask_clear_archive = lambda path, count: asked.append((path, count)) or False
    ctl._clear_archive(A)
    qtbot.waitUntil(lambda: bool(asked), timeout=5_000)
    # The archive's own row, its three messages and one attachment: five items.
    assert asked == [(A, 5)]
    assert window.indexed == []
    assert {row["path"]: row["messages"] for row in store.mail_archives()}[A] == 3


def test_clear_and_read_again_clears_then_reads(qtbot, window, store, own_lock):
    _fill(store)
    ctl = _controller(window)
    ctl.ask_clear_archive = lambda path, count: True
    ctl._clear_archive(A)
    qtbot.waitUntil(lambda: window.indexed == [A], timeout=5_000)
    paths = [row["path"] for row in store.mail_archives()]
    assert A not in paths and B in paths
    with store.write() as conn:
        left = conn.execute("SELECT COUNT(*) FROM messages WHERE store_path = ?",
                            (A,)).fetchone()[0]
        others = conn.execute("SELECT COUNT(*) FROM messages WHERE store_path = ?",
                              (B,)).fetchone()[0]
    assert left == 0 and others == 1
    assert any("5 items" in text for text in window.notices)
    assert not window.errors


def test_clearing_an_archive_with_nothing_in_the_index_just_reads_it(qtbot, window, store):
    ctl = _controller(window)
    ctl.ask_clear_archive = lambda path, count: pytest.fail("nothing to ask about")
    ctl._clear_archive(r"D:\Mail\never-read.pst")
    qtbot.waitUntil(lambda: window.indexed == [r"D:\Mail\never-read.pst"], timeout=5_000)


# -- the settings page ------------------------------------------------------------------

def test_the_box_sits_under_how_to_read_archives_and_relays_its_signals(qtbot, tmp_path):
    from app.core.config import load_settings
    from app.ui.settings_view import CATEGORY_WHATS_INDEXED, SettingsView
    from tests.unit.conftest import ENV

    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    view = SettingsView(load_settings(env))
    qtbot.addWidget(view)
    page = view._nav.page(CATEGORY_WHATS_INDEXED)
    assert page.isAncestorOf(view.mail_archives)
    layout = page.layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert order.index(view.mail_archives) == order.index(view.pst_box) + 1
    seen: list = []
    view.mail_archive_read_again_requested.connect(lambda p: seen.append(("read", p)))
    view.mail_archive_clear_requested.connect(lambda p: seen.append(("clear", p)))
    view.mail_archive_choice_changed.connect(lambda p, b: seen.append(("how", p, b)))
    view.mail_archives.set_archives(ROWS, {})
    _button(view.mail_archives, A, COL_READ).click()
    _button(view.mail_archives, A, COL_CLEAR).click()
    combo = view.mail_archives.tree.itemWidget(view.mail_archives.item_for(A), COL_HOW)
    combo.setCurrentIndex(combo.findData("outlook"))
    assert seen == [("read", A), ("clear", A), ("how", A, "outlook")]
