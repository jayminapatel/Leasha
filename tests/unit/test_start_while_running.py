"""Start pressed while a run is going says so, and offers to stop it.

Layer: L5

2026-09-29: the owner pressed Start with "Index in a separate process" on
while a long PST run was going and reported that the button "did not work".
It had shown a five-second note and nothing else. A second run into the same
index is rightly refused; the refusal now asks, and "Stop the current run"
does exactly that.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ui.controllers.index_controller import IndexController


class _View:
    def __init__(self) -> None:
        self.stopped = 0

    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped += 1


def _window():
    notes: list = []
    shown: list = []
    view = _View()
    window = SimpleNamespace(
        indexing_view=view, notify=lambda text, ms: notes.append(text),
        _show=shown.append, _resolving_index=False)
    return window, view, notes, shown


def _start(window, monkeypatch, answer: bool) -> list:
    asked: list = []

    def confirm(parent):
        asked.append(parent)
        return answer

    monkeypatch.setattr(IndexController, "confirm_stop_running", staticmethod(confirm))
    controller = IndexController.__new__(IndexController)
    controller._w = window
    controller._start_indexing()
    return asked


def test_keeping_the_run_changes_nothing_but_says_why(monkeypatch) -> None:
    window, view, notes, shown = _window()
    asked = _start(window, monkeypatch, answer=False)
    assert asked == [window], "the person is asked, not left with a passing note"
    assert view.stopped == 0 and shown == [view]
    assert window._resolving_index is False, "no second run was begun"
    # The pre-order note, word for word (test_ui_redesign keeps it that way).
    assert notes == ["An index run is already in progress."]


def test_stopping_the_run_stops_it_and_says_what_next(monkeypatch) -> None:
    window, view, notes, _ = _window()
    _start(window, monkeypatch, answer=True)
    assert view.stopped == 1
    assert "Press Start again" in notes[-1]
