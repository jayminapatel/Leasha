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
import re
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import AppErrorException, make_error

__all__ = ["Settings", "load_settings", "find_env_file", "log_dir_for",
           "DEFAULT_ENV_NAME"]

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


def log_dir_for(env_file: Optional[Path] = None) -> Path:
    r"""Where logs go, resolved without validating anything else.

    `load_settings` is the authority, but it refuses a bad `.env` - and a run
    that failed at configuration is precisely one worth having a log of. This
    answers the single question "which folder", guesses the default when it
    cannot tell, and never raises.

    **It also has to refuse a foreign path, because it runs *before* the
    guard that does.** `load_settings` raises `ERR_CONFIG_INVALID` on a Windows
    `LOG_PATH` read off Windows - but the run log is opened first, by design,
    so that the failure itself gets logged. So this bypassed the guard entirely
    and created a directory literally named `D:\SearchProject\logs` in whatever
    folder happened to be current, on every single command.

    That is the third time this family of mistake has been found here, and it
    was caught by `test_no_stray_paths` - an outcome tripwire written after the
    second, precisely because guarding one door is never enough.
    """
    values: dict[str, str] = {}
    try:
        path = find_env_file(env_file)
        if path.is_file():
            values = _read_env_file(path)
    except Exception:  # noqa: BLE001 - a fallback that can fail is not one
        values = {}
    raw = (os.environ.get("LOG_PATH") or values.get("LOG_PATH") or "").strip()
    if not raw:
        return project_root() / "logs"
    if sys.platform != "win32" and _WINDOWS_PATH.match(raw):
        # The default, not an exception: this is the fallback path, and a
        # fallback that raises is not one. `load_settings` reports it properly a
        # moment later, with the key named and a fix attached.
        return project_root() / "logs"
    return Path(raw)


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
    # --- search: what search may do on your behalf --------------------------
    #
    # **Each can only switch a behaviour off.** On means "follow the surface's
    # own contract" - see `app/search/policy.py` for the table. That is what
    # keeps spelling correction away from identifiers on the Code tab without
    # needing twenty-four settings instead of six.
    search_fix_spelling: str = "auto"
    search_relax_on_empty: bool = True
    search_auto_chips: bool = True
    search_recency_blend: bool = True
    search_version_folding: bool = True
    search_plain_words: bool = True

    #: `auto | cpu | gpu` - which processor runs the ONNX models. One value for
    #: the embedder, the reranker and OCR, because a machine where two of the
    #: three used the graphics card is one nobody could reason about.
    #:
    #: **Not destructive.** The same model gives the same vectors on either
    #: processor to within floating-point noise, so unlike `EMBED_MODEL` this
    #: does not invalidate an index - which is why it is a plain control.
    embed_device: str = "auto"
    #: **Changed on a measurement, not a preference.** `rerank-bench` on the
    #: owner's machine, 30 candidates windowed to 600 characters, median of
    #: three passes:
    #:
    #:     Xenova/ms-marco-MiniLM-L-6-v2    0.08 GB    0.66s    22ms/passage
    #:     jinaai/jina-reranker-v1-tiny-en  0.13 GB    0.81s    27ms/passage
    #:     Xenova/ms-marco-MiniLM-L-12-v2   0.12 GB    1.98s    66ms/passage
    #:     BAAI/bge-reranker-base           1.04 GB    6.08s   203ms/passage
    #:
    #: bge-reranker-base was the default and was 93% of a nine-second search.
    #: It is the better *ranker* - that is what the extra 960MB buys - but an
    #: ordering nobody waits for is worth nothing, and 6 seconds is nobody.
    #:
    #: **And the quality was then measured, not assumed.** Both models, full
    #: pipeline, recall at 1 over the built-in corpus: 80% overall, 88% topic,
    #: 75% constrained, the same four misses. Identical ordering at rank 1 for
    #: a ninth of the time.
    #:
    #: Recall at 1 over 20 questions cannot see reordering below the top result,
    #: so this is not proof that the two rank identically - only that the swap
    #: costs nothing visible on the measurement available. `RERANK_MODEL` in
    #: `.env` puts the old one back, and `--rerank-model` compares without one.
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_enabled: bool = True
    #: Candidates scored, and characters of each shown to the cross-encoder.
    #: Both multiply into the rerank time, which was 93% of one 9-second search.
    rerank_top_n: int = 30
    rerank_window_chars: int = 600

    # --- optional Ollama ----------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "mistral"

    # --- indexing: how hard this is allowed to work ------------------------
    #
    # Every one of these is a ceiling, not a target. The defaults assume the
    # machine belongs to somebody who is using it. See app/index/resources.py.
    #
    #: 0 -> half the cores, capped at four. Not `cores - 1`: on an 8-core laptop
    #: that hands seven to a background job and leaves one for the person.
    index_workers: int = 0
    #: Resident memory ceiling in MB. Above it the run pauses and drains rather
    #: than aborting - a pause costs minutes and loses nothing.
    index_memory_mb: int = 4000
    #: Pause while system-wide CPU is above this. 0 disables the check.
    index_cpu_percent: int = 80
    #: Pause on battery, resume on mains.
    index_pause_on_battery: bool = True
    #: Run below normal CPU and I/O priority.
    index_low_priority: bool = True

    # --- indexing: how fast, and how it is decided -------------------------
    #
    # `defaults | auto | manual`. **Which of the three is in force decides
    # whether the numbers below are read at all**, and that is the point of
    # having a mode rather than a scatter of sentinels: in Defaults and
    # Auto-tune the envelope's answer wins, and a value somebody typed once in
    # Manual stays stored but inert. Experimenting is then reversible, which is
    # what makes people willing to experiment.
    index_tuning_mode: str = "defaults"
    #: Intra-op threads for one ONNX session. 0 = decided per machine.
    onnx_intra_op_threads: int = 0
    #: Chunks per embedding call. 0 = decided per machine's memory. When set,
    #: it overrides `embedder.EMBED_BATCH`.
    embed_batch: int = 0
    #: Prefer the quantised model file: smaller and faster on a processor, no
    #: gain on a graphics card, a small cost in ranking quality.
    embed_quantised: bool = False
    #: Words searchable as soon as a file is read, meaning catching up behind.
    index_two_phase: bool = True
    #: `auto | on | off`. Build the word index once at the end of a big run.
    index_bulk_fts: str = "auto"
    #: Send each distinct passage to the model once and reuse the result.
    embed_dedup: bool = True
    #: `with-run | after-run | manual`. When the images pass happens.
    index_ocr_pass: str = "with-run"

    # --- indexing: when it runs --------------------------------------------
    #: manual | startup | interval | daily
    index_schedule: str = "manual"
    #: Hours between runs when `index_schedule` is "interval".
    index_interval_hours: int = 6
    #: Local time of day, HH:MM, when `index_schedule` is "daily".
    index_daily_at: str = "02:00"
    #: Record every file by name, even the ones nothing can read.
    #:
    #: On by default, because *"where is that file"* is the most common
    #: question anybody asks a search tool and a `.zip` used to produce no row
    #: at all. Off for somebody indexing a media drive who does not want two
    #: million video files in their index.
    index_name_only: bool = True
    #: Read the contents of files inside `.zip` archives.
    #:
    #: **On, with a ceiling** - `WORKORDER-zip-archives.md` §4.6 is explicit
    #: that "off by default is wrong and on by default is dangerous", so this
    #: is a switch beside a number rather than either on its own.
    archive_read_inside: bool = True
    #: The largest archive whose members are read, in MB. A 40GB backup zip is
    #: recorded by name with a message saying why, rather than read.
    archive_max_mb: int = 100
    #: Pages of one scanned PDF to read with OCR. 0 is off.
    #:
    #: **A budget rather than a switch**, and the arithmetic is the argument:
    #: at ~3.6 seconds a page, twenty pages is about a minute a document, and
    #: the first twenty pages of a manual are its title, contents and
    #: introduction - most of what makes it findable. All-or-nothing is the
    #: sixty-hour column.
    pdf_ocr_pages: int = 0
    #: both | text | images. Which pass an index run is. See `pipeline.
    #: OCR_MODES`: OCR costs about 3.6 seconds a page, so at a terabyte a
    #: single pass means nothing is searchable until everything is.
    index_ocr_mode: str = "both"
    #: How long a folder marked as an archive is trusted without evidence.
    #: 0 means "only when the folder itself changes, or when asked".
    #: See `app/index/archives.py` - the mtime tripwire is what actually
    #: catches change; this is the backstop for a change it cannot see.
    archive_recheck_days: int = 30

    # --- guards -------------------------------------------------------------
    min_free_gb: int = 5
    required_free_gb: int = 300

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


