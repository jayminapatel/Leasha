"""Export contacts (order 1k 6j).

Layer: L5

Everything, the current selection or the contacts in view; as a vCard file (3.0 or 4.0, one file or
one per contact), a CSV in Google's, Outlook's or Leasha's own all-fields layout, an Excel sheet, or a
printable PDF (a list, or business cards). The dialog says in plain words what a layout cannot hold
before the person chooses a file (`exporters.LAYOUT_CAPS`), then reads the contacts and writes the
file on a worker with a progress line. Exports only ever read the book.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QWidget

from app.contacts.card import KINDS, Capabilities
from app.ui.contacts_tools._common import ToolDialog, btn, named, plural

__all__ = ["ExportDialog", "ExportResult", "write_export", "FORMATS", "left_out"]

#: (words, kind, file suffix, layout key in `exporters.LAYOUT_CAPS` or "")
FORMATS = (
    ("vCard 3.0 (works almost everywhere)", "vcard3", ".vcf", "vcard"),
    ("vCard 4.0", "vcard4", ".vcf", "vcard"),
    ("CSV for Google Contacts", "csv-google", ".csv", "google"),
    ("CSV for Outlook", "csv-outlook", ".csv", "outlook"),
    ("CSV with every field (Leasha's own layout)", "csv-plain", ".csv", "plain"),
    ("Excel spreadsheet (.xlsx)", "xlsx", ".xlsx", "xlsx"),
    ("PDF list to print", "pdf-list", ".pdf", ""),
    ("PDF business cards to print", "pdf-cards", ".pdf", ""),
)

_KIND_WORDS = {"email": "email addresses", "phone": "phone numbers", "address": "addresses", "url": "websites",
               "date": "dates", "related": "related people", "social": "social profiles", "im": "chat addresses",
               "custom": "custom fields"}


def left_out(caps: Capabilities) -> list[str]:
    """What a layout cannot hold, in words: it is shown before the person chooses a file."""
    out = []
    missing = [_KIND_WORDS.get(k, k) for k in KINDS if k not in caps.kinds]
    if missing:
        out.append(", ".join(missing))
    if not caps.photo:
        out.append("photos")
    if not caps.favourite:
        out.append("the favourite star")
    if not caps.groups:
        out.append("groups")
    if caps.date_labels is not None:
        out.append("dates other than birthday and anniversary")
    return out


@dataclass
class ExportResult:
    count: int = 0
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def write_export(hub: Any, ids: Optional[list[int]], kind: str, path: Any, *, one_file: bool = True,
                 on_progress: Optional[Callable[[str], None]] = None) -> ExportResult:
    """Read the contacts and write them. `ids` None means every contact. For one vCard per contact
    `path` is a folder; otherwise a file. Never overwrites a file in a folder it creates into."""
    from app.contacts import exporters                          # noqa: PLC0415
    from app.contacts._pdf import NothingToPrint                # noqa: PLC0415

    say = on_progress or (lambda _s: None)
    say("Reading the contacts…")
    if ids is None:
        cards = [c for _i, c in hub.all_cards()]
    else:
        cards = [c for c in (hub.get_card(i) for i in ids) if c is not None]
    if not cards:
        return ExportResult(error="There are no contacts to export. Choose some contacts, or clear the filter, and try again.")
    out = ExportResult(count=len(cards))
    path = Path(path)
    say(f"Writing {plural(len(cards), 'contact')}…")
    try:
        if kind in ("vcard3", "vcard4"):
            version = "3.0" if kind == "vcard3" else "4.0"
            if one_file:
                text = exporters.export_vcard(cards, version=version, one_file=True)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                out.files.append(str(path))
            else:
                path.mkdir(parents=True, exist_ok=True)
                for n, (name, text) in enumerate(exporters.export_vcard(cards, version=version, one_file=False), 1):
                    target = _unique(path / name)
                    with open(target, "w", encoding="utf-8", newline="") as fh:
                        fh.write(text)
                    out.files.append(str(target))
                    if n % 50 == 0:
                        say(f"Written {n} of {len(cards)}…")
        elif kind.startswith("csv-"):
            exporters.write_csv(cards, path, layout=kind.split("-", 1)[1])
            out.files.append(str(path))
        elif kind == "xlsx":
            exporters.export_xlsx(cards, path)
            out.files.append(str(path))
        elif kind == "pdf-list":
            out.warnings.extend(exporters.export_pdf_list(cards, path, "Contacts").warnings)
            out.files.append(str(path))
        elif kind == "pdf-cards":
            out.warnings.extend(exporters.export_pdf_cards(cards, path).warnings)
            out.files.append(str(path))
        else:
            return ExportResult(error=f"Leasha does not know how to export as {kind!r}.")
    except NothingToPrint as exc:
        return ExportResult(error=str(exc))
    return out


class ExportDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Export contacts", glyph="download",
                         subtitle="Save your contacts to a file, or make a printable list or business cards. "
                                  "This only reads your book; nothing in it changes.",
                         size=(720, 460), name="contacts-export")
        self.hub = ctx.hub
        self.total = 0
        self.last_result: Optional[ExportResult] = None
        selected = list(getattr(ctx, "selected_ids", []) or [])
        visible = list(getattr(ctx, "visible_ids", []) or [])

        scope_row = QHBoxLayout()
        label = QLabel("Which contacts:")
        named(label, "Scope label", "Which contacts to export")
        self.scope = QComboBox()
        named(self.scope, "Which contacts", "Everything, the contacts you selected, or the contacts in the current view")
        self.scope.setToolTip("Everything, the contacts you selected, or the contacts in the current view")
        self.scope.addItem("Everything in my book", None)
        if selected:
            self.scope.addItem(f"The selected contacts ({len(selected)})", selected)
        if visible:
            self.scope.addItem(f"The contacts in the current view ({len(visible)})", visible)
        if selected:
            self.scope.setCurrentIndex(1)
        scope_row.addWidget(label)
        scope_row.addWidget(self.scope, 1)
        self.body.addLayout(scope_row)

        fmt_row = QHBoxLayout()
        fmt_label = QLabel("Save as:")
        named(fmt_label, "Format label", "The file format")
        self.format = QComboBox()
        named(self.format, "File format", "What kind of file to make")
        self.format.setToolTip("What kind of file to make")
        for words, kind, suffix, layout in FORMATS:
            self.format.addItem(words, (kind, suffix, layout))
        self.format.currentIndexChanged.connect(lambda _i: self._format_changed())
        fmt_row.addWidget(fmt_label)
        fmt_row.addWidget(self.format, 1)
        self.body.addLayout(fmt_row)

        self.per_contact = QCheckBox("One file for each contact")
        named(self.per_contact, "One file for each contact", "Make a separate .vcf file for every contact, in a folder you choose")
        self.per_contact.setToolTip("Make a separate .vcf file for every contact, in a folder you choose")
        self.body.addWidget(self.per_contact)
        self.left_out = QLabel("")
        self.left_out.setWordWrap(True)
        named(self.left_out, "What will not be saved", "What this format cannot hold")
        self.body.addWidget(self.left_out)
        self.body.addStretch(1)

        self.export_button = btn("Export…", "file-down", "primary", "Choose where to save the file", "Export")
        self.export_button.clicked.connect(self.choose_and_export)
        self.add_footer_buttons(self.export_button)
        self._format_changed()

    # -- state ---------------------------------------------------------------
    def current_format(self) -> tuple[str, str, str]:
        return self.format.currentData()

    def scope_ids(self) -> Optional[list[int]]:
        return self.scope.currentData()

    def _format_changed(self) -> None:
        kind, _suffix, layout = self.current_format()
        self.per_contact.setEnabled(kind in ("vcard3", "vcard4"))
        if kind not in ("vcard3", "vcard4"):
            self.per_contact.setChecked(False)
        if layout:
            from app.contacts.exporters import LAYOUT_CAPS      # noqa: PLC0415

            gone = left_out(LAYOUT_CAPS[layout])
            self.left_out.setText("Everything is saved." if not gone else "This layout cannot hold: " + "; ".join(gone) + ".")
        else:
            self.left_out.setText("A PDF is for reading and printing, not for importing again. It shows name, company, "
                                  "email and phone." if kind == "pdf-list" else
                                  "Business cards show photo or initials, name, title, company, email and phone.")

    # -- exporting -----------------------------------------------------------
    def choose_and_export(self) -> None:
        kind, suffix, _layout = self.current_format()
        if self.per_contact.isChecked() and self.per_contact.isEnabled():
            folder = QFileDialog.getExistingDirectory(self, "Choose a folder for the vCard files")
            if folder:
                self.run_export(folder)
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save contacts as", f"contacts{suffix}", f"*{suffix}")
        if path:
            self.run_export(path if path.lower().endswith(suffix) else path + suffix)

    def run_export(self, path: str) -> None:
        """Write the file (a worker). `path` is a file, or a folder for one vCard per contact."""
        kind, _suffix, _layout = self.current_format()
        hub, ids = self.hub, self.scope_ids()
        one_file = not (self.per_contact.isChecked() and self.per_contact.isEnabled())
        self.busy(True, "Exporting…")
        self.export_button.setEnabled(False)
        self.job(lambda on_progress=None: write_export(hub, ids, kind, path, one_file=one_file, on_progress=on_progress),
                 self._exported, on_progress=self.say)

    def _exported(self, result: ExportResult) -> None:
        self.last_result = result
        self.export_button.setEnabled(True)
        if result.error:
            self.show_error(result.error)
            return
        where = result.files[0] if len(result.files) == 1 else f"{len(result.files)} files"
        extra = f" ({'; '.join(result.warnings)})" if result.warnings else ""
        self.busy(False, f"Saved {plural(result.count, 'contact')} to {where}.{extra}")
