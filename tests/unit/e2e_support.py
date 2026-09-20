r"""Scaffolding for the black-box journeys in `test_e2e_pywinauto.py`
(order 0m section 3).

Layer: n/a (test support)

**Never the real index - by construction, not by promise.** The application
finds its `.env` from the *code's own location* (`app/core/config.py`
`project_root()` is two levels above the `app` package), not from the working
directory - so a launched app's data folder is decided by where its code sits.
`build_scratch_install` therefore copies `app/`, `assets/`, `config/` and
`VERSION` into a scratch folder and writes that folder its own `.env`, logs and
data. The launched process is `pythonw -m app.main` run *from there*: the same
entry point `leasha.cmd` uses, the same code as the working tree, and no path
that leads to `D:\Leasha\Data` (non-negotiable #10). It also gives the app a
scratch profile: everything it writes (window state, logs, crash reports) lands
under the scratch folder.

**Model cache.** A first-ever `bge-small` download is 65 MB, so the scratch
`.env` points `MODEL_CACHE` at the nightly's persistent cache
(`LEASHA_NIGHTLY_MODEL_CACHE`, else `logs/nightly-models`) - never the real
one. The launched app runs on the processor (`EMBED_DEVICE=cpu`): it must not
compete with a running index for the graphics card, and a window test has no
business timing it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ENV = """\
DATA_PATH={d}/data
VECTOR_PATH={d}/data/vectors
FTS_DB={d}/data/fts/knowledge.db
CACHE_PATH={d}/data/cache
MODEL_CACHE={m}
STATE_PATH={d}/data/state
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
EMBED_DEVICE=cpu
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=false

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""

#: Words the journeys search for. Distinct enough that a result row can only
#: be one of these files.
DOCUMENTS = {
    "barnsley-survey.txt": "The barnsley site survey report. Two isolation valves need replacing.",
    "newcastle-invoice.txt": "Newcastle invoice and receipt bundle for the pump station.",
    "leeds-notes.txt": "Leeds commissioning notes, draft. Flow rates checked on Tuesday.",
}
SEARCH_WORD = "barnsley"


def interpreter_dir() -> Path:
    """Where `python.exe` / `pythonw.exe` are: `LEASHA_PYTHON`'s folder, else
    the interpreter running pytest, else the checkout's own `venv`."""
    override = os.environ.get("LEASHA_PYTHON")
    if override:
        return Path(override).parent
    if Path(sys.executable).name.lower().startswith("python"):
        return Path(sys.executable).parent
    return ROOT / "venv" / "Scripts"


def model_cache() -> Path:
    override = os.environ.get("LEASHA_NIGHTLY_MODEL_CACHE")
    path = Path(override) if override else ROOT / "logs" / "nightly-models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_scratch_install(base: Path, *, index: bool = True) -> Path:
    """A self-contained copy of the application in `base/install`, with its own
    `.env`, and (unless `index=False`) three documents already in its index. Returns the install folder - the launched process's cwd."""
    install = base / "install"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("app", "assets", "config"):
        shutil.copytree(ROOT / name, install / name, ignore=ignore)
    shutil.copy2(ROOT / "VERSION", install / "VERSION")
    (install / "leasha.cmd").write_text("@echo off\r\n", encoding="ascii")   # deeplink looks for it

    env = install / ".env"
    env.write_text(ENV.format(d=install.as_posix(), m=model_cache().as_posix()),
                   encoding="utf-8")

    docs = base / "documents"
    docs.mkdir()
    for name, text in DOCUMENTS.items():
        (docs / name).write_text(text, encoding="utf-8")

    if index:
        _seed_index(install, docs)
    return install


_SEED = r"""
import json, sys
from pathlib import Path
from app.core.config import load_settings
from app.storage.sqlite_store import SqliteStore

docs = Path(sys.argv[1])
settings = load_settings(Path(".env"))
with SqliteStore(settings.fts_db).connect() as store:
    for name, text in json.loads(sys.argv[2]).items():
        path = docs / name
        stat = path.stat()
        file_id = store.upsert_file(
            str(path), parent_dir=str(docs), ext="txt", size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
"""


def _seed_index(install: Path, docs: Path) -> None:
    """Three rows, straight into the scratch store, from a child process run in
    the scratch install (so it reads the scratch copy of the code and never
    disturbs the test process's own `app` modules). No CLI, so it does not queue
    behind the machine-wide index-run mutex that a real index run - or the
    nightly - may be holding. Keyword search finds the rows without any model."""
    import json

    result = subprocess.run(
        [str(interpreter_dir() / "python.exe"), "-c", _SEED, str(docs), json.dumps(DOCUMENTS)],
        cwd=str(install), capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"seeding the scratch index failed: {result.stderr[-800:]}")


