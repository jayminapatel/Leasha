r"""Excel `.xlsx` cell text straight from the ZIP, without openpyxl's reader.

Layer: L2

Measured 2026-09-20 with cProfile on a real 880 KB dashboard workbook: 4.5 s per
read, and none of it was the cells a person can search for. openpyxl parsed 989
`conditionalFormatting` blocks (1.7 s), built descriptor objects for every column
and row property (`from_tree`: 3.7 s cumulative) and read a shared-string table
of 22,000 entries through the same object model (1.4 s). `read_only=True` already
skips most of that for cells, but not for the sheet's own metadata, which the
parser walks after the last row.

What is needed is small: the sheet names and their order from `workbook.xml`, the
shared-string table, and for each sheet the `<c>` elements of `<sheetData>`. This
reads exactly that, with `ElementTree.iterparse`, and stops at the row cap.

**Exactly openpyxl's values, because the index must not change.** Each cell is
converted the way openpyxl's own `WorkSheetParser.parse_cell` converts it, and the
shared-string table the way `read_string_table` does (including its removal of the
literal `x005F_` that Excel's escaping leaves behind): a number is an `int` unless
the text has `.`, `E` or `e`, a boolean is `True`/`False`, an error stays its `#N/A`
text, and a cell whose *style* is a date format becomes the `datetime` openpyxl
would give, through openpyxl's own `is_date_format` and `from_excel`. Those two are
imported only when a date cell is actually met.

Checked against openpyxl on the real workbooks in the measured sample: the
extractor's `Document.text` and `anchors` are identical for every file where both
paths run (`tests/unit/test_fast_xlsx.py` holds the shape). Anything this reader
cannot vouch for raises `XlsxUnreadable`, and the extractor uses openpyxl exactly
as before - so it can only ever be a speed-up.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

__all__ = ["XlsxUnreadable", "SheetData", "read_workbook"]

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MAX_PART = 512 * 1024 * 1024
_COLUMN = re.compile(r"([A-Za-z]+)")
_OPENPYXL_ESCAPE = "x005F_"
#: Column letters -> number, remembered: a sheet repeats the same few dozen.
_COLUMN_CACHE: dict[str, int] = {}


class XlsxUnreadable(Exception):
    """Not a workbook this reader will vouch for. The caller falls back to openpyxl."""


@dataclass
class SheetData:
    """One worksheet as `read_workbook` returns it: name, written rows, and
    whether the row cap cut it short."""

    name: str
    #: `(row number, [(column, value), ...])`, rows in order, values as openpyxl
    #: would return them (never None: an empty cell is simply absent).
    rows: list[tuple[int, list[tuple[int, Any]]]] = field(default_factory=list)
    truncated: bool = False


def _column_number(reference: str) -> int:
    match = _COLUMN.match(reference)
    if not match:
        raise XlsxUnreadable(f"bad cell reference {reference!r}")
    number = 0
    for character in match.group(1).upper():
        number = number * 26 + ord(character) - 64
    return number


def _open(archive: zipfile.ZipFile, name: str):
    info = archive.getinfo(name)
    if info.file_size > _MAX_PART:
        raise XlsxUnreadable(f"{name} is implausibly large")
    return archive.open(info)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    with _open(archive, "xl/sharedStrings.xml") as handle:
        for _event, node in ElementTree.iterparse(handle, events=("end",)):
            if node.tag != _M + "si":
                continue
            # openpyxl's `Text.content`: the plain `t` and every run's `t`, in
            # order, and *not* the phonetic runs (`rPh`), which are a sibling.
            pieces = []
            plain = node.find(_M + "t")
            if plain is not None and plain.text:
                pieces.append(plain.text)
            for run in node.findall(_M + "r"):
                text = run.find(_M + "t")
                if text is not None and text.text:
                    pieces.append(text.text)
            strings.append("".join(pieces).replace(_OPENPYXL_ESCAPE, ""))
            node.clear()
    return strings


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    """Indices of the cell formats (`cellXfs`) whose number format is a date."""
    if "xl/styles.xml" not in archive.namelist():
        return set()
    try:
        from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
    except ImportError as exc:                              # pragma: no cover - dependency of the app
        raise XlsxUnreadable("openpyxl is needed to tell a date format from a number") from exc

    custom: dict[int, str] = {}
    date_ids: set[int] = set()
    with _open(archive, "xl/styles.xml") as handle:
        root = ElementTree.parse(handle).getroot()
    formats = root.find(_M + "numFmts")
    if formats is not None:
        for entry in formats.findall(_M + "numFmt"):
            custom[int(entry.get("numFmtId", "0"))] = entry.get("formatCode", "")
    xfs = root.find(_M + "cellXfs")
    if xfs is None:
        return date_ids
    for index, xf in enumerate(xfs.findall(_M + "xf")):
        number_format = int(xf.get("numFmtId", "0"))
        code = custom.get(number_format)
        if code is None:
            code = BUILTIN_FORMATS.get(number_format)
        if code and is_date_format(code):
            date_ids.add(index)
    return date_ids


def _targets(archive: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    """`rId -> (type suffix, path inside the package)` from the workbook's rels."""
    names = archive.namelist()
    if "xl/_rels/workbook.xml.rels" not in names:
        raise XlsxUnreadable("no workbook relationships")
    with _open(archive, "xl/_rels/workbook.xml.rels") as handle:
        root = ElementTree.parse(handle).getroot()
    found = {}
    for rel in root.findall(_REL + "Relationship"):
        target = rel.get("Target", "")
        path = target[1:] if target.startswith("/") else "xl/" + target
        found[rel.get("Id", "")] = (rel.get("Type", "").rsplit("/", 1)[-1], path)
    return found


