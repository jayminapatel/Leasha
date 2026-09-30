r"""`app.cli watch`: the command-line entry point of the folder watch.

Layer: L3

Work order 0z, item F1. The watch itself is `test_folder_watch.py` and
`tests/integration/test_folder_watch_acceptance.py`; this is the command - its
arguments, its refusal, and the real process the window starts, spoken to the
way the window speaks to it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.cli import build_parser, main
from app.cli.watch import WATCH_EVENT, _Reporter
from app.core.errors import make_error


def test_watch_accepts_the_documented_flags() -> None:
    args = build_parser().parse_args(
        ["watch", "D:\\Docs", "E:\\More", "--backend", "poll", "--for", "30", "--quiet"])
    assert args.roots == ["D:\\Docs", "E:\\More"]
    assert args.backend == "poll" and args.seconds == 30.0 and args.quiet
    assert build_parser().parse_args(["watch"]).backend == "auto"


def test_no_folders_explains_what_to_type(capsys, temp_env: Path) -> None:
    code = main(["--env", str(temp_env), "watch"])
    err = capsys.readouterr().err
    assert code == 1
    assert "app.cli watch" in err and "Folders to index" in err


def test_what_a_person_is_told_is_words_and_what_the_window_is_told_is_json() -> None:
    import io

    words, lines = io.StringIO(), io.StringIO()
    update = {"indexed": 2, "removed": 1, "skipped": 0, "rescanned": 0,
              "seconds": 0.84, "names": ["report.docx"]}
    error = make_error("ERR_WATCH_FOLDER", "t", path=r"D:\Docs", reason="it has gone")

    person = _Reporter(words, machine=False, quiet=False)
    person.event("updated", update)
    person.event("pending", {"count": 3})
    person.event("problem", {"root": r"D:\Docs", "error": error})
    said = words.getvalue()
    assert "2 indexed, 1 removed in 0.8s: report.docx" in said
    assert "ERR_WATCH_FOLDER" in said and said.count("\n") >= 2

    window = _Reporter(lines, machine=True, quiet=False)
    window.event("updated", update)
    window.event("problem", {"root": r"D:\Docs", "error": error})
    first, second = (json.loads(line) for line in lines.getvalue().splitlines())
    assert first["event"] == WATCH_EVENT and first["kind"] == "updated"
    assert first["indexed"] == 2 and first["names"] == ["report.docx"]
    assert second["error"]["code"] == "ERR_WATCH_FOLDER" and second["error"]["suggestion"]


@pytest.mark.slow
def test_the_window_s_child_process_indexes_a_saved_file_and_stops_with_its_pipe(
        temp_env: Path, tmp_path: Path, project_root: Path) -> None:
    """The real process, as the window starts it: events on standard output,
    a saved file indexed, and the end of standard input ending it."""
    root = tmp_path / "Watched"
    root.mkdir()
    argv = [sys.executable, "-m", "app.cli", "--env", str(temp_env), "watch", str(root),
            "--events", "jsonl", "--fake-embedder-for-bench",
            "--lock-name", f"Leasha.Test.CliWatch.{uuid.uuid4().hex}"]
    env = dict(os.environ, PYTHONPATH=str(project_root), EMBED_DEVICE="cpu")
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, cwd=str(project_root), env=env)
    lines: "queue.Queue[str]" = queue.Queue()

    def pump() -> None:
        for raw in child.stdout:
            lines.put(raw.decode("ascii", "replace"))
        lines.put("")

    threading.Thread(target=pump, daemon=True).start()
    seen: list[dict] = []

    def wait_for(kind: str, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            seen.append(event)
            if event.get("kind") == kind:
                return event
        raise AssertionError(f"no {kind!r} event; saw {seen}")

    try:
        wait_for("ready", 180)
        (root / "saved.txt").write_text(
            "A file saved a moment ago about the orchard survey.", encoding="utf-8")
        updated = wait_for("updated", 120)
        assert updated["indexed"] == 1 and updated["names"] == ["saved.txt"]

        child.stdin.close()                      # the window has gone
        stopped = wait_for("stopped", 60)
        assert stopped["indexed"] == 1 and stopped["batches"] == 1
        assert child.wait(timeout=60) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)

    assert all(event["event"] == WATCH_EVENT for event in seen)
    from app.storage.sqlite_store import SqliteStore

    data = tmp_path / "index_data"
    with SqliteStore(data / "fts" / "knowledge.db") as store:
        assert store.get_file(str(root / "saved.txt")) is not None
        assert store.search_bm25('"orchard survey"')
