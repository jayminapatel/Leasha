r"""The Compute, Resources and Strategy groups of the Index Tuning screen.

Layer: L6 (UI)

Three group boxes, one file, because they are three answers to one question -
*how fast does a run go, and what does it cost the machine* - and because each
alone is too small to be worth a module.

**Every control here obeys the same two rules.**

*It says what happens at its limit.* `tuning.limit_note` supplies the sentence,
and the sentence is the feature: somebody choosing a memory ceiling who does
not know that exceeding it **pauses** rather than fails will set it far too
high out of fear of losing a run, and then it protects nothing. That is the
rule `indexing_settings.py` established and this screen inherits.

*It shows its resolved value.* In Defaults and Auto-tune a spin box reads
`Auto (6)` rather than `0`, because "0 means we decided something, and we are
not telling you what" is the settings-screen failure this whole order exists to
end.

No policy lives here. The envelope decides the bounds, `app/ui/tuning.py`
decides the words, and these classes put them on the screen.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
)

from app.core import envelope
from app.index import backends
from app.ui.tuning import MANUAL, limit_note, quantised_note, resolved_text
from app.ui.widgets.debounce import Debounced

__all__ = ["ComputeBox", "ResourcesBox", "StrategyBox", "AutoSpin"]


class AutoSpin(QSpinBox):
    """A spin box whose 0 means *auto*, and which says what auto came to.

    Qt's `setSpecialValueText` already renders 0 as a word; what it cannot do
    is render 0 as **the number auto chose**, which is the only version of the
    feature anybody can act on. `set_auto` writes that in, and `set_envelope`
    narrows the range to what this machine allows so an unreachable value
    cannot be typed at all - §3c's illegal-states rule, at the control.
    """

    def __init__(self, key: str, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        #: **The object name is set by the caller, with a literal.** Setting it
        #: from `key` here would work at runtime and be invisible to
        #: `test_settings_reachable`, which greps for `setObjectName("KEY")` -
        #: and a control the coverage test cannot see is a control that can
        #: quietly stop existing. Naming it twice is the price of the rule
        #: having teeth.
        self.key = key
        self.setKeyboardTracking(False)  # see IndexingSettings: 1500 emits 4x
        self.setSpecialValueText("Automatic")
        self._why = ""
        #: The registry's absolute maximum - what is legal on *any* machine.
        #: The envelope narrows it per machine; without a machine, this stands.
        from app.core.settings_registry import by_key

        setting = by_key(key)
        self._hard_max = int(getattr(setting, "maximum", 0) or 64)
        self.setRange(0, self._hard_max)

    def set_envelope(self, bounds: Any) -> None:
        """Clamp the range to this machine, keeping 0 reachable as *auto*.

        **`None` widens back to the registry's own maximum**, and that is the
        important half. Detection is asynchronous, so for the first moments of
        the window there is no profile - and bounding against an *unknown*
        machine gives a ceiling of one core, which silently displayed `1` over
        somebody's stored `6` and then swallowed the edit they made next,
        because setting 3 on a range of (0, 1) clamps to 1 and emits nothing.
        An unknown machine must mean "no local limit yet", never "a limit of
        one".
        """
        if bounds is None:
            self.setRange(0, self._hard_max)
            self._why = ""
            self._retip()
            return
        self.setRange(0, int(bounds.ceiling))
        self._why = str(bounds.why or "")
        self._retip()

    def set_auto(self, text: str) -> None:
        """`Auto (6)`, so the number in force is visible without switching
        modes to find it out."""
        self.setSpecialValueText(text)

    def _retip(self) -> None:
        note = limit_note(self.key)
        self.setToolTip("\n\n".join(part for part in (note, self._why) if part))


class ComputeBox(QGroupBox):
    """Which processor, how many readers, how many threads, how big a batch."""

    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: Optional[Any] = None
                 ) -> None:
        super().__init__("Compute", parent)

        self.embed_device = QComboBox()
        self.embed_device.setObjectName("EMBED_DEVICE")
        for value, label in (("auto", "Automatic"), ("cpu", "Processor"),
                             ("gpu", "Graphics card")):
            self.embed_device.addItem(label, value)
        self.embed_device.setToolTip(
            "Which processor runs the meaning model, the reranker and OCR.\n"
            "One choice for all three: a machine where two of them used the\n"
            "graphics card would be one nobody could account for.\n"
            "Takes effect on restart.")
        self.embed_device.currentIndexChanged.connect(self._device_changed)

        self.workers = AutoSpin("INDEX_WORKERS")
        self.workers.setObjectName("INDEX_WORKERS")
        self.threads = AutoSpin("ONNX_INTRA_OP_THREADS")
        self.threads.setObjectName("ONNX_INTRA_OP_THREADS")
        self.batch = AutoSpin("EMBED_BATCH")
        self.batch.setObjectName("EMBED_BATCH")
        self.batch.setSingleStep(32)

        self.quantised = QCheckBox("Use the smaller model file")
        self.quantised.setObjectName("EMBED_QUANTISED")
        self.quantised.setToolTip(self.QUANTISED_HELP)
        self._grey_quantised()

        #: §4e: **inline, never a modal.** The oversubscription warning fires
        #: while somebody is still adjusting the number that caused it, and a
        #: dialogue at that moment interrupts the very comparison they are
        #: making.
        self.warning = QLabel("")
        self.warning.setObjectName("tuningWarning")
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)

        self._save = Debounced(lambda: self.changed.emit(self.values()),
                               parent=self)
        for widget in (self.workers, self.threads, self.batch):
            widget.valueChanged.connect(lambda _v: self._save())
        self.quantised.stateChanged.connect(lambda _s: self._save())

        form = QFormLayout(self)
        form.addRow("Run models on", self.embed_device)
        form.addRow("Files read at once", self.workers)
        form.addRow("Threads per model call", self.threads)
        form.addRow("Chunks per model call", self.batch)
        form.addRow(self.quantised)
        form.addRow(self.warning)

        if settings is not None:
            self.load(settings)

    # -- state --------------------------------------------------------------

    def load(self, settings: Any) -> None:
        widgets = (self.embed_device, self.workers, self.threads, self.batch,
                   self.quantised)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            device = str(getattr(settings, "embed_device", "auto") or "auto")
            found = self.embed_device.findData(device)
            self.embed_device.setCurrentIndex(found if found >= 0 else 0)
            self.workers.setValue(int(getattr(settings, "index_workers", 0)))
            self.threads.setValue(
                int(getattr(settings, "onnx_intra_op_threads", 0)))
            self.batch.setValue(int(getattr(settings, "embed_batch", 0)))
            self.quantised.setChecked(
                bool(getattr(settings, "embed_quantised", False)))
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._save.cancel()

    def values(self) -> dict:
        return {
            "EMBED_DEVICE": str(self.embed_device.currentData() or "auto"),
            "INDEX_WORKERS": int(self.workers.value()),
            "ONNX_INTRA_OP_THREADS": int(self.threads.value()),
            "EMBED_BATCH": int(self.batch.value()),
            "EMBED_QUANTISED": bool(self.quantised.isChecked()),
        }

    def flush(self) -> None:
        """Persist anything still inside the debounce window.

        Called before the window closes. Without it, changing a control and
        immediately closing loses the change - a worse bug than the
        sluggishness the debounce exists to fix.
        """
        self._save.flush()

    def apply_profile(self, profile: Any, mode: str,
                      measured: Optional[dict] = None) -> None:
        r"""Re-bound and re-label every control for this machine and mode.

        **Signals stay blocked throughout, and this is not a nicety.** Opening
        the window wrote `INDEX_WORKERS=1` and `EMBED_DEVICE=auto` into `.env`
        before anybody had touched a control: narrowing a spin box's range
        clamps its value, clamping emits `valueChanged`, and the debounced
        writer duly saved a number nobody chose. On a machine whose cores
        could not be detected the ceiling is 1, so somebody's four workers
        became one just by looking at the screen - a settings panel quietly
        overwriting settings, which is the worst thing a settings panel can do.
        """
        for widget in (self.workers, self.threads, self.batch,
                       self.embed_device, self.quantised):
            widget.blockSignals(True)
        try:
            editable = str(mode) == MANUAL
            for widget in (self.workers, self.threads, self.batch):
                # **No profile means no local bound, not a bound of one.**
                # `envelope.for_setting` answers for *any* object, including
                # `None` - it reads missing fields as zero and a machine with
                # zero cores gets a ceiling of one. Asking it before detection
                # has answered is asking about a machine that does not exist,
                # so the question is not asked.
                bounds = (envelope.for_setting(widget.key, profile)
                          if profile is not None else None)
                widget.set_envelope(bounds)
                widget.set_auto(
                    resolved_text(widget.key, mode, widget.value(), profile,
                                  measured)
                    if profile is not None else "Automatic")
                widget.setEnabled(editable)
            self.embed_device.setEnabled(True)   # a device is always a choice
            self._grey_unavailable(profile)
        finally:
            for widget in (self.workers, self.threads, self.batch,
                           self.embed_device, self.quantised):
                widget.blockSignals(False)
        # Anything a clamp queued before the block took hold was about a value
        # nobody typed. Firing it would write it.
        self._save.cancel()
        self._warn(profile)

    def _grey_unavailable(self, profile: Any) -> None:
        """Grey what cannot work here, **with the reason on it**.

        Two different unavailabilities, two different sentences: a graphics
        card that is absent, and a quantised file that would buy nothing
        because a graphics card is in use. Either one silently ignored would
        leave somebody certain they had switched something on.
        """
        reason = backends.why_unavailable(profile)
        index = self.embed_device.findData("gpu")
        model = self.embed_device.model()
        item = model.item(index) if index >= 0 and hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(not reason)
            item.setToolTip(f"Unavailable: {reason}" if reason
                            else "DirectML is available on this machine")

        self._grey_quantised()

    #: The checkbox's explanation when it *is* available. Kept here so
    #: `_grey_quantised` can put it back, rather than leaving the "unavailable"
    #: sentence on a control that has become available again.
    #:
    #: **"Quantised" is not a word anybody has to know.** The label already
    #: says "the smaller model file", which is what it is; the tooltip says
    #: what that buys and what it costs. §7's plain-words guard caught this
    #: sentence saying "a quantised model", which is this codebase's
    #: vocabulary being spoken at somebody tuning their computer.
    QUANTISED_HELP = ("The smaller file is several times faster on a "
                      "processor,\nat a small cost in how well results are "
                      "ordered. It gains\nnothing on a graphics card. Takes "
                      "effect on restart.")

    def _grey_quantised(self) -> None:
        """Grey the smaller-model box **with its reason on it**.

        This lived only in `_grey_unavailable`, so choosing the graphics card
        disabled the checkbox and left the old tooltip in place - a control
        greyed with no explanation, which is precisely the failure the greying
        rule exists to prevent. Now one function does both, and both callers
        use it.
        """
        blocked = quantised_note(str(self.embed_device.currentData() or "auto"))
        self.quantised.setEnabled(not blocked)
        self.quantised.setToolTip(f"Unavailable: {blocked}" if blocked
                                  else self.QUANTISED_HELP)

    def _warn(self, profile: Any) -> None:
        """§3c's one warning worth having. **Warns, never blocks.**"""
        text = envelope.oversubscription_warning(
            profile, self.workers.value() or 0, self.threads.value() or 0)
        self.warning.setText(text or "")
        self.warning.setVisible(bool(text))

    def _device_changed(self, _index: int) -> None:
        # Quantisation depends on the device, so it re-greys the moment the
        # device changes rather than at the next restart.
        self._grey_quantised()
        self._save()


