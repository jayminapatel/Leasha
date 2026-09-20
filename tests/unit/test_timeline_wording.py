r"""Order 202626270602 (0n) section 5 - the new surfaces speak plain English.

Layer: L4 + L5

"plain-words deny-list and tooltip-effect tests cover the new surfaces". The
deny-list is the one this project already enforces, **imported and never
retyped** (`test_adoption_scenarios.DENIED`): a second copy is a second guard
that can disagree with the first. What is swept is what a person can *read* -
every label, tooltip, placeholder, menu entry and sentence the Reports page,
the timeline and its right-click doors show - gathered from the built widgets
and the wording module rather than pasted, so a reworded sentence is swept on
the next run.

The tooltip half is the runtime twin of `test_tooltips.py`: that test proves
statically that a tooltip is *set*; this one reads it off the built control and
checks it says what pressing the control does.
"""

from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (                                      # noqa: E402
    QAbstractButton, QComboBox, QLineEdit, QMenu, QWidget,
)

from app.reports import timeline_words as words                    # noqa: E402
from app.reports.timeline import Overview                          # noqa: E402
from tests.unit.test_adoption_scenarios import DENIED              # noqa: E402
from tests.unit.timeline_env import june_2015                      # noqa: E402

pytestmark = pytest.mark.gui


def offenders(strings: dict) -> list[str]:
    return [f"{where}: {word}" for where, text in strings.items()
            for word in DENIED if word in str(text).lower()]


def test_the_sweep_would_notice_this_codebases_language():
    """A guard that has never rejected anything has not been shown to work."""
    assert offenders({"a made-up label": "3 embedding vector hits"})
    assert not offenders({"a real label": "Everything from June 2015"})


def _texts_of_the_wording_module() -> dict:
    out = {"BAD_DATE": words.BAD_DATE, "NOTHING_INDEXED": words.NOTHING_INDEXED}
    for label, table in (("basis", words.BASIS_WORDS), ("basis tip", words.BASIS_TIPS),
                         ("kind", words.KIND_WORDS), ("kind tip", words.KIND_TIPS)):
        out.update({f"{label} {key}": value for key, value in table.items()})
    return out


def _sample_overviews() -> list:
    return [
        Overview(),
        Overview(months=((2015, 6, 120), (2019, 1, 3)), by_camera=0, by_file_date=123,
                 photos_by_file_date_only=90, undated=4),
        Overview(months=((1999, 1, 1),), by_camera=1, by_folder_guess=1, photos_with_camera_date=1,
                 photos_by_file_date_only=1, undated=1),
    ]


def test_every_sentence_in_the_wording_module_passes_the_deny_list():
    strings = _texts_of_the_wording_module()
    for at, overview in enumerate(_sample_overviews()):
        strings[f"summary {at}"] = words.summary_sentence(overview)
        for n, note in enumerate(words.thin_data_notes(overview)):
            strings[f"note {at}.{n}"] = note
    from app.reports.timeline import Period

    for period in (Period.month(2015, 6), Period.year(1999), Period.between(None, None),
                   Period.from_words("2015-06-05", "2015-06-07"), Period.from_words("2015", "")):
        strings[f"period {period.after}"] = words.period_words(period)
        strings[f"empty {period.after}"] = words.empty_period_sentence(period)
    assert len(strings) > 30, "the sweep stopped finding the timeline's strings"
    assert not offenders(strings), offenders(strings)


def test_the_thin_data_sentences_say_what_is_missing_in_words_a_person_would_use():
    none = " ".join(words.thin_data_notes(Overview(photos_by_file_date_only=90)))
    assert "None of your 90 photos has a date from the camera" in none
    some = " ".join(words.thin_data_notes(Overview(photos_with_camera_date=5, photos_by_file_date_only=1)))
    assert "1 photo has no camera date" in some and "the other 5" in some
    assert "guesses" in " ".join(words.thin_data_notes(Overview(by_folder_guess=3)))
    assert "not on the timeline" in " ".join(words.thin_data_notes(Overview(undated=2)))
    assert words.thin_data_notes(Overview(photos_with_camera_date=5)) == []       # nothing thin, nothing said


def _visible_strings(root: QWidget) -> dict:
    out: dict = {}
    for at, child in enumerate(root.findChildren(QWidget)):
        name = f"{type(child).__name__}#{at}"
        for method in ("text", "toolTip", "placeholderText", "accessibleName"):
            value = getattr(child, method, None)
            text = value() if callable(value) else ""
            if isinstance(text, str) and text:
                out[f"{name}.{method}"] = text
        if isinstance(child, QComboBox):
            for i in range(child.count()):
                out[f"{name}.item{i}"] = child.itemText(i)
                tip = child.itemData(i, 3)
                if tip:
                    out[f"{name}.item{i}.tip"] = tip
    return out


