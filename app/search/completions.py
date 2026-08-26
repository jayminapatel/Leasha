r"""The completions sidecar: what a shell can offer without starting Python.

Layer: L4 — written by the indexer, read by the PowerShell tab completer.

**Why a file exists at all.** A Tab press expects an answer in tens of
milliseconds. Starting this application's Python costs hundreds before a single
query runs — `app.cli` alone pulls in the parser, the catalogue and the store —
so a completer that shells out per keystroke is a completer nobody keeps
switched on. The values a shell needs are the same values that were just
computed at the end of an index run, so they are written down once and read as
JSON afterwards.

**Bounded on purpose, twice.** `TOP_PER_SOURCE` caps each list, and the sources
are named here rather than discovered — a sidecar that grows with the corpus
would be a file the shell has to parse on every Tab, which is the cost it
exists to avoid. Measured on the shape below: a few kilobytes.

**Written atomically.** A completer reading a half-written file gets a
`JSONDecodeError` at the exact moment somebody pressed Tab, which reads as the
application being broken rather than as a race. Write beside, rename over.

Nothing here imports Qt, and nothing here decides what a value *means* — the
sources and the bounds are the ones `distinct_values` already applies, so the
dropdown, the REPL and the tab completer offer the same words.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "SIDECAR_NAME",
    "SOURCES",
    "TOP_PER_SOURCE",
    "build_sidecar",
    "write_sidecar",
    "read_sidecar",
    "sidecar_path",
]

_log = logger.bind(component="search.completions")

#: The file, beside the index it describes.
#:
#: Beside the index rather than in the project folder, because the index is
#: what it is a summary of - two indexes on one machine must not share one, and
#: a repair run that rewrites the project folder must not delete it.
SIDECAR_NAME = "completions.json"

#: Values kept per source.
#:
#: Fifty is more than a menu shows and far less than a corpus holds. The point
#: of the list is the *common* answers - the extension somebody wants is nearly
#: always one of the three they have thousands of - and a completer that offers
#: a thousand is one nobody reads.
TOP_PER_SOURCE = 50

#: What the sidecar carries, and the `distinct_values` key each comes from.
#:
#: **Named, not discovered.** A source added to the store does not silently
#: start costing every Tab press; somebody adds it here, having decided the
#: shell wants it.
#:
#: `branch` is deliberately absent. Branches come from git rather than from the
#: index, and answering them would mean walking every checkout at the end of
#: every index run - minutes of subprocesses for a menu. The REPL (4g) answers
#: them in-process, where it costs one call at the moment somebody asks.
SOURCES: tuple[str, ...] = ("ext", "folder", "sender", "recipient", "repo")


def sidecar_path(data_path: Any) -> Path:
    """Where the sidecar for this index lives."""
    return Path(data_path) / SIDECAR_NAME


def build_sidecar(store: Any, *, limit: int = TOP_PER_SOURCE) -> dict[str, Any]:
    """The values worth offering, per source, with their counts.

    **Never raises.** This runs at the end of an index run that may have taken
    days, and a sidecar is a convenience: failing to write one must cost the
    completions and nothing else. A source the store cannot answer is left out
    rather than recorded empty, so the completer can tell "no such source" from
    "nothing indexed yet" and fall back for the first and not the second.
    """
    out: dict[str, Any] = {"version": 1, "sources": {}}
    for source in SOURCES:
        try:
            rows = store.distinct_value_counts(source, limit=limit)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("no {} values for the sidecar: {}", source, exc)
            continue
        if not rows:
            continue
        out["sources"][source] = [
            # Plain types only: this is read by a PowerShell script, and a
            # tuple would arrive as an object nobody wrote a reader for.
            {"value": row.value, "count": int(row.count), "exact": bool(row.exact)}
            for row in rows
        ]
    return out


def write_sidecar(store: Any, data_path: Any, *,
                  limit: int = TOP_PER_SOURCE) -> Optional[Path]:
    """Write the sidecar beside the index. Returns the path, or None.

    Atomic: a temporary file in the same directory, then `os.replace`. A
    completer that reads a half-written file raises at the exact moment
    somebody pressed Tab, which reads as the application being broken.

    Same directory, because `os.replace` across filesystems is not atomic and
    the index may be on another drive from the temp folder.
    """
    target = sidecar_path(data_path)
    try:
        payload = json.dumps(build_sidecar(store, limit=limit), indent=1)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=".completions-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as writer:
                writer.write(payload)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except Exception as exc:                     # noqa: BLE001 - a convenience
        _log.debug("could not write the completions sidecar: {}", exc)
        return None
    _log.debug("wrote {} ({:,} bytes)", target.name, len(payload))
    return target


def read_sidecar(data_path: Any) -> dict[str, list[dict[str, Any]]]:
    """What the sidecar holds, or `{}`.

    `{}` for absent, unreadable, half-written or from a version this build does
    not understand - every one of which means the same thing to a caller: ask
    the store instead.
    """
    try:
        text = sidecar_path(data_path).read_text(encoding="utf-8")
        payload = json.loads(text)
    except Exception:                            # noqa: BLE001 - see docstring
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    sources = payload.get("sources")
    return sources if isinstance(sources, dict) else {}
