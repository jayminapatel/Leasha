"""Layer 6 — the optional LLM pass, and the Ollama client under it.

No Ollama is installed anywhere these tests run. Every path - healthy, refused,
timed out, prose instead of JSON, an invented taxonomy, a hallucinated chunk id -
is driven through the injected transport, which is the whole reason the client
has one.

The acceptance criterion these exist for is the third one in the spec: *killing
Ollama mid-enrichment pauses the job with ERR_OLLAMA_DOWN, it resumes later, and
no data is lost.* That is asserted directly in
`test_killing_ollama_midway_pauses_and_the_next_run_resumes`.
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import AppErrorException
from app.graph.builder import GraphBuilder
from app.graph.entities_llm import (
    EntityEnricher,
    VALID_KINDS,
    _canonical_kind,
    build_prompt,
    parse_reply,
)
from app.llm.ollama import OllamaClient
from app.storage.sqlite_store import SqliteStore

TEXTS = [
    "Acme Water Ltd completed the HACCP review at Barnsley Dairy.",
    "SCADA and MES at Barnsley Dairy, installed by Acme Water Ltd.",
    "Jenny Okonkwo signed the HACCP audit for Barnsley Dairy.",
    "Northgate Systems delivered the MES at Leeds Bakery.",
]


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        for ordinal, text in enumerate(TEXTS):
            file_id = opened.upsert_file(
                f"D:/docs/doc{ordinal}.txt", size_bytes=len(text), mtime_ns=1,
                ext="txt", parent_dir="D:/docs", source_kind="file",
            )
            opened.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                             "char_end": len(text), "page": None}])
        yield opened


def replying(body, *, fail_on: set[int] | None = None):
    """A transport that answers /api/tags and returns `body` from /api/generate."""
    state = {"calls": 0}
    fail_on = fail_on or set()

    def transport(method, url, payload, timeout):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "mistral"}]}
        state["calls"] += 1
        if state["calls"] in fail_on:
            raise ConnectionError("connection refused")
        if callable(body):
            return {"response": body(payload)}
        return {"response": body}

    transport.state = state
    return transport


def entities_for(payload):
    ids = [
        int(line[1:-1])
        for line in payload["prompt"].splitlines()
        if line.startswith("[") and line.endswith("]")
    ]
    return json.dumps({
        str(chunk_id): [
            {"name": "Barnsley Dairy", "kind": "Location"},
            {"name": "HACCP", "kind": "standards"},
        ]
        for chunk_id in ids
    })


# -- the client --------------------------------------------------------------

def test_health_is_false_rather_than_raising_when_ollama_is_off():
    def refuse(*_args, **_kwargs):
        raise ConnectionError("refused")

    assert OllamaClient(transport=refuse).health() is False


def test_generate_against_a_dead_ollama_raises_exactly_one_shape():
    def refuse(*_args, **_kwargs):
        raise ConnectionError("refused")

    with pytest.raises(AppErrorException) as caught:
        OllamaClient(transport=refuse).generate("hello")
    assert caught.value.error.code == "ERR_OLLAMA_DOWN"


def test_a_timeout_is_reported_as_ollama_down_not_as_a_timeout():
    """Callers handle one code. A second one is a path someone forgets to catch."""
    def hang(method, url, payload, timeout):
        if url.endswith("/api/tags"):
            return {"models": []}
        raise TimeoutError("read timed out")

    with pytest.raises(AppErrorException) as caught:
        OllamaClient(transport=hang).generate("hello")
    assert caught.value.error.code == "ERR_OLLAMA_DOWN"


def test_prose_where_json_was_asked_for_is_the_same_failure_to_the_caller():
    client = OllamaClient(transport=replying("Certainly! Here are the entities:"))
    with pytest.raises(AppErrorException) as caught:
        client.generate("x", json_mode=True).json()
    assert caught.value.error.code == "ERR_OLLAMA_DOWN"


def test_a_failed_call_invalidates_the_cached_health():
    """Otherwise a crash is followed by ten seconds of calls into a dead process."""
    transport = replying('{"ok": 1}', fail_on={1})
    client = OllamaClient(transport=transport)
    assert client.health() is True
    with pytest.raises(AppErrorException):
        client.generate("x")
    assert client._healthy_until == 0.0


def test_the_error_says_how_to_fix_it():
    def refuse(*_args, **_kwargs):
        raise ConnectionError("refused")

    error = OllamaClient(transport=refuse, model="mistral").down_error("refused")
    assert "ollama serve" in (error.action_payload or "")


# -- the prompt and the parser -----------------------------------------------

def test_the_prompt_names_every_allowed_kind():
    prompt = build_prompt([(1, "some text")])
    for kind in VALID_KINDS:
        assert kind in prompt


def test_the_prompt_truncates_a_passage_rather_than_the_instructions():
    """A dropped instruction is worse than a dropped sentence: the shape goes."""
    prompt = build_prompt([(1, "x" * 50_000)])
    assert "Reply with JSON only" in prompt
    assert len(prompt) < 10_000


@pytest.mark.parametrize(("given", "expected"), [
    ("Person", "person"),
    ("persons", "person"),
    ("Organisation", "org"),
    ("organization", "org"),
    ("company", "org"),
    ("LOCATION", "place"),
    ("standards", "standard"),
])
def test_the_taxonomy_is_pinned_to_the_closed_set(given, expected):
    assert _canonical_kind(given) == expected


def test_stripping_a_plural_never_mangles_a_valid_kind():
    """A blanket rstrip('s') turns 'person' into 'perso' - silently, in production."""
    for kind in VALID_KINDS:
        assert _canonical_kind(kind) == kind


def test_an_invented_kind_is_dropped_not_stored():
    assert parse_reply({"1": [{"name": "Thing", "kind": "widget"}]}, [1]) == {}


def test_a_reply_that_is_not_an_object_yields_nothing():
    assert parse_reply(["a", "b"], [1]) == {}
    assert parse_reply("no", [1]) == {}
    assert parse_reply(None, [1]) == {}


def test_entities_attributed_to_a_chunk_that_was_not_sent_are_discarded():
    """A model echoing the id from its own example would mislabel a real chunk."""
    assert parse_reply({"41": [{"name": "X", "kind": "org"}]}, [1, 2]) == {}


def test_one_malformed_row_does_not_cost_the_whole_batch():
    parsed = parse_reply({
        "1": [
            "not a dict",
            {"name": "", "kind": "org"},
            {"name": "y" * 500, "kind": "org"},
            {"name": "Acme Ltd", "kind": "org"},
        ],
    }, [1])
    assert parsed == {1: [("Acme Ltd", "org")]}


def test_ids_arrive_as_strings_and_as_bracketed_text():
    assert parse_reply({"[2]": [{"name": "Acme", "kind": "org"}]}, [2]) == {
        2: [("Acme", "org")]
    }


# -- the job -----------------------------------------------------------------

def test_with_ollama_off_nothing_is_attempted_and_nothing_breaks(store):
    def refuse(*_args, **_kwargs):
        raise ConnectionError("refused")

    GraphBuilder(store).build()
    before = store.graph_stats()

    result = EntityEnricher(store, OllamaClient(transport=refuse)).run()

    assert result.paused
    assert result.error is not None and result.error.code == "ERR_OLLAMA_DOWN"
    assert result.chunks_processed == 0
    assert store.graph_stats() == before, "an unavailable model must change nothing"


def test_enrichment_types_what_cooccurrence_only_guessed(store):
    GraphBuilder(store).build()
    before = {row["display"]: row["kind"] for row in store.top_entities(50)}
    assert before.get("Barnsley Dairy") == "name"

    EntityEnricher(
        store, OllamaClient(transport=replying(entities_for)), batch_chunks=2
    ).run()

    after = {row["display"]: (row["kind"], row["source"]) for row in store.top_entities(50)}
    assert after["Barnsley Dairy"] == ("place", "llm")
    assert after["HACCP"] == ("standard", "llm")


def test_enrichment_never_removes_what_cooccurrence_found(store):
    """The deterministic graph stays intact, so the LLM's effect stays reversible."""
    GraphBuilder(store).build()
    before = {row["display"] for row in store.top_entities(500)}

    EntityEnricher(store, OllamaClient(transport=replying(entities_for))).run()

    after = {row["display"] for row in store.top_entities(500)}
    assert before <= after


