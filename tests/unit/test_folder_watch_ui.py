r""""Index files as soon as they are saved": the setting, the switch, the process
the switch starts, and the sentence under it.

Layer: L5

Work order 0z, item F1. The watch itself is `test_folder_watch.py`; the real
child process is `test_cli_watch.py`. Here the process is a stand-in, so what
is tested is the window's half: that the switch is off until somebody turns it
on, that turning it on starts a watch over the folders on screen and saves the
setting, that what the watch reports reaches the page, and that closing ends it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import settings_registry as registry
from app.core.config import SETTING_KEYS, load_settings
from app.index import watch_child
from app.index.watch_child import WatchChild, end_all_watch_children, watch_command
from app.ui.presenter.watch_words import NO_FOLDERS, OFF, watch_status


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def test_the_setting_is_declared_and_ships_switched_off(temp_env: Path) -> None:
    setting = registry.by_key("INDEX_WATCH_FOLDERS")
    assert setting.kind == "bool" and setting.default is False
    assert setting.surface == "indexing.schedule"
    assert setting.label == "Index files as soon as they are saved"
    assert "cannot be renamed or moved" in setting.help
    assert "INDEX_WATCH_FOLDERS" in SETTING_KEYS
    assert load_settings(temp_env).index_watch_folders is False

    temp_env.write_text(
        temp_env.read_text(encoding="utf-8") + "\nINDEX_WATCH_FOLDERS=true\n",
        encoding="utf-8")
    assert load_settings(temp_env).index_watch_folders is True


# ---------------------------------------------------------------------------
# The sentence under the switch
# ---------------------------------------------------------------------------

def test_every_event_has_a_plain_sentence_or_leaves_the_line_alone() -> None:
    assert watch_status("ready", {"folders": 1}) == "Watching 1 folder for changes."
    assert watch_status("ready", {}, folders=3) == "Watching 3 folders for changes."

    updated = watch_status("updated", {
        "indexed": 1, "removed": 0, "skipped": 0, "names": ["report.docx"],
        "t": time.mktime((2026, 9, 30, 14, 2, 0, 0, 0, -1))})
    assert updated == "Updated at 14:02: 1 indexed (report.docx)."
    assert watch_status("updated", {"indexed": 0, "removed": 0}) is None
    assert "2 removed" in watch_status("updated", {"removed": 2})

    busy = watch_status("busy", {"count": 3,
                                 "reason": "An index run is already in progress (the window)."})
    assert busy == ("3 changes are waiting: An index run is already in progress "
                    "(the window).")
    assert watch_status("busy", {"count": 1, "reason": ""}) == "1 change is waiting."

    problem = {"root": r"D:\Docs", "error": {"message": "Changes in 'D:\\Docs' are not "
                                             "being watched: the folder is not there."}}
    assert "not being watched" in watch_status("problem", problem)
    assert watch_status("problem", {"root": r"D:\Docs", "error": None}) == (
        r"Watching D:\Docs again.")

    assert watch_status("ended", {"expected": True}) == OFF
    assert "stopped unexpectedly" in watch_status("ended", {"expected": False})
    for quiet in ("watching", "pending", "stopped", "something new"):
        assert watch_status(quiet, {}) is None


# ---------------------------------------------------------------------------
# The child process, with the process replaced
# ---------------------------------------------------------------------------

class FakeProcess:
    """Enough of `subprocess.Popen`: a script of output lines, and an input
    pipe whose closing ends the output, as the real child's does."""

    def __init__(self, lines: list[bytes], *, ends_on_stop: bool = True) -> None:
        self.pid = None
        self._lines = list(lines)
        self._released = threading.Event()
        self._ends_on_stop = ends_on_stop
        self.written = b""
        self.killed = False
        self.stdin = self
        self.stdout = self
        self.returncode = None

    # stdin
    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self._ends_on_stop:
            self._released.set()

    # stdout
    def readline(self, _limit: int = -1) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self._released.wait(30)
        return b""

    # the process
    def wait(self, timeout=None):
        if not self._released.wait(timeout):
            raise subprocess.TimeoutExpired("watch", timeout)
        self.returncode = 0 if not self.killed else 1
        return self.returncode

    def terminate(self) -> None:
        self.killed = True
        self._released.set()

    kill = terminate