def _cell_value(cell: ElementTree.Element, shared: list[str], date_styles: set[int],
                epoch: Any) -> Any:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        inline = cell.find(_M + "is")
        if inline is None:
            return None
        pieces = []
        plain = inline.find(_M + "t")
        if plain is not None and plain.text:
            pieces.append(plain.text)
        for run in inline.findall(_M + "r"):
            text = run.find(_M + "t")
            if text is not None and text.text:
                pieces.append(text.text)
        # No `x005F_` removal here: openpyxl only does that to the shared-string
        # table, not to inline strings.
        return "".join(pieces) or None

    raw = cell.find(_M + "v")
    if raw is None or raw.text is None:
        return None
    text = raw.text
    if kind == "s":
        return shared[int(text)]
    if kind == "b":
        return bool(int(text))
    if kind in ("str", "e"):
        return text
    if kind != "n":
        return text
    number = float(text) if ("." in text or "E" in text or "e" in text) else int(text)
    if date_styles and int(cell.get("s", "0")) in date_styles:
        from openpyxl.utils.datetime import from_excel

        try:
            return from_excel(number, epoch)
        except (ValueError, OverflowError):
            return number
    return number


def read_workbook(path: Path, *, max_rows: int) -> list[SheetData]:
    """Every worksheet, in workbook order, with rows up to `max_rows` (+1 to see the cap)."""
    try:
        return _read(path, max_rows)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, IndexError,
            RuntimeError, EOFError) as exc:
        raise XlsxUnreadable(f"{type(exc).__name__}: {exc}") from exc


def _read(path: Path, max_rows: int) -> list[SheetData]:
    with zipfile.ZipFile(path) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            raise XlsxUnreadable("no xl/workbook.xml")
        with _open(archive, "xl/workbook.xml") as handle:
            workbook = ElementTree.parse(handle).getroot()
        properties = workbook.find(_M + "workbookPr")
        date1904 = properties is not None and properties.get("date1904", "0") in ("1", "true")
        from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900

        epoch = CALENDAR_MAC_1904 if date1904 else CALENDAR_WINDOWS_1900

        targets = _targets(archive)
        shared = _shared_strings(archive)
        date_styles = _date_styles(archive)

        sheets: list[SheetData] = []
        listing = workbook.find(_M + "sheets")
        if listing is None:
            raise XlsxUnreadable("no sheets element")
        for entry in listing.findall(_M + "sheet"):
            kind, part = targets.get(entry.get(_R + "id", ""), ("", ""))
            if kind != "worksheet" or part not in archive.namelist():
                continue                                    # a chartsheet has no cells
            data = SheetData(name=entry.get("name", ""))
            with _open(archive, part) as handle:
                # End events only: a start event per element doubled the work
                # (140,000 events for 25,000 cells) and a row's number is on the
                # row itself, which is complete by the time its end event fires.
                cells: list[tuple[int, Any]] = []
                last_row = 0
                column_of = _COLUMN_CACHE
                for _event, node in ElementTree.iterparse(handle, events=("end",)):
                    tag = node.tag
                    if tag == _M + "c":
                        reference = node.get("r")
                        if reference:
                            key = _COLUMN.match(reference)
                            if key is None:
                                raise XlsxUnreadable(f"bad cell reference {reference!r}")
                            letters = key.group(1)
                            column = column_of.get(letters)
                            if column is None:
                                column = column_of[letters] = _column_number(letters)
                        else:
                            column = cells[-1][0] + 1 if cells else 1
                        value = _cell_value(node, shared, date_styles, epoch)
                        if value is not None:
                            cells.append((column, value))
                        node.clear()
                    elif tag == _M + "row":
                        last_row = int(node.get("r", last_row + 1))
                        if last_row > max_rows:
                            data.truncated = True
                            break
                        data.rows.append((last_row, cells))
                        cells = []
                        node.clear()
                    elif tag == _M + "sheetData":
                        break
            sheets.append(data)
        return sheets
