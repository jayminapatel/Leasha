r"""The UX review of 2026-10-14 (`docs/REVIEW-2026-10-14-ux-architecture.md`), its guards.

Layer: L5, offscreen. The owner: "do recommended ... do all and fix all". Each guard here
failed on the tree it was written against, and each behaviour test failed before its fix.

* **DS-1** - a question asked before something happens goes through `widgets/confirm.py`:
  Cancel the default, the other button naming what it does. Nine `QMessageBox.question`
  calls with Yes and No had their own idea (two left Qt's default, which is Yes on Windows),
  and Settings' Clear logs built the same box by hand.
* **DS-5** - the spacing guard in `test_ui_style_rules.py` only sees a call whose every
  number is on the scale, so a new off-scale gap passed unseen. A ceiling here counts them.
* **DS-13** - a dialog's OK and Cancel row is `dialog_frame.buttons`, never a raw
  `QDialogButtonBox(Ok | Cancel)`.

The menu guard for DS-6 is in `test_menu_system.py`, beside the builder's own rules.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "app" / "ui"


def _files() -> list[Path]:
    return sorted(UI.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(UI).as_posix()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def answer(monkeypatch):
    """Every `QMessageBox` `exec`ed is recorded, and nobody clicks: Escape, as it were."""
    seen: list = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: seen.append(box) or 0)
    return seen


# ---------------------------------------------------------------------------
# DS-1: one way to ask
# ---------------------------------------------------------------------------

#: A question box built with Qt's own Yes. Only `confirm.py` builds a question box.
_YES = re.compile(r"QMessageBox\.StandardButton\.Yes\b|QMessageBox\.Yes\b")
_QUESTION_CALLS = {"question", "warning", "critical", "information"}


def _questions(source: str) -> list[int]:
    """Lines asking a Yes/No question with Qt's static helpers.

    `QMessageBox.question(...)` always is one. `warning`/`critical`/`information` are one
    when they are given buttons (a fourth argument, or `buttons=`): with the default OK
    they only tell.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Name) and owner.id == "QMessageBox"):
            continue
        name = node.func.attr
        if name == "question" or (name in _QUESTION_CALLS and (
                len(node.args) > 3 or any(k.arg in ("buttons", "defaultButton") for k in node.keywords))):
            found.append(node.lineno)
    return found


def test_the_question_guard_would_notice_a_yes_no_box() -> None:
    assert _questions("QMessageBox.question(w, 't', 'x')\n") == [1]
    assert _questions("QMessageBox.warning(w, 't', 'x', QMessageBox.StandardButton.Yes)\n") == [1]
    assert _questions("QMessageBox.warning(w, 't', 'x')\n") == []
    assert _YES.search("box.setStandardButtons(QMessageBox.StandardButton.Yes)")


@pytest.mark.slow
def test_no_question_is_asked_outside_confirm() -> None:
    """Review DS-1: every question goes through `confirm(parent, title, text, verb)`."""
    offenders = []
    for path in _files():
        if _rel(path) == "widgets/confirm.py":
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{_rel(path)}:{line}" for line in _questions(text)]
        offenders += [f"{_rel(path)}:{text[:m.start()].count(chr(10)) + 1}" for m in _YES.finditer(text)]
    assert not offenders, (
        "a Yes/No question built outside widgets/confirm.py - use confirm(parent, title, text, "
        "verb): Cancel the default, the verb naming what happens: " + ", ".join(offenders))


def _settings_controller():
    from app.ui.controllers.settings_controller import SettingsController

    window = QWidget()
    return SettingsController(window), window


@pytest.mark.parametrize("ask, verb", [
    (lambda c: c.ask_remove(["C:/Docs"], 12), "Remove"),
    (lambda c: c.ask_remove_leftovers(7), "Remove them from the index"),
    (lambda c: c.ask_clear_archive("C:/Mail/old.pst", 40), "Clear and read again"),
])
def test_settings_questions_name_their_verb_and_cancel_is_the_default(app, answer, ask, verb):
    controller, window = _settings_controller()
    try:
        assert ask(controller) is False, "nobody clicked: nothing may happen"
        box = answer[-1]
        assert sorted(b.text() for b in box.buttons()) == sorted(["Cancel", verb])
        assert box.defaultButton().text() == "Cancel" and box.escapeButton().text() == "Cancel"
        assert box.icon() == QMessageBox.Icon.Warning, "destructive: drawn as a warning"
    finally:
        window.deleteLater()


