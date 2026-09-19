r"""Section 4 of the review-remediation order: UI polish and hygiene.

Layer: L5

`docs/WORKORDER-202626082352-review-remediation.md` §4. Smaller than the
sections before it and not less real: a results list that is empty to a screen
reader, two switches for one setting that disagree, and a modal dialog per
keystroke are all things somebody meets on their first afternoon with the
application.

**Asserted without a display wherever possible.** Several of these are about
what a slot does *not* do - block, allocate, dialog - and the cheapest honest
way to check that is to read the code as code rather than to build a window and
hope the fault reproduces offscreen.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def _body(module: str, function: str) -> str:
    """The raw source of a function, prose and all. For presence checks only."""
    text = (UI / module).read_text(encoding="utf-8")
    return text.split(f"def {function}(")[1].split("\n    def ")[0]


def _code(module: str, function: str) -> str:
    r"""A function's **code**, with its docstring and comments removed.

    This project has now been bitten three times by a test that greps source
    text and matches the comment explaining why the thing it forbids is not
    there - `test_narrowing_the_tree_runs_no_subprocess` on the word
    "subprocess", `test_the_close_path_checks_the_flag` on "stop", and two of
    the tests below on their own first draft. A test that cannot tell an
    explanation from the thing it explains gets silenced rather than believed.

    `ast.unparse` drops comments outright; the docstring is dropped by hand,
    since it survives as the first statement.
    """
    source = textwrap.dedent(_body(module, function))
    tree = ast.parse(f"def {function}({source}")
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(node)


# ---------------------------------------------------------------------------
# M19 - the list says something to a screen reader
# ---------------------------------------------------------------------------

def test_a_result_row_carries_text_for_assistive_technology():
    r"""**Fifty results announced as fifty blanks.**

    Painting moved into `result_delegate`, which reads `ROLE_PAYLOAD` - so the
    model item itself carried no text. `QAccessible` reads
    `AccessibleTextRole` and falls back to `DisplayRole`, and neither was set,
    so there was nothing to fall back to.
    """
    body = _body("results_view.py", "_append")

    assert "AccessibleTextRole" in body, (
        "the results list is still invisible to a screen reader")
    assert "DisplayRole" in body, "no fallback for anything that reads Display"


def test_the_spoken_text_names_the_result_rather_than_reciting_it():
    """The tooltip is three lines with the full path and an explanation - right
    to hover, wrong to have read aloud for every row while arrowing down."""
    from app.ui.presenter import accessible_text

    class Row:
        name = "Safety report.pdf"
        folder = "Projects/Leeds"
        when = "yesterday"

    spoken = accessible_text(Row())

    assert spoken.startswith("Safety report.pdf")
    assert "Leeds" in spoken and "yesterday" in spoken
    assert "\n" not in spoken, "a screen reader reads this as one utterance"


def test_a_row_with_only_a_path_still_says_something():
    """Federated and history rows do not all carry a `name`."""
    from app.ui.presenter import accessible_text

    class Row:
        path = r"D:\work\notes\meeting.md"

    assert "meeting.md" in accessible_text(Row())


# ---------------------------------------------------------------------------
# M12 - one rerank setting, two controls
# ---------------------------------------------------------------------------

def test_both_rerank_controls_are_driven_from_the_stored_value():
    r"""The toolbar box was hard-coded checked and never persisted while the
    Settings box was persisted - so they disagreed from the first launch after
    anybody changed it, and the toolbar is the one every search reads."""
    # The handlers moved to `SettingsController` (order 202626082352 section
    # 7): `MainWindow` keeps same-named forwarding methods, so it is the
    # controller's source that says whether both boxes are really driven.
    text = (UI / "controllers" / "settings_controller.py").read_text(encoding="utf-8")

    assert "_set_toolbar_rerank" in text, "the toolbar box is still not driven"
    handler = _body("controllers/settings_controller.py", "_rerank_toggled")
    assert "_set_toolbar_rerank" in handler and "settings_view" in handler, (
        "one handler must move both controls, or they drift apart again")


def test_setting_one_control_cannot_bounce_off_the_other():
    """Both report into the same handler, so setting one from it would emit
    back in. Signals are blocked on the way in; without that the two would
    ping-pong."""
    handler = _body("controllers/settings_controller.py", "_rerank_toggled")
    setter = _body("controllers/settings_controller.py", "_set_toolbar_rerank")

    assert "blockSignals" in handler and "blockSignals" in setter


# ---------------------------------------------------------------------------
# M13 - nothing touches the store during construction
# ---------------------------------------------------------------------------

def test_settings_does_not_read_the_store_while_being_built():
    r"""`MainWindow.__init__` states the rule and `SettingsView.__init__` broke
    it: a `COUNT(*)` and an import probe for `pst_libpff`, on the UI thread,
    before the first frame."""
    constructor = _code("settings_view.py", "__init__")

    assert "count_searches" not in constructor
    assert "pst_libpff" not in constructor
    assert "refresh_slow_labels" not in constructor, (
        "even the worker must not start during construction - the window's "
        "rule is that nothing runs on a thread until construction is over")


def test_the_window_fills_those_labels_once_it_is_running():
    """Moved, not dropped. `_start_background_work` is the first moment
    anything may touch a thread or the store."""
    assert "refresh_slow_labels" in _body("shell.py", "_start_background_work")


def test_the_labels_say_what_they_are_doing_until_then():
    """A blank label is indistinguishable from a broken one."""
    text = (UI / "settings_view.py").read_text(encoding="utf-8")
    constructor = text.split("def __init__(")[1].split("\n    def ")[0]

    assert "Counting" in constructor or "…" in constructor


# ---------------------------------------------------------------------------
# M11 - images are decoded on a worker
# ---------------------------------------------------------------------------

def test_an_image_is_not_decoded_in_a_ui_thread_slot():
    r"""`QPixmap(path)` reads and decodes on the calling thread, and this is a
    slot - so a large scan, or any image on a network share, froze the window
    for as long as the decode took."""
    body = _code("widgets/preview.py", "_show_image")

    assert "Worker(" in body or "run(" in body, (
        "_show_image still decodes inline")
    assert "QPixmap(" not in body, "still constructing a QPixmap from a path"


def test_the_decode_returns_a_qimage_because_qpixmap_is_ui_only():
    """`QPixmap` may only be built on the UI thread; `QImage` may be decoded
    anywhere. Converting afterwards is a wrap, not a second decode."""
    from app.ui import preview_loader

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(preview_loader.decode_image)))
    node = tree.body[0]
    node.body = node.body[1:]                    # drop the docstring
    code = ast.unparse(node)

    assert "QImage" in code
    assert "QPixmap" not in code


def test_a_stale_decode_is_dropped():
    """The pane already generation-stamps everything else it does; a decode
    that lands after the person has clicked elsewhere must not paint."""
    assert "generation" in _body("widgets/preview.py", "_draw_image")


# ---------------------------------------------------------------------------
# M10 - a failed background search is a notice, not a dialog
# ---------------------------------------------------------------------------

def test_a_failed_search_does_not_raise_a_dialog():
    r"""This fires per debounced keystroke. A transiently locked database -
    which is exactly what an index run produces - used to raise one QMessageBox
    per character typed, each needing dismissal before the next appeared."""
    text = (UI / "search_view.py").read_text(encoding="utf-8")

    assert "self._search_failed" in text, "search failures still go to self.error"
    handler = _body("search_view.py", "_search_failed")
    assert "notices" in handler
    assert "QMessageBox" not in handler


# ---------------------------------------------------------------------------
# The four small ones
# ---------------------------------------------------------------------------

def test_the_snippet_height_matches_what_is_drawn():
    r"""`sizeHint` reserved two lines and `_paint_snippet` draws one, eliding at
    the right edge - so every long-snippet row carried a blank line under it,
    costing about a result per screenful."""
    from PyQt6.QtGui import QFont, QFontMetrics

    pytest.importorskip("PyQt6")
    from app.ui.result_delegate import _snippet_height

    font = QFont()
    one_line = QFontMetrics(font).height()
    long_text = "pump station commissioning " * 40

    assert _snippet_height(font, long_text, 200) == one_line
    assert _snippet_height(font, "", 200) == 0


def test_the_view_menu_is_deleted_when_it_closes():
    """One is built per click and parented to the button, so Qt kept every one
    of them - with its actions, spin box and labels - for the button's life."""
    text = (UI / "view_options.py").read_text(encoding="utf-8")

    assert "WA_DeleteOnClose" in text