class ResourcesBox(QGroupBox):
    """What indexing may take from the machine before it gets out of the way."""

    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: Optional[Any] = None
                 ) -> None:
        super().__init__("Resources", parent)

        self.memory_mb = QSpinBox()
        self.memory_mb.setObjectName("INDEX_MEMORY_MB")
        self.memory_mb.setRange(256, 32_000)
        self.memory_mb.setSingleStep(100)
        self.memory_mb.setSuffix(" MB")

        self.memory_mb.setToolTip(
            "Memory ceiling. Above it indexing PAUSES and resumes - it does\n"
            "not fail, and nothing already indexed is lost.")

        self.cpu_percent = QSpinBox()
        self.cpu_percent.setObjectName("INDEX_CPU_PERCENT")
        self.cpu_percent.setRange(0, 100)
        self.cpu_percent.setSuffix(" %")
        self.cpu_percent.setSpecialValueText("No limit")
        self.cpu_percent.setToolTip(
            "While the whole machine is busier than this, indexing PAUSES, so\n"
            "it gets out of the way of whatever you are doing. It does not\n"
            "fail. 0 turns the check off entirely.")

        self.min_free_gb = QSpinBox()
        self.min_free_gb.setObjectName("MIN_FREE_GB")
        self.min_free_gb.setRange(1, 500)
        self.min_free_gb.setSuffix(" GB")
        self.min_free_gb.setToolTip(
            "Indexing STOPS rather than filling the disk, and everything\n"
            "already indexed is kept - so this is a floor that protects the\n"
            "machine, not a budget for the index.")

        self.required_free_gb = QSpinBox()
        self.required_free_gb.setObjectName("REQUIRED_FREE_GB")
        self.required_free_gb.setRange(1, 10_000)
        self.required_free_gb.setSuffix(" GB")
        self.required_free_gb.setToolTip(
            "Checked on the index drive before a run starts. Below it you get\n"
            "a warning, not a refusal - the run still goes ahead.\n\n"
            "A 100GB corpus needs roughly 150GB once the vectors, the database\n"
            "and the cache are counted.")

        self.low_priority = QCheckBox("Run at low priority")
        self.low_priority.setObjectName("INDEX_LOW_PRIORITY")
        self.low_priority.setToolTip(
            "Let everything else have the processor and the disk first.\n"
            "Leave this on unless indexing is the only thing this machine does.")

        self.pause_on_battery = QCheckBox("Pause while on battery")
        self.pause_on_battery.setObjectName("INDEX_PAUSE_ON_BATTERY")
        self.pause_on_battery.setToolTip(
            "Hold indexing until the machine is on mains power. It resumes on\n"
            "its own when you plug in - nothing is lost by waiting.")

        self.note = QLabel("")
        self.note.setObjectName("resourcesNote")
        self.note.setWordWrap(True)
        self.note.setVisible(False)

        for widget in (self.memory_mb, self.cpu_percent, self.min_free_gb,
                       self.required_free_gb):
            # Typing `1500` otherwise emits at 1, 15, 150 and 1500 - four
            # rounds of writes for one number, three of them values nobody
            # chose. Tooltips are set one at a time above rather than from
            # `limit_note` here, because `test_accessible_names` greps for the
            # literal call - and a control it cannot see is a control that can
            # quietly lose its explanation.
            widget.setKeyboardTracking(False)

        self._save = Debounced(lambda: self.changed.emit(self.values()),
                               parent=self)
        for widget in (self.memory_mb, self.cpu_percent, self.min_free_gb,
                       self.required_free_gb):
            widget.valueChanged.connect(lambda _v: self._save())
        self.low_priority.stateChanged.connect(lambda _s: self._save())
        self.pause_on_battery.stateChanged.connect(lambda _s: self._save())

        form = QFormLayout(self)
        form.addRow("Memory ceiling", self.memory_mb)
        form.addRow("Pause above", self.cpu_percent)
        form.addRow("Stop below", self.min_free_gb)
        form.addRow("Warn before a run below", self.required_free_gb)
        form.addRow(self.low_priority)
        form.addRow(self.pause_on_battery)
        form.addRow(self.note)

        if settings is not None:
            self.load(settings)

    def load(self, settings: Any) -> None:
        widgets = (self.memory_mb, self.cpu_percent, self.min_free_gb,
                   self.required_free_gb, self.low_priority,
                   self.pause_on_battery)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.memory_mb.setValue(int(getattr(settings, "index_memory_mb", 4000)))
            self.cpu_percent.setValue(int(getattr(settings, "index_cpu_percent", 80)))
            self.min_free_gb.setValue(int(getattr(settings, "min_free_gb", 5)))
            self.required_free_gb.setValue(
                int(getattr(settings, "required_free_gb", 300)))
            self.low_priority.setChecked(
                bool(getattr(settings, "index_low_priority", True)))
            self.pause_on_battery.setChecked(
                bool(getattr(settings, "index_pause_on_battery", True)))
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._save.cancel()

    def values(self) -> dict:
        return {
            "INDEX_MEMORY_MB": int(self.memory_mb.value()),
            "INDEX_CPU_PERCENT": int(self.cpu_percent.value()),
            "MIN_FREE_GB": int(self.min_free_gb.value()),
            "REQUIRED_FREE_GB": int(self.required_free_gb.value()),
            "INDEX_LOW_PRIORITY": bool(self.low_priority.isChecked()),
            "INDEX_PAUSE_ON_BATTERY": bool(self.pause_on_battery.isChecked()),
        }

    def flush(self) -> None:
        """Persist anything still inside the debounce window.

        Called before the window closes. Without it, changing a control and
        immediately closing loses the change - a worse bug than the
        sluggishness the debounce exists to fix.
        """
        self._save.flush()

    def apply_profile(self, profile: Any, free_gb: int = 0) -> None:
        """Bound the ceilings by what this machine actually has.

        **A floor larger than the disk was settable**, which is a setting that
        stops every run on a machine that is working perfectly well. The free
        space is passed in rather than read here: this runs on the UI thread,
        and asking the filesystem from a paint path is how a screen freezes.
        """
        # Blocked for the same reason `ComputeBox.apply_profile` blocks:
        # narrowing a range clamps the value, and a clamp emits. Saving a
        # number the person never chose, on the way in, is how a settings
        # panel loses somebody's settings.
        for widget in (self.memory_mb, self.min_free_gb, self.required_free_gb):
            widget.blockSignals(True)
        try:
            bounds = (envelope.for_setting("INDEX_MEMORY_MB", profile)
                      if profile is not None else None)
            if bounds is not None:
                self.memory_mb.setRange(int(bounds.floor), int(bounds.ceiling))
                self.memory_mb.setToolTip(
                    f"{limit_note('INDEX_MEMORY_MB')}\n\n{bounds.why}")

            if free_gb > 0:
                # Both floors are about this volume, so neither may exceed it.
                self.min_free_gb.setRange(1, max(1, int(free_gb)))
                self.required_free_gb.setRange(1, max(1, int(free_gb)))
                self.note.setText(
                    f"The index drive has {int(free_gb):,} GB free, so neither "
                    f"figure above can be set higher - a floor larger than the "
                    f"disk would stop every run.")
                self.note.setVisible(True)
        finally:
            for widget in (self.memory_mb, self.min_free_gb,
                           self.required_free_gb):
                widget.blockSignals(False)
        self._save.cancel()


