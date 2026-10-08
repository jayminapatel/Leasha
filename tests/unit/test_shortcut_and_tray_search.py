"""The quick search shortcut, when Windows refuses it, and Tray › Search….

Layer: L5

2026-10-08, the owner: "the ctrl alt l shortcut is not working and the search
from the notification should bring the same box". Checked on the laptop: the
code works - with a free combination the press arrives - but another program
holds Ctrl+Alt+L (RegisterHotKey error 1409), and Ctrl+Alt+Space too. Only
Settings said so. And the tray's Search… brought the main window forward
instead of opening the quick box.
"""

from __future__ import annotations

from app.ui.hotkey import first_free, refused_notice


# --- a combination that is free ---------------------------------------------


def test_the_suggestion_is_the_first_combination_windows_would_grant():
    taken = {"Ctrl+Shift+Space"}
    assert first_free(("Ctrl+Shift+Space", "Ctrl+Alt+K"),
                      can_take=lambda text: text not in taken) == "Ctrl+Alt+K"


def test_no_suggestion_when_every_combination_is_taken():
    assert first_free(("Ctrl+Alt+K",), can_take=lambda _text: False) is None


def test_a_check_that_fails_is_skipped_not_raised():
    def broken(text):
        if text == "Ctrl+Alt+K":
            raise OSError("no")
        return True

    assert first_free(("Ctrl+Alt+K", "Ctrl+Shift+L"), can_take=broken) == "Ctrl+Shift+L"


def test_the_notice_names_the_shortcut_the_way_out_and_the_tray():
    text = refused_notice("ctrl+alt+l", "Ctrl+Alt+K")
    assert "Ctrl+Alt+L" in text and "Ctrl+Alt+K is free" in text
    assert "Settings, Search" in text and "tray" in text
    assert "is free" not in refused_notice("Ctrl+Alt+L", None)


# --- the window says it once ------------------------------------------------


class _Window:
    def __init__(self):
        self.said: list = []

    def notify(self, text, timeout_ms=None, *, level="info"):
        self.said.append((text, level))


def _say(window, monkeypatch, *, wanted=True, taken=False, text="Ctrl+Alt+L"):
    from app.ui import hotkey
    from app.ui.controllers.settings_controller import SettingsController

    monkeypatch.setattr(hotkey, "available", lambda: True)
    monkeypatch.setattr(hotkey, "first_free", lambda *a, **k: "Ctrl+Alt+K")
    controller = SettingsController.__new__(SettingsController)
    controller._w = window
    controller._say_if_refused(text, wanted=wanted, taken=taken)


def test_a_refused_shortcut_is_said_once_as_a_warning(monkeypatch):
    window = _Window()
    _say(window, monkeypatch)
    _say(window, monkeypatch)
    assert len(window.said) == 1
    text, level = window.said[0]
    assert level == "warning" and "Ctrl+Alt+K is free" in text


def test_nothing_is_said_when_it_was_granted_or_switched_off(monkeypatch):
    window = _Window()
    _say(window, monkeypatch, taken=True)
    _say(window, monkeypatch, wanted=False)
    assert window.said == []


def test_a_new_combination_refused_too_is_said_again(monkeypatch):
    window = _Window()
    _say(window, monkeypatch, text="Ctrl+Alt+L")
    _say(window, monkeypatch, text="Ctrl+Alt+Space")
    assert len(window.said) == 2


# --- Tray › Search… ---------------------------------------------------------


def test_tray_search_opens_the_quick_box_not_the_window():
    from app.ui.tray import TrayPresence

    class Window:
        shown = False
        quick = 0

        def _search_from_tray(self):
            self.quick += 1

        def show(self):
            self.shown = True

    window = Window()
    TrayPresence(window).restore_and_search()
    assert window.quick == 1 and window.shown is False


class _Box:
    def __init__(self):
        self.visible = True
        self.calls: list = []

    def isVisible(self):                # noqa: N802 - Qt's name
        return self.visible

    def dismiss(self):
        self.calls.append("dismiss")
        self.visible = False

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):           # noqa: N802 - Qt's name
        self.calls.append("activate")

    def summon(self):
        self.calls.append("summon")


def test_tray_search_brings_an_open_box_forward_and_keeps_what_was_typed():
    from app.ui.shell import MainWindow

    class Shell:
        _mini = _Box()

    shell = Shell()
    MainWindow._summon_mini(shell, toggle=False)
    assert shell._mini.calls == ["raise", "activate"]


def test_the_shortcut_still_hides_an_open_box():
    from app.ui.shell import MainWindow

    class Shell:
        _mini = _Box()

    shell = Shell()
    MainWindow._summon_mini(shell)
    assert shell._mini.calls == ["dismiss"]
