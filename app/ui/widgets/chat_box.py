"""The Chat group in Settings: the few things about chat a person may change.

Layer: L5 view

Work order 202626270611 3e and 4d. **Chat follows the Index Tuning modes**, so this
group is *invisible outside Manual*: in Defaults and Auto-tune Chat chooses for
itself (`app/chat/config.py` ignores what is stored here and the envelope decides),
and what was set in Manual is kept, not used, until Manual is chosen again. The
window calls `set_manual` when the mode changes; a `Settings` that carries no mode
(a test double) counts as Manual, the same rule `ChatSettings.from_settings` uses.

**Driven by the registry, not by a list here.** The engine declares its chat
settings in `app/core/settings_registry.py` (keys starting `CHAT_`); this group
builds one control per such key from the declaration - a tick box for a bool, a
number box for an int, a drop-down for a choice, a text box otherwise - and
gives each `setObjectName(<the key>)`, which is what
`tests/unit/test_settings_reachable.py` and the settings filter look controls
up by. A key added to the registry gets its control with no edit here.

**Three of the keys are models, and get a roles grid rather than a text box** (4d):
`CHAT_MODEL`, `CHAT_ROUTER_MODEL` and `CHAT_PLANNER_MODEL` are drop-downs of the
*installed* models with "Automatic" first, and a fourth row is the Describe role
(`OLLAMA_VISION_MODEL`, vision-capable models only). Below the grid, one line says
what the chosen models need in memory against what this computer has. What is
installed is asked of Ollama on a worker - never on the window thread.

**A number with an envelope says what the envelope says** (`app/core/envelope.py`):
its range is the envelope's and a line under it gives the reason and what Automatic
would use.

**The web section is not one of those.** Whether Chat may look things up on the web
(`CHAT_WEB_*`, owner 2026-09-20) is the person's own privacy choice, not a tuning of
the machine, so it is always shown, in Defaults and Auto-tune as much as in Manual, and
its words say exactly what leaves this computer. Only the models, the numbers and the
manner note follow the Index Tuning mode.

Sends `{KEY: value}` on `changed`, wired by `SettingsView` to its
`settings_changed` signal like every other group, so the window writes `.env`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Optional

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from app.chat.roles import InstalledModels, ram_line, resolve_roles
from app.core import envelope
from app.core import settings_registry as reg
from app.llm.models import SUGGESTED, VISION_SUGGESTED
from app.ui.widgets.chat_roles import ModelCombo, RolesGrid
from app.ui.widgets.model_download import DownloadRow
from app.ui.workers import CallableWorker, run

__all__ = ["ChatBox", "chat_settings", "ROLE_KEYS", "DESCRIBE_KEY", "INTRO"]

PREFIX = "CHAT_"

#: The keys of the always-visible web section.
WEB_PREFIX = "CHAT_WEB_"

WEB_INTRO = ("Off, Chat stays entirely on this computer. On, it can also look things up on the "
             "web - always after searching your files, and only when the Web switch in the Chat "
             "box is on for that conversation. What leaves this computer is one short search "
             "phrase, never a file name, a passage, an email or your conversation.")

#: The three registry keys that name a model for one of Chat's roles.
#: What was seen of each web search service when it was called live from the development
#: machine on 2026-09-20 (`docs/WORKORDER-202626270611-chat-tab.md`, dated note), said on the
#: choice itself so Settings never offers a dead one without saying so. The value stored
#: stays the bare word; only the text the person reads carries the note.
CHOICE_NOTES: dict[str, dict[str, str]] = {
    "CHAT_WEB_PROVIDER": {
        "wikipedia": " (works, no account; encyclopedia questions only)",
        "duckduckgo": " (not confirmed working: it asked for proof of a person)",
        "searxng": " (your own server; unverified)",
        "brave": " (needs your key; unverified)",
    },
}

ROLE_KEYS = ("CHAT_MODEL", "CHAT_ROUTER_MODEL", "CHAT_PLANNER_MODEL")

#: The Describe role lives on an existing setting (the photo description model).
DESCRIBE_KEY = "OLLAMA_VISION_MODEL"

INTRO = ("These apply because Tuning is set to Manual on the Indexing page. In Defaults "
         "and Auto-tune, Chat chooses for itself and anything set here is kept but not used.")

_DESCRIBE_FALLBACK = "llava"


def chat_settings() -> list[Any]:
    """The registry entries this group is responsible for."""
    return [s for s in reg.SETTINGS if s.key.startswith(PREFIX) or s.group == "Chat"]


def _setting(key: str) -> Optional[Any]:
    return next((s for s in reg.SETTINGS if s.key == key), None)


class ChatBox(QGroupBox):
    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: Optional[QWidget] = None, *,
                 probe: Optional[Callable[[str], InstalledModels]] = None) -> None:
        super().__init__("Chat", parent)
        self.controls: dict[str, QWidget] = {}
        #: What asks Ollama what is installed. A test hands one in; otherwise
        #: `app.chat.llm.probe_installed`. Looked up when used, so a test may also
        #: replace the module's own.
        self._probe = probe
        self._installed: Optional[InstalledModels] = None
        self._asked = False
        self._probing = False
        self._url = "http://127.0.0.1:11434"
        self._local_model = "mistral"
        self._manual = True
        self.notes: dict[str, QLabel] = {}

        box = QVBoxLayout(self)
        #: Everything that follows the Index Tuning mode lives here, so it can be hidden
        #: as one piece; the web section below it is never hidden.
        self.manual_part = QWidget()
        self.manual_part.setObjectName("chatManualPart")
        outer = QVBoxLayout(self.manual_part)
        outer.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.manual_part)
        self.intro = QLabel(INTRO)
        self.intro.setObjectName("chatManualNote")
        self.intro.setWordWrap(True)
        outer.addWidget(self.intro)

        self.roles = RolesGrid()
        self.roles.setObjectName("chatRoles")
        outer.addWidget(self.roles)
        self._describe: Optional[ModelCombo] = None

        look = QHBoxLayout()
        self.look_again = QPushButton("Look again")
        self.look_again.setObjectName("chatLookAgain")
        self.look_again.setToolTip("Ask Ollama which models are installed now.")
        self.look_again.setAccessibleName("Look for installed models again")
        self.look_again.clicked.connect(self.refresh)
        self.status = QLabel("")
        self.status.setObjectName("chatModelsStatus")
        self.status.setWordWrap(True)
        # Owner, 2026-09-29: the lists above can only offer what is installed,
        # so the models worth having that are not installed yet are offered
        # here, and pulling one asks for the lists again.
        self.download = DownloadRow("ollama", client_factory=self._client)
        self.download.finished.connect(lambda _n, _r: self.refresh())
        # Look again and Download on one line (owner, 2026-09-29); what Ollama
        # said about the installed models goes on the line below.
        look.addWidget(self.look_again)
        look.addWidget(self.download, 1)
        outer.addLayout(look)
        outer.addWidget(self.status)

        form = QFormLayout()
        outer.addLayout(form)
        self.web_part = QWidget()
        self.web_part.setObjectName("chatWebPart")
        web_form = QFormLayout(self.web_part)
        web_form.setContentsMargins(0, 8, 0, 0)
        self.web_intro = QLabel(WEB_INTRO)
        self.web_intro.setObjectName("chatWebNote")
        self.web_intro.setWordWrap(True)
        web_form.addRow(self.web_intro)
        box.addWidget(self.web_part)
        found = chat_settings()
        for setting in found:
            control = self._make(setting)
            control.setObjectName(setting.key)
            control.setToolTip(setting.help or setting.label)
            control.setAccessibleName(setting.label)
            self.controls[setting.key] = control
            target = web_form if setting.key.startswith(WEB_PREFIX) else form
            if isinstance(control, ModelCombo):
                self.roles.add_role(setting.label, control, setting.help)
            elif isinstance(control, QCheckBox):
                target.addRow(control)
            else:
                target.addRow(setting.label, control)
                if isinstance(control, QSpinBox) and envelope.for_setting(setting.key, None):
                    note = QLabel("")
                    note.setWordWrap(True)
                    note.setObjectName(setting.key + "_WHY")
                    note.hide()
                    self.notes[setting.key] = note
                    form.addRow("", note)
        self._add_describe()
        self.roles.finish()
        self._found = bool(found)
        # **Hide, never show, before there is a parent.** `setVisible(True)` on
        # a widget with no parent yet opens it as a window of its own, and this
        # box did, on every start, for as long as the rest of Settings took to
        # build - the "small window" between the splash and the main window
        # (2026-09-29; `test_window_opens`'s stray-window test names it). A
        # child that was never hidden appears with its page.
        if not self._found:
            self.hide()
        if settings is not None:
            self.load(settings)

    # -- building --------------------------------------------------------------------

    def _add_describe(self) -> None:
        """The Describe role: only models that can read pictures (4d)."""
        setting = _setting(DESCRIBE_KEY)
        label = setting.label if setting is not None else "Photo description model"
        help_text = setting.help if setting is not None else \
            "The model that describes photos. It has to be able to read pictures."
        combo = ModelCombo(DESCRIBE_KEY, vision=True, automatic=f"Automatic ({_DESCRIBE_FALLBACK})")
        combo.setObjectName("chatRoleDescribe")
        combo.setToolTip(help_text)
        combo.setAccessibleName(label)
        combo.activated.connect(lambda _i: self._emit_describe())
        self._describe = combo
        self.roles.add_role(label, combo, help_text)

    def _make(self, setting: Any) -> QWidget:
        if setting.key in ROLE_KEYS:
            combo = ModelCombo(setting.key, automatic="Automatic (the Local model)")
            combo.setToolTip(setting.help or setting.label)
            combo.activated.connect(lambda _i, s=setting: self._emit(s.key))
            return combo
        if setting.kind == "bool":
            box = QCheckBox(setting.label)
            box.setToolTip(setting.help or setting.label)
            box.toggled.connect(lambda _on, s=setting: self._emit(s.key))
            return box
        if setting.kind == "int":
            spin = QSpinBox()
            spin.setToolTip(setting.help or setting.label)
            low, high = int(setting.minimum or 0), int(setting.maximum or 1_000_000)
            bounds = envelope.for_setting(setting.key, None)
            if bounds is not None:
                low, high = max(low, bounds.floor), min(high, bounds.ceiling)
            spin.setRange(low, high)
            if setting.unit:
                spin.setSuffix(f" {setting.unit}")
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda _v, s=setting: self._emit(s.key))
            return spin
        if setting.kind == "choice":
            combo = QComboBox()
            combo.setToolTip(setting.help or setting.label)
            notes = CHOICE_NOTES.get(setting.key, {})
            for choice in setting.choices:
                combo.addItem(choice + notes.get(choice, ""), choice)
            combo.currentIndexChanged.connect(lambda _i, s=setting: self._emit(s.key))
            return combo
        line = QLineEdit()
        line.setToolTip(setting.help or setting.label)
        line.setAccessibleName(setting.label)
        line.setPlaceholderText(str(setting.default or ""))
        if "KEY" in setting.key:
            line.setEchoMode(QLineEdit.EchoMode.Password)      # a key is not read over a shoulder
        line.editingFinished.connect(lambda s=setting: self._emit(s.key))
        return line

    @staticmethod
    def _read(control: QWidget) -> Any:
        if isinstance(control, ModelCombo):
            return control.value()
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QSpinBox):
            return control.value()
        if isinstance(control, QComboBox):
            return control.currentData()
        return control.text().strip()

    def values(self) -> dict:
        return {key: self._read(control) for key, control in self.controls.items()}

    def _emit(self, key: str) -> None:
        self.changed.emit({key: self._read(self.controls[key])})
        if key in ROLE_KEYS:
            self._show_memory()

    def _emit_describe(self) -> None:
        if self._describe is not None:
            self.changed.emit({DESCRIBE_KEY: self._describe.value()})
            self._show_memory()

    # -- the mode: invisible outside Manual (3e) ----------------------------------------

    @property
    def manual(self) -> bool:
        return self._manual

    def set_manual(self, manual: bool) -> None:
        """Show the tuning part in Manual and hide it otherwise. The values stay either
        way. **The web section is always shown** - it is a privacy choice, not a tuning."""
        self._manual = bool(manual)
        self.manual_part.setVisible(self._manual)
        # Hide-only, as in `__init__`: `load` calls this from the constructor,
        # before the box has a parent, and `setVisible(True)` there opened it as
        # a window of its own. `_found` never changes after construction, so
        # there is nothing this ever needed to show again.
        if not self._found:
            self.hide()

    def showEvent(self, event: Any) -> None:               # noqa: N802 - Qt name
        super().showEvent(event)
        if self._manual and not self._asked:
            self.refresh()

    # -- what is installed (worker) ----------------------------------------------------

    def refresh(self) -> None:
        """Ask Ollama what is installed, on a worker. One at a time."""
        if self._probing:
            return
        self._asked = True
        self._probing = True
        self.look_again.setEnabled(False)
        self.status.setText("Looking for installed models...")
        worker = CallableWorker(self._run_probe, self._url, component="ui.chat.settings")
        worker.signals.finished.connect(self._probed)
        worker.signals.failed.connect(lambda _e: self._probed(InstalledModels()))
        run(QThreadPool.globalInstance(), worker)

    def _run_probe(self, url: str) -> InstalledModels:
        probe = self._probe
        if probe is None:
            from app.chat.llm import probe_installed

            probe = probe_installed
        return probe(url)

    def _probed(self, installed: Any) -> None:
        self._probing = False
        self.look_again.setEnabled(True)
        if not isinstance(installed, InstalledModels):
            installed = InstalledModels()
        self.set_installed(installed)

    def _client(self) -> Any:
        """Worker thread: a client for the address Chat uses. No I/O to build."""
        from app.llm.ollama import OllamaClient

        return OllamaClient(self._url)

    def _offer_downloads(self, installed: InstalledModels) -> None:
        have = set(installed.names)
        have |= {name.split(":")[0] for name in installed.names}
        offers = [(name, "for answering") for name in SUGGESTED]
        offers += [(name, f"reads pictures, {note}") for name, note in VISION_SUGGESTED]
        self.download.set_offers(
            (name, note) for name, note in offers
            if name not in have and name.split(":")[0] not in have)

    def set_installed(self, installed: InstalledModels) -> None:
        """Fill every drop-down from what is installed, keeping what was chosen."""
        self._installed = installed
        self._offer_downloads(installed)
        for key, control in self.controls.items():
            if isinstance(control, ModelCombo):
                control.populate(installed, control.value())
        if self._describe is not None:
            self._describe.populate(installed, self._describe.value())
        self.roles.show_reasons()
        n = len(installed.names)
        self.status.setText(
            f"{n} model{'s' if n != 1 else ''} installed." if installed.reachable else "")
        self._apply_envelope(installed.ram_mb)
        self._show_memory()

    def _apply_envelope(self, ram_mb: int) -> None:
        """Say why each bounded number has the range it has, and what Automatic uses."""
        profile = SimpleNamespace(ram_mb=int(ram_mb or 0))
        for key, note in self.notes.items():
            bounds = envelope.for_setting(key, profile)
            spin = self.controls.get(key)
            if bounds is None or not isinstance(spin, QSpinBox):
                continue
            spin.blockSignals(True)
            try:
                spin.setRange(bounds.floor, bounds.ceiling)
            finally:
                spin.blockSignals(False)
            note.setText(f"{bounds.why[:1].upper()}{bounds.why[1:]}. Automatic uses {bounds.auto}.")
            note.show()

    def _show_memory(self) -> None:
        installed = self._installed
        if installed is None or not installed.reachable:
            self.roles.set_ram_line("")
            return
        values = self.values()
        roles = resolve_roles(
            installed.names, configured=self._local_model,
            router=str(values.get("CHAT_ROUTER_MODEL", "") or ""),
            planner=str(values.get("CHAT_PLANNER_MODEL", "") or ""),
            answerer=str(values.get("CHAT_MODEL", "") or ""))
        names = list(roles.distinct())
        picture = (self._describe.value() if self._describe is not None else "") \
            or _DESCRIBE_FALLBACK
        if picture in installed.vision and picture not in names:
            names.append(picture)                # only counted when it is really installed
        self.roles.set_ram_line(ram_line(names, installed.sizes, installed.ram_mb))

    # -- loading -----------------------------------------------------------------------

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting - see `EditorBox.load` for why."""
        mode = str(getattr(settings, "index_tuning_mode", "") or "").strip().lower()
        self._url = str(getattr(settings, "ollama_url", "") or self._url)
        self._local_model = str(getattr(settings, "ollama_model", "") or self._local_model)
        for setting in chat_settings():
            control = self.controls.get(setting.key)
            if control is None:
                continue
            value = getattr(settings, setting.key.lower(), setting.default)
            control.blockSignals(True)
            try:
                if isinstance(control, ModelCombo):
                    control.populate(self._installed, str(value or ""))
                elif isinstance(control, QCheckBox):
                    control.setChecked(bool(value))
                elif isinstance(control, QSpinBox):
                    control.setValue(int(value))
                elif isinstance(control, QComboBox):
                    index = control.findData(value)
                    control.setCurrentIndex(index if index >= 0 else 0)
                else:
                    control.setText(str(value or ""))
            except (TypeError, ValueError):
                pass
            finally:
                control.blockSignals(False)
        if self._describe is not None:
            self._describe.populate(
                self._installed, str(getattr(settings, "ollama_vision_model", "") or ""))
        self.roles.show_reasons()
        self._show_memory()
        # "Nothing said" is Manual, the rule `ChatSettings.from_settings` follows.
        self.set_manual(mode in ("", "manual"))
