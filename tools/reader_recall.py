r"""Prove an in-process reader against the converter's own output.

Layer: tooling, not app code.

    venv\Scripts\python.exe tools\reader_recall.py .pub SAMPLES REFERENCE

`SAMPLES` holds the real files, `REFERENCE` holds what the converter wrote for
them (same stem: `x.pub` -> `x.txt`, `x.csv`, `x.pptx`, ...). For each file this
reads it with the registered extractor and prints, against the converter's text:

    recall     share of the converter's distinct words the reader also found
    precision  share of the reader's distinct words the converter also had
    ms         the reader's time, best of three

Non-negotiable 12 says a library reader has to be at least as good as the
converter it replaces, and "at least as good" is a number, so this prints one.
Words are lower-cased runs of letters and digits, two characters or more, because
that is the unit a search matches on. A reader whose recall is poor for a class of
files must not ship for that class - the converter stays the fallback.

The reference text is read with the same extractors the app uses (`.txt` and
`.csv` as plain text, `.pptx`/`.docx`/`.xlsx` through their own readers), so the
comparison is between what would have been indexed, not between raw bytes.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)
_REFERENCE_SUFFIXES = (".pdf", ".txt", ".csv", ".pptx", ".docx", ".xlsx", ".html", ".dxf")


def words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def read_all(path: Path) -> str:
    from app.extract.base import extract

    return "\n".join(document.text for document in extract(path))


def reference_for(sample: Path, reference: Path) -> Path | None:
    for suffix in _REFERENCE_SUFFIXES:
        candidate = reference / (sample.stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def compare(mine: str, theirs: str) -> tuple[float, float, int, int]:
    a, b = words(mine), words(theirs)
    recall = len(a & b) / len(b) if b else 1.0
    precision = len(a & b) / len(a) if a else 1.0
    return recall, precision, len(a), len(b)


def main(argv: list[str]) -> int:
    from loguru import logger

    logger.remove()
    import app.extract  # noqa: F401

    if len(argv) != 4:
        print(__doc__)
        return 2
    extension, samples, reference = argv[1].lower(), Path(argv[2]), Path(argv[3])
    failures = 0
    for sample in sorted(samples.glob(f"*{extension}")):
        ref_path = reference_for(sample, reference)
        if ref_path is None:
            print(f"{sample.name[:50]:50}  no converter output to compare with")
            continue
        try:
            theirs = read_all(ref_path)
        except Exception:                               # noqa: BLE001 - "no text" is an answer
            theirs = ""
        best, mine = None, ""
        try:
            for _ in range(3):
                started = time.perf_counter()
                mine = read_all(sample)
                elapsed = (time.perf_counter() - started) * 1000
                best = elapsed if best is None else min(best, elapsed)
        except Exception as exc:                        # noqa: BLE001 - a raise is a result
            code = getattr(getattr(exc, "error", None), "code", type(exc).__name__)
            print(f"{sample.name[:50]:50}  raised {code} (converter had {len(words(theirs))} words)")
            failures += 1
            continue
        recall, precision, n_mine, n_theirs = compare(mine, theirs)
        print(f"{sample.name[:50]:50}  recall {recall:6.1%}  precision {precision:6.1%}  "
              f"words {n_mine}/{n_theirs}  {best:8.1f} ms")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