class StrategyBox(QGroupBox):
    """The three choices whose right answer depends on the corpus, not the box."""

    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: Optional[Any] = None
                 ) -> None:
        super().__init__("Strategy", parent)

        self.two_phase = QCheckBox("Make text searchable first")
        self.two_phase.setObjectName("INDEX_TWO_PHASE")
        self.two_phase.setToolTip(
            "Words become searchable as soon as a file is read, and the\n"
            "meaning model catches up behind. On a large corpus that is the\n"
            "difference between search being useful on day one and on day\n"
            "fourteen. Nothing is skipped either way.")

        self.dedup = QCheckBox("Embed repeated text once")
        self.dedup.setObjectName("EMBED_DEDUP")
        self.dedup.setToolTip(
            "Signatures, disclaimers and boilerplate repeat across thousands\n"
            "of documents. Each distinct passage is sent to the model once and\n"
            "the result reused. No result changes; the time is saved.")

        self.bulk_fts = QComboBox()
        self.bulk_fts.setObjectName("INDEX_BULK_FTS")
        self.bulk_fts.addItem("Automatic - only for a large run", "auto")
        self.bulk_fts.addItem("Always bulk-load", "on")
        self.bulk_fts.addItem("Never - keep search working throughout", "off")
        self.bulk_fts.setToolTip(
            "For a large first run the word index is built once at the end\n"
            "rather than kept up to date file by file. Much faster, but\n"
            "nothing is searchable until the run finishes - which is why\n"
            "Automatic uses it only when the run is big enough to be worth it.")

        self.ocr_pass = QComboBox()
        self.ocr_pass.setObjectName("INDEX_OCR_PASS")
        self.ocr_pass.addItem("During the run", "with-run")
        self.ocr_pass.addItem("After the run finishes", "after-run")
        self.ocr_pass.addItem("Only when I ask", "manual")
        self.ocr_pass.setToolTip(
            "Reading text out of an image costs about 3.6 seconds a page.\n"
            "After-run leaves the rest of the index usable while the images\n"
            "are done, which on a scanned corpus is days of difference.")

        # 2026-09-29. A new control; see `app/index/read_order.py`.
        self.read_order = QComboBox()
        self.read_order.setObjectName("INDEX_ORDER")
        self.read_order.addItem("Newest first (mixed)", "newest")
        self.read_order.addItem("As found", "found")
        self.read_order.setToolTip(
            "Newest first finds every file before reading any, then reads the\n"
            "folders marked \"Index this folder first\", then everything else\n"
            "newest first, mail and files mixed. What you worked on lately is\n"
            "searchable soonest. As found reads in the order the scan goes.")

        # Work order 0x §2e. **Where the run happens, not how fast it goes** -
        # but it is on this shelf because it is watched here: somebody whose
        # window catches while an index runs is looking at this page. A new
        # label, reworded nowhere else. Off keeps the run on threads inside
        # the window's process, exactly as before; on runs it as a child
        # process (`app/index/child_run.py`). Read at Start, so a change
        # applies from the next run and never to the one in flight.
        self.separate_process = QCheckBox("Index in a separate process")
        self.separate_process.setObjectName("INDEX_SEPARATE_PROCESS")
        self.separate_process.setToolTip(
            "Runs the indexer as its own program beside the window, so a busy\n"
            "index can never make the window catch or stutter. Pause, Stop and\n"
            "the progress on this page work the same either way. Takes effect\n"
            "from the next Start.")
        # Work order 0x §5b. **How fast the run goes**: documents and mail read
        # in helper processes, one per reader, instead of threads that take
        # turns on one interpreter lock. A new label. Read at Start, like the
        # switch above; `app/index/read_process.py` has the design.
        self.read_processes = QCheckBox("Read files in separate processes")
        self.read_processes.setObjectName("INDEX_READ_PROCESSES")
        self.read_processes.setToolTip(
            "Reads documents and mail in helper processes, one per reader, so\n"
            "they use more of the computer's cores at once and indexing\n"
            "finishes sooner. Uses more memory while a run is going. A file\n"
            "that makes its reader fail is skipped without stopping the run.\n"
            "Takes effect from the next Start.")

        self._save = Debounced(lambda: self.changed.emit(self.values()),
                               parent=self)
        for widget in (self.two_phase, self.dedup, self.separate_process,
                       self.read_processes):
            widget.stateChanged.connect(lambda _s: self._save())
        for widget in (self.bulk_fts, self.ocr_pass, self.read_order):
            widget.currentIndexChanged.connect(lambda _i: self._save())

        form = QFormLayout(self)
        form.addRow(self.two_phase)
        form.addRow(self.dedup)
        form.addRow("Word index", self.bulk_fts)
        form.addRow("Read images", self.ocr_pass)
        form.addRow("Reading order", self.read_order)
        form.addRow(self.separate_process)
        form.addRow(self.read_processes)

        if settings is not None:
            self.load(settings)

    def load(self, settings: Any) -> None:
        widgets = (self.two_phase, self.dedup, self.bulk_fts, self.ocr_pass,
                   self.read_order, self.separate_process, self.read_processes)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.two_phase.setChecked(
                bool(getattr(settings, "index_two_phase", True)))
            self.dedup.setChecked(bool(getattr(settings, "embed_dedup", True)))
            self.separate_process.setChecked(
                bool(getattr(settings, "index_separate_process", False)))
            self.read_processes.setChecked(
                bool(getattr(settings, "index_read_processes", False)))
            for combo, name, fallback in (
                (self.bulk_fts, "index_bulk_fts", "auto"),
                (self.ocr_pass, "index_ocr_pass", "with-run"),
                (self.read_order, "index_order", "newest"),
            ):
                found = combo.findData(str(getattr(settings, name, fallback)))
                combo.setCurrentIndex(found if found >= 0 else 0)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._save.cancel()

    def values(self) -> dict:
        return {
            "INDEX_TWO_PHASE": bool(self.two_phase.isChecked()),
            "EMBED_DEDUP": bool(self.dedup.isChecked()),
            "INDEX_BULK_FTS": str(self.bulk_fts.currentData() or "auto"),
            "INDEX_OCR_PASS": str(self.ocr_pass.currentData() or "with-run"),
            "INDEX_ORDER": str(self.read_order.currentData() or "newest"),
            "INDEX_SEPARATE_PROCESS": bool(self.separate_process.isChecked()),
            "INDEX_READ_PROCESSES": bool(self.read_processes.isChecked()),
        }

    def flush(self) -> None:
        """Persist anything still inside the debounce window.

        Called before the window closes. Without it, changing a control and
        immediately closing loses the change - a worse bug than the
        sluggishness the debounce exists to fix.
        """
        self._save.flush()
