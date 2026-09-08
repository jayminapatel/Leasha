r"""`app.search.rerank`: transient GPU device-removed recovery (2026-09-08).

`logs/runs/run-20260908-050751-window.log` (line 121-123): the same DXGI
device-removed event that broke the embedder can just as easily hit the
reranker's scoring call. Before this, `_ensure_scorer()`'s cached
`self._scorer` was never invalidated on a scoring failure, so every retry
left in `RERANK_FAILURE_BUDGET` kept hitting the identical dead session -
the budget counted down to zero without recovery ever getting a real chance.

Other reranker behaviour (windows, textless-hit handling, the budget itself)
is covered in `test_rerank_window.py` and `test_review_section_three.py`;
this file is scoped to the new transient-GPU treatment only.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.search.rerank import RERANK_FAILURE_BUDGET, Reranker
from app.search import rerank as rerank_module


class _FakeLog:
    """Records warning calls without touching the real loguru sink - a bound
    loguru `Logger` does not accept `monkeypatch.setattr("...:_log.warning",
    ...)` cleanly, so the whole `_log` object is swapped instead, the same
    approach `test_crash_reporting.py` uses for its own bound logger."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, template, *args) -> None:
        self.warnings.append(template.format(*args) if args else template)

_TRANSIENT_MESSAGE = (
    "Fail: [ONNXRuntimeError] : 1 : FAIL : ...DmlExecutionProvider... "
    "887A0005 The GPU device instance has been suspended. Use "
    "GetDeviceRemovedReason to determine the appropriate action."
)

_HITS = [{"chunk_id": 1, "text": "pump station"}]


def test_transient_gpu_scoring_failure_invalidates_the_scorer() -> None:
    def device_removed(_query, _passages):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    ranker = Reranker("model", enabled=True)
    ranker._scorer = device_removed
    ranker.choice = SimpleNamespace(is_gpu=True)

    results = ranker.rerank("pump", list(_HITS), terms=["pump"])

    assert results == _HITS, "a scoring failure must still return the fused order"
    assert ranker._scorer is None, "the dead scorer must be cleared"
    assert ranker.choice is None
    # One failure, well under the budget, must not disable reranking outright.
    assert ranker.available


def test_transient_gpu_failure_gets_the_accurate_reason(monkeypatch) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(rerank_module, "_log", fake_log)

    def device_removed(_query, _passages):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    ranker = Reranker("model", enabled=True)
    ranker._scorer = device_removed

    ranker.rerank("pump", list(_HITS), terms=["pump"])

    assert fake_log.warnings, "a transient GPU failure must still be warned about once"
    message = fake_log.warnings[0].lower()
    assert "graphics-card session" in message or "driver" in message
    assert "re-run the installer" not in message, \
        "the generic missing-model message must not be used for this cause"


def test_a_non_transient_scoring_failure_keeps_the_generic_treatment(monkeypatch) -> None:
    """Only the classified failure class gets the accurate message and the
    invalidate-and-retry treatment - an ordinary scoring failure (a
    malformed passage, a one-off library bug) must keep behaving exactly as
    it did before this change."""
    fake_log = _FakeLog()
    monkeypatch.setattr(rerank_module, "_log", fake_log)

    def explode(_query, _passages):
        raise ValueError("malformed passage")

    ranker = Reranker("model", enabled=True)
    ranker._scorer = explode
    ranker.choice = SimpleNamespace(is_gpu=True)

    ranker.rerank("pump", list(_HITS), terms=["pump"])

    assert ranker._scorer is explode, \
        "a non-transient failure must not invalidate the (still broken) scorer"
    assert ranker.choice is not None
    assert fake_log.warnings
    message = fake_log.warnings[0].lower()
    assert "re-run the installer" in message, \
        "the generic message must still be used for a non-transient cause"


def test_the_failure_budget_still_ends_a_genuinely_broken_session() -> None:
    """The safety net for a machine that is repeatedly, not transiently,
    broken must survive this change."""
    def device_removed(_query, _passages):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    ranker = Reranker("model", enabled=True)

    for _ in range(RERANK_FAILURE_BUDGET):
        ranker._scorer = device_removed          # each retry gets a "fresh" attempt
        ranker.rerank("pump", list(_HITS), terms=["pump"])

    assert not ranker.available, "the budget must eventually be spent"


def test_each_retry_within_the_budget_gets_a_fresh_scorer_rebuild(monkeypatch) -> None:
    """The proof that invalidation matters: after a transient failure, the
    *next* search's `_ensure_scorer()` call actually rebuilds - through a
    fake `TextCrossEncoder` that fails once then succeeds - rather than
    every retry hitting the identical dead session until the budget runs out.
    """
    calls = {"built": 0}

    class FlakyCrossEncoder:
        def __init__(self, model_name, cache_dir=None, **_kwargs):
            self._broken = calls["built"] == 0
            calls["built"] += 1

        def rerank(self, query, passages):
            if self._broken:
                raise RuntimeError(_TRANSIENT_MESSAGE)
            return [1.0 for _ in passages]

    monkeypatch.setattr(
        "fastembed.rerank.cross_encoder.TextCrossEncoder", FlakyCrossEncoder)

    ranker = Reranker("fake-reranker-model")

    first = ranker.rerank("pump", list(_HITS), terms=["pump"])
    assert first == _HITS, "the failed first search keeps the fused order"
    assert ranker._scorer is None

    second = ranker.rerank("pump", list(_HITS), terms=["pump"])
    assert second[0]["rerank_score"] == 1.0, "the second search must actually succeed"
    assert calls["built"] == 2, \
        "recovery must rebuild a fresh scorer, not reuse the dead one"
    assert ranker.available
