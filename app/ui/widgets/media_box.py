r"""Videos and recordings: the switches, and what this machine is missing.

Layer: L5 view - thin. Every fact about what is installed comes from
`app/extract/media_tools.py` and `app/extract/transcribe.py`, which need no
display; this is five controls and three honest sentences.

**The sentences are the point.** Both features are off by default and both need
packages Leasha does not ship - PyAV to read a video (no ffmpeg program: it is
in-process), and the faster-whisper package with a speech model. A switch that
can be turned on and then silently does nothing is the worst version of that,
so beside each switch this says in words whether the thing it needs is here and,
when it is not, the exact command that fixes it. The command is selectable so
it can be copied; Leasha never runs it. Installing software is the owner's
call, and a model download is hundreds of megabytes of their bandwidth.

**And the third sentence is the cost**, from the measured figures in
`transcribe.cost_sentence` and `PICTURE_COST_SENTENCE`: the label of an
off-by-default switch has to say what turning it on costs, in minutes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QSpinBox,
    QVBoxLayout, QWidget,
)

from app.extract import media_tools, transcribe
from app.ui.widgets.model_download import DownloadRow

#: How a saved speech model the list does not offer is marked (2026-09-29).
NOT_LISTED = "(current, not in the list)"

__all__ = ["MediaBox", "tools_sentence", "speech_sentence", "model_sentence"]

PYAV_INSTALL = r"venv\Scripts\python.exe -m pip install av==18.1.0"
#: 2026-09-29: speech runs inside Leasha on ONNX Runtime; what it needs installed
#: is PyAV, which reads the sound. Was "pip install faster-whisper==1.2.1" - a
#: false instruction once faster-whisper was no longer used, so corrected, not kept.
WHISPER_INSTALL = PYAV_INSTALL


#: What reading a video's pictures costs. **Measured 2026-09-20**, and the
#: honest half of the cost line: taking the pictures out is quick (a 1080p film
#: is scanned at roughly 300 times its own length) but each picture is then
#: read like a photograph - text first, then a description - at several seconds
#: each on a computer with no spare graphics card, up to the picture cap per film.
PICTURE_COST_SENTENCE = (
    "Taking the pictures out of a film is quick (a two-hour film in about half "
    "a minute), but each picture is then read like a photograph, which takes "
    "several seconds each - so a film with the default 200 pictures can take "
    "ten to twenty minutes.")


def tools_sentence(status: dict[str, Optional[str]]) -> str:
    """Whether the video reader (PyAV) was found, in words. Pure, so a test can
    check it."""
    where = status.get("av")
    if where:
        return (f"Reading videos: ready (PyAV {where}, built in - no separate "
                f"program to install).")
    return (f"Reading videos: the PyAV package is not installed, so videos are "
            f"findable by name only until it is. To install it, run: "
            f"{PYAV_INSTALL}")


def speech_sentence(installed: bool) -> str:
    # 2026-09-29, corrected: these named the faster-whisper package, which the
    # speech engine no longer uses (it runs inside Leasha on ONNX Runtime).
    if installed:
        return "Speech to text: ready - it runs inside Leasha."
    return ("Speech to text: the PyAV package, which reads the sound, is not installed, "
            "so recordings are findable by name only. To install it, run: "
            f"{WHISPER_INSTALL}")


def model_sentence(model: str, present: bool) -> str:
    if present:
        return f"Speech model '{model}': downloaded."
    # 2026-09-29, corrected: "to fetch it once, run: <command>" - the fetch is the
    # Download button now, and `download_command` says where it is.
    return (f"Speech model '{model}': not downloaded. Leasha never downloads "
            f"anything by itself - to fetch it once: "
            f"{transcribe.download_command(model)}")


class MediaBox(QGroupBox):
    """Five settings and a status readout for video and audio."""

    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("Videos and recordings", parent)
        self._model_dir: Optional[Path] = None

        # **Object names spelled out as literals**, for
        # `test_settings_reachable`, which greps the source and must run with no
        # display.
        self.video = QCheckBox("Read videos on this computer")
        self.video.setObjectName("VIDEO_INDEXING_ENABLED")
        self.video.setToolTip(
            "Makes videos findable by how long they are, the date they were "
            "filmed, where a phone recorded them and the words and scenes at "
            "each moment. Off by default - it can take minutes per film, and is "
            "done in the background after everything else. Needs the PyAV "
            "package; the note below says whether it was found.")

        self.audio = QCheckBox("Write down what is said in recordings")
        self.audio.setObjectName("AUDIO_TRANSCRIPTION_ENABLED")
        self.audio.setToolTip(
            "Turns speech into searchable text with the time it was said, for "
            "voice memos, calls and the sound of videos. Off by default - it "
            "takes a good fraction of the recording's own length on this "
            "computer, done slowly in the background and resumable. Needs a "
            "speech model, downloaded once below.")    # was "the faster-whisper package and ..." (2026-09-29)

        self.model = QComboBox()
        self.model.setObjectName("TRANSCRIBE_MODEL")
        self.model.setToolTip(
            "Larger models hear accents and quiet speech better and take longer "
            "and more memory. 'base' is a sensible start.")
        for name in transcribe.MODELS:
            self.model.addItem(name, name)
        # Owner, 2026-09-29: models are chosen from the list only, and there is
        # a way to download them. The speech model is never fetched by itself
        # (`transcribe.load_engine` is `local_files_only`), so this row is the
        # one way to get it from inside the application.
        self.download = DownloadRow("speech")

        self.interval = QSpinBox()
        self.interval.setObjectName("VIDEO_KEYFRAME_INTERVAL_S")
        self.interval.setRange(5, 600)
        self.interval.setSuffix(" seconds")
        self.interval.setToolTip(
            "A picture is taken whenever the scene changes, and at least this "
            "often when nothing does. Shorter finds more and costs more. At the "
            "limit (5 seconds) a long film hits the cap below quickly.")

        self.cap = QSpinBox()
        self.cap.setObjectName("VIDEO_KEYFRAME_CAP")
        self.cap.setRange(10, 1000)
        self.cap.setSuffix(" pictures")
        self.cap.setToolTip(
            "The most pictures read from one video, so a three-hour film cannot "
            "take a whole day. Each costs about as much as reading a photo.")

        # Said on the box as well as on each control's tooltip: the index run
        # reads these when it starts, so a change made now is used by the next
        # run after Leasha is restarted, and a switch that appears to do nothing
        # is worse than one that says so.
        self.restart_note = QLabel(
            "Changes here take effect the next time Leasha starts.")
        self.restart_note.setObjectName("mediaRestartNote")
        self.restart_note.setWordWrap(True)

        # The cost, always visible - not a tooltip: it is what a person needs
        # before ticking a box that can occupy the computer for hours.
        self.cost_note = self._note("mediaCostNote")
        self.cost_note.setText(f"What it costs: {PICTURE_COST_SENTENCE} "
                               f"{transcribe.cost_sentence()}")
        self.tools_note = self._note("mediaToolsNote")
        self.speech_note = self._note("mediaSpeechNote")
        self.model_note = self._note("mediaModelNote")

        form = QFormLayout()
        form.addRow(self.video)
        form.addRow("Longest gap between pictures", self.interval)
        form.addRow("Most pictures per video", self.cap)
        form.addRow(self.audio)
        form.addRow("Speech model size", self.model)
        form.addRow("", self.download)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        for note in (self.cost_note, self.restart_note, self.tools_note,
                     self.speech_note, self.model_note):
            layout.addWidget(note)

        if settings is not None:
            self.load(settings)
        self.video.toggled.connect(lambda _on: self._emit())
        self.audio.toggled.connect(lambda _on: self._emit())
        self.model.currentIndexChanged.connect(lambda _i: self._on_model())
        self.interval.editingFinished.connect(self._emit)
        self.cap.editingFinished.connect(self._emit)
        self.refresh()

    @staticmethod
    def _note(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setWordWrap(True)
        # Selectable, so the command in it can be copied. Never executed.
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting on the way in - the reason is in
        `EditorBox.load`: a control set while connected writes a value nobody
        chose."""
        controls = (self.video, self.audio, self.model, self.interval, self.cap)
        for control in controls:
            control.blockSignals(True)
        try:
            self.video.setChecked(bool(getattr(settings, "video_indexing_enabled", False)))
            self.audio.setChecked(
                bool(getattr(settings, "audio_transcription_enabled", False)))
            saved = str(getattr(settings, "transcribe_model", "base") or "base")
            found = self.model.findData(saved)
            if found < 0:
                # Dated note, 2026-09-29: a saved size the list does not offer
                # is shown as itself, marked, rather than displayed as 'base' -
                # the list must not quietly say something other than the file.
                self.model.addItem(f"{saved} {NOT_LISTED}", saved)
                found = self.model.count() - 1
            self.model.setCurrentIndex(found)
            self.interval.setValue(int(getattr(settings, "video_keyframe_interval_s", 60)))
            self.cap.setValue(int(getattr(settings, "video_keyframe_cap", 200)))
            cache = getattr(settings, "model_cache", None)
            self._model_dir = (Path(cache) / "whisper") if cache else None
            self.download.set_model_cache(cache)
        finally:
            for control in controls:
                control.blockSignals(False)
        self.refresh()

    def refresh(self) -> None:
        """Re-ask what is installed. Cheap: file lookups, no processes, no imports."""
        self.tools_note.setText(tools_sentence(media_tools.tools_status()))
        self.speech_note.setText(speech_sentence(transcribe.available()))
        # Without the package there is nothing that could load a speech model,
        # and the note above already says how to install it.
        self.download.setEnabled(transcribe.available())
        self._on_model(emit=False)

    def _on_model(self, emit: bool = True) -> None:
        name = str(self.model.currentData() or "base")
        self.model_note.setText(model_sentence(
            name, transcribe.model_present(name, self._model_dir)))
        self.download.set_target(name)
        if emit:
            self._emit()

    def values(self) -> dict:
        """`{registry key: value}` for the writer."""
        return {
            "VIDEO_INDEXING_ENABLED": self.video.isChecked(),
            "AUDIO_TRANSCRIPTION_ENABLED": self.audio.isChecked(),
            "TRANSCRIBE_MODEL": str(self.model.currentData() or "base"),
            "VIDEO_KEYFRAME_INTERVAL_S": self.interval.value(),
            "VIDEO_KEYFRAME_CAP": self.cap.value(),
        }

    def _emit(self) -> None:
        self.changed.emit(self.values())
