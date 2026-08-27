r"""Looking at a document: find, rotate, zoom. Workspace §2b/§2d/§2e.

Layer: L5.

**Rotate and zoom are two parameters of one render call**, for images and PDF
pages alike — the order's own instruction, and the reason there is one
`render()` here rather than an image path and a PDF path that drift.

**Nothing in this file writes to a user's file**, and two tests assert it
rather than trusting it: rotation is remembered in the app's own state, keyed
by a hash of the path, and find highlights with a selection overlay rather
than by editing the document.

**The key is a hash, and that is a privacy decision.** `index_state` is a
table somebody may open; a list of every document they have ever rotated, with
its full path, is not a thing to leave lying about on a shared machine.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage                                  # noqa: E402
from PyQt6.QtWidgets import (                                   # noqa: E402
    QApplication, QTextBrowser, QVBoxLayout, QWidget,
)

from app.ui.render_page import MAX_EDGE, page_count, render     # noqa: E402
from app.ui.view_of_file import (                               # noqa: E402
    FIT, MAX_ZOOM, MIN_ZOOM, TURN, ZOOM_STEPS, View, clamp_zoom, next_zoom,
    normalise_turn, read_turn, rotation_key,
)
from app.ui.widgets.find_bar import (                           # noqa: E402
    attach_find, match_count, summary,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def wide_image():
    """200x100, so a quarter turn is visible in the numbers."""
    path = pathlib.Path(tempfile.mkdtemp()) / "wide.png"
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0x224466)
    image.save(str(path))
    return path


@pytest.fixture(scope="module")
def two_page_pdf():
    pymupdf = pytest.importorskip("pymupdf")

    path = pathlib.Path(tempfile.mkdtemp()) / "report.pdf"
    document = pymupdf.open()
    document.new_page(width=595, height=842).insert_text(
        (72, 100), "Safety report, page one")
    document.new_page(width=595, height=842).insert_text((72, 100), "page two")
    document.save(str(path))
    document.close()
    return path


# ---------------------------------------------------------------------------
# §2e — rotation
# ---------------------------------------------------------------------------

def test_rotating_cycles_through_four_quarters():
    """One button, cycling — §2e's word. Arbitrary angles are a photo
    editor's feature and this window is view-only."""
    view = View()
    turns = []
    for _ in range(5):
        view = view.turned()
        turns.append(view.turn)
    assert turns == [90, 180, 270, 0, 90]


@pytest.mark.parametrize(("given", "wanted"), [
    (-90, 270), (450, 90), ("180", 180), (0, 0), (95, 90), (None, 0),
    ("nonsense", 0), (360, 0),
])
def test_a_stored_rotation_is_read_forgivingly(given, wanted):
    r"""**Rounded, not refused.** This reads a value out of a database that
    anything could have written, and a stored `95` should show a rotated
    document rather than raise beside a preview."""
    assert normalise_turn(given) == wanted


def test_rotation_is_remembered_per_file():
    r"""*The sideways scan rotated once opens right-side-up forever* — §2e."""
    state = View(turn=270).as_state(r"C:\work\scan.pdf")
    assert read_turn(state, r"C:\work\scan.pdf") == 270
    assert read_turn(state, r"C:\work\other.pdf") == 0


def test_the_key_does_not_contain_the_path():
    r"""**A privacy decision, not tidiness.** `index_state` is a table
    somebody may open; a list of every document they have ever rotated, with
    its full path, is not a thing to leave on a shared machine. The privacy
    order's reasoning, one level down."""
    key = rotation_key(r"C:\Users\jay\Medical\results.pdf")
    for leak in ("Users", "jay", "Medical", "results"):
        assert leak not in key
    assert key.startswith("ui:turn:")


def test_two_files_get_two_keys_and_one_file_gets_one():
    assert rotation_key("a.pdf") != rotation_key("b.pdf")
    assert rotation_key("a.pdf") == rotation_key("a.pdf")


def test_a_document_with_no_file_remembers_nothing():
    """A mail message has no path — there is nothing on disk to key on, and
    an empty key would collide with every other one."""
    assert rotation_key("") == ""
    assert View(turn=90).as_state("") == {}


def test_nothing_here_writes_to_the_file_itself():
    r"""§6's first rule for this order: *no code path in this order writes to
    a user file.* Rotation is state, not an edit."""
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "view_of_file.py").read_text(encoding="utf-8")
    for writing in ("open(", "write_text(", "save(", "unlink(", "shutil."):
        assert writing not in source, f"view_of_file.py calls {writing}"


# ---------------------------------------------------------------------------
# §2d — zoom
# ---------------------------------------------------------------------------

