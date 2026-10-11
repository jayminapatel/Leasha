r"""Order 1j section 3: a message stored twice in one archive leaves the index, and stays out.

Layer: L3

2026-10-11, the owner: "yes remove the duplicates". Measured on the owner's
index: 3,458 extra copies of messages in 3,313 groups, plus 344 attachment
rows, each group inside one `.pst`. "The same message" is D2: the same
archive, text, subject, sender and sent time - a shared text alone is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extract import email_pst
from app.index import pipeline as pipeline_module
from app.index.mail_duplicates import find_mail_duplicates, remove_mail_duplicates, summary_lines
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_pst_folder_resume import CountingFolder
from tests.unit.test_pst_libpff import FakeAttachment, FakeMessage, install_fake, read_contents_of

TEXT = "Two valves need replacing before the autumn shutdown."


def _tree():
    twice = [FakeMessage(identifier=101, plain=TEXT,
                         attachments=[FakeAttachment("valves.txt", b"Valve list: V-12, V-14.")]),
             FakeMessage(identifier=102, plain=TEXT,
                         attachments=[FakeAttachment("valves.txt", b"Valve list: V-12, V-14.")])]
    # The same words under another subject: a different message, and it stays.
    other = FakeMessage(identifier=103, plain=TEXT, subject="Shutdown reminder")
    inbox = CountingFolder("Inbox", messages=[*twice, other])
    return CountingFolder("Top of Personal Folders", children=[inbox])


@pytest.fixture()
def archive(tmp_path: Path, monkeypatch) -> Path:
    from tests.unit.test_index_freshness import write_aged

    read_contents_of(monkeypatch, ".txt")
    monkeypatch.setattr(email_pst.PstExtractor, "backend", email_pst.PstBackend.AUTO)
    monkeypatch.setattr(email_pst.PstExtractor, "backends", {})
    monkeypatch.setattr(email_pst.PstExtractor, "session_factory", None)
    monkeypatch.setattr(pipeline_module, "RESUME_PERSIST_S", 0.0)
    root = tmp_path / "mail"
    root.mkdir()
    write_aged(root / "2019.pst", b"!BDN" + b"\0" * 4092)
    install_fake(monkeypatch, _tree())
    return root


def _index(db: Path, root: Path, *, force: bool = False):
    from tests.unit.test_index_freshness import NullVectors, fake_embedder

    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, force=force)
        return Pipeline(store, NullVectors(), fake_embedder(), config).run()


def _messages(db: Path) -> list[tuple[str, str]]:
    with SqliteStore(db) as store:
        return sorted((str(r[0]), str(r[1] or "")) for r in store.conn.execute(
            "SELECT f.path, m.subject FROM files f JOIN messages m ON m.file_id = f.id"))


def _attachments(db: Path) -> list[str]:
    with SqliteStore(db) as store:
        return [str(r[0]) for r in store.conn.execute(
            "SELECT path FROM files WHERE path LIKE '%/attachments/%'")]


def test_a_message_in_the_archive_twice_is_indexed_once_with_its_attachment(tmp_path, archive):
    db = tmp_path / "i.db"
    stats = _index(db, archive)
    messages = _messages(db)
    assert len(messages) == 2, messages                     # one copy, and the other subject
    assert {subject for _key, subject in messages} == {"Site survey findings", "Shutdown reminder"}
    assert len(_attachments(db)) == 1, "the second copy's attachment was written"
    # The copy is counted; its attachment may already be passed over by its own
    # text (the reader's per-message dedupe) before it reaches the write.
    assert stats.mail_repeats >= 1


def test_a_read_again_does_not_bring_the_copy_back(tmp_path, archive, monkeypatch):
    db = tmp_path / "i.db"
    _index(db, archive)
    install_fake(monkeypatch, _tree())
    _index(db, archive, force=True)
    assert len(_messages(db)) == 2
    assert len(_attachments(db)) == 1


class _Vectors:
    def __init__(self):
        self.deleted: list[int] = []

    def delete_by_file_ids(self, ids):
        self.deleted.extend(int(i) for i in ids)


def _seed_copies(store: SqliteStore) -> dict[str, int]:
    """Copies written as an index made before the guard would have left them."""
    ids = {}
    for key, subject, sent in (("pst://9/1", "Plan", 100), ("pst://9/2", "Plan", 100),
                               ("pst://9/3", "Plan", 200),            # another time: stays
                               ("pst://8/1", "Plan", 100)):           # another archive: stays
        file_id = store.upsert_file(key, size_bytes=1, mtime_ns=1, content_hash="same-text",
                                    status="INDEXED", source_kind="pst_message")
        store.set_message(file_id, store_path=f"D:/Mail/{key[6]}.pst", subject=subject,
                          sender="sam@example.com", sent_at=sent)
        ids[key] = file_id
    ids["attachment"] = store.upsert_file("pst://9/2/attachments/plan.pdf", size_bytes=1,
                                          mtime_ns=1, content_hash="pdf", status="INDEXED",
                                          source_kind="pst_message")
    return ids


def test_the_dry_run_names_the_copy_and_its_attachment_and_changes_nothing(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        ids = _seed_copies(store)
        found = find_mail_duplicates(store)
        assert found.message_ids == [ids["pst://9/2"]]
        assert found.attachment_ids == [ids["attachment"]]
        words = "\n".join(summary_lines(found))
        assert "1 extra copies of 1 messages, and 1 attachment rows" in words
        assert "pst://9/1  ->  pst://9/2" in words
        assert "not touched" in words
        assert store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5


def test_removing_takes_the_copy_its_attachment_and_their_vectors_only(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        ids = _seed_copies(store)
        vectors, pictures = _Vectors(), _Vectors()
        removed = remove_mail_duplicates(store, vectors, pictures, run_lock_owner="test",
                                         lock_dir=tmp_path, name="dedupe-test")
        assert removed == 2
        left = {str(r[0]) for r in store.conn.execute("SELECT path FROM files")}
        assert left == {"pst://9/1", "pst://9/3", "pst://8/1"}
        assert sorted(vectors.deleted) == sorted([ids["pst://9/2"], ids["attachment"]])
        assert find_mail_duplicates(store).all_ids == []
