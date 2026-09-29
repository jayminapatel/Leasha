"""Read the owner's own .pst files with the libpff reader, and say how it went.

Order 0z lane C, field test. **Read-only**: nothing is written to the index,
the settings or the archives. Each archive is read in a process of its own,
with pictures held (the text-first pass the pipeline makes), and watched: an
archive whose reader stops moving for `--stall` seconds is ended and reported
with the folder and message it stopped at, and the next archive is read.

Which archives:

* the paths given on the command line (files, or folders searched for .pst);
* otherwise every .pst under Leasha's "Folders to index" (`ui:roots`);
* plus, with `--outlook`, every .pst open in the local Outlook profile.

Usage, from the repository folder with Leasha's venv::

    python scripts/pst_field_test.py                 # your indexed folders
    python scripts/pst_field_test.py D:\\Mail --outlook
    python scripts/pst_field_test.py --stall 120 --out pst-report.txt

Close Outlook first if it has an archive open: Outlook can hold a .pst so that
nothing else may read it, which shows here as "could not open".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

BEAT_S = 2.0


# -- the child: read one archive ----------------------------------------------------

def _read_one(path: Path) -> int:
    """Read `path` with pictures held; print a JSON line every `BEAT_S`, then a
    final one. Runs in its own process, so the parent can end it."""
    from app.extract import progress
    from app.extract.pst_libpff import read_archive
    from app.extract.reading import IMAGES_HOLD, reading

    stack: list = []
    state: dict[str, Any] = {"docs": 0, "chars": 0}
    done = threading.Event()
    started = time.perf_counter()

    def emit(kind: str, **extra: Any) -> None:
        frame = stack[-1].as_dict() if stack else {}
        line = {"kind": kind, "t": round(time.perf_counter() - started, 2),
                "frame": frame, **state, **extra}
        sys.stdout.write(json.dumps(line, default=str) + "\n")
        sys.stdout.flush()

    def beat() -> None:
        while not done.wait(BEAT_S):
            emit("beat")

    progress.attach(stack)
    threading.Thread(target=beat, daemon=True).start()
    error = ""
    try:
        with reading(images=IMAGES_HOLD) as policy:
            for doc in read_archive(path):
                state["docs"] += 1
                state["chars"] += len(getattr(doc, "text", "") or "")
            counts = dict(getattr(policy, "counts", {}) or {})
    except BaseException as exc:                 # noqa: BLE001 - reported, not raised
        counts = {}
        error = f"{type(exc).__name__}: {exc}"
    done.set()
    emit("end", counts=counts, error=error)
    return 0


# -- the parent: find archives, read each, report -----------------------------------

def _saved_roots() -> list[Path]:
    try:
        from app.cli._common import _saved_roots as saved
        from app.core.config import load_settings

        settings = load_settings(create_dirs=False, check_writable=False)
        return [Path(p) for p in saved(settings)]
    except Exception as exc:                     # noqa: BLE001
        print(f"(could not read Leasha's folders to index: {exc})")
        return []


def _outlook_archives() -> list[Path]:
    try:
        import win32com.client                   # type: ignore[import-not-found]

        namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        found = []
        for store in namespace.Stores:
            path = str(getattr(store, "FilePath", "") or "")
            if path.lower().endswith(".pst"):
                found.append(Path(path))
        return found
    except Exception as exc:                     # noqa: BLE001
        print(f"(could not ask Outlook for its archives: {exc})")
        return []


def _archives(places: list[Path]) -> list[Path]:
    found: list[Path] = []
    for place in places:
        if place.is_file() and place.suffix.lower() == ".pst":
            found.append(place)
        elif place.is_dir():
            try:
                found.extend(p for p in place.rglob("*") if p.suffix.lower() == ".pst"
                             and p.is_file())
            except OSError as exc:
                print(f"(could not search {place}: {exc})")
    unique: dict[str, Path] = {}
    for p in found:
        unique.setdefault(os.path.normcase(str(p.resolve())), p)
    return sorted(unique.values(), key=lambda p: p.stat().st_size)


def _read_watched(path: Path, stall_s: float) -> dict[str, Any]:
    """Read one archive in a child process; end it if it stops moving."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--one", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(PROJECT))
    lines: list[dict] = []
    reader_done = threading.Event()

    def pump() -> None:
        for raw in proc.stdout:                  # type: ignore[union-attr]
            try:
                lines.append(json.loads(raw))
            except ValueError:
                pass
        reader_done.set()

    threading.Thread(target=pump, daemon=True).start()
    last_beat, last_moved = -1, time.monotonic()
    started = time.monotonic()
    outcome = "finished"
    while not reader_done.wait(1.0):
        if lines:
            frame = lines[-1].get("frame") or {}
            moved = (frame.get("beat", 0), frame.get("n", 0), lines[-1].get("docs", 0))
            if moved != last_beat:
                last_beat, last_moved = moved, time.monotonic()
            where = frame.get("where", "")
            print(f"\r  {time.monotonic() - started:6.0f}s  {lines[-1].get('docs', 0):>7} items"
                  f"  {where[-60:]:<60}", end="", flush=True)
        if time.monotonic() - last_moved > stall_s:
            outcome = "stalled"
            proc.kill()
            break
    print()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
    last = lines[-1] if lines else {}
    end = next((l for l in reversed(lines) if l.get("kind") == "end"), None)
    if end is None and outcome == "finished":
        outcome = "crashed"
    return {
        "path": str(path),
        "size_mb": round(path.stat().st_size / 1_048_576, 1),
        "outcome": outcome if not (end and end.get("error")) else "error",
        "seconds": round(time.monotonic() - started, 1),
        "items": last.get("docs", 0),
        "counts": (end or {}).get("counts") or (last.get("frame") or {}).get("counts", {}),
        "error": (end or {}).get("error", ""),
        "stopped_at": "" if end else (last.get("frame") or {}).get("where", ""),
        "stopped_at_message": "" if end else (last.get("frame") or {}).get("n", ""),
        "stderr_tail": "\n".join(stderr.strip().splitlines()[-15:]),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--outlook", action="store_true",
                        help="also read every .pst open in the local Outlook profile")
    parser.add_argument("--stall", type=float, default=300.0,
                        help="end an archive whose reader has not moved for this "
                             "many seconds (default 300)")
    parser.add_argument("--out", type=Path, default=Path("pst-field-test.txt"))
    parser.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.one:
        return _read_one(args.one)

    from app.extract import pst_libpff

    if not pst_libpff.available():
        print("libpff (libpff-python) is not installed in this venv; nothing to test.")
        return 2
    places = list(args.paths) or _saved_roots()
    if args.outlook:
        places += _outlook_archives()
    archives = _archives(places)
    if not archives:
        print("No .pst files found. Give a folder or file: "
              "python scripts/pst_field_test.py D:\\Mail")
        return 1
    total_mb = sum(a.stat().st_size for a in archives) / 1_048_576
    print(f"{len(archives)} archive(s), {total_mb:,.0f} MB, smallest first. "
          f"Read-only; pictures held.")

    results = []
    for n, path in enumerate(archives, 1):
        print(f"[{n}/{len(archives)}] {path}")
        results.append(_read_watched(path, args.stall))
        r = results[-1]
        print(f"  {r['outcome']}: {r['items']} items in {r['seconds']}s "
              f"{r['counts'] or ''} {r['error']}")

    lines = [f"Leasha PST field test - {time.strftime('%Y-%m-%d %H:%M')} - "
             f"{len(archives)} archives, {total_mb:,.0f} MB", ""]
    header = f"{'outcome':<9} {'MB':>8} {'items':>8} {'sec':>8} {'items/s':>8}  archive"
    lines += [header, "-" * len(header)]
    for r in results:
        rate = r["items"] / r["seconds"] if r["seconds"] else 0
        lines.append(f"{r['outcome']:<9} {r['size_mb']:>8} {r['items']:>8} "
                     f"{r['seconds']:>8} {rate:>8.1f}  {r['path']}")
    lines.append("")
    for r in results:
        if r["outcome"] != "finished" or r["counts"].get("failed"):
            lines.append(f"== {r['path']}: {r['outcome']}")
            for key in ("counts", "error", "stopped_at", "stopped_at_message", "stderr_tail"):
                if r[key]:
                    lines.append(f"   {key}: {r[key]}")
    lines += ["", "JSON:", json.dumps(results, indent=1, default=str)]
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[: 4 + len(results)]))
    print(f"\nFull report: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
