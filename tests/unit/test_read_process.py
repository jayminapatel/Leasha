r"""Reading files in a process of their own (work order 0x item 5b).

Layer: L3

Four promises, each pinned by one test:

* **Same answer.** A reader process produces exactly the documents and
  passages the same reader produces on a thread - the switch changes where
  reading happens, never what is read.
* **A crash costs one file.** A reader process that dies part-way through a
  file gives `ERR_READER_PROCESS_ENDED` for that file, and the next file is
  read by a fresh one.
* **An abandoned file leaves nothing behind.** A file given up part-way (Stop,
  Pause) ends its reader process, so the next file can never receive the old
  file's leftover documents.
* **The whole run agrees.** The benchmark corpus indexed with the switch on
  finds the same documents, passages and vectors as with it off.
"""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import chunk_document, extract
from app.extract.base import REGISTRY, NAME_REGISTRY, extractor_for
from app.index.pipeline_bench import BenchOptions, run_pipeline_bench
from app.index.read_process import PROCESS_READERS, ReaderProcess, reads_in_process


def _mbox(path: Path, messages: int) -> Path:
    box = mailbox.mbox(str(path))
    try:
        for number in range(messages):
            message = EmailMessage()
            message["From"] = f"sender{number}@example.com"
            message["To"] = "someone@example.com"
            message["Subject"] = f"Message number {number}"
            message["Date"] = "Mon, 01 Jan 2024 10:00:00 +0000"
            message["Message-ID"] = f"<m{number}@example.com>"
            message.set_content(f"Body of message {number}. " * 40)
            box.add(message)
    finally:
        box.close()
    return path


def _eml(path: Path) -> Path:
    message = EmailMessage()
    message["From"] = "a@example.com"
    message["To"] = "b@example.com"
    message["Subject"] = "Homework"
    message["Date"] = "Tue, 02 Jan 2024 09:00:00 +0000"
    message.set_content("The homework is about volcanoes and is due on Friday.")
    path.write_bytes(bytes(message))
    return path


def _local(path: Path) -> list[tuple]:
    return [(d.key, d.source_kind, d.meta, [c.text for c in chunk_document(d)])
            for d in extract(path)]


def _remote(reader: ReaderProcess, path: Path) -> list[tuple]:
    return [(d.key, d.source_kind, d.meta, [c["text"] for c in chunks])
            for d, chunks in reader.read(path)]


@pytest.fixture
def reader():
    process = ReaderProcess(low_priority=False)
    try:
        yield process
    finally:
        process.close()


def test_every_listed_reader_is_a_registered_reader() -> None:
    """A rename cannot quietly empty the list and turn the switch into a no-op."""
    registered = {type(e).__name__ for e in list(REGISTRY.values()) + list(NAME_REGISTRY.values())}
    assert PROCESS_READERS <= registered, PROCESS_READERS - registered


def test_readers_with_process_wide_state_stay_on_the_thread(tmp_path: Path) -> None:
    """OCR, converters and the zip reader (which hands members to any reader).

    2026-10-09 (order `reader-process-isolation`): `report.pdf` left this list.
    The PDF reader's text half is pure PyMuPDF parsing - the very library that
    faulted and ended the owner's overnight run - and its OCR half declines
    inside a child (`pdf._ocr_pages`), so PDF is now read in the reader process
    and `test_reader_process_isolation.py` holds that."""
    for name in ("scan.png", "old.doc", "backup.zip"):
        path = tmp_path / name
        path.write_bytes(b"x")
        if extractor_for(path) is not None:
            assert not reads_in_process(path), name
    assert reads_in_process(_eml(tmp_path / "note.eml"))


def test_a_reader_process_reads_exactly_what_a_thread_reads(tmp_path: Path, reader) -> None:
    files = [
        _eml(tmp_path / "note.eml"),
        _mbox(tmp_path / "mail.mbox", 25),
    ]
    (tmp_path / "notes.txt").write_text("Volcanoes erupt.\n\nLava is hot. " * 50, encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Title\n\nSome *markdown* text.", encoding="utf-8")
    files += [tmp_path / "notes.txt", tmp_path / "readme.md"]
    for path in files:
        assert reads_in_process(path), path.name
        assert _remote(reader, path) == _local(path), path.name
    assert reader.started == 1, "one child for the whole run, not one per file"


def test_a_crashed_reader_costs_one_file_and_the_next_file_is_read(tmp_path: Path, reader) -> None:
    big = _mbox(tmp_path / "big.mbox", 3000)      # far more than a pipe holds
    small = _eml(tmp_path / "next.eml")

    stream = reader.read(big)
    next(stream)
    reader._proc.kill()                            # the reader "crashes"
    with pytest.raises(AppErrorException) as caught:
        for _ in stream:
            pass
    assert caught.value.error.code == "ERR_READER_PROCESS_ENDED"
    assert str(big) in caught.value.error.message

    assert _remote(reader, small) == _local(small)
    assert reader.started == 2


def test_an_abandoned_file_leaves_nothing_for_the_next_one(tmp_path: Path, reader) -> None:
    big = _mbox(tmp_path / "big.mbox", 3000)
    small = _eml(tmp_path / "next.eml")

    stream = reader.read(big)
    next(stream)
    stream.close()                                 # Stop or Pause, part-way
    assert _remote(reader, small) == _local(small)

    # And one abandoned without being closed at all.
    stream = reader.read(big)
    next(stream)
    assert _remote(reader, small) == _local(small)


def test_a_reader_error_comes_back_as_the_same_error(tmp_path: Path, reader) -> None:
    empty = tmp_path / "empty.eml"
    empty.write_bytes(b"")
    try:
        expected = _local(empty)
    except AppErrorException as exc:
        with pytest.raises(AppErrorException) as caught:
            _remote(reader, empty)
        assert caught.value.error.code == exc.error.code
    else:
        assert _remote(reader, empty) == expected


def test_the_benchmark_corpus_indexes_the_same_with_reader_processes(tmp_path: Path) -> None:
    """End to end, through the real pipeline: same documents, passages, vectors."""
    results = {}
    for switch in (False, True):
        report = run_pipeline_bench(BenchOptions(
            corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
            env_file=tmp_path / "missing.env", work_dir=tmp_path / f"work-{switch}",
            read_processes=switch))
        assert report["conditions"]["pipeline"]["read_processes"] is switch
        results[switch] = report["results"]
    for field in ("documents", "chunks", "vectors", "expected_documents"):
        assert results[True][field] == results[False][field], field
    assert results[True]["documents"] == results[True]["expected_documents"]