def test_the_question_text_is_the_presenters_word_for_word(app, answer):
    from app.ui.presenter import remove_folders_confirmation

    controller, window = _settings_controller()
    try:
        controller.ask_remove(["C:/Docs"], 12)
        title, body = remove_folders_confirmation(["C:/Docs"], 12)
        assert (answer[-1].windowTitle(), answer[-1].text()) == (title, body)
    finally:
        window.deleteLater()


def test_moving_the_models_folder_asks_with_a_verb_and_enter_does_nothing(app, answer, monkeypatch, tmp_path):
    """It was the one with Qt's No as its default and Yes on the button - now "Change folder"."""
    from PySide6.QtWidgets import QFileDialog

    controller, window = _settings_controller()
    window._settings = SimpleNamespace(model_cache=tmp_path / "old")
    saved = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "new"))
    monkeypatch.setattr(controller, "_save_model_cache", lambda *a: saved.append(a))
    try:
        controller._change_model_cache()
        box = answer[-1]
        assert sorted(b.text() for b in box.buttons()) == ["Cancel", "Change folder"]
        assert box.defaultButton().text() == "Cancel"
        assert box.text().endswith("Change it now?")
        assert saved == []
    finally:
        window.deleteLater()


def test_moving_mail_copies_asks_with_a_verb(app, answer):
    from app.ui.controllers.outlook_cleanup_controller import OutlookCleanupController

    window = QWidget()
    controller = OutlookCleanupController(window)
    plan = SimpleNamespace(copies=[SimpleNamespace(subject="Rent", folder="Inbox")], count=1)
    try:
        assert controller.ask("C:/Mail/old.pst", plan) is False
        box = answer[-1]
        assert sorted(b.text() for b in box.buttons()) == ["Cancel", "Move to Deleted Items"]
        assert box.defaultButton().text() == "Cancel"
    finally:
        window.deleteLater()


def test_confirm_draws_a_combine_as_a_combine_not_a_delete(app, answer):
    from app.ui.widgets.confirm import confirm

    confirm(None, "Combine these two?", "Put them together?", "Combine", glyph="merge")
    go = next(b for b in answer[-1].buttons() if b.text() == "Combine")
    assert go.property("buttonIcon") == "merge"
    confirm(None, "Clear the logs?", "Delete them?", "Clear logs")
    go = next(b for b in answer[-1].buttons() if b.text() == "Clear logs")
    assert go.property("buttonIcon") == "eraser", "the button table's icon for the verb"


# ---------------------------------------------------------------------------
# DS-5: a ceiling on gaps written off the scale
# ---------------------------------------------------------------------------

_SPACING_CALLS = {"setSpacing", "addSpacing", "setContentsMargins", "setHorizontalSpacing",
                  "setVerticalSpacing"}
#: 0 and the scale (`theme.SPACING`).
_ON_SCALE = {0, 4, 8, 12, 16, 24}

#: **Measured 2026-10-14: 99** (the review counted 98 a few commits earlier); 19 after the
#: DS-5 migration, every one of them in the Contacts editor, reader and toolbar and the
#: activity table, which were being edited in parallel. This number may only go down - each
#: file moved onto `theme.SPACE_*` lowers it, and a new gap off the scale fails here instead
#: of passing unseen.
OFF_SCALE_CEILING = 0

#: A gap off the scale on purpose: a geometry, not a rhythm. Keyed "<file>: <call>" as
#: `ast.unparse` writes the call, so a changed number drops out of the list and is counted.
OFF_SCALE_OK: dict[str, str] = {
    "widgets/about_dialog.py: layout.setContentsMargins(36, 24, 36, 16)":
        "the brand's clear space round the logo: the L's height",
    "widgets/chat_markdown.py: column.setContentsMargins(1, 1, 1, 1)":
        "the code block's 1 px border; the contents sit just inside it",
    "contacts/parts.py: row.setContentsMargins(SPACE_XS, 1, 1, 1)":
        "the contact token's 1 px border; the remove button sits just inside it",
    "contacts/fields.py: grid.setContentsMargins(46, 0, 0, 0)":
        "lines the address parts up under the field's text, past the grip and the icon",
    "widgets/timeline_strip.py: self._layout.setSpacing(1)":
        "a 1 px hairline between the timeline's bars, so neighbours read as two",
}