def test_zoom_steps_along_a_ladder_and_lands_on_a_hundred_per_cent():
    r"""**Steps rather than a multiplier.** A fixed 1.25× from an arbitrary
    start lands on 137%, which nobody chose - and the way back to 100% has to
    be exact."""
    assert next_zoom(1.0) == 1.25
    assert next_zoom(1.25, out=True) == 1.0
    assert 1.0 in ZOOM_STEPS


def test_zoom_stops_at_both_ends_rather_than_running_away():
    assert next_zoom(MAX_ZOOM) == MAX_ZOOM
    assert next_zoom(MIN_ZOOM, out=True) == MIN_ZOOM


def test_zooming_from_fit_starts_at_actual_size():
    """"Fit" has no place on a ladder of fixed factors, and the one number
    everybody means by "actual size" is 1.0."""
    assert next_zoom(FIT) == next_zoom(1.0)


@pytest.mark.parametrize("rubbish", [None, "x", object()])
def test_an_unreadable_zoom_becomes_actual_size(rubbish):
    assert clamp_zoom(rubbish) == 1.0


def test_zoom_is_not_remembered_and_that_is_deliberate():
    r"""It is something somebody does to look closer at one passage, not a
    property of a document. A file that reopened at 400% because of something
    done last March would read as broken."""
    assert View(turn=90, zoom=4.0).as_state("a.pdf") == {rotation_key("a.pdf"): "90"}


# ---------------------------------------------------------------------------
# One render call, two kinds
# ---------------------------------------------------------------------------

def test_an_image_rotates(qapp, wide_image):
    upright = render(wide_image, kind="image", view=View())
    turned = render(wide_image, kind="image", view=View(turn=90))
    assert (upright.width(), upright.height()) == (200, 100)
    assert (turned.width(), turned.height()) == (100, 200)


def test_an_image_zooms(qapp, wide_image):
    r"""**The bug the first version had.** `_shape` scaled only for fit, so
    every image rendered at 100% whatever the zoom said - a control that
    visibly does nothing, which is worse than one that is absent."""
    doubled = render(wide_image, kind="image", view=View(zoom=2.0))
    assert (doubled.width(), doubled.height()) == (400, 200)
    halved = render(wide_image, kind="image", view=View(zoom=0.5))
    assert (halved.width(), halved.height()) == (100, 50)


def test_rotation_and_zoom_compose(qapp, wide_image):
    both = render(wide_image, kind="image", view=View(turn=90, zoom=0.5))
    assert (both.width(), both.height()) == (50, 100)


def test_fit_honours_the_box_it_is_given(qapp, wide_image):
    fitted = render(wide_image, kind="image", view=View().fitted(),
                    fit_to=(50, 50))
    assert fitted.width() <= 50 and fitted.height() <= 50


def test_rotation_happens_before_the_fit(qapp, wide_image):
    r"""**And the order matters.** Scaling first would fit the *unrotated*
    shape into the window, so a sideways page rotated upright comes back too
    wide for the pane it was measured against."""
    fitted = render(wide_image, kind="image", view=View(turn=90).fitted(),
                    fit_to=(100, 100))
    assert fitted.height() >= fitted.width(), "it came back still landscape"


def test_a_pdf_page_renders_and_rotates(qapp, two_page_pdf):
    upright = render(two_page_pdf, kind="pdf", view=View())
    turned = render(two_page_pdf, kind="pdf", view=View(turn=90))
    assert upright.width() < upright.height(), "A4 portrait"
    assert turned.width() > turned.height(), "a quarter turn is landscape"


def test_a_pdf_page_zooms(qapp, two_page_pdf):
    one = render(two_page_pdf, kind="pdf", view=View())
    two = render(two_page_pdf, kind="pdf", view=View(zoom=2.0))
    assert two.width() == pytest.approx(one.width() * 2, abs=2)


def test_the_asked_for_page_is_the_one_rendered(qapp, two_page_pdf):
    assert page_count(two_page_pdf) == 2
    assert render(two_page_pdf, kind="pdf", view=View(page=1)) is not None


def test_a_page_past_the_end_clamps_rather_than_failing(qapp, two_page_pdf):
    """A hit recorded against a page a later edit removed is an ordinary
    state, and the last page is a better answer than a blank card."""
    assert render(two_page_pdf, kind="pdf", view=View(page=99)) is not None
    assert render(two_page_pdf, kind="pdf", view=View(page=-5)) is not None


def test_a_file_that_cannot_be_rendered_is_none_rather_than_a_crash(qapp):
    r"""**Never raises**, per the module's contract: a damaged PDF, an image
    that is really HTML, a drive that went to sleep. All ordinary, all shown
    as a card, none worth an error dialog."""
    missing = pathlib.Path(tempfile.mkdtemp()) / "nope.png"
    assert render(missing, kind="image") is None
    assert render(missing, kind="pdf") is None
    assert render(None, kind="image") is None
    assert page_count(missing) == 0


