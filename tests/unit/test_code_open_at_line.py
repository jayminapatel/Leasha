r"""Order 0y §2c: a code result opens in the person's editor, at the line.

Layer: L0 / L5

What already existed before this item was built: the editor table and the
command builder (`app/ui/editors.py`), the finder for editors that are not on
`PATH` (`app/core/osbridge/programs.py`), the two settings (`CODE_EDITOR`,
`CODE_EDITOR_COMMAND`) and their control in Settings (`widgets/editor_box.py`).
What was missing was everything that *used* them: nothing called
`command_for`, so Enter and a double-click handed the file to its default
program whatever the setting said.

**No editor is started here.** Every test hands `open_at_line` a launcher that
records what it was asked to start.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from app.core import osbridge
from app.core.osbridge import launch as os_launch
from app.ui import editors, workers


@pytest.fixture()
def only(monkeypatch):
    """Pretend exactly these executables are installed."""
    def _only(*names):
        monkeypatch.setattr(
            editors, "installed",
            lambda name: f"/usr/bin/{name}" if name in names else None)
    return _only


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "engine.py"
    path.write_text("x = 1\n", encoding="utf-8")
    return str(path)


class Recorder:
    """A launcher and a fallback that only write down what they were given."""

    def __init__(self, fail: bool = False) -> None:
        self.started: list = []
        self.opened: list = []
        self._fail = fail

    def launch(self, command) -> None:
        if self._fail:
            raise OSError("the editor would not start")
        self.started.append(list(command))

    def fallback(self, path, *, select=True):
        self.opened.append((path, select))
        return None


# --- the order of the editors -------------------------------------------------


def test_the_order_is_vs_code_then_notepad_plus_plus_then_sublime(only) -> None:
    """The order's own words: VS Code, then Notepad++, then Sublime."""
    keys = [value for value, *_rest in editors.EDITORS]
    assert keys.index("vscode") < keys.index("notepadpp") < keys.index("sublime")

    only("subl", "notepad++")
    assert editors.command_for(editors.AUTO, "/repo/x.py", 7) == [
        "/usr/bin/notepad++", "-n7", "/repo/x.py"]


# --- the worker body -----------------------------------------------------------


def test_the_editor_is_started_at_the_line(only, source) -> None:
    only("code")
    seen = Recorder()
    outcome = workers.open_at_line(source, 512, choice="auto",
                                   launch=seen.launch, fallback=seen.fallback)
    assert outcome is None
    assert seen.started == [["/usr/bin/code", "-g", f"{source}:512"]]
    assert seen.opened == [], "an editor was found, so nothing else opens it"


def test_with_no_editor_it_opens_in_the_default_program_and_says_so(only, source) -> None:
    only()
    seen = Recorder()
    outcome = workers.open_at_line(source, 12, choice="auto",
                                   launch=seen.launch, fallback=seen.fallback)
    assert seen.started == []
    assert seen.opened == [(source, False)], "opened, not shown in its folder"
    assert isinstance(outcome, str) and "12" in outcome and "Settings" in outcome


def test_none_was_chosen_so_it_opens_plainly_and_says_nothing(only, source) -> None:
    only("code")
    seen = Recorder()
    outcome = workers.open_at_line(source, 12, choice="none",
                                   launch=seen.launch, fallback=seen.fallback)
    assert seen.started == [] and seen.opened == [(source, False)]
    assert outcome is None, "the person chose this; there is nothing to tell them"


def test_a_command_of_the_persons_own_wins(only, source) -> None:
    only("code")
    seen = Recorder()
    workers.open_at_line(source, 9, choice="vscode", custom="myeditor --at {line} {path}",
                         launch=seen.launch, fallback=seen.fallback)
    assert seen.started == [["myeditor", "--at", "9", source]]


def test_an_editor_that_will_not_start_falls_back_to_the_default_program(only, source) -> None:
    only("code")
    seen = Recorder(fail=True)
    outcome = workers.open_at_line(source, 3, choice="vscode",
                                   launch=seen.launch, fallback=seen.fallback)
    assert seen.opened == [(source, False)]
    assert isinstance(outcome, str) and "would not start" in outcome


