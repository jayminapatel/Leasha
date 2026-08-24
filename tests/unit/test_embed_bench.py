"""Measuring what embedding costs - and not measuring the wrong thing.

Layer: L3

This module exists because an estimate was wrong twice in a row, and the tests
here pin both mistakes so they cannot happen a third time.

**First**, 1.53 passages/second was called "twenty to sixty times too slow" on
the assumption that a 33M-parameter model should manage tens per second. True
for short sentences; false for the 512-token passages this application embeds.
The arithmetic predicts 1.5/sec. Nothing was misconfigured - the estimate was.

**Second**, the tool written to fix that reported `model.onnx 1112MB` for a
model whose full-precision build is 133MB. It had taken the largest `.onnx` in
the cache, and the cache also holds the reranker. It described one file and
timed another, which is worse than measuring nothing, because a diagnostic gets
believed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.index.embed_bench import (
    BenchResult,
    _infer_quantised,
    inspect_model,
    precision_of,
    project,
    providers,
)


def make_cache(tmp_path: Path, models: dict[str, int]) -> Path:
    """A model cache laid out the way huggingface_hub lays one out."""
    for repo, size in models.items():
        folder = tmp_path / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc"
        folder.mkdir(parents=True)
        target = folder / "model.onnx"
        target.write_bytes(b"\0")
        os.truncate(target, size)
    return tmp_path


# ---------------------------------------------------------------------------
# It must describe the model it is about to time
# ---------------------------------------------------------------------------

def test_the_reranker_is_not_mistaken_for_the_embedder(tmp_path):
    """The bug this file exists for.

    The cache holds both an embedder and a reranker. Taking the largest file
    reported 1112MB for a 133MB model, then timed a different one - and the
    number was quoted as evidence in a decision about which model to use.
    """
    cache = make_cache(tmp_path, {
        "BAAI/bge-small-en-v1.5": 133_000_000,
        "BAAI/bge-reranker-base": 1_112_000_000,
    })

    result = inspect_model(cache, BenchResult(model_name="BAAI/bge-small-en-v1.5"))

    # Against the model's own folder, not the whole path - pytest names its
    # temp directory after the test, so the path contains "reranker" no matter
    # which file was chosen.
    assert Path(result.model_file).parts[-4] == "models--BAAI--bge-small-en-v1.5"
    assert result.model_mb == pytest.approx(133, abs=1)


def test_the_largest_file_within_the_right_model_is_still_chosen(tmp_path):
    """A model folder holds tokenizer and config files too; the weights are the
    big one. Narrowing to the right folder must not lose that."""
    cache = make_cache(tmp_path, {"BAAI/bge-small-en-v1.5": 133_000_000})
    folder = next(cache.rglob("model.onnx")).parent
    small = folder / "model_quantized.onnx"
    small.write_bytes(b"\0")
    os.truncate(small, 1_000)

    result = inspect_model(cache, BenchResult(model_name="BAAI/bge-small-en-v1.5"))
    assert result.model_mb == pytest.approx(133, abs=1)


def test_nothing_is_reported_when_the_model_is_not_in_the_cache(tmp_path):
    """Sizes are not comparable across models, so reporting the wrong one is
    worse than reporting none. It says so rather than guessing."""
    cache = make_cache(tmp_path, {"BAAI/bge-reranker-base": 1_112_000_000})

    result = inspect_model(cache, BenchResult(model_name="BAAI/bge-small-en-v1.5"))

    assert result.model_file == ""
    assert "none in a folder naming" in result.error


def test_an_empty_cache_says_what_to_do(tmp_path):
    result = inspect_model(tmp_path, BenchResult(model_name="BAAI/bge-small-en-v1.5"))
    assert "no .onnx model found" in result.error
    assert "downloads" in result.error


# ---------------------------------------------------------------------------
# Reading a file size as a precision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("model", "size_mb", "expected"), [
    ("BAAI/bge-small-en-v1.5", 133, False),      # four bytes a weight: fp32
    ("BAAI/bge-small-en-v1.5", 130, False),
    ("BAAI/bge-small-en-v1.5", 33, True),        # one byte a weight: int8
    ("BAAI/bge-small-en-v1.5", 40, True),
    # fp16. Not int8, so the flag is False - and the *label* below is what a
    # caller should actually use, because "not int8" hides that this is already
    # half precision and an int8 build is worth two times rather than four.
    ("BAAI/bge-small-en-v1.5", 67, False),
    ("BAAI/bge-reranker-base", 435, False),
    ("BAAI/bge-reranker-base", 109, True),
    ("who/knows", 500, None),                    # unknown model, no claim
])
def test_size_reads_as_precision(model, size_mb, expected):
    """Answers "is it already quantised" without needing `onnx` installed.

    That matters because the answer decides whether a whole line of work is
    worth starting, and it should not depend on a dependency the user has no
    other reason to have.
    """
    assert _infer_quantised(model, size_mb) is expected


def test_an_unmeasurable_size_makes_no_claim():
    assert _infer_quantised("BAAI/bge-small-en-v1.5", 0) is None


# ---------------------------------------------------------------------------
# Providers and projection
# ---------------------------------------------------------------------------

def test_providers_never_raises_and_always_returns_the_result():
    """A diagnostic that fails when things are broken is worse than useless."""
    result = providers(BenchResult(model_name="x"))
    assert isinstance(result.available_providers, list)


def test_a_projection_needs_a_measured_rate_behind_it():
    assert project(3600, 1.0)["hours"] == pytest.approx(1.0)
    assert project(800_000, 4.42)["hours"] == pytest.approx(50.3, abs=0.5)


def test_a_zero_rate_does_not_divide_by_zero():
    assert project(1000, 0)["hours"] == float("inf")


# ---------------------------------------------------------------------------
# The arithmetic that should have been checked the first time
# ---------------------------------------------------------------------------

#: A stable run on the owner's machine - three passes per length, medians,
#: spreads under 10%. The earlier single-pass numbers swung 44% and the
#: conclusion drawn from them was wrong; these are what replaced them.
MEASURED = {128: 17.11, 256: 7.04, 512: 2.29}


def test_a_throughput_figure_is_meaningless_without_its_sequence_length():
    """The original wrong estimate, written down.

    The same model on the same machine differs by more than seven times between
    128 and 512 tokens. Quoting one figure as though it were a property of the
    model is what sent a week of attention at the wrong problem.
    """
    assert MEASURED[128] / MEASURED[512] > 5


def test_shorter_chunks_are_cheaper_per_token_not_merely_smaller():
    """**The correction.** Attention is quadratic, so the saving is real.

    An earlier version of this test asserted that halving the chunk size was
    roughly a wash - twice as many chunks at twice the speed - and said so on
    the strength of an unstable measurement. With three passes and a tight
    spread it is plainly false: per *token*, 256-token chunks run about 1.5
    times faster than 512-token ones and 128-token chunks about 1.9 times.

    That makes chunk size a real lever on indexing cost, and one that costs no
    accuracy in the embedding itself - only context per vector, which is a
    retrieval question rather than a throughput one.
    """
    def tokens_per_second(size: int) -> float:
        return size * MEASURED[size]

    assert tokens_per_second(256) / tokens_per_second(512) > 1.4
    assert tokens_per_second(128) / tokens_per_second(512) > 1.7
    # Monotonic: shorter is always cheaper per token at these lengths.
    rates = [tokens_per_second(size) for size in sorted(MEASURED, reverse=True)]
    assert rates == sorted(rates)


def test_the_projection_uses_tokens_because_that_is_what_a_corpus_has():
    """A corpus is a quantity of text. How it is cut into chunks is a choice,
    and quoting hours against a chunk count hides that the choice matters."""
    total_tokens = 800_000 * 512
    hours = {
        size: total_tokens / (size * MEASURED[size]) / 3600
        for size in MEASURED
    }
    assert hours[512] > hours[256] > hours[128]
    assert hours[512] / hours[128] > 1.7


# ---------------------------------------------------------------------------
# Precision, reported as a precision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("model", "size_mb", "expected"), [
    ("BAAI/bge-small-en-v1.5", 133, "fp32"),
    ("BAAI/bge-small-en-v1.5", 66, "fp16"),      # the real file on this project
    ("BAAI/bge-small-en-v1.5", 33, "int8"),
    ("BAAI/bge-reranker-base", 435, "fp32"),
    ("who/knows", 500, None),
])
def test_precision_is_reported_as_a_precision(model, size_mb, expected):
    """A label, not a quantised/not flag.

    The shipped model turned out to be a 66MB build of a 33M-parameter network -
    two bytes a weight, so fp16. A boolean called that "unclear", which is the
    wrong answer twice over: it is perfectly clear, and it means an int8 build is
    still available and worth roughly another two times.
    """
    assert precision_of(model, size_mb) == expected


# ---------------------------------------------------------------------------
# A measurement you can trust, or one that says you cannot
# ---------------------------------------------------------------------------

def test_a_wide_spread_is_reported_as_unstable():
    """Two runs on the same machine gave 4.42 and 2.47 for the same sequence
    length - a 44% swing. One pass cannot tell a slow model from four seconds
    of background load, and a projection built on it is fiction."""
    result = BenchResult(model_name="x")
    result.spread = {128: (17.2, 17.5), 512: (2.47, 4.42)}

    assert result.unstable == [512]


def test_a_tight_spread_is_trusted_silently():
    result = BenchResult(model_name="x")
    result.spread = {128: (17.2, 17.5), 256: (8.9, 9.0)}
    assert result.unstable == []


def test_an_unmeasured_length_is_not_called_unstable():
    assert BenchResult(model_name="x").unstable == []
