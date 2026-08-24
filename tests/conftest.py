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
