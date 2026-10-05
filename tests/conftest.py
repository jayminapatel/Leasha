"""Shared pytest fixtures.

Layer: L0
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import os

import pytest

# Tests import `app.*`, so the project root must be importable even when pytest
# is invoked from somewhere else.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# hypothesis (order 0m §2e)
# ---------------------------------------------------------------------------
#
# The default profile is deliberately cheap: `max_examples=25` keeps every
# property test in this suite under CI's ordinary budget instead of the
# hundreds hypothesis tries by default, which is how a fuzz test becomes the
# one that flakes on a slow runner and gets skipped rather than trusted.
# `deadline=None` for the same reason from the other direction - the *count*
# of examples is the knob that matters, not a per-example wall clock this
# machine's own variance would trip on its own. A test that wants to fuzz
# harder opts in explicitly with its own `@settings(...)`, which hypothesis
# always prefers over the loaded profile.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "leasha", max_examples=25, deadline=None,
        suppress_health_check=[HealthCheck.too_slow])
    settings.register_profile(
        "leasha-thorough", max_examples=300, deadline=None,
        suppress_health_check=[HealthCheck.too_slow])
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "leasha"))
except ImportError:                                   # pragma: no cover
    pass


ENV_TEMPLATE = """\
DATA_PATH={data}
VECTOR_PATH={data}/vectors
FTS_DB={data}/fts/knowledge.db
CACHE_PATH={data}/cache
MODEL_CACHE={data}/models
STATE_PATH={data}/state
PROJECT_PATH={project}
LOG_PATH={project}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=true

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=5
REQUIRED_FREE_GB=150
"""


# ---------------------------------------------------------------------------
# The suite runs on the processor, not the graphics card (2026-09-19)
# ---------------------------------------------------------------------------
#
# `EMBED_DEVICE` defaults to `auto`, and on a machine with DirectML `auto` means
# the graphics card. So the real-model tests (embedder, CLIP, reranker, OCR)
# quietly built DirectML ONNX sessions inside the pytest process. After roughly
# a thousand tests had loaded torch, pyarrow and more ONNX sessions alongside
# them, a later DirectML run - RapidOCR's text detector, `InferenceSession.run` -
# raised a native access violation: exit code 0xC0000005, no Python traceback,
# no Windows event, and every test after it silently never reported. It killed
# three full runs in a row before being found (`onnxruntime` fault stack in the
# probe log, then a run pinned to the processor completing normally).
#
# It also meant the suite competed with the running application for the same
# card. Nothing here tests the card - the device *choice* is tested with fake
# profiles in `test_backends.py` - so the suite does not need it.
#
# Set to `auto` (or `gpu`) in the environment to run the suite against the card
# on purpose: `EMBED_DEVICE=auto python -m pytest tests/unit/test_embedder.py`.
os.environ.setdefault("EMBED_DEVICE", "cpu")

# **Real fonts for the offscreen Qt on Windows.** The suite runs Qt with
# `QT_QPA_PLATFORM=offscreen`, and that platform does not know where Windows
# keeps its fonts (Qt no longer bundles any; it looks beside its own libraries,
# finds nothing, and says "QFontDatabase: Cannot find font directory" once, at
# start-up, before any test's messages are captured). With no font at all Qt
# measures every letter as a square box one font-size wide - so on the Windows
# CI "Storage & maintenance" measured 325 pixels, "Up to date" wrapped onto two
# lines, and a chip was too short for its rounded corners. Eight layout tests
# failed there that pass on Linux, where fontconfig finds the fonts, and they
# fail here the same way, to the pixel, with fontconfig pointed at an empty
# folder (2026-09-29). The real application uses the `windows` platform, which
# asks Windows for its fonts and never reads this variable; `tools/grab_ui.py`
# sets it the same way for the goldens. Set before the first `QApplication`,
# because the font folder is read once, when that is built.
if sys.platform == "win32":
    _windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if _windows_fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_windows_fonts))


@pytest.fixture(autouse=True, scope="session")
def _repository_detection_stops_at_the_test_tree(tmp_path_factory) -> Iterator[None]:
    """A test's folder must not be "inside a repository" because of what happens
    to sit above pytest's temp directory.

    `enclosing_repo` climbs to the drive root looking for a `.git` - correct for
    an indexed root below a checkout, and wrong for a test that says "this folder
    is not a repository": on 2026-09-19 that was a stray `.git` in the user's home
    folder (Windows' temp directory lives under it), and the project's own `.git`
    when the temp directory is `.pytest_tmp`. About twenty tests failed for it,
    on every run, on the one machine that had it - and passed everywhere else.

    The walk is bounded at pytest's base temp directory for any path inside it.
    A repository a test *builds* inside its own folder is still found, because
    it is below the ceiling; nothing above the ceiling is looked at.
    """
    from app.index import pipeline, walker

    base = Path(tmp_path_factory.getbasetemp()).resolve()
    real = walker.enclosing_repo

    def bounded(start, *, ceiling=None):
        try:
            resolved = Path(start).resolve()
            inside = base == resolved or base in resolved.parents
        except OSError:
            inside = False
        return real(start, ceiling=ceiling if ceiling is not None else (base if inside else None))

    walker.enclosing_repo, pipeline.enclosing_repo = bounded, bounded
    # The same boundary for the real `git` the gitsearch commands run: git
    # climbs on its own, and `GIT_CEILING_DIRECTORIES` is its own way to stop it.
    # Set to the *parent* of the temp tree so a repository inside it is found and
    # nothing above it is.
    previous = os.environ.get("GIT_CEILING_DIRECTORIES")
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base.parent)
    try:
        yield
    finally:
        walker.enclosing_repo, pipeline.enclosing_repo = real, real
        if previous is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = previous


@pytest.fixture(autouse=True, scope="session")
def _the_machines_own_locks_are_left_alone() -> Iterator[None]:
    """No test takes Leasha's real window lock or its real index-run lock.

    Both are machine-wide Windows mutexes, and `lock_dir` / `TMPDIR` - how the
    tests thought they were isolated - only place a lock *file* on Linux and
    macOS. So on the owner's laptop an open Leasha failed the tests that
    needed the window's lock, a real index run would have failed every test
    that needed the run lock, and a test holding it could have refused the
    owner's real run (2026-09-30). `tests/private_locks.py` has the whole
    story; `tests/unit/test_run_lock.py` holds the real locks and proves
    nothing here notices.

    Session-wide and autouse because most of the tests that reach a lock never
    name it: they call `cmd_index`, start an `IndexWorker`, or build a window
    whose four-second timer probes the run lock. Not undone at the end - the
    process is ending, and a window left open by a test is still probing.
    """
    from tests import private_locks

    private_locks.install_everywhere()
    yield


@pytest.fixture(autouse=True, scope="session")
def _pin_ocr_to_the_processor() -> Iterator[None]:
    """`ocr._device` is a module default that only the entry points set, so the
    environment variable above never reaches it in a test process."""
    from app.extract import ocr

    ocr.configure_device(os.environ.get("EMBED_DEVICE", "cpu"))
    yield


@pytest.fixture(autouse=True)
def _clear_gpu_unreliable_latch() -> Iterator[None]:
    """`app.core.gpu_serialize.mark_gpu_unreliable` is a process-wide latch
    that the application never clears (2026-09-08). One test that trips it
    - a simulated driver failure in the embedder, OCR or reranker suites -
    would otherwise send every later `backends.choose()` in the same pytest
    process to the processor and turn the backends suite red for a reason
    that has nothing to do with the test that failed. Cleared before and
    after every test, so no module has to remember."""
    from app.core import gpu_serialize

    gpu_serialize._reset_for_tests()
    yield
    gpu_serialize._reset_for_tests()


@pytest.fixture()
def temp_env(tmp_path: Path) -> Iterator[Path]:
    """A valid .env pointing at throwaway directories. Yields the .env path."""
    data = tmp_path / "index_data"
    project = tmp_path / "project"
    data.mkdir()
    project.mkdir()

    env_file = tmp_path / ".env"
    env_file.write_text(
        ENV_TEMPLATE.format(data=data.as_posix(), project=project.as_posix()),
        encoding="utf-8",
    )
    yield env_file


@pytest.fixture()
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def fixture_root() -> Path:
    """The Layer 2 fixture corpus, generated on demand.

    Fixtures are built rather than committed - see `tests/fixtures/generate.py`
    for why. Generation is idempotent, so this costs nothing after the first run.
    """
    from tests.fixtures.generate import ensure_fixtures

    return ensure_fixtures()


# ---------------------------------------------------------------------------
# Qt
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _qt_application():
    """One `QApplication` for the whole session, held for the whole session.

    **Not tidiness - the suite was aborting without it.** Several test modules
    wrote `QApplication.instance() or QApplication([])` and discarded the
    result. When that line is the one that *creates* the application, nothing
    holds a reference, Python collects it, and the next widget built anywhere
    in the process aborts inside Qt. Which module hit it depended on collection
    order, so the same suite passed and crashed on alternate runs and the
    failure never pointed at the line responsible.

    Session-scoped and autouse so no module has to remember. Skipped entirely
    where Qt is not installed, which is a normal state for this project - the
    headless checks are the majority.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:                            # noqa: BLE001 - no Qt, no fixture
        yield None
        return

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    yield application
    # Deliberately not `quit()`: teardown order across modules is exactly what
    # this fixture exists to stop mattering, and destroying the application
    # while a widget somewhere is still alive is the same abort by another
    # route. The process is ending anyway.


def pytest_collection_modifyitems(config, items):
    """A test marked `windows` runs on Windows and is skipped anywhere else.

    The marker has been in `pyproject.toml` all along ("requires Windows") and
    nothing acted on it: such a test ran on macOS and failed for want of
    PowerShell's scheduled tasks or a drive letter (2026-10-05, the first
    whole-suite run on a Mac). Skipped, not deselected, so the count says how
    many there are.
    """
    import sys

    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="needs Windows (marked `windows`)")
    for item in items:
        if "windows" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _on_mains_power(monkeypatch):
    """Every test runs as if the machine were plugged in.

    **A hang, not a failure.** The resource governor pauses an index run "On
    battery. Indexing resumes on mains power." - which is right for the owner's
    laptop and wrong for a test that starts a real run: it waits for a charger
    for ever and is killed by its time limit, with nothing in its output but a
    stack in `resources.wait_while_throttled`. Found on 2026-10-05, when the
    laptop was unplugged in the middle of a session: `test_run_setup.py` stalled
    at its first real run, twice, and the cause was only in the run's own log
    file under the test's temporary folder.

    A test about the battery rule sets its own reading after this one and
    wins; the governor's own tests hand it a snapshot and never ask psutil.
    """
    try:
        import psutil
    except Exception:                            # noqa: BLE001 - no psutil, nothing to pin
        return

    class _Plugged:
        percent = 100.0
        secsleft = -2
        power_plugged = True

    monkeypatch.setattr(psutil, "sensors_battery", lambda: _Plugged(), raising=False)