def test_a_file_that_has_gone_is_an_error_and_nothing_is_started(only, tmp_path) -> None:
    only("code")
    seen = Recorder()
    outcome = workers.open_at_line(str(tmp_path / "gone.py"), 3, choice="vscode",
                                   launch=seen.launch)
    assert getattr(outcome, "code", "") == "ERR_FILE_CORRUPT"
    assert seen.started == []


def test_the_fallback_error_is_handed_back(only, source) -> None:
    only()
    from app.core.errors import make_error

    error = make_error("ERR_FILE_CORRUPT", "ui.open", path=source,
                       suggestion="x", details="y")
    outcome = workers.open_at_line(source, 3, choice="auto", launch=lambda _c: None,
                                   fallback=lambda _p, select=True: error)
    assert outcome is error


# --- how the editor is started -------------------------------------------------


def test_a_new_console_is_a_windows_flag_and_nothing_elsewhere(monkeypatch) -> None:
    monkeypatch.setattr(os_launch, "is_windows", lambda: True)
    assert osbridge.new_console_flags() == getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    monkeypatch.setattr(os_launch, "is_windows", lambda: False)
    assert osbridge.new_console_flags() == 0


def test_a_window_editor_gets_no_console_and_a_terminal_editor_gets_its_own(monkeypatch) -> None:
    """VS Code's `code.cmd` is a console program that starts a window: with no
    flag a black box flashes. Vim *is* a console program: hidden, it would run
    where nobody can see it."""
    monkeypatch.setattr(workers, "hidden_console_flags", lambda: 0x08000000)
    monkeypatch.setattr(workers, "new_console_flags", lambda: 0x10)
    assert workers.editor_creationflags([r"C:\VS Code\bin\code.cmd", "-g", "x:1"]) == 0x08000000
    assert workers.editor_creationflags([r"C:\Vim\vim.exe", "+1", "x"]) == 0x10
    assert workers.editor_creationflags(["nvim", "+1", "x"]) == 0x10
    assert workers.editor_creationflags([]) == 0x08000000


def test_the_real_launcher_passes_those_flags(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(workers.subprocess, "Popen",
                        lambda command, **kwargs: seen.update(command=command, **kwargs))
    monkeypatch.setattr(workers, "hidden_console_flags", lambda: 0x08000000)
    workers.start_editor(["code", "-g", "x.py:3"])
    assert seen["command"] == ["code", "-g", "x.py:3"]
    assert seen["creationflags"] == 0x08000000 and seen["shell"] is False


# --- a git hit in the checkout is a place too ----------------------------------


def test_a_checkout_hit_from_git_carries_its_line() -> None:
    from app.search.gitsearch import GitRow
    from app.ui.presenter import git_result_row

    row = git_result_row(GitRow(kind="content", path="app/engine.py", line_no=41,
                                text="class Engine:"), "D:/repo")
    assert row.line_no == 41 and row.line == "41"
    historical = git_result_row(GitRow(kind="commit", commit="abc1234", subject="s"), "D:/repo")
    assert historical.line_no == 0 and historical.full_path == ""


# --- the window -----------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.presenter.code import RepoFileRow  # noqa: E402
from app.ui.view_options import ViewPreferences  # noqa: E402
from app.ui.widgets.code_results import CodeResults  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _row(**fields) -> RepoFileRow:
    base = dict(name="engine.py", size="", kind="py", seen="", path="app/engine.py",
                full_path="D:/repo/app/engine.py")
    base.update(fields)
    return RepoFileRow(**base)


@pytest.fixture()
def results(qapp):
    widget = CodeResults()
    widget.show_rows([
        _row(match="Definition", line="512", line_no=512, code="def search():"),
        _row(name="README.md", full_path="D:/repo/README.md"),
        _row(name="a commit", full_path=""),
    ], ViewPreferences())
    seen = SimpleNamespace(at=[], plain=[])
    widget.open_at_requested.connect(lambda path, line: seen.at.append((path, line)))
    widget.open_requested.connect(seen.plain.append)
    return widget, seen


def _press(widget, key) -> None:
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key,
                                             Qt.KeyboardModifier.NoModifier))


