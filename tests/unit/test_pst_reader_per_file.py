r"""Each Outlook archive can be read its own way, and "Read again" reads it all.

Layer: L2/L3

2026-10-07, the owner: "need a way for each pst file it can be configured how
to index outlook or direct ... there should be a reindex button on those
files". The Settings page keeps a list of mail archives with a choice per
file; what that choice means for a run is pinned here:

* **The saved choices** (`run_setup.PST_BACKENDS_STATE_KEY`) are a JSON object
  `{archive key: backend}`, keyed by `archives.normalise`, so `D:/Mail/a.pst`
  and `d:\mail\A.pst` are one archive. `auto` is the default and is never
  stored; anything unreadable is no choice at all, never an error.
* **The reader** (`PstExtractor.backend_for`) takes a file's own choice when
  there is one and the global choice otherwise, and `extract()` follows it.
* **Every run applies both** (`apply_saved_pst_backend`, called by
  `Pipeline.run`).
* **A forced run reads the whole archive again** - no message passed over by
  its read stamp, no resume part-way through - because that is what the
  owner's "reindex" asks for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.extract import email_pst, pst_libpff
from app.extract.email_pst import PstBackend, PstExtractor
from app.index import pipeline as pipeline_module
from app.index.pipeline import ARCHIVE_RESUME_PREFIX, Pipeline, PipelineConfig
from app.index.run_setup import (
    PST_BACKEND_STATE_KEY, PST_BACKENDS_STATE_KEY, apply_saved_pst_backend,
    dump_pst_backends, load_pst_backends,
)
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore


# --- the saved choices -------------------------------------------------------

def test_the_saved_choices_round_trip_and_auto_is_never_stored() -> None:
    raw = dump_pst_backends({
        r"D:\Mail\2010.pst": "outlook",
        "D:/Mail/2011.pst": "libpff",
        r"D:\Mail\2012.pst": "auto",
    })
    assert json.loads(raw) == {r"d:\mail\2010.pst": "outlook",
                               r"d:\mail\2011.pst": "libpff"}
    assert list(json.loads(raw)) == sorted(json.loads(raw)), "keys are sorted"
    assert load_pst_backends(raw) == {r"d:\mail\2010.pst": "outlook",
                                      r"d:\mail\2011.pst": "libpff"}


@pytest.mark.parametrize("raw", [
    "", "{torn", "null", "[]", '"outlook"', "42",
    '{"d:\\\\mail\\\\a.pst": "sideways"}', '{"d:\\\\mail\\\\a.pst": 3}',
    '{"d:\\\\mail\\\\a.pst": "auto"}', '{"": "outlook"}',
])
def test_garbage_is_no_choice_and_never_an_error(raw) -> None:
    assert load_pst_backends(raw) == {}


def test_load_keeps_the_good_entries_beside_the_bad() -> None:
    raw = json.dumps({"D:/Mail/a.pst": "Outlook", "D:/Mail/b.pst": "nonsense",
                      "D:/Mail/c.pst": ["libpff"]})
    assert load_pst_backends(raw) == {r"d:\mail\a.pst": "outlook"}


def test_two_spellings_of_one_archive_are_one_key() -> None:
    raw = dump_pst_backends({"D:/Mail/a.pst": "outlook"})
    assert load_pst_backends(raw) == load_pst_backends(
        dump_pst_backends({"d:\\mail\\A.pst": "outlook"}))
    reader = PstExtractor()
    reader.backends = load_pst_backends(raw)
    assert reader.backend_for(Path(r"d:\mail\A.pst")) == "outlook"
    assert reader.backend_for("D:/Mail/a.pst") == "outlook"


def test_dump_drops_what_load_would_drop() -> None:
    assert json.loads(dump_pst_backends({"a.pst": "auto", "b.pst": "x", "": "libpff"})) == {}


# --- the reader takes a file's own choice ------------------------------------

def test_backend_for_falls_back_to_the_global_choice() -> None:
    reader = PstExtractor()
    reader.backend = PstBackend.OUTLOOK
    reader.backends = {r"d:\mail\a.pst": PstBackend.LIBPFF}
    assert reader.backend_for(Path(r"D:\Mail\a.pst")) == PstBackend.LIBPFF
    assert reader.backend_for(Path(r"D:\Mail\b.pst")) == PstBackend.OUTLOOK
    assert PstExtractor().backend_for(Path("x.pst")) == PstBackend.AUTO, \
        "nothing chosen anywhere is auto"


class _FakeOutlook:
    """Stands in for `Win32ComSession`: records the archive it was asked to open."""

    opened: list[Path] = []

    def attach(self, path: Path) -> None:
        _FakeOutlook.opened.append(path)

    def close(self) -> None:
        pass


@pytest.fixture()
def routes(monkeypatch) -> dict[str, list]:
    """Both routes replaced by recorders, libpff installed (so `auto` picks it)."""
    seen: dict[str, list] = {"libpff": [], "outlook": []}

    def read_archive(path, **kwargs):
        seen["libpff"].append(Path(path))
        yield "read directly"

    def walk_session(session, **kwargs):
        seen["outlook"].append(kwargs.get("only_paths"))
        yield "read through Outlook"

    _FakeOutlook.opened = []
    monkeypatch.setattr(pst_libpff, "available", lambda: True)
    monkeypatch.setattr(pst_libpff, "read_archive", read_archive)
    monkeypatch.setattr(email_pst, "Win32ComSession", _FakeOutlook)
    monkeypatch.setattr(email_pst, "walk_session", walk_session)
    monkeypatch.setattr(email_pst, "_busy_warning", lambda path: None, raising=False)
    return seen


def test_extract_follows_each_files_own_choice(routes) -> None:
    reader = PstExtractor()
    reader.backend = PstBackend.AUTO
    reader.backends = {r"d:\mail\old.pst": PstBackend.OUTLOOK,
                       r"d:\mail\new.pst": PstBackend.LIBPFF}

    assert list(reader.extract(Path(r"D:\Mail\old.pst"))) == ["read through Outlook"]
    assert _FakeOutlook.opened == [Path(r"D:\Mail\old.pst")]
    assert list(reader.extract(Path(r"D:\Mail\new.pst"))) == ["read directly"]
    # No choice of its own: the global `auto`, which picks libpff here.
    assert list(reader.extract(Path(r"D:\Mail\other.pst"))) == ["read directly"]
    assert routes["libpff"] == [Path(r"D:\Mail\new.pst"), Path(r"D:\Mail\other.pst")]


def test_a_files_own_choice_overrides_a_global_outlook(routes) -> None:
    reader = PstExtractor()
    reader.backend = PstBackend.OUTLOOK
    reader.backends = {r"d:\mail\new.pst": PstBackend.LIBPFF}
    assert list(reader.extract(Path(r"D:\Mail\new.pst"))) == ["read directly"]
    assert list(reader.extract(Path(r"D:\Mail\old.pst"))) == ["read through Outlook"]


def test_an_ost_is_read_through_outlook_whatever_its_choice(routes) -> None:
    reader = PstExtractor()
    reader.backends = {r"d:\mail\cache.ost": PstBackend.LIBPFF}
    assert list(reader.extract(Path(r"D:\Mail\cache.ost"))) == ["read through Outlook"]
    assert routes["libpff"] == []


def _locked(path, **kwargs):
    from app.core.errors import raise_error

    raise_error("ERR_FILE_LOCKED", "extract.pst", path=str(path))
    yield  # pragma: no cover - a generator, like the real reader


def test_a_held_archive_falls_back_to_outlook_only_when_its_own_choice_is_auto(
        routes, monkeypatch) -> None:
    from app.core.errors import AppErrorException

    monkeypatch.setattr(pst_libpff, "read_archive", _locked)
    reader = PstExtractor()
    reader.backend = PstBackend.LIBPFF                 # the global choice is forced...
    reader.backends = {}
    with pytest.raises(AppErrorException):
        list(reader.extract(Path(r"D:\Mail\a.pst")))

    reader.backend = PstBackend.AUTO                   # ...and this file's choice is
    reader.backends = {r"d:\mail\a.pst": PstBackend.LIBPFF}
    with pytest.raises(AppErrorException):
        list(reader.extract(Path(r"D:\Mail\a.pst")))
    assert _FakeOutlook.opened == [], "a forced choice was quietly overridden"

    # Auto everywhere: the held archive is read through Outlook instead.
    reader.backends = {}
    assert list(reader.extract(Path(r"D:\Mail\a.pst"))) == ["read through Outlook"]


def test_a_file_chosen_as_libpff_says_so_when_libpff_is_missing(routes, monkeypatch) -> None:
    from app.core.errors import AppErrorException

    def missing(path, **kwargs):
        raise pst_libpff.LibpffUnavailable("no pypff")
        yield  # pragma: no cover

    monkeypatch.setattr(pst_libpff, "read_archive", missing)
    reader = PstExtractor()
    reader.backends = {r"d:\mail\a.pst": PstBackend.LIBPFF}
    with pytest.raises(AppErrorException) as raised:
        list(reader.extract(Path(r"D:\Mail\a.pst")))
    assert raised.value.error.code == "ERR_OUTLOOK_MISSING"
    # The same file on `auto` falls through to Outlook instead.
    reader.backends = {}
    assert list(reader.extract(Path(r"D:\Mail\a.pst"))) == ["read through Outlook"]


# --- every run applies the saved choices -------------------------------------

@pytest.fixture()
def registered():
    """The registered PST reader, its own attributes put back exactly as they
    were - a leftover instance attribute would shadow every later test's
    class-level `monkeypatch`."""
    from app.extract.base import extractor_for

    extractor = extractor_for(Path("x.pst"))
    if extractor is None:
        pytest.skip("no PST reader registered here")
    before = {name: vars(extractor)[name] for name in ("backend", "backends")
              if name in vars(extractor)}
    try:
        yield extractor
    finally:
        for name in ("backend", "backends"):
            if name in before:
                setattr(extractor, name, before[name])
            else:
                vars(extractor).pop(name, None)


def test_a_run_applies_the_global_and_the_per_file_choices(tmp_path, registered) -> None:
    with SqliteStore(tmp_path / "x.db") as store:
        store.set_state(PST_BACKEND_STATE_KEY, "outlook")
        store.set_state(PST_BACKENDS_STATE_KEY,
                        dump_pst_backends({"D:/Mail/2011.pst": "libpff"}))
        assert apply_saved_pst_backend(store) == "outlook"
    assert registered.backend == "outlook"
    assert registered.backends == {r"d:\mail\2011.pst": "libpff"}
    assert registered.backend_for(Path(r"D:\Mail\2011.pst")) == "libpff"
    assert registered.backend_for(Path(r"D:\Mail\2010.pst")) == "outlook"


def test_per_file_choices_apply_with_no_global_choice_saved(tmp_path, registered) -> None:
    registered.backend = "auto"
    with SqliteStore(tmp_path / "x.db") as store:
        store.set_state(PST_BACKENDS_STATE_KEY,
                        dump_pst_backends({"D:/Mail/2010.pst": "outlook"}))
        assert apply_saved_pst_backend(store) is None, "no global choice was saved"
    assert registered.backend == "auto"
    assert registered.backends == {r"d:\mail\2010.pst": "outlook"}


def test_a_choice_taken_away_is_gone_on_the_next_run(tmp_path, registered) -> None:
    """A long-lived window runs many times: a removed choice must not linger."""
    with SqliteStore(tmp_path / "x.db") as store:
        store.set_state(PST_BACKENDS_STATE_KEY,
                        dump_pst_backends({"D:/Mail/2010.pst": "outlook"}))
        apply_saved_pst_backend(store)
        assert registered.backends
        store.set_state(PST_BACKENDS_STATE_KEY, "")
        apply_saved_pst_backend(store)
    assert registered.backends == {}


def test_an_unreadable_store_leaves_the_reader_as_it_was(registered) -> None:
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("this database is having a day")

    registered.backend = "auto"
    registered.backends = {r"d:\mail\a.pst": "outlook"}
    assert apply_saved_pst_backend(Broken()) is None
    assert registered.backend == "auto"
    assert registered.backends == {r"d:\mail\a.pst": "outlook"}


# --- a forced run reads the whole archive again ------------------------------

@pytest.fixture()
def fake_archive(tmp_path: Path, monkeypatch) -> Path:
    """One `.pst` read through the real reader over a fake `pypff`, as
    `test_pst_folder_resume.py` does it."""
    from tests.unit.test_index_freshness import write_aged
    from tests.unit.test_pst_libpff import read_contents_of

    read_contents_of(monkeypatch, ".txt")
    monkeypatch.setattr(email_pst.PstExtractor, "backend", PstBackend.AUTO)
    monkeypatch.setattr(email_pst.PstExtractor, "backends", {})
    monkeypatch.setattr(email_pst.PstExtractor, "session_factory", None)
    monkeypatch.setattr(pipeline_module, "RESUME_PERSIST_S", 0.0)
    root = tmp_path / "mail"
    root.mkdir()
    write_aged(root / "2007.pst", b"!BDN" + b"\0" * 4092)
    return root


def _index(db: Path, root: Path, *, force: bool = False, stop_after: int = 0):
    from tests.unit.test_index_freshness import NullVectors, fake_embedder

    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                checkpoint_every=1, force=force)
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)

        def maybe_stop(stats) -> None:
            if stop_after and stats.indexed >= stop_after:
                pipeline.request_stop()

        return pipeline.run(on_progress=maybe_stop if stop_after else None)


def _spy_on_the_reader(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = pst_libpff.read_archive

    def spy(path, **kwargs):
        calls.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(pst_libpff, "read_archive", spy)
    return calls


def test_a_forced_run_does_not_carry_on_part_way(tmp_path, fake_archive, monkeypatch) -> None:
    """A run stopped part-way leaves a folder cursor. An ordinary run carries
    on from it; "Read again" (a forced run) starts at the top."""
    from tests.unit.test_pst_folder_resume import build_tree
    from tests.unit.test_pst_libpff import install_fake

    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree()["root"])
    _index(db, fake_archive, stop_after=6)
    with SqliteStore(db) as store:
        assert any(key.startswith(ARCHIVE_RESUME_PREFIX) for key in store.all_state()), \
            "the stopped run left no cursor to ignore"

    tree = build_tree()
    install_fake(monkeypatch, tree["root"])
    calls = _spy_on_the_reader(monkeypatch)
    _index(db, fake_archive, force=True)
    assert [call["resume_from"] for call in calls] == [0]
    assert tree["inbox"].fetched == 3, "the forced run skipped the folders read before"


def test_an_ordinary_run_still_carries_on_part_way(tmp_path, fake_archive, monkeypatch) -> None:
    """The other half: the cursor is ignored only when the run is forced."""
    from tests.unit.test_pst_folder_resume import build_tree
    from tests.unit.test_pst_libpff import install_fake

    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree()["root"])
    _index(db, fake_archive, stop_after=6)
    install_fake(monkeypatch, build_tree()["root"])
    calls = _spy_on_the_reader(monkeypatch)
    _index(db, fake_archive)
    assert calls and calls[0]["resume_from"] > 0


def _stamped_tree() -> dict:
    """`build_tree`, every message given a modification time - which is what
    `pst_libpff.read_stamp` reads, and the fake leaves out."""
    from tests.unit.test_pst_folder_resume import build_tree

    tree = build_tree()
    for folder in tree.values():
        for message in folder._messages:
            message._values["get_modification_time_as_integer"] = 132_000_000_000_000_000
    return tree


def test_a_forced_run_passes_over_no_message_by_its_read_stamp(
        tmp_path, fake_archive, monkeypatch) -> None:
    from tests.unit.test_pst_libpff import install_fake

    db = tmp_path / "index.db"
    install_fake(monkeypatch, _stamped_tree()["root"])
    _index(db, fake_archive)
    with SqliteStore(db) as store:
        assert store.known_read_stamps(str(fake_archive / "2007.pst")), \
            "the whole read kept no stamps, so this test would prove nothing"

    install_fake(monkeypatch, _stamped_tree()["root"])
    calls = _spy_on_the_reader(monkeypatch)
    _index(db, fake_archive, force=True)
    assert len(calls) == 1
    assert not calls[0]["known_stamps"], "a forced run was told what not to read"


def test_a_message_waiting_for_its_vectors_is_still_passed_over_by_its_stamp(
        tmp_path, fake_archive, monkeypatch) -> None:
    """2026-10-10. A text-first run (`index_two_phase`) leaves every message
    PARTIAL until the meaning step reaches it - on the owner's index, millions
    of passages behind. `known_read_stamps` asked for INDEXED only, so while
    that backlog lasted no message had a stamp to hand the reader, and an
    archive whose header Outlook moved was read again in full - bodies and
    attachments - only for every message to be found unchanged by its text.
    PARTIAL counts as read everywhere else (`_classify`, `_already_current`)."""
    import os
    import time

    from tests.unit.test_pst_libpff import install_fake

    db = tmp_path / "index.db"
    archive = fake_archive / "2007.pst"
    install_fake(monkeypatch, _stamped_tree()["root"])
    _index(db, fake_archive)
    with SqliteStore(db) as store:
        with store.write() as conn:
            changed = conn.execute(
                "UPDATE files SET status = 'PARTIAL' WHERE id IN "
                "(SELECT file_id FROM messages WHERE store_path = ?)",
                (str(archive),)).rowcount
        assert changed, "no message rows to make PARTIAL"
        assert store.known_read_stamps(str(archive)), \
            "a PARTIAL message lost its stamp"

    # Outlook mounts it: the date moves, nothing else.
    later = time.time() - 1800
    os.utime(archive, (later, later))
    tree = _stamped_tree()
    install_fake(monkeypatch, tree["root"])
    calls = _spy_on_the_reader(monkeypatch)
    _index(db, fake_archive)
    assert len(calls) == 1, "the archive was not read again, so this proves nothing"
    assert calls[0]["known_stamps"], "the reader was told nothing to pass over"
