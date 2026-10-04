r"""The two settings that are not settings.

Layer: L5

`DATA_PATH` and `EMBED_MODEL` both change **what the index is** rather than how
it behaves, and the registry marks them `destructive` with a named flow for
exactly that reason. This module is those flows.

**Why neither can be a text box.**

Typing a new path into an index-location field does not move an index. It points
the application at a different, probably empty one - the person's index appears
to have vanished, nothing is wrong, and no error is available to say so. The
three things somebody could actually mean are all reasonable and all different:
move what is there, adopt an index already at the new location, or start empty.
A field cannot ask which.

Changing the meaning model makes every vector already stored meaningless. The
old vectors do not become wrong in a way search can detect; they become noise
that still ranks. So the honest form is an action that states the cost -
"4.2 million chunks, roughly six hours" - and asks.

**These dialogs decide and confirm. They do not move anything.** The shell owns
the stores and has to close them before a byte moves, so each returns a decision
and the window carries it out. A dialog that reached into the storage layer
would be a dialog that has to know when the layer is busy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, QTimer
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from app.ui.widgets.buttons import style_all
from app.ui.widgets.model_download import DownloadRow
from app.ui.widgets.number_field import fit_all as fit_number_fields
from app.ui.workers import CallableWorker, run

__all__ = [
    "IndexLocationDialog", "RebuildVectorsDialog", "LocationChoice",
    "MOVE", "ADOPT", "FRESH", "EMBED_MODELS",
]

#: Embedding models: identifier, output width, and what the size costs.
#:
#: **The width is data, not prose, and that is the fix.** It used to live only
#: in the note - `"768 dimensions, ~440MB - set EMBED_DIM to 768"` - so the one
#: number the swap depends on was a sentence asking the person to go and edit
#: `.env` by hand, from inside the flow whose whole job is to change the model
#: safely. They did not, because nobody reads a dropdown as an instruction; the
#: window then downloaded 219MB and failed with `ERR_MODEL_LOAD` naming a
#: setting they had never typed. Reported from the window, 2026-08-26.
#:
#: Held here so `chosen_dim()` can write `EMBED_DIM` alongside `EMBED_MODEL`.
#: A model that is *not* in this list has an unknown width - see `chosen_dim`.
#:
#: Dated note, 2026-09-29: "Editable" below is no longer so - the owner ruled
#: that a model is only ever chosen from the list. See `RebuildVectorsDialog`.
#:
#: `bge-small-en-v1.5` is what this project ships and measures against; the
#: others are the common alternatives at each size. Editable, so anything else
#: still works.
EMBED_MODELS: tuple[tuple[str, int, str], ...] = (
    ("BAAI/bge-small-en-v1.5", 384, "~130MB - the shipped default"),
    ("BAAI/bge-base-en-v1.5", 768, "~440MB - better quality, slower"),
    ("sentence-transformers/all-MiniLM-L6-v2", 384, "~90MB"),
    # Dated note, 2026-09-29 (owner: the list is the only way to choose, so
    # the other options go in it). Dense models fastembed 0.8.0 loads through
    # the same `TextEmbedding(model_name, cache_dir)` call `Embedder` makes,
    # each with the width fastembed's own catalogue gives. None needs a text
    # prefix the embedder does not add. Sizes are fastembed's download sizes.
    ("snowflake/snowflake-arctic-embed-s", 384, "~130MB - small, English"),
    ("jinaai/jina-embeddings-v2-small-en", 512, "~120MB - reads longer passages"),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384,
     "~220MB - many languages"),
    ("snowflake/snowflake-arctic-embed-m", 768, "~430MB - English"),
    ("mixedbread-ai/mxbai-embed-large-v1", 1024, "~640MB - large, slow on a processor"),
    ("BAAI/bge-large-en-v1.5", 1024, "~1.2GB - largest, slowest"),
)

#: What an unlisted model already in use is called in the list (2026-09-29).
NOT_LISTED = "(current, not in the list)"

MOVE = "move"
ADOPT = "adopt"
FRESH = "fresh"

#: How long the typing pauses before the folder in the box is checked.
CHECK_DELAY_MS = 250
#: What the dialog says while a worker is still answering (2026-10-04).
CHECKING = "Checking that folder…"
MEASURING = "Measuring the index…"

#: An index folder is recognised by these. Enough to tell "an index lives here"
#: from "an empty folder", without opening either store - this runs while a
#: dialog is being drawn.
_INDEX_MARKERS = ("vectors", "fts")


@dataclass(frozen=True, slots=True)
class LocationChoice:
    """What the person decided. The window carries it out."""

    action: str                 # MOVE | ADOPT | FRESH
    destination: Path


def looks_like_an_index(path: Path) -> bool:
    """Does an index already live here? Never raises."""
    try:
        return any((path / marker).exists() for marker in _INDEX_MARKERS)
    except OSError:
        return False


def free_gb(path: Path) -> float:
    """Free space on the drive holding `path`, walking up to something real."""
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(str(target)).free / 1e9
    except OSError:
        return 0.0


def check_destination(text: str, current: Path) -> dict:
    """What the location dialog needs to know about a typed folder. **Worker.**

    2026-10-04, code review: `_refresh` asked all of this on the interface
    thread on every keystroke - `resolve`, `exists` and `disk_usage` against
    whatever drive the half-typed path named, each a wait on a sleeping or
    network drive. Now asked here, once the typing pauses. Never raises.
    """
    destination = Path(str(text or "").strip() or ".")
    try:
        same = destination.resolve() == Path(current).resolve()
    except (OSError, RuntimeError, ValueError):
        same = False
    return {"text": text, "occupied": looks_like_an_index(destination), "same": same,
            "free_gb": free_gb(destination)}


def folder_gb(path: Path) -> float:
    """Size of an index folder. Best effort, and cheap enough for a dialog."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total / 1e9


