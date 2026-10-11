"""What goes to the meaning model, and in what order (the owner, 2026-10-12).

epub and svg are found by their words only - configurable, and a type taken off the list is given meaning
again without a re-read; mail is embedded before documents; the model is given passages of similar length together
also for the smaller file.
"""

from __future__ import annotations

from app.core.config import load_settings
from app.index.embedder import Embedder
from app.index.pipeline import WORDS_ONLY_EXTS
from app.storage.sqlite_store import SqliteStore


def _add(store, path, kind="file", n=2):
    fid = store.upsert_file(path=path, size_bytes=1, mtime_ns=1, source_kind=kind)
    store.replace_chunks(fid, [{"text": f"passage {i} of {path}"} for i in range(n)])
    return fid


def test_epub_and_svg_are_words_only_by_default():
    assert WORDS_ONLY_EXTS == frozenset({"svg", "epub"})


def test_marking_a_type_words_only_and_bringing_it_back_needs_no_reread(tmp_path):
    with SqliteStore(tmp_path / "k.db") as store:
        _add(store, "/b/book.epub")
        _add(store, "/b/notes.docx")
        assert store.mark_keyword_only_for_exts({"epub"}) == 2          # off the model's queue
        waiting = [cid for batch in store.unembedded_by_file() for cid, _f in batch]
        assert len(waiting) == 2                                       # only the docx
        assert store.reset_keyword_only({"epub"}) == 2                  # taken off the list: queued again
        assert sum(len(b) for b in store.unembedded_by_file()) == 4


def test_mail_is_embedded_before_documents_newest_first_within_each(tmp_path):
    with SqliteStore(tmp_path / "k.db") as store:
        old_doc = _add(store, "/d/old.docx")
        new_doc = _add(store, "/d/new.docx")
        mail = _add(store, "/m/one.eml", kind="eml")
        store.conn.execute("UPDATE files SET mtime_ns = 100 WHERE id = ?", (old_doc,))
        store.conn.execute("UPDATE files SET mtime_ns = 900 WHERE id = ?", (new_doc,))
        store.conn.execute("UPDATE files SET mtime_ns = 50 WHERE id = ?", (mail,))
        store.conn.commit()
        order = []
        for batch in store.unembedded_by_file(batch_size=1):
            for _cid, fid in batch:
                if fid not in order:
                    order.append(fid)
        assert order == [mail, new_doc, old_doc]


def test_the_smaller_model_file_is_grouped_by_length_when_the_setting_says_so():
    encoder = lambda texts: [[0.0] * 384 for _ in texts]            # noqa: E731
    on = Embedder("m", dim=384, encoder=encoder, quantised=True, group_quantised=True)
    off = Embedder("m", dim=384, encoder=encoder, quantised=True)
    for e in (on, off):
        e._quantised_in_use = True
    assert on.groups_by_length and not off.groups_by_length


def test_the_settings_have_the_defaults_the_owner_chose(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={tmp_path.as_posix()}\n", encoding="utf-8")
    s = load_settings(env)
    assert s.index_words_only_types == "svg,epub" and s.embed_group_by_length is True
    env.write_text(f"DATA_PATH={tmp_path.as_posix()}\nINDEX_WORDS_ONLY_TYPES=svg\nEMBED_GROUP_BY_LENGTH=false\n",
                   encoding="utf-8")
    s = load_settings(env)
    assert s.index_words_only_types == "svg" and s.embed_group_by_length is False
