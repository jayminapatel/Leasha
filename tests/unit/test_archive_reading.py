r"""Reading inside a `.zip`, and every way one can take a run down.

Layer: L2

From `docs/WORKORDER-zip-archives.md` §3 and §5. §1a made every archive findable
by name; this makes its *contents* findable. The gate in §6 - *"do this after
the first full index"* - was waived by the owner.

**§4 is most of this file, and its opening line is the reason:** *"Every one of
these is a way an archive takes down a run that would otherwise have finished."*
A five-day index over 1.5TB is not allowed to die on one crafted file, so each
guard has a test asserting the run continues and the archive says what it
refused.

The order asks for these specific cases and each is here:

* a zip of three documents yields three documents, with the right paths;
* a nested zip is read at depth 2 and refused at depth 3, **by code**;
* a bomb is refused **without allocating its expansion**;
* the per-archive byte budget stops a thousand-member archive;
* an encrypted member is skipped and its siblings are still read;
* a traversal name cannot write outside the temp directory;
* a member with no extractor is recorded by name rather than dropped;
* a corrupt zip is a skip and the run continues.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.extract.archive import (
    BOMB_MIN_BYTES,
    MAX_DEPTH,
    ArchiveExtractor,
    _Budget,
    read_archive,
    safe_member_name,
)


def _zip(path: Path, members: dict, *, compress=zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compress) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


def _inside(document, archive: Path) -> str:
    """The member's path within the archive, for readable assertions."""
    key = str(document.virtual_path or "")
    return key[len(str(archive)):].lstrip("/\\")


def _codes(document) -> list[str]:
    return [warning.code for warning in document.warnings]


# --- the ordinary case ------------------------------------------------------

def test_a_zip_of_documents_yields_one_document_each(tmp_path):
    archive = _zip(tmp_path / "reports.zip", {
        "q3/notes.txt": "Northern pump station commissioning report",
        "q3/summary.md": "# The valve was replaced in autumn",
    })

    found = list(read_archive(archive))

    assert sorted(_inside(d, archive) for d in found) == [
        "q3/notes.txt", "q3/summary.md"]
    assert any("commissioning" in d.text for d in found)


def test_a_member_is_keyed_as_container_slash_member(tmp_path):
    r"""**The convention `.pst` already established.** The UI, the skip ledger
    and the change detector all handle `container/member` today, which is why
    none of that had to be built again."""
    archive = _zip(tmp_path / "backup.zip", {"q3/report.txt": "text"})

    document = next(iter(read_archive(archive)))

    assert document.virtual_path == f"{archive}/q3/report.txt"
    assert document.path == archive


def test_a_directory_entry_is_not_a_document(tmp_path):
    """Zips record folders as zero-length members; counting them would put an
    empty document in the index for every directory in the tree."""
    archive = _zip(tmp_path / "tree.zip", {"src/": b"", "src/main.py": "x = 1"})

    assert len(list(read_archive(archive))) == 1


def test_the_extractor_registry_is_reused_rather_than_reimplemented(tmp_path):
    r"""An archive reader with its own idea of how to read a `.docx` is a
    second, worse copy of Layer 2 that drifts from the first the moment either
    is fixed."""
    archive = _zip(tmp_path / "mixed.zip", {"a.txt": "plain text here"})

    document = next(iter(read_archive(archive)))

    assert "plain text here" in document.text


# --- §4.2 nesting -----------------------------------------------------------

def test_a_nested_archive_is_read_at_depth_two(tmp_path):
    inner = _zip(tmp_path / "inner.zip", {"deep.txt": "the deepest text"})
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(inner, "inner.zip")

    found = list(read_archive(outer))

    assert [_inside(d, outer) for d in found] == ["inner.zip/deep.txt"]
    assert "deepest" in found[0].text