SETTING_KEYS: tuple[str, ...] = (
    "DATA_PATH",
    "VECTOR_PATH",
    "FTS_DB",
    "CACHE_PATH",
    "MODEL_CACHE",
    "STATE_PATH",
    "PROJECT_PATH",
    "LOG_PATH",
    "EMBED_MODEL",
    "EMBED_DIM",
    "EMBED_DEVICE",
    "RERANK_MODEL",
    "RERANK_ENABLED",
    "RERANK_TOP_N",
    "RERANK_WINDOW_CHARS",
    "SEARCH_FIX_SPELLING",
    "SEARCH_RELAX_ON_EMPTY",
    "SEARCH_AUTO_CHIPS",
    "SEARCH_RECENCY_BLEND",
    "SEARCH_VERSION_FOLDING",
    "SEARCH_PLAIN_WORDS",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "INDEX_TUNING_MODE",
    "INDEX_WORKERS",
    "ONNX_INTRA_OP_THREADS",
    "EMBED_BATCH",
    "EMBED_QUANTISED",
    "INDEX_TWO_PHASE",
    "INDEX_BULK_FTS",
    "EMBED_DEDUP",
    "INDEX_OCR_PASS",
    "INDEX_MEMORY_MB",
    "INDEX_CPU_PERCENT",
    "INDEX_PAUSE_ON_BATTERY",
    "INDEX_LOW_PRIORITY",
    "INDEX_SCHEDULE",
    "INDEX_INTERVAL_HOURS",
    "INDEX_DAILY_AT",
    "INDEX_OCR_MODE",
    "INDEX_NAME_ONLY",
    "ARCHIVE_RECHECK_DAYS",
    "ARCHIVE_READ_INSIDE",
    "ARCHIVE_MAX_MB",
    "PDF_OCR_PAGES",
    "MIN_FREE_GB",
    "REQUIRED_FREE_GB",
)
"""Every key `load_settings` reads, whether or not .env mentions it.

A test keeps this in step with the function - drift here is silent, and the
symptom is an environment variable that appears to be ignored.
"""


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
    # without editing anything on disk. Iterate the settings Leasha knows
    # about, not the keys the file happens to contain: overriding only what
    # was already written down means `set INDEX_WORKERS=4` does nothing unless
    # INDEX_WORKERS is already in .env, which is precisely the case where
    # somebody reaches for an environment variable.
    for key in SETTING_KEYS:
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
            embed_device=(values.get("EMBED_DEVICE") or "auto").strip().lower(),
            rerank_model=values.get("RERANK_MODEL") or "Xenova/ms-marco-MiniLM-L-6-v2",
            rerank_enabled=_as_bool("RERANK_ENABLED", values.get("RERANK_ENABLED", "true")),
            rerank_top_n=_as_int(
                "RERANK_TOP_N", values.get("RERANK_TOP_N") or "30"),
            rerank_window_chars=_as_int(
                "RERANK_WINDOW_CHARS", values.get("RERANK_WINDOW_CHARS") or "600"),
            search_fix_spelling=(
                values.get("SEARCH_FIX_SPELLING") or "auto").strip().lower(),
            search_relax_on_empty=_as_bool(
                "SEARCH_RELAX_ON_EMPTY", values.get("SEARCH_RELAX_ON_EMPTY", "true")),
            search_auto_chips=_as_bool(
                "SEARCH_AUTO_CHIPS", values.get("SEARCH_AUTO_CHIPS", "true")),
            search_recency_blend=_as_bool(
                "SEARCH_RECENCY_BLEND", values.get("SEARCH_RECENCY_BLEND", "true")),
            search_version_folding=_as_bool(
                "SEARCH_VERSION_FOLDING", values.get("SEARCH_VERSION_FOLDING", "true")),
            search_plain_words=_as_bool(
                "SEARCH_PLAIN_WORDS", values.get("SEARCH_PLAIN_WORDS", "true")),
            ollama_url=values.get("OLLAMA_URL") or "http://127.0.0.1:11434",
            ollama_model=values.get("OLLAMA_MODEL") or "mistral",
            index_tuning_mode=(
                values.get("INDEX_TUNING_MODE") or "defaults").strip().lower(),
            index_workers=_as_int("INDEX_WORKERS", values.get("INDEX_WORKERS", "0")),
            onnx_intra_op_threads=_as_int(
                "ONNX_INTRA_OP_THREADS", values.get("ONNX_INTRA_OP_THREADS", "0")),
            embed_batch=_as_int("EMBED_BATCH", values.get("EMBED_BATCH", "0")),
            embed_quantised=_as_bool(
                "EMBED_QUANTISED", values.get("EMBED_QUANTISED", "false")),
            index_two_phase=_as_bool(
                "INDEX_TWO_PHASE", values.get("INDEX_TWO_PHASE", "true")),
            index_bulk_fts=(values.get("INDEX_BULK_FTS") or "auto").strip().lower(),
            embed_dedup=_as_bool("EMBED_DEDUP", values.get("EMBED_DEDUP", "true")),
            index_ocr_pass=(
                values.get("INDEX_OCR_PASS") or "with-run").strip().lower(),
            index_memory_mb=_as_int("INDEX_MEMORY_MB", values.get("INDEX_MEMORY_MB", "4000")),
            index_cpu_percent=_as_int("INDEX_CPU_PERCENT", values.get("INDEX_CPU_PERCENT", "80")),
            index_pause_on_battery=_as_bool(
                "INDEX_PAUSE_ON_BATTERY", values.get("INDEX_PAUSE_ON_BATTERY", "true")),
            index_low_priority=_as_bool(
                "INDEX_LOW_PRIORITY", values.get("INDEX_LOW_PRIORITY", "true")),
            index_schedule=(values.get("INDEX_SCHEDULE") or "manual").strip().lower(),
            index_interval_hours=_as_int(
                "INDEX_INTERVAL_HOURS", values.get("INDEX_INTERVAL_HOURS", "6")),
            index_daily_at=(values.get("INDEX_DAILY_AT") or "02:00").strip(),
            index_ocr_mode=(values.get("INDEX_OCR_MODE") or "both").strip().lower(),
            index_name_only=_as_bool(
                "INDEX_NAME_ONLY", values.get("INDEX_NAME_ONLY", "true")),
            archive_recheck_days=_as_int(
                "ARCHIVE_RECHECK_DAYS", values.get("ARCHIVE_RECHECK_DAYS", "30")),
            archive_read_inside=_as_bool(
                "ARCHIVE_READ_INSIDE", values.get("ARCHIVE_READ_INSIDE", "true")),
            archive_max_mb=_as_int(
                "ARCHIVE_MAX_MB", values.get("ARCHIVE_MAX_MB", "100")),
            pdf_ocr_pages=_as_int(
                "PDF_OCR_PAGES", values.get("PDF_OCR_PAGES", "0")),
            min_free_gb=_as_int("MIN_FREE_GB", values.get("MIN_FREE_GB", "5")),
            required_free_gb=_as_int("REQUIRED_FREE_GB", values.get("REQUIRED_FREE_GB", "300")),
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

    # **Checked here rather than shrugged off at load.** `backends.choose`
    # treats an unrecognised device as `auto`, which is the right behaviour for
    # a value already in flight; it is the wrong behaviour for a typed setting,
    # where silently ignoring `EMBED_DEVICE=gpu ` with a trailing character
    # would leave somebody certain they had switched something on.
    #
    # The tuple is written out rather than imported from `app.index.backends`:
    # L0 does not import L3, and the registry's `choices` are tested against
    # this list, so a third spelling cannot appear in only one of them.
    if settings.embed_device not in ("auto", "cpu", "gpu"):
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="EMBED_DEVICE",
            reason=f"must be auto, cpu or gpu, got {settings.embed_device!r}",
        ))

    # The other closed sets, checked the same way and for the same reason: a
    # typed value that silently falls back to the default leaves somebody
    # certain they switched something on. The tuples are written out rather
    # than imported from the registry so this stays a self-contained L0 check;
    # `test_settings_registry` holds the two lists to each other.
    for key, value, allowed in (
        ("INDEX_TUNING_MODE", settings.index_tuning_mode,
         ("defaults", "auto", "manual")),
        ("INDEX_BULK_FTS", settings.index_bulk_fts, ("auto", "on", "off")),
        ("INDEX_OCR_PASS", settings.index_ocr_pass,
         ("with-run", "after-run", "manual")),
        ("SEARCH_FIX_SPELLING", settings.search_fix_spelling,
         ("auto", "suggest", "off")),
    ):
        if value not in allowed:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "core.config", key=key,
                reason=f"must be one of {', '.join(allowed)}, got {value!r}",
            ))

    if not settings.ollama_url.startswith(("http://", "https://")):
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="OLLAMA_URL", reason=f"must start with http:// or https://, got '{settings.ollama_url}'",
        ))

    _validate_indexing(settings)
    _validate_paths(settings, create_dirs=create_dirs, check_writable=check_writable)
    return settings