def test_killing_ollama_midway_pauses_and_the_next_run_resumes(store):
    """The spec's third acceptance criterion, asserted end to end."""
    GraphBuilder(store).build()
    client = OllamaClient(transport=replying(entities_for, fail_on={2}))

    first = EntityEnricher(store, client, batch_chunks=1).run()
    assert first.paused
    assert first.error.code == "ERR_OLLAMA_DOWN"
    assert 0 < first.chunks_processed < len(TEXTS)
    stopped_at = int(store.get_state("graph:llm_cursor"))

    second = EntityEnricher(
        store, OllamaClient(transport=replying(entities_for)), batch_chunks=1
    ).run()

    assert not second.paused
    assert first.chunks_processed + second.chunks_processed == len(TEXTS)
    assert int(store.get_state("graph:llm_cursor")) > stopped_at


def test_the_two_jobs_do_not_share_a_cursor(store):
    """They walk the same chunks at completely different speeds.

    One cursor between them means whichever ran last dictates where the other
    resumes, skipping everything in between without a word.
    """
    GraphBuilder(store).build()
    EntityEnricher(
        store, OllamaClient(transport=replying(entities_for, fail_on={2})), batch_chunks=1
    ).run()

    cooccurrence_cursor = int(store.get_state("graph:cursor"))
    llm_cursor = int(store.get_state("graph:llm_cursor"))
    assert llm_cursor < cooccurrence_cursor


def test_a_batch_the_model_cannot_parse_is_recorded_and_skipped(store):
    """Retrying it forever would stall the job on the same eight chunks."""
    GraphBuilder(store).build()
    result = EntityEnricher(
        store, OllamaClient(transport=replying('{"999": []}')), batch_chunks=2
    ).run()

    assert result.batches_failed > 0
    assert result.chunks_processed == len(TEXTS), "the job still finished"


def test_resetting_keeps_what_was_already_found(store):
    """Re-running with a better model must not cost the previous pass."""
    GraphBuilder(store).build()
    enricher = EntityEnricher(store, OllamaClient(transport=replying(entities_for)))
    enricher.run()
    typed = {row["display"] for row in store.top_entities(500) if row["source"] == "llm"}

    enricher.reset()

    assert store.get_state("graph:llm_cursor") == "0"
    assert {row["display"] for row in store.top_entities(500) if row["source"] == "llm"} == typed
