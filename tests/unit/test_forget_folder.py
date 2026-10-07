r"""Removing a folder takes what was read from it out of the index.

Layer: L3

2026-10-07, the owner: "if a location is removed the data for that location
should be removed, ask confirmation at removal that the index data will be
removed too - the only way the list must reflect what is in the index."

The case a path test gets wrong is mail: a message read out of `D:\Mail\a.pst`
is stored as `pst://a/<entry>`, under no folder at all, and its attachments
below that. Measured on the owner's index the day this was written: 51,585 of
its 51,952 rows were mail, so a folder removal that went by path alone would
have left almost everything behind.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import AppErrorException
from app.index.archives import RECORD_STATE_KEY, ArchiveRecord, dump_records, load_records
from app.index.forget_folder import (
    count_outside,
    file_ids_from_folders,
    file_ids_outside,
    forget_folders,
    forget_outside,
)
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


@pytest.fixture()
def lock(tmp_path):
    """A run lock of this test's own - the real one is machine-wide."""
    return {"name": f"Leasha.Test.ForgetFolder.{uuid.uuid4().hex}",
            "lock_dir": tmp_path / "locks"}


def _file(store, path, kind="file"):
    return store.upsert_file(path, size_bytes=10, mtime_ns=1, source_kind=kind,
                             status="INDEXED")


def _message(store, mailbox, entry, *, store_path):
    file_id = _file(store, f"pst://{mailbox}/{entry}", "pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT INTO messages (file_id, store_path, entry_id, subject, sender, "
            "recipients, sent_at, has_attach) VALUES (?, ?, ?, 's', 'a@b', '[]', 1, 1)",
            (file_id, store_path, entry))
    return file_id


@pytest.fixture()
def rows(store):
    """Two listed folders, one inside the other, a mailbox in each, a message
    read through Outlook, and a catalogued drive's file."""
    ids = {
        "doc": _file(store, r"D:\Docs\a.txt"),
        "zip": _file(store, r"D:\Docs\b.zip", "archive"),
        "member": _file(store, r"D:\Docs\b.zip#inner.txt", "archive"),
        "work": _file(store, r"D:\Docs\Work\c.txt"),
        "pst": _file(store, r"D:\Docs\mail.pst", "archive"),
        "message": _message(store, "mail", "1", store_path=r"D:\Docs\mail.pst"),
        "attachment": _file(store, "pst://mail/1/attachments/x.docx", "pst_message"),
        "work_message": _message(store, "work", "1", store_path=r"D:\Docs\Work\w.pst"),
        "work_attachment": _file(store, "pst://work/1/attachments/y.docx", "pst_message"),
        "outlook": _message(store, "Outlook", "9", store_path=None),
        "elsewhere": _file(store, r"E:\Old\d.txt"),
        "docs2": _file(store, r"D:\Docs2\e.txt"),
    }
    volume = store.upsert_volume("test-volume", kind="drive", name="Stick")
    ids["drive"] = store.upsert_file(
        "leasha-volume://x/f.txt", size_bytes=1, mtime_ns=1, status="INDEXED",
        volume_id=volume, relative_path="f.txt")
    return ids


def _named(rows, ids):
    by_id = {value: key for key, value in rows.items()}
    return sorted(by_id[file_id] for file_id in ids)


def test_a_removed_folder_takes_its_files_mail_and_attachments(store, rows):
    found = file_ids_from_folders(store, [r"D:\Docs"], kept=[r"D:\Docs\Work"])
    assert _named(rows, found) == [
        "attachment", "doc", "member", "message", "pst", "zip"]


def test_the_folder_as_settings_spells_it_matches(store, rows):
    """Settings saves `D:/Docs`, the rows say `D:\\Docs`, and case may differ."""
    assert (sorted(file_ids_from_folders(store, ["d:/docs/"], kept=["D:/Docs/Work"]))
            == sorted(file_ids_from_folders(store, [r"D:\Docs"], kept=[r"D:\Docs\Work"])))


def test_a_folder_inside_a_removed_one_keeps_its_own(store, rows):
    found = _named(rows, file_ids_from_folders(store, [r"D:\Docs"], kept=[r"D:\Docs\Work"]))
    assert not {"work", "work_message", "work_attachment"} & set(found)


def test_removing_the_inner_folder_leaves_rows_the_outer_one_still_covers(store, rows):
    assert file_ids_from_folders(store, [r"D:\Docs\Work"], kept=[r"D:\Docs"]) == []


def test_a_folder_whose_name_starts_the_same_is_not_touched(store, rows):
    found = _named(rows, file_ids_from_folders(store, [r"D:\Docs"]))
    assert "docs2" not in found


def test_a_drive_and_outlook_mail_are_never_a_folder_s(store, rows):
    everything = file_ids_outside(store, [])
    assert not {rows["drive"], rows["outlook"]} & set(everything)


def test_leftovers_are_what_no_listed_folder_covers(store, rows):
    assert _named(rows, file_ids_outside(store, [r"D:\Docs", "D:/Docs2"])) == ["elsewhere"]
    assert count_outside(store, [r"D:\Docs", "D:/Docs2", r"E:\Old"]) == 0


