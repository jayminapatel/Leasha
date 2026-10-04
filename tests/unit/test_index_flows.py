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
    EMBED_MODELS,
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


def settled(dialog):
    """Dated note, 2026-10-04, code review: the dialog asks a worker about the
    folder in the box once the typing pauses, and measures the index once,
    as it opens - so a test waits for both answers before reading it."""
    import time

    from PyQt6.QtCore import QThreadPool

    app = QApplication.instance()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        QThreadPool.globalInstance().waitForDone(50)
        app.processEvents()
        if not dialog.checking() and not dialog._check_timer.isActive():
            break
        time.sleep(0.01)
    return dialog


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
    settled(dialog)

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
    settled(dialog)

    assert dialog.adopt.isEnabled()
    assert not dialog.move.isEnabled()


def test_choosing_the_current_location_is_refused(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(current))
    settled(dialog)

    assert "already is" in dialog.consequence.text()


def test_each_option_says_what_it_will_do_before_it_is_chosen(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")
    empty = tmp_path / "empty"
    empty.mkdir()

    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(empty))
    settled(dialog)

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
    settled(dialog)
    dialog.adopt.setChecked(True)

    choice = dialog.choice()
    assert choice.action == ADOPT
    assert choice.destination == other


def test_a_fresh_start_is_available_anywhere(qapp, tmp_path: Path):
    current = make_index(tmp_path / "current")
    dialog = IndexLocationDialog(current)
    dialog.destination.setText(str(tmp_path / "brand-new"))
    settled(dialog)
    dialog.fresh.setChecked(True)

    assert dialog.choice().action == FRESH
    assert MOVE != FRESH != ADOPT


# --- the rebuild dialog ----------------------------------------------------
#
# Dated note, 2026-09-29: the owner - "where there are models it has to be
# dropdown only no manual entry for models". The box is no longer editable, so
# the tests below that typed a model now choose a listed one, and the typing
# test became the two after it: nothing can be typed, and a model already in
# use that the list does not know is shown, marked, rather than swapped.

def _pick(dialog, identifier="BAAI/bge-base-en-v1.5"):
    dialog.model.setCurrentIndex(dialog.model.findData(identifier))


def test_the_meaning_model_cannot_be_typed(qapp):
    """**The expensive direction of a small bug**, closed for good: a model
    that cannot be typed cannot be typed wrongly, and the dialog that
    invalidates every vector only ever uses a name from its own list."""
    dialog = RebuildVectorsDialog("BAAI/bge-small-en-v1.5", chunk_count=1_000)

    assert not dialog.model.isEditable()
    assert dialog.model.lineEdit() is None


def test_an_unlisted_model_in_use_is_shown_as_itself_and_marked(qapp):
    """An `.env` from before the list was the only way in may name a model the
    list does not know. It is still the model in use: shown, selected, marked -
    never quietly replaced by the first entry."""
    from app.ui.widgets.index_flows import NOT_LISTED

    dialog = RebuildVectorsDialog("somebody/older-model", chunk_count=1_000)

    assert dialog.chosen_model() == "somebody/older-model"
    assert NOT_LISTED in dialog.model.currentText()
    assert "already in use" in dialog.cost.text()


def test_picking_from_the_list_still_uses_its_identifier(qapp):
    """The dimensions shown beside a preset are for the reader and must never
    reach `.env` - so a display match still resolves through the item data."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=1_000)
    dialog.model.setCurrentIndex(0)

    assert "   —   " not in dialog.chosen_model()
    assert dialog.chosen_model() == dialog.model.itemData(0)


def test_the_cost_is_stated_in_hours_for_a_real_index(qapp):
    dialog = RebuildVectorsDialog("BAAI/bge-small-en-v1.5", chunk_count=4_200_000)
    _pick(dialog, "sentence-transformers/all-MiniLM-L6-v2")      # the same width, 384

    text = dialog.cost.text()
    assert "4,200,000" in text
    assert "hour" in text
    assert "poor until" in text, "say that search degrades meanwhile"


def test_a_small_index_says_minutes_rather_than_zero_hours(qapp):
    dialog = RebuildVectorsDialog("a/model", chunk_count=1_000)
    _pick(dialog)

    assert "few minutes" in dialog.cost.text()


def test_the_same_model_is_not_a_change(qapp):
    """The button must not offer to spend six hours doing nothing."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=10_000)

    assert "already in use" in dialog.cost.text()
    assert dialog.chosen_model() == "a/model"


