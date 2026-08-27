r"""Opening a code result at its line.

Layer: L5. **A code result is a place, not a document.** Reveal-in-Explorer is
right for a spreadsheet and wrong for line 512 of a file: somebody who found
that line wants to be at it, and being handed its folder makes them repeat the
search inside their editor.

Detection follows `extract/converter.py` and for the same written-up reason:
`shutil.which` alone is not enough on Windows, where several editors never put
themselves on `PATH` - so a machine with VS Code installed would report
nothing found and offer to install software already present.
"""

from __future__ import annotations

import pytest

from app.ui.editors import AUTO, EDITORS, command_for, copyable, detect


@pytest.fixture()
def only(monkeypatch):
    """Pretend exactly these executables are installed."""
    def _only(*names):
        import app.ui.editors as editors

        monkeypatch.setattr(
            editors, "installed",
            lambda name: f"/usr/bin/{name}" if name in names else None)
        return editors
    return _only


# --------------------------------------------------------------------------
# Building the command
# --------------------------------------------------------------------------

def test_vs_code_opens_at_the_line(only):
    only("code")
    assert command_for("vscode", "/repo/app/engine.py", 512) == [
        "/usr/bin/code", "-g", "/repo/app/engine.py:512"]


@pytest.mark.parametrize("choice,expected", [
    ("sublime", ["/repo/x.py:12"]),
    ("notepadpp", ["-n12", "/repo/x.py"]),
    ("idea", ["--line", "12", "/repo/x.py"]),
    ("vim", ["+12", "/repo/x.py"]),
])
def test_each_editor_has_its_own_way_of_saying_the_line(choice, expected, only):
    """**Every one of them spells this differently**, which is the whole
    reason a table exists rather than one string with a `{line}` in it."""
    executable = {"sublime": "subl", "notepadpp": "notepad++",
                  "idea": "idea", "vim": "vim"}[choice]
    only(executable)
    assert command_for(choice, "/repo/x.py", 12)[1:] == expected


def test_automatic_takes_the_first_one_installed(only):
    """`EDITORS` is ordered by how likely somebody searching a code index is
    to have it, not alphabetically: a machine with both VS Code and Vim
    almost certainly wants the first."""
    only("vim", "code")
    assert command_for(AUTO, "/repo/x.py", 3)[0] == "/usr/bin/code"


def test_automatic_with_nothing_installed_is_no_command(only):
    """**Not a crash and not a guess.** No editor means the old behaviour -
    show it in Explorer - and the box says so."""
    only()
    assert command_for(AUTO, "/repo/x.py", 3) is None


def test_an_editor_this_has_never_heard_of_is_a_line_of_configuration(only):
    """The escape hatch, and it wins over the choice above it."""
    only("code")
    assert command_for("vscode", "/repo/x.py", 9,
                       custom="myeditor --at {line} {path}") == [
        "myeditor", "--at", "9", "/repo/x.py"]


def test_a_custom_command_with_no_placeholder_still_opens_the_file():
    """**Landing on line one of the right file is worth having.** Refusing
    because the template is simple is not."""
    assert command_for("", "/repo/x.py", 9, custom="notepad") == [
        "notepad", "/repo/x.py"]


def test_a_command_with_no_line_placeholder_is_accepted(only):
    only("code")
    assert command_for("", "/repo/x.py", 9, custom="myeditor {path}") == [
        "myeditor", "/repo/x.py"]


def test_an_argument_list_not_a_string():
    """A path with a space in it is the normal case on Windows, and joining
    by hand is how that breaks."""
    found = command_for("", "C:/My Documents/x.py", 4, custom="ed {path}")
    assert found == ["ed", "C:/My Documents/x.py"]


def test_no_path_is_no_command():
    assert command_for("vscode", "", 5) is None
    assert command_for("vscode", None) is None


def test_an_unknown_choice_produces_nothing(only):
    only("code")
    assert command_for("emacs-but-spelled-wrong", "/repo/x.py", 1) is None


def test_no_line_means_line_one(only):
    only("code")
    assert command_for("vscode", "/repo/x.py")[-1].endswith(":1")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def test_only_what_is_installed_is_offered(only):
    """**The converter rule**: Settings offers exactly what was found, so
    nobody can choose something that will then fail on every click."""
    only("code", "vim")
    assert [value for value, _label, _where in detect()] == ["vscode", "vim"]


def test_detection_never_raises(monkeypatch):
    import app.ui.editors as editors

    def explode(_name):
        raise OSError("the filesystem is having a day")

    monkeypatch.setattr(editors.shutil, "which", explode)
    assert editors.installed("code") is None


def test_every_editor_in_the_table_has_a_line_placeholder():
    """An entry that cannot say where to go is an entry that does not belong
    in a feature called "open at the line"."""
    for value, label, executable, template in EDITORS:
        assert "{line}" in template, f"{value} cannot open at a line"
        assert "{path}" in template, f"{value} never receives the file"
        assert label and executable


def test_the_windows_locations_only_name_editors_we_know():
    """A location for an executable no entry uses is dead weight - the same
    check the converter table has, and the one that found a stale `pandoc`
    entry there."""
    import app.ui.editors as editors

    known = {executable for _v, _l, executable, _t in EDITORS}
    assert set(editors._WINDOWS_LOCATIONS) <= known


# --------------------------------------------------------------------------
# The copy action
# --------------------------------------------------------------------------

def test_path_and_line_is_the_universal_currency():
    """It pastes into a terminal, a chat message, another editor and half the
    tools on the machine."""
    assert copyable("/repo/app/engine.py", 512) == "/repo/app/engine.py:512"


def test_without_a_line_it_is_just_the_path():
    assert copyable("/repo/app/engine.py") == "/repo/app/engine.py"
    assert copyable("") == ""


# --------------------------------------------------------------------------
# The control
# --------------------------------------------------------------------------

def test_the_box_offers_automatic_and_none_whatever_is_installed(_qt_application, only):
    only()
    from app.ui.widgets.editor_box import EditorBox

    box = EditorBox()
    values = [box.editor.itemData(n) for n in range(box.editor.count())]
    assert values == [AUTO, "none"]


def test_loading_settings_does_not_write_them_back(_qt_application, only):
    r"""**Blocked signals, and not out of habit.** A combo box set while it is
    connected emits `currentIndexChanged`, the debounced writer saves it, and
    a value nobody chose lands in `.env` - which is exactly how opening the
    window once rewrote the worker count on an undetectable machine.
    """
    from types import SimpleNamespace

    only("code")
    from app.ui.widgets.editor_box import EditorBox

    box = EditorBox()
    seen = []
    box.changed.connect(seen.append)
    box.load(SimpleNamespace(code_editor="vscode", code_editor_command="x {path}"))
    assert seen == []
    assert box.values() == {"CODE_EDITOR": "vscode",
                            "CODE_EDITOR_COMMAND": "x {path}"}
