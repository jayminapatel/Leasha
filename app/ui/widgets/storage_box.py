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

`REQUIRED_FREE_GB` is an ordinary number and is treated as one.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.debounce import Debounced

__all__ = ["StorageBox"]


class StorageBox(QGroupBox):
    """Index location, the space it needs, and the model that fills it."""

    move_index_requested = pyqtSignal()
    rebuild_vectors_requested = pyqtSignal()
    #: `{registry key: value}` for the one setting here that is an ordinary
    #: number. The other two are flows and persist through those.
    changed = pyqtSignal(dict)

    def _grey_the_graphics_card(self, settings: Any) -> None:
        """Disable the GPU row when it cannot work, and say why on it.

        Detection happens here rather than in `__init__`'s flow so that a
        machine which cannot be probed - no PowerShell, no onnxruntime, a
        locked-down box - gets a usable Settings page rather than an exception
        while it is being built. §4 M13's rule about `__init__` not doing work
        applies doubly to work that shells out.
        """
        reason = ""
        try:
            from app.core.compute_profile import detect
            from app.index.backends import why_unavailable

            reason = why_unavailable(getattr(settings, "compute_profile", None)
                                     or detect())
        except Exception:                        # noqa: BLE001 - never fatal
            reason = "this machine's graphics support could not be checked"

        index = self.embed_device.findData("gpu")
        item = None
        if index >= 0:
            model = self.embed_device.model()
            # `item` exists on the QStandardItemModel a QComboBox builds for
            # itself. Guarded because a caller is free to set another model,
            # and a Settings page that raises is worse than one that offers a
            # choice the backend will decline with a notice anyway.
            item = model.item(index) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(not reason)
            item.setToolTip(f"Unavailable: {reason}" if reason
                            else "DirectML is available on this machine")

        self.embed_device.setToolTip(
            "Which processor runs the meaning model, the reranker and OCR.\n"
            "Automatic uses the graphics card when this machine has one that\n"
            "works, and the processor otherwise. Takes effect on restart.\n"
            + (f"\nGraphics card unavailable: {reason}" if reason else ""))

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

        self.required_free_gb = QSpinBox()
        self.required_free_gb.setObjectName("REQUIRED_FREE_GB")
        self.required_free_gb.setRange(1, 10_000)
        self.required_free_gb.setSuffix(" GB")
        self.required_free_gb.setValue(int(getattr(settings, "required_free_gb", 300)))
        self.required_free_gb.setToolTip(
            "Checked on the index drive before a run starts.\n"
            "A 100GB corpus needs roughly 150GB once the vectors, the database\n"
            "and the cache are counted."
        )

        self.required_free_gb.setKeyboardTracking(False)
        self._save = Debounced(
            lambda: self.changed.emit(
                {"REQUIRED_FREE_GB": int(self.required_free_gb.value())}),
            parent=self,
        )
        self.required_free_gb.valueChanged.connect(lambda _v: self._save())

        self.embed_model = QLineEdit(str(getattr(settings, "embed_model", "")))
        self.embed_model.setAccessibleName("Meaning model")
        self.embed_model.setObjectName("EMBED_MODEL")
        self.embed_model.setReadOnly(True)
        self.embed_model.setToolTip(
            "The model that turns text into vectors. Shown, not edited: changing "
            "it invalidates every vector in the index."
        )

        # **`EMBED_DEVICE` is an ordinary control, and that is the decision.**
        #
        # It sits beside `EMBED_MODEL`, which is a flow because changing it
        # invalidates every vector stored. This changes none of them: the same
        # model on a different processor produces the same vectors to within
        # floating-point noise, so it costs a restart and nothing else.
        #
        # The graphics-card option is **greyed with its reason showing** rather
        # than accepted and then quietly ignored - §3c's rule about illegal
        # states being unreachable at the control, applied to a choice instead
        # of a number. Somebody who cannot use it learns why here, rather than
        # from a log file after an index run they thought was accelerated.
        self.embed_device = QComboBox()
        self.embed_device.setObjectName("EMBED_DEVICE")
        self.embed_device.setAccessibleName("Run models on")
        for value, label in (("auto", "Automatic"), ("cpu", "Processor"),
                             ("gpu", "Graphics card")):
            self.embed_device.addItem(label, value)
        self._grey_the_graphics_card(settings)
        current = str(getattr(settings, "embed_device", "auto") or "auto")
        found = self.embed_device.findData(current)
        self.embed_device.setCurrentIndex(found if found >= 0 else 0)
        self.embed_device.currentIndexChanged.connect(
            lambda _i: self.changed.emit(
                {"EMBED_DEVICE": str(self.embed_device.currentData() or "auto")}))

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
        form.addRow("Free space needed", self.required_free_gb)
        form.addRow("Meaning model", self.embed_model)
        form.addRow("", self.rebuild_vectors)
        form.addRow("Run models on", self.embed_device)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