@pytest.fixture
def built(qtbot, tmp_path):
    from app.ui.reports_view import ReportsView

    store, _ids = june_2015(tmp_path)
    view = ReportsView(store)
    qtbot.addWidget(view)
    view.resize(1000, 700)
    view.show()
    yield view
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


def test_every_label_tooltip_and_placeholder_on_the_reports_page_and_timeline_passes(built):
    strings = _visible_strings(built)
    for row in range(built.list.count()):
        strings[f"reports row {row}"] = built.list.item(row).text()
        strings[f"reports row {row} tip"] = built.list.item(row).toolTip()
    assert len(strings) > 40
    assert not offenders(strings), offenders(strings)


def test_every_control_on_the_timeline_says_what_pressing_it_does(built):
    r"""Runtime tooltip-effect: not merely present, but a sentence about an effect."""
    timeline = built.timeline
    controls = timeline.findChildren((QAbstractButton, QComboBox, QLineEdit))
    assert len(controls) >= 18                     # kind, group, year, 13 month buttons, 2 dates, Show
    for control in controls:
        said = control.toolTip() or (control.placeholderText() if isinstance(control, QLineEdit) else "")
        assert said, f"{type(control).__name__} {getattr(control, 'text', lambda: '')()!r} says nothing"
        assert len(said.split()) >= 3, said
    # Buttons whose *label* is a bare abbreviation must explain it:
    assert all(b.toolTip() for b in timeline.picker.month_buttons)
    assert "both days included" in timeline.picker.range_go.toolTip()
    assert "Right-click" in timeline.picker.fold_box.toolTip()
    for at in range(timeline.picker.kind_box.count()):
        assert timeline.picker.kind_box.itemData(at, 3)


def test_the_right_click_doors_speak_plain_english(qtbot, monkeypatch):
    from app.ui.widgets.file_menu import FileActions, build_menu
    from app.ui.widgets.timeline_strip import TimelineStrip

    parent = QWidget()
    qtbot.addWidget(parent)
    menu = build_menu(parent, "C:/x.txt", FileActions(same_period=lambda: None))
    strings = {a.text(): a.toolTip() for a in menu.actions() if a.text()}
    strip = TimelineStrip()
    qtbot.addWidget(strip)

    class Row:
        def __init__(self, n):
            self.mtime_ns = n

    from tests.unit.timeline_env import noon

    strip.set_rows([Row(noon(2015, 6, 10)), Row(noon(2016, 1, 10))])
    button = strip._layout.itemAt(0).widget()
    shown: dict = {}
    monkeypatch.setattr(QMenu, "exec", lambda self, *_a: shown.update(
        {a.text(): a.toolTip() for a in self.actions()}))
    button.customContextMenuRequested.emit(button.rect().center())
    strings.update(shown)
    strings["strip tooltip"] = button.toolTip()
    assert "See everything from this month" in strings and "See everything from this period" in strings
    assert not offenders({**strings, **{f"{k} (tip)": v for k, v in strings.items()}}), offenders(strings)


def test_the_presenters_sentences_pass_and_say_what_they_mean(tmp_path):
    from app.reports.timeline import Period, timeline_page
    from app.ui.presenter.timeline import loading_sentence, photo_tip, row_text, shown_sentence

    store, _ids = june_2015(tmp_path)
    try:
        page = timeline_page(store, Period.month(2015, 6), connected={})
        strings = {"loading": loading_sentence("June 2015"),
                   "shown 1": shown_sentence(1, True), "shown many": shown_sentence(1234, False)}
        for fold in page.items:
            strings[f"tip {fold.head.name}"] = photo_tip(fold)
            text = row_text(fold)
            strings[f"row {fold.head.name}"] = " | ".join([text.title, text.detail, text.basis,
                                                            text.basis_tip, text.badge, text.folded])
        assert not offenders(strings), offenders(strings)
        assert strings["shown 1"] == "1 item - that is everything from this period."
        assert strings["shown many"] == "1,234 items so far - scroll down for more."
        assert re.search(r"Old WD - not plugged in", " ".join(strings.values()))
    finally:
        store.close()


def test_the_command_lines_help_speaks_plain_english():
    from app import cli

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None) and "timeline" in a.choices)
    timeline = sub.choices["timeline"]
    strings = {"description": timeline.description or ""}
    for action in timeline._actions:
        strings[f"{action.dest} help"] = action.help or ""
    strings["help line"] = next(c.help for c in sub._choices_actions if c.dest == "timeline")
    assert not offenders(strings), offenders(strings)
