r"""Eyes for the coding agent: every surface of the real window, to PNG.

Layer: tooling, not app code - it drives the real L5 window offscreen and is
imported by `tests/unit/test_grab_ui.py` and `tools/guide_pictures.py`.

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
import contextlib
import os
import shutil
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
    "photos": {"page": "Photos"},
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


#: What the status line says a search took ("4 result(s) · 35ms"). The real
#: figure is different on every run - measured 2ms to 78ms on one machine - and
#: it is drawn on the results surface, so it is pinned here the way `_SEED_MTIME`
#: pins the dates. 35 is what the committed dark-1920x1080 golden shows.
SHOWN_ELAPSED_MS = 35.0


def _steady_clock(engine: Any) -> Any:
    """`engine`, with every response reporting `SHOWN_ELAPSED_MS`."""
    for name in ("search", "interim"):
        real = getattr(engine, name)

        def timed(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
            response = _real(*args, **kwargs)
            response.elapsed_ms = SHOWN_ELAPSED_MS
            return response

        setattr(engine, name, timed)
    return engine


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
    _require(app, lambda: (not view._loading and view.list.block_count() > 0
                           and view._overview is not None),
             "the timeline's June 2015 list and overview")
    _settle(app, window, "the timeline")
    # **Painting is what asks for the pictures, and workers decode them.** The
    # old wait was for "five or more decoded"; at 1100x760 four are on screen,
    # so it timed out silently on every run (measured 2026-09-29) and the grab
    # was of whichever had landed. The honest condition is the one the list
    # itself keeps: paint, let every picture that paint asked for arrive, and
    # repeat until a paint asks for nothing new. A decode the list skipped as
    # stale is asked for again by the next paint, so it cannot be missed.
    view_list = view.list
    for _ in range(10):
        view.grab()
        if not view_list._asked:
            break
        _require(app, lambda: not view_list._asked, "the timeline's thumbnails")
        _settle(app, window, "the timeline's thumbnails")
    else:
        raise GrabNotReadyError("the timeline kept asking for thumbnails after ten paints")
    if not view_list._pictures:
        raise GrabNotReadyError("the timeline drew no thumbnails at all")


def _pump(app: Any, n: int = 8) -> None:
    """Turn the event loop `n` times. One `processEvents` is not enough: a
    layout change posts events that post further events (resize, then polish,
    then paint), and a grab taken after one turn is of a half-laid-out page."""
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
    from PySide6.QtWidgets import QApplication

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
        engine: Any = _steady_clock(SearchEngine(store, _NoVectors(), _NoModel(), log_usage=False))
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
    # Every page refreshes itself on a worker the first time it is shown
    # (counts, lists, status). Without this the picture was of whichever of
    # those had landed by the time the grab happened.
    _settle(app, window, f"the {spec['page']} page")
    # A startup toast ("Indexing: Only when you ask.") is still up when the
    # first grabs are taken and sits over whatever is at the bottom of the page.
    window.toast.clear()
    _settle(app, window, f"the {spec['page']} page")
    return window


def _wait(app: Any, done: Any, seconds: float = 8.0) -> bool:
    """Run the event loop until `done()` or the time is up."""
    import time

    from PySide6.QtTest import QTest

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done():
            return True
        QTest.qWait(25)
    return bool(done())


#: How long anything a grab depends on may take to arrive. Generous on purpose:
#: a shared Windows CI runner is several times slower than a desk, and the only
#: cost of a long limit is a slow failure. The cost of a short one was a picture
#: of a half-drawn page that drifted from its golden (2026-09-29).
WAIT_SECONDS = 30.0


class GrabNotReadyError(RuntimeError):
    """Something a surface needs never arrived, so no picture was taken."""


def _require(app: Any, done: Any, what: str, seconds: float = WAIT_SECONDS,
             seen: Any = None) -> None:
    """`_wait`, but a timeout is an error naming `what` - never a picture.

    **A grab of a page that has not finished drawing is not a grab of that
    page.** `_wait` used to return False on a timeout and every caller ignored
    it, so a slow runner quietly photographed whatever state the page was in
    and the golden comparison then reported a drifted look that was really a
    race."""
    if not _wait(app, done, seconds):
        # 2026-10-05: `seen` says what *had* arrived. This error came up in
        # every four-process run of the suite that day and in none alone, and
        # "never arrived" could not tell a search that was never asked for from
        # one still running or one that answered with nothing.
        detail = ""
        if seen is not None:
            try:
                detail = f" Seen instead: {seen()}."
            except Exception as error:               # noqa: BLE001 - a diagnostic
                detail = f" (What was seen could not be read: {error}.)"
        raise GrabNotReadyError(f"{what} never arrived within {seconds:.0f}s, so the "
                           "surface was not grabbed (a slow machine, or a real hang)." + detail)


def _pending_timers(window: Any) -> list:
    """Single-shot timers still armed anywhere in the window: debounces (the
    search box's two tiers, the preview's), each one a piece of work that has
    not started yet. Repeating timers (watchers, pollers) never finish and are
    not counted.

    **Nor is how long a notice stays up.** A toast's timer is not work in
    flight, and a queue of them can outlast the wait: on 2026-10-08 the
    "shortcut is taken" warning (20s), queued behind the startup notices,
    kept the window "busy" past 30s on GitHub's runner and every grab there
    failed. The grab clears toasts anyway (`_reach`)."""
    from PySide6.QtCore import QTimer

    toast = getattr(window, "toast", None)

    def a_notice(timer: Any) -> bool:
        owner = timer.parent()
        while owner is not None:
            if owner is toast:
                return True
            owner = owner.parent()
        return False

    return [t for t in window.findChildren(QTimer)
            if t.isSingleShot() and t.isActive() and not a_notice(t)]


def _settle(app: Any, window: Any, what: str, seconds: float = WAIT_SECONDS) -> None:
    """Drain everything in flight - debounce timers, `QThreadPool` workers and
    the signals they post back - until the window is quiet for several turns.

    A worker that finishes posts its result to this thread, and handling that
    can start another (results, then their mail subtitles; a page's refresh,
    then its counts). So "quiet" is checked repeatedly, not once."""
    import time

    from PySide6.QtCore import QThreadPool
    from PySide6.QtTest import QTest

    pool = QThreadPool.globalInstance()
    end = time.monotonic() + seconds
    quiet = 0
    while quiet < 4:
        left = end - time.monotonic()
        if left <= 0:
            raise GrabNotReadyError(
                f"{what}: the window never went quiet within {seconds:.0f}s "
                f"({pool.activeThreadCount()} background task(s) running, "
                f"{len(_pending_timers(window))} debounce timer(s) armed), so the surface "
                "was not grabbed")
        pool.waitForDone(max(1, int(left * 1000)))
        QTest.qWait(25)
        busy = pool.activeThreadCount() or _pending_timers(window)
        quiet = 0 if busy else quiet + 1


def _show_results(app: Any, window: Any) -> None:
    """Type a query, wait for the rows, select the first and open the inspector -
    the state §9i calls "results with the inspector"."""
    from PySide6.QtCore import Qt

    from app.ui.view_options import ViewPreferences

    view = window.search_view
    # **Typing answers twice**: the keyword ("interim") tier after 150ms, the
    # full search after 400ms, each with its own status line, notices and
    # decoration pass. Waiting only for "some rows" photographed whichever had
    # landed - measured 2026-09-29 under CPU load: the interim page ("keyword
    # only, still searching...", phash 6 from the settled page), or the full
    # search's degraded-meaning banner arriving after it had been cleared
    # (phash 12). So: wait for the full tier by name, then for everything it
    # started, and only then select and preview.
    tiers: list[str] = []
    view.searched.connect(lambda shape: tiers.append(shape.get("tier", "")))
    view.input.setText("boiler quote dave")
    model = view.results._model
    def seen() -> str:
        from PySide6.QtCore import QThreadPool

        pool = QThreadPool.globalInstance()
        return (f"tiers answered {tiers}, {model.rowCount()} row(s), box holds "
                f"{view.input.text()!r}, status {view.status.text()!r}, "
                f"{pool.activeThreadCount()} of {pool.maxThreadCount()} pool thread(s) busy, "
                f"{len(_pending_timers(window))} debounce timer(s) armed, "
                f"page {window.rail.tabText(window.rail.currentIndex())!r}")

    _require(app, lambda: "full" in tiers and model.rowCount() > 0,
             "the full search's results for 'boiler quote dave'", seen=seen)
    # A message's sender, subject and kind arrive from a worker a beat after its
    # row; until they do it is drawn without its badge. Wait for that too, or the
    # picture depends on which got there first.
    _require(app, lambda: any(getattr(model.index(i, 0).data(int(Qt.ItemDataRole.UserRole)),
                                      "kind", "") == "email" for i in range(model.rowCount())),
             "the e-mail row's badge (its sender and kind, from the decoration worker)")
    _settle(app, window, "search results")
    view.set_view_preferences(ViewPreferences(preview=True))
    view.results._list.setCurrentIndex(model.index(0, 0))
    _require(app, lambda: view.preview.subtitle.text() != "Loading…"
             and bool(view.preview.text.toPlainText().strip()),
             "the preview of the first result")
    _settle(app, window, "the preview")
    # The grab runs keyword-only, so the search says "meaning-based search
    # returned nothing" in a banner over the rows; that is true of this run and
    # of no user's, and it would sit in every golden. The status line stays.
    view.notices.show_notices(())


#: The temporary store's folder. **A fixed name, not `mkdtemp`'s random one**:
#: the results surface draws the seeded files' paths (every row's breadcrumb,
#: the preview's title), so a random `leasha-grab-XXXXXXXX` put different
#: glyphs in every grab - 2 to 4 bits of phash between two runs of the same
#: code on the same machine, measured 2026-09-29. Eight letters after the dash,
#: like the random names the goldens were made with, so the width is the same.
GRAB_DIR_NAME = "leasha-grab-snapshot"


@contextlib.contextmanager
def _grab_dir() -> Any:
    """`<temp>/GRAB_DIR_NAME`, empty, removed afterwards. One grab at a time:
    a folder that is still there and cannot be removed is an error, not a
    reason to pick a different (random) name."""
    path = Path(tempfile.gettempdir()) / GRAB_DIR_NAME
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            raise GrabNotReadyError(f"{path} is left from an earlier grab and could not be removed "
                               "- is another grab running?")
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        # ignore_errors: see the note on the Windows handle in `grab`.
        shutil.rmtree(path, ignore_errors=True)


def grab(names: Iterable[str], out: Path, *, theme: str = "system",
         size: str = "1100x760", show: bool = False, workdir: Path | None = None) -> list[Path]:
    """Grab every named surface to `out/<name>.png`. Returns the files written.

    The temporary store lives in `workdir` when given (an empty folder the
    caller owns), otherwise in the fixed `_grab_dir()` - which is what makes two
    grabs of the same surface draw the same paths."""
    names = list(names)
    out.mkdir(parents=True, exist_ok=True)
    # Cleanup ignores errors (`_grab_dir`): SqliteStore hands out one sqlite3
    # connection per native thread (threading.local()), but a QThreadPool worker thread that
    # is reused for a second task does not reliably see its own cached
    # connection on the second call - it opens a fresh one, and the first is
    # silently overwritten in SqliteStore._open rather than closed. Walking
    # every rail page fans out enough background refreshes to hit this, so a
    # handle can still be open on Windows when this tries to delete the
    # tempdir - after every screenshot is already written to --out, which
    # lives outside this tempdir. Worth fixing in SqliteStore itself, not
    # papering over here; tracked as a follow-up rather than blocking this
    # tool on it.
    with (contextlib.nullcontext(workdir) if workdir is not None else _grab_dir()) as tmp:
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
            from PySide6.QtCore import QThreadPool
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