SCHEDULES = ("manual", "startup", "interval", "daily")


def _validate_indexing(settings: Settings) -> None:
    """Reject an unusable indexing configuration at load, naming the key.

    Every message here says what to type. A setting that is silently corrected
    to a default is worse than one that fails: the person believes the machine
    is protected by a 500MB memory cap that was quietly ignored.
    """
    if settings.index_schedule not in SCHEDULES:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_SCHEDULE",
            reason=f"'{settings.index_schedule}' is not one of {', '.join(SCHEDULES)}",
            suggestion=(
                "manual  - only when you ask\n"
                "startup - once, shortly after the app opens\n"
                "interval - every INDEX_INTERVAL_HOURS hours\n"
                "daily   - once a day at INDEX_DAILY_AT"
            ),
        ))

    if settings.index_workers < 0:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_WORKERS",
            reason=f"cannot be negative, got {settings.index_workers}",
            suggestion="Use 0 to let the app choose (half your cores, capped at four).",
        ))

    # 256MB is below what the ONNX runtime alone needs, so a cap under it means
    # an indexer that pauses immediately and never does anything - a hang, from
    # the outside, with no error.
    if settings.index_memory_mb < 256:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_MEMORY_MB",
            reason=f"{settings.index_memory_mb}MB is below the 256MB floor",
            suggestion=(
                "The embedding model alone needs more than that, so the indexer "
                "would pause immediately and never resume. 4000 is the default; "
                "800 is about as low as is useful."
            ),
        ))

    if not 0 <= settings.index_cpu_percent <= 100:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_CPU_PERCENT",
            reason=f"must be between 0 and 100, got {settings.index_cpu_percent}",
            suggestion="0 disables the check entirely. 80 is the default.",
        ))

    if settings.index_interval_hours < 1:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_INTERVAL_HOURS",
            reason=f"must be at least 1, got {settings.index_interval_hours}",
        ))

    if parse_daily_at(settings.index_daily_at) is None:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.config",
            key="INDEX_DAILY_AT",
            reason=f"'{settings.index_daily_at}' is not a time of day",
            suggestion="Use 24-hour HH:MM, for example 02:00 or 18:30.",
        ))


