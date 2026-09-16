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
