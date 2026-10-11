"""The document-card pass: one loop, run by the index run's end and by `leasha cards`.

Layer: L6

Order `smarter-answers` 2c and 2e. `doc_cards.py` decides what may be kept of a
reply; this walks the files that need a card, asks the model, and stores what
was kept. One loop for both callers, so the command line makes cards exactly as
an index run does (the owner, 2026-10-04: "the same code should run for
functions so they are all consistent").

Newest first, paced by whatever `throttle` the caller hands in, stopped by
`interrupted`, and resumed next time from `doc_cards` alone - there is no
cursor to lose. A document too short to need a card is noted without asking
the model, so it is not offered again. A model that cannot be had costs one
check; one that keeps failing stops the pass rather than every document
waiting out its time limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.index import doc_cards as cards

__all__ = ["CardRun", "run_cards", "CARD_MAX_TOKENS", "CARD_TIME_LIMIT_S",
           "CARD_FAILURES_STOP", "model_ready"]

#: The pass's limits. Not settings (non-negotiable 11): a card is a few short
#: lists and two sentences, which fit in 300 tokens with room over; two minutes
#: is far beyond what one card should take, so only a stuck model reaches it;
#: three failures running means the model is gone, not one awkward document.
#: A measurement of cards cut short, or of a slower model, would change them.
CARD_MAX_TOKENS = 300
CARD_TIME_LIMIT_S = 120.0
CARD_FAILURES_STOP = 3


@dataclass
class CardRun:
    """What one pass did."""

    made: int = 0
    nothing: int = 0
    failed: int = 0
    seconds_asking: float = 0.0
    #: "" when it ran out of work or was stopped; else why it gave up.
    gave_up: str = ""


def model_ready(client: Any) -> bool:
    """Can this client answer at all? `has_model` where it has one (installed or
    downloaded), else `health`. Never raises."""
    try:
        check = getattr(client, "has_model", None) or getattr(client, "health", None)
        return bool(check()) if check is not None else True
    except Exception:                                 # noqa: BLE001 - "no" is the answer
        return False


def run_cards(
    store: Any, factory: Callable[[], Any], *,
    interrupted: Callable[[], bool],
    throttle: Optional[Callable[[], bool]] = None,
    on_card: Optional[Callable[[str], None]] = None,
    on_first: Optional[Callable[[], None]] = None,
    delete_vectors: Optional[Callable[[list[int]], None]] = None,
    log: Any = None,
    limit: Optional[int] = None,
) -> CardRun:
    """Write cards until there are none to write, `limit` are made, or it is stopped.

    `throttle()` is called before each batch and returns False to stop (the
    index run's governor). `on_first()` is called once, when the first card is
    about to be asked for (the run's phase line). `delete_vectors` gets the ids
    of card passages a new card replaced.
    """
    import time

    result = CardRun()
    client: Any = None
    model_name = ""
    first = True
    running_failures = 0
    for batch in store.iter_card_candidates(
            cards.DOC_EXTS, cards.MAIL_EXTS,
            prompt_version=cards.PROMPT_VERSION, label=cards.CARD_LABEL):
        if interrupted():
            return result
        if throttle is not None and not throttle():
            return result
        for file_id, path, content_hash in batch:
            if interrupted() or (limit is not None and result.made >= limit):
                return result
            source = store.card_source_text(file_id, cards.MAX_CHARS)
            if len(source.strip()) < cards.MIN_CHARS:
                store.save_doc_card(
                    file_id, content_hash, outcome="nothing", fields=None, text=None,
                    label=cards.CARD_LABEL, model="", prompt_version=cards.PROMPT_VERSION)
                result.nothing += 1
                continue
            if client is None:
                client = factory()
                if client is None or not model_ready(client):
                    result.gave_up = "no model to write them with"
                    return result
                model_name = str(getattr(client, "model", "") or "")
            if first and on_first is not None:
                on_first()
            first = False
            name = Path(path).name
            started = time.perf_counter()
            try:
                reply = client.generate(
                    cards.build_prompt(name, source), json_mode=True, temperature=0.0,
                    max_tokens=CARD_MAX_TOKENS, timeout=CARD_TIME_LIMIT_S,
                    should_stop=interrupted)
            except Exception as exc:                  # noqa: BLE001 - one document, not the pass
                result.failed += 1
                running_failures += 1
                if log is not None:
                    log.warning("document card for {} not written: {}", name, exc)
                if running_failures >= CARD_FAILURES_STOP:
                    result.gave_up = f"the model failed {running_failures} times running"
                    return result
                continue
            finally:
                result.seconds_asking += time.perf_counter() - started
            running_failures = 0
            card = cards.read_card(str(getattr(reply, "text", "") or ""), source)
            if card is None:
                store.save_doc_card(
                    file_id, content_hash, outcome="nothing", fields=None, text=None,
                    label=cards.CARD_LABEL, model=model_name,
                    prompt_version=cards.PROMPT_VERSION)
                result.nothing += 1
            else:
                if card.dropped and log is not None:
                    log.debug("document card for {}: dropped {}", name, ", ".join(card.dropped))
                _chunk, removed = store.save_doc_card(
                    file_id, content_hash, outcome="made", fields=card.as_row(),
                    text=cards.card_text(card), label=cards.CARD_LABEL,
                    model=model_name, prompt_version=cards.PROMPT_VERSION)
                if removed and delete_vectors is not None:
                    delete_vectors(removed)
                result.made += 1
            if on_card is not None:
                on_card(name)
    return result