def parse_daily_at(value: str) -> Optional[tuple[int, int]]:
    """`"02:00"` -> `(2, 0)`, or None if it is not a valid time of day.

    Returned rather than raised so both the validator and the scheduler can use
    it, and so the settings panel can grey out a Save button without catching
    an exception to decide.
    """
    text = (value or "").strip()
    if ":" not in text:
        return None
    hour, _, minute = text.partition(":")
    try:
        hours, minutes = int(hour), int(minute)
    except ValueError:
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return (hours, minutes)
    return None



#: A Windows path is `C:\...` or `\\server\share`. On Windows these are the
#: only paths there are; anywhere else they are meaningless - and, because a
#: backslash is a perfectly legal filename character on Linux and macOS,
#: `Path("D:\\Data").mkdir(parents=True)` cheerfully creates a *directory
#: literally named* `D:\Data` in the working folder.
#:
#: That is exactly what happened: running the CLI from a Linux sandbox against a
#: Windows `.env` littered the project root with folders called
#: `D:\KnowledgeGraphData`, `D:\KnowledgeGraphData\cache` and so on. Nothing
#: raised, nothing warned, and the index appeared to be configured correctly
#: while writing somewhere else entirely.
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _refuse_foreign_path(key: str, target: Path) -> None:
    """Refuse a Windows path on a platform that has no idea what it means.

    Better to fail at startup with a sentence than to silently create a folder
    with a colon and backslashes in its name and index into it.
    """
    if sys.platform == "win32":
        return
    if not _WINDOWS_PATH.match(str(target)):
        return
    raise AppErrorException(make_error(
        "ERR_CONFIG_INVALID", "core.config",
        key=key,
        reason=f"'{target}' is a Windows path and this is {sys.platform}",
        suggestion=(
            "Point this at a path for this platform, or run on Windows. "
            "Creating it here would make a directory whose *name* contains a "
            "drive letter and backslashes, which is not what anybody meant."
        ),
    ))


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
        _refuse_foreign_path(key, target)
        if not target.exists():
            if not create_dirs:
                # **A missing location and a wrong one need different fixes**,
                # and this fired for both with one sentence. An empty value
                # means the line is *gone* from `.env` - which is what a
                # "restore defaults" used to do - and no amount of checking the
                # path helps, because there is no path to check.
                #
                # Reported as *"now the application does not even start says
                # some path is missing"*, and the message it printed named the
                # key, said "'' does not exist", and stopped there.
                blank = not str(target).strip() or str(target) == "."
                raise AppErrorException(make_error(
                    "ERR_CONFIG_INVALID", "core.config",
                    key=key,
                    reason=(
                        f"{key} is not set in .env, so there is nothing to open"
                        if blank else f"'{target}' does not exist"
                    ),
                    details=(
                        f"Add a line to .env naming the folder, for example:\n"
                        f"    {key}=D:\\Leasha\\Data\n\n"
                        f"Every location key must be present: DATA_PATH, "
                        f"VECTOR_PATH, FTS_DB, CACHE_PATH, MODEL_CACHE, "
                        f"STATE_PATH, PROJECT_PATH and LOG_PATH. If several are "
                        f"missing, re-running install.ps1 rewrites them all, "
                        f"and it does not touch an index that already exists."
                        if blank else
                        f"Create the folder, or point {key} at where it "
                        f"actually is. Moving an index is a flow in Settings; "
                        f"editing this line by hand only changes where the "
                        f"application looks."
                    ),
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
