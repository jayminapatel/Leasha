r"""Eyes for the coding agent: every surface of the real window, to PNG.

Work order 0m §0 (`202626270547`), folded into the UI Redesign
(`202626160950` §9k) by the owner on 2026-09-16.

Builds the real `MainWindow` offscreen (`QT_QPA_PLATFORM=offscreen`) against
a temporary, empty store; walks every rail page and, on Settings and
Indexing, every category; grabs each with `widget.grab()` and writes
`outputs/screenshots/<surface>.png`. No new dependencies: `grab()` renders
offscreen with what is already installed.

    venv\Scripts\python.exe tools\grab_ui.py
    venv\Scripts\python.exe tools\grab_ui.py --surface search-home --theme dark
    venv\Scripts\python.exe tools\grab_ui.py --size 1024x600 --out tests/golden/ui-redesign

The workflow it unlocks: "the Files tab looks wrong" -> run this -> read the
PNG -> hypothesis -> pytest-qt regression -> fix -> re-grab -> confirm.
`SURFACES` is the list a smoke test checks for drift against the rail: a new
page without a grab entry fails the test.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform plugin does not know where to look for fonts on
# Windows (Qt stopped bundling any - "QFontDatabase: Cannot find font
# directory"), so every grab came back with every glyph as a tofu box until
# this pointed it at the real system font directory. Nothing to do on a
# platform without one.
if sys.platform == "win32":
    _windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if _windows_fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_windows_fonts))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Surface name -> how to reach it. `page` is a rail title; `category` a
#: CategoryNav entry on that page; `state` a Search-page state.
SURFACES: dict[str, dict[str, str]] = {
    "search-home": {"page": "Search", "state": "home"},
    "search-results": {"page": "Search", "state": "results"},
    "files": {"page": "Files"},
    "mail": {"page": "Mail"},
    "code": {"page": "Code"},
    "chat": {"page": "Chat"},
    "offline-media": {"page": "Offline"},
    "reports": {"page": "Reports"},
    # Order 0n section 4: the Life Timeline, opened at June 2015 over a small
    # seeded index (real thumbnails, a drive in a drawer, a burst).
    "timeline": {"page": "Reports", "state": "timeline"},
    "indexing-status": {"page": "Indexing", "category": "Status"},
    "indexing-schedule": {"page": "Indexing", "category": "Schedule"},
    "indexing-tuning": {"page": "Indexing", "category": "Tuning"},
    "settings-whats-indexed": {"page": "Settings", "category": "What's indexed"},
    "settings-search": {"page": "Settings", "category": "Search"},
    "settings-models": {"page": "Settings", "category": "Models & AI"},
    "settings-appearance": {"page": "Settings", "category": "Appearance"},
    "settings-storage": {"page": "Settings", "category": "Storage & maintenance"},
}

ENV = """\
DATA_PATH={d}
VECTOR_PATH={d}/vectors
FTS_DB={d}/fts/knowledge.db
CACHE_PATH={d}/cache
MODEL_CACHE={d}/models
STATE_PATH={d}/state
PROJECT_PATH={d}
LOG_PATH={d}/logs
EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=false
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral
MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""


