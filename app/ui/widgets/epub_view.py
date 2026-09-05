r"""An EPUB, shown as chapters. Workspace §4c.

Layer: L5

**A chapter list beside the existing HTML renderer.** An EPUB is a zip of
XHTML files, and `QTextBrowser` already renders sanitised HTML for
`KIND_HTML` - this widget is the two halves put together, plus a list to
move between chapters, which the index's own flattened text has no place
for.

Every chapter arrives already sanitised (`preview_loader._epub_preview`
cleans each one through the same cleaner an email uses), so nothing here
does anything a `QTextBrowser` would not do for any other HTML preview.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidget, QSplitter, QTextBrowser, QVBoxLayout, QWidget

__all__ = ["EpubView"]


class EpubView(QWidget):
    """A chapter list on the left, the selected chapter's text on the right."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._chapters: list[Any] = []

        self.list = QListWidget()
        self.list.setAccessibleName("Chapters")
        self.list.currentRowChanged.connect(self._show_chapter)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setOpenLinks(False)
        self.browser.setAccessibleName("Chapter text")
        # §2f, the same rule everywhere else in this order: view-only still
        # means copy-out works.
        self.browser.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.list)
        split.addWidget(self.browser)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

    def show_chapters(self, chapters: Any) -> None:
        """Rebuild the list from `EpubChapter` objects. UI thread; no I/O -
        every chapter's markup already arrived from the worker."""
        self._chapters = list(chapters or [])
        self.list.blockSignals(True)
        self.list.clear()
        for chapter in self._chapters:
            self.list.addItem(str(getattr(chapter, "title", "") or "Chapter"))
        self.list.blockSignals(False)

        if self._chapters:
            self.list.setCurrentRow(0)
            self._show_chapter(0)
        else:
            self.browser.setHtml("")

    def _show_chapter(self, row: int) -> None:
        if 0 <= row < len(self._chapters):
            self.browser.setHtml(self._chapters[row].html)
        else:
            self.browser.setHtml("")
