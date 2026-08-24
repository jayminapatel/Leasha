"""The session recorder: what it captures, and what it must never capture.

Layer: L5

Two things are being pinned here, and the second matters more than the first.

**It must not be able to break anything.** A recorder that raises would turn a
small bug into a crash, and it would do it inside the handler for the bug you
were trying to catch. Every method swallows everything, and that is tested by
feeding it objects designed to misbehave.

**It must not record content.** A file full of somebody's actual searches is a
liability, and a recorder nobody dares send is a recorder that does nothing. So
the tests assert the *absence* of query text and file names, not just the
presence of the counters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ui.debug_recorder import (
    MAX_EVENTS,
    DebugRecorder,
    NullRecorder,
    recorder_for,
)


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- off by default ----------------------------------------------------------

def test_recording_is_off_unless_asked_for(tmp_path):
    """This application's whole promise is that nothing leaves the machine. A
    tool that watches by default is one people stop trusting."""
    recorder = recorder_for(tmp_path, enabled=False)

    assert isinstance(recorder, NullRecorder)
    assert not recorder.enabled
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_the_null_recorder_accepts_every_call():
    """A null object rather than `if recorder is not None:` at forty call sites -
    the check forgotten once is a crash in the code that exists to find crashes."""
    recorder = NullRecorder()
    recorder.event("anything", whatever=object(), nested={"a": [1, 2]})
    recorder.close()


def test_enabling_it_writes_one_file_under_sessions(tmp_path):
    recorder = recorder_for(tmp_path, enabled=True)
    recorder.event("tab", name="Search")
    recorder.close()

    files = list((tmp_path / "sessions").glob("session-*.jsonl"))
    assert len(files) == 1
    kinds = [event["kind"] for event in events(files[0])]
    assert kinds == ["session_start", "tab", "session_end"]


# -- what a reader needs -----------------------------------------------------

def test_the_first_event_carries_the_environment(tmp_path):
    """Nothing after it makes sense without the Python, Qt and platform it ran
    on - and that is precisely what a bug report always leaves out."""
    path = tmp_path / "s.jsonl"
    DebugRecorder(path).close()

    start = events(path)[0]
    assert start["kind"] == "session_start"
    assert "python" in start and "platform" in start


def test_every_event_is_timed_and_ordered(tmp_path):
    """Reconstructing a freeze needs the gaps between events, not just the
    events - "then nothing happened for 200 seconds" is the finding."""
    path = tmp_path / "s.jsonl"
    recorder = DebugRecorder(path)
    recorder.event("one")
    recorder.event("two")
    recorder.close()

    for event in events(path):
        assert "at" in event and "t" in event and "thread" in event
    times = [event["t"] for event in events(path)]
    assert times == sorted(times)


def test_it_flushes_as_it_goes(tmp_path):
    """The session worth reading is the one that ended in a way that skipped
    every cleanup path. A buffered log loses exactly those last lines."""
    path = tmp_path / "s.jsonl"
    recorder = DebugRecorder(path)
    recorder.event("crash_is_coming")

    assert any(e["kind"] == "crash_is_coming" for e in events(path)), (
        "the event must be on disk before close() is called"
    )
    recorder.close()


# -- shape, never content ----------------------------------------------------

def test_a_long_string_is_recorded_as_a_length(tmp_path):
    path = tmp_path / "s.jsonl"
    secret = "commercially sensitive phrase " * 10
    recorder = DebugRecorder(path)
    recorder.event("search", text=secret)
    recorder.close()

    written = path.read_text(encoding="utf-8")
    assert "commercially sensitive phrase commercially" not in written
    payload = [e for e in events(path) if e["kind"] == "search"][0]
    assert payload["text"]["len"] == len(secret)


def test_a_path_is_recorded_as_an_extension(tmp_path):
    """File names carry client names, case numbers and people's names."""
    path = tmp_path / "s.jsonl"
    recorder = DebugRecorder(path)
    recorder.event("opened", file=Path(r"D:\Clients\Acme Ltd\2024 settlement.pdf"))
    recorder.close()

    written = path.read_text(encoding="utf-8")
    assert "Acme" not in written and "settlement" not in written
    payload = [e for e in events(path) if e["kind"] == "opened"][0]
    assert payload["file"]["ext"] == ".pdf"


def test_numbers_are_kept_exactly(tmp_path):
    """The counters are the whole point - redacting those would leave a file
    that proves nothing."""
    path = tmp_path / "s.jsonl"
    recorder = DebugRecorder(path)
    recorder.event("search", results=0, keyword_hits=42, vector_hits=0, elapsed_ms=12.5)
    recorder.close()

    payload = [e for e in events(path) if e["kind"] == "search"][0]
    assert payload["keyword_hits"] == 42
    assert payload["vector_hits"] == 0, "the number that reveals a dead vector store"


# -- it can never be the cause of a failure ----------------------------------

def test_an_object_that_cannot_be_serialised_does_not_raise(tmp_path):
    class Awkward:
        def __repr__(self):
            raise ValueError("even repr fails")

    recorder = DebugRecorder(tmp_path / "s.jsonl")
    recorder.event("odd", value=Awkward())          # must not raise
    recorder.close()


def test_an_unwritable_path_disables_recording_rather_than_failing(tmp_path):
    """Recording is a diagnostic aid, never a precondition for the app running."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    recorder = DebugRecorder(blocker / "nested" / "s.jsonl")

    assert not recorder.enabled
    recorder.event("ignored")
    recorder.close()


def test_closing_twice_is_harmless(tmp_path):
    recorder = DebugRecorder(tmp_path / "s.jsonl")
    recorder.close()
    recorder.close()


def test_events_after_close_are_dropped_silently(tmp_path):
    """A worker thread finishing after the window closed must not raise - which
    is the exact shape of the bug that produced three nested tracebacks."""
    recorder = DebugRecorder(tmp_path / "s.jsonl")
    recorder.close()
    recorder.event("late_worker_finished")


def test_a_runaway_loop_stops_rather_than_filling_the_disk(tmp_path):
    """A session producing this many events is itself the finding, and the file
    says so before it stops."""
    path = tmp_path / "s.jsonl"
    recorder = DebugRecorder(path)
    recorder._count = MAX_EVENTS - 1

    for _ in range(5):
        recorder.event("spin")
    recorder.close()

    kinds = [event["kind"] for event in events(path)]
    assert "recording_stopped" in kinds
    assert kinds.count("spin") == 1, "only the one below the cap should be written"


# -- the branding, which is now one definition -------------------------------

def test_the_application_name_has_a_single_definition():
    """It used to be spelled out in nine files. A rename was then a
    find-and-replace, which is the kind of change that misses one and leaves the
    old name in a dialog for a year."""
    from app.core.branding import NAME, banner, window_title

    assert NAME == "Leasha"
    assert banner("1.2.3") == "Leasha  version 1.2.3"
    assert window_title() == "Leasha"
    assert window_title("Settings") == "Leasha - Settings"


@pytest.mark.parametrize("module", [
    "app/main.py", "app/cli.py", "app/ui/shell.py", "app/core/diagnostics.py",
])
def test_no_module_hardcodes_the_old_name(module: str):
    text = (Path(__file__).resolve().parents[2] / module).read_text(encoding="utf-8")
    assert "Local Knowledge Graph" not in text, (
        f"{module} still spells the name out; import it from app.core.branding"
    )