def started(lines, **options):
    events: list[tuple[str, dict]] = []
    process = FakeProcess(lines, **options)
    child = WatchChild(["python", "-m", "app.cli", "watch"],
                       on_event=lambda kind, data: events.append((kind, data)),
                       popen=lambda *a, **k: process)
    child.start()
    return child, process, events


def wait_until(condition, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


def test_the_command_names_the_folders_and_leaves_archives_out(tmp_path) -> None:
    argv = watch_command([tmp_path / "A", tmp_path / "-odd"], env_file=tmp_path / ".env",
                         python="py")
    assert argv[:5] == ["py", "-m", "app.cli", "--env", str(tmp_path / ".env")]
    assert argv[5:9] == ["watch", "--events", "jsonl", "--live-only"]
    assert argv[9] == "--" and argv[10:] == [str(tmp_path / "A"), str(tmp_path / "-odd")]


def test_events_are_passed_on_and_stopping_is_said_to_be_expected() -> None:
    child, process, events = started([
        b'{"event": "watch", "kind": "ready", "folders": 2}\n',
        b"a library printed this\n",
        b'{"event": "watch", "kind": "updated", "indexed": 1, "names": ["a.txt"]}\n',
    ])
    wait_until(lambda: len(events) == 2)
    assert events[0] == ("ready", {"event": "watch", "folders": 2})
    assert events[1][0] == "updated" and events[1][1]["names"] == ["a.txt"]
    assert child.running

    child.stop()
    wait_until(lambda: not child.running)

    assert process.written == b"stop\n"
    assert events[-1] == ("ended", {"code": 0, "expected": True})


def test_a_process_that_ends_by_itself_is_reported_as_unexpected() -> None:
    child, process, events = started([b'{"event": "watch", "kind": "ready"}\n'])
    wait_until(lambda: len(events) == 1)
    process._released.set()                       # it died
    wait_until(lambda: not child.running)
    assert events[-1][0] == "ended" and events[-1][1]["expected"] is False


def test_closing_ends_a_watch_that_will_not_stop(monkeypatch) -> None:
    monkeypatch.setattr(watch_child, "STOP_GRACE_S", 0.2)
    child, process, _events = started([], ends_on_stop=False)
    wait_until(lambda: child._proc is not None)

    assert end_all_watch_children(0.1) == 1
    wait_until(lambda: not child.running)
    assert process.killed
    assert end_all_watch_children(0.1) == 0, "nothing left to end"


def test_a_process_that_cannot_be_started_says_so_instead_of_raising() -> None:
    events: list = []

    def refuse(*_a, **_k):
        raise OSError("no such program")

    child = WatchChild(["nowhere"], on_event=lambda kind, data: events.append((kind, data)),
                       popen=refuse)
    child.start()
    wait_until(lambda: not child.running)
    assert events == [("ended", {"code": None, "expected": False,
                                 "reason": "no such program"})]


# ---------------------------------------------------------------------------
# The switch, in the real window
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")


class StandIn:
    """A `WatchChild` that starts nothing."""

    def __init__(self, roots: list[str]) -> None:
        self.roots = list(roots)
        self.running = False
        self.stopped = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.stopped = True
        self.running = False


@pytest.fixture()
def watch(gui_mainwindow, monkeypatch, tmp_path):
    """The real window's folder watch, with the process replaced and two
    folders on the Settings page. Put back as it was afterwards."""
    app, window, _store, _engine = gui_mainwindow
    control = window.folder_watch
    built: list[StandIn] = []

    def build(roots):
        control._generation += 1
        built.append(StandIn(roots))
        return built[-1]

    monkeypatch.setattr(control, "_build", build)
    before_roots = window.settings_view.current_roots()
    folders = [str(tmp_path / "Docs"), str(tmp_path / "Work")]
    window.settings_view.set_roots(folders)
    box = window.indexing_view.schedule_box
    yield app, window, control, box, built, folders
    if box.watch.isChecked():
        box.watch.click()
    control._settle.stop()
    control._again.stop()
    window.settings_view.set_roots(before_roots)


@pytest.mark.gui
def test_the_switch_is_off_and_nothing_is_watched_until_it_is_pressed(watch) -> None:
    _app, window, control, box, built, _folders = watch

    assert box.watch.text() == "Index files as soon as they are saved"
    assert box.findChild(type(box.watch), "INDEX_WATCH_FOLDERS") is box.watch
    assert not box.watch.isChecked() and not control.enabled and not control.running

    control.apply()                               # what start-up does
    assert built == [] and box.watch_status.text() == OFF


@pytest.mark.gui
def test_pressing_the_switch_saves_it_and_starts_a_watch_on_the_folders_shown(
        watch) -> None:
    app, window, control, box, built, folders = watch

    box.watch.click()
    app.processEvents()

    assert window._settings.index_watch_folders is True
    assert load_settings(Path(window._settings.env_file)).index_watch_folders is True
    assert len(built) == 1 and built[0].roots == folders and control.running

    number = control._generation
    control._event.emit("ready", {"folders": 2, "_process": number})
    app.processEvents()
    assert box.watch_status.text() == "Watching 2 folders for changes."

    control._event.emit("updated", {"indexed": 1, "removed": 0, "names": ["a.txt"],
                                    "_process": number})
    app.processEvents()
    assert "1 indexed (a.txt)" in box.watch_status.text()

    # A late word from a process that was replaced changes nothing.
    control._event.emit("busy", {"count": 9, "reason": "old", "_process": number - 1})
    app.processEvents()
    assert "1 indexed" in box.watch_status.text()

    box.watch.click()
    app.processEvents()
    assert built[0].stopped and not control.running
    assert window._settings.index_watch_folders is False
    assert load_settings(Path(window._settings.env_file)).index_watch_folders is False
    assert box.watch_status.text() == OFF


@pytest.mark.gui
def test_changing_the_folders_restarts_the_watch_on_the_new_list(watch, tmp_path) -> None:
    app, window, control, box, built, folders = watch
    box.watch.click()
    app.processEvents()

    more = folders + [str(tmp_path / "Photos")]
    window.settings_view.set_roots(more)
    control.folders_changed()
    assert control._settle.isActive(), "three folders added in a row are one restart"
    control.apply()                               # the timer firing
    app.processEvents()

    assert built[0].stopped and built[-1].roots == more and len(built) == 2

    window.settings_view.set_roots([])
    control.apply()
    assert not control.running and box.watch_status.text() == NO_FOLDERS


@pytest.mark.gui
def test_a_watch_that_ends_by_itself_is_started_again_a_few_times_then_left(watch) -> None:
    app, _window, control, box, built, _folders = watch
    box.watch.click()
    app.processEvents()

    from app.ui.folder_watch import RESTARTS

    for attempt in range(RESTARTS):
        control._event.emit("ended", {"code": 1, "expected": False,
                                      "_process": control._generation})
        app.processEvents()
        assert control._again.isActive(), f"restart {attempt + 1} is on its way"
        assert "stopped unexpectedly" in box.watch_status.text()
        control.apply()                           # the timer firing
    assert len(built) == RESTARTS + 1

    control._event.emit("ended", {"code": 1, "expected": False,
                                  "_process": control._generation})
    app.processEvents()
    assert not control._again.isActive()
    assert "keeps stopping" in box.watch_status.text()


@pytest.mark.gui
def test_loading_the_page_does_not_press_the_switch(qtbot) -> None:
    from types import SimpleNamespace

    from app.ui.indexing_settings import IndexingSettings

    box = IndexingSettings()
    qtbot.addWidget(box)
    heard: list[bool] = []
    box.watch_toggled.connect(heard.append)

    box.load_indexing(SimpleNamespace(index_watch_folders=True))
    assert box.watch.isChecked() and heard == []
    box.load_indexing(SimpleNamespace())
    assert not box.watch.isChecked() and heard == [], "off unless the setting says on"

    box.watch.click()
    assert heard == [True]


def test_nothing_in_the_window_s_half_starts_a_process_on_the_window_s_thread() -> None:
    """The window only ever calls `WatchChild.start` and `stop`, which return
    at once; the process is opened on the child's own thread."""
    source = (Path(watch_child.__file__).parent.parent / "ui" / "folder_watch.py").read_text(
        encoding="utf-8")
    assert "subprocess" not in source and "Popen" not in source
    assert sys.modules["app.index.watch_child"].WatchChild._run.__name__ == "_run"
