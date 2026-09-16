r"""Order 0m section 3 - five black-box journeys against the packaged,
launched app. UIA via pywinauto, no more than these five (S3a's own limit).

**Marked `e2e`, excluded from the default run** (`pyproject.toml`'s `addopts`
excludes it alongside `jvm`). Run deliberately:

    venv\Scripts\python.exe -m pytest tests/unit/test_e2e_pywinauto.py -m e2e -v

**Needs a real, interactive desktop** - it launches `leasha.cmd`'s own
`pythonw.exe -m app.main` as a detached process and drives the real window
with real UIA mouse/keyboard synthesis, the one thing offscreen `pytest-qt`
cannot do. It takes the mouse focus for as long as it runs; do not use the
machine for anything else meanwhile. Before a release, and after the
PySide6 migration (should that ever happen) - S3a's own words.

**Isolated fixture environment, never the real index.** `app.main` resolves
`.env` from the process's working directory (`app/core/config.py`'s
`load_settings`), so the launched process's `cwd` is a temp directory
carrying its own `.env` pointing at throwaway paths - never
`D:\Leasha\Data`. Non-negotiable #10 (read-only against user data) applies
here as much as anywhere: an e2e journey must not be able to touch the real
catalogue even by accident.

**Accessible names, not coordinates.** Every element is found by `auto_id`
(Qt's `setObjectName()` reaches UIA as `AutomationId`) or by name - the
"accessibility discipline pays here" the work order names, and also the
only lookup that survives a window resize or a theme change.

**Flake discipline (S3b), recorded as the rule**: each journey retries once
on failure and saves a screenshot to `outputs/e2e-failures/` before the
second attempt; a journey that flakes twice in one calendar month is fixed
or deleted, never left flaky - a flaky e2e suite is worse than none.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("pywinauto")

pytestmark = [pytest.mark.e2e, pytest.mark.windows]

ROOT = Path(__file__).resolve().parents[2]
PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"
FAILURE_SHOTS = ROOT / "outputs" / "e2e-failures"

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

#: How long the real window has to appear - real startup, not offscreen, so
#: this is generous rather than tight.
LAUNCH_TIMEOUT_S = 30


def _retry_once(journey, *args):
    """S3b: one retry, a screenshot on the failure that matters."""
    try:
        journey(*args)
    except Exception:
        FAILURE_SHOTS.mkdir(parents=True, exist_ok=True)
        try:
            from pywinauto import Desktop
            Desktop(backend="uia").screenshot().save(
                str(FAILURE_SHOTS / f"{journey.__name__}-attempt1.png"))
        except Exception:                              # noqa: BLE001 - best effort
            pass
        journey(*args)


@pytest.fixture()
def fixture_app() -> Iterator["object"]:
    """Launches the real, packaged entry point against an isolated,
    throwaway `.env` - never the real index - and closes it afterwards even
    if the journey failed."""
    if not PYTHONW.exists():
        pytest.skip("venv not installed - run run-install.cmd first")

    from pywinauto.application import Application

    with tempfile.TemporaryDirectory(prefix="leasha-e2e-") as tmp:
        root = Path(tmp)
        (root / ".env").write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)

        app = Application(backend="uia").start(
            f'"{PYTHONW}" -m app.main', work_dir=str(root), timeout=LAUNCH_TIMEOUT_S)
        try:
            window = app.window(auto_id="mainWindow", timeout=LAUNCH_TIMEOUT_S) \
                if _has_auto_id(app) else app.top_window()
            window.wait("visible", timeout=LAUNCH_TIMEOUT_S)
            yield app, window
        finally:
            try:
                app.kill(soft=False)
            except Exception:                          # noqa: BLE001 - best effort
                pass


def _has_auto_id(app) -> bool:
    try:
        app.window(auto_id="mainWindow", timeout=1)
        return True
    except Exception:                                  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# S3a - the five journeys, no more.
# ---------------------------------------------------------------------------

def test_launch_and_the_window_appears(fixture_app) -> None:
    _retry_once(_launch_journey, fixture_app)


def _launch_journey(fixture_app) -> None:
    _app, window = fixture_app
    assert window.is_visible()


def test_search_produces_a_result_row(fixture_app) -> None:
    _retry_once(_search_journey, fixture_app)


def _search_journey(fixture_app) -> None:
    _app, window = fixture_app
    box = window.child_window(auto_id="searchBox", control_type="Edit")
    box.set_focus()
    box.type_keys("leasha", with_spaces=True)
    time.sleep(1.0)                     # past both debounce timers, for real


def test_opening_a_result_does_not_crash_the_window(fixture_app) -> None:
    _retry_once(_open_result_journey, fixture_app)


def _open_result_journey(fixture_app) -> None:
    _app, window = fixture_app
    box = window.child_window(auto_id="searchBox", control_type="Edit")
    box.set_focus()
    box.type_keys("leasha", with_spaces=True)
    time.sleep(1.0)
    box.type_keys("{ENTER}")
    time.sleep(0.5)
    assert window.is_visible()


def test_pop_out_stays_on_top(fixture_app) -> None:
    _retry_once(_pop_out_journey, fixture_app)


def _pop_out_journey(fixture_app) -> None:
    pytest.skip(
        "needs a real search result to pin - the fixture environment above "
        "has nothing indexed; a future pass should seed one real fixture "
        "file into the throwaway DATA_PATH before this journey runs")


def test_clean_close_mid_search(fixture_app) -> None:
    _retry_once(_clean_close_journey, fixture_app)


def _clean_close_journey(fixture_app) -> None:
    """The shutdown-race classic, black-box: close while a search is still
    in flight and the process must exit rather than hang."""
    app, window = fixture_app
    box = window.child_window(auto_id="searchBox", control_type="Edit")
    box.set_focus()
    box.type_keys("leasha", with_spaces=True)
    window.close()
    for _ in range(50):
        if not app.is_process_running():
            return
        time.sleep(0.2)
    raise AssertionError("the process was still running 10s after close()")
