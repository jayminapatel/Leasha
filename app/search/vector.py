"""ANN search — the half of hybrid search that knows what words mean.

Layer: L4

Where BM25 finds the document containing "pump station", this finds the one
about "compressor house" when that is what the person meant. On its own it is
worse than keyword search at names, part numbers and codes, and better at
everything vague. Neither is the primary; that is the whole argument for fusion.

**Filters are pushed down, not applied afterwards.** LanceDB cannot join to
`files`, so eligible file ids are resolved in SQL first and handed over as a
prefilter. Fetching the top 100 and *then* discarding the ones that fail
`type:pdf` would leave whatever survived — which is not the top 100 PDFs, it is
the PDFs that happened to be in the global top 100, and on a large index that is
frequently none of them.

**One embedding per search, never per candidate.** The query is embedded once.
That single call is the layer's largest fixed cost at roughly 15ms, and it is why
the model is warmed at startup rather than on first search.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from app.core.errors import AppErrorException
from app.core.logging import logger
from app.search.query import ParsedQuery

__all__ = [
    "search", "VECTOR_LIMIT", "MAX_PREFILTER_IDS", "search_images", "hydrate_images",
    "CLIP_TEXT_MODEL", "CLIP_TEXT_DIM", "clip_text_embedder_from_settings",
    "search_by_image",
]

#: Candidates handed to fusion. From the spec's pipeline diagram.
VECTOR_LIMIT = 100

#: Above this many eligible file ids, the pushed-down prefilter is skipped on
#: the first attempt. An `IN (...)` list of 50,000 ids is slower to build and
#: parse than the search it was meant to narrow - but skipping it must never
#: change *which* results come back, so the large-filter path over-fetches and
#: escalates until the eligible top-k is guaranteed (see `search`).
MAX_PREFILTER_IDS = 2_000

#: Escalation ladder for the large-filter path: fetch `limit * factor` global
#: candidates and post-filter. A filter big enough to skip the prefilter
#: usually matches most of the corpus, so the first rung nearly always fills
#: the page in one query.
OVERFETCH_FACTORS = (4, 16)

_log = logger.bind(component="search.vector")


def search(
    vectors: Any,
    embedder: Any,
    parsed: ParsedQuery,
    *,
    limit: int = VECTOR_LIMIT,
    allowed_file_ids: Optional[set[int]] = None,
    problems: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    r"""ANN hits for a parsed query, nearest first.

    Returns [] when there is nothing to embed, when the index is empty, or when
    the filters exclude everything. All three are ordinary states rather than
    errors: searching before the first index run is normal, and a filter that
    matches nothing should produce no results, not a failure.

    **And [] when this half breaks, which is the fourth reason and was the bug.**
    Both boundaries below used to re-raise `AppErrorException` while catching
    everything else - and `Embedder.embed` raises exactly that for a model that
    is missing, corrupt or half-downloaded. So a broken embedding model did not
    degrade hybrid search to keyword search; it failed the **whole** search and
    discarded the keyword hits that had already been computed alongside it.

    That contradicted this module's own comment two lines further down - *"a
    vector-store hiccup must not"* stop the search - and it contradicted
    `engine.NOTICE_NO_VECTORS`, machinery built precisely to tell somebody that
    they are looking at keyword-only results. The notice could never fire for
    the one failure it describes best.

    `problems` is how the cause survives the degradation. Returning [] silently
    would trade a loud wrong behaviour for a quiet one, and this codebase has a
    standing rule against exactly that: *"for all things it should not fail
    silently it should notify in some way."* Append-only, optional, and
    unexamined here - the caller decides what to do with the words.
    """
    text = parsed.embed_text
    if not text:
        return []

    eligible = _as_eligibility(allowed_file_ids)
    if eligible is not None and eligible.excludes_everything:
        return []                      # filters excluded everything; nothing to search

    try:
        query_vector = embedder.embed([text])[0]
    except AppErrorException as exc:
        # The model itself. Named separately from a generic failure because it
        # is the one with an action attached - the AppError already carries a
        # suggestion, and dropping it here would waste the best sentence
        # available to whoever is looking at half a search.
        _log.error("query embedding failed, continuing with keyword results "
                   "only: {}", exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - boundary
        _log.error("query embedding failed: {}", exc)
        _note(problems, f"The query could not be embedded ({exc}).")
        return []

    try:
        pushdown = eligible.ids if eligible is not None else None
        if pushdown is not None and len(pushdown) <= MAX_PREFILTER_IDS:
            # Fast path: a filter small enough to push down whole.
            rows = vectors.search(query_vector, k=limit, where=_id_clause(pushdown))
            return [_normalise(row) for row in rows]
        if eligible is None:
            rows = vectors.search(query_vector, k=limit, where=None)
            return [_normalise(row) for row in rows]
        return _search_large_filter(vectors, query_vector, limit, eligible)
    except AppErrorException as exc:
        _log.error("ANN search failed, continuing with keyword results only: {}",
                   exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - a vector-store hiccup must not
        _log.error("ANN search failed, continuing with keyword results only: {}", exc)
        _note(problems, f"The vector store could not be searched ({exc}).")
        return []


def _note(problems: Optional[list[str]], text: str) -> None:
    """Record why this half produced nothing. **Never raises.**

    A sink that throws would take down the search this function exists to keep
    alive, which would be an absurd way to lose it.
    """
    if problems is None:
        return
    try:
        problems.append(str(text))
    except Exception:                  # noqa: BLE001
        return


def _search_large_filter(
    vectors: Any,
    query_vector: Sequence[float],
    limit: int,
    eligible: Any,
) -> list[dict[str, Any]]:
    """The eligible top-`limit` when the id list is too big to push down cheaply.

    Fetching the global top-`limit` and discarding ineligible rows would lose
    every eligible hit ranked past `limit` globally - `type:pdf` on a large
    mixed corpus could return nothing while thousands of PDFs match. So:
    over-fetch, post-filter, escalate, and if the ladder runs out, push the
    full id list down anyway. Slower on the last rung, never wrong.
    """
    for factor in OVERFETCH_FACTORS:
        k = limit * factor
        rows = vectors.search(query_vector, k=k)
        # **One question about the candidates, not a list of the eligible.**
        # `keeps` intersects a held set when the filter is narrow and asks
        # SQLite about these few hundred ids when it is broad - which is what
        # stops a `type:pdf` over a large corpus paying 383ms to build a
        # quarter of a million ids nothing then wanted.
        candidates = [_normalise(r) for r in rows]
        allowed = eligible.keeps([row["file_id"] for row in candidates])
        results = [row for row in candidates if row["file_id"] in allowed]
        if len(results) >= limit or len(rows) < k:
            # A full page of eligible hits, or the table itself is exhausted:
            # either way the eligible top-k is complete.
            return results[:limit]
        _log.debug(
            "over-fetch k={} found only {} of {} eligible hits; escalating",
            k, len(results), limit,
        )

    # Last rung: the exact prefilter, however large. This is the query the cap
    # exists to avoid, paid only when the cheap attempts could not fill a page.
    ids = eligible.ids
    if ids is None:
        # **A filter too broad to list, and the ladder still did not fill a
        # page.** There is no id list to push down - building one is the cost
        # this path exists to avoid - so take the widest over-fetch and answer
        # with what is eligible in it. Short of a full page rather than wrong,
        # and only reachable when the filter is broad *and* the query matches
        # almost nothing that passes it.
        rows = vectors.search(query_vector, k=limit * OVERFETCH_FACTORS[-1])
        candidates = [_normalise(r) for r in rows]
        allowed = eligible.keeps([row["file_id"] for row in candidates])
        return [row for row in candidates if row["file_id"] in allowed][:limit]

    _log.debug("over-fetch exhausted; pushing down full {}-id prefilter", len(ids))
    rows = vectors.search(query_vector, k=limit, where=_id_clause(ids))
    return [_normalise(row) for row in rows]


class _SetEligibility:
    """An `Eligibility` face over a plain set of ids.

    `vector.search` is called with a bare set by six regression tests and by
    anybody experimenting at a REPL, and those calls are worth keeping working.
    One adapter here is cheaper than a second code path inside the search.
    """

    __slots__ = ("ids",)

    def __init__(self, ids: set[int]) -> None:
        self.ids = {int(f) for f in ids}

    @property
    def excludes_everything(self) -> bool:
        return not self.ids

    def keeps(self, file_ids: Sequence[int]) -> set[int]:
        return {int(f) for f in file_ids} & self.ids


def _as_eligibility(allowed: Any) -> Any:
    """None, an `Eligibility`, or a set - normalised to the first two."""
    if allowed is None:
        return None
    if hasattr(allowed, "keeps"):
        return allowed
    return _SetEligibility(allowed)


def _id_clause(allowed_file_ids: Optional[set[int]]) -> Optional[str]:
    if allowed_file_ids is None:
        return None
    ids = ", ".join(str(int(fid)) for fid in sorted(allowed_file_ids))
    return f"file_id IN ({ids})"


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """LanceDB returns `_distance`; the rest of the pipeline speaks `distance`.

    Kept as a distance rather than converted to a similarity, because RRF fuses
    on rank and never looks at the number - and inventing a similarity score
    would invite somebody downstream to compare it against BM25, which is
    exactly the mistake fusion exists to prevent.
    """
    out = dict(row)
    if "_distance" in out:
        out["distance"] = out.pop("_distance")
    out["chunk_id"] = int(out.get("chunk_id", 0))
    out["file_id"] = int(out.get("file_id", 0))
    return out


def hydrate(store: Any, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill in text, path and page for ANN hits.

    The vector store holds ids and vectors only - deliberately, so SQLite stays
    the authority on content and LanceDB can be rebuilt from it at any time. The
    cost is this lookup, which is one indexed query for the whole batch.
    """
    if not rows:
        return []

    ids = [int(row["chunk_id"]) for row in rows]
    placeholders = ", ".join("?" for _ in ids)
    found = {
        int(record["chunk_id"]): dict(record)
        for record in store.conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.text, c.page, c.label,
                   c.char_start, c.char_end, f.path, f.ext, f.mtime_ns,
                   f.taken_at_ns, f.content_hash, f.volume_id, f.relative_path
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE c.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    }

    hydrated = []
    for row in rows:
        detail = found.get(int(row["chunk_id"]))
        if detail is None:
            # A vector whose chunk is gone: LanceDB is derived, so it can lag
            # SQLite for a moment after a delete. Dropping it is correct - the
            # alternative is a result that opens nothing.
            continue
        merged = dict(detail)
        merged["distance"] = row.get("distance")
        hydrated.append(merged)
    return hydrated


