"""Typed entities from a local LLM. The upgrade, never the requirement.

Layer: L6

The co-occurrence graph already knows that "Barnsley Dairy" and "HACCP" belong
together. What it cannot tell you is that one is a *site* and the other is a
*standard* - and it will happily promote "Kind Regards" to a node. This pass asks
a local model to type what is already there and to find the entities a
capitalisation rule cannot see.

**It only ever adds and refines.** It never deletes a co-occurrence entity, and
it never rewrites edges. Two reasons, and the second is the one that matters:
the co-occurrence graph is deterministic and reproducible, so leaving it intact
means the effect of enrichment can always be seen and always be undone; and a
model that hallucinates on one batch would otherwise silently remove real
findings with no record of what was lost.

**Ollama going away mid-run is normal, not exceptional.** It is a process
somebody starts and stops. So the job checkpoints after every batch, catches
exactly `ERR_OLLAMA_DOWN`, stops cleanly, and reports where it got to. Running it
again resumes from that point. Nothing is lost, nothing is repeated, and no
search anywhere is affected while it happens - Layer 4 does not read these
tables.

**The model's output is untrusted input.** It is parsed defensively, bounded in
length, and filtered: a batch that comes back as prose, as the wrong shape, or
with a 400-character "entity" costs that batch and nothing more.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence

from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import logger
from app.graph.cooccurrence import normalise

__all__ = ["EntityEnricher", "EnrichResult", "VALID_KINDS", "build_prompt", "parse_reply"]

log = logger.bind(component="graph.entities_llm")

#: The closed set from the spec. Closed on purpose: an open vocabulary gives a
#: different taxonomy every few batches ("company", "organisation", "org",
#: "business"), and then nothing can be coloured, filtered or counted by type.
VALID_KINDS = frozenset({"person", "org", "project", "system", "date", "place", "standard"})

#: An entity longer than this is a sentence the model mislabelled.
MAX_ENTITY_CHARS = 80

#: Chunks per request. Small enough that one bad batch is cheap to lose and a
#: stop is responsive; large enough that the per-request overhead is not most of
#: the run.
BATCH_CHUNKS = 8

#: Characters of each chunk sent. A local 7B model with a 4k context cannot take
#: eight full chunks, and a truncated *prompt* is worse than a truncated input -
#: the instructions are what gets dropped.
CHUNK_CHARS = 1200


@dataclass(slots=True)
class EnrichResult:
    chunks_processed: int = 0
    entities_added: int = 0
    entities_typed: int = 0
    batches_failed: int = 0
    elapsed_s: float = 0.0
    paused: bool = False
    error: Optional[AppError] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks_processed": self.chunks_processed,
            "entities_added": self.entities_added,
            "entities_typed": self.entities_typed,
            "batches_failed": self.batches_failed,
            "elapsed_s": round(self.elapsed_s, 2),
            "paused": self.paused,
            "error": self.error.code if self.error else None,
        }


class _Client(Protocol):
    def health(self, *, force: bool = ...) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...): ...


class _Store(Protocol):
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]: ...
    def set_state(self, key: str, value: str) -> None: ...
    def iter_chunks_after(self, chunk_id: int, batch_size: int = ...): ...
    def commit_graph_batch(self, *, entities, mentions, pairs, cursor, cursor_key=...) -> dict[str, int]: ...
    def retype_entities(self, rows: Sequence[tuple[str, str]]) -> int: ...
    def recount_entities(self) -> None: ...


def build_prompt(passages: Sequence[tuple[int, str]]) -> str:
    """The extraction prompt. One string, so it can be diffed and tested.

    Written to make the *failure* modes cheap rather than to maximise recall.
    It asks for a flat object keyed by chunk id, gives the closed kind list
    inline, and shows one example - which is what stops a small model from
    inventing a nested schema that then has to be parsed defensively forever.
    """
    kinds = ", ".join(sorted(VALID_KINDS))
    blocks = "\n\n".join(
        f"[{chunk_id}]\n{text[:CHUNK_CHARS]}" for chunk_id, text in passages
    )
    return (
        "Extract named entities from each numbered passage below.\n\n"
        f"Allowed kinds, use no others: {kinds}\n\n"
        "Rules:\n"
        "- Use the exact spelling from the passage.\n"
        "- Skip generic words, greetings, and anything you are unsure about.\n"
        "- Return at most 10 entities per passage.\n"
        "- Reply with JSON only, no explanation.\n\n"
        'Format: {"<passage number>": [{"name": "...", "kind": "..."}]}\n'
        'Example: {"41": [{"name": "Barnsley Dairy", "kind": "place"}, '
        '{"name": "HACCP", "kind": "standard"}]}\n\n'
        f"Passages:\n\n{blocks}\n"
    )


#: The synonyms a small model reaches for instead of the word it was given.
_KIND_SYNONYMS = {
    "organisation": "org", "organization": "org", "company": "org",
    "business": "org", "institution": "org",
    "location": "place", "site": "place", "city": "place", "country": "place",
    "people": "person", "human": "person", "individual": "person",
    "technology": "system", "software": "system", "application": "system",
    "regulation": "standard", "specification": "standard",
    "time": "date", "datetime": "date",
}


def _canonical_kind(raw: str) -> str:
    """Map what the model said onto the closed vocabulary, or return it unchanged.

    Plurals are stripped only when doing so *lands on a valid kind*. A blanket
    `rstrip("s")` turns "person" into "perso" the moment a model returns
    "persons" - the kind of fix that works on the example and silently discards a
    whole category in production.
    """
    kind = raw.strip().casefold()
    kind = _KIND_SYNONYMS.get(kind, kind)
    if kind in VALID_KINDS:
        return kind
    singular = kind[:-1] if kind.endswith("s") else kind
    singular = _KIND_SYNONYMS.get(singular, singular)
    return singular if singular in VALID_KINDS else kind


def parse_reply(payload: Any, allowed_ids: Sequence[int]) -> dict[int, list[tuple[str, str]]]:
    """Turn whatever came back into `{chunk_id: [(name, kind), ...]}`.

    Every rule here exists because a local model breaks it. Ids arrive as strings
    and as ints; kinds arrive capitalised, pluralised, or invented; names arrive
    as whole sentences; the top level arrives as a list instead of an object.
    Anything that does not fit is dropped silently and the rest is kept - a batch
    that is 80% usable is worth keeping, and one malformed row must not cost the
    other seven passages.

    Ids not in `allowed_ids` are discarded. A model echoing an id from its own
    example would otherwise attach entities to an unrelated chunk, which is the
    one failure here that would be genuinely hard to notice later.
    """
    if not isinstance(payload, dict):
        return {}
    permitted = {int(value) for value in allowed_ids}
    found: dict[int, list[tuple[str, str]]] = {}

    for raw_id, items in payload.items():
        try:
            chunk_id = int(str(raw_id).strip().strip("[]"))
        except (TypeError, ValueError):
            continue
        if chunk_id not in permitted or not isinstance(items, list):
            continue

        cleaned: list[tuple[str, str]] = []
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            kind = _canonical_kind(str(item.get("kind", "")))
            if not name or len(name) > MAX_ENTITY_CHARS or kind not in VALID_KINDS:
                continue
            cleaned.append((name, kind))
        if cleaned:
            found[chunk_id] = cleaned
    return found


class EntityEnricher:
    """Runs typed extraction over the index, resumably, in the background."""

    CURSOR_KEY = "graph:llm_cursor"

    def __init__(
        self,
        store: _Store,
        client: _Client,
        *,
        batch_chunks: int = BATCH_CHUNKS,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.batch_chunks = batch_chunks
        self.on_progress = on_progress
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def reset(self) -> None:
        """Start again from the beginning next run, keeping what was found.

        Deliberately does not delete anything: re-running is how someone tries a
        better model, and losing the previous pass to find out the new one is
        worse would be a bad trade.
        """
        self.store.set_state(self.CURSOR_KEY, "0")

    def run(self, *, chunks_total: int = 0, max_chunks: int = 0) -> EnrichResult:
        started = time.monotonic()
        result = EnrichResult()

        if not self.client.health(force=True):
            # Not an exception: Ollama being off is the expected state on most
            # machines most of the time, and the caller's correct response is to
            # carry on with the co-occurrence graph, not to handle a failure.
            result.paused = True
            result.error = make_error(
                "ERR_OLLAMA_DOWN", "graph.entities_llm",
                details="Typed entity extraction was not started because Ollama is not running.",
                suggestion=(
                    "Start Ollama and run this again - it resumes where it left off. "
                    "The co-occurrence graph is already built and does not need it."
                ),
            )
            log.info("llm enrichment: Ollama unavailable; nothing attempted")
            result.elapsed_s = time.monotonic() - started
            return result

        cursor = int(self.store.get_state(self.CURSOR_KEY, "0") or 0)

        for rows in self.store.iter_chunks_after(cursor, self.batch_chunks):
            if self._stop:
                break
            passages = [(chunk_id, text) for chunk_id, _file_id, text in rows]
            files = {chunk_id: file_id for chunk_id, file_id, _text in rows}

            try:
                reply = self.client.generate(build_prompt(passages), json_mode=True).json()
            except AppErrorException as exc:
                if exc.error.code != "ERR_OLLAMA_DOWN":
                    raise
                # Ollama stopped, or answered with nonsense. Either way the
                # cursor still points at the last committed batch, so this
                # resumes exactly here. Nothing partial has been written.
                result.paused = True
                result.error = exc.error
                log.info(
                    "llm enrichment: paused at chunk {} after {} chunks - {}",
                    cursor, result.chunks_processed, exc.error.message,
                )
                break

            extracted = parse_reply(reply, list(files))
            if not extracted:
                result.batches_failed += 1

            cursor = rows[-1][0]
            added, typed = self._write(extracted, files, cursor)
            result.entities_added += added
            result.entities_typed += typed
            result.chunks_processed += len(rows)

            if self.on_progress:
                self.on_progress(result.chunks_processed, chunks_total)
            if max_chunks and result.chunks_processed >= max_chunks:
                break

        if result.chunks_processed:
            self.store.recount_entities()

        result.elapsed_s = time.monotonic() - started
        log.info("llm enrichment: {}", result.as_dict())
        return result

    def _write(
        self,
        extracted: dict[int, list[tuple[str, str]]],
        files: dict[int, int],
        cursor: int,
    ) -> tuple[int, int]:
        """One transaction: new entities, their mentions, their pairs, the cursor.

        The cursor moves even when the model returned nothing usable for a batch.
        That is intentional - retrying a batch that a model cannot parse just
        stalls the job on the same eight chunks forever, and `batches_failed`
        records that it happened.
        """
        entities: dict[str, tuple[str, str, str, str]] = {}
        mentions: list[tuple[str, int, int, int]] = []
        pairs: dict[tuple[str, str], int] = {}
        retype: list[tuple[str, str]] = []

        for chunk_id, items in extracted.items():
            keys: list[str] = []
            for name, kind in items:
                key = normalise(name)
                if not key:
                    continue
                entities.setdefault(key, (key, name, kind, "llm"))
                retype.append((key, kind))
                mentions.append((key, chunk_id, files[chunk_id], 1))
                keys.append(key)
            unique = sorted(set(keys))
            for index, key_a in enumerate(unique):
                for key_b in unique[index + 1:]:
                    pairs[(key_a, key_b)] = pairs.get((key_a, key_b), 0) + 1

        ids = self.store.commit_graph_batch(
            entities=list(entities.values()), mentions=mentions, pairs=pairs,
            cursor=cursor, cursor_key=self.CURSOR_KEY,
        )
        typed = self.store.retype_entities(retype) if retype else 0
        return len(ids), typed