def test_the_code_tab_has_a_shortcut_like_every_other_find_tab():
    """Search, Files and Mail all have a key that jumps to their box. Code did
    not - the one tab whose users are most likely keyboard-driven."""
    body = _body("shell.py", "_build_shortcuts")

    assert "_focus_code" in body


def test_ctrl_enter_is_gated_on_interpret_being_available():
    r"""Interpret is hidden when Ollama is off or absent, and the shortcut was
    not - so Ctrl+Enter started a translation that could only fail, from a
    feature the person had switched off or never had."""
    text = (UI / "widgets" / "search_bar.py").read_text(encoding="utf-8")
    shortcut = text.split("for keys in (")[1].split("\n\n")[0]

    assert "isVisible" in shortcut


def test_the_scheduler_does_not_read_the_store_every_minute():
    r"""`status()` runs on every tick - once a minute for as long as the window
    is open - and called `_load_last_run()` each time, to produce a status-bar
    string that changes only when a run ends."""
    body = _body("scheduler.py", "_safe_last_run")

    assert "_read_stored" in body or "_stored_last_run" in body, (
        "the stored value is still re-read on every tick")


def test_the_scheduler_writes_the_last_run_on_a_worker():
    """Once per run rather than per minute, but still a store write from a Qt
    slot - landing exactly when the run has released the write lock and the
    vector store is compacting."""
    body = _body("scheduler.py", "notify_finished")

    assert "Worker(" in body or "run_worker(" in body
