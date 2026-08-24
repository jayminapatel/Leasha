"""Typed configuration, loaded and validated from .env at startup.

Layer: L0

Deliberately built on plain pydantic plus python-dotenv, NOT pydantic-settings:
that is a separate distribution and is not in requirements.txt. Adding a
dependency to save a dozen lines of parsing is not a trade worth making.

Every path is validated when the app starts, not when it is first used. A typo
in .env should stop the app immediately with a message naming the key, rather
than surfacing three minutes into a 100GB index run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import AppErrorException, make_error

__all__ = ["Settings", "load_settings", "find_env_file", "DEFAULT_ENV_NAME"]

DEFAULT_ENV_NAME = ".env"

# Directories the app creates and owns under DATA_PATH.
_INDEX_SUBDIRS = ("vectors", "fts", "cache", "models", "state")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def project_root() -> Path:
    """The project folder: two levels up from this file (app/core/config.py)."""
    return Path(__file__).resolve().parents[2]


def find_env_file(explicit: Optional[Path] = None) -> Path:
    """Locate .env: the explicit path if given, otherwise the project root."""
    if explicit is not None:
        return Path(explicit)
    return project_root() / DEFAULT_ENV_NAME


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file with the stdlib.

    python-dotenv is a dependency and would do this, but a hand-rolled parser
    keeps config loadable even when the venv is half-built - which is exactly
    when a clear configuration error matters most.

    utf-8-sig, because a BOM-prefixed .env is a real hazard on Windows.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _as_bool(key: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise AppErrorException(make_error(
        "ERR_CONFIG_INVALID", "core.config",
        key=key, reason=f"expected true or false, got '{value}'",
    ))


def _as_int(key: str, value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key=key, reason=f"expected a whole number, got '{value}'",
        )) from None


class Settings(BaseModel):
    """Validated application configuration."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # --- paths --------------------------------------------------------------
    data_path: Path
    vector_path: Path
    fts_db: Path
    cache_path: Path
    model_cache: Path
    state_path: Path
    project_path: Path
    log_path: Path

    # --- models -------------------------------------------------------------
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_enabled: bool = True

    # --- optional Ollama ----------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "mistral"

    # --- guards -------------------------------------------------------------
    min_free_gb: int = 5
    required_free_gb: int = 150

    # --- provenance ---------------------------------------------------------
    env_file: Optional[Path] = None

    def describe(self) -> dict[str, Any]:
        """Flat, printable view for `cli stats` and the Settings panel."""
        out: dict[str, Any] = {}
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            out[name] = str(value) if isinstance(value, Path) else value
        return out

    @property
    def index_dirs(self) -> tuple[Path, ...]:
        return tuple(self.data_path / name for name in _INDEX_SUBDIRS)


def _require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key=key, reason="missing or empty",
        ))
    return value


def load_settings(
    env_file: Optional[Path] = None,
    *,
    create_dirs: bool = True,
    check_writable: bool = True,
) -> Settings:
    """Load, validate and return Settings.

    Raises AppErrorException(ERR_CONFIG_MISSING) if there is no .env, and
    AppErrorException(ERR_CONFIG_INVALID) naming the offending key otherwise.

    `create_dirs` makes the index subdirectories if absent, which is the normal
    startup behaviour. `check_writable` proves each one is actually writable
    rather than merely present - a read-only or disconnected drive fails here,
    loudly, instead of part-way through indexing.
    """
    path = find_env_file(env_file)
    if not path.is_file():
        raise AppErrorException(make_error(
            "ERR_CONFIG_MISSING", "core.config", path=str(path),
        ))

    try:
        values = _read_env_file(path)
    except OSError as exc:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key=str(path), reason=f"could not be read ({exc.__class__.__name__})",
            details=str(exc),
        )) from exc

    # Environment variables win over the file, so a run can be overridden
    # without editing anything on disk.
    for key in list(values):
        if key in os.environ:
            values[key] = os.environ[key]

    data_path = Path(_require(values, "DATA_PATH"))
    root = project_root()

    def path_of(key: str, default: Path) -> Path:
        raw = values.get(key, "").strip()
        return Path(raw) if raw else default

    try:
        settings = Settings(
            data_path=data_path,
            vector_path=path_of("VECTOR_PATH", data_path / "vectors"),
            fts_db=path_of("FTS_DB", data_path / "fts" / "knowledge.db"),
            cache_path=path_of("CACHE_PATH", data_path / "cache"),
            model_cache=path_of("MODEL_CACHE", data_path / "models"),
            state_path=path_of("STATE_PATH", data_path / "state"),
            project_path=path_of("PROJECT_PATH", root),
            log_path=path_of("LOG_PATH", root / "logs"),
            embed_model=values.get("EMBED_MODEL") or "BAAI/bge-small-en-v1.5",
            embed_dim=_as_int("EMBED_DIM", values.get("EMBED_DIM", "384")),
            rerank_model=values.get("RERANK_MODEL") or "BAAI/bge-reranker-base",
            rerank_enabled=_as_bool("RERANK_ENABLED", values.get("RERANK_ENABLED", "true")),
            ollama_url=values.get("OLLAMA_URL") or "http://127.0.0.1:11434",
            ollama_model=values.get("OLLAMA_MODEL") or "mistral",
            min_free_gb=_as_int("MIN_FREE_GB", values.get("MIN_FREE_GB", "5")),
            required_free_gb=_as_int("REQUIRED_FREE_GB", values.get("REQUIRED_FREE_GB", "150")),
            env_file=path,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        key = ".".join(str(p) for p in first.get("loc", ())) or "unknown"
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key=key.upper(), reason=first.get("msg", "invalid value"),
            details=str(exc),
        )) from exc

    if settings.embed_dim <= 0:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="EMBED_DIM", reason=f"must be positive, got {settings.embed_dim}",
        ))

    if not settings.ollama_url.startswith(("http://", "https://")):
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="OLLAMA_URL", reason=f"must start with http:// or https://, got '{settings.ollama_url}'",
        ))

    _validate_paths(settings, create_dirs=create_dirs, check_writable=check_writable)
    return settings


def _validate_paths(settings: Settings, *, create_dirs: bool, check_writable: bool) -> None:
    """Prove every directory the app needs exists and can be written to."""
    targets: list[tuple[str, Path]] = [
        ("DATA_PATH", settings.data_path),
        ("VECTOR_PATH", settings.vector_path),
        ("FTS_DB", settings.fts_db.parent),
        ("CACHE_PATH", settings.cache_path),
        ("MODEL_CACHE", settings.model_cache),
        ("STATE_PATH", settings.state_path),
        ("LOG_PATH", settings.log_path),
    ]

    for key, target in targets:
        if not target.exists():
            if not create_dirs:
                raise AppErrorException(make_error(
                    "ERR_CONFIG_INVALID", "core.config",
                    key=key, reason=f"'{target}' does not exist",
                ))
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppErrorException(make_error(
                    "ERR_CONFIG_INVALID", "core.config",
                    key=key,
                    reason=f"'{target}' could not be created ({exc.__class__.__name__})",
                    details=str(exc),
                )) from exc

        if not target.is_dir():
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "core.config",
                key=key, reason=f"'{target}' exists but is not a directory",
            ))

        if check_writable:
            probe = target / ".write_test"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise AppErrorException(make_error(
                    "ERR_CONFIG_INVALID", "core.config",
                    key=key,
                    reason=f"'{target}' is not writable ({exc.__class__.__name__})",
                    details=str(exc),
                )) from exc