def _at(widget, row: int):
    """The middle of a row, in the table's own coordinates (as a right-click gives it)."""
    table = widget.table
    return table.viewport().mapTo(table, table.visualItemRect(table.item(row, 0)).center())


def test_enter_on_a_content_row_asks_for_the_line(results) -> None:
    widget, seen = results
    widget.table.selectRow(0)
    _press(widget.table, Qt.Key.Key_Return)
    assert seen.at == [("D:/repo/app/engine.py", 512)] and seen.plain == []


def test_a_double_click_does_the_same(results) -> None:
    widget, seen = results
    widget.table.selectRow(0)
    widget.table.itemDoubleClicked.emit(widget.table.item(0, 0))
    assert seen.at == [("D:/repo/app/engine.py", 512)]


def test_a_row_with_no_line_opens_as_it_always_did(results) -> None:
    widget, seen = results
    widget.table.selectRow(1)
    _press(widget.table, Qt.Key.Key_Enter)
    assert seen.plain == ["D:/repo/README.md"] and seen.at == []


def test_a_row_with_no_file_opens_nothing(results) -> None:
    widget, seen = results
    widget.table.selectRow(2)
    _press(widget.table, Qt.Key.Key_Return)
    assert seen.plain == [] and seen.at == []


def test_the_row_menu_opens_at_the_line_and_copies_path_and_line(results, monkeypatch) -> None:
    widget, seen = results
    captured: dict = {}
    monkeypatch.setattr("app.ui.widgets.code_results.show_for",
                        lambda _w, _p, path, actions: captured.update(actions=actions))
    widget.table.selectRow(0)
    widget._on_context_menu(_at(widget, 0))
    captured["actions"].open_file()
    assert seen.at == [("D:/repo/app/engine.py", 512)]
    assert ("Copy path and line", "D:/repo/app/engine.py:512") in captured["actions"].copy

    widget.table.selectRow(1)
    widget._on_context_menu(_at(widget, 1))
    assert all(label != "Copy path and line" for label, _v in captured["actions"].copy)


def test_the_code_tab_passes_it_up(qapp, monkeypatch) -> None:
    from app.ui.code_view import CodeView
    from tests.unit.test_code_view import FakeStore

    monkeypatch.setattr("app.ui.code_view.run", lambda _pool, worker: worker.run())
    view = CodeView(FakeStore())
    seen: list = []
    view.open_at_requested.connect(lambda path, line: seen.append((path, line)))
    view.results.open_at_requested.emit("D:/repo/x.py", 9)
    assert seen == [("D:/repo/x.py", 9)]


def test_the_window_reads_the_setting_in_force_and_never_launches_inline(monkeypatch) -> None:
    from app.ui import shell

    asked: dict = {}
    monkeypatch.setattr(
        shell, "open_at_line_async",
        lambda path, line, **kwargs: asked.update(path=path, line=line, **kwargs))
    window = SimpleNamespace(
        _settings=SimpleNamespace(code_editor="auto", code_editor_command=""),
        _settings_overrides={"code_editor": "notepadpp"},
        _show_error=lambda _e: None, notify=lambda *_a: None)
    shell.MainWindow._open_code_at(window, "D:/repo/x.py", 9)
    assert asked["path"] == "D:/repo/x.py" and asked["line"] == 9
    assert asked["choice"] == "notepadpp", "a choice just made in Settings applies at once"
    assert asked["custom"] == ""


def test_a_changed_editor_applies_without_a_restart(tmp_path) -> None:
    from app.ui.controllers.settings_controller import SettingsController

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    window = SimpleNamespace(
        _settings=SimpleNamespace(env_file=str(env)), _settings_overrides={},
        _engine=SimpleNamespace(reranker=None), _show_error=lambda _e: None,
        notify=lambda *_a: None)
    SettingsController._settings_changed(
        SimpleNamespace(_w=window),
        {"CODE_EDITOR": "vscode", "CODE_EDITOR_COMMAND": "ed {path}"})
    assert window._settings_overrides["code_editor"] == "vscode"
    assert window._settings_overrides["code_editor_command"] == "ed {path}"
    assert "CODE_EDITOR=vscode" in env.read_text(encoding="utf-8")
