r"""The Photo Tagger as a window of its own. Work order 0j (`202626270512`) §2.

Layer: L5

`PhotoTaggerPage` was built, tested and ticked - and nothing in the
application ever created it, so a person who turned face recognition on had
no way to name a single pile. This is the missing doorway.

**A window, not a rail page, and that is deliberate.** Naming faces is a
chore done in bursts, after an index run, by somebody who has the feature on;
a permanent rail entry would sit in every other person's way. It opens from
Settings ("Name the people in your photos...") and from Go > People in photos,
and it is built the first time it is asked for - a person who never opens it
never pays for it, the same rule the log window follows.

**It reloads every time it is shown**, because the piles change whenever an
index run finds new faces and a window kept open across two runs would
otherwise show last week's groups.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

__all__ = ["PhotoTaggerWindow"]


class PhotoTaggerWindow(QWidget):
    """Top-level window around `PhotoTaggerPage`. Owned by the main window."""

    #: A photo was chosen inside the page - the main window decides what
    #: "open" means, exactly as it does for the thumbnail grid.
    opened = pyqtSignal(str)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        # `Qt.Window` on a child: its own frame and taskbar entry, but closed
        # with the main window rather than outliving it.
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("photoTaggerWindow")
        self.setWindowTitle("Name the people in your photos")
        self.resize(900, 640)
        self.page = PhotoTaggerPage(store, self)
        self.page.opened.connect(self.opened)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.page)
        self._loaded_once = True          # the page's constructor loads

    def showEvent(self, event: Any) -> None:          # noqa: N802 - Qt override
        # The constructor already reloaded; every later show re-reads.
        if self._loaded_once:
            self._loaded_once = False
        else:
            self.page.reload()
        super().showEvent(event)
