r"""Aesthetics found by looking at the real window on Windows at 125% (2026-09-20).

Every one of these was seen in a screenshot before it was written down, and each
test says what the picture showed. They are property checks, not pixel diffs: the
offscreen platform this suite runs on has other fonts, so a test that measured
text widths would pass or fail on the font, not on the fix.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# --- the stylesheet ----------------------------------------------------------

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_labels_do_not_paint_a_box_of_their_own(scheme: str) -> None:
    """The picture: every settings row, and the whole toast, had a grey box round
    its text, because `QWidget { background: window }` reaches labels too."""
    sheet = theme.stylesheet(scheme, detected=scheme, base_pt=9.0)
    assert re.search(r"QLabel, QCheckBox, QRadioButton\s*\{\s*background:\s*transparent;", sheet)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_toast_text_is_drawn_in_the_toast_colour(scheme: str) -> None:
    """The picture: dark text on a light box inside a dark toast, and the reverse."""
    sheet = theme.stylesheet(scheme, detected=scheme, base_pt=9.0)
    colours = theme.theme_colours()
    assert f"#toastText {{ color: {colours['toast_text']};" in sheet


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_info_dot_can_be_seen_on_the_toast(scheme: str) -> None:
    """The picture: a navy dot on a navy toast, in the light theme."""
    sheet = theme.stylesheet(scheme, detected=scheme, base_pt=9.0)
    colours = theme.theme_colours()
    assert f'#toastDot[level="info"] {{ background: {colours["toast_text"]}; }}' in sheet
    assert colours["toast_text"] != colours["toast_bg"]


# --- the rail's indexing pill ------------------------------------------------

_HEADLINES = ("Up to date", "Needs attention", "Indexing", "Paused", "Stopped", "Index",
              "Nothing yet")      # order 0x section 9: the empty index


def test_the_pill_headline_wraps_instead_of_clipping(qapp) -> None:
    """The picture: "Up to date" read "p to dat" - a 58px headline in a 40px label."""
    from app.ui.rail_state import PillState
    from app.ui.widgets.rail import Rail

    # **On the rail, not on the application.** `QApplication.setStyleSheet` makes Qt
    # re-polish every widget alive in the process, and this suite leaks widgets: tests
    # build real ones and let Python drop them without `deleteLater`, so by the time a
    # long run reaches this file the walk eventually steps into a C++ object that is
    # already gone and the process dies with an access violation - three times on
    # 2026-09-20, always here, at -j 4 and again at -j 3, and never when this file runs
    # alone. Reproduced deliberately with the fourteen files that precede it, and neither
    # half of those crashes on its own: it is the number of leaked widgets, not one
    # culprit. Qt cascades a stylesheet to children, so the pill's labels get the same
    # metrics from the rail as they did from the application, and nothing global is
    # touched. The leak itself is the real defect and is recorded in HANDOFF.md's traps.
    rail = Rail()
    rail.setStyleSheet(theme.stylesheet("light", detected="light", base_pt=9.0))
    rail.resize(72, 600)
    rail.show()
    for headline in _HEADLINES:
        rail.show_pill(PillState(headline, "1,234 so far", busy=False), None)
        qapp.processEvents()
        label = rail.pill.headline
        assert label.wordWrap(), headline
        # Held to the height its wrapped text needs, whatever the layouts above
        # report - a headline of two lines was cut to one.
        assert label.minimumHeight() >= label.heightForWidth(label.width()) > 0, headline
    rail.close()


def test_the_pill_has_no_side_padding_beyond_its_layout() -> None:
    """The stylesheet padded the pill 4px a side on top of the layout's 6, leaving
    40px of a 60px pill for the words."""
    sheet = theme.stylesheet("light", detected="light", base_pt=9.0)
    pill = re.search(r"#railPill \{[^}]*\}", sheet)
    assert pill is not None and "padding: 6px 0;" in pill.group(0)


# --- the results list --------------------------------------------------------

def test_the_results_list_lays_its_rows_out_again_when_it_is_resized(qapp) -> None:
    """The picture: with the preview and inspector open the list was 207px wide and
    its rows still 269, so the date and path were clipped and a horizontal
    scrollbar appeared. `Fixed` (the default) never lays out again."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListView

    from app.ui.results_view import ResultsView

    view = ResultsView()
    assert view._list.resizeMode() == QListView.ResizeMode.Adjust
    assert view._list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


