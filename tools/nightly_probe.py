r"""In-process measurements for the nightly loop - order 0m section 5a.

Layer: tooling. Run by `tools/nightly.py` as a subprocess (so the models load
in their own process, exactly as a CLI command's would), never imported by the
app:

    venv\Scripts\python.exe tools\nightly_probe.py warm    --env <.env>
    venv\Scripts\python.exe tools\nightly_probe.py measure --env <.env> --fixture <dir>

`warm` loads the embedding model once, untimed, so a first-ever download lands
in the shared model cache instead of inside a timed stage. `measure` prints one
JSON object on its last line:

  chunker_chunks_per_second   `chunk_text` over a fixed synthetic document set
                              (the same vocabulary `app/index/index_bench.py`
                              uses), single thread, model-free.
  chunker_chars_per_second    the same run, in characters.
  embed_chunks_per_second     `Embedder.embed_all` over chunks that came out of
                              that chunker, after an untimed warm-up batch.
  search_p95_ms / _p50_ms     `SearchEngine.search` over the index the nightly
                              just built from the fixture corpus - reranker
                              off, result cache off, models pre-warmed - the
                              same queries repeated, so the p95 is a real
                              tail and not one sample.

Anything that cannot be measured is reported as `null` with a reason in
`notes`; nothing here fabricates a number.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Real words from this project's own domain; same idea as `index_bench`.
_VOCABULARY = (
    "pump station commissioning report valve replacement shutdown flow rate "
    "manifold safety induction training budget forecast quarterly maintenance "
    "schedule pressure vessel inspection certificate calibration record "
    "instrument loop drawing isolation permit contractor handover"
).split()

DOCUMENTS = 120
WORDS_PER_DOCUMENT = 400
EMBED_CHUNKS = 48
SEARCH_REPEATS = 12

#: Queries chosen from the fixture corpus's own text (`tests/fixtures/
#: generate.py`), so most of them find something - a p95 over searches that
#: return nothing would time the cheapest path.
SEARCH_QUERIES = (
    "commissioning report pump station",
    "isolation valves shutdown",
    "flow rates manifold",
    "survey concluded March",
    "quarterly review handover",
    "centrifugal pump cost",
    "vibration within tolerance",
    "site survey notes northern plant",
)


def _documents() -> list[str]:
    size = len(_VOCABULARY)
    docs = []
    for number in range(DOCUMENTS):
        words = [_VOCABULARY[(number * 7 + i) % size] for i in range(WORDS_PER_DOCUMENT)]
        docs.append("".join(" ".join(words[i:i + 12]) + ". " for i in range(0, len(words), 12)))
    return docs


def measure_chunker() -> dict:
    from app.extract.chunker import chunk_text

    docs = _documents()
    chunk_text(docs[0])                               # untimed: imports, caches
    started = time.perf_counter()
    chunks = []
    for doc in docs:
        chunks.extend(chunk_text(doc))
    elapsed = max(time.perf_counter() - started, 1e-9)
    chars = sum(len(d) for d in docs)
    return {
        "chunker_chunks_per_second": round(len(chunks) / elapsed, 1),
        "chunker_chars_per_second": round(chars / elapsed),
        "_chunks": [c.text for c in chunks],
    }


def _settings(env: Path):
    from app.core.config import load_settings
    return load_settings(env)


def measure_embed(settings, texts: list[str]) -> dict:
    from app.index.embedder import Embedder

    embedder = Embedder.from_settings(settings)
    embedder.warm_up()
    sample = texts[:EMBED_CHUNKS]
    list(embedder.embed_all(sample[:embedder.batch_size]))   # untimed warm-up batch
    started = time.perf_counter()
    count = sum(1 for _ in embedder.embed_all(sample))
    elapsed = max(time.perf_counter() - started, 1e-9)
    return {"embed_chunks_per_second": round(count / elapsed, 2), "_embed_n": count}


def measure_search(settings) -> dict:
    from app.index.embedder import Embedder
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    embedder = Embedder.from_settings(settings)
    reranker = Reranker.from_settings(settings, enabled=False)
    samples: list[float] = []
    hits = 0
    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        engine = SearchEngine(store, vectors, embedder, reranker=reranker, log_usage=False)
        try:
            engine.warm_up()
            engine.search(SEARCH_QUERIES[0], use_cache=False, rerank=False)   # untimed
            for _ in range(SEARCH_REPEATS):
                for query in SEARCH_QUERIES:
                    started = time.perf_counter()
                    response = engine.search(query, use_cache=False, rerank=False)
                    samples.append((time.perf_counter() - started) * 1000.0)
                    hits += 1 if response.results else 0
        finally:
            engine.close()
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return {
        "search_p95_ms": round(p95, 1),
        "search_p50_ms": round(statistics.median(samples), 1),
        "_search_n": len(samples),
        "_search_queries_with_results": hits,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("warm", "measure"))
    parser.add_argument("--env", required=True)
    parser.add_argument("--fixture", default="")
    args = parser.parse_args(argv)
    settings = _settings(Path(args.env))

    if args.mode == "warm":
        from app.index.embedder import Embedder
        Embedder.from_settings(settings).warm_up()
        print(json.dumps({"warmed": True}))
        return 0

    out: dict = {"notes": []}
    chunker = measure_chunker()
    texts = chunker.pop("_chunks")
    out.update(chunker)
    for name, fn in (("embed", lambda: measure_embed(settings, texts)),
                     ("search", lambda: measure_search(settings))):
        try:
            out.update(fn())
        except Exception as exc:                          # noqa: BLE001 - report, keep going
            out["notes"].append(f"{name} not measured: {type(exc).__name__}: {exc}")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
