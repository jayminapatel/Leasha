r"""The Status shelf tells this run's counts from the index's, and every chip has a picture.

Layer: L5

2026-10-26, the owner: *"look at the index status page that does not make sense"* -
the headline read "0 documents · 137,803 files seen · 2,523 skipped" over an index of
305,725, and the 2,523 were mostly photos held for their text pass
(`ERR_PICTURE_TEXT_LATER`). And *"tidy on the page no icon ... they dont have tool tips"*:
a step not yet reached drew an empty slot, and no chip or tile said what it was.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ui.presenter import cockpit as cp
from app.ui.presenter.indexing import progress_text, run_skipped_and_held


def _stats(**values):
    base = dict(phase="reading", paused=False, paused_by_person=False, indexed=0,
                seen=137_803, skipped=2_523, unchanged=0, chunks=0, walk_complete=True,
                recent_files_per_minute=None, files_per_minute=0.0, current="",
                skipped_by_code={"ERR_PICTURE_TEXT_LATER": 2_502, "ERR_CORRUPT": 21})
    base.update(values)
    return SimpleNamespace(**base)


def test_held_for_later_is_not_counted_as_skipped():
    assert run_skipped_and_held(_stats()) == (21, 2_502)
    assert run_skipped_and_held(_stats(skipped_by_code={})) == (2_523, 0)
    assert run_skipped_and_held(SimpleNamespace()) == (0, 0)
    # A by-code tally larger than the total never makes skipped negative.
    assert run_skipped_and_held(_stats(skipped=5)) == (0, 5)


def test_the_headline_says_this_run_and_held_for_later():
    headline, _detail = progress_text(_stats())
    assert headline.startswith("0 documents this run")
    assert "21 skipped" in headline
    assert "2,502 held for later" in headline


def test_the_tiles_count_this_run_and_explain_themselves():
    values = {caption: (value, tip) for caption, value, tip in cp.tiles(_stats())}
    assert values["Indexed this run"][0] == "0"
    assert "line above" in values["Indexed this run"][1]
    assert values["Skipped"][0] == "21"
    assert "2,502 held for later" in values["Skipped"][1]
    assert all(tip for _caption, _value, tip in cp.tiles(None)), "every tile has a tooltip"


def test_every_step_has_its_own_picture_and_a_tooltip():
    words = [step for step, _codes in cp.STEPS]
    assert set(cp.STEP_ICONS) == set(words) == set(cp.STEP_TIPS)
    assert len(set(cp.STEP_ICONS.values())) == len(words), "no two steps share a picture"
    assert cp.step_tip("Tidy", "todo").endswith("Not reached yet.")
    assert cp.step_tip("Tidy", "current").endswith("Happening now.")


def test_the_stepper_draws_a_picture_and_a_tip_on_every_chip(qtbot):
    from app.ui.widgets.cockpit_parts import PhaseStepper

    stepper = PhaseStepper()
    qtbot.addWidget(stepper)
    for glyph in stepper._glyphs:
        assert not glyph.pixmap().isNull(), "a step not reached still has its picture"
    for chip in stepper.chips:
        assert chip.toolTip()
    tidy = stepper.chips[[w for w, _c in cp.STEPS].index("Tidy")]
    assert tidy.toolTip().startswith("Tidies the index")
