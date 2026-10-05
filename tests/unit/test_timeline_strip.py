r"""The timeline strip widget. Workspace §3d.

Layer: L5 widget. No draft existed for this half - see `timeline_strip.py`'s
own docstring - so it is checked against `app/ui/timeline.py`'s `Band` shape
directly.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                     # noqa: E402

from app.ui.timeline import NANOS                              # noqa: E402
from app.ui.widgets.timeline_strip import TimelineStrip, enabled_checkbox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _row(d: date):
    ns = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * NANOS)
    return SimpleNamespace(mtime_ns=ns)


def test_the_strip_is_hidden_until_rows_arrive(qapp):
    strip = TimelineStrip()
    assert not strip.isVisible()


def test_setting_rows_with_no_spread_stays_hidden(qapp):
    strip = TimelineStrip()
    strip.set_rows([_row(date(2020, 1, 1))])
    assert strip._layout.count() == 0


def test_setting_a_wide_spread_draws_bars_and_shows(qapp):
    strip = TimelineStrip()
    rows = [_row(date(2019, 1, 1)), _row(date(2022, 6, 1)), _row(date(2019, 1, 6))]
    strip.set_rows(rows)
    assert strip._layout.count() >= 2


def test_clicking_a_bar_emits_the_after_before_filter(qapp):
    strip = TimelineStrip()
    rows = [_row(date(2019, 1, 1)), _row(date(2022, 6, 1))]
    strip.set_rows(rows)

    chosen: list = []
    strip.filter_chosen.connect(chosen.append)
    button = strip._layout.itemAt(0).widget()
    button.click()

    assert len(chosen) == 1
    assert chosen[0].startswith("after:") and " before:" in chosen[0]


def test_disabling_hides_the_bars_without_forgetting_them(qapp):
    strip = TimelineStrip()
    rows = [_row(date(2019, 1, 1)), _row(date(2022, 6, 1))]
    strip.set_rows(rows)
    assert strip._layout.count() > 0

    strip.set_enabled(False)
    assert strip._layout.count() == 0
    assert not strip.isVisible()

    strip.set_enabled(True)
    assert strip._layout.count() > 0


def test_set_rows_never_raises_on_bad_data(qapp):
    strip = TimelineStrip()
    strip.set_rows([object(), object()])
    assert strip._layout.count() == 0


def test_enabled_checkbox_defaults_on_with_no_store(qapp):
    box = enabled_checkbox(None, on_toggle=lambda _checked: None)
    assert box.isChecked()


def test_enabled_checkbox_reads_a_stored_off_state(qapp):
    store = SimpleNamespace(get_state=lambda key, default=None: "off")
    box = enabled_checkbox(store, on_toggle=lambda _checked: None)
    assert not box.isChecked()