def launch_command(install: Path) -> str:
    """What `leasha.cmd` runs for the window: the windowed interpreter, `-m app.main`."""
    return f'"{interpreter_dir() / "pythonw.exe"}" -m app.main'


# ---------------------------------------------------------------------------
# Driving the launched app (pywinauto / UIA)
# ---------------------------------------------------------------------------

import json                                                         # noqa: E402
import time                                                         # noqa: E402
from datetime import datetime                                        # noqa: E402

FAILURE_SHOTS = ROOT / "outputs" / "e2e-failures"
E2E_LOG = ROOT / "logs" / "e2e.log"

#: The window's real startup is 12 s on an idle machine and 40 s on a busy one
#: (the splash, then the stores, then the window - see the run log's
#: "startup: timings" line), so this is generous rather than tight.
WINDOW_TIMEOUT_S = 100

#: Hard wall-clock cap on one launched app. A watchdog thread kills *its own*
#: process tree when this passes, so a hung app can never outlive the run that
#: started it (and nothing else on the machine is ever touched).
APP_LIFETIME_S = 420


class FocusLost(AssertionError):
    """Another window took the foreground; refusing to click or type blind."""


class LaunchedApp:
    r"""`pythonw -m app.main` running from a scratch install.

    `venv\Scripts\pythonw.exe` is a launcher stub: the process it starts is
    not the one that owns the window (the stub's child, the base interpreter,
    does). `pids()` is therefore the whole tree, and "the app has exited"
    means every one of them is gone."""

    def __init__(self, install: Path) -> None:
        self.install = install
        # `tests/conftest.py` sets QT_QPA_PLATFORM=offscreen for the whole pytest
        # process; inherited, the app would start with no window on any desktop
        # (its log says "This plugin does not support setting window opacity").
        env = {k: v for k, v in os.environ.items()
               if k not in ("QT_QPA_PLATFORM", "PYTEST_CURRENT_TEST")}
        self.proc = subprocess.Popen(
            launch_command(install), cwd=str(install), close_fds=True, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.perf_counter()
        self.window_seconds: float | None = None
        import threading

        self._watchdog = threading.Timer(APP_LIFETIME_S, self.kill)
        self._watchdog.daemon = True
        self._watchdog.start()

    # -- input, only ever into this app -------------------------------------

    def focus(self, window) -> None:
        """Bring `window` to the foreground and prove it is there.

        `click_input` and `type_keys` act on the *screen*: with another
        application on top (the coding tool this suite is often run from), a
        click lands in that application and typed text goes to its input box.
        So nothing is sent until the foreground window belongs to this app's own
        process tree; otherwise `FocusLost`, which the journey's retry absorbs."""
        import ctypes
        from ctypes import wintypes

        pids = self.pids()
        for _ in range(6):
            try:
                window.set_focus()
            except Exception:                                        # noqa: BLE001 - try again
                pass
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
            if pid.value in pids:
                return
            time.sleep(0.4)
        raise FocusLost("another window holds the foreground; not clicking or typing into it")

    def click(self, window, element) -> None:
        self.focus(window)
        element.click_input()

    def type(self, window, element, keys: str, **kwargs) -> None:
        self.focus(window)
        element.click_input()
        self.focus(window)
        element.type_keys(keys, **kwargs)

    def pids(self) -> set[int]:
        import psutil

        try:
            root = psutil.Process(self.proc.pid)
            return {root.pid} | {c.pid for c in root.children(recursive=True)}
        except psutil.NoSuchProcess:
            return set()

    def running(self) -> bool:
        import psutil

        return any(psutil.pid_exists(pid) and psutil.Process(pid).status()
                   != psutil.STATUS_ZOMBIE for pid in self.pids())

    def find_window(self, timeout: float = WINDOW_TIMEOUT_S):
        """The main window (`class MainWindow`, title `Leasha`), once visible."""
        from pywinauto import Desktop

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = self.pids()
            if not pids and self.proc.poll() is not None:
                raise RuntimeError(
                    f"the app exited before its window appeared (exit code {self.proc.returncode}); "
                    f"see {self.install / 'logs' / 'runs'}")
            for candidate in Desktop(backend="uia").windows(class_name="MainWindow"):
                try:
                    if candidate.process_id() in pids and candidate.is_visible():
                        self.window_seconds = time.perf_counter() - self.started
                        return candidate
                except Exception:                                    # noqa: BLE001 - it is still appearing
                    continue
            time.sleep(0.5)
        raise TimeoutError(f"no Leasha window within {timeout:.0f} s")

    def close_and_time(self, window, timeout: float = 60.0) -> float:
        """Close the way a person does and return the seconds until every
        process in the tree has gone. Raises `AssertionError` when it has not."""
        started = time.perf_counter()
        window.close()
        while time.perf_counter() - started < timeout:
            if not self.running():
                return time.perf_counter() - started
            time.sleep(0.1)
        raise AssertionError(
            f"the app was still running {timeout:.0f} s after its window was closed "
            f"(pids {sorted(self.pids())}) - order 0u section 6d: capture its stack, then kill it")

    def kill(self) -> None:
        import psutil

        self._watchdog.cancel()

        for pid in sorted(self.pids(), reverse=True):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def other_leasha_window_is_open() -> bool:
    """The window holds a machine-wide named mutex: a second launch hands over
    to the first and exits. A journey must not run into the owner's session."""
    from pywinauto import Desktop

    return any(w.window_text() == "Leasha"
               for w in Desktop(backend="uia").windows(class_name="MainWindow"))


def descendants(window, **kwargs) -> list:
    """`window.descendants()`, tolerating the tree changing under it.

    The tree is live: a control that is created or destroyed while pywinauto
    enumerates it comes back with no control type (`KeyError: None`) or a
    `COMError` from the provider. Both are "look again", never a finding."""
    last: Exception | None = None
    for _ in range(8):
        try:
            return window.descendants(**kwargs)
        except (KeyError, OSError, AttributeError, ValueError) as exc:
            last = exc
        except Exception as exc:                                     # noqa: BLE001 - comtypes.COMError
            if type(exc).__name__ != "COMError":
                raise
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"the UI tree would not hold still: {last!r}")


