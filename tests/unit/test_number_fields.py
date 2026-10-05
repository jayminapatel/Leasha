r"""Every number field is typed, has no arrows, and has a back-to-default button.

Layer: L5

**The owner, 2026-09-29:** *"in the ui remove the up down controls for numbers
instead have a default button with just logo, they will be entered this is
every where"*. `app/ui/widgets/number_field.py` does it in one place; these
tests hold it to "every where":

* the helper itself - no arrows, the button's words, what a press does and
  that it goes through the same `valueChanged` a typed number does;
* **the guard** - the whole window (every page, including the two built a
  beat after first paint) and every dialog or menu that builds a number field
  of its own, walked for any `QAbstractSpinBox` that still shows arrows or has
  no button;
* a source check that a new module building a number field is added to the
  walk, so the guard cannot go quietly out of date.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractSpinBox, QApplication, QSpinBox, QTimeEdit, QWidget,
)

from app.ui.widgets import number_field  # noqa: E402
from app.ui.widgets.number_field import RESET_NAME, default_of, fit, fit_all, reset_button  # noqa: E402

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _spin(name: str = "", low: int = 0, high: int = 100, value: int = 0,
          suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    if name:
        spin.setObjectName(name)
    spin.setRange(low, high)
    spin.setSuffix(suffix)
    spin.setValue(value)
    return spin


# -- the helper ------------------------------------------------------------------

def test_a_fitted_field_has_no_arrows_and_one_reset_button(qapp):
    spin = fit(_spin(value=5))

    assert spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    button = reset_button(spin)
    assert button is not None and button.objectName() == RESET_NAME
    assert button.text() == "", "the owner asked for just the logo"
    assert not button.icon().isNull(), "an icon-only button needs its icon"


def test_the_registry_default_wins_for_a_registered_key(qapp):
    """`RERANK_TOP_N` defaults to 30 in the registry, whatever it was loaded as."""
    spin = fit(_spin("RERANK_TOP_N", 5, 100, 70, " results"))

    assert default_of(spin) == 30
    button = reset_button(spin)
    assert button.toolTip() == "Back to the default (30 results)"
    assert button.accessibleName() == button.toolTip()
    assert button.isEnabled()


def test_pressing_it_goes_through_the_typed_value_path(qapp):
    """The same `valueChanged` a typed number emits, so every save, debounce and
    warning already wired to the field runs - nothing is written behind its back."""
    spin = _spin("RERANK_TOP_N", 5, 100, 70)
    spin.setKeyboardTracking(False)
    fit(spin)
    seen: list[int] = []
    spin.valueChanged.connect(seen.append)

    reset_button(spin).click()

    assert spin.value() == 30 and seen == [30]
    assert not reset_button(spin).isEnabled(), "already at the default: greyed"


def test_an_unregistered_field_defaults_to_what_it_was_built_with(qapp):
    spin = fit(_spin(value=12))
    spin.setValue(40)
    reset_button(spin).click()
    assert spin.value() == 12


def test_a_default_passed_in_wins_over_the_built_value(qapp):
    spin = _spin(low=5, high=40, value=20)
    spin.setSpecialValueText("System")
    fit(spin, default=5)
    assert reset_button(spin).toolTip() == "Back to the default (System)"
    reset_button(spin).click()
    assert spin.value() == 5


def test_the_daily_time_is_covered_too(qapp):
    edit = QTimeEdit()
    edit.setObjectName("INDEX_DAILY_AT")
    edit.setDisplayFormat("HH:mm")
    fit(edit)
    assert edit.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    assert reset_button(edit).toolTip() == "Back to the default (02:00)"
    reset_button(edit).click()
    assert edit.time().toString("HH:mm") == "02:00"


def test_a_load_with_signals_blocked_still_updates_the_button(qapp):
    r"""Every page loads its values with the field's signals blocked, and Qt blocks
    the text box's too - so no signal says the number moved. The repaint does."""
    spin = fit(_spin("RERANK_TOP_N", 5, 100, 30))
    spin.show()
    qapp.processEvents()
    assert not reset_button(spin).isEnabled()

    spin.blockSignals(True)
    spin.setValue(80)
    spin.blockSignals(False)
    spin.repaint()
    qapp.processEvents()

    assert reset_button(spin).isEnabled()
    spin.hide()


def test_fitting_twice_adds_nothing(qapp):
    spin = fit(fit(_spin(value=3)))
    buttons = [child for child in spin.findChildren(QWidget) if child.objectName() == RESET_NAME]
    assert len(buttons) == 1


def test_fit_all_reaches_every_field_under_a_widget(qapp):
    root = QWidget()
    for value in (1, 2, 3):
        _spin(value=value).setParent(root)
    assert fit_all(root) == 3
    assert fit_all(root) == 0, "a second pass fits nothing new"


