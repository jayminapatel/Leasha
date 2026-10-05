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

#: The platform the pictures are taken through. **Set by `main`, never at
#: import**: the first version set it here, `test_guide_pictures.py` imports
#: this module, and every test run that collected that file then ran its Qt
#: tests on the real Windows platform instead of offscreen - the real
#: clipboard, the real tray, real fonts against the goldens (three "passes
#: alone" failures on 2026-10-04 before the cause was found).
PLATFORM = "windows"

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
    # A real keyword search over the demo store ("boiler quote dave"), first
    # result previewed; the dark twin is the same page with the theme switched
    # for the grab and put back.
    "search-results": "Search results with the preview pane open",
    "search-results-dark": "Search results in the dark theme",
    "timeline": "The Life Timeline at June 2015",
    "files": "The Files page",
    "photos": "The Photos page",                 # 2026-10-05: the Photos tab
    "mail": "The Mail page",
    "code": "The Code page",
    "chat": "The Chat page",
    "offline-media": "The Offline page",
    "reports": "The Reports page",
    "space-report": "The Space Report",
    "indexing-status": "Indexing, Status",
    "indexing-what-gets-read": "Indexing, What gets read",
    "indexing-schedule": "Indexing, Schedule",
    "indexing-tuning": "Indexing, Tuning",
    "settings-whats-indexed": "Settings, What's indexed",
    "settings-search": "Settings, Search",
    "settings-models": "Settings, Models and AI",
    "settings-appearance": "Settings, Appearance",
    "settings-storage": "Settings, Storage and maintenance",
    # 2026-10-04: the menus, each grabbed as the menu itself (`MENUS`).
    "menu-file": "File menu",
    "menu-edit": "Edit menu",
    "menu-view": "View menu",
    "menu-go": "Go menu",
    "menu-help": "Help menu",
    # 2026-10-04 (later): the last three that were by hand (`WINDOWS`).
    "more-menu": "The More menu",
    "mini-search": "The mini search box",
    "photo-tagger": "The Photo Tagger window",
}

#: Pictures that are neither a page nor a bar menu: each has its own taker
#: in `grab_window`, because each is a different kind of thing.
WINDOWS = ("more-menu", "mini-search", "photo-tagger")

#: Picture name -> the menu's title on the bar. A menu is not a page: it is
#: popped up off the screen and grabbed on its own.
MENUS: dict[str, str] = {
    "menu-file": "File", "menu-edit": "Edit", "menu-view": "View",
    "menu-go": "Go", "menu-help": "Help",
}
grab_ui.SURFACES.setdefault("indexing-what-gets-read",
                            {"page": "Indexing", "category": "What gets read"})
grab_ui.SURFACES.setdefault("search-results-dark", {"page": "Search", "state": "results"})
grab_ui.SURFACES.setdefault("space-report", {"page": "Reports"})

#: Which report each Reports picture shows. Chosen every time: the page keeps
#: whichever report was last opened, and the first `--all` run photographed
#: the timeline twice because the timeline picture had been taken before it.
REPORT_SHOWN = {"reports": "inheritance", "space-report": "space"}


def _dress(window, name: str) -> None:
    """What a surface needs beyond reaching it."""
    if name in REPORT_SHOWN:
        from app.ui.widgets.timeline_host import REPORT_KEY

        reports = window.reports_view.list
        for row in range(reports.count()):
            if reports.item(row).data(REPORT_KEY) == REPORT_SHOWN[name]:
                reports.setCurrentRow(row)
    if name == "photos":
        # 2026-10-05: a photo selected, so the info panel shows what it is for.
        browser = window.photos_view.browser
        if browser.model.rowCount():
            browser.select_path(str(browser.model.row_at(0).path))
    if name == "settings-whats-indexed":
        # The demo store has no roots. Two folders and a file, set without
        # emitting (nothing is written), so the per-line controls are seen.
        window.settings_view.roots_box.set_roots(
            [str(DEMO / "documents"), str(DEMO / "pictures"),
             str(DEMO / "documents" / "rent-2025.csv")],
            first=[str(DEMO / "documents")])