class _Engine:
    """Enough of a `SearchEngine` to build the window; searches answer empty.

    `_reach` types into the search box to capture the "results" surface,
    which runs the real interim-search seam (`app.ui.workers.SearchWorker`,
    tier "interim") against whatever this hands it - so this needs the two
    methods that seam actually calls, not just enough to construct a window.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def warm_up(self) -> None:
        pass

    def close(self) -> None:
        pass

    def interim(self, raw: str, *, limit: int = 0, scope: str = "all") -> Any:
        from app.search.engine import SearchResponse
        from app.search.query import parse_query
        return SearchResponse(parsed=parse_query(raw).scoped(scope), interim=True)

    def search(self, raw: str, **options: Any) -> Any:
        from app.search.engine import SearchResponse
        from app.search.query import parse_query
        return SearchResponse(parsed=parse_query(raw).scoped(options.get("scope", "all")))


class _NoVectors:
    def search(self, *_a: Any, **_k: Any) -> list:
        return []


class _NoModel:
    """Keyword-only: `app/search/vector.py` degrades to no vectors on this."""

    def embed(self, _t: Any) -> Any:
        raise RuntimeError("no model - grab_ui stays keyword-only")

    def embed_all(self, _t: Any) -> Any:
        raise RuntimeError("no model - grab_ui stays keyword-only")

    def warm_up(self) -> None:
        pass


#: What the "results" surface finds: (file name, kind, text). One document, one
#: message and one piece of code, so the three badge colours all show, and every
#: file is really written to disk so the inspector has something to preview.
_SEED = (
    ("boiler-quote-dave.txt", "txt",
     "Quote for the new boiler from Dave, sent last winter. Includes the boiler, the flue kit "
     "and fitting, and a second boiler visit for the annual service. Valid for thirty days."),
    ("boiler-service-notes.md", "md",
     "Notes from the boiler service: pressure was low, topped up. The quote Dave sent covers "
     "a replacement boiler if it fails again."),
    ("boiler_calc.py", "py",
     "# boiler quote calculator: adds the flue kit, fitting and Dave's margin to the boiler price\n"
     "def quote(boiler, flue, fitting, margin=0.1):\n    return (boiler + flue + fitting) * (1 + margin)\n"),
)
_SEED_MTIME = 1_741_780_800          # 12 March 2025, so dates do not drift with the day it is run


def seed(store: Any, root: Path) -> None:
    """Real files and their index rows, so the search page has rows to show."""
    import json

    folder = root / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    for name, ext, text in _SEED:
        path = folder / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (_SEED_MTIME, _SEED_MTIME))
        file_id = store.upsert_file(
            path.as_posix(), parent_dir=folder.as_posix(), ext=ext, size_bytes=len(text),
            mtime_ns=_SEED_MTIME * 1_000_000_000, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    message = store.upsert_file(
        "pst://msg/Boiler quote", parent_dir="pst://msg", size_bytes=1,
        mtime_ns=_SEED_MTIME * 1_000_000_000, status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message, "Boiler quote", "dave@example.com", json.dumps(["me@example.com"]),
             _SEED_MTIME, 0))
    store.replace_chunks(message, [{"ordinal": 0, "text":
        "Hi, here is the boiler quote I promised. Dave. The flue kit and fitting are included."}])


def seed_timeline(store: Any, root: Path) -> None:
    """June 2015 for the timeline surface: real pictures on disk (so thumbnails
    are drawn), a burst, a letter, a video, a message, and a photograph on a
    drive that is in a drawer."""
    import json
    from datetime import datetime as _dt

    from PIL import Image, ImageDraw

    def ns(day: int, hour: int = 12, minute: int = 0) -> int:
        return int(_dt(2015, 6, day, hour, minute).timestamp()) * 1_000_000_000

    folder = root / "pictures"
    folder.mkdir(parents=True, exist_ok=True)
    colours = [(196, 92, 60), (60, 140, 196), (90, 170, 100), (200, 170, 60), (140, 90, 190), (60, 60, 70)]
    base = 0x0F0F0F0F0F0F0F0F
    for number in range(9):
        path = folder / f"lake-{number + 1:02d}.jpg"
        picture = Image.new("RGB", (480, 320 if number % 3 else 640), colours[number % len(colours)])
        ImageDraw.Draw(picture).ellipse((60, 60, 260, 260), fill=(255, 255, 255))
        picture.save(path)
        day = 10 if number < 6 else 14
        file_id = store.upsert_file(
            path.as_posix(), parent_dir=folder.as_posix(), ext="jpg", size_bytes=path.stat().st_size,
            mtime_ns=ns(1) + 4 * 365 * 86400 * 1_000_000_000, status="INDEXED", source_kind="file",
            taken_at_ns=ns(day, 10 + number // 2, (number % 2) * 20))
        with store.write() as conn:
            conn.execute("UPDATE files SET phash = ? WHERE id = ?",
                         (f"{base ^ (1 << (number % 3)):016x}" if number < 3 else f"{number * 0x1111111111111111 & (2**64 - 1):016x}",
                          file_id))
    store.upsert_file(
        (root / "documents" / "to-the-bank.docx").as_posix(), parent_dir=(root / "documents").as_posix(),
        ext="docx", size_bytes=48_213, mtime_ns=ns(20, 9, 30), status="INDEXED", source_kind="file")
    store.upsert_file(
        (root / "pictures" / "wedding.mp4").as_posix(), parent_dir=(root / "pictures").as_posix(),
        ext="mp4", size_bytes=88_400_000, mtime_ns=ns(20, 18), status="INDEXED", source_kind="file",
        taken_at_ns=ns(14, 16, 5))
    volume = store.upsert_volume("grab-old-wd", kind="drive", name="Old WD", status="OFFLINE")
    store.upsert_file(
        f"leasha-volume://{volume}/Holiday/beach.jpg", parent_dir="Holiday", ext="jpg", size_bytes=3_100_000,
        mtime_ns=ns(1) + 5 * 365 * 86400 * 1_000_000_000, status="INDEXED", source_kind="file",
        taken_at_ns=ns(14, 11), volume_id=volume, relative_path="Holiday/beach.jpg")
    message = store.upsert_file(
        "pst://msg/Wedding plans", parent_dir="pst://msg", size_bytes=1, mtime_ns=ns(1) + 6 * 365 * 86400 * 10**9,
        status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message, "Wedding plans", "meera@example.com", json.dumps(["me@example.com"]),
             ns(25, 8, 15) // 1_000_000_000, 0))
        conn.execute("UPDATE files SET indexed_at = 1700000000 + id WHERE indexed_at IS NULL")


def _show_timeline(app: Any, window: Any) -> None:
    """Open the timeline at June 2015 and wait until its list and its pictures are there."""
    from datetime import datetime as _dt

    from app.ui.widgets.timeline_host import show_timeline

    view = window.reports_view.timeline
    show_timeline(window.reports_view, lambda t: t.browse_month_of(
        int(_dt(2015, 6, 10, 12).timestamp()) * 1_000_000_000))
    _wait(app, lambda: not view._loading and view.list.block_count() > 0 and view._overview is not None)
    view.grab()                                    # painting is what asks for the pictures...
    _wait(app, lambda: len(view.list._pictures) >= 5, seconds=6.0)   # ...and workers decode them


def _pump(app: Any, n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


def build_window(root: Path, *, theme: str = "system", size: str = "1100x760",
                 seeded: bool = False, show: bool = False, timeline: bool = False) -> tuple:
    """The real window against a temporary store. Returns `(app, window, closers)`.

    `seeded` gives it documents and the real (keyword-only) `SearchEngine`, for
    the surface that needs results; every other surface stays an empty store.

    `show` **shows the window**. A window that was never shown is laid out at
    Qt's default 640x480 whatever `resize` said, so a tall page grabs
    *compressed* - buttons squeezed to grey bars, no scrollbar - which is not
    what anybody sees (measured 2026-09-20 on the Storage page: shown, its
    scrollbar is there and every box is at its minimum height). The goldens are
    made without it, so they keep matching; use `--show` when the question is
    what a page looks like on a screen.
    """
    from PyQt6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    if theme in ("light", "dark"):
        store.set_state("ui:theme", theme)
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    if seeded:
        from app.search.engine import SearchEngine

        seed(store, root)
        engine: Any = SearchEngine(store, _NoVectors(), _NoModel(), log_usage=False)
    else:
        engine = _Engine(store)
    if timeline:
        seed_timeline(store, root)
    window = MainWindow(settings, store, vectors, engine, debug=False)
    w, h = (int(x) for x in size.lower().split("x"))
    window.resize(w, h)
    if show:
        window.show()
        _pump(app, 20)
    _pump(app)
    return app, window, (store.close, vectors.close)


def _reach(app: Any, window: Any, spec: dict) -> Any:
    """Bring the surface forward; return the widget to grab."""
    rail = window.rail
    for index in range(rail.count()):
        if rail.tabText(index) == spec["page"]:
            rail.setCurrentIndex(index)
            break
    _pump(app)
    if spec.get("category"):
        page = rail.widget(rail.currentIndex())
        nav = None
        for view in (window.settings_view, window.indexing_view):
            if view is page or view.isAncestorOf(page) or page.isAncestorOf(view):
                nav = getattr(view, "_nav", None)
        if nav is not None:
            nav.show_category(spec["category"], persist=False)
    if spec.get("state") == "results":
        _show_results(app, window)
    elif spec.get("state") == "home":
        window.search_view.input.setText("")
    elif spec.get("state") == "timeline":
        _show_timeline(app, window)
    _pump(app)
    # A startup toast ("Indexing: Only when you ask.") is still up when the
    # first grabs are taken and sits over whatever is at the bottom of the page.
    window.toast.clear()
    _pump(app)
    return window


def _wait(app: Any, done: Any, seconds: float = 8.0) -> bool:
    """Run the event loop until `done()` or the time is up."""
    import time

    from PyQt6.QtTest import QTest

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done():
            return True
        QTest.qWait(25)
    return bool(done())


def _show_results(app: Any, window: Any) -> None:
    """Type a query, wait for the rows, select the first and open the inspector -
    the state §9i calls "results with the inspector"."""
    from PyQt6.QtCore import Qt

    from app.ui.view_options import ViewPreferences

    view = window.search_view
    view.input.setText("boiler quote dave")
    model = view.results._model
    _wait(app, lambda: model.rowCount() > 0)
    # A message's sender, subject and kind arrive from a worker a beat after its
    # row; until they do it is drawn without its badge. Wait for that too, or the
    # picture depends on which got there first.
    _wait(app, lambda: any(getattr(model.index(i, 0).data(int(Qt.ItemDataRole.UserRole)),
                                   "kind", "") == "email" for i in range(model.rowCount())))
    view.set_view_preferences(ViewPreferences(preview=True))
    view.results._list.setCurrentIndex(model.index(0, 0))
    _wait(app, lambda: view.preview.subtitle.text() != "Loading…"
          and bool(view.preview.text.toPlainText().strip()), seconds=4.0)
    # The grab runs keyword-only, so the search says "meaning-based search
    # returned nothing" in a banner over the rows; that is true of this run and
    # of no user's, and it would sit in every golden. The status line stays.
    view.notices.show_notices(())


def grab(names: Iterable[str], out: Path, *, theme: str = "system",
         size: str = "1100x760", show: bool = False) -> list[Path]:
    """Grab every named surface to `out/<name>.png`. Returns the files written."""
    names = list(names)
    out.mkdir(parents=True, exist_ok=True)
    # ignore_cleanup_errors: SqliteStore hands out one sqlite3 connection per
    # native thread (threading.local()), but a QThreadPool worker thread that
    # is reused for a second task does not reliably see its own cached
    # connection on the second call - it opens a fresh one, and the first is
    # silently overwritten in SqliteStore._open rather than closed. Walking
    # every rail page fans out enough background refreshes to hit this, so a
    # handle can still be open on Windows when this tries to delete the
    # tempdir - after every screenshot is already written to --out, which
    # lives outside this tempdir. Worth fixing in SqliteStore itself, not
    # papering over here; tracked as a follow-up rather than blocking this
    # tool on it.
    with tempfile.TemporaryDirectory(prefix="leasha-grab-", ignore_cleanup_errors=True) as tmp:
        seeded = any(SURFACES[n].get("state") == "results" for n in names)
        timeline = any(SURFACES[n].get("state") == "timeline" for n in names)
        app, window, closers = build_window(Path(tmp), theme=theme, size=size, seeded=seeded,
                                            show=show, timeline=timeline)
        written: list[Path] = []
        try:
            for name in names:
                spec = SURFACES[name]
                target = _reach(app, window, spec)
                image = target.grab()
                path = out / f"{name}.png"
                if not image.save(str(path), "PNG"):
                    raise RuntimeError(f"could not write {path}")
                written.append(path)
        finally:
            # Walking every rail page fans work out onto QThreadPool (each
            # view's own refresh() on first show) that is still in flight
            # when the last surface is grabbed. Closing the store while a
            # worker thread still holds it open is what left `knowledge.db`
            # locked and `TemporaryDirectory` unable to delete it on Windows.
            from PyQt6.QtCore import QThreadPool
            QThreadPool.globalInstance().waitForDone(10_000)
            for _ in range(5):
                app.processEvents()
            for close in closers:
                try:
                    close()
                except Exception:                 # noqa: BLE001 - teardown
                    pass
        return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--surface", action="append",
                        help="one surface only (repeatable); default: all")
    parser.add_argument("--size", default="1100x760", help="WxH, e.g. 1024x600")
    parser.add_argument("--theme", default="system", choices=("system", "light", "dark"))
    parser.add_argument("--out", default=str(ROOT / "outputs" / "screenshots"))
    parser.add_argument("--show", action="store_true",
                        help="show the window first, so pages get their real size and scrollbars")
    parser.add_argument("--list", action="store_true", help="print the surface names")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(SURFACES))
        return 0
    names = args.surface or list(SURFACES)
    unknown = [n for n in names if n not in SURFACES]
    if unknown:
        parser.error(f"unknown surface(s): {', '.join(unknown)}; try --list")
    out = Path(args.out)
    if args.theme != "system":
        out = out / args.theme
    for path in grab(names, out, theme=args.theme, size=args.size, show=args.show):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
