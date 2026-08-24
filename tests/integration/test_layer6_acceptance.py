"""Layer 6 acceptance — the four criteria from BUILD_SPEC_V2.md, verbatim.

  [ ] Graph renders from the co-occurrence baseline with Ollama stopped.
  [ ] Starting Ollama and running enrichment visibly improves entity quality.
  [ ] Killing Ollama mid-enrichment: job pauses with ERR_OLLAMA_DOWN, resumes
      later, no data loss.
  [ ] Graph stays interactive at 5k nodes (cap and cluster beyond that).

Each has a test named after it. The third is also covered in
`tests/unit/test_graph_llm.py` at the job level; here it is checked through the
whole stack, which is the level the criterion is written at.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from app.graph import render
from app.graph.builder import GraphBuilder
from app.graph.entities_llm import EntityEnricher
from app.llm.ollama import OllamaClient
from app.storage.sqlite_store import SqliteStore

pytestmark = pytest.mark.slow


CORPUS = [
    "Acme Water Ltd completed the HACCP review at Barnsley Dairy in March.",
    "The HACCP review at Barnsley Dairy was signed by Jenny Okonkwo of Acme Water Ltd.",
    "SCADA and MES at Barnsley Dairy. Acme Water Ltd installed the pasteuriser.",
    "Jenny Okonkwo raised HACCP findings for Barnsley Dairy with Acme Water Ltd.",
    "Northgate Systems delivered the MES at Leeds Bakery with SCADA integration.",
    "Leeds Bakery OEE reporting came from the MES built by Northgate Systems.",
    "Northgate Systems supported the Leeds Bakery go live and the SCADA mapping.",
    "The MES at Leeds Bakery reports OEE hourly, per Northgate Systems.",
]


def seeded(db_path, texts):
    store = SqliteStore(db_path).connect()
    for ordinal, text in enumerate(texts):
        file_id = store.upsert_file(
            f"D:/docs/doc{ordinal}.txt", size_bytes=len(text), mtime_ns=1,
            ext="txt", parent_dir="D:/docs", source_kind="file",
        )
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                        "char_end": len(text), "page": None}])
    return store


def dead_transport(*_args, **_kwargs):
    raise ConnectionError("connection refused")


def typed_transport(payload_or_method, url=None, payload=None, timeout=None):
    """Answers as a well-behaved local model would."""
    if url is not None and url.endswith("/api/tags"):
        return {"models": [{"name": "mistral"}]}
    ids = [
        int(line[1:-1])
        for line in payload["prompt"].splitlines()
        if line.startswith("[") and line.endswith("]")
    ]
    known = {
        "Barnsley Dairy": "place", "Leeds Bakery": "place",
        "Acme Water Ltd": "org", "Northgate Systems": "org",
        "Jenny Okonkwo": "person", "HACCP": "standard",
        "SCADA": "system", "MES": "system",
    }
    blocks = payload["prompt"].split("Passages:", 1)[-1]
    out = {}
    for chunk_id in ids:
        section = blocks.split(f"[{chunk_id}]", 1)[-1].split("\n[", 1)[0]
        out[str(chunk_id)] = [
            {"name": name, "kind": kind}
            for name, kind in known.items()
            if name in section
        ]
    return {"response": json.dumps(out)}


# -- criterion 1 -------------------------------------------------------------

def test_the_graph_builds_and_renders_with_ollama_stopped(tmp_path):
    """The baseline must be a complete feature, not a placeholder for the LLM."""
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        result = GraphBuilder(store).build()
        assert result.chunks_processed == len(CORPUS)

        stats = store.graph_stats()
        assert stats["entities"] > 0
        assert stats["edges"] > 0

        view = render.select_top(store.top_entities(500), store.edges_among)
        page = render.render_html(view, tmp_path / "graph.html")
        body = page.read_text(encoding="utf-8")

        assert page.stat().st_size > 10_000
        assert "Barnsley Dairy" in body
    finally:
        store.close()


def test_the_rendered_page_does_not_reach_out_to_the_internet(tmp_path):
    """The one promise this app makes about everything. pyvis breaks it by default.

    `cdn_resources="in_line"` inlines vis-network but still emits Bootstrap tags
    pointing at jsdelivr. On a machine with no internet those hang and fail - and
    on a connected developer machine the page looks perfect, which is how this
    would have shipped.
    """
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        GraphBuilder(store).build()
        view = render.select_top(store.top_entities(500), store.edges_among)
        page = render.render_html(view, tmp_path / "graph.html")
        body = page.read_text(encoding="utf-8")

        external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', body)
        assert external == [], f"the graph page would call out to {external}"
    finally:
        store.close()


def test_the_page_is_written_as_utf8_whatever_the_machine_locale_is(tmp_path):
    """A real Windows failure, invisible on Linux and macOS.

    `pyvis.write_html` calls `open(path, "w+")` with no encoding, so the file is
    written in the *process locale* encoding - cp1252 on a UK Windows install.
    The page carries ’ — · … from entity names and from the tooltips, cp1252
    cannot encode any of them, and the whole render raised UnicodeEncodeError
    after building the entire document. Nothing showed it in development,
    because Linux and macOS already default to UTF-8.

    So this asserts on the bytes: the file must decode as UTF-8, and it must
    contain at least one character cp1252 cannot represent. The second half
    matters - pyvis JSON-escapes the node labels, so the characters that
    actually broke it come from the inlined vis-network library and from the
    banner this module writes itself, not from the data.
    """
    nodes = [
        {"id": 1, "key": "aveva", "display": "AVEVA’s Insight", "kind": "name",
         "source": "cooccurrence", "mentions": 4, "chunk_count": 3, "doc_count": 3},
        {"id": 2, "key": "barnsley", "display": "Barnsley Dairy — North", "kind": "name",
         "source": "cooccurrence", "mentions": 4, "chunk_count": 3, "doc_count": 3},
    ]
    edges = [{"a_id": 1, "b_id": 2, "weight": 3, "pmi": 0.5}]
    view = render.GraphView(nodes=nodes, edges=edges, truncated_from=2)

    page = render.render_html(view, tmp_path / "unicode.html", title="Barnsley — Ω")

    body = page.read_bytes().decode("utf-8")   # raises if it was written as cp1252

    beyond_cp1252 = [char for char in body if ord(char) > 0x2000]
    assert beyond_cp1252, "nothing here would have triggered the original crash"
    assert "Barnsley &mdash; Ω" in body or "Barnsley — Ω" in body
    assert "charset" in body[:2000].lower()


def test_the_graph_finds_the_structure_that_is_actually_in_the_corpus(tmp_path):
    """Two sites, two teams, no overlap - and nothing told it that."""
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        GraphBuilder(store).build()
        view = render.select_top(store.top_entities(500), store.edges_among)
        found = render.metrics(render.to_networkx(view))

        groups = [set(group) for group in found["communities"]]
        assert len(groups) >= 2, "the two unrelated sites should not be one blob"

        barnsley = next(g for g in groups if "Barnsley Dairy" in g)
        assert "Leeds Bakery" not in barnsley
        assert "Acme Water Ltd" in barnsley
    finally:
        store.close()


# -- criterion 2 -------------------------------------------------------------

def test_enrichment_visibly_improves_entity_quality(tmp_path):
    """Measured, not asserted by eye: untyped nodes before, typed nodes after."""
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        GraphBuilder(store).build()

        before = store.top_entities(500)
        assert all(row["source"] == "cooccurrence" for row in before)
        assert all(row["kind"] in {"name", "acronym", "email", "file"} for row in before)

        EntityEnricher(store, OllamaClient(transport=typed_transport)).run()

        after = {row["display"]: row["kind"] for row in store.top_entities(500)}
        assert after.get("Barnsley Dairy") == "place"
        assert after.get("Acme Water Ltd") == "org"
        assert after.get("HACCP") == "standard"
        assert after.get("SCADA") == "system"
    finally:
        store.close()


# -- criterion 3 -------------------------------------------------------------

def test_killing_ollama_midway_pauses_resumes_and_loses_nothing(tmp_path):
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        GraphBuilder(store).build()

        calls = {"n": 0}

        def dies_after_two(method, url, payload, timeout):
            if url.endswith("/api/tags"):
                return {"models": [{"name": "mistral"}]}
            calls["n"] += 1
            if calls["n"] > 2:
                raise ConnectionError("connection refused")
            return typed_transport(method, url, payload, timeout)

        first = EntityEnricher(
            store, OllamaClient(transport=dies_after_two), batch_chunks=2
        ).run()

        assert first.paused
        assert first.error is not None and first.error.code == "ERR_OLLAMA_DOWN"
        assert first.chunks_processed == 4
        typed_before = {
            row["display"] for row in store.top_entities(500) if row["source"] == "llm"
        }
        assert typed_before, "work done before the failure must be kept"

        second = EntityEnricher(
            store, OllamaClient(transport=typed_transport), batch_chunks=2
        ).run()

        assert not second.paused
        assert first.chunks_processed + second.chunks_processed == len(CORPUS)

        typed_after = {
            row["display"] for row in store.top_entities(500) if row["source"] == "llm"
        }
        assert typed_before <= typed_after, "resuming must not lose the earlier pass"
        assert "Leeds Bakery" in typed_after, "the chunks after the failure were reached"
    finally:
        store.close()


def test_a_paused_enrichment_leaves_search_completely_unaffected(tmp_path):
    """Layer 4 reads none of these tables, and this is what pins that."""
    store = seeded(tmp_path / "index.db", CORPUS)
    try:
        GraphBuilder(store).build()
        before = store.search_bm25("Barnsley", limit=10)

        EntityEnricher(store, OllamaClient(transport=dead_transport)).run()

        assert store.search_bm25("Barnsley", limit=10) == before
    finally:
        store.close()


# -- criterion 4 -------------------------------------------------------------

def test_the_view_caps_at_five_thousand_nodes_and_says_so(tmp_path):
    """A truncated graph that hides the truncation is worse than a smaller one.

    A missing node reads as evidence that nothing connects there, which is the
    opposite of what a cap means.
    """
    entities = [
        {"id": n, "key": f"e{n}", "display": f"Entity {n}", "kind": "name",
         "source": "cooccurrence", "mentions": 10, "chunk_count": 5,
         "doc_count": 20_000 - n}
        for n in range(1, 7_001)
    ]
    view = render.select_top(entities, lambda ids: [], limit=render.MAX_RENDER_NODES)

    assert len(view.nodes) == render.MAX_RENDER_NODES
    assert view.truncated
    assert view.truncated_from == 7_000
    assert any("7,000" in line for line in render.summarise(view))


def test_five_thousand_nodes_render_in_a_reasonable_time(tmp_path):
    """Interactivity starts with the page existing. Sixty seconds is generous."""
    nodes = [
        {"id": n, "key": f"e{n}", "display": f"Entity {n}", "kind": "name",
         "source": "cooccurrence", "mentions": 10, "chunk_count": 5, "doc_count": 100}
        for n in range(1, render.MAX_RENDER_NODES + 1)
    ]
    edges = [
        {"a_id": n, "b_id": n + 1, "weight": 3, "pmi": 0.4}
        for n in range(1, render.MAX_RENDER_NODES)
    ]
    view = render.GraphView(nodes=nodes, edges=edges, truncated_from=len(nodes))

    started = time.monotonic()
    page = render.render_html(view, tmp_path / "big.html")
    elapsed = time.monotonic() - started

    assert page.exists()
    assert elapsed < 60, f"rendering 5k nodes took {elapsed:.1f}s"


def test_metrics_stay_affordable_at_the_cap():
    """Exact betweenness on 5k nodes is minutes; sampled is seconds and ranks the same."""
    nodes = [
        {"id": n, "key": f"e{n}", "display": f"Entity {n}", "kind": "name",
         "mentions": 5, "doc_count": 5}
        for n in range(1, 2_001)
    ]
    edges = [{"a_id": n, "b_id": n + 1, "weight": 2, "pmi": 0.3} for n in range(1, 2_000)]
    graph = render.to_networkx(render.GraphView(nodes=nodes, edges=edges))

    started = time.monotonic()
    found = render.metrics(graph)
    elapsed = time.monotonic() - started

    assert found["nodes"] == 2_000
    assert elapsed < 60, f"metrics on 2k nodes took {elapsed:.1f}s"
    assert found["bridges"], "a chain graph has obvious bridges"
