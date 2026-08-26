r"""The Index storage group: where the index is, and what it is made of.

Layer: L5

Split out of `settings_view.py` for the 250-line guard, like `SearchBox` and
`ModelBox` before it.

**Two of these are not settings, and must never be fields.**

`DATA_PATH` was a read-only box, which failed the "everything tunable has a UI"
rule by being untunable - but whoever made it read-only chose correctly, because
the obvious alternative is worse. Typing a new path does not move an index. It
points the application at a different, probably empty one, and the person's
index appears to have vanished with no error anywhere. So it stays a display,
and a button starts a flow that offers the three things somebody could actually
mean: move it, use one already there, or start empty.

`EMBED_MODEL` is the same shape for a different reason: changing it makes every
vector already stored meaningless. That is not a text box either - it is an
action that states the cost ("this will re-embed 4.2 million chunks, about six
hours") and confirms.

**Two ordinary controls left here and did not come back.** `REQUIRED_FREE_GB`
and `EMBED_DEVICE` are both about *how a run goes* rather than about where the
index lives, so §4c of the index-tuning order moved them onto the Index Tuning
screen with the rest of that question. The free-space figure gained a ceiling
there that it could not have here - it is bounded by the index volume's real
free space, and a floor larger than the disk stops every run.

What is left is two displays and their flows, which is what this group is for.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


__all__ = ["StorageBox"]


class StorageBox(QGroupBox):
    """Index location, the space it needs, and the model that fills it."""

    move_index_requested = pyqtSignal()
    rebuild_vectors_requested = pyqtSignal()
    #: `{registry key: value}`. Nothing here emits it today - both remaining
    #: settings persist through their flow - and it is kept because the panel
    #: is connected to the writer, so a control added here is wired the moment
    #: it exists rather than a release later.
    changed = pyqtSignal(dict)

    def __init__(self, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__("Index storage", parent)

        self.data_path = QLineEdit(str(getattr(settings, "data_path", "")))
        self.data_path.setAccessibleName("Index location")
        self.data_path.setObjectName("DATA_PATH")
        self.data_path.setReadOnly(True)
        self.data_path.setToolTip(
            "Where the index lives. Nothing here is original data - it is all "
            "rebuildable from your documents, so deleting it is always safe."
        )

        self.move_index = QPushButton("Move or change index location…")
        self.move_index.setObjectName("move-index")
        self.move_index.setToolTip(
            "Move the index to another drive, use an index already there, or "
            "start a new empty one. Each says what it will do before it does it."
        )
        self.move_index.clicked.connect(
            lambda _c=False: self.move_index_requested.emit()
        )

        self.embed_model = QLineEdit(str(getattr(settings, "embed_model", "")))
        self.embed_model.setAccessibleName("Meaning model")
        self.embed_model.setObjectName("EMBED_MODEL")
        self.embed_model.setReadOnly(True)
        self.embed_model.setToolTip(
            "The model that turns text into vectors. Shown, not edited: changing "
            "it invalidates every vector in the index."
        )

        self.rebuild_vectors = QPushButton("Change the meaning model…")
        self.rebuild_vectors.setObjectName("rebuild-vectors")
        self.rebuild_vectors.setToolTip(
            "Changing the model makes every vector already stored meaningless, "
            "so everything has to be re-embedded before search works properly "
            "again. The flow says how long that will take before it starts."
        )
        self.rebuild_vectors.clicked.connect(
            lambda _c=False: self.rebuild_vectors_requested.emit()
        )

        note = QLabel(
            "The index is never inside the project folder and holds no original "
            "data. Both buttons above say what they will do, and what it costs, "
            "before anything changes."
        )
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Index location", self.data_path)
        form.addRow("", self.move_index)
        form.addRow("Meaning model", self.embed_model)
        form.addRow("", self.rebuild_vectors)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