class IndexLocationDialog(QDialog):
    """Move the index, adopt one already there, or start empty."""

    def __init__(
        self, current: Path, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Index location")
        self.setMinimumWidth(560)
        self._current = Path(current)
        #: 2026-10-04, code review: what the workers found - the index's size,
        #: measured once as the dialog opens (it was a walk of the whole index
        #: up to three times a keystroke), and the facts about the folder in
        #: the box (`check_destination`), None while they are being asked.
        #: `_check_generation` drops an answer about text since replaced.
        self._index_gb: Optional[float] = None
        self._facts: Optional[dict] = None
        self._check_generation = 0
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(CHECK_DELAY_MS)
        self._check_timer.timeout.connect(self._check)

        self.destination = QLineEdit(str(current))
        self.destination.setAccessibleName("New index location")
        self.destination.setToolTip(
            "Where the index should live. A fast local disk with room to grow - "
            "the index is roughly a tenth of the size of what it has read.")
        self.destination.textChanged.connect(lambda _t: self._typed())

        browse = QPushButton("Browse…")
        browse.setToolTip("Choose the folder in Explorer instead of typing it.")
        browse.clicked.connect(lambda _c=False: self._browse())

        # **Three choices about somebody's whole index, and not one of them
        # said what it did.** The labels are short because they are radio
        # buttons; short is fine only when hovering explains the consequence,
        # and the consequences here are days of indexing apart.
        self.move = QRadioButton("Move the index there")
        self.move.setToolTip(
            "Copy this index to the new folder and use it from there.\n\n"
            "Nothing is re-indexed and nothing is lost. Needs enough free space "
            "at the destination for the whole index while both copies exist.")
        self.adopt = QRadioButton("Use the index already there")
        self.adopt.setToolTip(
            "Leave this index alone and switch to one that already exists at "
            "the destination.\n\n"
            "For pointing a fresh install at an index built earlier, or on "
            "another machine. What is indexed here now stays where it is.")
        self.fresh = QRadioButton("Start a new, empty index there")
        self.fresh.setToolTip(
            "Begin again at the destination with nothing indexed.\n\n"
            "Everything has to be read again, which on a large corpus is hours "
            "or days. Your documents are never touched - only the index is.")
        self.move.setChecked(True)

        self._group = QButtonGroup(self)
        for index, button in enumerate((self.move, self.adopt, self.fresh)):
            self._group.addButton(button, index)
            button.toggled.connect(lambda _c: self._refresh())

        self.consequence = QLabel("")
        self.consequence.setWordWrap(True)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #c62828;")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        row = QVBoxLayout()
        row.addWidget(self.destination)
        row.addWidget(browse)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"The index is currently in {current}."))
        layout.addLayout(row)
        layout.addWidget(self.move)
        layout.addWidget(self.adopt)
        layout.addWidget(self.fresh)
        layout.addWidget(self.consequence)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self._refresh()
        self._measure()
        self._check()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def checking(self) -> bool:
        """Still waiting for a worker's answer about the folder or the index."""
        return self._facts is None or self._index_gb is None

    def _measure(self) -> None:
        worker = CallableWorker(folder_gb, self._current, component="ui.index_location")
        worker.signals.finished.connect(self._measured)
        run(QThreadPool.globalInstance(), worker)

    def _measured(self, size: Any) -> None:
        self._index_gb = float(size or 0.0)
        self._refresh()

    def _typed(self) -> None:
        """A keystroke: forget what was known about the old text, ask again
        once the typing pauses."""
        self._check_generation += 1
        self._facts = None
        self._refresh()
        self._check_timer.start()

    def _check(self) -> None:
        generation = self._check_generation
        worker = CallableWorker(check_destination, self.destination.text(), self._current,
                                component="ui.index_location")
        worker.signals.finished.connect(
            lambda facts, g=generation: self._checked(facts, g))
        run(QThreadPool.globalInstance(), worker)

    def _checked(self, facts: Any, generation: int) -> None:
        if generation != self._check_generation:
            return                               # typed since; a later check answers
        self._facts = dict(facts or {})
        self._refresh()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where should the index live?", self.destination.text())
        if chosen:
            self.destination.setText(chosen)

    def choice(self) -> LocationChoice:
        action = MOVE if self.move.isChecked() else (
            ADOPT if self.adopt.isChecked() else FRESH)
        return LocationChoice(action, Path(self.destination.text().strip()))

    def _refresh(self) -> None:
        """Say what each option would do to *this* folder, before it is chosen.

        The options are not equally available: adopting a folder with no index
        in it does nothing, and moving onto one that already holds an index
        would have to overwrite it. Rather than allowing a choice and failing
        afterwards, each is enabled only when it means something.

        2026-10-04, code review: **no I/O here any more** - it decides from
        what the workers found (`_facts`, `_index_gb`), and until they answer
        it says it is checking and keeps OK off.
        """
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._facts is None and str(self.destination.text()).strip():
            self.consequence.setText(CHECKING)
            self.problem.setText("")
            if ok is not None:
                ok.setEnabled(False)
            return
        facts = self._facts or {}
        occupied = bool(facts.get("occupied"))
        same = bool(facts.get("same"))
        size = self._index_gb

        self.adopt.setEnabled(occupied and not same)
        self.move.setEnabled(not occupied and not same)
        self.fresh.setEnabled(not same)

        if same:
            self.consequence.setText("That is where the index already is.")
        elif self.move.isChecked() and size is None:
            self.consequence.setText(MEASURING)
        elif self.move.isChecked():
            self.consequence.setText(
                f"Copies about {size:.1f}GB there, checks it, then removes the "
                "original. Nothing is deleted until the copy has been verified."
            )
        elif self.adopt.isChecked():
            self.consequence.setText(
                "Uses the index already in that folder and leaves the current "
                "one where it is. Nothing is deleted."
            )
        else:
            self.consequence.setText(
                "Starts empty and indexes from scratch. The current index is "
                "left where it is, so nothing is lost - but searching finds "
                "nothing until a run finishes."
            )

        problem = ""
        if not str(self.destination.text()).strip():
            problem = "Choose a folder."
        elif same:
            problem = ""
        elif self.move.isChecked() and size is None:
            problem = ""                         # measured shortly; OK waits for it
        elif self.move.isChecked() and float(facts.get("free_gb") or 0.0) < size:
            problem = (
                f"Not enough space: the index is about "
                f"{size:.1f}GB and that drive has "
                f"{float(facts.get('free_gb') or 0.0):.1f}GB free."
            )
        elif not any(b.isChecked() and b.isEnabled()
                     for b in (self.move, self.adopt, self.fresh)):
            problem = "Choose what to do with the folder you picked."

        self.problem.setText(problem)
        if ok is not None:
            ok.setEnabled(not problem and not same
                          and not (self.move.isChecked() and size is None))


