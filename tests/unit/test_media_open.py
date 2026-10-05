r"""Open a recording at the moment the search result is about.

Work order 202626270515, "open-at-time". Layer: L5.

Nothing here launches a player. The finder and the launcher are parameters of
`media_open.open_at`, so every rule is a plain assertion; the Qt half proves the
worker and the window route a recording's click to it and everything else to the
old open.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core import media_open
from app.core.errors import AppError
from app.core.media_open import KNOWN_PLAYERS, Player, command_for, find_player, open_at


def player(name: str) -> Player:
    return next(p for p in KNOWN_PLAYERS if p.name == name)


# --------------------------------------------------------------------------
# The command each player is given
# --------------------------------------------------------------------------

def test_each_player_is_told_where_to_start_the_way_it_documents():
    path = r"D:\Family\holiday 2019.mp4"
    assert command_for(player("VLC"), r"C:\vlc.exe", path, 761) == [
        r"C:\vlc.exe", "--start-time=761", path]
    assert command_for(player("mpv"), "mpv", path, 761) == ["mpv", "--start=761", path]
    # MPC-HC counts in milliseconds.
    assert command_for(player("MPC-HC"), "mpc-hc64", path, 761) == [
        "mpc-hc64", path, "/start", "761000"]
    # PotPlayer wants a clock.
    assert command_for(player("PotPlayer"), "pot.exe", path, 3725) == [
        "pot.exe", path, "/seek=01:02:05"]
    assert command_for(player("PotPlayer"), "pot.exe", path, 61)[-1] == "/seek=00:01:01"


def test_a_hostile_file_name_stays_one_argument():
    name = "a; del b & echo hi.mp4"
    command = command_for(player("VLC"), "vlc", name, 5)
    assert command[-1] == name and len(command) == 3


def test_a_time_before_zero_is_zero():
    assert command_for(player("mpv"), "mpv", "x.mp4", -5)[1] == "--start=0"


# --------------------------------------------------------------------------
# Finding a player
# --------------------------------------------------------------------------

def test_the_first_player_found_wins_and_path_comes_before_install_folders():
    found = find_player(which=lambda name: r"C:\bin\mpv.exe" if name == "mpv" else None,
                        installed=lambda p: r"C:\Program Files\MPC-HC\mpc-hc64.exe"
                        if p.name == "MPC-HC" else None)
    assert found[0].name == "mpv"                      # VLC is not installed; mpv is on PATH
    both = find_player(which=lambda name: None,
                       installed=lambda p: rf"C:\x\{p.name}.exe"
                       if p.name in ("MPC-HC", "VLC") else None)
    assert both[0].name == "VLC"


def test_no_player_at_all_is_the_ordinary_answer_not_an_error():
    assert find_player(which=lambda name: None, installed=lambda p: None) is None


def test_an_install_folder_is_found_without_a_real_install(tmp_path, monkeypatch):
    root = tmp_path / "Program Files"
    (root / "VideoLAN" / "VLC").mkdir(parents=True)
    exe = root / "VideoLAN" / "VLC" / "vlc.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("ProgramFiles", str(root))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert media_open._executable_in_folders(player("VLC")) == str(exe)
    assert media_open._executable_in_folders(player("mpv")) is None


# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------

@pytest.fixture()
def recording(tmp_path) -> Path:
    path = tmp_path / "holiday 2019.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


def test_a_player_that_can_seek_is_started_at_the_moment(recording):
    launched: list[list[str]] = []
    plain: list[str] = []
    outcome = open_at(
        str(recording), 761,
        finder=lambda: (player("VLC"), r"C:\vlc.exe"),
        launch=launched.append, system_open=plain.append)
    assert outcome.opened and outcome.seeked and outcome.player == "VLC"
    assert outcome.note == "" and plain == []          # nothing more to say
    assert launched == [[r"C:\vlc.exe", "--start-time=761", str(recording)]]


def test_without_a_player_the_file_still_opens_and_the_moment_is_said(recording):
    plain: list[str] = []
    outcome = open_at(str(recording), 761, finder=lambda: None,
                      launch=lambda c: pytest.fail("no player, nothing to launch"),
                      system_open=plain.append)
    assert outcome.opened and not outcome.seeked
    assert plain == [str(recording)]
    assert "12:41" in outcome.note and "VLC" in outcome.note


def test_a_player_that_will_not_start_falls_back_to_a_plain_open(recording):
    def broken(command):
        raise OSError("access denied")

    plain: list[str] = []
    outcome = open_at(str(recording), 30, finder=lambda: (player("mpv"), "mpv"),
                      launch=broken, system_open=plain.append)
    assert outcome.opened and not outcome.seeked and plain == [str(recording)]
    assert "0:30" in outcome.note


def test_a_file_that_is_not_a_time_is_opened_plainly_with_no_note(recording):
    plain: list[str] = []
    outcome = open_at(str(recording), None, finder=lambda: pytest.fail("not asked"),
                      launch=lambda c: pytest.fail("not launched"),
                      system_open=plain.append)
    assert outcome.opened and outcome.note == "" and plain == [str(recording)]


def test_a_moved_file_is_an_error_that_says_how_to_fix_it(tmp_path):
    outcome = open_at(str(tmp_path / "gone.mp4"), 5, finder=lambda: None,
                      launch=lambda c: None, system_open=lambda p: None)
    assert not outcome.opened and isinstance(outcome.error, AppError)
    assert "Re-index" in outcome.error.suggestion


def test_the_launch_is_a_list_and_never_a_shell(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen.update(command=command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    media_open._launch(["vlc", "--start-time=5", "a; del b.mp4"])
    assert seen["shell"] is False and isinstance(seen["command"], list)


@pytest.mark.parametrize("label,expected", [
    ("12:41", 761), ("1:02:05", 3725), ("0:00", 0),
    ("Q3!D14", None), ("", None), (None, None), ("12:99", None)])
def test_only_a_time_is_a_moment(label, expected):
    assert media_open.seconds_for_result(label) == expected


# --------------------------------------------------------------------------
# The window: a click on a recording goes to the moment
# --------------------------------------------------------------------------

pytest.importorskip("PySide6")

ENV = """\
DATA_PATH={d}
VECTOR_PATH={d}/vectors
FTS_DB={d}/fts/knowledge.db
CACHE_PATH={d}/cache
MODEL_CACHE={d}/models
STATE_PATH={d}/state
PROJECT_PATH={d}
LOG_PATH={d}/logs
EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=false
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral
MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""


