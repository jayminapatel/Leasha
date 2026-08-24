"""Layer 0: configuration is validated at startup, not at first use.

A typo in .env must stop the app immediately with a message naming the key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings, load_settings
from app.core.errors import AppErrorException


def test_valid_env_loads(temp_env: Path) -> None:
    settings = load_settings(temp_env)
    assert isinstance(settings, Settings)
    assert settings.embed_dim == 384
    assert settings.rerank_enabled is True
    assert settings.env_file == temp_env


def test_index_subdirectories_are_created(temp_env: Path) -> None:
    settings = load_settings(temp_env)
    for directory in (settings.vector_path, settings.cache_path,
                      settings.model_cache, settings.state_path):
        assert directory.is_dir(), f"{directory} should have been created"
    assert settings.fts_db.parent.is_dir()


def test_missing_env_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(AppErrorException) as caught:
        load_settings(tmp_path / "nope.env")
    err = caught.value.error
    assert err.code == "ERR_CONFIG_MISSING"
    assert "nope.env" in err.message
    assert err.suggestion


def test_missing_data_path_names_the_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EMBED_DIM=384\n", encoding="utf-8")
    with pytest.raises(AppErrorException) as caught:
        load_settings(env)
    err = caught.value.error
    assert err.code == "ERR_CONFIG_INVALID"
    assert "DATA_PATH" in err.message


@pytest.mark.parametrize(
    "line,key",
    [
        ("EMBED_DIM=not-a-number", "EMBED_DIM"),
        ("RERANK_ENABLED=perhaps", "RERANK_ENABLED"),
        ("MIN_FREE_GB=lots", "MIN_FREE_GB"),
        ("OLLAMA_URL=127.0.0.1:11434", "OLLAMA_URL"),
    ],
)
def test_bad_value_names_the_offending_key(temp_env: Path, line: str, key: str) -> None:
    """This is the acceptance criterion: a readable AppError, not a traceback."""
    original = temp_env.read_text(encoding="utf-8")
    name = line.split("=")[0]
    rewritten = "\n".join(
        line if existing.startswith(name + "=") else existing
        for existing in original.splitlines()
    )
    temp_env.write_text(rewritten, encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        load_settings(temp_env)

    err = caught.value.error
    assert err.code == "ERR_CONFIG_INVALID"
    assert key in err.message, f"the message should name {key}: {err.message}"
    assert err.suggestion


def test_zero_embed_dim_rejected(temp_env: Path) -> None:
    text = temp_env.read_text(encoding="utf-8").replace("EMBED_DIM=384", "EMBED_DIM=0")
    temp_env.write_text(text, encoding="utf-8")
    with pytest.raises(AppErrorException) as caught:
        load_settings(temp_env)
    assert "EMBED_DIM" in caught.value.error.message


def test_uncreatable_data_path_is_reported(temp_env: Path, tmp_path: Path) -> None:
    """A path that cannot be created fails at startup, naming the key."""
    blocker = tmp_path / "a_file_not_a_directory"
    blocker.write_text("x", encoding="utf-8")

    text = temp_env.read_text(encoding="utf-8")
    text = "\n".join(
        f"DATA_PATH={blocker.as_posix()}" if line.startswith("DATA_PATH=") else line
        for line in text.splitlines()
    )
    temp_env.write_text(text, encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        load_settings(temp_env)
    err = caught.value.error
    assert err.code == "ERR_CONFIG_INVALID"
    assert "DATA_PATH" in err.message


def test_env_file_with_bom_is_parsed(tmp_path: Path) -> None:
    """A BOM-prefixed .env is a real Windows hazard; utf-8-sig handles it."""
    data = tmp_path / "data"
    data.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={data.as_posix()}\n", encoding="utf-8-sig")
    settings = load_settings(env)
    assert settings.data_path == data


def test_environment_variable_overrides_file(temp_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMBED_MODEL", "override/model")
    settings = load_settings(temp_env)
    assert settings.embed_model == "override/model"


def test_describe_is_flat_and_printable(temp_env: Path) -> None:
    described = load_settings(temp_env).describe()
    assert all(isinstance(v, (str, int, bool)) for v in described.values())
    assert "data_path" in described


def test_settings_are_immutable(temp_env: Path) -> None:
    """Configuration must not drift at runtime."""
    settings = load_settings(temp_env)
    with pytest.raises(Exception):
        settings.embed_dim = 512  # type: ignore[misc]