class RebuildVectorsDialog(QDialog):
    """Change the meaning model, knowing what it costs."""

    #: Measured on this project's own corpus: roughly this many chunks embed per
    #: second on CPU. Only used to turn a chunk count into a duration somebody
    #: can plan around - a number with the wrong order of magnitude is worse
    #: than no number, so it is deliberately pessimistic.
    CHUNKS_PER_SECOND = 200

    def __init__(
        self,
        current_model: str,
        chunk_count: int,
        parent: Optional[QWidget] = None,
        *,
        current_dim: int = 384,
        model_cache: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change the meaning model")
        self.setMinimumWidth(560)

        # **A list, and the dimensions matter.** An embedding model has a fixed
        # output width, and one that does not match `EMBED_DIM` cannot be
        # written to the existing table at all - the store refuses it, correctly,
        # with an error about a number nobody typed. Choosing from names that
        # carry their own width makes the mismatch impossible rather than
        # explained afterwards.
        #
        # Dated note, 2026-09-29 - the owner overruled the next line: "where
        # there are models it has to be dropdown only no manual entry for
        # models". The box is not editable. A model already in use that the
        # list does not know is added as its own entry, marked as such, so it
        # is shown rather than silently swapped; the width question below is
        # asked only for that one. More models are listed, and the Download
        # row fetches the chosen one before the re-embed starts.
        #
        # Editable, because a model not listed here is a legitimate choice.
        self.model = QComboBox()
        self.model.setToolTip(
            "The model that turns text into vectors for meaning-based search.\n\n"
            "Changing it invalidates every vector already stored, so the index "
            "has to be re-embedded. Larger models are slower and need the width "
            "beside them to match.")
        # The name goes on the control that *chooses*. `storage_box` carries it
        # too, on the read-only display that shows what is in use - which is
        # where the reachability test finds it, and is not where the decision
        # is made.
        self.model.setObjectName("EMBED_MODEL")
        self.model.setAccessibleName("Meaning model")
        self.model.setEditable(False)
        for identifier, dim, note in EMBED_MODELS:
            self.model.addItem(f"{identifier}   —   {dim} dimensions, {note}",
                               identifier)
        index = self.model.findData(current_model)
        if index < 0 and str(current_model or "").strip():
            self.model.addItem(f"{current_model}   —   {NOT_LISTED}", current_model)
            index = self.model.count() - 1
        self.model.setCurrentIndex(max(0, index))
        self.model.currentTextChanged.connect(lambda _t: self._refresh())
        self._current = current_model

        # **Only for a model this list does not know.** A listed model carries
        # its width in `EMBED_MODELS` and the person is never asked for a
        # number they would have to look up. An unlisted one is a legitimate
        # choice and its width cannot be guessed - and guessing is the failure
        # being fixed here, so it is asked for instead of assumed.
        self.dim = QSpinBox()
        self.dim.setToolTip(
            "How wide the chosen model's vectors are.\n\n"
            "It has to match the model exactly - the vector store refuses a "
            "mismatch, and the number is listed against each model above.")
        self.dim.setObjectName("EMBED_DIM")
        self.dim.setAccessibleName("Meaning model dimensions")
        self.dim.setRange(1, 8192)
        self.dim.setValue(int(current_dim) or 384)
        self.dim.valueChanged.connect(lambda _v: self._refresh())

        self.dim_row = QWidget()
        dim_layout = QHBoxLayout(self.dim_row)
        dim_layout.setContentsMargins(0, 0, 0, 0)
        dim_label = QLabel("Output dimensions:")
        dim_label.setBuddy(self.dim)
        dim_layout.addWidget(dim_label)
        dim_layout.addWidget(self.dim)
        dim_layout.addStretch(1)

        self.cost = QLabel("")
        self.cost.setWordWrap(True)

        explanation = QLabel(
            "The meaning model turns text into the vectors semantic search "
            "compares. Vectors made by one model are meaningless to another - "
            "they do not become obviously wrong, they become noise that still "
            "ranks - so everything indexed has to be embedded again before "
            "search is trustworthy."
        )
        explanation.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Re-embed everything")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self.download = DownloadRow("embed", model_cache=model_cache)
        self.download.setVisible(bool(model_cache))
        self.model.currentIndexChanged.connect(
            lambda _i: self.download.set_target(self.chosen_model()))

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(self.model)
        layout.addWidget(self.download)
        layout.addWidget(self.dim_row)
        layout.addWidget(self.cost)
        layout.addWidget(self.buttons)

        self._chunks = max(0, int(chunk_count))
        self._current_dim = int(current_dim) or 384
        self._refresh()
        self.download.set_target(self.chosen_model())
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)
        # Number fields: typed, no arrows, a back-to-default button.
        fit_number_fields(self)

    def chosen_model(self) -> str:
        r"""The identifier alone - the dimensions shown beside it are for the
        reader and must never reach `.env`.

        **`currentData()` alone is wrong on an editable combo, and wrong in the
        expensive direction.** The box is editable so any model can be named,
        but typing into it does not move `currentIndex` - so after picking a
        preset and then typing something else, `currentData()` still returned
        *the preset*. This is the dialog that invalidates every vector in the
        index and re-embeds the corpus: it would have spent those hours
        rebuilding against a model nobody chose, and `.env` would then disagree
        with what the person typed.

        The text is the authority, and the data is consulted only when the text
        is still exactly an item's display - which is the case where the person
        picked from the list and the display carries the dimensions.
        """
        text = self.model.currentText().strip()
        index = self.model.findText(text)
        if index >= 0:
            return str(self.model.itemData(index) or text)
        return text.split("   —   ")[0].strip()

    def known_dim(self) -> int | None:
        """The listed width for the chosen model, or None if it is not listed."""
        chosen = self.chosen_model()
        for identifier, dim, _note in EMBED_MODELS:
            if identifier == chosen:
                return dim
        return None

    def chosen_dim(self) -> int:
        """The width to write to `EMBED_DIM`, always alongside `EMBED_MODEL`.

        **Never returns the old width for a new model.** The bug this closes was
        `EMBED_MODEL` being written on its own: the index kept `EMBED_DIM=384`,
        the 768-wide model loaded fine, and the mismatch surfaced only on the
        first batch - after a 219MB download - as an error naming a setting the
        person had never touched.

        A listed model answers from `EMBED_MODELS`. An unlisted one cannot be
        guessed, so the spin box answers, and it is on screen precisely in that
        case.
        """
        known = self.known_dim()
        return int(known) if known is not None else int(self.dim.value())

    def _refresh(self) -> None:
        chosen = self.chosen_model()
        known = self.known_dim()
        # The number is not asked for when it is already known - that is a
        # question with one right answer, and asking it is how it gets typed
        # wrongly.
        self.dim_row.setVisible(known is None)
        if known is not None and self.dim.value() != known:
            # **Signals blocked while this module drives its own control.**
            # `setValue` emits `valueChanged`, which is wired back into this
            # function. It would settle after one extra pass here, but this
            # project has already lost a process to exactly that shape in
            # `view_options._apply_widths`, and the guard costs one line.
            blocked = self.dim.blockSignals(True)
            try:
                self.dim.setValue(known)
            finally:
                self.dim.blockSignals(blocked)

        changed = bool(chosen) and (
            chosen != self._current or self.chosen_dim() != self._current_dim)
        hours = self._chunks / self.CHUNKS_PER_SECOND / 3600 if self._chunks else 0
        width_changes = self.chosen_dim() != self._current_dim

        if not changed:
            self.cost.setText("This is the model already in use.")
        else:
            # **The assumed rate is stated.** A duration derived from a constant
            # nobody can see cannot be checked against the machine it is shown
            # on - and this one has been wrong by two orders of magnitude on a
            # CPU-only fp16 build, where the true rate was nearer 1/sec than
            # 200. Naming the figure turns a promise into something falsifiable,
            # and names the command that measures it.
            rate = (f"at ~{self.CHUNKS_PER_SECOND} chunks/sec - measure yours "
                    f"with  app.cli embed-bench")
            duration = (
                f"{self._chunks:,} chunks to re-embed - roughly {hours:.0f} "
                f"hour(s) {rate}."
                if hours >= 1 else
                f"{self._chunks:,} chunks to re-embed - a few minutes {rate}."
            )

            if width_changes:
                # **A width change is a different and larger thing**, and the
                # old text did not distinguish them: re-embedding replaces the
                # *contents* of the vector table, changing the width replaces
                # the table. The reassurance below - that search keeps working
                # on the old vectors - is true of the first and false of the
                # second, so it must not be shown here. Two costs, two
                # sentences; saying both would contradict itself.
                self.cost.setText(
                    f"The vector store is rebuilt from empty: "
                    f"{self._current_dim} to {self.chosen_dim()} dimensions, and "
                    f"vectors of different widths cannot share an index. "
                    f"Meaning-based search returns nothing until the run "
                    f"finishes; keyword search is unaffected.\n\n{duration}"
                )
            elif hours >= 1:
                self.cost.setText(
                    f"{duration} Search keeps working on the old vectors until "
                    "the run finishes, and their answers will be poor until it "
                    "does."
                )
            else:
                self.cost.setText(duration)

        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(changed))
            ok.setText("Rebuild the vector store" if width_changes
                       else "Re-embed everything")
