r"""Order 0y section 4d: open the original message.

Layer: L0 / L5

> **4d** Open the original. "Open in Outlook" for a message from Outlook (its
> `entry_id`), "Open" for a `.eml`/`.msg` file, so the full message - including
> the quoted text the index deliberately does not hold - is one click away. The
> honest quoted-text notice stays.

**Outlook is never started here.** Starting it on a machine with archives in its
profile attaches and locks them, so the real launcher
(`osbridge.outlook.show_in_outlook`) is behind a seam and every test passes a
fake. What the real one does with a real Outlook is an owner check (A9).

What is tested: which messages have an original and what the button says
(`presenter.mail.original_target`); how an identifier the libpff reader wrote is
turned into the one Outlook wants (`osbridge.outlook.pst_entry_id`); that
previewing a message opens nothing; that the click reaches the launcher with
the message's identifier and its archive, on a worker; and that a failure is an
error with a way out.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.core.osbridge.outlook import pst_entry_id
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.mail import OriginalTarget, original_target
from tests.unit.test_mail_preview_card import TUESDAY, add_message

ROOT_ID = "00000000" + "A1B2C3D4E5F60718293A4B5C6D7E8F90" + "22800000"


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


# --- which messages have an original ----------------------------------------------

def test_a_message_from_an_outlook_archive_opens_in_outlook() -> None:
    target = original_target(
        {"entry_id": "0000ABCD", "store_path": "D:/mail/Archive2019.pst"},
        "pst://Archive2019/0000ABCD")

    assert target == OriginalTarget(
        kind="outlook", label="Open in Outlook", entry_id="0000ABCD",
        store_path="D:/mail/Archive2019.pst", path="pst://Archive2019/0000ABCD")


def test_an_eml_or_msg_file_opens_as_a_file() -> None:
    for name in ("C:/mail/trip.eml", "C:/mail/TRIP.MSG", "C:/mail/note.emlx"):
        target = original_target({"entry_id": None, "store_path": None}, name)

        assert (target.kind, target.label, target.path) == ("file", "Open", name)


def test_a_message_inside_a_mailbox_file_has_no_original_to_open() -> None:
    """An mbox or .olm message is not a file, and Outlook cannot be pointed at
    it. No button is better than one that fails."""
    assert original_target({"entry_id": None, "store_path": None},
                           "C:/mail/All mail.mbox/message-12") is None
    assert original_target({"entry_id": "12", "store_path": None}, "pst://x/12") is None
    assert original_target({"entry_id": "", "store_path": "D:/a.pst"}, "pst://x/12") is None
    assert original_target(None, "") is None


# --- the identifier Outlook wants ---------------------------------------------------

def test_an_outlook_identifier_is_used_as_it_is() -> None:
    real = "00000000A1B2C3D4E5F60718293A4B5C6D7E8F9064002000"

    assert pst_entry_id(real, ROOT_ID) == real


def test_a_libpff_number_becomes_an_identifier_in_the_same_archive() -> None:
    """The libpff reader stores the message's number inside the archive. An
    Outlook identifier for a .pst item is four zero bytes, the archive's own
    sixteen, and that number as four bytes low-first - so it is the archive's
    root identifier with the last four bytes replaced."""
    assert pst_entry_id("2097188", ROOT_ID) == (
        "00000000" + "A1B2C3D4E5F60718293A4B5C6D7E8F90" + "24002000")


def test_an_identifier_that_cannot_be_made_says_so() -> None:
    with pytest.raises(ValueError):
        pst_entry_id("Inbox/Sub#1234567", ROOT_ID)          # libpff's last-resort key
    with pytest.raises(ValueError):
        pst_entry_id("2097188", "not-an-entry-id")
    with pytest.raises(ValueError):
        pst_entry_id("", ROOT_ID)


# --- the worker body -----------------------------------------------------------------

class FakeOutlook:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, entry_id: str, store_path: str) -> None:
        self.calls.append((entry_id, store_path))
        if self.fail is not None:
            raise self.fail


OUTLOOK = OriginalTarget(kind="outlook", label="Open in Outlook", entry_id="0000ABCD",
                         store_path="D:/mail/Archive2019.pst", path="pst://Archive2019/0000ABCD")


def test_the_launcher_is_given_the_identifier_and_the_archive() -> None:
    from app.ui.widgets.mail_open import open_original

    outlook = FakeOutlook()

    assert open_original(OUTLOOK, outlook=outlook) is None
    assert outlook.calls == [("0000ABCD", "D:/mail/Archive2019.pst")]


def test_a_failure_is_an_error_with_a_way_out() -> None:
    from app.ui.widgets.mail_open import open_original

    error = open_original(OUTLOOK, outlook=FakeOutlook(RuntimeError("Outlook did not start")))

    assert error.code == "ERR_OUTLOOK_OPEN"
    assert "Archive2019.pst" in error.render()
    assert "Outlook did not start" in (error.details or "")
    assert error.suggestion


def test_a_file_is_opened_by_the_file_opener_not_by_outlook(tmp_path) -> None:
    from app.ui.widgets.mail_open import open_original

    opened: list[str] = []
    outlook = FakeOutlook()
    target = OriginalTarget(kind="file", label="Open", path=str(tmp_path / "trip.eml"))

    assert open_original(target, outlook=outlook, open_file=opened.append) is None
    assert opened == [str(tmp_path / "trip.eml")] and outlook.calls == []


def test_the_real_launcher_is_not_reached_by_importing_anything() -> None:
    """The seam: the module that knows Outlook imports nothing of it until called."""
    import app.core.osbridge.outlook
    import app.ui.widgets.mail_open  # noqa: F401

    source = open(app.core.osbridge.outlook.__file__, encoding="utf-8").read()
    head = source.split("def show_in_outlook", 1)[0]
    assert "import win32com" not in head and "import pythoncom" not in head


# --- the pane ---------------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _mail_view(store, outlook):
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import pump

    view = MailView(store)
    view.preview.outlook_launcher = outlook
    pump()
    return view


def test_a9_previewing_opens_nothing_and_the_click_opens_that_message(qapp, store) -> None:
    """Acceptance A9, as far as it can go without Outlook: the button is there
    for an Outlook message, previewing starts nothing, and one click hands the
    launcher this message's identifier and archive."""
    from tests.unit.test_mail_preview_card import preview_now, pump

    add_message(store, path="pst://Archive2019/0000ABCD", subject="School trip",
                sender="dave@acme.com", sent_at=TUESDAY, body="The trip is on Friday.",
                entry_id="0000ABCD", store_path="D:/mail/Archive2019.pst", quoted_removed=480)
    outlook = FakeOutlook()
    view = _mail_view(store, outlook)
    try:
        preview_now(view.preview, view._rows[0])
        pane = view.preview

        assert not pane.original_button.isHidden()
        assert pane.original_button.text().replace("&", "") == "Open in Outlook"
        assert "480" in pane.notice.text(), "the quoted-text notice stays"
        assert outlook.calls == [], "previewing must never start Outlook"

        pane.original_button.click()
        pump()

        assert outlook.calls == [("0000ABCD", "D:/mail/Archive2019.pst")]
    finally:
        view.shutdown()


