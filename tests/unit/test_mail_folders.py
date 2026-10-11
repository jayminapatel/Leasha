r"""Order 1n: Mail's mailboxes and folders on the left, and a click narrows the list.

Layer: L2 and L5
"""

from __future__ import annotations

import os

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.tasks import browse_messages_typed
from app.ui.widgets.mail_folders import folder_tree, in_folder

ARCHIVE = r"D:\Mail\2018.pst"
OTHER = r"D:\Mail\2019.pst"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "m.db") as store:
        for key, archive, folder, subject in (
                ("pst://1/1", ARCHIVE, "Inbox", "Boiler quote"),
                ("pst://1/2", ARCHIVE, "Inbox/Projects", "Pump survey"),
                ("pst://1/3", ARCHIVE, "Inbox/Projects", "Pump drawings"),
                ("pst://1/4", ARCHIVE, "Sent Items", "Re: Boiler quote"),
                ("pst://1/5", ARCHIVE, "Inboxes old", "Not under Inbox"),
                ("pst://2/1", OTHER, "Inbox", "Boiler again")):
            file_id = store.upsert_file(key, size_bytes=1, mtime_ns=1, status="INDEXED",
                                        source_kind="pst_message")
            store.set_message(file_id, store_path=archive, folder_path=folder, subject=subject,
                              sender="sam@example.com", sent_at=1_700_000_000 + file_id)
        yield store


def _subjects(page):
    return sorted(row["subject"] for row in page["rows"])


def test_folder_counts_nest_and_add_up(store):
    rows = store.mail_folder_counts()
    tree = folder_tree(rows)
    assert tree[ARCHIVE]["count"] == 5 and tree[OTHER]["count"] == 1
    inbox = tree[ARCHIVE]["folders"]["Inbox"]
    assert inbox["count"] == 3 and inbox["folders"]["Projects"]["count"] == 2
    assert inbox["folders"]["Projects"]["path"] == "Inbox/Projects"


def test_a_folder_narrows_to_itself_and_the_folders_inside_it(store):
    page = browse_messages_typed(store, "", limit=50, folder=(ARCHIVE, "Inbox"))
    assert _subjects(page) == ["Boiler quote", "Pump drawings", "Pump survey"]
    page = browse_messages_typed(store, "", limit=50, folder=(ARCHIVE, "Inbox/Projects"))
    assert _subjects(page) == ["Pump drawings", "Pump survey"]


def test_a_whole_archive_and_typed_words_together(store):
    page = browse_messages_typed(store, "", limit=50, folder=(ARCHIVE, ""))
    assert len(page["rows"]) == 5
    page = browse_messages_typed(store, "/subject boiler", limit=50, folder=(ARCHIVE, ""))
    assert _subjects(page) == ["Boiler quote", "Re: Boiler quote"]
    assert len(browse_messages_typed(store, "", limit=50)["rows"]) == 6     # All mail


def test_the_summary_says_where():
    assert in_folder("6 messages", (ARCHIVE, "Inbox/Projects")) == "6 messages - in Projects"
    assert in_folder("6 messages", (ARCHIVE, "")) == "6 messages - in 2018.pst"
    assert in_folder("6 messages", None) == "6 messages"


@pytest.mark.gui
def test_the_list_the_switch_and_the_view_entry(store):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.mail_view import MailView
    from app.ui.state_writes import settle_before_run

    view = MailView(store)
    view.show()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    folders = view.folder_split.widget(0)
    # Order 1p: drawn by `SideList` - All mail, then a Mailboxes section.
    names = [folders.topLevelItem(i).text(0) for i in range(folders.topLevelItemCount())]
    assert names == ["All mail", "Mailboxes"]
    boxes = folders.topLevelItem(1)
    assert [boxes.child(i).text(0) for i in range(boxes.childCount())] == ["2018.pst", "2019.pst"]

    item = boxes.child(0).child(0)                           # 2018.pst > Inbox
    folders._clicked(item, 0)
    assert view.mail_folder == (ARCHIVE, "Inbox")

    view.toggles["folders"].click()
    assert not folders.isVisible()
    settle_before_run(5000)
    assert store.get_state("ui:mail_folders") == "0"
    menu = view.view_button.menu_for(view)
    entry = next(a for a in menu.actions() if a.text() == "Folders")
    entry.trigger()
    assert folders.isVisible() and view.toggles["folders"].isChecked()
    view.shutdown()


# -- live account mail (2026-10-11, the owner): it was "(no archive)", and a click on its folders showed nothing ----

