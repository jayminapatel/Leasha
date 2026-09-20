r"""How long every file type takes to read, measured - order: the speed audit.

Layer: tooling, not app code.

    venv\Scripts\python.exe tools\extract_speed.py --samples C:\scratch\samples
    venv\Scripts\python.exe tools\extract_speed.py --samples DIR --repeat 5 --write-docs
    venv\Scripts\python.exe tools\extract_speed.py --samples DIR --only .pub .wpd
    venv\Scripts\python.exe tools\extract_speed.py --samples DIR --baseline

Regenerates `docs/EXTRACTION_SPEED.md` (`--write-docs`). Non-negotiable 9:
"it feels fast" is not a result, so the table in that document is this script's
output and nothing else.

**Samples.** `DIR/<ext>/` holds real files of that type - copy a few from your own
corpus (never point this at a folder you care about; it only reads, but a
scratch copy costs nothing). Types with no real sample get a small generated one
under `DIR/generated/<ext>/`, so the audit always covers the whole registry. A
generated file is much smaller and simpler than a real one, and the document
says which rows are which.

**Method.** One untimed warm-up read per type (library import cost is real but
is paid once per process, not once per file), then `--repeat` timed reads of each
file; the reported figure is the *minimum* per file, because every noise source
on a busy machine only adds time. A read counts as *spawning a process* when
`subprocess.Popen` was called during it - LibreOffice, dwg2dxf and friends are
found by observation, not by reading the config.

**`--baseline`** forces every type that has both an in-process reader and an
enabled converter through the converter, for the before/after comparison.
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: A read slower than this is not repeated - LibreOffice's start-up alone is
#: seconds, and five of them per file is an audit nobody runs twice.
SLOW_S = 2.0


# ---------------------------------------------------------------------------
# Generated samples, for the types with no real file to hand
# ---------------------------------------------------------------------------

PARAGRAPH = ("The annual inspection of the Leeds site found the licence number 12400 "
             "current, and the boiler certificate expires in March. ")


def _text(paragraphs: int) -> str:
    return "\n\n".join(f"Section {i}. " + PARAGRAPH * 3 for i in range(paragraphs))


def generate(samples: Path) -> None:
    """Small, valid files of every type this can build without a converter."""
    out = samples / "generated"

    def put(ext: str, name: str, data: bytes | str) -> Path:
        folder = out / ext.lstrip(".")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if not target.exists():
            if isinstance(data, str):
                target.write_text(data, encoding="utf-8")
            else:
                target.write_bytes(data)
        return target

    body = _text(60)
    put(".txt", "notes.txt", body)
    put(".md", "readme.md", "# Notes\n\n" + body)
    put(".log", "app.log", "\n".join(f"2026-09-20 12:00:{i % 60:02d} INFO request {i} ok" for i in range(5000)))
    put(".csv", "table.csv", "id,name,city,amount\n" + "\n".join(f"{i},Name {i},Leeds,{i * 3}" for i in range(4000)))
    put(".json", "data.json", json.dumps([{"id": i, "note": PARAGRAPH} for i in range(300)]))
    put(".py", "module.py", "\n".join(f"def function_{i}(x):\n    return x + {i}\n" for i in range(400)))
    put(".html", "page.html", "<html><body>" + "".join(f"<p>{PARAGRAPH}</p>" for i in range(80)) + "</body></html>")
    put(".xml", "data.xml", "<r>" + "".join(f"<item id='{i}'>{PARAGRAPH}</item>" for i in range(80)) + "</r>")
    put(".rtf", "letter.rtf", "{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}"
        + "".join(f"\\pard {PARAGRAPH}\\par " for _ in range(60)) + "}")

    try:
        import docx
        target = out / "docx" / "report.docx"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            document = docx.Document()
            for i in range(60):
                document.add_paragraph(f"Section {i}. " + PARAGRAPH * 3)
            table = document.add_table(rows=20, cols=4)
            for r in range(20):
                for c in range(4):
                    table.cell(r, c).text = f"cell {r}-{c}"
            document.save(str(target))
    except ImportError:
        pass

    try:
        import openpyxl
        target = out / "xlsx" / "sheet.xlsx"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.title = "Costs"
            for r in range(1, 5001):
                sheet.append([f"Item {r}", "Leeds", r * 3, r / 7, "2026-09-20"])
            book.save(str(target))
    except ImportError:
        pass

    try:
        import pptx
        target = out / "pptx" / "deck.pptx"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            deck = pptx.Presentation()
            for i in range(25):
                slide = deck.slides.add_slide(deck.slide_layouts[1])
                slide.shapes.title.text = f"Slide {i}"
                slide.placeholders[1].text = PARAGRAPH
            deck.save(str(target))
    except ImportError:
        pass

    try:
        import fitz
        target = out / "pdf" / "report.pdf"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            pdf = fitz.open()
            for i in range(30):
                page = pdf.new_page()
                page.insert_textbox(fitz.Rect(50, 50, 550, 780), f"Page {i}. " + PARAGRAPH * 8, fontsize=11)
            pdf.save(str(target))
            pdf.close()
    except ImportError:
        pass

    # ODF: a zip holding content.xml.
    def odf(ext: str, mimetype: str, inner: str) -> None:
        target = out / ext.lstrip(".") / f"sample{ext}"
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
              'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
              'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"')
        with zipfile.ZipFile(target, "w") as z:
            z.writestr("mimetype", mimetype)
            z.writestr("content.xml", f'<?xml version="1.0"?><office:document-content {ns}>'
                                      f'<office:body>{inner}</office:body></office:document-content>')

    paragraphs = "".join(f"<text:p>{PARAGRAPH}</text:p>" for _ in range(60))
    odf(".odt", "application/vnd.oasis.opendocument.text", f"<office:text>{paragraphs}</office:text>")
    rows = "".join(f"<table:table-row><table:table-cell><text:p>Item {i}</text:p></table:table-cell>"
                   f"<table:table-cell><text:p>{i * 3}</text:p></table:table-cell></table:table-row>"
                   for i in range(500))
    odf(".ods", "application/vnd.oasis.opendocument.spreadsheet",
        f"<office:spreadsheet><table:table table:name='S'>{rows}</table:table></office:spreadsheet>")
    odf(".odp", "application/vnd.oasis.opendocument.presentation",
        f"<office:presentation>{paragraphs}</office:presentation>")

    # EPUB and FB2: the same two shapes the tests use.
    target = out / "epub" / "book.epub"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        chapters = {f"c{i}.xhtml": f"<html><body><h1>Chapter {i}</h1><p>{PARAGRAPH * 5}</p></body></html>"
                    for i in range(20)}
        opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Sample</dc:title></metadata><manifest>'
               + "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(20))
               + '</manifest><spine>' + "".join(f'<itemref idref="c{i}"/>' for i in range(20)) + '</spine></package>')
        with zipfile.ZipFile(target, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                       'version="1.0"><rootfiles><rootfile full-path="OEBPS/book.opf" '
                       'media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("OEBPS/book.opf", opf)
            for name, content in chapters.items():
                z.writestr("OEBPS/" + name, content)
    put(".fb2", "book.fb2", '<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/'
        'fictionbook/2.0"><description><title-info><book-title>Sample</book-title></title-info></description><body>'
        + "".join(f"<section><p>{PARAGRAPH}</p></section>" for _ in range(60)) + "</body></FictionBook>")

    try:
        import ezdxf
        target = out / "dxf" / "drawing.dxf"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            drawing = ezdxf.new()
            space = drawing.modelspace()
            for i in range(200):
                space.add_text(f"Note {i}: boiler room, licence 12400").set_placement((0, i))
            drawing.saveas(str(target))
    except ImportError:
        pass

    put(".eml", "mail.eml", "From: ada@example.com\nTo: bob@example.com\nSubject: Leeds inspection\n"
        "Date: Sat, 20 Sep 2026 10:00:00 +0000\nContent-Type: text/plain\n\n" + _text(20))
    put(".mbox", "box.mbox", "".join(
        f"From ada@example.com Sat Sep 20 10:00:00 2026\nFrom: ada@example.com\nSubject: Message {i}\n\n{PARAGRAPH * 4}\n"
        for i in range(200)))
    put(".gdoc", "doc.gdoc", json.dumps({"url": "https://docs.google.com/document/d/abc123", "doc_id": "abc123"}))

    target = out / "zip" / "bundle.zip"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for i in range(40):
                z.writestr(f"folder/note{i}.txt", PARAGRAPH * 20)

    try:
        from PIL import Image, ImageDraw
        target = out / "png" / "text.png"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGB", (900, 300), "white")
            draw = ImageDraw.Draw(image)
            for i in range(6):
                draw.text((20, 20 + i * 40), "Invoice 12400 Leeds boiler certificate", fill="black")
            image.save(str(target))
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    path: Path
    ms: float = 0.0
    #: CPU time of this process for the same read. **On a machine that is doing
    #: other work (every developer machine) wall time is contention plus the
    #: read; CPU time is the read.** Both are reported.
    cpu_ms: float = 0.0
    chars: int = 0
    spawned: int = 0
    route: str = ""
    error: str = ""


@dataclass
class Row:
    extension: str
    extractor: str
    readings: list[Reading] = field(default_factory=list)
    generated: bool = False

    @property
    def ok(self) -> list[Reading]:
        return [r for r in self.readings if not r.error]

    def median_ms(self) -> Optional[float]:
        values = [r.ms for r in self.ok]
        return statistics.median(values) if values else None

    def median_cpu_ms(self) -> Optional[float]:
        values = [r.cpu_ms for r in self.ok]
        return statistics.median(values) if values else None

    def spawns(self) -> bool:
        return any(r.spawned for r in self.readings)


class _PopenCounter:
    """Counts `subprocess.Popen` constructions, restoring the original after."""

    def __init__(self) -> None:
        self.count = 0
        self._original = subprocess.Popen

    def __enter__(self) -> "_PopenCounter":
        counter = self
        original = self._original

        class Counting(original):                      # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                counter.count += 1
                super().__init__(*args, **kwargs)

        subprocess.Popen = Counting                    # type: ignore[misc, assignment]
        return self

    def __exit__(self, *_exc: Any) -> None:
        subprocess.Popen = self._original              # type: ignore[misc]


def _read_once(path: Path, *, baseline: bool) -> tuple[int, str]:
    """Extract one file completely. Returns `(characters, route)`."""
    from app.extract.base import extract, extractor_for

    if baseline:
        forced = _converter_read(path)
        if forced is not None:
            return forced

    chars = 0
    route = "in-process"
    for document in extract(path):
        chars += len(document.text)
        made_by = document.meta.get("converted_by")
        if made_by:
            route = f"converter ({made_by})"
    extractor = extractor_for(path)
    if extractor is None and route == "in-process":
        route = "converter"
    return chars, route


def _converter_read(path: Path) -> Optional[tuple[int, str]]:
    from app.core.formats import load_rules
    from app.extract.converter import extract_via_converter

    rule = load_rules().converter_for(path.suffix.lower())
    if rule is None or not rule.enabled:
        return None
    chars = 0
    for document in extract_via_converter(path, rule):
        chars += len(document.text)
    return chars, f"converter ({Path(rule.command[0]).name})"


def measure(path: Path, repeat: int, *, baseline: bool) -> Reading:
    reading = Reading(path=path)
    best: Optional[float] = None
    best_cpu: Optional[float] = None
    for attempt in range(max(1, repeat)):
        with _PopenCounter() as counter:
            started = time.perf_counter()
            cpu_started = time.process_time()
            try:
                chars, route = _read_once(path, baseline=baseline)
            except Exception as exc:                    # noqa: BLE001 - an error is a result
                code = getattr(getattr(exc, "error", None), "code", type(exc).__name__)
                reading.error = str(code)
                reading.spawned = max(reading.spawned, counter.count)
                return reading
            elapsed = time.perf_counter() - started
            cpu = time.process_time() - cpu_started
        reading.spawned = max(reading.spawned, counter.count)
        reading.chars, reading.route = chars, route
        best = elapsed if best is None else min(best, elapsed)
        best_cpu = cpu if best_cpu is None else min(best_cpu, cpu)
        if elapsed > SLOW_S:
            break
    reading.ms = (best or 0.0) * 1000.0
    reading.cpu_ms = (best_cpu or 0.0) * 1000.0
    return reading


def collect(samples: Path) -> dict[str, tuple[list[Path], bool]]:
    """`extension -> (files, generated?)`. Real samples win over generated ones."""
    found: dict[str, tuple[list[Path], bool]] = {}
    if not samples.is_dir():
        return found
    for folder in sorted(p for p in samples.iterdir() if p.is_dir() and p.name != "generated"):
        files = sorted(p for p in folder.iterdir() if p.is_file())
        if files:
            found["." + folder.name] = (files, False)
    generated = samples / "generated"
    if generated.is_dir():
        for folder in sorted(p for p in generated.iterdir() if p.is_dir()):
            ext = "." + folder.name
            if ext not in found:
                files = sorted(p for p in folder.iterdir() if p.is_file())
                if files:
                    found[ext] = (files, True)
    return found


def _forbid_libreoffice() -> None:
    """Make LibreOffice look uninstalled for the whole audit.

    **Deliberate, and the reason is the owner's machine.** LibreOffice run in a
    loop by an audit - one cold start per sample file - crash-looped `soffice.bin`
    on 2026-09-20 and the owner saw a stream of error dialogs. A reader that
    declines a file then falls back to a converter that is "missing", the fallback
    raises quickly, and the row is reported as a failure with its code: what the
    audit measures is the in-process route, which is what it is for. Cold
    LibreOffice cost is measured by hand, once per type, and quoted in the doc.
    """
    import app.extract.converter as converter

    original = converter.resolve_binary

    def resolve(name: str):
        if name in ("soffice", "libreoffice"):
            return None
        return original(name)

    converter.resolve_binary = resolve            # type: ignore[assignment]


def run(samples: Path, *, repeat: int, only: Optional[set[str]], baseline: bool,
        allow_soffice: bool = False) -> list[Row]:
    import app.extract  # noqa: F401 - registers every extractor
    from app.extract.base import extractor_for

    if not allow_soffice:
        _forbid_libreoffice()

    rows: list[Row] = []
    for extension, (files, generated) in collect(samples).items():
        if only and extension not in only:
            continue
        extractor = extractor_for(files[0])
        row = Row(extension=extension, extractor=getattr(extractor, "name", "converter"), generated=generated)
        try:
            _read_once(files[0], baseline=baseline)         # the untimed warm-up
        except Exception:                                   # noqa: BLE001 - measured below
            pass
        for path in files:
            row.readings.append(measure(path, repeat, baseline=baseline))
        rows.append(row)
        median = row.median_ms()
        print(f"{extension:9} {row.extractor:10} n={len(files):<2} "
              f"{'-' if median is None else f'{median:9.1f} ms wall'} "
              f"{'-' if row.median_cpu_ms() is None else f'{row.median_cpu_ms():8.1f} ms cpu'}  "
              f"{'SPAWNS' if row.spawns() else ''}", flush=True)
    return rows


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def _route_of(row: Row) -> str:
    routes = sorted({r.route for r in row.ok if r.route})
    if not routes:
        return "(failed)"
    return " / ".join(routes)


def render(rows: list[Row], *, baseline: bool, machine: str) -> str:
    stamp = time.strftime("%Y-%m-%d")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
    ordered = sorted(rows, key=lambda r: -(r.median_ms() or -1))
    lines = [
        "# Extraction speed",
        "",
        f"**Doc version:** 1.0 \u00b7 **Updated:** {stamp} \u00b7 **Applies to:** app v{version}",
        "",
        "Generated by `tools/extract_speed.py` - **do not edit by hand**; re-run it. "
        "Every figure is the minimum of the repeats for that file (noise only ever "
        "adds time), and the row shows the median across its files. "
        "`Spawns` means `subprocess.Popen` was called while reading, observed and not read from config. "
        f"Measured on: {machine}. Rows marked `gen` used a small generated sample "
        "(no real file of that type to hand), so their milliseconds are a floor, not a corpus figure.",
        "",
        ("**This table is a `--baseline` run: every type that has an enabled converter was forced "
         "through it, to show what it cost before an in-process reader existed.**"
         if baseline else "**Slowest first.**"),
        "",
        "| Type | Reader | Route | Files | Sample KB (median) | wall ms / file (median) | CPU ms / file (median) | wall ms (max) | Spawns | Sample |",
        "|---|---|---|---:|---:|---:|---:|---:|:-:|:-:|",
    ]
    for row in ordered:
        ok = row.ok
        sizes = [r.path.stat().st_size / 1024 for r in row.readings if r.path.exists()]
        median = row.median_ms()
        lines.append(
            f"| `{row.extension}` | {row.extractor} | {_route_of(row)} | {len(row.readings)} "
            f"| {statistics.median(sizes):,.0f} | "
            f"{'-' if median is None else f'{median:,.1f}'} | "
            f"{'-' if row.median_cpu_ms() is None else f'{row.median_cpu_ms():,.1f}'} | "
            f"{max((r.ms for r in ok), default=0):,.1f} | "
            f"{'yes' if row.spawns() else 'no'} | {'gen' if row.generated else 'real'} |"
        )
    failures = [(row, r) for row in rows for r in row.readings if r.error]
    if failures:
        lines += ["", "## Files that raised", "",
                  "Expected for some: a raise is the correct answer for a file with no text or a damaged one.", "",
                  "| Type | File | Code |", "|---|---|---|"]
        for row, r in failures:
            lines.append(f"| `{row.extension}` | {r.path.name} | {r.error} |")
    lines.append("")
    return "\n".join(lines)


BEGIN, END = "<!-- BEGIN MEASURED -->", "<!-- END MEASURED -->"


def _merge(target: Path, generated: str) -> str:
    """Put the measured table between the markers of the hand-written document.

    The prose around the table (what changed, the recall figures, what could not
    be done) is written by a person and must survive a re-run; only the table is
    the script's. With no markers in the file, the whole generated document is
    written, header included.
    """
    if not target.exists():
        return generated
    existing = target.read_text(encoding="utf-8")
    if BEGIN not in existing or END not in existing:
        return generated
    head, _, rest = existing.partition(BEGIN)
    _, _, tail = rest.partition(END)
    # Drop the H1 and the header line the generator writes; the document has its own.
    body = generated.partition("\n\n")[2].partition("\n\n")[2]
    return head + BEGIN + "\n" + body.strip() + "\n" + END + tail


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=Path, required=True, help="folder with <ext>/ subfolders of sample files")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--only", nargs="*", default=None, help="extensions, e.g. .pub .wpd")
    parser.add_argument("--baseline", action="store_true", help="force converter-routed types through the converter")
    parser.add_argument("--allow-soffice", action="store_true",
                        help="let a declined file reach LibreOffice (default: never; see _forbid_libreoffice)")
    parser.add_argument("--no-generate", action="store_true", help="do not build generated samples")
    parser.add_argument("--write-docs", action="store_true", help="write docs/EXTRACTION_SPEED.md")
    parser.add_argument("--json", type=Path, default=None, help="also dump raw readings here")
    args = parser.parse_args(argv)
    if args.baseline and not args.allow_soffice:
        parser.error("--baseline runs LibreOffice once per file; add --allow-soffice to say you mean it "
                     "(and do not run it in a loop - see _forbid_libreoffice)")
    from loguru import logger
    logger.remove()

    args.samples.mkdir(parents=True, exist_ok=True)
    if not args.no_generate:
        generate(args.samples)
    only = {e if e.startswith(".") else "." + e for e in args.only} if args.only else None
    rows = run(args.samples, repeat=args.repeat, only=only, baseline=args.baseline,
               allow_soffice=args.allow_soffice)

    import platform
    machine = f"{platform.platform()}, Python {platform.python_version()}, {__import__('os').cpu_count()} logical CPUs"
    document = render(rows, baseline=args.baseline, machine=machine)
    if args.write_docs:
        target = ROOT / "docs" / "EXTRACTION_SPEED.md"
        target.write_text(_merge(target, document), encoding="utf-8")
        print(f"wrote {target}")
    else:
        print()
        print(document)
    if args.json:
        args.json.write_text(json.dumps([
            {"extension": row.extension, "extractor": row.extractor, "generated": row.generated,
             "readings": [{"file": r.path.name, "ms": round(r.ms, 2), "cpu_ms": round(r.cpu_ms, 2), "chars": r.chars,
                           "spawned": r.spawned, "route": r.route, "error": r.error}
                          for r in row.readings]}
            for row in rows], indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
