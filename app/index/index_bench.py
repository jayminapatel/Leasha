r"""Time this machine on the *whole* pipeline, not just the model.

Layer: L3

`embed_bench.py` answers "how fast is the model here", which turned out to be
the smaller half of the question. A run whose model is fast and whose disk is
slow is bounded by the disk, and a tuning screen that only knows the model
number will confidently recommend the graphics card to somebody who needs a
different drive. §5a asks for all three: extract, embed, write.

**A fixed synthetic workload, on purpose.** Benching the person's own corpus
would measure their corpus rather than their machine - a folder of scanned PDFs
and a folder of text files give numbers that differ by an order of magnitude
and say nothing about the hardware. The same generated text every time is the
only way two machines, or the same machine before and after a change, can be
compared at all.

**Under two minutes, and it says so before it starts.** The order's budget.
Everything here is sized to hit it on a slow laptop: a few hundred small
documents, a few hundred chunks embedded, and no model download - if the model
is not already present the bench reports that instead of fetching 130MB while
somebody waits.

Nothing here writes to the real index. The bench builds its own store in a
temporary folder and deletes it, because a benchmark that leaves rows behind is
a benchmark nobody runs twice.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["IndexBench", "run_index_bench", "DOCUMENTS", "WORDS_PER_DOCUMENT"]

_log = logger.bind(component="index.bench")

#: Documents in the synthetic corpus, and how long each is.
#:
#: 240 documents of 400 words is roughly 600KB of text and about 900 chunks -
#: enough that per-call overhead is amortised and the numbers are stable,
#: small enough that the slowest machine finishes inside the budget.
DOCUMENTS = 240
WORDS_PER_DOCUMENT = 400

#: The vocabulary the synthetic text is built from. Deliberately real words in
#: this project's own domain, not `lorem ipsum`: the chunker splits on
#: sentences and the tokeniser is English, so nonsense would measure a code
#: path nobody's corpus takes.
_VOCABULARY = (
    "pump station commissioning report valve replacement shutdown flow rate "
    "manifold safety induction training budget forecast quarterly maintenance "
    "schedule pressure vessel inspection certificate calibration record "
    "instrument loop drawing isolation permit contractor handover"
).split()


@dataclass
class IndexBench:
    """What the machine managed, per stage."""

    documents: int = 0
    chunks: int = 0
    #: Files a second through extraction and chunking, single-threaded. The
    #: per-worker rate: the pipeline runs several, and multiplying is the
    #: caller's business.
    extract_per_second: float = 0.0
    #: Chunks a second into SQLite.
    write_per_second: float = 0.0
    #: device -> chunks a second through the model.
    embed_per_second: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""
    #: Said out loud when a number should not be trusted - a busy machine, a
    #: model that had to be downloaded mid-bench. Silence would let a bad
    #: number be quoted for a year.
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "extract_per_second": round(self.extract_per_second, 1),
            "write_per_second": round(self.write_per_second, 1),
            "embed_per_second": {name: round(rate, 1)
                                 for name, rate in self.embed_per_second.items()},
            "seconds": round(self.seconds, 1),
            "error": self.error,
            "notes": list(self.notes),
        }

    def as_measured(self, fingerprint: str) -> Any:
        """The record §5b stores, labelled with the machine it came from."""
        from app.core.measured import Measured

        return Measured(
            fingerprint=fingerprint,
            source="bench",
            embed_per_second=dict(self.embed_per_second),
            extract_per_second=self.extract_per_second,
            write_per_second=self.write_per_second,
        )


def _corpus(root: Path) -> list[Path]:
    """Write the synthetic documents. Deterministic, so two runs compare."""
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    size = len(_VOCABULARY)
    for number in range(DOCUMENTS):
        words = [_VOCABULARY[(number * 7 + index) % size]
                 for index in range(WORDS_PER_DOCUMENT)]
        # Sentence breaks every dozen words, because the chunker looks for
        # them: one 400-word sentence would exercise the fallback path rather
        # than the one a real document takes.
        text = ""
        for index in range(0, len(words), 12):
            text += " ".join(words[index:index + 12]) + ". "
        path = root / f"bench{number:04d}.txt"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def run_index_bench(settings: Any, *, devices: Optional[tuple] = None,
                    embed_chunks: int = 512) -> IndexBench:
    r"""Extract, chunk, write and embed a fixed workload. Never raises.

    `devices` is which processors to time. `None` means "whichever the
    settings ask for", which is the honest default: benching a graphics card
    somebody has switched off tells them about a machine they are not running.
    Passing both is how the *Benchmark now* button answers "would the graphics
    card be faster here" - the one question §0 says the specification sheet
    cannot.
    """
    result = IndexBench()
    started = time.perf_counter()
    folder = Path(tempfile.mkdtemp(prefix="leasha-bench-"))
    try:
        paths = _corpus(folder / "corpus")
        result.documents = len(paths)

        texts = _time_extraction(paths, result)
        if result.error:
            return result
        _time_write(folder, texts, result)
        _time_embedding(settings, texts, devices, embed_chunks, result)
    except Exception as exc:                     # noqa: BLE001 - a benchmark
        # **A failed benchmark is a missing number, never a failed anything
        # else.** It runs from a button on a settings screen; an exception
        # reaching the window would make measuring feel dangerous, and nobody
        # would press it again.
        result.error = f"{type(exc).__name__}: {exc}"
        _log.warning("the index benchmark stopped: {}", exc)
    finally:
        result.seconds = time.perf_counter() - started
        shutil.rmtree(folder, ignore_errors=True)
    return result


def _time_extraction(paths: list[Path], result: IndexBench) -> list[str]:
    """Read and chunk, single-threaded, and keep the chunks for the rest."""
    from app.extract.base import extract
    from app.extract.chunker import chunk_text

    texts: list[str] = []
    started = time.perf_counter()
    for path in paths:
        # `extract` yields documents, not one document: an archive is many.
        # A plain `.txt` yields exactly one, and the loop costs nothing.
        for document in extract(path):
            texts.extend(chunk.text for chunk in chunk_text(document.text))
    seconds = time.perf_counter() - started

    result.chunks = len(texts)
    if seconds > 0:
        result.extract_per_second = len(paths) / seconds
    if not texts:
        result.error = "the benchmark corpus produced no text"
    return texts


def _time_write(folder: Path, texts: list[str], result: IndexBench) -> None:
    """Rows a second into SQLite, into a throwaway database.

    **Its own store, deleted afterwards.** Timing writes into the real index
    would leave several hundred fictional documents in somebody's search
    results, which is a far worse outcome than not knowing the number.
    """
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(folder / "bench.db").connect()
    try:
        started = time.perf_counter()
        file_id = store.upsert_file(
            str(folder / "bench.txt"), parent_dir=str(folder), ext="txt",
            size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [
            {"ordinal": index, "text": text} for index, text in enumerate(texts)
        ])
        seconds = time.perf_counter() - started
        if seconds > 0:
            result.write_per_second = len(texts) / seconds
    finally:
        store.close()


def _time_embedding(settings: Any, texts: list[str], devices: Optional[tuple],
                    embed_chunks: int, result: IndexBench) -> None:
    """Chunks a second through the model, per device asked for."""
    from app.index import backends
    from app.index.embedder import Embedder

    sample = texts[:embed_chunks]
    wanted = devices or (str(getattr(settings, "embed_device", "auto") or "auto"),)

    for device in wanted:
        embedder = Embedder.from_settings(settings, device=device)
        try:
            # **The load is excluded on purpose.** It happens once per run and
            # would otherwise smear across the first batch - which is exactly
            # how an earlier "59 passages a minute" figure came to look far
            # worse than the steady state it was meant to describe.
            embedder.warm_up()
        except Exception as exc:                 # noqa: BLE001
            result.notes.append(
                f"the model would not load for the {device}: {exc}")
            continue

        started = time.perf_counter()
        # **Consumed, not just called.** `embed_all` is a generator - lazy on
        # purpose, so a caller can write each batch as it arrives - and this
        # line used to be `embedder.embed_all(sample)` alone. That times the
        # creation of a generator, which is microseconds, so the stored rate
        # was 106,666,662 chunks a second and the tuning arithmetic read it as
        # "fast enough to double the batch". `deque(maxlen=0)` drains it
        # without holding a single vector.
        deque(embedder.embed_all(sample), maxlen=0)
        seconds = time.perf_counter() - started
        if seconds <= 0:
            continue

        choice = getattr(embedder, "choice", None)
        # Recorded under **what ran**, not what was asked for: `auto` on a
        # machine with no graphics card is a processor measurement, and filing
        # it as "auto" would make it useless to compare.
        name = getattr(choice, "device", None) or device
        result.embed_per_second[str(name)] = len(sample) / seconds
        if choice is not None and getattr(choice, "fell_back_from", ""):
            result.notes.append(choice.why)

    if not result.embed_per_second and not result.notes:
        result.notes.append("nothing could be embedded, so there is no rate")
