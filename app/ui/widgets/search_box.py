r"""The Search group in Settings: reranking, and what it costs.

Layer: L5

Split out of `settings_view.py` when it crossed the 250-line guard - the same
route `ModelBox` and `EnvironmentBox` took, and for the same reason: a view that
keeps growing is a view where logic starts to live.

**Reranking is the main quality dial and it was only ever in `.env`.** The
switch was on screen; the two numbers that decide what it costs were not, so the
honest choice available to somebody finding search slow was on or off. Cost is
roughly linear in `RERANK_TOP_N`, so the useful move is usually to lower it
rather than to give up the precision entirely.

Each control carries its registry key as its object name. That is what
`test_settings_reachable.py` looks for, and what stops a setting being declared,
documented, and wired to nothing - which has already happened twice here.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.debounce import Debounced
from app.ui.widgets.model_download import DownloadRow

__all__ = ["SearchBox", "RERANK_MODELS", "NOT_LISTED"]

#: The rerankers measured on this project's own machine, fastest first, with
#: what each cost. **Not a guess and not a marketing table** - these are the
#: numbers recorded in `app/core/config.py` beside `rerank_model`, from
#: `rerank-bench` with 30 candidates windowed to 600 characters, median of
#: three passes.
#:
#: They are here because the difference is not marginal: the slowest is nine
#: times the fastest, and it was the default long enough to make a nine-second
#: search look normal. Somebody choosing between them should not have to find
#: that out by trying one.
#:
#: Dated note, 2026-09-29: the owner overruled the next line - the box is not
#: editable any more; see the note in `SearchBox.__init__`.
#:
#: The box stays editable, so an identifier not listed still works.
RERANK_MODELS: tuple[tuple[str, str], ...] = (
    ("Xenova/ms-marco-MiniLM-L-6-v2", "fastest, 22ms per result, 80MB"),
    ("jinaai/jina-reranker-v1-tiny-en", "27ms per result, 130MB"),
    ("Xenova/ms-marco-MiniLM-L-12-v2", "66ms per result, 120MB"),
    ("BAAI/bge-reranker-base", "best ranking but 203ms per result, 1GB"),
    # Dated note, 2026-09-29 (owner: the list is the only way to choose, so
    # the other options go in it). The two more rerankers fastembed 0.8.0 can
    # load. Not timed on this machine, so they say their size and nothing more.
    ("jinaai/jina-reranker-v1-turbo-en", "not timed here, 150MB"),
    ("jinaai/jina-reranker-v2-base-multilingual",
     "many languages, not timed here, 1.1GB"),
)

#: What an unlisted saved model is called in the list. Owner, 2026-09-29: a
#: model is chosen from the list only - but a value saved before that, by hand
#: or by an older version, is still the person's choice and is shown as such
#: rather than quietly swapped for the first entry.
NOT_LISTED = "(current, not in the list)"


def _select_model(combo: QComboBox, identifier: str) -> None:
    """Show the configured model, listed or not.

    A model the list does not know about is set as the edit text rather than
    dropped: the box is a suggestion, and silently replacing somebody's choice
    with the first entry would be worse than offering no list at all.
    """
    if not identifier:
        return
    index = combo.findData(identifier)
    if index < 0:
        # Dated note, 2026-09-29: the box is no longer editable, so an unlisted
        # model is added as an entry of its own, marked, and selected.
        combo.addItem(f"{identifier}   —   {NOT_LISTED}", identifier)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


class SearchBox(QGroupBox):
    """Rerank on/off, how deep it goes, how much it reads, and which model."""

    #: `{registry key: value}` once the person has stopped changing things.
    #:
    #: **Emitted, because a control that persists nothing is a control that
    #: does nothing.** These were built with a `values()` method that nothing
    #: called - the same shape as `rerank_toggled` and `cloud_toggled` before
    #: them, and introduced in the commit that fixed those.
    changed = Signal(dict)

    def __init__(self, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__("Search", parent)

        self.rerank = QCheckBox("Rerank results (slower, more precise)")
        self.rerank.setObjectName("RERANK_ENABLED")
        self.rerank.setChecked(bool(getattr(settings, "rerank_enabled", False)))
        self.rerank.setToolTip(
            "Runs a second, more careful model over the top results.\n"
            "If search feels slow, lower the two numbers below before turning\n"
            "this off - it is the difference between finding the right document\n"
            "and finding one that mentions the same words."
        )

        self.rerank_top_n = QSpinBox()
        self.rerank_top_n.setObjectName("RERANK_TOP_N")
        self.rerank_top_n.setRange(5, 100)
        self.rerank_top_n.setSuffix(" results")
        self.rerank_top_n.setValue(int(getattr(settings, "rerank_top_n", 30)))
        self.rerank_top_n.setToolTip(
            "How many results the reranker looks at.\n"
            "Cost is roughly linear in this, so halving it roughly halves the\n"
            "time reranking adds."
        )

        self.rerank_window = QSpinBox()
        self.rerank_window.setObjectName("RERANK_WINDOW_CHARS")
        self.rerank_window.setRange(200, 2000)
        self.rerank_window.setSingleStep(100)
        self.rerank_window.setSuffix(" characters")
        self.rerank_window.setValue(int(getattr(settings, "rerank_window_chars", 600)))
        self.rerank_window.setToolTip(
            "How much of each result the reranker reads.\n"
            "Too little and it judges on a fragment; too much and it spends its\n"
            "time on text nobody will see."
        )

        # **A list, not a text box.** A free-text field for a model name means
        # knowing an exact HuggingFace identifier, and a typo is not discovered
        # until the next start fails to download it - by which time the person
        # has forgotten what they typed.
        #
        # Dated note, 2026-09-29 - the owner: "where there are models it has to
        # be dropdown only no manual entry for models". The paragraph below is
        # overruled: the box is no longer editable, a saved identifier that is
        # not listed is kept as its own marked entry (`_select_model`), and the
        # Download row under it fetches the chosen model ahead of the restart.
        #
        # Editable, so an identifier not listed here still works: the registry
        # declares this `kind="text"` and an editable combo is still text.
        self.rerank_model = QComboBox()
        self.rerank_model.setObjectName("RERANK_MODEL")
        self.rerank_model.setAccessibleName("Rerank model")
        self.rerank_model.setEditable(False)
        for identifier, note in RERANK_MODELS:
            self.rerank_model.addItem(f"{identifier}   —   {note}", identifier)
        _select_model(self.rerank_model, str(getattr(settings, "rerank_model", "")))
        self.rerank_model.setToolTip(
            "A small local cross-encoder that search runs directly. Nothing to\n"
            "do with Ollama - search never calls a service.\n\n"
            "The timings are from rerank-bench on this machine, 30 candidates\n"
            "windowed to 600 characters. Loaded at startup, so a change takes\n"
            "effect next time."
        )

        restart = QLabel(
            "The rerank model is loaded at startup, so a change to it takes "
            "effect the next time the app opens."
        )
        restart.setWordWrap(True)

        form = QFormLayout()
        form.addRow("", self.rerank)
        form.addRow("Rerank the top", self.rerank_top_n)
        form.addRow("Reading", self.rerank_window)
        form.addRow("Model", self.rerank_model)
        self.rerank_download = DownloadRow(
            "rerank", model_cache=getattr(settings, "model_cache", None))
        form.addRow("", self.rerank_download)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(restart)

        # Debounced for the same reason the indexing ceilings are: typing 600
        # into a spin box otherwise writes at 6, 60 and 600, two of which
        # nobody chose.
        self._save = Debounced(lambda: self.changed.emit(self.values()), parent=self)

        for widget in (self.rerank_top_n, self.rerank_window):
            widget.setKeyboardTracking(False)
            widget.valueChanged.connect(lambda _v: self._save())
        self.rerank_model.currentTextChanged.connect(lambda _t: self._save())
        self.rerank_model.currentIndexChanged.connect(
            lambda _i: self.rerank_download.set_target(self.chosen_model()))
        self.rerank_download.set_target(self.chosen_model())
        self.rerank.stateChanged.connect(lambda _s: self._save())

        self.rerank.stateChanged.connect(lambda _s: self._sync())
        self._sync()

    def _sync(self) -> None:
        """Grey the numbers when reranking is off.

        They still apply the moment it is switched back on, so they are disabled
        rather than hidden - a control that vanishes reads as a setting that was
        removed.
        """
        on = self.rerank.isChecked()
        self.rerank_top_n.setEnabled(on)
        self.rerank_window.setEnabled(on)
        self.rerank_model.setEnabled(on)
        self.rerank_download.setEnabled(on)

    def chosen_model(self) -> str:
        """The model identifier, without the timing shown beside it.

        `currentData` for a listed entry, the typed text for anything else -
        the item text carries "22ms per result" for the reader and that must
        never reach `.env`.
        """
        data = self.rerank_model.currentData()
        if data:
            return str(data)
        return self.rerank_model.currentText().split("   —   ")[0].strip()

    def values(self) -> dict[str, Any]:
        """What the controls currently say, keyed by registry key."""
        return {
            "RERANK_ENABLED": self.rerank.isChecked(),
            "RERANK_TOP_N": self.rerank_top_n.value(),
            "RERANK_WINDOW_CHARS": self.rerank_window.value(),
            "RERANK_MODEL": self.chosen_model(),
        }