def menu_of(window, title: str):
    """The menu on the window's bar with this title (its `&` ignored)."""
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is not None and menu.title().replace("&", "") == title:
            return menu
    raise SystemExit(f"the menu bar has no {title!r} menu")


def grab_menu(app, window, title: str):
    """The menu drawn as it opens, without opening it on the screen."""
    return grab_popup(app, menu_of(window, title))


def grab_popup(app, menu):
    from PyQt6.QtCore import QPoint, Qt

    menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    menu.popup(QPoint(0, 0))
    grab_ui._pump(app, 10)
    try:
        return menu.grab()
    finally:
        menu.hide()
        menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        grab_ui._pump(app, 5)


def grab_window(app, window, store, name: str):
    """The three that are neither a page nor a bar menu."""
    from PyQt6.QtCore import Qt

    if name == "more-menu":
        # The "..." at the right of the Search bar.
        grab_ui._reach(app, window, grab_ui.SURFACES["search-home"])
        return grab_popup(app, window.search_view.more_menu)
    if name == "mini-search":
        # Ctrl+Alt+L's box, with "boiler" searched. Not `summon`: that asks
        # for the keyboard, and a box that then finds it is not the active
        # window dismisses itself.
        from app.ui.widgets.mini_search import MiniSearch

        box = MiniSearch(window._engine)
        box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        box.show()
        box.box.setText("boiler")
        box._search()
        grab_ui._require(app, lambda: box.list.count() > 0, "the mini box's results for 'boiler'")
        grab_ui._settle(app, window, "the mini box")
        try:
            return box.grab()
        finally:
            box.close()
    if name == "photo-tagger":
        # Go > People in photos, as the window builds it.
        from app.ui.widgets.photo_tagger_window import PhotoTaggerWindow

        tagger = PhotoTaggerWindow(store, window)
        tagger.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        tagger.resize(900, 600)
        tagger.show()
        grab_ui._settle(app, window, "the Photo Tagger")
        try:
            return tagger.grab()
        finally:
            tagger.close()
    raise SystemExit(f"no taker for {name!r}")


def _grab_in_dark(app, window, store, name: str):
    """The surface with the dark theme on, and the store's own choice put
    back whatever happens - the demo store is shared by every picture."""
    was = store.get_state("ui:theme", "") or "light"
    try:
        # The window's own route (Settings > Appearance): `_apply_theme` alone
        # reads a preference the window cached when it was built.
        window._theme_changed("dark")
        grab_ui._settle(app, window, "the dark theme")
        # The same words already in the box start no search, and the wait for
        # one then times out (seen on the first run): empty it first.
        window.search_view.input.setText("")
        grab_ui._settle(app, window, "the emptied search box")
        target = grab_ui._reach(app, window, grab_ui.SURFACES[name])
        grab_ui._pump(app, 10)
        return target.grab()
    finally:
        window._theme_changed(was)
        grab_ui._settle(app, window, "the theme put back")


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
    # The real engine, keyword-only (no model is loaded for a picture), with
    # the status line's "35ms" pinned the way the goldens pin it.
    from app.search.engine import SearchEngine

    engine = grab_ui._steady_clock(
        SearchEngine(store, grab_ui._NoVectors(), grab_ui._NoModel(), log_usage=False))
    window = MainWindow(settings, store, vectors, engine, debug=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1280, 800)
    window.show()
    grab_ui._pump(app, 20)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for name in names:
            if name in MENUS:
                image = grab_menu(app, window, MENUS[name])
            elif name in WINDOWS:
                image = grab_window(app, window, store, name)
            elif name.endswith("-dark"):
                image = _grab_in_dark(app, window, store, name)
            else:
                target = grab_ui._reach(app, window, grab_ui.SURFACES[name])
                _dress(window, name)
                grab_ui._pump(app, 10)
                grab_ui._settle(app, window, f"{name}, dressed")
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
    os.environ["QT_QPA_PLATFORM"] = PLATFORM      # before the QApplication exists
    written = take(names, Path(args.out))
    if not args.no_swap:
        count = swap(dict(zip(names, written)))
        print(f"{count} picture(s) replaced in {GUIDE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
