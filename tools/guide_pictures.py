r"""Retake the user guide's pictures from the real window, and put them in.

2026-10-04. The guide's pictures were first taken on 1 October with a script
that lived in a session's scratch folder and was lost (HANDOFF 2026-10-01:
"regenerating the guides after a UI change means re-grabbing and editing the
HTML by hand"). This is that script, kept.

The recipe, the same as the first time: the real `MainWindow` through the
**Windows** platform (offscreen's font fallback is condensed), 1280x800 on a
screen at 125% so each picture is 1600x1000, against the demonstration store
in `D:\Demo\leasha-guide` (built on `grab_ui`'s seed; 27 files, one offline
drive). The window is laid out as if shown but kept off the screen
(`WA_DontShowOnScreen`), so nothing flashes on the desktop.

    venv\Scripts\python.exe tools\guide_pictures.py --list
    venv\Scripts\python.exe tools\guide_pictures.py files chat
    venv\Scripts\python.exe tools\guide_pictures.py --all --out outputs\guide

Each surface is written to `--out` and, unless `--no-swap`, replaces the
`<img>` in `docs/USER_GUIDE.html` whose `alt` is the surface's caption. Only
the pictures named are touched; the guide's text is not.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import grab_ui  # noqa: E402  (one module object, however this is run)

DEMO = Path(r"D:\Demo\leasha-guide")
GUIDE = ROOT / "docs" / "USER_GUIDE.html"

#: Surface -> the `alt` of its picture in the guide. The surfaces themselves
#: are `grab_ui.SURFACES`, plus the Indexing categories the guide shows.
CAPTIONS: dict[str, str] = {
    "search-home": "The Search page before anything is typed",
    # Not here: "Search results with the preview pane open", its dark twin and
    # "The Life Timeline at June 2015" need a real search over the demo store
    # (`grab_ui._show_results` waits for the full tier) - still by hand.
    "files": "The Files page",
    "mail": "The Mail page",
    "code": "The Code page",
    "chat": "The Chat page",
    "offline-media": "The Offline page",
    "reports": "The Reports page",
    "indexing-status": "Indexing, Status",
    "indexing-what-gets-read": "Indexing, What gets read",
    "indexing-schedule": "Indexing, Schedule",
    "indexing-tuning": "Indexing, Tuning",
    "settings-whats-indexed": "Settings, What's indexed",
    "settings-search": "Settings, Search",
    "settings-models": "Settings, Models and AI",
    "settings-appearance": "Settings, Appearance",
    "settings-storage": "Settings, Storage and maintenance",
}
grab_ui.SURFACES.setdefault("indexing-what-gets-read",
                            {"page": "Indexing", "category": "What gets read"})


def _dress(window, name: str) -> None:
    """What a surface needs beyond reaching it."""
    if name == "settings-whats-indexed":
        # The demo store has no roots. Two folders and a file, set without
        # emitting (nothing is written), so the per-line controls are seen.
        window.settings_view.roots_box.set_roots(
            [str(DEMO / "documents"), str(DEMO / "pictures"),
             str(DEMO / "documents" / "rent-2025.csv")],
            first=[str(DEMO / "documents")])


def take(names: list[str], out: Path) -> list[Path]:
    from PyQt6.QtCore import Qt, QThreadPool
    from PyQt6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    if not (DEMO / ".env").is_file():
        raise SystemExit(f"the demonstration store is not at {DEMO}")
    settings = load_settings(DEMO / ".env")
    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    window = MainWindow(settings, store, vectors, grab_ui._Engine(store), debug=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1280, 800)
    window.show()
    grab_ui._pump(app, 20)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for name in names:
            target = grab_ui._reach(app, window, grab_ui.SURFACES[name])
            _dress(window, name)
            grab_ui._pump(app, 10)
            image = target.grab()
            path = out / f"{name}.png"
            if not image.save(str(path), "PNG"):
                raise RuntimeError(f"could not write {path}")
            written.append(path)
            print(f"{path}  {image.width()}x{image.height()}")
    finally:
        QThreadPool.globalInstance().waitForDone(10_000)
        for _ in range(5):
            app.processEvents()
        window.close()
        store.close()
        vectors.close()
    return written


def swap(pictures: dict[str, Path], guide: Path = GUIDE) -> int:
    """Replace each captioned `<img>`'s data URI. Returns how many were swapped."""
    raw = guide.read_bytes()
    ending = "\r\n" if b"\r\n" in raw else "\n"           # keep the file's own line ends
    html = raw.decode("utf-8").replace("\r\n", "\n")
    swapped = 0
    for name, path in pictures.items():
        caption = CAPTIONS[name]
        pattern = re.compile(r'(<img[^>]*src=")data:image/png;base64,[^"]*("[^>]*alt="'
                             + re.escape(caption) + '")')
        if len(pattern.findall(html)) != 1:
            raise SystemExit(f"{caption!r}: expected exactly one picture in the guide")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        html = pattern.sub(lambda m: f"{m.group(1)}data:image/png;base64,{data}{m.group(2)}", html)
        swapped += 1
    guide.write_bytes(html.replace("\n", ending).encode("utf-8"))
    return swapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("surfaces", nargs="*", help="which pictures; see --list")
    parser.add_argument("--all", action="store_true", help="every picture the guide has")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "outputs" / "guide"))
    parser.add_argument("--no-swap", action="store_true", help="write the PNGs only")
    args = parser.parse_args(argv)
    if args.list:
        for name, caption in CAPTIONS.items():
            print(f"{name:26} {caption}")
        return 0
    names = list(CAPTIONS) if args.all else args.surfaces
    unknown = [n for n in names if n not in CAPTIONS]
    if unknown or not names:
        parser.error(f"unknown or no surface: {', '.join(unknown) or '(none)'}; try --list")
    written = take(names, Path(args.out))
    if not args.no_swap:
        count = swap(dict(zip(names, written)))
        print(f"{count} picture(s) replaced in {GUIDE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
