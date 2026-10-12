"""The Chat tab's top lines: why chat is not ready, and the Fast / Thoughtful box.

Layer: L5 view helper

Moved out of `chat_view.py` on 2026-10-13 (UI phase B) so the view stays under the
250-line guard (`tests/unit/test_presenter.py`) as it took on the list's and the
box's new keys. The widgets are the view's own attributes as before - `notice`,
`recheck_button`, `speed`, `speed_note` - so nothing that reaches for them changed.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

__all__ = ["SPEEDS", "build_head"]

SPEEDS = (("Fast", "fast"), ("Thoughtful", "thoughtful"))


def build_head(view: Any) -> tuple[QHBoxLayout, QHBoxLayout]:
    """Make the notice, Check again, the speed box and its note on `view`, and
    return `(notice_row, head)` for the view to lay out."""
    view.notice = QLabel("")
    view.notice.setObjectName("chatNotice")
    view.notice.setWordWrap(True)
    view.notice.setAccessibleName("Why chat is not ready")
    view.notice.setVisible(False)
    view.recheck_button = QPushButton("Check again")
    view.recheck_button.setToolTip(
        "Look again for the helper program Chat needs. Use this after "
        "installing or starting it. Searching is not affected either way.")
    view.recheck_button.setVisible(False)
    view.recheck_button.clicked.connect(lambda _c=False: view.recheck_requested.emit())
    view.speed = QComboBox()
    view.speed.setAccessibleName("How Chat answers")
    view.speed.setToolTip(
        "Fast answers sooner; Thoughtful takes longer over harder "
        "questions. Applies to the next question you ask.")
    for label, value in SPEEDS:
        view.speed.addItem(label, value)
    view.speed.currentIndexChanged.connect(
        lambda _i: view.speed_changed.emit(str(view.speed.currentData())))

    view.speed_note = QLabel("")
    view.speed_note.setObjectName("chatSpeedNote")
    view.speed_note.setWordWrap(True)
    view.speed_note.setAccessibleName("What Fast and Thoughtful do")
    view.speed_note.setVisible(False)

    # 2026-10-05: the notice has a line of its own. It shared the line
    # below with the model and speed boxes, which took the room first, so
    # it was squeezed to a column a few words wide and ten lines tall.
    notice_row = QHBoxLayout()
    notice_row.addWidget(view.notice, stretch=1)
    notice_row.addWidget(view.recheck_button, alignment=Qt.AlignmentFlag.AlignTop)
    head = QHBoxLayout()
    # Spare room goes to empty space, never to the Fast/Thoughtful box.
    # While the notice shows it takes the room (its stretch wins); once
    # chat is ready the notice hides, and without this the box stretched
    # across the whole pane - one word, "Fast", in a bar as wide as the
    # conversation, looking like a title rather than a choice (grabbed
    # 2026-09-27, order 0x section 9).
    head.addStretch(1)
    # 2026-10-04: the same Preview toggle every other tab carries; 2026-10-08,
    # the Sources toggle beside it (the owner: "the source tab viewing should be
    # able to turn off and on too"). Each shows its page of the side panel, or
    # puts the panel away when its page is the one showing.
    head.addWidget(view.toggles["sources"])
    head.addWidget(view.toggles["inspector"])
    return notice_row, head