@pytest.fixture(autouse=True)
def _no_florence_model_load(request, monkeypatch):
    """No test may download or load the Florence-2 captioning model.

    **A hang, not a failure.** Any test whose OCR fake reads nothing makes the
    image "photo-class", and the extractor then calls `florence_tagger.tag_image`,
    which - with torch and transformers installed - imports transformers (tens of
    seconds) and downloads about a gigabyte from the Hugging Face hub. Stack dump of
    `test_clip_lane_pipeline::test_image_vector_is_written_regardless_of_ocr_text`
    (2026-09-20): the extraction worker sat in `florence_tagger._load` while
    `Pipeline._consume` polled its queue every 0.25 s - a live worker, not a lost
    sentinel. A test that wants a tagger patches `_load` itself, which overrides this;
    the two real-model proofs are marked `slow`, and that marker opts them out.
    """
    if request.node.get_closest_marker("slow") is not None:
        return
    try:
        from app.extract import florence_tagger
    except Exception:                            # noqa: BLE001 - not importable, nothing to guard
        return
    monkeypatch.setattr(florence_tagger, "_load", lambda: None)


@pytest.fixture(autouse=True)
def no_window_is_collected_while_it_paints():
    """The cyclic collector is off while a test runs, and runs once after it.

    **Found 2026-09-30.** `test_number_fields.py` and `test_timed_out_panel.py`
    in one process ended in a Windows access violation inside
    `ShimmerBar.paintEvent`, at `painter.setPen`, with an active painter. Each
    file passed alone; so did the pair with any extra line ahead of the painter.

    The cause: a test builds a top-level widget as a local and shows it.
    pytest-qt keeps only a weak reference, and processes events *after* the
    test function has returned - so the widget is painted while nothing holds
    it. Its signal connections put it in a reference cycle, so it waits for the
    collector, and the allocations of a paint are what trigger the collector:
    the window is deleted from inside its own child's `paintEvent`. Whether the
    threshold falls inside a paint depends on everything allocated before it,
    which is why it needed a particular file in front and vanished under
    observation.

    With the collector off for the length of a test the window lives until
    pytest-qt closes it, and the collection afterwards happens outside any
    paint. A test that wants a collection still calls `gc.collect()` itself.
    Proved by the pair above: 139 every time without this, 0 with it.

    **Only the young generations are collected after each test.** A full
    collection there walks everything the run has ever kept - and windows are
    deliberately kept (`gui_mainwindow`) - so it cost more with every test:
    81 s against 32 s for six files, and a whole run that had taken 25
    minutes was at 56% after an hour. What a test leaves behind is new, so
    generation 1 reaches it; the old generation is still collected by Python
    itself between tests, where the collector is on.
    """
    import gc

    was_enabled = gc.isenabled()
    gc.disable()
    yield
    if was_enabled:
        gc.enable()
    gc.collect(1)
