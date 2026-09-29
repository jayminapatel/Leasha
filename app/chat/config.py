"""Chat's tunables, read from the application's settings - and safe without them.

Layer: L8b - no Qt.

The work order (3e) says chat's behaviours - rounds, the verification threshold,
which model plays which role - are **envelope tunables, invisible outside Manual**.
They are declared once in `app/core/settings_registry.py` (`CHAT_*`), read by
`app/core/config.py` into `Settings.chat_*`, and read *here* into one frozen
`ChatSettings` the engine holds.

**They follow the Index Tuning modes** (`INDEX_TUNING_MODE`), the same rule as the
indexing knobs: in Defaults and Auto-tune the *envelope decides*
(`app/core/envelope.py`: `chat_max_rounds`, `chat_verify_strictness`) and no model is
forced onto a role; only in Manual do the stored values count, clamped to the
envelope. What somebody typed in Manual is kept and inert while the mode is
elsewhere, so trying Manual is reversible. A settings object that carries no mode at
all (a test double, a mapping) is asking for exactly what it says, so its values
stand. Nothing else in `app/chat/` reads `Settings`.

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


def _mode_of(source: Any) -> str:
    """`"manual"`, another mode's name, or `""` when the source says nothing."""
    mode = _pick(source, "index_tuning_mode", "INDEX_TUNING_MODE", default="")
    return str(mode or "").strip().lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


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
    #: `onnx` - the chat model inside Leasha (`app/ort/llm.py`) - or `ollama`
    #: (`CHAT_ENGINE`, 2026-09-29), and the model folder and processor it uses.
    engine: str = "onnx"
    model_cache: str = ""
    device: str = "auto"
    #: Documents put in front of the answering model, at most.
    max_sources: int = 6
    #: Rows a FIND or list answer carries; more is "the best N of them".
    result_limit: int = 50
    #: Seconds one model call may take before the engine gives up on it.
    timeout_s: float = 120.0
    #: Whether a SYNTHESIS answer (one that spans several documents) is **combined**
    #: into one flowing account. **On** since 2026-09-20 (owner: the chat has to read
    #: like an assistant wrote it); off asks for one short paragraph per document
    #: instead. It used to gate a second model call over verified extracts, the work
    #: order's downgrade for a small model - that two-step is gone, the answer is now
    #: written once, streamed, and checked sentence by sentence (`app/chat/reconcile.py`).
    synthesis_combine: bool = True
    #: Tokens the model is asked to read at once (`CHAT_CONTEXT_TOKENS`): what the
    #: conversation's sliding memory is fitted to. The model's own limit still applies.
    context_tokens: int = 8192
    #: The person's own addition to Chat's manner (`CHAT_STYLE_NOTE`), in their words.
    style_note: str = ""
    #: The web (owner, 2026-09-20): OFF unless switched on, and never through the
    #: Index Tuning mode - these are the person's own switches, read as stored.
    web_enabled: bool = False
    web_provider: str = "auto"
    web_ask_first: bool = True
    web_show_query: bool = True
    web_searxng_url: str = ""
    web_brave_key: str = ""
    #: "Today", for "last year" and "this month". Fixed in tests and evaluation so
    #: a question does not change its answer on New Year's Day.
    today: Optional[date] = None

    #: Which Index Tuning mode these values came from: `"manual"`, `"defaults"`,
    #: `"auto"`, or `""` when the source did not say. Shown in the debug pane so a
    #: stored value that is not in force can be explained.
    tuning_mode: str = ""

    @classmethod
    def from_settings(cls, settings: Any = None, *, profile: Any = None,
                      **overrides: Any) -> "ChatSettings":
        """Chat's settings for one engine.

        `profile` is the machine (anything with `ram_mb`), for the envelope's
        automatic numbers; `None` means "unknown", which the envelope answers with
        its ordinary defaults - so a test or a command line with no profile is
        deterministic.
        """
        from app.core import envelope

        mode = _mode_of(settings)
        stored = mode in ("", "manual")     # nothing said, or Manual: the values stand

        rounds_bounds = envelope.chat_max_rounds(profile)
        strict_bounds = envelope.chat_verify_strictness(profile)
        context_bounds = envelope.chat_context_tokens(profile)
        if stored:
            rounds = _clamp(_pick(settings, "chat_max_rounds", "CHAT_MAX_ROUNDS",
                                  default=MAX_ROUNDS_CEILING),
                            1, MAX_ROUNDS_CEILING, MAX_ROUNDS_CEILING)
            if mode == "manual":
                rounds = _clamp(rounds, rounds_bounds.floor, rounds_bounds.ceiling,
                                rounds_bounds.auto)
            percent = _clamp(_pick(settings, "chat_verify_strictness", "CHAT_VERIFY_STRICTNESS",
                                   default=DEFAULT_VERIFY_PERCENT), 30, 90, DEFAULT_VERIFY_PERCENT)
            context = _clamp(_pick(settings, "chat_context_tokens", "CHAT_CONTEXT_TOKENS",
                                   default=context_bounds.auto),
                             context_bounds.floor, context_bounds.ceiling, context_bounds.auto)
            note = " ".join(str(_pick(settings, "chat_style_note", "CHAT_STYLE_NOTE",
                                      default="") or "").split())
        else:
            rounds, percent = rounds_bounds.auto, strict_bounds.auto
            context, note = context_bounds.auto, ""

        def model(*names: str) -> str:
            if not stored:
                return ""                    # nobody forces a model outside Manual
            return str(_pick(settings, *names, default="") or "").strip()

        values: dict[str, Any] = {
            "max_rounds": rounds,
            "verify_threshold": percent / 100.0,
            "router_model": model("chat_router_model", "CHAT_ROUTER_MODEL"),
            "planner_model": model("chat_planner_model", "CHAT_PLANNER_MODEL"),
            "answer_model": model("chat_model", "CHAT_MODEL"),
            "ollama_url": str(_pick(settings, "ollama_url", "OLLAMA_URL",
                                    default="http://127.0.0.1:11434")).strip(),
            "ollama_model": str(_pick(settings, "ollama_model", "OLLAMA_MODEL",
                                      default="mistral")).strip() or "mistral",
            "tuning_mode": mode,
            # 2026-09-29: which engine answers, and where its model lives.
            "engine": ("ollama" if str(_pick(settings, "chat_engine", "CHAT_ENGINE",
                                             default="onnx") or "").strip().lower() == "ollama"
                       else "onnx"),
            "model_cache": str(_pick(settings, "model_cache", "MODEL_CACHE", default="") or ""),
            "device": str(_pick(settings, "embed_device", "EMBED_DEVICE", default="auto")
                          or "auto").strip().lower(),
            "context_tokens": context,
            "style_note": note,
            "web_enabled": _truthy(_pick(settings, "chat_web_enabled", "CHAT_WEB_ENABLED",
                                         default=False)),
            "web_provider": str(_pick(settings, "chat_web_provider", "CHAT_WEB_PROVIDER",
                                      default="auto") or "auto").strip().lower(),
            "web_ask_first": _truthy(_pick(settings, "chat_web_ask_first", "CHAT_WEB_ASK_FIRST",
                                           default=True)),
            "web_show_query": _truthy(_pick(settings, "chat_web_show_query", "CHAT_WEB_SHOW_QUERY",
                                            default=True)),
            "web_searxng_url": str(_pick(settings, "chat_web_searxng_url", "CHAT_WEB_SEARXNG_URL",
                                         default="") or "").strip(),
            "web_brave_key": str(_pick(settings, "chat_web_brave_key", "CHAT_WEB_BRAVE_KEY",
                                       default="") or "").strip(),
        }
        values.update(overrides)
        return cls(**values)
