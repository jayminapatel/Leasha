r"""Order 202626270514 (0l) section 2b - the CLI surface.

Layer: L3

`--allow-cloud-content` and `--cloud-content-cap-mb` reach `WalkConfig`
through `cmd_index` - checked here by capturing the `PipelineConfig` a real
parsed command line produces, with `Pipeline` itself replaced by a stub so
this never touches a real model or walks a real corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import cli


def _env_file(tmp_path: Path) -> str:
    data = tmp_path / "data"
    path = tmp_path / ".env"
    path.write_text(
        f"DATA_PATH={data}\n"
        f"VECTOR_PATH={data / 'vectors'}\n"
        f"FTS_DB={data / 'index.db'}\n"
        f"CACHE_PATH={data / 'cache'}\n"
        f"MODEL_CACHE={data / 'models'}\n"
        f"STATE_PATH={data / 'state'}\n"
        f"LOG_PATH={tmp_path / 'logs'}\n"
        "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n",
        encoding="utf-8",
    )
    return str(path)


class _StubPipeline:
    """Captures the `PipelineConfig` it was built with; never walks anything."""

    captured: list = []

    def __init__(self, store, vectors, embedder, config, **kwargs):
        _StubPipeline.captured.append(config)
        self._store = store

    def run(self, on_progress=None):
        from app.index.pipeline import IndexStats
        return IndexStats()


@pytest.fixture
def captured_config(tmp_path, monkeypatch):
    r"""`cmd_index` imports `Pipeline` locally
    (`from app.index.pipeline import Pipeline, ...`), so the module
    attribute has to be patched, not anything on `cli` itself - the local
    import re-reads it at call time."""
    _StubPipeline.captured = []
    import app.index.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "Pipeline", _StubPipeline)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    return tmp_path, corpus


def _run_index(tmp_path, corpus, extra_args):
    parser = cli.build_parser()
    args = parser.parse_args([
        "index", str(corpus), "--env", _env_file(tmp_path), "--quiet", "--json",
        *extra_args,
    ])
    cli.cmd_index(args)
    assert _StubPipeline.captured, "Pipeline was never constructed - cmd_index did not reach it"
    return _StubPipeline.captured[-1]


def test_allow_cloud_content_reaches_the_walk_config(captured_config):
    tmp_path, corpus = captured_config
    cloud_dir = tmp_path / "synced"
    cloud_dir.mkdir()

    config = _run_index(tmp_path, corpus, ["--allow-cloud-content", str(cloud_dir)])

    expected = str(cloud_dir).rstrip("\\/").lower()
    assert expected in config.walk.cloud_content_roots


def test_cloud_content_cap_mb_reaches_the_walk_config_in_bytes(captured_config):
    tmp_path, corpus = captured_config

    config = _run_index(tmp_path, corpus, ["--cloud-content-cap-mb", "5"])

    assert config.walk.cloud_content_cap_bytes == 5 * 1024 * 1024


def test_no_cloud_flags_means_nothing_opted_in(captured_config):
    tmp_path, corpus = captured_config

    config = _run_index(tmp_path, corpus, [])

    assert config.walk.cloud_content_roots == frozenset()


def test_the_default_cap_comes_from_settings_when_not_overridden(captured_config):
    tmp_path, corpus = captured_config

    config = _run_index(tmp_path, corpus, [])

    assert config.walk.cloud_content_cap_bytes == 1024 * 1024 * 1024  # the .env default, 1024MB


def test_multiple_allow_cloud_content_flags_are_all_opted_in(captured_config):
    tmp_path, corpus = captured_config
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()

    config = _run_index(tmp_path, corpus, [
        "--allow-cloud-content", str(first), "--allow-cloud-content", str(second),
    ])

    roots = config.walk.cloud_content_roots
    assert str(first).rstrip("\\/").lower() in roots
    assert str(second).rstrip("\\/").lower() in roots
