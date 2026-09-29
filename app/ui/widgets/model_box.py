"""Choose which Ollama model Interpret uses, from the ones actually installed.

Layer: L5

Typing a model name into `.env` and hoping is not a choice, it is a guess — and
this session showed what the guess costs. The default was `mistral` with a
five-second budget; the prompt is 1,900 characters; the feature failed every
time on a machine with five perfectly good models installed, one of them small
enough to have answered instantly.

So the list comes from Ollama, each row says what that model will cost, and
**Test** runs a real translation and reports the seconds. The whole point is to
turn a guess into a measurement, because the measurement is the thing that was
missing.

**Nothing here touches the network on the UI thread.** Probing a dead Ollama
costs the connect timeout, and a window that stops repainting for three seconds
is indistinguishable from a crashed one. Both the refresh and the test go
through `workers.run()`.

**The configured model survives a failed probe.** If Ollama is not answering the
list is empty, and dropping the saved value because of that would silently
change a setting the person chose. The box says why the list is short and keeps
what it had.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from app.llm.models import (
    TIMEOUT_RANGE,
    choose,
    rank,
    suggested_timeout_s,
    with_suggestions,
)
from app.search.translate import TEST_SENTENCE
from app.ui.widgets.model_download import DownloadRow
from app.ui.widgets.number_field import fit
from app.ui.workers import CallableWorker, run

__all__ = ["ModelBox", "TEST_SENTENCE"]


class ModelBox(QGroupBox):
    """The Interpret model, its budget, and a button that proves both."""

    #: (enabled, model, timeout_s). One signal for all three, because they are
    #: one decision: a model without a budget that fits it looks broken rather
    #: than slow, and either without the switch does nothing at all.
    changed = pyqtSignal(bool, str, int)
    #: The address, when it has been edited and focus has left the box.
    #: Separate from `changed` because it persists to `.env` rather than to
    #: window state, and because a half-typed URL must not be saved.
    url_changed = pyqtSignal(str)

    def __init__(self, client_factory: Any, parent: Optional[QWidget] = None) -> None:
        # **The word people look for is "Ollama".** Reported: *"i dont seem to
        # find the button to turn olama on and off"* - and it was on screen, in
        # a group headed "AI query interpretation" with a tick reading "Let a
        # local model turn sentences into queries". Every word of that is
        # accurate and none of it is the word somebody scanning the page has in
        # mind. Naming the thing beats describing it.
        super().__init__("Ollama — AI query interpretation (optional)", parent)
        #: Called with no arguments to get a fresh OllamaClient. A factory
        #: rather than a client, so the box can build one against whatever URL
        #: and model are current without owning that knowledge.
        self._client_factory = client_factory
        self._configured = ""
        self._loading = False

        # **Off unless asked for.** Interpretation is the only part of this
        # application that talks to another process, and most machines have no
        # Ollama at all. Off by default means nobody is shown a button that
        # cannot work, and nothing probes a service that is not there - which
        # matters for an app whose promise is that it is entirely local.
        self.enabled = QCheckBox(
            "Use Ollama to turn sentences into search queries")
        self.enabled.setToolTip(
            "Adds an Interpret button beside the search box.\n\n"
            "It rewrites 'emails from chris about a licence' as\n"
            "'from:chris licence' and puts that in the box for you to edit.\n\n"
            "Search itself never uses this, and works exactly the same with it\n"
            "switched off. Nothing contacts Ollama while this is unticked."
        )
        self.enabled.toggled.connect(self._on_toggled)

        self.model = QComboBox()
        self.model.setObjectName("OLLAMA_MODEL")
        self.model.setToolTip(
            "Which model rewrites a sentence into a search query.\n\n"
            "Smaller is usually better here: the job is to turn one sentence into\n"
            "a short query, and a 1.5B model does that as well as a 7B one in a\n"
            "fraction of the time. Search never uses this - only the Interpret\n"
            "button does."
        )
        self.model.currentIndexChanged.connect(self._on_model_chosen)

        self.timeout = QSpinBox()
        self.timeout.setRange(*TIMEOUT_RANGE)
        self.timeout.setSuffix(" s")
        self.timeout.setKeyboardTracking(False)
        self.timeout.setToolTip(
            "How long to let the model think before giving up.\n\n"
            "This was five seconds against a job that takes thirty, so Interpret\n"
            "failed every time and blamed Ollama for not answering. Choosing a\n"
            "model sets a sensible budget for it; adjust only if you see timeouts."
        )
        self.timeout.valueChanged.connect(lambda _v: self._emit())
        # No arrows, and its back-to-default button restores the budget that
        # fits the chosen model - the number this box would set itself.
        fit(self.timeout, default=suggested_timeout_s(None))

        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.setToolTip("Ask Ollama which models are installed")
        self.refresh_button.clicked.connect(lambda _c=False: self.refresh())

        self.test_button = QPushButton("Test")
        self.test_button.setToolTip(
            "Run one real interpretation and show what came back, and how long\n"
            "it took. The only way to know whether a model is fast enough."
        )
        self.test_button.clicked.connect(lambda _c=False: self.test())

        # **Editable, and here rather than in Settings.** It was a read-only box
        # on the Settings page, which failed the "everything tunable has a UI"
        # rule by being untunable - and it belongs beside Refresh and Test,
        # which are the two things that tell you whether the address is right.
        self.url = QLineEdit()
        self.url.setObjectName("OLLAMA_URL")
        self.url.setAccessibleName("Ollama address")
        self.url.setPlaceholderText("http://127.0.0.1:11434")
        self.url.setToolTip(
            "Where Ollama is listening. Only the Interpret button uses it -\n"
            "search never calls a service, so an unreachable address costs\n"
            "nothing else.\n\n"
            "Use Refresh or Test after changing it."
        )
        self.url.editingFinished.connect(
            lambda: self.url_changed.emit(self.url.text().strip()))

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("resultsSummary")

        # Owner, 2026-09-29: model lists are drop-downs only, "if there are
        # other options add them and put a mechanism to download". The greyed
        # suggestions in the list are what Download offers; once one is pulled
        # the list is asked for again and it becomes choosable.
        self.download = DownloadRow("ollama", client_factory=self._client_factory)
        self.download.finished.connect(lambda _n, _r: self.refresh())

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)

        form = QFormLayout(self)
        form.addRow(self.enabled)
        form.addRow("Address", self.url)
        form.addRow("Model", self.model)
        form.addRow("Give it up to", self.timeout)
        form.addRow(buttons)
        form.addRow(self.download)
        form.addRow(self.status)

    # -- loading ---------------------------------------------------------------

    def load(self, model: str, timeout_s: int, *, enabled: bool = False) -> None:
        """Show the saved settings, then go and ask Ollama what exists.

        In that order deliberately: the saved model appears immediately even if
        Ollama is slow or absent, so the panel is never briefly blank or briefly
        wrong.

        **Nothing is probed while switched off.** Opening Settings must not
        contact a service somebody has declined to use.
        """
        self._configured = (model or "").strip()
        with _quiet(self.enabled):
            self.enabled.setChecked(bool(enabled))
        with _quiet(self.timeout):
            self.timeout.setValue(int(timeout_s or suggested_timeout_s(None)))
        from app.llm.models import parameter_billions

        fit(self.timeout, default=suggested_timeout_s(parameter_billions(self._configured)))
        self._show_models([])
        self._sync_enabled()
        if enabled:
            self.refresh()
        else:
            self.status.setText(
                "Switched off. Search works normally without it; tick the box to "
                "have sentences rewritten as queries."
            )

    def _on_toggled(self, on: bool) -> None:
        """Turning it on is what triggers the first probe."""
        self._sync_enabled()
        if on:
            self.refresh()
        else:
            self.status.setText("Switched off. Nothing will contact Ollama.")
        self._emit()

    def _sync_enabled(self) -> None:
        """Grey out what has no meaning while it is off.

        Disabled rather than hidden: a panel that changes height as you tick a
        box makes everything below it jump, and seeing the controls is how
        somebody knows what turning it on would give them.
        """
        on = self.enabled.isChecked()
        for widget in (self.model, self.timeout, self.refresh_button, self.test_button,
                       self.download):
            widget.setEnabled(on)

    def refresh(self) -> None:
        """Ask Ollama for its model list, off the UI thread."""
        if self._loading:
            return
        self._loading = True
        self.refresh_button.setEnabled(False)
        self.status.setText("Asking Ollama which models are installed…")

        worker = CallableWorker(self._list_models, component="ui.models")
        worker.signals.finished.connect(self._show_models)
        worker.signals.failed.connect(lambda _e: self._show_models([]))
        worker.signals.done.connect(self._loaded)
        run(QThreadPool.globalInstance(), worker)

    def _list_models(self) -> list[str]:
        """Worker thread. `available_models` never raises; it returns []."""
        try:
            return list(self._client_factory().available_models())
        except Exception:                        # noqa: BLE001 - a probe, not a search
            return []

    def _loaded(self) -> None:
        self._loading = False
        self.refresh_button.setEnabled(True)

    def _show_models(self, installed: Any) -> None:
        """Fill the dropdown. UI thread, no I/O."""
        names = [str(name) for name in (installed or [])]
        selected, note = choose(self._configured, names)

        with _quiet(self.model):
            self.model.clear()
            # `with_suggestions`, not `rank`: the installed models, then the
            # ones worth having, greyed out with the command that installs
            # them. See `llm/models.SUGGESTED` for why a one-line dropdown was
            # the bug even when it was accurate.
            for choice in with_suggestions(names):
                self.model.addItem(choice.label, choice.name)
                if not choice.selectable:
                    # Shown but not choosable, with the reason in the label.
                    # Hiding it invites hunting for a model that `ollama list`
                    # plainly shows.
                    index = self.model.count() - 1
                    item = self.model.model().item(index)
                    if item is not None:
                        item.setEnabled(False)

            # The configured model always appears, even when the probe found
            # nothing and even when it is not installed. Dropping it would
            # silently change a setting somebody chose.
            if selected and self.model.findData(selected) < 0:
                # Dated note, 2026-09-29: marked, when Ollama answered and the
                # model is not among what it has - the list says so itself.
                label = f"{selected}  (current, not installed)" if names else selected
                self.model.insertItem(0, label, selected)
            position = self.model.findData(selected)
            self.model.setCurrentIndex(max(0, position))

        self.status.setText(note or self._speed_note(names, selected))
        self.download.set_offers(
            (choice.name, "not installed") for choice in with_suggestions(names)
            if not choice.installed)

    def _speed_note(self, installed: list[str], selected: str) -> str:
        """One line under the dropdown: how many there are, and what to do.

        **"1 models installed." was the whole of it**, which is ungrammatical
        and, worse, says nothing about the thing the reader is looking at - a
        list with one usable row in it. The greyed-out suggestions now explain
        themselves in the list; this says the same thing in a sentence, because
        a dropdown has to be opened before it can be read.
        """
        count = len(installed)
        smaller = [
            choice for choice in rank(installed)
            if choice.selectable and choice.billions is not None and choice.billions <= 2
            and choice.name != selected
        ]
        if smaller:
            return (
                f"{count} models installed. Interpret only rewrites one "
                f"sentence, so {smaller[0].name} would likely do the same job faster."
            )
        if count == 1:
            return (
                "One model installed, which is all this needs - Interpret "
                "rewrites a single sentence. The greyed-out entries in the list "
                "are alternatives, with the command that installs them.")
        if count:
            return f"{count} models installed."
        return ""

    # -- changing --------------------------------------------------------------

    def _on_model_chosen(self, _index: int) -> None:
        chosen = str(self.model.currentData() or "")
        if not chosen or chosen == self._configured:
            return
        self._configured = chosen
        # A new model gets a budget that fits it. Changing the model without
        # the budget is how a large model looks broken rather than slow - which
        # is exactly what happened with a 7B model and five seconds.
        from app.llm.models import parameter_billions

        with _quiet(self.timeout):
            self.timeout.setValue(suggested_timeout_s(parameter_billions(chosen)))
        fit(self.timeout, default=suggested_timeout_s(parameter_billions(chosen)))
        if not self.enabled.isChecked():
            # Choosing a model is the act of asking for the feature. Making
            # somebody then find a separate switch is a step that exists only
            # because the code has two flags.
            with _quiet(self.enabled):
                self.enabled.setChecked(True)
            self._sync_enabled()
        self.status.setText(f"Using {chosen}. Press Test to see how fast it is.")
        self._emit()

    def _emit(self) -> None:
        model = str(self.model.currentData() or self._configured or "")
        self.changed.emit(
            bool(self.enabled.isChecked()), model, int(self.timeout.value()))

    # -- testing ---------------------------------------------------------------

    def test(self) -> None:
        """One real interpretation, off the UI thread, reported with its time."""
        model = str(self.model.currentData() or self._configured or "")
        if not model:
            self.status.setText("Choose a model first.")
            return

        self.test_button.setEnabled(False)
        self.status.setText(f"Asking {model} to interpret a sentence…")

        budget = int(self.timeout.value())
        worker = CallableWorker(self._translate, model, budget, component="ui.models")
        worker.signals.finished.connect(self._show_test)
        worker.signals.failed.connect(
            lambda error: self.status.setText(f"[{error.code}] {error.message}"))
        worker.signals.done.connect(lambda: self.test_button.setEnabled(True))
        run(QThreadPool.globalInstance(), worker)

    def _translate(self, model: str, budget: int) -> Any:
        r"""Worker thread. Uses the real translator, so this tests what runs.

        **`enabled=True`, and without it this button could never work.**
        `QueryTranslator` defaults to disabled - the feature is optional and off
        until somebody asks for it - and the probe built one with the default,
        so every press returned the switched-off fallback and logged
        *"Interpreting is switched off. Turn it on in Settings."* The person is
        standing in Settings, with it switched on, pressing Test.

        Pressing it again builds another fresh translator, so the "logged once
        per translator" guard in `_fallback` does not dedupe them either: four
        presses, four identical lines, and the advice in each one is to do the
        thing that has already been done.

        The switch guards *search*, which must not reach the network when
        somebody has turned this off. Pressing Test **is** asking for it, so the
        probe is enabled by construction rather than by reading the checkbox -
        the checkbox has already had its say by enabling the button.
        """
        from app.search.translate import QueryTranslator

        client = self._client_factory()
        client.model = model
        return QueryTranslator(
            client, timeout_s=float(budget), enabled=True).translate(TEST_SENTENCE)

    def _show_test(self, result: Any) -> None:
        seconds = getattr(result, "elapsed_s", 0.0)
        if getattr(result, "changed", False):
            self.status.setText(
                f"Worked in {seconds:.1f}s.  \"{TEST_SENTENCE}\"  became  "
                f"\"{result.query}\""
            )
            return

        error = getattr(result, "error", None)
        if error is not None and getattr(error, "code", "") == "ERR_OLLAMA_TIMEOUT":
            # The specific failure this panel exists to prevent, so it gets the
            # specific fix rather than a generic one.
            self.status.setText(
                f"Gave up after {seconds:.0f}s. Either raise the budget above, or "
                f"choose a smaller model - Interpret only has to rewrite a sentence."
            )
            return
        self.status.setText(getattr(result, "note", "") or "No usable query came back.")


class _quiet:
    """Set a widget's value without its signals firing.

    Without it, filling the dropdown emits `currentIndexChanged` for every item
    added, each one read as the person choosing a model - so loading the panel
    would save a different model than the one that was saved.
    """

    def __init__(self, widget: Any) -> None:
        self._widget = widget

    def __enter__(self) -> Any:
        self._widget.blockSignals(True)
        return self._widget

    def __exit__(self, *_exc: Any) -> None:
        self._widget.blockSignals(False)
