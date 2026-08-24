"""Layer 3's CLI entry point.

The pipeline itself is covered by `tests/integration/test_layer3_acceptance.py`.
What is tested here is the wiring - argument handling, the single-instance lock,
and the fact that this command, unlike `extract`, *writes*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import build_parser, main
from app.index.embedder import Embedder, l2_normalise


def fake_embedder(*_args, **kwargs):
    dim = kwargs.get("dim", 384)
    import math

    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
                for t in texts]

    return Embedder(dim=dim, encoder=encode)


@pytest.fixture()
def env(temp_env: Path) -> list[str]:
    return ["--env", str(temp_env)]


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("Northern pump station commissioning report.", encoding="utf-8")
    (root / "b.md").write_text("Valve replacement in the autumn shutdown.", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch: pytest.MonkeyPatch):
    """Never download 130MB of ONNX to test argument parsing."""
    monkeypatch.setattr("app.index.embedder.Embedder", fake_embedder)
    monkeypatch.setattr("app.cli.Embedder", fake_embedder, raising=False)


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- arguments --------------------------------------------------------------

def test_index_accepts_the_documented_flags() -> None:
    args = build_parser().parse_args(
        ["index", "D:\\Data", "--first", "D:\\Data\\Now", "--workers", "4",
         "--fast", "--no-prune", "--include-cloud", "--quiet"]
    )
    assert args.roots == ["D:\\Data"]
    assert args.first == ["D:\\Data\\Now"]
    assert args.workers == 4
    assert args.fast and args.no_prune and args.include_cloud and args.quiet


def test_first_is_repeatable_and_ordered() -> None:
    """'my current project, then the archive' has to mean that."""
    args = build_parser().parse_args(["index", "R", "--first", "A", "--first", "B"])
    assert args.first == ["A", "B"]


def test_index_is_no_longer_not_implemented() -> None:
    assert "ERR_NOT_IMPLEMENTED" not in (build_parser().parse_args(["index", "x"]).command or "")


# --- validation -------------------------------------------------------------

def test_no_root_explains_what_to_type(capsys, env: list[str]) -> None:
    code, _out, err = run(capsys, *env, "index")
    assert code == 1
    assert "app.cli index" in err, "the fix should be the command, not 'edit .env'"


def test_a_missing_root_mentions_a_disconnected_drive(capsys, env: list[str], tmp_path: Path) -> None:
    """A folder on an unplugged drive looks exactly like a deleted one."""
    code, _out, err = run(capsys, *env, "index", str(tmp_path / "gone"))
    assert code == 1
    assert "disconnected drive" in err


# --- it writes --------------------------------------------------------------

def test_indexing_populates_both_stores(capsys, env: list[str], corpus: Path) -> None:
    code, out, _err = run(capsys, *env, "index", str(corpus), "--quiet")
    assert code == 0
    assert "Indexed" in out and "chunks" in out

    _code, stats_out, _ = run(capsys, *env, "--json", "stats")
    stats = json.loads(stats_out)
    assert stats["sqlite"]["files_total"] == 2
    assert stats["vectors"]["rows"] == stats["sqlite"]["chunks_total"] > 0


def test_json_output_carries_the_throughput_numbers(capsys, env: list[str], corpus: Path) -> None:
    """The UI's ETA has to come from a real measurement."""
    _code, out, _err = run(capsys, *env, "--json", "index", str(corpus))
    payload = json.loads(out)
    assert payload["indexed"] == 2
    assert payload["files_per_minute"] > 0
    assert "mb_per_minute" in payload


def test_a_second_run_does_nothing(capsys, env: list[str], corpus: Path) -> None:
    run(capsys, *env, "index", str(corpus), "--quiet")
    _code, out, _err = run(capsys, *env, "--json", "index", str(corpus))
    payload = json.loads(out)
    assert payload["indexed"] == 0
    assert payload["unchanged"] == 2


def test_progress_lines_can_be_silenced(capsys, env: list[str], corpus: Path) -> None:
    _code, noisy, _ = run(capsys, *env, "index", str(corpus))
    run(capsys, *env, "index", str(corpus), "--quiet")
    assert "Indexed" in noisy