def test_an_unknown_chunk_count_does_not_invent_a_duration(qapp):
    """Zero means "could not read it". Saying "0 hours" would be a promise."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=0)
    _pick(dialog)

    assert "hour" not in dialog.cost.text()


# -- the width travels with the model ----------------------------------------
#
# Reported from the window on 2026-08-26: the model was changed from the GUI
# and nothing else touched. `EMBED_MODEL` was written alone, `EMBED_DIM` kept
# describing the previous model, and the mismatch surfaced only after a 219MB
# download as ERR_MODEL_LOAD naming a setting the owner had never edited.

def test_every_listed_model_carries_its_width_as_a_number():
    """It used to live in the note text - "set EMBED_DIM to 768" - which is an
    instruction, not a value, and instructions in a dropdown are not followed."""
    for entry in EMBED_MODELS:
        identifier, dim, note = entry
        assert isinstance(identifier, str) and identifier
        assert isinstance(dim, int) and dim > 0, f"{identifier} has no width"
        assert isinstance(note, str)
        assert "EMBED_DIM" not in note, (
            f"{identifier}: the width is data now, not an instruction in prose")


def test_choosing_a_listed_model_answers_with_that_models_width(qapp):
    """The regression, exactly as it happened: 384 in use, bge-base chosen."""
    dialog = RebuildVectorsDialog(
        "BAAI/bge-small-en-v1.5", chunk_count=39_306, current_dim=384)
    dialog.model.setCurrentIndex(dialog.model.findData("BAAI/bge-base-en-v1.5"))

    assert dialog.chosen_model() == "BAAI/bge-base-en-v1.5"
    assert dialog.chosen_dim() == 768, "the old width would break the index"


def test_a_listed_model_never_asks_for_a_number_it_knows(qapp):
    """A question with one right answer is a question that gets typed wrongly."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=10, current_dim=384)
    dialog.model.setCurrentIndex(dialog.model.findData("BAAI/bge-base-en-v1.5"))

    assert dialog.known_dim() == 768
    assert not dialog.dim_row.isVisibleTo(dialog), "asked for a known width"


def test_an_unlisted_model_asks_rather_than_guesses(qapp):
    """An unlisted model is a legitimate choice and its width cannot be known.
    Guessing it is the bug; asking is the fix."""
    dialog = RebuildVectorsDialog("somebody/typed-this", chunk_count=10, current_dim=384)

    assert dialog.known_dim() is None
    assert dialog.dim_row.isVisibleTo(dialog), "guessed a width it cannot know"

    dialog.dim.setValue(1024)
    assert dialog.chosen_dim() == 1024


def test_changing_only_the_width_still_counts_as_a_change(qapp):
    """A custom model at a new width is as destructive as a new model, and the
    button was enabled on the name alone."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=10, current_dim=384)
    dialog.dim.setValue(768)

    ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
    assert ok.isEnabled()


def test_a_width_change_says_the_store_is_rebuilt_from_empty(qapp):
    """Re-embedding replaces the contents of the vector table; changing the
    width replaces the table. Somebody told only the first has not been told
    that semantic search returns nothing until the run finishes."""
    dialog = RebuildVectorsDialog(
        "BAAI/bge-small-en-v1.5", chunk_count=39_306, current_dim=384)
    dialog.model.setCurrentIndex(dialog.model.findData("BAAI/bge-base-en-v1.5"))

    said = dialog.cost.text().lower()
    assert "384" in said and "768" in said
    assert "rebuil" in said or "from empty" in said
    assert "returns nothing" in said or "until" in said


def test_the_cost_states_the_rate_it_assumed(qapp):
    """`CHUNKS_PER_SECOND` is a constant, and a duration derived from an
    unstated assumption cannot be checked against the machine it is shown on."""
    dialog = RebuildVectorsDialog("a/model", chunk_count=39_306, current_dim=384)
    _pick(dialog)

    assert "chunks/sec" in dialog.cost.text()
    assert "embed-bench" in dialog.cost.text()


# --- 2026-10-04, code review: nothing on the interface thread ---------------

def test_typing_a_path_does_no_io_on_the_interface_thread(qapp, tmp_path, monkeypatch):
    """`textChanged` ran `folder_gb` - a walk of the whole index - up to three
    times, and `resolve`, `exists` and `disk_usage`, per keystroke, on the
    thread that paints. Now the index is measured once, on a worker, and the
    folder is checked on a worker once the typing pauses."""
    import threading

    from app.ui.widgets import index_flows

    measured: list = []
    checked: list = []
    real_size, real_check = index_flows.folder_gb, index_flows.check_destination
    monkeypatch.setattr(index_flows, "folder_gb", lambda path: measured.append(
        threading.current_thread() is threading.main_thread()) or real_size(path))
    monkeypatch.setattr(index_flows, "check_destination", lambda text, current: checked.append(
        (text, threading.current_thread() is threading.main_thread()))
        or real_check(text, current))
    monkeypatch.setattr(index_flows, "looks_like_an_index", lambda path: pytest.fail(
        "a stat on the interface thread") if threading.current_thread()
        is threading.main_thread() else False)

    current = make_index(tmp_path / "current")
    dialog = IndexLocationDialog(current)
    for typed in ("D", "D:", "D:/n", "D:/ne", "D:/new"):
        dialog.destination.setText(typed)
    assert dialog.consequence.text() == index_flows.CHECKING
    assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
    settled(dialog)

    assert measured == [False], "measured once, on a worker"
    assert [main for _text, main in checked] == [False] * len(checked)
    assert checked[-1][0] == "D:/new" and len(checked) <= 2, "one check after the pause"


def test_an_answer_about_text_since_replaced_is_dropped(qapp, tmp_path):
    current = make_index(tmp_path / "current")
    dialog = settled(IndexLocationDialog(current))
    stale = dialog._check_generation
    dialog.destination.setText(str(tmp_path / "elsewhere"))
    dialog._checked({"occupied": True, "same": False, "free_gb": 1.0}, stale)
    assert dialog._facts is None, "the old text's answer is not this text's"
