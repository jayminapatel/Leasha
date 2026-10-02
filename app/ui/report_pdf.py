r"""Writing a report to a PDF, on a worker.

Layer: L5

Moved here from `reports_view.py` on 2026-10-02, unchanged: that view is held
under 250 lines (`test_presenter.py`) and had gone over. `reports_view` still
imports it as `_write_pdf`, which is the name the tests replace and call.
"""

from __future__ import annotations

__all__ = ["write_pdf"]


def write_pdf(document: str, path: str) -> None:
    """PDF from the same `QTextDocument` layout `preview_window.py`'s Print
    uses, on a worker. Never on the UI thread: a long report over a large
    catalogue lays out every page before anything is written.

    `QPdfWriter` rather than a `QPrinter` set to PDF output: same layout and
    the same bytes (measured on a 200-entry report: 148,742 both ways), but it
    never touches the Windows printer subsystem - `QPrinter()` on a worker
    thread died with COM error 0x80040155 (a hard process crash, not an
    exception) in a process that already held a MainWindow.
    """
    from PyQt6.QtGui import QPdfWriter, QTextDocument

    doc = QTextDocument()
    doc.setMarkdown(document)
    writer = QPdfWriter(path)
    doc.print(writer)
