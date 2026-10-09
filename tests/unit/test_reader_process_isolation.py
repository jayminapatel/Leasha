"""Work order `reader-process-isolation`: the index process never dies on a file.

Layer: L2/L3

The owner's overnight run of 2026-10-08/09 died with an access violation inside
the PDF library on a PDF inside a zip. A native fault cannot be caught; it can
only be kept in a process of its own. This order puts the PDF reader on the
reader-process list (OCR stays in the parent), routes archive members through
the thread's reader process, and switches reader processes on by default.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.extract import archive, base
from app.extract.base import Document, extract
from app.index.read_process import PROCESS_READERS, ReaderProcess, reads_in_process


@pytest.fixture
def reader():
    process = ReaderProcess(low_priority=False)
    try:
        yield process
    finally:
        process.close()


@pytest.fixture(autouse=True)
def _no_member_reader_left_behind():
    yield
    archive.set_member_reader(None)


def _text_pdf(path: Path, text: str) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(str(path))
    document.close()
    return path


def _blank_pdf(path: Path) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    document.new_page()
    document.save(str(path))
    document.close()
    return path


# --- 1b: PDF is read in the reader process, and the child never OCRs -----------------------

def test_pdf_is_on_the_list_and_the_zip_reader_is_not(tmp_path):
    assert "PdfExtractor" in PROCESS_READERS
    pdf = _text_pdf(tmp_path / "report.pdf", "Pump station drawings, revision C.")
    assert reads_in_process(pdf)
    (tmp_path / "backup.zip").write_bytes(b"x")
    assert not reads_in_process(tmp_path / "backup.zip"), "the zip reader stays on the thread"


def test_a_pdf_read_in_a_child_matches_the_thread(tmp_path, reader):
    pdf = _text_pdf(tmp_path / "report.pdf", "Pump station drawings, revision C.")
    local = [(d.key, d.source_kind, [c.text for c in base_chunks(d)]) for d in extract(pdf)]
    remote = [(d.key, d.source_kind, [c["text"] for c in chunks]) for d, chunks in reader.read(pdf)]
    assert remote == local and "Pump station" in remote[0][2][0]


def base_chunks(document):
    from app.extract import chunk_document

    return list(chunk_document(document))


def test_in_a_reader_process_the_pdf_reader_never_ocrs(tmp_path, monkeypatch):
    """Inside the child the OCR models must never load: `_ocr_pages` and
    `_ocr_specific_pages` decline, and a scanned PDF raises `ERR_NO_TEXT_LAYER`
    for the pictures pass to read back in the parent - as the text-first pass
    already does."""
    from app.core.errors import AppErrorException
    from app.extract import pdf as pdf_module

    def never(*_a, **_k):
        raise AssertionError("OCR was called inside a reader process")

    monkeypatch.setattr(pdf_module, "_pdf_ocr_pages", lambda: 20)       # budget says yes
    monkeypatch.setattr(pdf_module, "_pictures_held", lambda: False)    # the images pass
    monkeypatch.setattr("app.extract.ocr.ocr_image", never)
    monkeypatch.setattr("app.extract.ocr.available", lambda: True)
    base.set_reader_process(True)
    try:
        assert base.in_reader_process()
        with pytest.raises(AppErrorException) as caught:
            list(extract(_blank_pdf(tmp_path / "scan.pdf")))
        assert caught.value.error.code == "ERR_NO_TEXT_LAYER"
    finally:
        base.set_reader_process(False)
    assert not base.in_reader_process()


def test_the_child_itself_knows_its_role(tmp_path, reader):
    """The flag is set by `read_process.main` in the child, not by the test."""
    pdf = _blank_pdf(tmp_path / "scan.pdf")
    from app.core.errors import AppErrorException

    with pytest.raises(AppErrorException) as caught:
        list(reader.read(pdf))
    assert caught.value.error.code == "ERR_NO_TEXT_LAYER"


# --- 2a-2c: archive members go through the thread's reader process -------------------------

def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_read_raw_returns_documents_the_thread_would_build(tmp_path, reader):
    note = tmp_path / "note.txt"
    note.write_text("Lava is hot.\n\nVolcanoes erupt. " * 20, encoding="utf-8")
    local = list(extract(note))
    remote = list(reader.read_raw(note))
    assert [(d.text, [(s.text, s.page) for s in d.segments]) for d in remote] == \
        [(d.text, [(s.text, s.page) for s in d.segments]) for d in local]
    assert remote[0].path == note and remote[0].source_kind == local[0].source_kind


def test_a_member_on_the_list_is_read_by_the_member_reader(tmp_path):
    seen: list[str] = []

    def fake_member_reader(temp: Path):
        seen.append(temp.name)
        yield Document(path=temp, text="from the child", source_kind="file")

    archive.set_member_reader(fake_member_reader)
    z = _zip(tmp_path / "pack.zip", {"a.txt": b"alpha alpha", "b.txt": b"beta beta"})
    documents = list(extract(z))
    assert sorted(seen) == ["a.txt", "b.txt"], "both members went through the hook"
    assert all(d.text == "from the child" for d in documents)
    assert all(d.meta.get("inside_archive") == str(z) for d in documents)


def test_without_a_member_reader_members_are_read_on_the_thread(tmp_path):
    archive.set_member_reader(None)
    z = _zip(tmp_path / "pack.zip", {"a.txt": b"alpha alpha"})
    documents = list(extract(z))
    assert documents and "alpha" in documents[0].text


def test_a_member_that_kills_the_child_costs_that_member_only(tmp_path, reader):
    """2c. The child dies on the second member; the first and third are indexed,
    the second is recorded by name under the archive with
    `ERR_READER_PROCESS_ENDED`, and the thread has a fresh child for the third."""
    members = {"a.txt": b"alpha alpha", "boom.txt": b"bang", "c.txt": b"gamma"}
    z = _zip(tmp_path / "pack.zip", members)

    def member_reader(temp: Path):
        if temp.name == "boom.txt":
            stream = reader.read_raw(temp)
            reader._proc.kill()                        # the native fault, stood in for
            yield from stream
            return
        yield from reader.read_raw(temp)

    archive.set_member_reader(member_reader)
    documents = list(extract(z))
    texts = {d.meta.get("member"): d for d in documents}
    assert "alpha" in texts["a.txt"].text and "gamma" in texts["c.txt"].text
    boom = texts["boom.txt"]
    assert boom.meta.get("contents_read") is False, "recorded by name, not read"
    assert any(w.code == "ERR_READER_PROCESS_ENDED" for w in boom.warnings), boom.warnings
    assert reader.started == 2, "a fresh child for the member after the crash"


# --- 1a: the switch is on by default -------------------------------------------------------

def test_reader_processes_are_on_unless_switched_off(tmp_path):
    from app.core.config import load_settings

    env = tmp_path / ".env"
    (tmp_path / "data").mkdir()
    env.write_text(f"DATA_PATH={(tmp_path / 'data').as_posix()}\n", encoding="utf-8")
    assert load_settings(env, create_dirs=True, check_writable=False).index_read_processes is True
    env.write_text(f"DATA_PATH={(tmp_path / 'data').as_posix()}\nINDEX_READ_PROCESSES=false\n",
                   encoding="utf-8")
    assert load_settings(env, create_dirs=True, check_writable=False).index_read_processes is False


def test_the_registry_default_matches(tmp_path):
    from app.core.settings_registry import by_key

    setting = by_key("INDEX_READ_PROCESSES")
    assert setting is not None and setting.default is True
    assert "skipped" in setting.help.lower() or "not the run" in setting.help.lower()
