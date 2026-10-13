r"""The one frame every main page is laid out in: a title row, the page's toolbar row, the content.

Layer: L5

**Why this exists** (owner, 2026-10-13, UI phase B): *"every screen ... look and feel like using
different apps not one app"*. Each page built its own `QVBoxLayout`: Search 16 px at the sides and
8 between rows, Files, Mail, Code and Photos the style's default (9 and 6), Browse none and 2, Tidy
the default around a splitter, Indexing 9, Chat 10 inside a frame of its own. Moving from one page
to the next moved the box, the toolbar and the list by a few pixels each time - the "different
apps" feeling in its smallest form.

**The frame.** `PageFrame(page)` is the page's layout (a `QVBoxLayout`, so `page.layout()` is still
the thing a page and `side_panel.attach` walk), with Search's margins - `MARGINS`, the page that
was redesigned last and the one the goldens hold - and `SPACING` between rows, on the 8 px rhythm
(`ROW_SPACING` in `buttons.py` is the same 8). In order, top to bottom:

1. **The title row** (`title=`), the page's heading drawn one way (`page_title`): bold, a quarter
   larger than the body text. Optional - no page shows a title row it did not have before; Browse
   draws its period with `page_title`, as it is that page's title.
2. **The toolbar row** (`toolbar=`, or `add_toolbar` later), its controls `SPACING` apart.
3. **The content**: whatever the page adds after, the list or splitter given the stretch.
4. **An optional side list**: `side_panel.attach` splits it in left of the content and records it
   here as `side`, so every page's list is attached one way.

`page_row` is the same frame for a page whose outer layout runs across (Chat: its conversation,
then the side panel's strip on the window's edge).

Words are never changed here: the frame takes the widgets a page already had.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QVBoxLayout, QWidget

__all__ = ["MARGINS", "SPACING", "TITLE_SCALE", "PageFrame", "page_row", "page_title", "frame_of"]

#: left, top, right, bottom - Search's since the redesign, now every page's.
MARGINS = (16, 12, 16, 12)
#: Between rows, and between the controls of a toolbar row.
SPACING = 8
#: A page title is this much larger than the body text, and bold.
TITLE_SCALE = 1.25


def page_title(label: Optional[QLabel] = None, text: str = "") -> QLabel:
    """Draw `label` (or a new one with `text`) as a page's title. Returns it.

    Its object name is left alone, so a page's own styling and a test that finds it by name
    still do; the `pageTitle` property says what it is.
    """
    label = label if label is not None else QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSizeF(max(9.0, font.pointSizeF()) * TITLE_SCALE)
    label.setFont(font)
    label.setProperty("pageTitle", True)
    return label


class PageFrame(QVBoxLayout):
    """A main page's layout: margins, rhythm, title, toolbar, then the content."""

    def __init__(self, page: QWidget, *, title: Any = None, toolbar: Optional[QLayout] = None,
                 spacing: int = SPACING) -> None:
        super().__init__(page)
        self.setObjectName("pageFrame")
        self.setContentsMargins(*MARGINS)
        self.setSpacing(spacing)
        # No reference back to `page`: the page holds its layout, and a layout holding the page
        # would keep a let-go page alive (`test_views_are_freed`).
        #: The title label, when the page has one.
        self.title: Optional[QLabel] = None
        #: The toolbar row, when the page has one.
        self.toolbar: Optional[QLayout] = None
        #: The side list, once `side_panel.attach` has put one on the page.
        self.side: Optional[QWidget] = None
        if title is not None:
            self.title = page_title(title if isinstance(title, QLabel) else None,
                                    "" if isinstance(title, QLabel) else str(title))
            self.addWidget(self.title)
        if toolbar is not None:
            self.add_toolbar(toolbar)

    def add_toolbar(self, row: QLayout) -> QLayout:
        """Put the page's toolbar row next, its controls `SPACING` apart."""
        row.setSpacing(SPACING)
        self.toolbar = row
        self.addLayout(row)
        return row


def page_row(page: QWidget) -> QHBoxLayout:
    """The frame for a page laid out across rather than down: the same margins and rhythm."""
    row = QHBoxLayout(page)
    row.setObjectName("pageFrame")
    row.setContentsMargins(*MARGINS)
    row.setSpacing(SPACING)
    return row


def frame_of(page: QWidget) -> Optional[PageFrame]:
    """The page's frame, or None for a page not built in one."""
    layout = page.layout() if page is not None else None
    return layout if isinstance(layout, PageFrame) else None