class _Vectors:
    def __init__(self):
        self.deleted: list[int] = []

    def delete_by_file_ids(self, ids):
        self.deleted.extend(ids)


def test_forgetting_deletes_rows_and_both_kinds_of_vector(store, rows, lock):
    text, pictures = _Vectors(), _Vectors()
    result = forget_folders(store, text, pictures, [r"D:\Docs"], [r"D:\Docs\Work"],
                            run_lock_owner="a test", **lock)

    assert result == {"files": 6, "folders": [r"D:\Docs"]}
    gone = {rows[key] for key in ("attachment", "doc", "member", "message", "pst", "zip")}
    assert set(text.deleted) == gone == set(pictures.deleted)
    left = {file_id for file_id, *_rest in store.iter_file_origins()}
    assert not gone & left
    assert rows["work"] in left and rows["elsewhere"] in left
    assert store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE file_id = ?", (rows["message"],)).fetchone()[0] == 0


def test_a_removed_folder_forgets_its_archive_record(store, rows, lock):
    """Added back and marked as an archive, a stale record would skip it as
    unchanged - with nothing of it left in the index."""
    records = {key: ArchiveRecord(root=root, archived_at=1, files=1, mtime_ns=1)
               for key, root in (("d:\\docs", r"D:\Docs"), ("e:\\old", r"E:\Old"))}
    store.set_state(RECORD_STATE_KEY, dump_records(records))

    forget_folders(store, None, None, ["D:/Docs"], run_lock_owner="a test", **lock)

    assert set(load_records(store.get_state(RECORD_STATE_KEY, ""))) == {"e:\\old"}


def test_nothing_is_deleted_while_an_index_run_holds_the_index(store, rows, lock):
    from app.core.run_lock import IndexRunLock

    with IndexRunLock(None, owner="an index run", **lock):
        with pytest.raises(AppErrorException) as raised:
            forget_folders(store, None, None, [r"D:\Docs"], run_lock_owner="a test", **lock)
    assert raised.value.error.code == "ERR_INDEX_RUNNING"
    assert len(file_ids_from_folders(store, [r"D:\Docs"])) == 9


def test_leftovers_go_and_listed_folders_stay(store, rows, lock):
    result = forget_outside(store, None, None, [r"D:\Docs", "D:/Docs2"],
                            run_lock_owner="a test", **lock)
    assert result == {"files": 1, "folders": []}
    assert count_outside(store, [r"D:\Docs", "D:/Docs2"]) == 0
    assert len(file_ids_from_folders(store, [r"D:\Docs"])) == 9


def test_a_pst_deleted_from_disk_takes_its_mail_out_of_the_index(tmp_path):
    """The clean-up after a run removed a deleted archive's own row and members
    stored under its path - and never its messages, which are `pst://...` rows
    tied to it only by `messages.store_path`. Found 2026-10-07; the older test
    of this (`test_review_section_two`) models a message as `<archive>/message-1`,
    a shape no reader writes."""
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    with SqliteStore(tmp_path / "index.db") as store:
        archive = tmp_path / "old.pst"                    # never created: deleted
        marker = _file(store, str(archive), "archive")
        message = _message(store, "old", "1", store_path=str(archive))
        attachment = _file(store, "pst://old/1/attachments/a.docx", "pst_message")
        other = _message(store, "kept", "1", store_path=str(tmp_path / "kept.pst"))
        pipeline = Pipeline(store=store, vectors=_Vectors(), embedder=None,
                            config=PipelineConfig(walk=WalkConfig(roots=[tmp_path])))

        doomed = pipeline._doomed_inside_archives(seen=set(), archived=None)

        assert set(doomed) == {marker, message, attachment}
        assert other not in doomed


# --- the words ------------------------------------------------------------------


def test_the_question_names_the_folder_and_the_amount():
    from app.ui.presenter import remove_folders_confirmation

    title, body = remove_folders_confirmation([r"D:\Docs"], 1234)
    assert title == r"Remove D:\Docs?"
    assert "1,234 items" in body and "not touched" in body


def test_the_leftovers_line_is_empty_when_there_are_none():
    from app.ui.presenter import folders_removed_message, leftovers_text

    assert leftovers_text(0) == ""
    assert "342 items" in leftovers_text(342)
    assert "1 item " in folders_removed_message({"files": 1, "folders": [r"D:\Docs"]})


# --- the folder list -----------------------------------------------------------


def test_remove_asks_the_page_and_keeps_the_row_until_told(qtbot):
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    box.set_roots([r"D:\Docs", r"E:\Old"])
    box.confirms_removal = True
    asked: list = []
    box.remove_requested.connect(asked.append)

    box.tree.setCurrentItem(box.tree.topLevelItem(0))
    box._remove_root()

    assert asked == [[r"D:\Docs"]]
    assert box.current_roots() == [r"D:\Docs", r"E:\Old"]
    box.remove_roots(["d:/docs"])
    assert box.current_roots() == [r"E:\Old"]


def test_the_leftovers_line_shows_only_when_there_are_some(qtbot):
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    box.show()
    assert not box.clear_leftovers.isVisible()
    box.set_leftovers(5)
    assert box.clear_leftovers.isVisible() and "5 items" in box.leftovers.text()
    box.set_leftovers(0)
    assert not box.leftovers.isVisible()
