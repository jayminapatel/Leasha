"""Shared pytest fixtures.

Layer: L0
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

# Tests import `app.*`, so the project root must be importable even when pytest
# is invoked from somewhere else.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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