def test_depth_three_is_refused_by_code(tmp_path):
    r"""**Absolute, not adaptive.** A zip whose member is itself exists, and
    quines exist; a limit that keeps going "while it looks reasonable" does not
    terminate on either."""
    inner = _zip(tmp_path / "inner.zip", {"deep.txt": "text"})
    middle = tmp_path / "middle.zip"
    with zipfile.ZipFile(middle, "w") as archive:
        archive.write(inner, "inner.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(middle, "middle.zip")

    found = list(read_archive(outer))

    assert [_codes(d) for d in found] == [["ERR_ARCHIVE_TOO_DEEP"]]
    assert MAX_DEPTH == 2


def test_a_refusal_is_never_fatal(tmp_path):
    """`SKIP_CONTINUE` on every one of them: one bad archive must not end a
    five-day index."""
    inner = _zip(tmp_path / "inner.zip", {"a.txt": "t"})
    middle = tmp_path / "middle.zip"
    with zipfile.ZipFile(middle, "w") as archive:
        archive.write(inner, "inner.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(middle, "middle.zip")

    warning = list(read_archive(outer))[0].warnings[0]

    assert not warning.is_fatal


# --- §4.1 bombs and budgets -------------------------------------------------

def test_a_bomb_is_refused_from_the_header(tmp_path):
    r"""**Without allocating its expansion.** Both sizes are in the central
    directory, so a claimed gigabyte is declined without being read - the work
    order's *"check the header, not the read"*."""
    archive = _zip(tmp_path / "bomb.zip", {
        "big.bin": b"\x00" * (8 * BOMB_MIN_BYTES),
        "ordinary.txt": "real text that compresses normally. " * 40,
    })

    found = {_inside(d, archive): d for d in read_archive(archive)}

    assert _codes(found["big.bin"]) == ["ERR_ARCHIVE_TOO_LARGE"]
    assert _codes(found["ordinary.txt"]) == []
    assert "real text" in found["ordinary.txt"].text


def test_a_small_compressible_member_is_not_a_bomb(tmp_path):
    r"""A 5KB log of one repeated line compresses a thousand to one and is
    harmless. A bomb is dangerous because of what it expands *to*, so the
    expansion qualifies it - the same floor `index/scan.py` uses."""
    archive = _zip(tmp_path / "logs.zip", {
        "app.log": "the same line over and over\n" * 200})

    assert _codes(next(iter(read_archive(archive)))) == []


def test_the_archive_budget_stops_a_thousand_small_members(tmp_path):
    r"""**A thousand members of 50MB each passes every per-member check** and
    still costs 50GB."""
    archive = _zip(tmp_path / "many.zip", {
        f"f{n}.txt": "word " * 5_000 for n in range(6)})

    found = list(read_archive(archive, budget=_Budget(30_000)))

    read = [d for d in found if not _codes(d)]
    refused = [d for d in found if _codes(d) == ["ERR_ARCHIVE_TOO_LARGE"]]
    assert read and refused, "some read, then the budget bit"


def test_the_budget_is_shared_with_nested_archives(tmp_path):
    r"""A counter each would let three levels of nesting cost three times the
    ceiling, which is the whole thing the ceiling exists to stop."""
    inner = _zip(tmp_path / "inner.zip", {"big.txt": "word " * 5_000})
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(inner, "inner.zip")
        archive.writestr("also.txt", "word " * 5_000)

    budget = _Budget(30_000)
    list(read_archive(outer, budget=budget))

    assert budget.left < 30_000


# --- §4.3 encryption --------------------------------------------------------

def test_an_encrypted_member_is_skipped_and_its_siblings_are_read(tmp_path):
    r"""Read from the flag bits **before** anything is attempted: trying and
    failing is slower and says less. And this never prompts and never guesses -
    somebody with the password can extract it by hand."""
    path = tmp_path / "locked.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("secret.txt", "x" * 100)
        archive.writestr("open.txt", "readable text")
    raw = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = raw.find(signature)
        raw[at + offset] |= 0x1
    path.write_bytes(bytes(raw))

    found = {_inside(d, path): d for d in read_archive(path)}

    assert _codes(found["secret.txt"]) == ["ERR_ARCHIVE_ENCRYPTED"]
    assert "readable text" in found["open.txt"].text


# --- §4.4 traversal ---------------------------------------------------------

@pytest.mark.parametrize("name", [
    r"..\..\Windows\System32\evil.dll",
    "../../etc/passwd",
    "/etc/passwd",
    r"C:\Windows\evil.dll",
    "a/../../b.txt",
])
def test_a_traversing_name_is_refused(name: str) -> None:
    r"""**Refused, not sanitised into silence.** A member called
    `..\..\Windows\System32\evil.dll` is not a mistake to be quietly corrected;
    it is the one thing in an archive that indicates intent."""
    assert safe_member_name(name) is None


@pytest.mark.parametrize("name,expected", [
    ("q3/report.docx", "q3/report.docx"),
    (r"q3\report.docx", "q3/report.docx"),
    ("report.docx", "report.docx"),
])
def test_an_ordinary_name_survives(name: str, expected: str) -> None:
    assert safe_member_name(name) == expected


def test_a_traversing_member_produces_no_document(tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../escape.txt", "should never be written")
        archive.writestr("fine.txt", "ordinary")

    found = list(read_archive(path))

    assert [_inside(d, path) for d in found] == ["fine.txt"]


# --- what is not read is still recorded -------------------------------------

def test_a_member_with_no_extractor_is_recorded_by_name(tmp_path):
    r"""**An archive of `.dwg` files should say what is in it.** Silence is the
    failure §1a exists to remove, and reproducing it one level down would be
    the same mistake twice."""
    archive = _zip(tmp_path / "drawings.zip", {"plans/site.dwg": b"\x00" * 40})

    document = next(iter(read_archive(archive)))

    assert _inside(document, archive) == "plans/site.dwg"
    assert document.meta.get("contents_read") is False
    assert "site.dwg" in document.text


def test_the_name_text_does_not_carry_the_containers_whole_path(tmp_path):
    r"""Otherwise `D:`, `SearchData` and every folder above it become
    searchable words on every unreadable member, and a search for a drive letter
    returns thousands of them."""
    archive = _zip(tmp_path / "backup.zip", {"q3/site.dwg": b"\x00" * 40})

    text = next(iter(read_archive(archive))).text

    assert "backup.zip" in text and "q3" in text
    assert str(tmp_path) not in text


def test_a_corrupt_archive_is_a_skip_rather_than_a_crash(tmp_path):
    r"""**A skip with its own name, not an empty result.**

    This returned `[]` until the corrupt case was traced through the pipeline.
    Yielding nothing is indistinguishable from an empty archive, so the run
    reached "extracted successfully but produced no text" and filed a damaged
    file under `ERR_NO_TEXT_LAYER` - a statement about a document that opened
    fine, and the queue `--only-ocr` reads back.

    `SKIP_CONTINUE`, so this is still one line in a report and never the run.
    """
    from app.core.errors import ActionType, AppErrorException

    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04" + b"garbage" * 20)

    with pytest.raises(AppErrorException) as raised:
        list(read_archive(path))

    assert raised.value.error.code == "ERR_ARCHIVE_UNREADABLE"
    assert raised.value.error.action_type == ActionType.SKIP_CONTINUE


def test_a_missing_archive_is_a_skip(tmp_path):
    """Deleted between the walk and the read - the same skip, not a crash."""
    from app.core.errors import AppErrorException

    with pytest.raises(AppErrorException) as raised:
        list(read_archive(tmp_path / "not-here.zip"))

    assert raised.value.error.code == "ERR_ARCHIVE_UNREADABLE"


# --- §4.6 the ceiling, and the switch ---------------------------------------

def test_the_extractor_is_registered_for_the_zip_family():
    r"""`.zip` in `resolved_extensions()` is the guard that would have caught
    the whole tier-1/tier-2 walker bug."""
    from app.extract.base import extractor_for

    for name in ("a.zip", "a.jar", "a.nupkg"):
        assert extractor_for(Path(name)) is not None


def test_office_documents_keep_their_own_extractors():
    r"""`.docx` is a zip too. Reading it here would produce one "document" per
    part of a Word file."""
    from app.extract.base import extractor_for

    chosen = extractor_for(Path("report.docx"))

    assert getattr(chosen, "name", "") != "archive"


def test_an_archive_over_the_ceiling_is_recorded_by_name(tmp_path, monkeypatch):
    """A 40GB backup zip is a decision about time, and somebody who cannot find
    what is in it deserves to know why and where the number lives."""
    # Genuinely over the ceiling: the setting is whole megabytes, so the
    # archive has to be bigger than one. Incompressible, or the file on disk
    # stays tiny and the ceiling - which is about the file, not its contents -
    # never applies.
    import os

    archive = _zip(tmp_path / "huge.zip", {"a.bin": os.urandom(2 * 1024 * 1024)},
                   compress=zipfile.ZIP_STORED)

    class Tiny:
        archive_read_inside = True
        archive_max_mb = 1

    monkeypatch.setattr("app.extract.archive._settings", lambda: Tiny())

    found = list(ArchiveExtractor().extract(archive))

    assert len(found) == 1
    assert _codes(found[0]) == ["ERR_ARCHIVE_TOO_LARGE"]


def test_reading_inside_can_be_switched_off(tmp_path, monkeypatch):
    r"""*"Off by default is wrong and on by default is dangerous"*, so it is a
    switch beside a number rather than either on its own."""
    archive = _zip(tmp_path / "a.zip", {"a.txt": "text"})

    class Off:
        archive_read_inside = False
        archive_max_mb = 100

    monkeypatch.setattr("app.extract.archive._settings", lambda: Off())

    assert list(ArchiveExtractor().extract(archive)) == []


# --- through the pipeline, which is where both real bugs were ---------------

class _NoVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _pipeline(store, root):
    import math

    from app.index.embedder import Embedder, l2_normalise
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
                for t in texts]

    return Pipeline(store, _NoVectors(), Embedder(dim=8, encoder=encode),
                    PipelineConfig(
                        walk=WalkConfig(roots=[root],
                                        extensions=frozenset({".txt", ".md", ".zip"})),
                        workers=1, min_free_gb=1))


@pytest.fixture()
def indexed(tmp_path):
    """A corpus with one archive in it, indexed once."""
    from app.storage.sqlite_store import SqliteStore

    corpus = tmp_path / "corpus"
    _zip(corpus / "backup.zip", {
        "q3/notes.txt": "Northern pump station commissioning report",
        "q3/valve.md": "# The valve was replaced during the autumn shutdown",
        "q3/plan.dwg": b"\x00" * 400,
    })
    (corpus / "loose.txt").write_text("an ordinary file", encoding="utf-8")

    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, corpus).run()
        yield store, corpus, stats


def test_a_members_contents_are_searchable(indexed):
    r"""**The whole point of §3**, and the assertion that would have caught
    both bugs found writing it.

    The extractor was registered as a class rather than an instance, so every
    call was an unbound `extract(path)` - a `TypeError` recorded as
    `ERR_UNEXPECTED` against the archive and invisible until something opened a
    zip. The unit tests above call `read_archive` directly and all passed.
    """
    store, corpus, _stats = indexed

    found = store.search_bm25("commissioning")

    # Separators normalised: the key is the container's native path plus
    # `/member`, so on Windows it reads `<corpus>\backup.zip/q3/notes.txt`.
    assert [row["path"].replace(str(corpus), "").replace("\\", "/") for row in found] == [
        r"/backup.zip/q3/notes.txt"]


def test_members_are_not_pruned_at_the_end_of_the_run(indexed):
    r"""**The second bug, and a subtle one.** `_prune_missing` deletes any
    `source_kind='file'` row whose path is not on disk - and a member's path,
    `backup.zip/q3/report.docx`, never is.

    Members written as `file` were indexed and then deleted in the same run:
    four documents indexed, one chunk, nothing findable, and no error anywhere.
    `.pst` avoids it with its own `source_kind`, which is why `ARCHIVE` exists.
    """
    store, corpus, stats = indexed

    kinds = {row["path"].replace(str(corpus), "").replace("\\", "/"): row["source_kind"]
             for row in store.conn.execute(
                 "SELECT path, source_kind FROM files")}

    assert stats.deleted == 0
    assert kinds[r"/backup.zip/q3/notes.txt"] == "archive"
    assert kinds["/loose.txt"] == "file"


def test_a_member_with_no_reader_still_gets_a_row(indexed):
    store, corpus, _stats = indexed

    paths = {row["path"].replace(str(corpus), "").replace("\\", "/")
             for row in store.conn.execute("SELECT path FROM files")}

    assert r"/backup.zip/q3/plan.dwg" in paths


def test_the_archive_itself_keeps_a_row(indexed):
    """So the next run can see it is unchanged and skip it whole."""
    store, corpus, _stats = indexed

    row = store.conn.execute(
        "SELECT source_kind FROM files WHERE path = ?",
        (str(corpus / "backup.zip"),)).fetchone()

    assert row is not None and row["source_kind"] == "archive"


def test_a_second_run_reads_nothing_again(indexed):
    """§4.5: the archive's own row carries mtime and size, so an unchanged
    archive costs one `stat` rather than every member."""
    store, corpus, _stats = indexed

    again = _pipeline(store, corpus).run()

    assert again.indexed == 0
    assert again.deleted == 0
