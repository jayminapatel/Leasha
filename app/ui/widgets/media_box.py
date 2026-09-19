r"""Videos and recordings: the switches, and what this machine is missing.

Layer: L5 view - thin. Every fact about what is installed comes from
`app/extract/media_tools.py` and `app/extract/transcribe.py`, which need no
display; this is five controls and three honest sentences.

**The sentences are the point.** Both features are off by default and both need
software Leasha does not ship - FFmpeg, and the faster-whisper package with a
speech model. A switch that can be turned on and then silently does nothing is
the worst version of that, so beside each switch this says in words whether the
thing it needs is here and, when it is not, the exact command that fixes it.
The command is selectable so it can be copied; Leasha never runs it. Installing
software is the owner's call, and a model download is hundreds of megabytes of
their bandwidth.
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

__all__ = ["MediaBox", "tools_sentence", "speech_sentence", "model_sentence"]

FFMPEG_INSTALL = "winget install --id Gyan.FFmpeg -e"
WHISPER_INSTALL = r"venv\Scripts\python.exe -m pip install faster-whisper==1.2.1"


def tools_sentence(status: dict[str, Optional[str]]) -> str:
    """Whether FFmpeg was found, in words. Pure, so a test can check it."""
    missing = [name for name, where in status.items() if not where]
    if not missing:
        return f"FFmpeg: found ({status.get('ffmpeg')})."
    return (f"FFmpeg: not found ({', '.join(missing)}). Videos are findable by "
            f"name only until it is installed. To install it, run: "
            f"{FFMPEG_INSTALL}")


def speech_sentence(installed: bool) -> str:
    if installed:
        return "Speech to text: the faster-whisper package is installed."
    return ("Speech to text: the faster-whisper package is not installed, so "
            "recordings are findable by name only. To install it, run: "
            f"{WHISPER_INSTALL}")


def model_sentence(model: str, present: bool) -> str:
    if present:
        return f"Speech model '{model}': downloaded."
    return (f"Speech model '{model}': not downloaded. Leasha never downloads "
            f"anything by itself - to fetch it once, run: "
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
            "each moment. Off by default - it can take minutes per film. "
            "Needs FFmpeg; the note below says whether it was found.")

        self.audio = QCheckBox("Write down what is said in recordings")
        self.audio.setObjectName("AUDIO_TRANSCRIPTION_ENABLED")
        self.audio.setToolTip(
            "Turns speech into searchable text with the time it was said, for "
            "voice memos, calls and the sound of videos. Off by default - it "
            "takes a good fraction of the recording's own length on this "
            "computer, done slowly in the background and resumable. Needs the "
            "faster-whisper package and a speech model.")

        self.model = QComboBox()
        self.model.setObjectName("TRANSCRIBE_MODEL")
        self.model.setToolTip(
            "Larger models hear accents and quiet speech better and take longer "
            "and more memory. 'base' is a sensible start.")
        for name in transcribe.MODELS:
            self.model.addItem(name, name)

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

        self.tools_note = self._note("mediaToolsNote")
        self.speech_note = self._note("mediaSpeechNote")
        self.model_note = self._note("mediaModelNote")

        form = QFormLayout()
        form.addRow(self.video)
        form.addRow("Longest gap between pictures", self.interval)
        form.addRow("Most pictures per video", self.cap)
        form.addRow(self.audio)
        form.addRow("Speech model size", self.model)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        for note in (self.restart_note, self.tools_note, self.speech_note,
                     self.model_note):
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
            found = self.model.findData(
                str(getattr(settings, "transcribe_model", "base") or "base"))
            self.model.setCurrentIndex(found if found >= 0 else self.model.findData("base"))
            self.interval.setValue(int(getattr(settings, "video_keyframe_interval_s", 60)))
            self.cap.setValue(int(getattr(settings, "video_keyframe_cap", 200)))
            cache = getattr(settings, "model_cache", None)
            self._model_dir = (Path(cache) / "whisper") if cache else None
        finally:
            for control in controls:
                control.blockSignals(False)
        self.refresh()

    def refresh(self) -> None:
        """Re-ask what is installed. Cheap: file lookups, no processes, no imports."""
        self.tools_note.setText(tools_sentence(media_tools.tools_status()))
        self.speech_note.setText(speech_sentence(transcribe.available()))
        self._on_model(emit=False)

    def _on_model(self, emit: bool = True) -> None:
        name = str(self.model.currentData() or "base")
        self.model_note.setText(model_sentence(
            name, transcribe.model_present(name, self._model_dir)))
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
