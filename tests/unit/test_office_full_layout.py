r"""Workspace §4e: "Show full layout", on demand.

Layer: L5

A text-rendered Office/ODF preview can ask, once, to see the real thing -
converted to PDF through the **existing** LibreOffice converter route and
cached beside the index, keyed by a hash of the file's own bytes. Never at
index time: paid once per document a user actually opens, and never again
after that, which is the point of the cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ui.preview_loader import (
    KIND_NONE,
    KIND_PDF,
    OFFICE_CONVERTER_MISSING_NOTE,
    ensure_office_pdf,
    office_converter_available,
    office_pdf_cache_path,
)


# --- detecting the converter --------------------------------------------------

def test_available_when_soffice_is_present():
    assert office_converter_available({"soffice": "C:/LibreOffice/soffice.exe"})


def test_available_when_only_libreoffice_is_present():
    assert office_converter_available({"libreoffice": "/usr/bin/libreoffice"})


def test_not_available_when_neither_is_present():
    assert not office_converter_available({"soffice": None, "libreoffice": None})
    assert not office_converter_available({})


def test_the_sentence_names_the_fix():
    assert "LibreOffice" in OFFICE_CONVERTER_MISSING_NOTE
    assert "winget" in OFFICE_CONVERTER_MISSING_NOTE


# --- the cache key -------------------------------------------------------------

def test_the_cache_path_is_keyed_by_content_not_by_name(tmp_path: Path):
    """Two different filenames, the same bytes, share a cache entry - and a
    cache is only worth having if a rename does not invalidate it."""
    root = tmp_path / "cache"
    one = tmp_path / "report.docx"
    two = tmp_path / "copy of report.docx"
    one.write_bytes(b"same bytes")
    two.write_bytes(b"same bytes")

    assert (office_pdf_cache_path(one, cache_root=root)
           == office_pdf_cache_path(two, cache_root=root))


def test_different_content_gets_a_different_cache_entry(tmp_path: Path):
    root = tmp_path / "cache"
    one = tmp_path / "a.docx"
    two = tmp_path / "b.docx"
    one.write_bytes(b"version one")
    two.write_bytes(b"version two, edited")

    assert (office_pdf_cache_path(one, cache_root=root)
           != office_pdf_cache_path(two, cache_root=root))


def test_the_cache_path_lives_under_the_given_root(tmp_path: Path):
    root = tmp_path / "cache"
    target = tmp_path / "report.docx"
    target.write_bytes(b"stub")

    path = office_pdf_cache_path(target, cache_root=root)

    assert path.parent == root
    assert path.suffix == ".pdf"


# --- ensure_office_pdf: the cache hit ------------------------------------------

def test_a_cache_hit_never_calls_the_converter(tmp_path, monkeypatch):
    """The whole point of caching: paid once, never again."""
    import app.ui.preview_loader as module

    root = tmp_path / "cache"
    root.mkdir()
    target = tmp_path / "report.docx"
    target.write_bytes(b"stub")
    cached = office_pdf_cache_path(target, cache_root=root)
    cached.write_bytes(b"%PDF-1.4 already converted")

    monkeypatch.setattr(module, "office_pdf_cache_path",
                        lambda _p, cache_root=None: cached)

    def explode(*_a, **_kw):
        raise AssertionError("the converter must not run on a cache hit")

    monkeypatch.setattr("app.extract.converter.convert", explode)

    preview = ensure_office_pdf(str(target))

    assert preview.kind == KIND_PDF
    assert preview.path == str(cached)


# --- ensure_office_pdf: no converter on this machine ---------------------------

def test_no_converter_reports_the_fix_rather_than_failing_silently(
    tmp_path, monkeypatch
):
    import app.ui.preview_loader as module

    root = tmp_path / "cache"
    target = tmp_path / "report.docx"
    target.write_bytes(b"stub")
    monkeypatch.setattr(module, "office_pdf_cache_path",
                        lambda _p, cache_root=None: root / "x.pdf")
    monkeypatch.setattr("app.extract.converter.available_binaries",
                        lambda: {"soffice": None, "libreoffice": None})

    preview = ensure_office_pdf(str(target))

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert preview.error.code == "ERR_CONVERTER_MISSING"


# --- ensure_office_pdf: a fresh conversion --------------------------------------

def test_a_fresh_conversion_is_cached_for_next_time(tmp_path, monkeypatch):
    import app.ui.preview_loader as module

    root = tmp_path / "cache"
    target = tmp_path / "report.docx"
    target.write_bytes(b"stub")
    cache_file = root / "x.pdf"
    monkeypatch.setattr(module, "office_pdf_cache_path",
                        lambda _p, cache_root=None: cache_file)
    monkeypatch.setattr("app.extract.converter.available_binaries",
                        lambda: {"soffice": "C:/LibreOffice/soffice.exe"})

    produced = tmp_path / "produced.pdf"
    produced.write_bytes(b"%PDF-1.4 fresh conversion")

    class _FakeResult:
        def __init__(self) -> None:
            self.path = produced

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr("app.extract.converter.convert",
                        lambda *_a, **_kw: _FakeResult())

    preview = ensure_office_pdf(str(target))

    assert preview.kind == KIND_PDF
    assert cache_file.is_file()
    assert cache_file.read_bytes() == b"%PDF-1.4 fresh conversion"


def test_a_converter_failure_is_reported_not_raised(tmp_path, monkeypatch):
    import app.ui.preview_loader as module
    from app.core.errors import AppErrorException, make_error

    root = tmp_path / "cache"
    target = tmp_path / "report.docx"
    target.write_bytes(b"stub")
    monkeypatch.setattr(module, "office_pdf_cache_path",
                        lambda _p, cache_root=None: root / "x.pdf")
    monkeypatch.setattr("app.extract.converter.available_binaries",
                        lambda: {"soffice": "C:/LibreOffice/soffice.exe"})

    def refuse(*_a, **_kw):
        raise AppErrorException(make_error(
            "ERR_CONVERTER_FAILED", "extract.converter", path=str(target)))

    monkeypatch.setattr("app.extract.converter.convert", refuse)

    preview = ensure_office_pdf(str(target))

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert preview.error.code == "ERR_CONVERTER_FAILED"


def test_ensure_office_pdf_never_raises_for_nonsense_input():
    for path in ("", "   ", "Z:/not/mounted/x.docx"):
        preview = ensure_office_pdf(path)
        assert preview is not None


# --- the meta flag `_extracted` carries ----------------------------------------

def test_extracted_previews_carry_whether_a_converter_was_found(
    tmp_path, monkeypatch
):
    """§4e's detection travels with the preview, computed on the same worker
    that already reads the file - never a fresh probe on the UI thread."""
    import app.ui.preview_loader as module

    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Northern pump station commissioning report.")
    path = tmp_path / "report.docx"
    document.save(path)

    monkeypatch.setattr(module, "_office_converter_default", lambda: True)
    preview = module.load_preview(str(path))
    assert preview.meta["office_converter_available"] is True

    monkeypatch.setattr(module, "_office_converter_default", lambda: False)
    preview = module.load_preview(str(path))
    assert preview.meta["office_converter_available"] is False


# --- the pop-out window ----------------------------------------------------------

def _qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _Row:
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = Path(path).name
        self.page = 0


def test_the_button_is_hidden_for_a_plain_text_file(tmp_path):
    _qapp()
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview_window import PreviewWindow

    txt = tmp_path / "notes.txt"
    txt.write_text("hello", encoding="utf-8")
    window = PreviewWindow(_Row(str(txt)), state={})

    window._loaded(Preview(kind=KIND_TEXT, path=str(txt), title="notes.txt",
                           body="hello", meta={}), window._generation)

    assert window.full_layout_button.isHidden()


def test_the_button_appears_when_a_converter_is_detected(tmp_path):
    _qapp()
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview_window import PreviewWindow

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"stub")
    window = PreviewWindow(_Row(str(docx)), state={})

    window._loaded(
        Preview(kind=KIND_TEXT, path=str(docx), title="report.docx",
               body="text", meta={"extracted": True,
                                  "office_converter_available": True}),
        window._generation,
    )

    assert not window.full_layout_button.isHidden()


def test_the_note_carries_the_fix_when_no_converter_is_found(tmp_path):
    _qapp()
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview_window import PreviewWindow

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"stub")
    window = PreviewWindow(_Row(str(docx)), state={})

    window._loaded(
        Preview(kind=KIND_TEXT, path=str(docx), title="report.docx",
               body="text", meta={"extracted": True,
                                  "office_converter_available": False}),
        window._generation,
    )

    assert window.full_layout_button.isHidden()
    assert "LibreOffice" in window.note.text()


def test_clicking_the_button_never_touches_the_original_path(tmp_path, monkeypatch):
    """§6's rule again: `self._path` is what "Open the real file" and "Show
    in folder" use, and must survive a full-layout conversion untouched."""
    _qapp()
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview_window import PreviewWindow

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"stub")
    window = PreviewWindow(_Row(str(docx)), state={})
    window._loaded(
        Preview(kind=KIND_TEXT, path=str(docx), title="report.docx",
               body="text", meta={"extracted": True,
                                  "office_converter_available": True}),
        window._generation,
    )
    original_path = window._path

    cached_pdf = tmp_path / "converted.pdf"
    cached_pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        "app.ui.preview_loader.ensure_office_pdf",
        lambda _p: Preview(kind=KIND_PDF, path=str(cached_pdf), title="report.docx"),
    )
    monkeypatch.setattr(window, "_render", lambda: None)

    window.full_layout_button.click()
    import time

    for _ in range(50):
        _qapp().processEvents()
        if window._display_path == str(cached_pdf):
            break
        time.sleep(0.02)

    assert window._path == original_path
    assert window._display_path == str(cached_pdf)


def test_nothing_in_the_window_still_opens_a_file_for_writing():
    """The guard from test_preview_window.py, re-asserted after this item's
    own change - the cache write lives in preview_loader.py, not here."""
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "widgets"
             / "preview_window.py").read_text(encoding="utf-8")
    for writing in ("write_text(", "write_bytes(", "shutil.", "os.remove",
                    "unlink(", '"w"', "'w'"):
        assert writing not in source, f"preview_window.py has {writing}"