def test_outlook_failing_is_reported_not_raised(qapp, store) -> None:
    from tests.unit.test_mail_preview_card import preview_now, pump

    add_message(store, path="pst://Archive2019/0000ABCD", subject="School trip",
                sender="dave@acme.com", sent_at=TUESDAY, body="x",
                entry_id="0000ABCD", store_path="D:/mail/Archive2019.pst")
    view = _mail_view(store, FakeOutlook(RuntimeError("no Outlook here")))
    errors: list = []
    view.error.connect(errors.append)
    try:
        preview_now(view.preview, view._rows[0])
        view.preview.original_button.click()
        pump()

        assert [error.code for error in errors] == ["ERR_OUTLOOK_OPEN"]
    finally:
        view.shutdown()


def test_an_eml_message_opens_with_the_panes_open_button(qapp, store, tmp_path, monkeypatch) -> None:
    """For a message that is a file, "Open" opens the file - not Outlook, and
    not a search inside it."""
    from app.ui.widgets import mail_open
    from tests.unit.test_mail_preview_card import preview_now, pump

    letter = tmp_path / "trip.eml"
    letter.write_text("Subject: School trip\n\nThe trip is on Friday.", encoding="utf-8")
    add_message(store, path=str(letter), subject="School trip", sender="dave@acme.com",
                sent_at=TUESDAY, body="The trip is on Friday.", source_kind="eml")
    opened: list[str] = []
    monkeypatch.setattr(mail_open, "_open_file", lambda path: opened.append(path))
    outlook = FakeOutlook()
    view = _mail_view(store, outlook)
    searched: list[str] = []
    view.search_inside_requested.connect(searched.append)
    try:
        preview_now(view.preview, view._rows[0])
        pane = view.preview

        assert pane.original_button.isHidden(), "no Outlook button for a file"
        assert opened == [], "previewing opens nothing"

        pane.open_button.click()
        pump()

        assert opened == [str(letter)]
        assert outlook.calls == [] and searched == []
    finally:
        view.shutdown()


def test_a_file_that_is_not_a_message_keeps_its_open_button_as_it_was(qapp) -> None:
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    asked: list = []
    pane.open_requested.connect(asked.append)
    row = SimpleNamespace(path="D:/x/note.txt", name="note.txt")
    pane.show_row(row)
    pane._timer.stop()
    pane._rendered(Preview(kind=KIND_TEXT, body="plain", path="D:/x/note.txt"), pane._generation)

    pane.open_button.click()

    assert asked == [row]
    assert pane.original_button.isHidden()
    pane.close()


def test_the_button_is_in_the_button_system() -> None:
    from app.ui.widgets.buttons import lookup

    assert lookup("Open in Outlook") is not None
