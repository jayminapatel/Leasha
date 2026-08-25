"""What reranking actually costs, per model, on this machine.

Layer: L4

Reranking was 8.3 seconds of a 9-second search - 93% of it, against a spec
budget of 300ms warm. Three things multiply into that number: how many
candidates are scored, how long each passage is, and which model reads them.
This measures all three so the choice is made from data.

**Written because four throughput claims in this project were wrong.** Every one
had the same cause: a number quoted without the conditions that produced it.
This reports the conditions alongside the result, runs three passes and shows
the spread, and refuses to compare a model it could not load.

Nothing here is on the search path. It downloads models on request, which is
exactly why it is a command somebody runs deliberately rather than something the
application does on its own.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.search.window import RERANK_WINDOW_CHARS, windows_for

__all__ = ["ModelTiming", "BenchResult", "measure", "CANDIDATES", "SAMPLE_QUERY"]

#: Models worth comparing, smallest first. Sizes are fastembed's own figures.
#:
#: `bge-reranker-base` is 1.04GB and was the default; `ms-marco-MiniLM-L-6-v2`
#: is 0.08GB - thirteen times smaller - and is the standard choice for exactly
#: this job. Whether the quality difference matters on one particular corpus is
#: a question for `app.cli evaluate`, not for this file.
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Xenova/ms-marco-MiniLM-L-6-v2", "0.08 GB"),
    ("Xenova/ms-marco-MiniLM-L-12-v2", "0.12 GB"),
    ("jinaai/jina-reranker-v1-tiny-en", "0.13 GB"),
    ("BAAI/bge-reranker-base", "1.04 GB"),
)

#: A real query with a filter word, a name and a noun, because a one-word query
#: is not what anybody types and would flatter every model equally.
SAMPLE_QUERY = "a project schedule file for the licence renewal"

#: Passes per measurement. One pass cannot tell a slow model from four seconds
#: of background load - the mistake that produced a 44% swing between two runs
#: earlier in this project and a conclusion drawn from the noisier one.
PASSES = 3


@dataclass
class ModelTiming:
    name: str
    size: str = ""
    #: Seconds per full rerank of `count` passages, one per pass.
    passes: list[float] = field(default_factory=list)
    load_s: float = 0.0
    error: str = ""

    @property
    def median_s(self) -> float:
        return statistics.median(self.passes) if self.passes else 0.0

    @property
    def spread(self) -> tuple[float, float]:
        return (min(self.passes), max(self.passes)) if self.passes else (0.0, 0.0)

    @property
    def unstable(self) -> bool:
        """A spread this wide means the machine, not the model, was measured."""
        low, high = self.spread
        return bool(self.passes) and low > 0 and (high - low) / low > 0.25

    @property
    def per_passage_ms(self) -> float:
        return 0.0


@dataclass
class BenchResult:
    """Every condition, beside every number. See the module docstring."""

    count: int
    window_chars: int
    mean_passage_chars: int
    timings: list[ModelTiming] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates_scored": self.count,
            "window_chars": self.window_chars,
            "mean_passage_chars": self.mean_passage_chars,
            "models": [
                {
                    "model": timing.name,
                    "size": timing.size,
                    "median_s": round(timing.median_s, 3),
                    "per_passage_ms": round(timing.median_s / max(1, self.count) * 1000, 1),
                    "load_s": round(timing.load_s, 2),
                    "spread_s": [round(v, 3) for v in timing.spread],
                    "unstable": timing.unstable,
                    "error": timing.error,
                }
                for timing in self.timings
            ],
        }


def sample_passages(store: Any, count: int) -> list[str]:
    """Real chunks from the index, or synthetic ones if there is no index.

    Real text matters: a cross-encoder's cost follows the token count, and
    lorem ipsum tokenises differently from an email thread full of paths,
    signatures and quoted replies.
    """
    if store is not None:
        try:
            rows = store.conn.execute(
                "SELECT text FROM chunks WHERE length(text) > 200 LIMIT ?", (count,)
            ).fetchall()
            texts = [str(row["text"]) for row in rows]
            if len(texts) >= max(2, count // 2):
                return texts
        except Exception:                        # noqa: BLE001 - fall through
            pass

    filler = (
        "The project schedule was circulated to the team on Friday and the "
        "licence renewal paperwork is attached for review before the meeting. "
    )
    return [filler * 8 for _ in range(count)]


def measure(
    store: Any = None,
    *,
    models: Sequence[str] = (),
    count: int = 30,
    window_chars: int = RERANK_WINDOW_CHARS,
    cache_dir: Optional[str] = None,
    passes: int = PASSES,
    query: str = SAMPLE_QUERY,
) -> BenchResult:
    """Time a full rerank of `count` passages, for each model, `passes` times."""
    passages = sample_passages(store, count)
    mean_chars = int(sum(len(p) for p in passages) / max(1, len(passages)))

    # Windowed exactly as the search does it, so the measurement is of the work
    # the application actually performs rather than of a different job.
    scored = windows_for(passages, query.split(), width=window_chars)

    wanted = list(models) or [name for name, _size in CANDIDATES]
    sizes = dict(CANDIDATES)
    result = BenchResult(
        count=len(scored), window_chars=window_chars, mean_passage_chars=mean_chars)

    for name in wanted:
        timing = ModelTiming(name=name, size=sizes.get(name, ""))
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            started = time.perf_counter()
            model = TextCrossEncoder(model_name=name, cache_dir=cache_dir)
            # The first call is what actually loads the weights, so it is timed
            # separately - reporting it as query cost would triple every figure.
            list(model.rerank(query, scored[:1]))
            timing.load_s = time.perf_counter() - started

            for _ in range(max(1, passes)):
                mark = time.perf_counter()
                list(model.rerank(query, scored))
                timing.passes.append(time.perf_counter() - mark)
        except Exception as exc:                 # noqa: BLE001 - a bench, not a search
            timing.error = f"{type(exc).__name__}: {exc}"[:200]
        result.timings.append(timing)

    return result