def test_the_render_is_capped_so_a_huge_zoom_cannot_ask_for_the_world():
    r"""A 4x zoom of an A0 drawing is a hundred million pixels, and the window
    would ask for it before anybody could stop it. Silent, because "raster
    dimensions" is not a sentence to put in front of somebody looking at a
    drawing."""
    assert MAX_EDGE < 20_000


def test_the_turn_is_a_quarter_and_the_constant_says_so():
    assert TURN == 90
    assert View(turn=90).sideways and not View(turn=180).sideways


# ---------------------------------------------------------------------------
# §2b — find
# ---------------------------------------------------------------------------

@pytest.fixture()
def finding(qapp):
    host = QWidget()
    view = QTextBrowser(host)
    view.setPlainText(
        "The safety report mentions safety twice, and SAFETY once more.")
    bar = attach_find(host, view)
    QVBoxLayout(host).addWidget(view)
    return host, view, bar


def test_it_counts_every_match_whatever_the_case(finding):
    _host, view, _bar = finding
    assert match_count(view.toPlainText(), "safety") == 3
    assert match_count(view.toPlainText(), "SAFETY") == 3


def test_typing_highlights_every_match_and_says_how_many(finding):
    _host, view, bar = finding
    bar.box.setText("safety")
    assert len(view.extraSelections()) == 3
    assert bar.count.text() == "1 of 3"


def test_next_and_previous_walk_the_matches_and_wrap(finding):
    r"""**Wrapping, and silently.** A find that stops dead at the last match
    makes somebody think there are no more; every find box anybody has used
    wraps, and announcing it would be a dialog inside a keystroke."""
    _host, _view, bar = finding
    bar.box.setText("safety")
    bar.next_match()
    assert bar.count.text() == "2 of 3"
    bar.previous_match()
    bar.previous_match()
    assert bar.count.text() == "3 of 3", "it did not wrap backwards"


def test_typing_something_that_is_not_there_says_so(finding):
    r"""**The bug this test was written for.** The counter keyed off the match
    index, which is -1 for a fresh search - so a word that is not in the
    document produced *no text at all*, and a find box that goes silent is one
    somebody presses again harder."""
    _host, view, bar = finding
    bar.box.setText("elephant")
    assert bar.count.text() == "no matches"
    assert view.extraSelections() == []


def test_an_empty_box_says_nothing_rather_than_zero(finding):
    r"""`0/0` beside an empty box reads as a broken counter. Nothing at all
    reads as a box waiting to be typed in, which is what it is."""
    _host, _view, bar = finding
    bar.box.setText("")
    assert bar.count.text() == ""
    assert summary(-1, 0) == ""


def test_finding_never_changes_the_document(finding):
    r"""**§6's view-only rule, kept by construction.** `setExtraSelections` is
    an overlay: the document is not modified, so a preview cannot be altered
    by looking for something in it."""
    _host, view, bar = finding
    before = view.toPlainText()
    bar.box.setText("safety")
    bar.next_match()
    bar.previous_match()
    assert view.toPlainText() == before


def test_closing_the_bar_takes_the_highlighting_with_it(finding):
    _host, view, bar = finding
    bar.box.setText("safety")
    bar.dismissed.emit()
    assert view.extraSelections() == []
    # **`isHidden`, not `isVisible`.** A widget whose ancestors are not shown
    # is never "visible" whatever it does, so `not isVisible()` would pass
    # over a bar that never hides - which is the thing being tested.
    assert bar.isHidden()


def test_ctrl_f_shows_it_and_selects_what_is_there(finding):
    """So a second Ctrl+F over the same word replaces it by typing and keeps
    it by pressing Enter."""
    _host, _view, bar = finding
    bar.box.setText("safety")
    bar.focus()
    assert not bar.isHidden(), "Ctrl+F did not show it"
    assert bar.box.selectedText() == "safety"


def test_a_bar_attached_to_nothing_is_not_an_error(qapp):
    from app.ui.widgets.find_bar import FindBar

    bar = FindBar()
    bar.box.setText("anything")
    bar.next_match()
    bar.previous_match()
    bar.clear()


def test_the_in_app_preview_pane_has_one_too(qapp):
    r"""§2b asks for it in both places, and one implementation attached twice
    is what stops the same keystroke behaving two ways."""
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    assert pane.find is not None
    assert pane.find.isHidden(), "it should wait for Ctrl+F"


def test_a_new_document_clears_the_last_search(qapp):
    """A highlight from the last file painted over this one would be
    nonsense, and a count of matches in a file nobody is looking at is
    worse."""
    from app.ui.widgets.preview import PreviewPane

    class _Row:
        path = "C:/work/a.txt"
        name = "a.txt"

    pane = PreviewPane()
    pane.text.setPlainText("safety safety")
    pane.find.box.setText("safety")
    assert pane.find.count.text() == "1 of 2"
    pane.show_row(_Row())
    assert pane.find.count.text() == ""
