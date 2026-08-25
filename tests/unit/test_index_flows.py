"""The two settings that are flows, and the decisions they have to get right.

Layer: L5

`DATA_PATH` and `EMBED_MODEL` change what the index *is*. A text box for either
is a data-loss trap wearing the costume of a setting: typing a new path does not
move an index, it points at a different and probably empty one, and the person's
index appears to have vanished with no error anywhere.

The helpers below are the judgement each dialog rests on - is there an index
there, how big is it, is there room - and they are tested without a display,
because that is where the harm is decided.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.widgets.index_flows import (  # noqa: E402
    ADOPT,
    FRESH,
    MOVE,
    IndexLocationDialog,
    RebuildVectorsDialog,
    folder_gb,
    looks_like_an_index,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def make_index(root: Path) -> Path:
    (root / "vectors").mkdir(parents=True)
    (root / "fts").mkdir(parents=True)
    (root / "fts" / "knowledge.db").write_bytes(b"x" * 2048)
    return root


# --- the judgement ---------------------------------------------------------

def test_an_index_folder_is_recognised(tmp_path: Path):
    assert looks_like_an_index(make_index(tmp_path / "here"))


def test_an_empty_folder_is_not_an_index(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not looks_like_an_index(empty)


def test_a_missing_folder_is_not_an_index_and_does_not_raise(tmp_path: Path):
    assert not looks_like_an_index(tmp_path / "nowhere" / "deeper")


def test_folder_size_is_measured_not_guessed(tmp_path: Path):
    index = make_index(tmp_path / "here")
    assert folder_gb(index) > 0


def test_measuring_an_unreadable_folder_returns_zero_rather_than_raising(tmp_path):
    """A dialog being drawn must not die because a drive went away."""
    assert folder_gb(tmp_path / "gone") == 0.0


# --- the location dialog ---------------------------------------------------

def test_adopting_is_offered_only_where_an_index_exists(qapp, tmp_path: Path):
    """Adopting an empty folder does nothing at all, so it is not offered -
    rather than being allowed and failing afterwards."""
    current = make_index(tmp_path / "current")
    empty = tmp_path / "empty"
    empty.mkdir()

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(empty))

    assert not dialog.adopt.isEnabled()
    assert dialog.move.isEnabled()
    assert dialog.fresh.isEnabled()


def test_moving_onto_an_existing_index_is_not_offered(qapp, tmp_path: Path):
    """Moving there would have to overwrite it. Adopting is what somebody
    pointing at an existing index almost certainly means."""
    current = make_index(tmp_path / "current")
    other = make_index(tmp_path / "other")

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(other))

    assert dialog.adopt.isEnabled()
    assert not dialog.move.isEnabled()


def test_choosing_the_current_location_is_refused(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(current))

    assert "already is" in dialog.consequence.text()


def test_each_option_says_what_it_will_do_before_it_is_chosen(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")
    empty = tmp_path / "empty"
    empty.mkdir()

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(empty))

    dialog.move.setChecked(True)
    assert "removes the original" in dialog.consequence.text()
    assert "verified" in dialog.consequence.text(), "say the copy is checked first"

    dialog.fresh.setChecked(True)
    text = dialog.consequence.text()
    assert "nothing is lost" in text.lower()
    assert "finds nothing until" in text, "the cost has to be stated"


def test_the_choice_reports_what_was_picked(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")
    other = make_index(tmp_path / "other")

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(other))
    dialog.adopt.setChecked(True)

    choice = dialog.choice()
    assert choice.action == ADOPT
    assert choice.destination == other


def test_a_fresh_start_is_available_anywhere(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")
    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(tmp_path / "brand-new"))
    dialog.fresh.setChecked(True)

    assert dialog.choice().action == FRESH
    assert MOVE != FRESH != ADOPT


# --- the rebuild dialog ----------------------------------------------------

def test_typing_a_model_beats_whatever_was_last_picked(qapp):
    """**The expensive direction of a small bug.**

    The box is editable so any model can be named, but typing does not move
    `currentIndex` - so `currentData()` kept returning the preset that happened
    to be selected. This is the dialog that invalidates every vector and
    re-embeds the corpus; it would have spent those hours on a model nobody
    chose, and written it to `.env` afterwards.
    """
    dialog = RebuildVectorsDialog("a/model", chunk_count=1_000)
    dialog.model.setCurrentIndex(0)
    picked = dialog.chosen_model()
    dialog.model.setEditText("somebody/typed-this")

    assert dialog.chosen_model() == "somebody/typed-this", (
        f"the box says one thing and the dialog would use {picked!r}")


def test_picking_from_the_list_still_uses_its_identifier(qapp):
    """The dimensions shown beside a preset are for the reader and must never
    reach `.env` - so a display match still resolves through the item data."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=1_000)
    dialog.model.setCurrentIndex(0)

    assert "   —   " not in dialog.chosen_model()
    assert dialog.chosen_model() == dialog.model.itemData(0)


def test_the_cost_is_stated_in_hours_for_a_real_index(qapp):
    dialog = RebuildVectorsDialog("BAAI/bge-small-en-v1.5", chunk_count=4_200_000)
    dialog.model.setEditText("some/other-model")

    text = dialog.cost.text()
    assert "4,200,000" in text
    assert "hour" in text
    assert "poor until" in text, "say that search degrades meanwhile"


def test_a_small_index_says_minutes_rather_than_zero_hours(qapp):
    dialog = RebuildVectorsDialog("a/model", chunk_count=1_000)
    dialog.model.setEditText("b/model")

    assert "few minutes" in dialog.cost.text()


def test_the_same_model_is_not_a_change(qapp):
    """The button must not offer to spend six hours doing nothing."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=10_000)

    assert "already in use" in dialog.cost.text()
    assert dialog.chosen_model() == "a/model"


def test_an_unknown_chunk_count_does_not_invent_a_duration(qapp):
    """Zero means "could not read it". Saying "0 hours" would be a promise."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=0)
    dialog.model.setEditText("b/model")

    assert "hour" not in dialog.cost.text()