@pytest.fixture
def live(tmp_path):
    from urllib.parse import quote

    from app.accounts.store import AccountStore

    with SqliteStore(tmp_path / "live.db") as store:
        accounts = AccountStore(store)
        gmail = accounts.add("google", "me@gmail.example", "GMail Main", mail_on=True)
        work = accounts.add("microsoft", "pending-123", "MS Main", mail_on=True)
        for aid, folder, remote, subject in (
                (gmail, "", "g1", "Gmail one"), (gmail, "", "g2", "Gmail two"),
                (work, "Inbox", "m1", "Inbox one"), (work, "2026/2026", "m2", "Deep one"),
                (work, "Q1_100%", "m3", "Odd name")):
            file_id = store.upsert_file(f"account://{aid}/{quote(folder, safe='')}/{remote}", size_bytes=1,
                                        mtime_ns=1, status="INDEXED", source_kind="eml")
            store.set_message(file_id, store_path=None, folder_path=None, subject=subject,
                              sender="sam@example.com", sent_at=1_700_000_000 + file_id)
        loose = store.upsert_file(r"D:\Saved\note.eml", size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="eml")
        store.set_message(loose, store_path=None, folder_path=None, subject="A saved file",
                          sender="sam@example.com", sent_at=1_700_000_500)
        pst = store.upsert_file("pst://9/1", size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="pst_message")
        store.set_message(pst, store_path=ARCHIVE, folder_path="Inbox", subject="From an archive",
                          sender="sam@example.com", sent_at=1_700_000_900)
        # An attachment inside an archive is recorded with no archive of its own (4,000 of them in the owner's
        # index, folder "2025/2025"): it is not a message saved as a file.
        att = store.upsert_file("pst://2025/0000/attachments/image007.png", size_bytes=1, mtime_ns=1,
                                status="INDEXED", source_kind="pst_message")
        store.set_message(att, store_path=None, folder_path="2025/2025", subject=None,
                          sender=None, sent_at=1_700_000_950)
        yield store, str(gmail), str(work)


def test_live_mail_is_grouped_by_account_and_named_for_it(live):
    from app.ui.presenter import mail_places as places
    from app.ui.widgets.mail_folders import mail_entries

    store, gmail, work = live
    rows = store.mail_folder_counts()
    assert (f"account://{gmail}", "", 2) in rows and (f"account://{work}", "Inbox", 1) in rows
    assert ("", "", 1) in rows and (ARCHIVE, "Inbox", 1) in rows
    assert not any(a == "" and f for a, f, _n in rows), "an archive's attachment landed under saved files"
    names = store.mail_account_names()
    assert names[gmail] == "GMail Main - me@gmail.example" and names[work] == "MS Main"   # no "pending-" address
    entries = {e.key: e for e in mail_entries(folder_tree(rows), names) if e.section == "Mailboxes"}
    assert entries[f"account://{gmail}|"].label == "GMail Main - me@gmail.example"
    assert entries[f"account://{gmail}|"].icon == "cloud" and entries["|"].label == "Messages saved as files"
    assert "(no archive)" not in {e.label for e in entries.values()}
    assert places.place_label(ARCHIVE, names) == "2018.pst"
    assert list(entries)[0].startswith("account://")                                     # accounts first


def test_a_click_on_live_mail_lists_it(live):
    store, gmail, work = live
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{gmail}", ""))) == \
        ["Gmail one", "Gmail two"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{work}", ""))) == \
        ["Deep one", "Inbox one", "Odd name"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{work}", "Inbox"))) == ["Inbox one"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{work}", "2026"))) == ["Deep one"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{work}", "2026/2026"))) == ["Deep one"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(f"account://{work}", "Q1_100%"))) == ["Odd name"]
    assert _subjects(browse_messages_typed(store, "/subject one", limit=50, folder=(f"account://{work}", ""))) == \
        ["Deep one", "Inbox one"]


def test_messages_saved_as_files_and_archives_still_list_and_do_not_mix(live):
    store, gmail, _work = live
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=("", ""))) == ["A saved file"]
    assert _subjects(browse_messages_typed(store, "", limit=50, folder=(ARCHIVE, ""))) == ["From an archive"]
    assert len(browse_messages_typed(store, "", limit=50)["rows"]) == 8                    # All mail


def test_the_summary_names_the_account(live):
    store, gmail, _work = live
    from app.ui.widgets import mail_folders

    mail_folders._NAMES.update(store.mail_account_names())
    assert in_folder("2 messages", (f"account://{gmail}", "")) == "2 messages - in GMail Main - me@gmail.example"
