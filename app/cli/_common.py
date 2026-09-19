"""Shared by every subcommand: the exit codes, how an error is printed, and the one
place every command gets its configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import Settings, load_settings
from app.core.errors import AppError
from app.core.logging import logger
from app.core.runlog import current as current_run

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2


def _report(error: AppError, as_json: bool) -> int:
    """Print an AppError to the console in the requested shape."""
    if as_json:
        print(json.dumps(error.model_dump(mode="json"), indent=2))
    else:
        print(error.render(), file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED if error.code == "ERR_NOT_IMPLEMENTED" else EXIT_ERROR


def _load(args: argparse.Namespace) -> Settings:
    env_file = Path(args.env) if getattr(args, "env", None) else None
    settings = load_settings(env_file)

    # **The one place every command gets its configuration**, and therefore the
    # only place the run log can record what the configuration actually was.
    # Writing it from `.env` instead would record the file rather than the
    # values in force, which is the mistake `doctor` made for a week.
    run = current_run()
    if run is not None:
        run.settings(settings)

    # `ocr.py` is a registered extractor reached with a path and nothing else,
    # so `EMBED_DEVICE` has to be pushed to it rather than read by it. Here,
    # because this is the one function every command's settings pass through.
    from app.extract import ocr

    ocr.configure_device(settings.embed_device)
    return settings


#: Where the window keeps "Folders to index". One key, `|`-separated - the same
#: string `shell._save_roots` writes, and the reason this constant exists rather
#: than the literal appearing in two files.
ROOTS_STATE_KEY = "ui:roots"


def _saved_roots(settings: Settings) -> "list[str]":
    """The folders the window is configured to index, or an empty list.

    **Read-only and guarded.** This runs before the pipeline is built, on a
    store that may not exist yet - a first run has no database - and a missing
    setting is the normal case rather than an error. Anything that goes wrong
    here means "no saved folders", which is exactly what a fresh install has.
    """
    if not Path(settings.fts_db).is_file():
        return []
    try:
        from app.storage.sqlite_store import SqliteStore

        with SqliteStore(settings.fts_db) as store:
            raw = store.get_state(ROOTS_STATE_KEY, "") or ""
    except Exception as exc:                     # noqa: BLE001 - see docstring
        logger.bind(component="cli.index").debug(
            "could not read the saved index folders: {}", exc)
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


def _preview(text: str, width: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