def _off_scale(source: str, rel: str = "") -> list[int]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in _SPACING_CALLS:
            numbers = [a.value for a in node.args
                       if isinstance(a, ast.Constant) and isinstance(a.value, int)]
            if any(n not in _ON_SCALE for n in numbers) and \
                    f"{rel}: {ast.unparse(node)}" not in OFF_SCALE_OK:
                found.append(node.lineno)
    return found


@pytest.mark.slow
def test_every_allowed_geometry_is_still_in_the_tree() -> None:
    """A stale entry would let the next gap with the same text through unseen."""
    calls = set()
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in _SPACING_CALLS:
                calls.add(f"{_rel(p)}: {ast.unparse(node)}")
    assert not set(OFF_SCALE_OK) - calls, sorted(set(OFF_SCALE_OK) - calls)


def test_the_ceiling_counts_a_mixed_call() -> None:
    """The case the old guard let through: one number on the scale, one off it."""
    assert _off_scale("row.setContentsMargins(8, 6, 8, 6)\nrow.setSpacing(8)\n") == [1]
    assert _off_scale("row.setContentsMargins(SPACE_S, 0, SPACE_S, 0)\n") == []


@pytest.mark.slow
def test_gaps_off_the_scale_never_grow() -> None:
    found = [f"{_rel(p)}:{line}" for p in _files()
             for line in _off_scale(p.read_text(encoding="utf-8"), _rel(p))]
    assert len(found) <= OFF_SCALE_CEILING, (
        f"{len(found)} layout gaps off the 4/8/12/16/24 scale, over the ceiling of "
        f"{OFF_SCALE_CEILING}. The ceiling may only go down: use theme.SPACE_* for the new one.")
    if len(found) < OFF_SCALE_CEILING:
        pytest.fail(f"only {len(found)} off-scale gaps now - lower OFF_SCALE_CEILING to "
                    f"{len(found)} so it holds the gain")


# ---------------------------------------------------------------------------
# DS-13: OK and Cancel come from the dialog frame
# ---------------------------------------------------------------------------

_STANDARD = re.compile(r"\b(Ok|Cancel|Yes|No|Save|Close|Discard|Apply)\b")
#: A raw box allowed to stay, and why. Close-only and hand-added rows are not OK/Cancel.
RAW_BOX_OK: dict[str, str] = {}


def _raw_boxes(source: str) -> list[int]:
    """`QDialogButtonBox(<standard buttons>)` calls asking OK/Cancel or Yes/No."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name != "QDialogButtonBox" or not node.args:
            continue
        words = set(_STANDARD.findall(ast.unparse(node.args[0])))
        if words & {"Ok", "Yes"}:
            found.append(node.lineno)
    return found


def test_the_box_guard_would_notice_an_ok_cancel_row() -> None:
    assert _raw_boxes("b = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | "
                      "QDialogButtonBox.StandardButton.Cancel)\n") == [1]
    assert _raw_boxes("b = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)\n") == []
    assert _raw_boxes("b = QDialogButtonBox()\n") == []


@pytest.mark.slow
def test_no_dialog_builds_its_own_ok_cancel_row() -> None:
    offenders = [f"{_rel(p)}:{line}" for p in _files()
                 if _rel(p) not in RAW_BOX_OK and _rel(p) != "widgets/dialog_frame.py"
                 for line in _raw_boxes(p.read_text(encoding="utf-8"))]
    assert not offenders, ("an OK/Cancel row built by hand - use dialog_frame.buttons(dialog, "
                           "primary=<verb>): " + ", ".join(offenders))


def test_the_smart_group_dialogs_ok_says_what_it_does(app):
    from PySide6.QtWidgets import QDialogButtonBox

    from app.ui.contacts.smart import SmartDialog

    dialog = SmartDialog(None)
    try:
        assert dialog.buttons.objectName() == "dialogButtons", "the frame's row"
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok.text() == "Save smart group" and ok.isDefault()
    finally:
        dialog.deleteLater()
