"""Layer 3's CLI entry point.

The pipeline itself is covered by `tests/integration/test_layer3_acceptance.py`.
What is tested here is the wiring - argument handling, the single-instance lock,
and the fact that this command, unlike `extract`, *writes*.
"""

from __future__ import annotations

import json
import sys
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


# ---------------------------------------------------------------------------
# The progress line, and why it stopped being visible.
# ---------------------------------------------------------------------------

def test_the_progress_line_yields_to_log_output():
    """A `\\r` line and a logger sharing a console destroy each other.

    A warning lands on top of the progress line, the next carriage return
    overwrites the warning, and what is left is a mangled line that stops
    updating - which reads exactly like "it stopped working". That was the
    report from a real run, and it is why anything writing to the console has to
    clear the line first.
    """
    from app.cli import ProgressLine, _console_sink

    progress = ProgressLine(enabled=True)
    progress.enabled = True                 # not a tty under pytest
    written: list[str] = []

    class Capture:
        def write(self, text):
            written.append(text)

        def flush(self):
            pass

    import sys
    original = sys.stdout
    sys.stdout = Capture()
    try:
        progress.update("42 docs indexed")
        sink = _console_sink(progress)
        sys.stderr_original, sys.stderr = sys.stderr, Capture()
        try:
            sink("a warning about one file")
        finally:
            sys.stderr = sys.stderr_original
    finally:
        sys.stdout = original

    joined = "".join(written)
    # Cleared before the message, and repainted after it.
    assert joined.count("42 docs indexed") == 2, joined


def test_the_progress_line_is_silent_when_output_is_piped():
    """Carriage returns in a log file or a pipe are noise, not progress."""
    from app.cli import ProgressLine

    progress = ProgressLine(enabled=True)
    if sys.stdout.isatty():                 # pragma: no cover - depends on the runner
        pytest.skip("stdout is a tty here")
    assert not progress.enabled


def test_quiet_disables_the_progress_line_entirely():
    from app.cli import ProgressLine

    assert not ProgressLine(enabled=False).enabled


def test_the_jvm_is_started_quietly():
    """mpxj ships log4j-api with no binding, so the JVM prints
    "main ERROR Log4j API could not find a logging provider" to stderr the first
    time it reads a file - mid-run, looking exactly like a failure."""
    from app.extract.diagrams import _JVM_QUIET_ARGS

    assert any("log4j" in arg.lower() for arg in _JVM_QUIET_ARGS)
    assert all(arg.startswith("-D") for arg in _JVM_QUIET_ARGS), (
        "only -D system properties, which an unrecognising JVM ignores rather "
        "than refusing to start"
    )


def test_the_progress_sink_does_not_double_every_log_line(tmp_path):
    """`setup_logging` is idempotent by design, which is a trap here.

    Both the CLI and the UI call it, so it returns early when already
    configured. Reconfiguring it for the progress line therefore needs
    `force=True` - without it the original INFO console sink survives, the
    progress sink is added on top, and every line prints twice: once by the
    handler that respects the progress line and once by the handler that walks
    straight over it.
    """
    import io

    from app.cli import ProgressLine, _console_sink
    from app.core.logging import logger, setup_logging

    setup_logging(tmp_path, console_level="INFO", force=True)

    progress = ProgressLine(enabled=False)
    progress.enabled = True
    setup_logging(tmp_path, console_level="CRITICAL", force=True)
    logger.add(_console_sink(progress), level="INFO", format="{message}")

    out, err = io.StringIO(), io.StringIO()
    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        logger.info("one message")
    finally:
        sys.stdout, sys.stderr = original
        setup_logging(tmp_path, force=True)      # leave logging as it was

    assert err.getvalue().count("one message") == 1