def test_the_wheel_rule_is_untouched(qapp):
    """`no_scroll` still decides the wheel; losing the arrows changes nothing
    about when a field may be scrolled."""
    from PySide6.QtCore import Qt

    from app.ui.widgets.no_scroll import protect

    spin = fit(protect(_spin(value=1)))
    assert spin.focusPolicy() == Qt.FocusPolicy.StrongFocus


# -- the guard: every field, everywhere ----------------------------------------------

def _problems(root: QWidget) -> list[str]:
    found = []
    for field in root.findChildren(QAbstractSpinBox):
        name = field.objectName() or type(field).__name__
        if field.buttonSymbols() != QAbstractSpinBox.ButtonSymbols.NoButtons:
            found.append(f"{name}: still has up/down arrows")
        if reset_button(field) is None:
            found.append(f"{name}: no back-to-default button")
    return found


@pytest.mark.gui
def test_no_number_field_in_the_window_has_arrows_or_lacks_the_button(gui_mainwindow):
    app, window, _store, _engine = gui_mainwindow
    for _ in range(20):
        app.processEvents()
    assert getattr(window, "settings_view", None) is not None, "Settings was never built"
    assert getattr(window, "indexing_view", None) is not None, "Indexing was never built"

    fields = window.findChildren(QAbstractSpinBox)
    assert len(fields) >= 20, f"expected the settings pages' number fields, found {len(fields)}"
    problems = _problems(window)
    assert not problems, "\n".join(problems)


def test_every_dialog_and_menu_with_a_number_field_is_covered(qapp):
    from PySide6.QtWidgets import QMenu

    from app.ui.view_options import ViewPreferences, build_menu
    from app.ui.widgets.add_file_type import AddFileTypeWizard
    from app.ui.widgets.file_types import EditFileTypeDialog
    from app.ui.widgets.index_flows import RebuildVectorsDialog
    from app.ui.widgets.photo_tagger_page import _BatchEraDialog

    parent = QWidget()
    built = [
        RebuildVectorsDialog("BAAI/bge-small-en-v1.5", chunk_count=10),
        EditFileTypeDialog(".pdf", "pdf", ["pdf", "text"], 50 << 20, True),
        AddFileTypeWizard(["text"], {}),
        _BatchEraDialog(),
        build_menu(parent, ViewPreferences(), columns=(("name", "Name"),),
                   available=("name",), on_change=lambda _p: None),
    ]
    for root in built:
        fields = root.findChildren(QAbstractSpinBox)
        assert fields, f"{type(root).__name__} builds no number field any more - update this list"
        assert not _problems(root), (type(root).__name__, _problems(root))
    menu = built[-1]
    assert isinstance(menu, QMenu)
    spin = menu.findChildren(QAbstractSpinBox)[0]
    assert reset_button(spin).toolTip() == "Back to the default (System)"


#: Every module under `app/ui` that builds a number field, and where the walk
#: above reaches it. A new one fails the test below until it is added here -
#: and to the walk, if it is not on a page of the window.
COVERED = {
    "indexing_settings.py": "window",
    "view_options.py": "menu",
    "widgets/photo_tagger_page.py": "dialog",
    "widgets/long_run_box.py": "window",
    "widgets/tuning_groups.py": "window",
    "widgets/chat_box.py": "window",
    "widgets/converter_box.py": "window",
    "widgets/search_box.py": "window",
    "widgets/model_box.py": "window",
    "widgets/file_types.py": "dialog",
    "widgets/index_flows.py": "dialog",
    "widgets/storage_box.py": "window",
    "widgets/media_box.py": "window",
    "widgets/add_file_type.py": "dialog",
    "widgets/timed_out_panel.py": "window",     # order 0z F3, on the Indexing page
    "widgets/mcp_box.py": "window",             # 2026-10-04, Settings > AI programs
}

_SPIN_TYPES = {"QSpinBox", "QDoubleSpinBox", "QTimeEdit", "QDateEdit",
               "QDateTimeEdit", "AutoSpin"}


def test_every_module_that_builds_a_number_field_is_walked():
    building = set()
    for path in sorted(UI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in _SPIN_TYPES):
                building.add(path.relative_to(UI).as_posix())
    missing = building - set(COVERED)
    assert not missing, (
        f"these build a number field the guard does not walk: {sorted(missing)}. "
        "Fit it (number_field.fit / fit_all) and add it to COVERED.")


def test_the_window_fits_everything_in_one_place():
    shell = (UI / "shell.py").read_text(encoding="utf-8")
    assert shell.count("fit_number_fields(self)") >= 3, (
        "each protect_all pass in the window needs its number-field pass beside it")
    assert number_field.RESET_ICON == "rotate-ccw"
