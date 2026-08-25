r"""Legacy Excel: .xls, via `xlrd`. No external converter.

Layer: L2

**`xlrd` 2.0 reads `.xls` only.** It dropped `.xlsx` deliberately, in 1.2 -> 2.0,
after a security advisory about its XML path, and that is what makes it the
right dependency here rather than a competitor to `openpyxl`. The split is
clean: `openpyxl` owns OOXML, `xlrd` owns the OLE2 binary, and neither can be
handed the other's format by accident.

The consequence is a failure mode worth naming, because it is common. A file
saved by Excel as `.xlsx` and then *renamed* to `.xls` - which happens whenever
somebody tries to make a modern file open in an old system - reaches this
extractor and `xlrd` refuses it. That refusal must arrive as a sentence saying
what to do, not as an `XLRDError` traceback. See `_open`.

The row and column caps are imported from `office.py` rather than restated, so
a 1998 spreadsheet and a 2024 one truncate at the same place. Two constants
meaning "how much of a sheet to index" is how they end up different.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.errors import make_error, raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, SourceKind, register
from app.extract.office import MAX_SHEET_COLUMNS, MAX_SHEET_ROWS

__all__ = ["XlsExtractor"]

log = logger.bind(component="extract.xls")


def _cell_text(book: Any, cell: Any) -> str:
    """One cell as the string a person would search for.

    Two conversions earn their place:

    **Dates.** Excel stores 2019-04-01 as the float 43556.0. Indexed raw, no
    search for a date ever matches - and dates are most of what anybody looks
    for in an old spreadsheet.

    **Whole numbers.** Excel stores every number as a float, so an invoice
    number renders as `12400.0` and a search for `12400` misses it.
    """
    import xlrd

    kind = cell.ctype
    value = cell.value

    if kind == xlrd.XL_CELL_EMPTY or kind == xlrd.XL_CELL_BLANK:
        return ""
    if kind == xlrd.XL_CELL_TEXT:
        return str(value).strip()
    if kind == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if value else "FALSE"
    if kind == xlrd.XL_CELL_ERROR:
        return ""
    if kind == xlrd.XL_CELL_DATE:
        try:
            stamp = xlrd.xldate.xldate_as_datetime(value, book.datemode)
        except Exception:                                    # noqa: BLE001
            return str(value)
        # A date-only cell has a zero time. Printing 00:00:00 on every one of
        # them is noise in every chunk of the sheet.
        if (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0):
            return stamp.strftime("%Y-%m-%d")
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    if kind == xlrd.XL_CELL_NUMBER:
        if float(value).is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


class XlsExtractor:
    """Excel 97-2003 workbooks. `.xlsx` belongs to `XlsxExtractor`."""

    name = "xls"
    extensions = frozenset({".xls", ".xlt"})
    reads_externally = False
    requires = (Requirement("xlrd", "xlrd",
                            provides="legacy Excel cell text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        book = self._open(path)
        if book is None:
            return

        try:
            builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
            names = list(book.sheet_names())
            builder.meta["sheets"] = names
            builder.meta["format"] = "excel-97"

            for index, name in enumerate(names, start=1):
                sheet = book.sheet_by_index(index - 1)
                lines: list[str] = []
                rows = min(sheet.nrows, MAX_SHEET_ROWS)
                columns = min(sheet.ncols, MAX_SHEET_COLUMNS)

                for row in range(rows):
                    values = [
                        text for text in (
                            _cell_text(book, sheet.cell(row, column))
                            for column in range(columns)
                        ) if text
                    ]
                    if values:
                        # Tabs between cells: "Licence" and "12400" must stay
                        # adjacent, exactly as odf.py argues.
                        lines.append("\t".join(values))

                if sheet.nrows > MAX_SHEET_ROWS:
                    builder.warn(make_error(
                        "ERR_FILE_CORRUPT", "extract.xls", path=str(path),
                        suggestion=(
                            f"Sheet '{name}' has more than {MAX_SHEET_ROWS:,} rows. The first "
                            f"{MAX_SHEET_ROWS:,} were indexed; the rest were not, to stop one "
                            "spreadsheet from dominating the index. Split it, or export the "
                            "table to CSV, if the later rows matter."
                        ),
                        details=f"Sheet '{name}' truncated at {MAX_SHEET_ROWS} rows.",
                    ))

                builder.add(
                    "\n".join(lines),
                    page=index,
                    label=f"Sheet: {name}",
                    prefix_label=True,
                )

            yield builder.build()
        finally:
            # `on_demand` keeps the file mapped until this is called. Without it
            # an index run holds every workbook it has read.
            try:
                book.release_resources()
            except Exception:                                # noqa: BLE001
                pass

    def _open(self, path: Path) -> Optional[Any]:
        """The workbook, or None with a precise error already raised."""
        import io

        import xlrd

        # **xlrd writes its warnings to stdout unless given a file.** "No
        # CODEPAGE record" and friends are normal for old workbooks, and without
        # this they are printed straight through a CLI index run - thousands of
        # lines of someone else's diagnostics interleaved with the progress
        # output. Captured and logged at debug, where they belong.
        chatter = io.StringIO()
        try:
            book = xlrd.open_workbook(
                str(path), on_demand=True, formatting_info=False, logfile=chatter
            )
            noise = chatter.getvalue().strip()
            if noise:
                log.debug("xlrd notes", path=str(path), notes=noise)
            return book
        except xlrd.XLRDError as exc:
            message = str(exc)
            if "xlsx" in message.lower() or "zip" in message.lower():
                # The renamed-file case from the module docstring. The fix is a
                # rename, so the message says so rather than reporting corruption.
                raise_error(
                    "ERR_FILE_CORRUPT", "extract.xls", path=str(path),
                    suggestion="This file is a modern Excel workbook with an .xls "
                               "name. Rename it to .xlsx and it will be indexed.",
                    details=message,
                )
            else:
                raise_error("ERR_FILE_CORRUPT", "extract.xls", path=str(path),
                            details=message)
            return None
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.xls", path=str(path), details=str(exc))
            return None
        except Exception as exc:                             # noqa: BLE001
            raise_error("ERR_FILE_CORRUPT", "extract.xls", path=str(path),
                        details=f"{type(exc).__name__}: {exc}")
            return None


register(XlsExtractor())
