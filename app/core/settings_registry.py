"""The one place a user-facing setting is declared.

Layer: L0

Non-negotiable 11 says every tunable has a UI or is not tunable. A rule nobody
can check is a rule that decays - which is exactly the finding of
`docs/REVIEW-2026-08-25.md`, where a documented search cache turned out never to
have been wired up. So the rule is enforced the way doc versions and handoff
currency already are: by a registry plus tests over it.

This module is **declarative only**. It reads nothing, writes nothing, and
imports nothing from the rest of the application, so the tests over it run in
milliseconds and cannot be broken by unrelated work.

Three tests give the rule teeth (`tests/unit/test_settings_registry.py`):

  * every key `config.py` reads appears here - a new setting without a control
    fails the suite
  * every entry names a UI surface that exists
  * every entry round-trips through the writer unchanged

`app/core/env_writer.py` is the other half: the application writes `.env`, the
user never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "Setting",
    "SETTINGS",
    "GROUPS",
    "SURFACES",
    "by_key",
    "by_group",
    "keys",
    "needs_restart",
]

#: Where a setting is shown. A registry entry naming a surface not listed here
#: is a typo, and the test says so rather than the control quietly not existing.
SURFACES = (
    "settings.search",
    "settings.indexing",
    "settings.reading",
    "settings.models",
    "settings.storage",
)

#: Display order of the groups in Settings.
GROUPS = ("Search", "Indexing", "Reading", "Models", "Storage")


@dataclass(frozen=True, slots=True)
class Setting:
    """One user-facing setting.

    `key` is the `.env` key, which is also the storage format - there is no
    second name to keep in step.

    `kind` drives the control the UI builds: bool -> checkbox, int -> spin box,
    choice -> combo, text -> line edit, path -> a flow (see `destructive`).

    `destructive` marks a setting that changes what the index *is* rather than
    how it behaves. Those are never plain fields: they state the cost and
    confirm, because silently repointing at an empty index or invalidating four
    million vectors is not something a text box should be able to do.
    """

    key: str
    label: str
    kind: str                       # bool | int | choice | text | path
    default: Any
    group: str
    surface: str
    help: str = ""
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    restart: bool = False
    destructive: bool = False
    #: Set when a value is deliberately not a plain control, with the reason.
    #: The UI is expected to render the named flow instead.
    flow: str = ""


SETTINGS: tuple[Setting, ...] = (
    # --- Search ------------------------------------------------------------
    Setting(
        key="RERANK_ENABLED", label="Rerank results", kind="bool", default=True,
        group="Search", surface="settings.search",
        help="Slower and more precise. Reranking runs a second model over the "
             "top results; switch it off if search feels sluggish.",
    ),
    Setting(
        key="RERANK_TOP_N", label="Results to rerank", kind="int", default=30,
        group="Search", surface="settings.search", minimum=5, maximum=100,
        unit="results",
        help="How many results the reranker looks at. More is slower.",
    ),
    Setting(
        key="RERANK_WINDOW_CHARS", label="Text per result when reranking",
        kind="int", default=600, group="Search", surface="settings.search",
        minimum=200, maximum=2000, unit="characters",
        help="How much of each result the reranker reads.",
    ),

    # --- Indexing ----------------------------------------------------------
    Setting(
        key="INDEX_WORKERS", label="Indexing workers", kind="int", default=0,
        group="Indexing", surface="settings.indexing", minimum=0, maximum=32,
        unit="threads",
        help="0 chooses a sensible number for this machine. Raise it only if "
             "indexing is slow and the machine is otherwise idle.",
    ),
    Setting(
        key="INDEX_MEMORY_MB", label="Memory ceiling", kind="int", default=1500,
        group="Indexing", surface="settings.indexing", minimum=256, maximum=16384,
        unit="MB",
        help="Indexing pauses rather than exceeding this.",
    ),
    Setting(
        key="INDEX_CPU_PERCENT", label="CPU ceiling", kind="int", default=80,
        group="Indexing", surface="settings.indexing", minimum=10, maximum=100,
        unit="%",
        help="Lower this to keep the machine responsive while indexing runs.",
    ),
    Setting(
        key="INDEX_LOW_PRIORITY", label="Run at low priority", kind="bool",
        default=True, group="Indexing", surface="settings.indexing",
        help="Lets everything else on the machine go first.",
    ),
    Setting(
        key="INDEX_PAUSE_ON_BATTERY", label="Pause on battery", kind="bool",
        default=True, group="Indexing", surface="settings.indexing",
        help="Indexing is expensive; on a laptop this stops it draining the battery.",
    ),
    Setting(
        key="INDEX_SCHEDULE", label="When to index", kind="choice",
        default="manual", group="Indexing", surface="settings.indexing",
        choices=("manual", "interval", "daily"),
        help="Manual means it only runs when you ask.",
    ),
    Setting(
        key="INDEX_INTERVAL_HOURS", label="Index every", kind="int", default=6,
        group="Indexing", surface="settings.indexing", minimum=1, maximum=168,
        unit="hours",
        help="Used when the schedule is set to interval.",
    ),
    Setting(
        key="INDEX_DAILY_AT", label="Index daily at", kind="text",
        default="02:00", group="Indexing", surface="settings.indexing",
        help="24-hour time. Used when the schedule is set to daily.",
    ),
    Setting(
        key="MIN_FREE_GB", label="Stop if free space drops below", kind="int",
        default=5, group="Indexing", surface="settings.indexing",
        minimum=1, maximum=500, unit="GB",
        help="Indexing pauses rather than filling the disk. Progress is kept.",
    ),

    # --- Models ------------------------------------------------------------
    Setting(
        key="OLLAMA_URL", label="Ollama address", kind="text",
        default="http://127.0.0.1:11434", group="Models",
        surface="settings.models",
        help="Only used to interpret what you type. Search never calls it, so "
             "leaving it unreachable costs nothing but the Interpret button.",
    ),
    Setting(
        key="OLLAMA_MODEL", label="Local model", kind="text", default="mistral",
        group="Models", surface="settings.models",
        help="Any model you have pulled in Ollama.",
    ),
    Setting(
        key="RERANK_MODEL", label="Rerank model", kind="text",
        default="Xenova/ms-marco-MiniLM-L-6-v2", group="Models",
        surface="settings.models", restart=True,
        help="Changing this downloads a new model on next start.",
    ),
    # Both of the following change what the index *is*, not how it behaves.
    Setting(
        key="EMBED_MODEL", label="Meaning model", kind="text",
        default="BAAI/bge-small-en-v1.5", group="Models",
        surface="settings.models", restart=True, destructive=True,
        flow="rebuild-vectors",
        help="Changing this invalidates every vector in the index and requires "
             "re-embedding everything. The flow states the cost before it starts.",
    ),
    Setting(
        key="EMBED_DIM", label="Meaning model dimensions", kind="int",
        default=384, group="Models", surface="settings.models",
        minimum=64, maximum=4096, unit="dimensions", restart=True,
        destructive=True, flow="rebuild-vectors",
        help="Set by the meaning model. Changing it by hand cannot make an "
             "existing index work; it only makes the mismatch louder.",
    ),

    # --- Storage -----------------------------------------------------------
    Setting(
        key="DATA_PATH", label="Index location", kind="path", default="",
        group="Storage", surface="settings.storage", restart=True,
        destructive=True, flow="move-index",
        help="Where the index lives. Typing a new path here would not move an "
             "index - it would point at a different, probably empty one. The "
             "flow offers move, use existing, or start new.",
    ),
    Setting(
        key="REQUIRED_FREE_GB", label="Free space needed to index", kind="int",
        default=150, group="Storage", surface="settings.storage",
        minimum=1, maximum=10000, unit="GB",
        help="Checked before a run starts, on the index drive.",
    ),
)


def keys() -> frozenset[str]:
    """Every `.env` key with a control."""
    return frozenset(setting.key for setting in SETTINGS)


def by_key(key: str) -> Optional[Setting]:
    for setting in SETTINGS:
        if setting.key == key:
            return setting
    return None


def by_group() -> dict[str, list[Setting]]:
    """Settings grouped for display, in `GROUPS` order."""
    grouped: dict[str, list[Setting]] = {name: [] for name in GROUPS}
    for setting in SETTINGS:
        grouped.setdefault(setting.group, []).append(setting)
    return {name: items for name, items in grouped.items() if items}


def needs_restart() -> tuple[Setting, ...]:
    """Settings whose change only takes effect on the next start.

    The UI must say so on the control. A change that appears to work and does
    not is worse than one that is refused.
    """
    return tuple(setting for setting in SETTINGS if setting.restart)
