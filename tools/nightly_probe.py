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
  ladder_rung01_images_per_second
                              the OCR ladder's free rungs (filename, then the
                              256px thumbnail histogram) over a fixed set of
                              generated images, model-free.
  ladder_probe_ms             rung 2, the detection-only probe, per image, over
                              the generated photo-like images - needs the OCR
                              engine, so it is `null` (with a note) where that
                              cannot load.
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


LADDER_IMAGES = 60
LADDER_PROBE_IMAGES = 12


def _ladder_images(folder: Path) -> tuple[list[Path], list[Path]]:
    """Two kinds, both deterministic: white pages with a few dark lines (rung 1
    sends these straight to OCR) and busy colour "photos" (they fall through to
    rung 2). Neutral filenames, so rung 0 - which routes on names - stays out
    of the way of what is being timed."""
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(20260920)
    pages: list[Path] = []
    photos: list[Path] = []
    for number in range(LADDER_IMAGES // 2):
        page = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(page)
        for line in range(12):
            y = 100 + line * 60
            draw.rectangle([100, y, 1100 - rng.randrange(400), y + 14], fill="black")
        path = folder / f"scan-{number:03d}.png"
        page.save(path)
        pages.append(path)
        photo = Image.effect_noise((1600, 1200), 64).convert("RGB")
        tint = Image.new("RGB", photo.size, (rng.randrange(40, 160), rng.randrange(40, 160),
                                            rng.randrange(40, 160)))
        path = folder / f"view-{number:03d}.jpg"
        Image.blend(photo, tint, 0.5).save(path, quality=85)
        photos.append(path)
    return pages, photos


def measure_ladder(settings=None) -> dict:
    """Rungs 0-1 always; rung 2 when the OCR engine loads (see the docstring)."""
    import tempfile

    from app.extract.ocr_ladder import route

    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="leasha-ladder-") as tmp:
        pages, photos = _ladder_images(Path(tmp))
        everything = pages + photos
        route(everything[0], detect=None)                 # untimed: imports, caches
        started = time.perf_counter()
        for path in everything:
            route(path, detect=None)
        elapsed = max(time.perf_counter() - started, 1e-9)
        out["ladder_rung01_images_per_second"] = round(len(everything) / elapsed, 1)

        try:
            from app.extract import ocr
            if settings is not None:
                ocr.configure_device(settings.embed_device)
            engine = ocr._load_engine()
            if engine is None:
                raise RuntimeError("the OCR engine did not load")
            detect = ocr._detect_only(engine)
            sample = photos[:LADDER_PROBE_IMAGES]
            detect(sample[0])                             # untimed warm-up
            started = time.perf_counter()
            for path in sample:
                route(path, detect=detect)
            out["ladder_probe_ms"] = round(
                (time.perf_counter() - started) * 1000.0 / len(sample), 1)
        except Exception as exc:                          # noqa: BLE001 - report, keep going
            out["ladder_probe_ms"] = None
            out.setdefault("notes", []).append(
                f"ladder rung 2 not measured: {type(exc).__name__}: {exc}")
    return out


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
    for name, fn in (("ladder", lambda: measure_ladder(settings)),
                     ("embed", lambda: measure_embed(settings, texts)),
                     ("search", lambda: measure_search(settings))):
        try:
            result = fn()
            out["notes"].extend(result.pop("notes", []))
            out.update(result)
        except Exception as exc:                          # noqa: BLE001 - report, keep going
            out["notes"].append(f"{name} not measured: {type(exc).__name__}: {exc}")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
