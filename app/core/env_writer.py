"""Writing `.env`, so the user never has to.

Layer: L0

Non-negotiable 11: `.env` is written by the application, never by the user.

Three properties this module exists to guarantee:

**Atomic.** A crash part-way through a write must not leave an unparseable
config. Writing a temp file in the same directory and then replacing is the only
way to get that on Windows, where `os.replace` is atomic within a volume.

**Non-destructive.** The file carries comments, blank lines and keys this build
does not know about - a newer version's settings, or something the installer
wrote. A naive rewrite silently deletes all of it, so the parse keeps every line
and edits in place.

**BOM-free UTF-8.** PowerShell 5.1's `Set-Content -Encoding UTF8` writes a byte
order mark, and a BOM on the first key makes it read as `﻿DATA_PATH`, which
matches nothing. `config.py` reads with `utf-8-sig` to tolerate one; this writes
without so it never appears in the first place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app.core.errors import AppErrorException, make_error

__all__ = ["write_env", "apply_values", "render", "pinned_keys"]


def _split(line: str) -> Optional[tuple[str, str]]:
    """`KEY=value` as a pair, or None for comments, blanks and junk."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    return key.strip(), value


def render(existing: str, values: Mapping[str, object]) -> str:
    """Return `existing` with `values` applied, preserving everything else.

    Keys already present are edited where they sit, so a hand-ordered file keeps
    its order and its comments. Keys not present are appended under a marked
    section, so it is obvious which lines the application added.

    **A value of `None` removes the key**, which is the only way to say "use
    whatever the code decides". Without it a key, once written, was pinned for
    ever: `.env` always beats the default in `config.py`, so an improved
    default could never reach anyone whose file mentioned that key. The
    installer wrote `RERANK_MODEL=BAAI/bge-reranker-base` into every install
    and froze all of them on a model measured 9.2x slower than the one the code
    had chosen - and there was no operation, in the UI or anywhere else, that
    could undo it. Removal is that operation.
    """
    lines = existing.splitlines()
    remaining = dict(values)
    out: list[str] = []

    for line in lines:
        pair = _split(line)
        if pair is None:
            out.append(line)
            continue
        key, _ = pair
        if key in remaining:
            value = remaining.pop(key)
            if value is None:
                continue           # drop the line: fall back to the default
            out.append(f"{key}={_format(value)}")
        else:
            out.append(line)

    # Removing a key that is not there is not an error, and must not append it.
    remaining = {key: value for key, value in remaining.items() if value is not None}

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Written by Leasha. Change these in Settings, not here.")
        for key in sorted(remaining):
            out.append(f"{key}={_format(remaining[key])}")

    return "\n".join(out).rstrip("\n") + "\n"


def _format(value: object) -> str:
    """Booleans become `true`/`false`, which is what `config._as_bool` accepts.

    `str(True)` would produce `True`, which that parser rejects - so this is the
    one conversion that must not be left to `str()`.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_env(path: Path, values: Mapping[str, object]) -> Path:
    """Apply `values` to the `.env` at `path`, atomically. Returns the path.

    A value of `None` removes that key, so the default in `config.py` applies
    again. See `render`.

    A missing file is created. Failure raises `ERR_CONFIG_INVALID` naming the
    path, never a bare `OSError` - a settings screen that dies with a traceback
    when the disk is full breaks the error contract.
    """
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError as exc:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.env_writer",
            key=str(path), reason=f"could not be read ({exc.__class__.__name__})",
            details=str(exc),
        )) from exc

    content = render(existing, values)

    # The temp file must share a directory with the target: os.replace is only
    # atomic within a volume, and %TEMP% is routinely on a different one.
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".env.", suffix=".tmp"
        )
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            handle = None          # fdopen owns it now
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        if handle is not None:
            os.close(handle)
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.env_writer",
            key=str(path), reason=f"could not be written ({exc.__class__.__name__})",
            details=str(exc),
            suggestion="Check the project folder is writable and the disk is not full.",
        )) from exc

    return path


def apply_values(path: Path, values: Mapping[str, object]) -> dict[str, str]:
    """Write, then read back and return what is now on disk.

    Reading back is deliberate: it is what makes a round-trip test meaningful,
    and it catches a value that formats to something the parser will not accept.
    """
    write_env(path, values)
    result: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        pair = _split(line)
        if pair is not None:
            result[pair[0]] = pair[1].strip().strip('"').strip("'")
    return result


def pinned_keys(path: Path, known: "Iterable[str] | None" = None) -> list[str]:
    r"""Registry keys this `.env` file pins, in the order they appear.

    **The list a "restore defaults" needs, and it is read from the file rather
    than from the controls.** `.env` always beats the default in `config.py`, so
    what matters is not whether a control *shows* something unusual - it is
    whether the key is written down at all. A key set to exactly the current
    default is still pinned, and still stops a better default from ever
    reaching this machine.

    That is not hypothetical: the installer wrote
    `RERANK_MODEL=BAAI/bge-reranker-base` into every install, the code default
    was later changed to a model measured 9.2x faster, and no machine got it.

    `known` restricts the answer to keys the application recognises - normally
    `settings_registry.keys()` - so a line somebody else's installer added is
    reported as pinned by nothing this window offers to remove. Never raises:
    an unreadable file pins nothing, which is the safe answer.
    """
    wanted = frozenset(known) if known is not None else None
    found: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return found
    for line in text.splitlines():
        pair = _split(line)
        if pair is None:
            continue
        key = pair[0]
        if key in found:
            continue
        if wanted is None or key in wanted:
            found.append(key)
    return found