# -- work order 0h §1c: the third lane, CLIP image search --------------------

#: The CLIP text tower paired with `CLIP_IMAGE_MODEL`
#: (`app.index.clip_embedder.CLIP_IMAGE_MODEL`) - same family, same 512
#: dimensions, same MIT license, so a query vector from this model and an
#: image vector from that one are comparable at all. `clip_embedder.py`'s
#: `CLIP_IMAGE_DIM` docstring has pointed here since it was written; this is
#: that promise made real rather than left as a forward reference nothing
#: defined.
CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
CLIP_TEXT_DIM = 512


def clip_text_embedder_from_settings(
    settings: object,
    *,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Any:
    r"""The CLIP text-tower embedder every `SearchEngine` call site builds alike.

    **Not `Embedder.from_settings`.** That classmethod reads
    `settings.embed_model` for the model name and passes it *positionally* to
    `Embedder.__init__` - so `Embedder.from_settings(settings, model_name=
    CLIP_TEXT_MODEL)` would raise `TypeError: got multiple values for
    argument 'model_name'` rather than overriding it, since `model_name`
    lands in `**overrides` and collides with the positional one underneath.
    This function plays the same "one constructor, every caller" role
    `Embedder.from_settings` and `Reranker.from_settings` already play for
    their own models (work order 0h §1c names that precedent explicitly),
    just without going through a classmethod that cannot take this one
    override.

    Still reads `cache_dir` and `device` from `settings`, same as
    `Embedder.from_settings` does, so the CLIP text tower shares the model
    cache directory and the DirectML-with-CPU-fallback seam every other
    embedding model in this application already has - there is no reason
    for a 512-dim CLIP model to be the one exception.

    **`on_progress`, work order 0r item 1c's second clause.** This embedder is
    built once, at startup, before the window exists (see `app/main.py`) - but
    it does not load or download until the first image search reaches
    `search_images`, well after the window is showing and the splash is long
    closed. A caller built at startup therefore cannot yet hand in a callback
    that reaches the window. `search_images` below accepts its own
    `on_progress` for exactly that reason: it is set on this embedder at
    *call* time, once a caller (`SearchEngine`) actually has somewhere to
    route it. Passing one here too is for symmetry and for callers (tests,
    a future CLI path) that build and use the embedder in one place and have
    a real target from the start. `None` is the default and costs nothing -
    see `Embedder._start_progress_watcher_if_downloading`, which only starts
    its watcher thread when someone is listening.
    """
    from app.index.embedder import Embedder

    return Embedder(
        CLIP_TEXT_MODEL, dim=CLIP_TEXT_DIM,
        cache_dir=str(getattr(settings, "model_cache", "") or "") or None,
        device=str(getattr(settings, "embed_device", "auto") or "auto"),
        on_progress=on_progress,
    )


def search_images(
    image_vectors: Any,
    text_embedder: Any,
    parsed: ParsedQuery,
    *,
    limit: int = VECTOR_LIMIT,
    allowed_file_ids: Optional[set[int]] = None,
    problems: Optional[list[str]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> list[dict[str, Any]]:
    r"""CLIP hits for a parsed query, nearest first - the third retrieval lane.

    **Reuses `search()` outright rather than reimplementing it.** Nothing in
    that function is specific to the text tower or the 384-dim table - it
    takes a vector store and an embedder and does the rest generically, H4
    degradation included, so this *is* the image lane, called with an
    `ImageVectorStore` and an embedder configured for the CLIP **text** tower
    (`Qdrant/clip-ViT-B-32-text` - see `app/index/clip_embedder.py` for its
    paired vision tower). `text_embedder` is deliberately not the FastEmbed
    text model already used for keyword-adjacent semantic search: that model
    and CLIP's text tower are trained into two different embedding spaces, and
    a vector from one means nothing compared against vectors from the other.
    `text_embedder` here must be built with `Qdrant/clip-ViT-B-32-text` (a
    plain `app.index.embedder.Embedder` configured with that model name and
    `dim=512` already satisfies the interface `search()` calls - one `.embed`
    method, one vector per text - so no new embedder class was needed for
    this side of the pair; only the vision tower in `clip_embedder.py` needed
    one, because FastEmbed's `ImageEmbedding` has a different call shape).

    **v1 heuristic, as the work order specifies: always run this lane** and
    let RRF weight it in - no "does this query look like it wants a photo"
    classifier. Measure against `evaluate`'s photo sentences before tuning
    further; that is future work, not this item.

    **The `chunk_id` rewrite is the reason this is not simply `search()`
    called with different arguments.** `ImageVectorStore` writes `chunk_id`
    equal to `file_id` on every row (see that class's docstring) - correct
    for a table with no chunk concept, but `chunk_id` is also `fuse_hits`'s
    default fusion key, and `chunks.id` and `files.id` are two independent
    SQLite sequences that both start at 1. Fusing an image hit next to a
    text hit under a bare integer `chunk_id` would silently collide the
    moment a photo's file id equalled some unrelated passage's chunk id.
    Renamed to `"img:<file_id>"` here, before this list ever reaches
    `fuse_hits`, which cannot collide with an integer key and reads
    unambiguously in a log or a debugger.

    **`on_progress`, work order 0r item 1c's second clause.** `text_embedder`
    is built once at startup and reused for every call, so its own
    `on_progress` (set at construction - see `clip_text_embedder_from_settings`)
    is usually `None`: nobody has a live target that early. A caller that
    does have one now (`SearchEngine`, once the window it belongs to exists)
    hands it in here instead, and it is set onto `text_embedder` immediately
    before the call it might apply to - the one that actually reaches
    `Embedder._ensure_encoder()` and, if the model cache has been emptied
    mid-life (a moved index, a re-staged data directory), starts a real
    download with nowhere else to report it: the splash is long closed by
    then, correctly, and this is the seam that lets the window's own notices
    bar carry the message instead. **Never lets a broken callback break the
    search itself** - H4, matching every guard already in
    `app/index/embedder.py` around this same hook - `text_embedder` is a
    plain object here, not guaranteed to have this attribute, so setting it
    is wrapped rather than assumed to succeed.
    """
    if on_progress is not None:
        try:
            text_embedder._on_progress = on_progress          # noqa: SLF001 - see docstring
        except Exception:                  # noqa: BLE001 - H4: must never break image search
            _log.debug("could not attach download-progress reporting to the "
                       "CLIP text embedder; the search itself is unaffected")

    rows = search(
        image_vectors, text_embedder, parsed,
        limit=limit, allowed_file_ids=allowed_file_ids, problems=problems,
    )
    for row in rows:
        row["chunk_id"] = f"img:{row['file_id']}"
    return rows


def hydrate_images(store: Any, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill in path and ext for CLIP hits - the image lane's `hydrate`.

    A photo is not a passage: there is no text, no page, no `chunks` row to
    join. Only `files`, by `file_id` - the same "the vector store holds ids
    only" reasoning `hydrate` documents, applied to a table with nothing but
    ids and vectors in it either.

    **Carries `taken_at_ns` too, work order 0f §3a's third clause.** This is
    the CLIP image lane - reverse-image search and "find a photo like this" -
    so its hits are photographs more often than any other lane's, which
    makes this the SELECT where the shot date matters most, not least. The
    §3a lane-b note counted one SELECT site in this module; this second one
    exists (`hydrate` above is the other) and is exactly the site the
    fix is for, so it is included here even though the note did not name it
    by line - verified by grepping this file for `mtime_ns` rather than
    trusting the note's count.
    """
    if not rows:
        return []

    ids = [int(row["file_id"]) for row in rows]
    placeholders = ", ".join("?" for _ in ids)
    found = {
        int(record["file_id"]): dict(record)
        for record in store.conn.execute(
            f"""
            SELECT id AS file_id, path, ext, mtime_ns, taken_at_ns, content_hash, phash,
                   volume_id, relative_path
            FROM files
            WHERE id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    }

    hydrated = []
    for row in rows:
        detail = found.get(int(row["file_id"]))
        if detail is None:
            # Same reasoning as `hydrate`: LanceDB is derived and can lag a
            # deleted file for a moment. Dropping it is correct.
            continue
        merged = dict(detail)
        merged["distance"] = row.get("distance")
        merged["chunk_id"] = row.get("chunk_id")
        hydrated.append(merged)
    return hydrated


# -- work order 0h §2c: reverse image search ----------------------------------


def search_by_image(
    image_vectors: Any,
    image_embedder: Any,
    image_path: Any,
    *,
    limit: int = VECTOR_LIMIT,
    allowed_file_ids: Optional[set[int]] = None,
    problems: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    r"""CLIP hits for a **photo** query, nearest first - reverse image search.

    Work order 0h §2c. Drop or paste an image into the search box and find
    it, and its relatives, in the index. Everything the text lanes need is a
    string to embed; this lane's query is a picture, so it is embedded with
    the CLIP **vision** tower (`image_embedder`, an
    `app.index.clip_embedder.ClipImageEmbedder` - the same class `Pipeline`
    uses at index time) rather than the text tower `search_images` uses for
    a typed description. The two towers share one embedding space by
    construction (`Qdrant/clip-ViT-B-32-vision` and `-text` are trained as a
    pair - see `clip_embedder.py`), which is what makes an image-to-image
    comparison and a text-to-image comparison both meaningful ANN searches
    over the same table.

    **Not built on `search()`, unlike `search_images`.** That function's
    entire contract is built around a `ParsedQuery` and an embedder whose
    `.embed` takes strings - neither fits a bare image path, and forcing one
    in would cost more in translation than it saves in reuse. The H4 shape
    is kept identical by hand instead: any failure to embed or to search
    degrades to `[]` plus a note in `problems`, never a raised exception,
    exactly as `search()` promises for its own two failure points.

    **The `chunk_id` rewrite is repeated here, not shared as code**, for the
    same reason `search_images` needs it at all: `ImageVectorStore` writes
    `chunk_id == file_id`, and a bare integer would collide with an
    unrelated passage's real `chunks.id` inside `fuse_hits`. Duplicating four
    lines is cheaper here than adding a dependency between the two functions
    for a rewrite this small.

    `allowed_file_ids` is accepted for symmetry with every other retrieval
    function in this module, even though no caller in this order builds a
    filtered reverse-image search yet - a caller that wants `type:` or a
    folder scope on a reverse-image search gets it for free rather than
    needing a second function later.
    """
    if image_vectors is None or image_embedder is None:
        return []

    try:
        query_vector = image_embedder.embed([str(image_path)])[0]
    except AppErrorException as exc:
        _log.error("reverse-image query could not be embedded: {}",
                   exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - boundary, mirrors `search()`
        _log.error("reverse-image query could not be embedded: {}", exc)
        _note(problems, f"The query photo could not be embedded ({exc}).")
        return []

    eligible = _as_eligibility(allowed_file_ids)
    if eligible is not None and eligible.excludes_everything:
        return []

    try:
        pushdown = eligible.ids if eligible is not None else None
        if pushdown is not None and len(pushdown) <= MAX_PREFILTER_IDS:
            rows = image_vectors.search(query_vector, k=limit, where=_id_clause(pushdown))
        elif eligible is None:
            rows = image_vectors.search(query_vector, k=limit, where=None)
        else:
            rows = _search_large_filter(image_vectors, query_vector, limit, eligible)
    except AppErrorException as exc:
        _log.error("reverse-image ANN search failed: {}", exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - a vector-store hiccup must not
        _log.error("reverse-image ANN search failed: {}", exc)
        _note(problems, f"The vector store could not be searched ({exc}).")
        return []

    hits = [_normalise(row) for row in rows]
    for hit in hits:
        hit["chunk_id"] = f"img:{hit['file_id']}"
    return hits
