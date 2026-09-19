"""Chat's tunables, read from the application's settings - and safe without them.

Layer: L8b - no Qt.

The work order (3e) says chat's behaviours - rounds, the verification threshold,
which model plays which role - are **envelope tunables, invisible outside Manual**.
They are declared once in `app/core/settings_registry.py` (`CHAT_*`), read by
`app/core/config.py` into `Settings.chat_*`, and read *here* into one frozen
`ChatSettings` the engine holds. Nothing else in `app/chat/` reads `Settings`.

`ChatSettings.from_settings` accepts a real `Settings`, any object with the same
attribute names, a mapping, or `None` - every missing value takes the default, so
the engine is constructible in a test with no configuration at all, and a value
outside its allowed range is clamped rather than trusted (an out-of-range
`CHAT_MAX_ROUNDS` in a hand-edited `.env` must not become an unbounded loop).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

__all__ = [
    "ChatSettings",
    "MAX_ROUNDS_CEILING",
    "DEFAULT_VERIFY_PERCENT",
]

#: The most searches one question may cost. The work order's bound: "<=3 rounds".
#: A ceiling on the *setting*, so no configuration can lift it.
MAX_ROUNDS_CEILING = 3

#: How much of a sentence's meaningful words must be found in the passage it
#: cites before it may be shown, as a percentage. Chosen on the fixture set
#: (`tests/fixtures/chat_eval.py`) - see the measurement record in
#: `docs/WORKORDER-202626270611-chat-tab.md` once the lead has recorded it - and
#: pinned in `tests/unit/test_chat_evaluate.py`.
DEFAULT_VERIFY_PERCENT = 70


def _pick(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source and source[name] not in (None,):
                return source[name]
        elif source is not None and getattr(source, name, None) is not None:
            return getattr(source, name)
    return default


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class ChatSettings:
    """Everything tunable about the chat engine, in one immutable value."""

    #: Searches one question may run (1..3). The loop stops earlier when it has
    #: enough; this only bounds the worst case.
    max_rounds: int = MAX_ROUNDS_CEILING
    #: 0.3..0.9. A sentence below this share of supported words is not shown.
    verify_threshold: float = DEFAULT_VERIFY_PERCENT / 100.0
    #: Empty means "choose": see `app/chat/roles.py`.
    router_model: str = ""
    planner_model: str = ""
    answer_model: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    #: The application-wide model, which every empty role inherits.
    ollama_model: str = "mistral"
    #: Documents put in front of the answering model, at most.
    max_sources: int = 6
    #: Rows a FIND or list answer carries; more is "the best N of them".
    result_limit: int = 50
    #: Seconds one model call may take before the engine gives up on it.
    timeout_s: float = 120.0
    #: Whether the combine step of a SYNTHESIS answer may run. **Off**: the
    #: work order's honest downgrade - synthesis ships as extract-and-quote (each
    #: document's verified sentences, listed) until measured on a model that
    #: justifies more. See `app/chat/engine.py`.
    synthesis_combine: bool = False
    #: "Today", for "last year" and "this month". Fixed in tests and evaluation so
    #: a question does not change its answer on New Year's Day.
    today: Optional[date] = None

    @classmethod
    def from_settings(cls, settings: Any = None, **overrides: Any) -> "ChatSettings":
        percent = _clamp(_pick(settings, "chat_verify_strictness", "CHAT_VERIFY_STRICTNESS",
                               default=DEFAULT_VERIFY_PERCENT), 30, 90, DEFAULT_VERIFY_PERCENT)
        values: dict[str, Any] = {
            "max_rounds": _clamp(_pick(settings, "chat_max_rounds", "CHAT_MAX_ROUNDS",
                                       default=MAX_ROUNDS_CEILING),
                                 1, MAX_ROUNDS_CEILING, MAX_ROUNDS_CEILING),
            "verify_threshold": percent / 100.0,
            "router_model": str(_pick(settings, "chat_router_model", "CHAT_ROUTER_MODEL",
                                      default="") or "").strip(),
            "planner_model": str(_pick(settings, "chat_planner_model", "CHAT_PLANNER_MODEL",
                                       default="") or "").strip(),
            "answer_model": str(_pick(settings, "chat_model", "CHAT_MODEL",
                                      default="") or "").strip(),
            "ollama_url": str(_pick(settings, "ollama_url", "OLLAMA_URL",
                                    default="http://127.0.0.1:11434")).strip(),
            "ollama_model": str(_pick(settings, "ollama_model", "OLLAMA_MODEL",
                                      default="mistral")).strip() or "mistral",
        }
        values.update(overrides)
        return cls(**values)