# --- the Offline Media page --------------------------------------------------

def test_offline_media_text_keeps_its_own_height(qapp) -> None:
    """The picture: with no drive catalogued the intro, buttons, empty-state line
    and help text sat a screen apart, because the labels were the only things that
    could take the spare height."""
    from PyQt6.QtWidgets import QSizePolicy

    from app.ui.offline_media_view import OfflineMediaView

    view = OfflineMediaView()
    for label in (view.empty, view.status_line, view.help_line):
        assert label.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
    layout = view.layout()
    tree_at = next(i for i in range(layout.count()) if layout.itemAt(i).widget() is view.tree)
    assert layout.stretch(tree_at) == 1


# --- the taskbar -------------------------------------------------------------

def test_relaunch_properties_are_a_no_op_off_windows(monkeypatch) -> None:
    from app.ui import tray

    monkeypatch.setattr(sys, "platform", "linux")
    assert tray.set_window_relaunch(1) is False


def test_a_bad_window_id_never_raises() -> None:
    """Cosmetic: a failure here must never stop the app opening."""
    from app.ui import tray

    assert tray.set_window_relaunch(0) in (True, False)


_READ_BACK = r'''
import os, sys, json
os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication, QWidget
app = QApplication([])
window = QWidget()
window.show()
app.processEvents()
from app.ui import tray
hwnd = int(window.winId())
ok = tray.set_window_relaunch(hwnd)
from win32com.propsys import propsys, pscon
store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
read = {name: store.GetValue(getattr(pscon, "PKEY_AppUserModel_" + name)).GetValue()
        for name in ("ID", "RelaunchCommand", "RelaunchIconResource",
                     "RelaunchDisplayNameResource")}
print("RESULT " + json.dumps({"ok": ok, "read": read}))
'''


def test_the_relaunch_properties_can_be_read_back_from_a_real_window() -> None:
    """**What this proves, and what it does not.** It proves the four properties
    reach the window's property store on the Windows platform plugin - written,
    committed, and readable back as the strings set. It does **not** prove that
    a taskbar *pin* then relaunches Leasha or shows its icon: that is decided by
    the shell when somebody pins, and has never been observed here (unpin and
    re-pin Leasha, then check the pinned button's icon and that it opens the
    app). Two doubts are recorded rather than settled: the command is
    `leasha.cmd`, a batch file, not an executable - it is used because a pinned
    shortcut has no working directory and the launcher is what `cd`s to the
    install - and the display name is a plain string where the property is
    documented as a resource-style string.
    """
    import json
    from pathlib import Path
    import subprocess

    pytest.importorskip("win32com.propsys")
    if sys.platform != "win32":
        pytest.skip("the Windows taskbar")
    from app.ui import tray

    root = Path(__file__).resolve().parents[2]
    run = subprocess.run([sys.executable, "-c", _READ_BACK], cwd=str(root),
                         capture_output=True, text=True, timeout=90)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("RESULT ")), None)
    if line is None:
        pytest.skip("no window could be made on the Windows platform plugin here: "
                    + run.stderr[-300:])
    found = json.loads(line[len("RESULT "):])
    launcher, icon = root / "leasha.cmd", tray.icon_path()

    assert found["ok"] is True
    assert found["read"] == {
        "ID": tray.APP_USER_MODEL_ID,
        "RelaunchCommand": f'"{launcher}"',
        "RelaunchIconResource": f"{icon},0",
        "RelaunchDisplayNameResource": "Leasha",
    }
    assert launcher.is_file() and icon is not None and icon.is_file()
