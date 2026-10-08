"""The words of the "Models Leasha uses" box in Settings, Models & AI.

Layer: L5. Part of the presenter package; imports no Qt.

2026-10-08, the owner: "do all the models needed to run the system ship with
the release or during the install can you put the download buttons to download
all needed models individually and a button for all". One line per model
Leasha needs (`app/core/model_catalogue.py` says which): what it is for, its
name, about how big it is, whether it is here, and a Download button of its
own. One Download all fetches every missing one in turn, and a line above the
list says what is left.

**Nothing here looks at the disk.** Whether a model is here is asked on a
worker by the box; these functions only turn the answers into words.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "NEEDED_MODELS_TITLE", "NEEDED_MODELS_INTRO", "NEEDED_PRESENT", "NEEDED_MISSING",
    "NEEDED_LOOKING", "NEEDED_STARTING", "NEEDED_STOPPING", "NEEDED_STOPPED",
    "NEEDED_DOWNLOAD_TIP", "NEEDED_DOWNLOAD_ALL_TIP", "NEEDED_STOP_TIP",
    "NEEDED_STOP_ALL_TIP", "NEEDED_NO_LIBRARY", "needed_size_words",
    "needed_progress_words", "needed_failed_words", "needed_summary",
    "needed_list_failed", "needed_reason", "needed_failed_tip",
]

NEEDED_MODELS_TITLE = "Models Leasha uses"

NEEDED_MODELS_INTRO = (
    "Every model Leasha needs, and whether it is on this computer. Nothing "
    "downloads unless you press Download or Download all; search and indexing "
    "carry on while it runs.")

#: A line's status, by where it stands.
NEEDED_PRESENT = "On this computer"
NEEDED_MISSING = "Not downloaded"
NEEDED_LOOKING = "Looking..."
NEEDED_STARTING = "Starting to download..."
NEEDED_STOPPING = "Stopping..."
NEEDED_STOPPED = "Stopped - not downloaded yet"

#: In place of a button, for a model whose library is not installed here
#: (faces: insightface). A button that cannot work is not offered.
NEEDED_NO_LIBRARY = ("Cannot be downloaded here - the program library it "
                     "needs is not installed")

NEEDED_DOWNLOAD_TIP = (
    "Downloads this model from the internet, now, once. Nothing goes online "
    "unless you press it; search and indexing carry on while it runs.")
NEEDED_DOWNLOAD_ALL_TIP = (
    "Downloads every model not on this computer yet, one after another. "
    "Nothing goes online unless you press it; Stop ends the whole run.")
NEEDED_STOP_TIP = "Stops downloading now. Press Download to try again later."
NEEDED_STOP_ALL_TIP = ("Stops the whole run now. Models already downloaded "
                       "are kept.")

_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def needed_size_words(approx_mb: Any) -> str:
    """"about 67 MB", "about 1.2 GB", or "" when the size is not known."""
    try:
        mb = int(approx_mb or 0)
    except (TypeError, ValueError):
        return ""
    if mb <= 0:
        return ""
    if mb >= 1024:
        return f"about {mb / 1024:.1f} GB"
    return f"about {mb} MB"


def needed_progress_words(line: Any) -> str:
    """A progress line from the engine as the status shows it.

    A percentage anywhere in the line becomes "Downloading 45%..."; a line
    with none is shown as it came, so somebody still sees something moving.
    """
    text = " ".join(str(line or "").split())
    found = _PERCENT.search(text)
    if found:
        value = min(100, int(float(found.group(1))))
        return f"Downloading {value}%..."
    return text or "Downloading..."


def needed_reason(error: Any) -> str:
    """The plain reason inside whatever a download raised."""
    inner = getattr(error, "error", None)       # AppErrorException carries an AppError
    for source in (inner, error):
        message = str(getattr(source, "message", "") or "").strip()
        if message:
            return message
    text = str(error or "").strip()
    return text or "the reason was not given"


def needed_failed_words(error: Any) -> str:
    """"Did not download - <reason>"."""
    return f"Did not download - {needed_reason(error)}"


def needed_failed_tip(error: Any) -> str:
    """The detail and what to do, for the status's tooltip: the line itself
    keeps to the plain sentence, as the error contract keeps details folded."""
    inner = getattr(error, "error", None) or error
    parts = [str(getattr(inner, name, "") or "").strip() for name in ("details", "suggestion")]
    return "\n\n".join(p for p in parts if p)


def needed_summary(models: Iterable[Any], present: Mapping[str, bool],
                   unavailable: Optional[Iterable[str]] = ()) -> str:
    """"3 of 7 on this computer - 1.9 GB still to download".

    `models` carry `key` and `approx_mb`; `present` says which are here. A
    model that cannot be downloaded on this computer (`unavailable`) counts in
    the total but not in what is still to download.
    """
    models = list(models)
    blocked = set(unavailable or ())
    total = len(models)
    here = sum(1 for m in models if present.get(m.key))
    if total == 0:
        return "No models are listed."
    if here == total:
        return f"All {total} on this computer."
    still = sum(int(getattr(m, "approx_mb", 0) or 0) for m in models
                if not present.get(m.key) and m.key not in blocked)
    head = f"{here} of {total} on this computer"
    if still <= 0:
        return f"{head}."
    size = needed_size_words(still).removeprefix("about ")
    return f"{head} - {size} still to download"


def needed_list_failed(error: Any) -> str:
    """When the list itself could not be read."""
    return f"Could not list the models Leasha uses - {needed_reason(error)}"
