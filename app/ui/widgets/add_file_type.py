r"""The Add file type wizard: three tiers, one dialog.

Layer: L5

Adding a file type meant knowing which of three mechanisms applied, then editing
TOML or writing a module and remembering four separate places to touch. The
knowledge was written down in `docs/adding-a-file-type.md`, which is exactly the
kind of document somebody reads once and then re-derives from memory, badly.

**The dialog asks the one question that decides the tier** - can something we
already have read this? - and then does as much of the rest as can be done:

| Tier | What this does |
|---|---|
| 1 | Writes the route. Nothing else is needed. |
| 2 | Writes the converter command, and refuses a binary that is not installed. |
| 3 | Generates the module, the registration and the pin; installs the library. |

**Tier 3 cannot be finished by a wizard, and says so plainly** rather than
producing something that looks complete and raises `NotImplementedError` on the
first file. It writes everything around the parsing - the contract, the lazy
import, the error handling, the registration - and leaves one marked `TODO`.

Nothing is written until the plan has been shown. `app/core/scaffold.py` holds
the generation and the refusals; this is only the questions.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

__all__ = ["AddFileTypeWizard", "TIER_ROUTE", "TIER_CONVERT", "TIER_CODE"]

TIER_ROUTE = "route"
TIER_CONVERT = "convert"
TIER_CODE = "code"


class AddFileTypeWizard(QDialog):
    """Ask what the format is, then do as much of the work as is safe."""

    def __init__(
        self,
        readers: list[str],
        binaries: dict[str, Optional[str]],
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a file type")
        self.setMinimumWidth(560)
        self._readers = readers
        self._binaries = binaries
        self._plan: Any = None

        self.extension = QLineEdit()
        self.extension.setPlaceholderText(".dxf")
        self.extension.textChanged.connect(lambda _t: self._refresh())

        heading = QFormLayout()
        heading.addRow("File extension", self.extension)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._route_tab(), "Use a reader we have")
        self.tabs.addTab(self._convert_tab(), "Convert it first")
        self.tabs.addTab(self._code_tab(), "Write a new reader")
        self.tabs.currentChanged.connect(lambda _i: self._refresh())

        self.summary = QLabel("")
        self.summary.setWordWrap(True)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #c62828;")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(heading)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self._refresh()

    # -- tier 1 --------------------------------------------------------------

    def _route_tab(self) -> QWidget:
        self.reader = QComboBox()
        self.reader.addItems(self._readers)
        if "plaintext" in self._readers:
            # Right for the overwhelming majority of what anybody adds here:
            # config formats, logs, and source in a language nobody listed.
            self.reader.setCurrentText("plaintext")

        explanation = QLabel(
            "For a format one of our readers already handles - anything that is "
            "really plain text under another name, or content a reader covers.\n\n"
            "This writes one line of configuration. Nothing to install, and it "
            "applies from the next index run."
        )
        explanation.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Read it with", self.reader)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(explanation)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    # -- tier 2 --------------------------------------------------------------

    def _convert_tab(self) -> QWidget:
        self.binary = QComboBox()
        for name, location in sorted(self._binaries.items()):
            label = name if location else f"{name}  (not installed)"
            self.binary.addItem(label, name)
        self.binary.currentIndexChanged.connect(lambda _i: self._refresh())

        self.command = QLineEdit()
        self.command.setPlaceholderText(
            "--headless --convert-to txt:Text --outdir {outdir} {input}"
        )
        self.produces = QLineEdit("{stem}.txt")
        self.produces.setAccessibleName("File the converter produces")
        self.then = QComboBox()
        self.then.addItems(self._readers)
        if "plaintext" in self._readers:
            self.then.setCurrentText("plaintext")

        explanation = QLabel(
            "For a format an external program can turn into something we read. "
            "The program must be one of the allowed converters - a configuration "
            "file that could name any executable would be a way to run anything.\n\n"
            "{input}, {outdir} and {stem} are filled in for each file."
        )
        explanation.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Convert with", self.binary)
        form.addRow("Arguments", self.command)
        form.addRow("It produces", self.produces)
        form.addRow("Then read with", self.then)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(explanation)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    # -- tier 3 --------------------------------------------------------------

    def _code_tab(self) -> QWidget:
        self.reader_name = QLineEdit()
        self.reader_name.setPlaceholderText("cad")
        self.reader_name.textChanged.connect(lambda _t: self._refresh())

        self.import_name = QLineEdit()
        self.import_name.setPlaceholderText("ezdxf")
        self.import_name.textChanged.connect(lambda _t: self._refresh())

        self.package_name = QLineEdit()
        self.package_name.setPlaceholderText("ezdxf")

        self.pin = QLineEdit()
        self.pin.setPlaceholderText("1.4.2  (optional)")

        self.provides = QLineEdit()
        self.provides.setPlaceholderText("text inside drawings")

        self.hard = QCheckBox("Without the library, nothing can be read at all")
        self.hard.setChecked(True)
        self.hard.setToolTip(
            "Ticked: the format shows as 'Cannot read' until the library is "
            "installed. Unticked: it shows as 'Limited' and files are still "
            "indexed by name."
        )

        self.install_now = QCheckBox("Install the library now")
        self.install_now.setChecked(True)

        self.cap = QSpinBox()
        self.cap.setRange(0, 4096)
        self.cap.setSuffix(" MB")
        self.cap.setSpecialValueText("default (100 MB)")

        explanation = QLabel(
            "For a format that needs real parsing. This generates the reader "
            "module, registers it, and pins the library - every step except the "
            "parsing itself, which is left as a marked TODO in the file.\n\n"
            "Note the import name and the package name are often different: "
            "python-docx imports as 'docx', pymupdf as 'fitz'."
        )
        explanation.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Reader name", self.reader_name)
        form.addRow("Python import", self.import_name)
        form.addRow("pip package", self.package_name)
        form.addRow("Version pin", self.pin)
        form.addRow("It provides", self.provides)
        form.addRow("", self.hard)
        form.addRow("Skip files over", self.cap)
        form.addRow("", self.install_now)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(explanation)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    # -- state ---------------------------------------------------------------

    @property
    def tier(self) -> str:
        return (TIER_ROUTE, TIER_CONVERT, TIER_CODE)[self.tabs.currentIndex()]

    def clean_extension(self) -> str:
        raw = self.extension.text().strip().lower()
        if raw and not raw.startswith("."):
            raw = f".{raw}"
        return raw

    def _refresh(self) -> None:
        """Keep the summary and the OK button honest as things are typed."""
        problem = self._problem()
        self.problem.setText(problem)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not problem)

        extension = self.clean_extension() or "the file type"
        if self.tier == TIER_ROUTE:
            self.summary.setText(
                f"{extension} will be read by {self.reader.currentText()}."
            )
        elif self.tier == TIER_CONVERT:
            self.summary.setText(
                f"{extension} will be converted by {self.binary.currentData()}, "
                f"then read by {self.then.currentText()}."
            )
        else:
            name = self.reader_name.text().strip() or "your reader"
            self.summary.setText(
                f"Generates app/extract/{name}.py, registers it, and pins "
                f"{self.package_name.text().strip() or 'the library'}. "
                "You then write the parsing."
            )

    def _problem(self) -> str:
        extension = self.clean_extension()
        if len(extension) < 2:
            return "Enter a file extension, for example .dxf"
        if " " in extension or extension.count(".") > 1:
            return f"{extension!r} is not a file extension."

        if self.tier == TIER_CONVERT:
            binary = self.binary.currentData()
            if not self._binaries.get(binary):
                return (
                    f"{binary} is not installed on this machine, so every "
                    f"{extension} file would fail. Install it first, or choose "
                    "another converter."
                )
            if not self.command.text().strip():
                return "Give the arguments to pass to the converter."

        if self.tier == TIER_CODE:
            if not self.reader_name.text().strip():
                return "Give the reader a name, for example 'cad'."
            if self.package_name.text().strip() and not self.import_name.text().strip():
                return (
                    "Give the Python import name too - it is often different "
                    "from the package name."
                )
        return ""

    # -- results -------------------------------------------------------------

    def route(self) -> tuple[str, str]:
        return self.clean_extension(), self.reader.currentText()

    def converter(self) -> dict[str, Any]:
        binary = self.binary.currentData()
        arguments = self.command.text().strip().split()
        return {
            "extension": self.clean_extension(),
            "command": [binary, *arguments],
            "produces": self.produces.text().strip() or "{stem}.txt",
            "then": self.then.currentText(),
            "enabled": True,
        }

    def spec(self) -> Any:
        """The scaffold spec for tier 3."""
        from app.core.scaffold import ExtractorSpec

        return ExtractorSpec(
            name=self.reader_name.text().strip().lower(),
            extensions=(self.clean_extension(),),
            module=self.import_name.text().strip(),
            package=self.package_name.text().strip(),
            provides=self.provides.text().strip(),
            hard=self.hard.isChecked(),
            max_bytes=self.cap.value() * (1 << 20),
            version=self.pin.text().strip(),
        )

    def accept(self) -> None:
        """For tier 3, show the plan before anything is written.

        A generator that writes into the application's own source tree without
        showing what it will touch is asking for trust it has not earned.
        """
        if self._problem():
            return
        if self.tier != TIER_CODE:
            super().accept()
            return

        try:
            from app.core.scaffold import plan

            self._plan = plan(self.spec())
        except Exception as exc:                 # noqa: BLE001 - shown, not raised
            error = getattr(exc, "error", None)
            self.problem.setText(error.render() if error is not None else str(exc))
            return

        if not ConfirmScaffoldDialog(self._plan, self).exec():
            return

        try:
            from app.core.scaffold import apply

            apply(self._plan)
        except Exception as exc:                 # noqa: BLE001
            error = getattr(exc, "error", None)
            self.problem.setText(error.render() if error is not None else str(exc))
            return

        super().accept()

    def wants_install(self) -> bool:
        """Whether the caller should install the library after this closes.

        **The install itself is not done here.** pip can take a minute, and the
        UI thread never does I/O - so the editor starts it on a worker once the
        dialog is gone, and reports the result in its status line.
        """
        return bool(self.install_now.isChecked() and self.spec().package)

    def plan_result(self) -> Any:
        return self._plan


class ConfirmScaffoldDialog(QDialog):
    """What is about to be written, before it is written."""

    def __init__(self, scaffold: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Files to be created")
        self.setMinimumSize(680, 520)

        heading = QLabel(
            "These files will be created or amended in the application's own "
            "folder. Nothing is overwritten - an existing file stops the whole "
            "operation."
        )
        heading.setWordWrap(True)

        listing = QPlainTextEdit()
        listing.setReadOnly(True)
        listing.setPlainText(_describe(scaffold))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Write them")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(listing, 1)
        layout.addWidget(buttons)


def _describe(scaffold: Any) -> str:
    lines: list[str] = []
    for change in scaffold.changes:
        verb = "CREATE" if change.action == "create" else "AMEND "
        lines.append(f"{verb}  {change.path}")
        if change.summary:
            lines.append(f"         {change.summary}")
    lines.append("")
    lines.append("Then:")
    for number, note in enumerate(scaffold.notes, start=1):
        lines.append(f"  {number}. {note}")
    lines.append("")
    lines.append("-" * 68)
    lines.append("The generated reader:")
    lines.append("-" * 68)
    created = next((c for c in scaffold.changes if c.action == "create"), None)
    if created is not None:
        lines.append(created.content)
    return "\n".join(lines)
