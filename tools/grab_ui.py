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
    "offline-media": {"page": "Offline"},
    "reports": {"page": "Reports"},
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


def _pump(app: Any, n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


def build_window(root: Path, *, theme: str = "system", size: str = "1100x760") -> tuple:
    """The real window against a temporary store. Returns `(app, window, closers)`."""
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
    window = MainWindow(settings, store, vectors, _Engine(store), debug=False)
    w, h = (int(x) for x in size.lower().split("x"))
    window.resize(w, h)
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
        window.search_view.input.setText("boiler quote dave sent last winter")
    elif spec.get("state") == "home":
        window.search_view.input.setText("")
    _pump(app)
    return window


def grab(names: Iterable[str], out: Path, *, theme: str = "system",
         size: str = "1100x760") -> list[Path]:
    """Grab every named surface to `out/<name>.png`. Returns the files written."""
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
        app, window, closers = build_window(Path(tmp), theme=theme, size=size)
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
    for path in grab(names, out, theme=args.theme, size=args.size):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