def by_id(window, suffix: str, control_type: str | None = None, timeout: float = 10.0):
    """The descendant whose UIA automation id ends `.suffix` - Qt reports the
    full object-name path (`QApplication.MainWindow.…searchBox`), and only the
    last part is a name anyone chose. Raises `LookupError` when none appears."""
    deadline = time.monotonic() + timeout
    while True:
        kwargs = {"control_type": control_type} if control_type else {}
        for element in descendants(window, **kwargs):
            automation_id = element.element_info.automation_id or ""
            if automation_id == suffix or automation_id.endswith("." + suffix):
                return element
        if time.monotonic() >= deadline:
            raise LookupError(f"nothing with automation id ...{suffix} appeared")
        time.sleep(0.3)


def by_name(window, name: str, control_type: str, timeout: float = 10.0, *, prefix: bool = False):
    deadline = time.monotonic() + timeout
    while True:
        for element in descendants(window, control_type=control_type):
            found = element.element_info.name or ""
            if found == name or (prefix and found.startswith(name)):
                return element
        if time.monotonic() >= deadline:
            raise LookupError(f"no {control_type} named {name!r} appeared")
        time.sleep(0.3)


def names(window, control_type: str) -> list[str]:
    return [e.element_info.name or "" for e in descendants(window, control_type=control_type)]


def all_text(window) -> str:
    """Every name and value the window exposes, in one string - for "is this
    sentence anywhere on screen" when the control holding it has no name."""
    parts = []
    for element in descendants(window):
        parts.append(element.element_info.name or "")
        try:
            parts.append(element.get_value())
        except Exception:                                            # noqa: BLE001 - most controls have none
            pass
        try:
            parts.append(element.window_text())
        except Exception:                                            # noqa: BLE001
            pass
    return " ".join(p for p in parts if isinstance(p, str))


def settled(window, timeout: float = 30.0) -> None:
    """Wait until the search has finished (the status line stops saying
    "still searching") and the rows have had time for the metadata redraw that
    follows. Clicking a row *during* that window is a real-user race the
    journeys report separately rather than depend on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = by_id(window, "searchStatus", "Text", timeout=2).element_info.name or ""
        except LookupError:
            status = ""
        if "still searching" not in status:
            time.sleep(1.5)
            return
        time.sleep(0.4)
    raise TimeoutError("the search never finished")


def preview_text(window) -> str:
    """What the preview pane's text browser holds, read through UIA's text
    pattern (its control name is only the word "Preview")."""
    try:
        browser = by_id(window, "QTextBrowser", "Text", timeout=3)
        return browser.iface_text.DocumentRange.GetText(-1) or ""
    except Exception:                                                # noqa: BLE001
        return ""


def is_topmost(window) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.GetWindowLongW(window.handle, -20) & 0x8)   # WS_EX_TOPMOST


def screenshot(path: Path) -> None:
    """The whole desktop, best effort - a failing journey's evidence."""
    try:
        from PIL import ImageGrab

        path.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(all_screens=True).save(str(path))
    except Exception:                                                # noqa: BLE001
        pass


def note(event: dict) -> None:
    """One JSON line in `logs/e2e.log`: timings and flakes, for whoever reads it later."""
    try:
        E2E_LOG.parent.mkdir(parents=True, exist_ok=True)
        with E2E_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **event}) + "\n")
    except OSError:
        pass


def flakes_this_month(journey: str) -> int:
    month = datetime.now().strftime("%Y-%m")
    try:
        lines = E2E_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "flake" and row.get("journey") == journey \
                and str(row.get("at", "")).startswith(month):
            count += 1
    return count
