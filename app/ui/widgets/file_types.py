r"""The file-types editor: what gets indexed, and whether it actually works.

Layer: L5

`config/extractors.toml` decides what is read; this is where that is managed
without finding a file on disk and editing TOML correctly.

**The column that matters is Status.** Until it existed, a format could be
switched on, correctly routed, and fail on every single file - `.doc` with no
LibreOffice, `.msg` with no extract-msg - and nothing anywhere said so. The
person saw an empty result set and concluded the search was bad. Every row now
states whether it reads files today, and when it does not, the exact command
that would fix it. That is the difference between configuration and management.

**Health is read from `core.format_health`, never probed here.** `doctor` shows
the same states from the same function, so the two cannot disagree - a format
reported healthy in Settings and broken in doctor is a support call nobody can
answer.

**Double-click a row to change its reader and size cap**; the checkbox alone was
not enough, and doing nothing on double-click is worse than either - it is the
first thing anybody tries on a table of settings. The consequences of a large cap
are real (an OCR cap at 100MB is minutes of work per image), so the dialog says
so at the moment the number is raised rather than leaving it to be discovered
during a run.

**Adding a type is a wizard covering all three tiers** - see
`widgets/add_file_type.py`. Tier 1 writes a route, Tier 2 writes a converter
block, and Tier 3 generates the reader module, registers it and pins the
library. Only the parsing is left to write, and it says so plainly rather than
producing something that looks finished.

**Changes are saved as differences, never as a snapshot**, so an upgrade still
delivers new defaults. And **nothing takes effect until the next index run** -
it says so, because a format switched on does not retrospectively index the
files already skipped, and somebody not told that concludes it did not work.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.no_scroll import protect_view
from app.ui.widgets.result_table import align_headers
from app.ui.widgets.buttons import style_all
from app.ui.widgets.number_field import fit_all as fit_number_fields
from app.ui.qtsip import open_menu

__all__ = ["FileTypesEditor", "EditFileTypeDialog"]

#: Status column text and colour per health state. Colours are chosen to survive
#: both themes and to remain distinguishable without relying on hue alone - the
#: word carries the meaning, the colour only speeds the scan.
_STATE_LABEL = {
    "ready": ("Ready", "#2e7d32"),
    "degraded": ("Limited", "#e65100"),
    "blocked": ("Cannot read", "#c62828"),
    "off": ("Off", "#757575"),
}


class EditFileTypeDialog(QDialog):
    """One file type's reader, size cap and on/off state.

    **Opened by double-clicking the row**, which is what a table of settings
    implies and what people try first. Before this, double-clicking did nothing
    at all and the only editable thing was the checkbox - so changing a size cap
    meant finding a TOML file on disk, which is the situation the editor exists
    to end.

    The cap is in MB because that is the unit people think in; bytes are what
    the file stores, and the conversion belongs here rather than in somebody's
    head.
    """

    #: Above this, a cap is doing nothing useful and is more likely a typo -
    #: the largest thing anybody indexes is a mail archive, and those are read
    #: through Outlook rather than by size.
    MAX_MB = 4096

    def __init__(
        self,
        extension: str,
        reader: str,
        readers: list[str],
        max_bytes: int,
        enabled: bool,
        *,
        editable_reader: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{extension} settings")
        self._extension = extension

        self.enabled = QCheckBox("Index files of this type")
        self.enabled.setToolTip(
            "Turn this off and files of this type are recorded by name only - "
            "still findable, contents not searchable. Nothing already indexed "
            "is removed until the next run.")
        self.enabled.setChecked(enabled)

        self.reader = QComboBox()
        self.reader.addItems(readers)
        if reader in readers:
            self.reader.setCurrentText(reader)
        self.reader.setEnabled(editable_reader)
        if not editable_reader:
            # A converter route's reader is the converter's `then`, decided by
            # what the converter produces. Offering to change it here would be
            # offering a setting that cannot work.
            self.reader.setToolTip(
                "This type is converted first, so its reader is fixed by the "
                "converter. Change it in config\\extractors.toml."
            )

        self.limit = QSpinBox()
        self.limit.setToolTip(
            "The largest file of this type worth opening.\n\n"
            "Above it the file is indexed by name only. A ceiling stops one "
            "enormous file holding up a run, which on a large corpus is the "
            "difference between hours and days.")
        self.limit.setRange(1, self.MAX_MB)
        self.limit.setSuffix(" MB")
        self.limit.setValue(max(1, round(max_bytes / (1 << 20))))

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #e65100;")
        self.limit.valueChanged.connect(self._warn_about_cost)
        self._warn_about_cost(self.limit.value())

        form = QFormLayout()
        form.addRow("", self.enabled)
        form.addRow("Read by", self.reader)
        form.addRow("Skip files over", self.limit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        note = QLabel(
            "Applies from the next index run. Files already skipped are not "
            "re-read until then."
        )
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.warning)
        layout.addWidget(note)
        layout.addWidget(buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)
        # Number fields: typed, no arrows, a back-to-default button.
        fit_number_fields(self)

    def _warn_about_cost(self, value: int) -> None:
        """Say what a large cap costs, for the types where it is not obvious.

        OCR is the one that bites: its cost scales with pixels, so a cap raised
        to 100MB is minutes of work per image on a corpus that may hold
        thousands. A number box that silently makes an index run twenty times
        longer is not a kindness.
        """
        reader = self.reader.currentText()
        if reader == "ocr" and value > 25:
            self.warning.setText(
                f"OCR cost scales with image size - at {value}MB a single scan "
                "can take minutes, and a folder of them can dominate an entire "
                "index run. 25MB is the shipped cap."
            )
        elif value > 512:
            self.warning.setText(
                f"{value}MB is large. One file that size is read, chunked and "
                "embedded in a single pass and will hold up the run behind it."
            )
        else:
            self.warning.setText("")

    def value(self) -> tuple[str, int, bool]:
        """`(reader, max_bytes, enabled)`."""
        return (
            self.reader.currentText(),
            int(self.limit.value()) * (1 << 20),
            self.enabled.isChecked(),
        )


class FileTypesEditor(QGroupBox):
    """A row per file type: on/off, what reads it, whether that works, and the fix."""

    #: `{extension: enabled}` for everything that differs from the defaults.
    changes_saved = Signal(dict)
    error = Signal(object)

    def __init__(self, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__("File types", parent)
        self._settings = settings
        self._rules: Any = None
        self._boxes: dict[str, QCheckBox] = {}
        self._statuses: dict[str, Any] = {}
        self._rows: list[dict] = []
        self._added: dict[str, str] = {}
        self._new_routes: dict[str, str] = {}
        self._removed: set[str] = set()

        self.summary = QLabel("")
        self.summary.setWordWrap(True)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by extension or reader, e.g. pdf, ocr, converter")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)

        self.problems_only = QCheckBox("Only show what needs attention")
        self.problems_only.setToolTip(
            "Show only the types that cannot be read right now - a missing "
            "library, a converter that is not installed, a reader that failed.")
        self.problems_only.stateChanged.connect(lambda _s: self._apply_filter())

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Index", "Type", "Read by", "Status", "Limit"]
        )
        # §2b: Qt centres a heading and left-aligns its column, which is the
        # mismatch the owner reported. **Not made sortable**, and that is a
        # decision with a reason: column 0 is a `QCheckBox` inside a
        # `setCellWidget` holder, and Qt moves item *data* when it sorts and
        # not cell widgets - so after one header click every checkbox would
        # sit against the wrong row. The widget is there for the accessible
        # names argued for in `_fill`, which a checkable item cannot carry.
        align_headers(self.table)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu_at)
        # Double-click is what people try first on a table of settings, and it
        # used to do nothing whatsoever.
        self.table.cellDoubleClicked.connect(lambda row, _column: self.edit_row(row))
        # Otherwise scrolling the Settings page stops dead the moment the
        # pointer crosses this table - see no_scroll.ViewWheelGuard.
        protect_view(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(240)

        self.all_button = QPushButton("Select all")
        self.all_button.setToolTip(
            "Tick every type shown. With a filter applied, only the rows you "
            "can see are changed."
        )
        self.all_button.clicked.connect(lambda _checked=False: self.set_all(True))

        self.none_button = QPushButton("Select none")
        self.none_button.setToolTip(
            "Untick every type shown. Anything unticked is never opened at all."
        )
        self.none_button.clicked.connect(lambda _checked=False: self.set_all(False))

        self.add_button = QPushButton("Add file type...")
        self.add_button.setToolTip(
            "Teach the indexer a file type it does not know yet, by naming the "
            "reader or the command that turns it into text.")
        self.add_button.clicked.connect(lambda _checked=False: self.add_type())

        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.setToolTip(
            "Switch every supported file type back on with its shipped reader "
            "and size limit, and remove any types added here."
        )
        self.reset_button.clicked.connect(lambda _checked=False: self.reset_to_defaults())

        self.save_button = QPushButton("Save file types")
        self.save_button.setToolTip(
            "Write these choices to extractors.toml. They take effect on the "
            "next index run; nothing already indexed changes until then.")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda _checked=False: self.save())

        self.status = QLabel("")
        self.status.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(self.filter, 1)
        controls.addWidget(self.problems_only)

        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.all_button)
        buttons.addWidget(self.none_button)
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        buttons.addWidget(self.reset_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

        self.reload()

    # -- reading -------------------------------------------------------------

    def reload(self) -> None:
        """Read the rules and rebuild the table. Never raises.

        A configuration file with a mistake in it must not stop Settings
        opening - Settings is where somebody would go to fix it.
        """
        try:
            from app.core.formats import added_routes, load_rules

            self._rules = load_rules(self._settings.data_path)
            self._added = added_routes(self._rules)
        except Exception as exc:                 # noqa: BLE001 - reported, not raised
            self.summary.setText(
                f"Could not read the file-type settings: {exc}\n"
                "Delete extractors.toml in your index folder to start again - "
                "the shipped defaults are always restored by removing it."
            )
            self.table.setRowCount(0)
            return

        self._statuses = {s.extension: s for s in self._health()}
        self._new_routes.clear()
        self._removed.clear()
        self._fill()
        self.save_button.setEnabled(False)
        self.status.setText("")

    def _health(self) -> list[Any]:
        """Per-format health. Never raises: a probe that fails must not empty
        the table - the rows are still the only way to switch anything off."""
        try:
            from app.core.format_health import format_health

            return format_health(self._rules)
        except Exception:                        # noqa: BLE001
            return []

    def _registry(self) -> dict:
        """The live extension -> extractor map. Empty if extraction cannot be
        imported at all, which must not stop Settings opening."""
        try:
            from app.extract.base import REGISTRY

            return dict(REGISTRY)
        except Exception:                        # noqa: BLE001
            return {}

    def _fill(self) -> None:
        # With the registry, so the table lists everything the app reads rather
        # than only what configuration happens to mention - without it there was
        # no `.pdf` row and no way to switch PDFs off.
        rows = self._rules.describe(self._registry())
        self._rows = rows
        self.table.setRowCount(len(rows))
        self._boxes.clear()

        for index, row in enumerate(rows):
            extension = row["extension"]
            status = self._statuses.get(extension)

            box = QCheckBox()
            # **The column heading is not the label.** A checkbox in a table
            # cell has no text of its own, so a screen reader announces sixty
            # identical "check box, not checked" and the row it belongs to is
            # visual information only. The extension is the label.
            box.setAccessibleName(f"Index {extension} files")
            box.setAccessibleDescription(
                f"{extension} is read by {row['extractor']}"
            )
            box.setToolTip(f"Index {extension} files")
            box.setChecked(bool(row["enabled"]))
            box.stateChanged.connect(lambda _s: self._mark_dirty())
            holder = QWidget()
            holder.setObjectName("rowCell")       # the row shows through (theme)
            centred = QHBoxLayout(holder)
            centred.setContentsMargins(0, 0, 0, 0)
            centred.addWidget(box, alignment=Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(index, 0, holder)
            self._boxes[extension] = box

            name = QTableWidgetItem(extension)
            if extension in self._added:
                # Says where the row came from, because only these can be removed.
                name.setToolTip("Added by you. Right-click to remove it.")
                name.setText(f"{extension}  *")
            self.table.setItem(index, 1, name)

            reader = QTableWidgetItem(row["extractor"])
            if row["note"]:
                reader.setToolTip(row["note"])
            self.table.setItem(index, 2, reader)

            self.table.setItem(index, 3, self._status_item(status, row))
            self.table.setItem(index, 4, QTableWidgetItem(_human(row["max_bytes"])))

        self._summarise(rows)
        self._apply_filter()

    def _status_item(self, status: Any, row: dict) -> QTableWidgetItem:
        if status is None:
            return QTableWidgetItem("-")

        label, colour = _STATE_LABEL.get(status.state, (status.state, ""))
        text = label
        if status.state in ("blocked", "degraded") and status.fix:
            # The fix on the row itself, not hidden in a tooltip: a tooltip is
            # not discoverable, and this is the one thing the person must act on.
            text = f"{label} - {status.fix}"

        item = QTableWidgetItem(text)
        if status.detail:
            item.setToolTip(f"{status.detail}\n\n{status.fix}".strip())
        if colour:
            item.setForeground(QColor(colour))
        return item

    def _summarise(self, rows: list) -> None:
        from app.core.format_health import summarise

        counts = summarise(self._statuses.values()) if self._statuses else {}
        blocked = counts.get("blocked", 0)
        degraded = counts.get("degraded", 0)
        on = sum(1 for row in rows if row["enabled"])

        text = (
            f"{on} of {len(rows)} file types are indexed. "
            "Anything switched off is never opened at all."
        )
        if blocked:
            text += (
                f"\n{blocked} type(s) are switched on but cannot read anything - "
                "their rows say what to install. Files of those types are still "
                "found by name."
            )
        if degraded:
            text += f"\n{degraded} type(s) work with something optional missing."
        text += (
            "\nSize limits and built-in routing live in config\\extractors.toml, "
            "where the reason for each is written down."
        )
        self.summary.setText(text)

    # -- filtering -----------------------------------------------------------

    def _apply_filter(self) -> None:
        """Hide rows that do not match. Sixty-odd formats is a scroll, and the
        one being looked for is never the one on screen."""
        needle = self.filter.text().strip().lower()
        problems_only = self.problems_only.isChecked()

        for row in range(self.table.rowCount()):
            extension_item = self.table.item(row, 1)
            reader_item = self.table.item(row, 2)
            if extension_item is None or reader_item is None:
                continue
            extension = extension_item.text().replace(" *", "").strip()
            haystack = f"{extension} {reader_item.text()}".lower()

            status = self._statuses.get(extension)
            interesting = status is not None and status.state in ("blocked", "degraded")

            hide = (needle and needle not in haystack) or (problems_only and not interesting)
            self.table.setRowHidden(row, bool(hide))

    # -- adding and removing --------------------------------------------------

    def _readers(self) -> list[str]:
        try:
            from app.extract.base import extractor_names

            return sorted(extractor_names())
        except Exception:                        # noqa: BLE001
            return ["plaintext"]

    def _converter_binaries(self) -> dict:
        try:
            from app.extract.converter import available_binaries

            return available_binaries()
        except Exception:                        # noqa: BLE001
            return {}

    def add_type(self) -> None:
        """The three-tier wizard: route it, convert it, or generate a reader."""
        from app.ui.widgets.add_file_type import (
            TIER_CODE,
            TIER_CONVERT,
            TIER_ROUTE,
            AddFileTypeWizard,
        )

        wizard = AddFileTypeWizard(self._readers(), self._converter_binaries(), parent=self)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return

        extension = wizard.clean_extension()
        if extension in self._rules.extensions and wizard.tier == TIER_ROUTE:
            self.status.setText(
                f"{extension} is already listed - find it in the table to change it."
            )
            return

        if wizard.tier == TIER_ROUTE:
            self._stage_route(*wizard.route())
        elif wizard.tier == TIER_CONVERT:
            self._stage_converter(wizard.converter())
        elif wizard.tier == TIER_CODE:
            # The files are already written by the wizard - it shows the plan
            # first, so the decision was made there rather than here.
            spec = wizard.spec()
            self.status.setText(
                f"Created app/extract/{spec.name}.py and registered it. "
                "Write the parsing where the TODO is, then restart - extractors "
                "register on import, so a running process will not see it."
            )
            self.reload()
            if wizard.wants_install():
                self._install(spec.package, spec.version)

    def _install(self, package: str, version: str) -> None:
        """pip install on a worker, never on the UI thread.

        A cold pip install is tens of seconds, which inline is a white window -
        and the rule here is not a preference. `presenter.install_package` is
        the blocking half; this is only the wiring and the reporting.
        """
        try:
            from PySide6.QtCore import QThreadPool

            from app.ui.presenter import install_package
            from app.ui.workers import CallableWorker, run
        except ImportError as exc:               # pragma: no cover - partial install
            self.status.setText(f"Install it by hand: pip install {package} ({exc})")
            return

        target = f"{package}=={version}" if version else package
        self.status.setText(f"Installing {target}...")

        worker = CallableWorker(install_package, package, version,
                                component="ui.file_types")
        worker.signals.finished.connect(self._installed)
        worker.signals.failed.connect(
            lambda error: self.status.setText(
                f"Could not install {target}: {error.render()}"
            )
        )
        run(QThreadPool.globalInstance(), worker)

    def _installed(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        if result.get("ok"):
            # The health column is computed from what is importable, so it is
            # stale the moment an install succeeds.
            self.reload()
            self.status.setText(
                f"Installed {result['package']}. Write the parsing in the "
                "generated module, then restart."
            )
            return
        self.status.setText(
            f"The files were written, but installing {result['package']} failed: "
            f"{result['detail']}\nRun it by hand: {result['fix']}"
        )

    def _stage_route(self, extension: str, reader: str) -> None:
        try:
            from app.core.formats import with_route

            self._rules = with_route(self._rules, extension, reader)
        except Exception as exc:                 # noqa: BLE001
            self.status.setText(f"Could not add {extension}: {exc}")
            return

        self._new_routes[extension] = reader
        self._added[extension] = reader
        self._removed.discard(extension)
        self._statuses = {s.extension: s for s in self._health()}
        self._fill()
        self._mark_dirty()
        self.status.setText(
            f"{extension} will be read by {reader}. Save to keep it - it applies "
            "from the next index run."
        )

    def _stage_converter(self, rule: dict) -> None:
        """Write a converter route straight to the user's file.

        Not staged behind Save like the switches are: a converter is a whole
        block rather than a flag, and `save_overrides` writes extensions only.
        Appending it here keeps one writer per section instead of teaching the
        switch-saver about commands it would then have to round-trip.
        """
        try:
            from app.core.formats import append_converter

            path = append_converter(self._settings.data_path, rule)
        except Exception as exc:                 # noqa: BLE001
            error = getattr(exc, "error", None)
            self.status.setText(
                f"Could not add the converter: "
                f"{error.render() if error is not None else exc}"
            )
            return

        self.reload()
        self.status.setText(
            f"{rule['extension']} will be converted by {rule['command'][0]} and "
            f"read by {rule['then']}. Written to {path.name}; it applies from "
            "the next index run."
        )

    def edit_row(self, row: int) -> None:
        """Open one file type's settings. Wired to double-click."""
        item = self.table.item(row, 1)
        if item is None or self._rules is None:
            return
        extension = item.text().replace(" *", "").strip()

        row_data = next((r for r in self._rows if r["extension"] == extension), None)
        if row_data is None:
            return
        converter = self._rules.converters.get(extension)
        converted = converter is not None and extension not in self._registry()

        box = self._boxes.get(extension)
        enabled = box.isChecked() if box is not None else bool(row_data["enabled"])

        dialog = EditFileTypeDialog(
            extension,
            reader=row_data["extractor"].replace("converter -> ", ""),
            readers=self._readers(),
            max_bytes=self._rules.max_bytes_for(extension),
            enabled=enabled,
            # A converted type's reader is decided by what the converter
            # produces, so it is shown but not editable.
            editable_reader=not converted,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        reader, max_bytes, now_enabled = dialog.value()

        try:
            from app.core.formats import with_override, with_route

            if not converted:
                # `with_route` creates the rule if there is none and re-points
                # the reader if there is; the cap then applies to whichever it
                # was. One call each, in that order, or a new rule is written
                # with the default cap after the chosen one was set.
                self._rules = with_route(self._rules, extension, reader,
                                         enabled=now_enabled)
                self._rules = with_override(self._rules, extension,
                                            max_bytes=max_bytes)
        except Exception as exc:                 # noqa: BLE001
            self.status.setText(f"Could not change {extension}: {exc}")
            return

        if box is not None:
            box.setChecked(now_enabled)
        self._statuses = {s.extension: s for s in self._health()}
        self._fill()
        self._mark_dirty()
        self.status.setText(f"{extension} updated. Save to keep the change.")

    def _menu_at(self, point) -> None:
        """Right-click: copy the fix, or remove a row this machine added."""
        row = self.table.rowAt(point.y())
        if row < 0:
            return
        item = self.table.item(row, 1)
        if item is None:
            return
        extension = item.text().replace(" *", "").strip()

        menu = QMenu(self)
        status = self._statuses.get(extension)

        edit = menu.addAction(f"Edit {extension}...")
        edit.triggered.connect(lambda _checked=False, r=row: self.edit_row(r))
        menu.addSeparator()

        if status is not None and status.fix:
            copy_fix = menu.addAction("Copy the fix command")
            copy_fix.triggered.connect(
                lambda _checked=False, text=status.fix: _to_clipboard(text)
            )

        if extension in self._added:
            remove = menu.addAction(f"Remove {extension}")
            remove.triggered.connect(
                lambda _checked=False, ext=extension: self.remove_type(ext)
            )
        else:
            disabled = menu.addAction("Built in - cannot be removed here")
            disabled.setEnabled(False)

        if not menu.isEmpty():
            open_menu(menu, self.table.viewport().mapToGlobal(point))

    def remove_type(self, extension: str) -> None:
        """Drop a user-added route. Built-ins are not offered, because removing
        one from a text file would not stop its extractor claiming the
        extension - it would only look as though it had."""
        try:
            from app.core.formats import without_route

            self._rules = without_route(self._rules, extension)
        except Exception as exc:                 # noqa: BLE001
            self.status.setText(f"Could not remove {extension}: {exc}")
            return

        self._new_routes.pop(extension, None)
        self._added.pop(extension, None)
        self._removed.add(extension)
        self._fill()
        self._mark_dirty()
        self.status.setText(f"{extension} removed. Save to keep the change.")

    def reset_to_defaults(self) -> None:
        """Restore every shipped file type, reader and size limit.

        **Deletes the override file rather than writing "all on".** Writing the
        current state back as the new baseline would pin today's defaults
        forever: a format added in a later release would arrive switched off,
        and a limit improved upstream would never reach this machine. Removing
        the file is the one action that genuinely means "as shipped", now and
        after every upgrade.

        Types added here go too - they only ever existed in that file.
        """
        from PySide6.QtWidgets import QMessageBox

        added = len(self._added)
        question = (
            "Switch every supported file type back on, with the reader and size "
            "limit it shipped with?"
        )
        if added:
            question += f"\n\nThe {added} type(s) you added here will be removed."

        confirmed = QMessageBox.question(
            self, "Reset file types", question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        try:
            from app.core.formats import user_path

            override = user_path(self._settings.data_path)
            existed = override.is_file()
            override.unlink(missing_ok=True)
        except OSError as exc:
            self.status.setText(
                f"Could not reset: {exc}\n"
                f"Delete this file by hand to restore the defaults: {override}"
            )
            return

        self.reload()
        self.status.setText(
            "Reset to the shipped defaults - every supported file type is on, "
            "with its original reader and size limit. Applies from the next "
            "index run."
            if existed else
            "Already at the shipped defaults - nothing was overridden."
        )

    def set_all(self, enabled: bool) -> int:
        """Tick or untick every type **currently shown**. Returns how many changed.

        **Visible rows only, and that is the whole point.** Filter to `ocr` and
        "select none" should switch off the eight image types, not the eighty
        formats behind the filter. A button that silently acts on rows somebody
        cannot see is a button that turns one decision into eighty, and the
        person finds out at the next index run.

        The filter is named in the status line for the same reason: when the
        count is smaller than the table, it must be obvious why.
        """
        changed = 0
        shown = 0
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 1)
            if item is None:
                continue
            shown += 1
            box = self._boxes.get(item.text().replace(" *", "").strip())
            if box is not None and box.isChecked() != enabled:
                box.setChecked(enabled)
                changed += 1

        if changed:
            self._mark_dirty()

        word = "on" if enabled else "off"
        filtered = shown < len(self._boxes)
        self.status.setText(
            f"Switched {word} {changed} of the {shown} type(s) shown"
            + (" by the current filter. " if filtered else ". ")
            + ("Save to keep the change." if changed else "Nothing to change.")
        )
        return changed

    def _mark_dirty(self) -> None:
        self.save_button.setEnabled(True)

    # -- saving --------------------------------------------------------------

    def current(self) -> dict[str, bool]:
        return {extension: box.isChecked() for extension, box in self._boxes.items()}

    def save(self) -> None:
        """Write the differences and say what happens next.

        Off the UI thread would be over-engineering: this writes a file of a few
        dozen lines. It is wrapped instead, because a read-only index folder
        should produce a sentence rather than a traceback.
        """
        if self._rules is None:
            return

        try:
            from app.core.formats import (
                added_routes,
                changed_limits,
                differences,
                save_overrides,
                with_override,
            )

            updated = self._rules
            for extension, enabled in self.current().items():
                if updated.converters.get(extension) and extension not in self._registry():
                    # A converter route's on/off lives in its own section, which
                    # this editor does not write. Skipped rather than turned into
                    # an extension rule that would shadow the converter entirely.
                    continue
                # No `extractor`: a policy-only override, so switching off a
                # built-in format never copies the reader's name into the user's
                # config where a rename would strand it.
                updated = with_override(updated, extension, enabled=enabled)

            changes = differences(updated)
            routes = added_routes(updated)
            limits = changed_limits(updated)
            path = save_overrides(self._settings.data_path, changes, routes, limits)
        except Exception as exc:                 # noqa: BLE001
            self.status.setText(f"Could not save: {exc}")
            return

        self._rules = updated
        self._new_routes.clear()
        self._removed.clear()
        self.save_button.setEnabled(False)
        self.changes_saved.emit(changes)

        # The `/type` menu offers every enabled format, and it caches the list
        # rather than re-reading two TOML files on a keystroke. This is the
        # moment that list stops being true - without it a format switched on
        # here is missing from the filter menu until the window is restarted,
        # which is the same "the setting did not work" the status line below is
        # written to prevent.
        from app.ui.presenter import clear_format_catalogue
        clear_format_catalogue()

        # **Says what it does not do.** A format switched on does not
        # retrospectively index the files already skipped, and somebody not told
        # that concludes the setting did not work.
        total = len({*changes, *routes, *limits})
        if total:
            self.status.setText(
                f"Saved {total} change(s) to {path.name}. "
                "They apply to the next index run - files already skipped are "
                "not re-read until then."
            )
        else:
            self.status.setText(
                "Nothing differs from the defaults, so no overrides are stored."
            )


def _to_clipboard(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def _human(count: int) -> str:
    # 2026-10-04, code review: `row_facts.format_size`, the one size wording.
    # This copy wrote "4.2MB" and stopped at GB; it now reads "4.2 MB".
    from app.core.row_facts import format_size

    return format_size(count)