class _Engine:
    def __init__(self, store):
        self.store = store

    def warm_up(self):
        pass

    def close(self):
        pass


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    from PySide6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path_factory.mktemp("window")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    built = MainWindow(settings, store, vectors, _Engine(store), debug=False)
    yield built
    store.close()
    vectors.close()


class Row:
    volume_id = None

    def __init__(self, path, ext, label):
        self.path, self.ext, self.label = path, ext, label


def test_clicking_a_transcript_hit_opens_the_recording_at_that_time(window, monkeypatch):
    """The window hands the row to the one open route (2026-10-04), which reads
    the moment off it - `presenter.opening.plan_for` is that decision."""
    from app.ui import shell
    from app.ui.presenter.opening import plan_for

    rows = []
    monkeypatch.setattr(shell, "open_row_async", lambda store, row, **kw: rows.append((row, kw)))
    hit = Row(r"D:\v\holiday.mp4", "mp4", "12:41")
    window._open_result(hit)
    assert rows == [(hit, {"reveal": False, "on_error": window._show_error})]
    plan = plan_for(hit)
    assert (plan.how, plan.path, plan.seconds) == ("media", r"D:\v\holiday.mp4", 761)


def test_everything_else_opens_exactly_as_it_did():
    from app.ui.presenter.opening import plan_for

    assert plan_for(Row(r"D:\books\q3.xlsx", "xlsx", "Q3!D14")).how == "file"   # a cell, not a time
    assert plan_for(Row(r"D:\notes\a.txt", "txt", "12:41")).how == "file"       # not a recording
    assert plan_for(Row(r"D:\v\holiday.mp4", "mp4", "12:41"), reveal=True).how == "reveal"
    assert plan_for(Row(r"D:\v\holiday.mp4", "mp4", "")).how == "file"         # no time known


def test_the_worker_hands_a_note_to_the_toast_and_an_error_to_the_error(qtbot, monkeypatch):
    from app.core import media_open as mo
    from app.core.errors import make_error
    from app.ui import workers

    notes, errors = [], []
    monkeypatch.setattr(workers, "_CONTEXT", workers.OpenContext())
    monkeypatch.setattr(mo, "open_at", lambda path, seconds: mo.OpenOutcome(
        opened=True, note=f"Opened. At {seconds}"))
    workers.open_row_async(None, Row("x.mp4", "mp4", "12:41"),
                           on_error=errors.append, on_note=notes.append)
    qtbot.waitUntil(lambda: bool(notes), timeout=5000)
    assert notes == ["Opened. At 761"] and errors == []

    boom = make_error("ERR_FILE_CORRUPT", "ui.open", path="x.mp4")
    monkeypatch.setattr(mo, "open_at", lambda path, seconds: mo.OpenOutcome(
        opened=False, error=boom))
    workers.open_row_async(None, Row("x.mp4", "mp4", "0:05"),
                           on_error=errors.append, on_note=notes.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=5000)
    assert errors == [boom] and len(notes) == 1


# --------------------------------------------------------------------------
# Found running the CLI over a video: indexed text can hold any character
# --------------------------------------------------------------------------

def test_a_result_the_console_cannot_draw_prints_as_a_question_mark_not_a_traceback(monkeypatch):
    import io
    import sys

    from app.cli._common import make_console_safe

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")        # what a Windows pipe is
    monkeypatch.setattr(sys, "stdout", console)
    with pytest.raises(UnicodeEncodeError):
        print("\u53e3")                                        # the fault, before the fix
    make_console_safe()
    print("caf\u00e9 \u53e3")
    console.flush()
    assert raw.getvalue().decode("cp1252").strip().endswith("caf\u00e9 ?")


def test_making_the_console_safe_leaves_a_plain_buffer_alone(monkeypatch):
    import io
    import sys

    from app.cli._common import make_console_safe

    monkeypatch.setattr(sys, "stdout", io.StringIO())         # no `reconfigure`
    make_console_safe()                                       # must not raise


def test_every_command_starts_with_a_safe_console(monkeypatch):
    from app import cli

    called = []
    monkeypatch.setattr(cli, "make_console_safe", lambda: called.append(True))
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert called == [True]
