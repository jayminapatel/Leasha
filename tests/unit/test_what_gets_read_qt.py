"""The *What gets read* page: levers by place, each with what it does there.

Layer: L5 (Qt, offscreen). Owner, 1 October 2026: *"put all the levers and
create a section on configurable how indexing works in different places"*.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication, QGroupBox, QWidget

from app.ui.presenter.coverage import ATTACHMENTS, PLACES, ZIPS


def _box(settings=None):
    from app.ui.widgets.long_run_box import LongRunBox

    QApplication.instance()
    box = LongRunBox()
    if settings is not None:
        box.load(settings)
    return box


def test_every_place_has_a_block_with_its_sentence(qapp):
    box = _box()
    titles = [b.title() for b in box.findChildren(QGroupBox, "place")]
    assert titles == list(PLACES)
    assert all(box.sentences[place].text() for place in PLACES)


def test_moving_the_attachment_lever_rewrites_its_sentence_and_wakes_the_logo_switch(qapp):
    box = _box()
    seen = []
    box.changed.connect(lambda: seen.append(box.values()["mail_attachments"]))
    assert "never read" in box.sentences[ATTACHMENTS].text()
    assert not box.junk_images.isEnabled()

    box.mail_attachments.setCurrentIndex(box.mail_attachments.findData("pictures"))
    assert seen == ["pictures"]
    assert "text in pictures is read too" in box.sentences[ATTACHMENTS].text()
    assert box.junk_images.isEnabled() and box.junk_images_note.isHidden()


def test_a_lever_change_reaches_its_own_sentence_at_once(qapp):
    box = _box()
    box.archive_read_inside.setChecked(False)
    assert box.sentences[ZIPS].text().startswith("Zips are listed by name")


def test_load_fills_the_lever_and_says_nothing_until_somebody_moves_one(qapp, temp_env):
    from app.core.config import load_settings

    temp_env.write_text(temp_env.read_text(encoding="utf-8") + "\nMAIL_ATTACHMENTS=names\n",
                        encoding="utf-8")
    box = _box()
    fired = []
    box.changed.connect(lambda: fired.append(1))
    box.load(load_settings(temp_env))
    assert fired == []
    assert box.values()["mail_attachments"] == "names"
    assert box.sentences[ATTACHMENTS].text().startswith("Nothing attached to an email")


def test_the_tuning_page_relays_the_new_lever_for_saving(qapp):
    from app.ui.widgets.tuning_box import TuningBox

    tuning = TuningBox()
    sent = []
    tuning.coverage_changed.connect(sent.append)
    tuning.coverage.mail_attachments.setCurrentIndex(
        tuning.coverage.mail_attachments.findData("everything"))
    assert sent and sent[-1]["mail_attachments"] == "everything"


def test_the_page_renders(qapp, tmp_path):
    """Tier 2: a real paint, offscreen. A layout that raises only when drawn
    is how a startup crash once passed review."""
    box = _box()
    box.resize(720, 1400)
    image = box.grab()
    assert not image.isNull()
    image.save(str(tmp_path / "what_gets_read.png"))
    assert isinstance(box, QWidget)


# -- stage two: the levers the Settings page owns, drawn here by place ----------

from tests.unit.test_pages_reorg import _settings_view, settings_and_store  # noqa: E402,F401


def _gathered(settings, store):
    from app.ui.widgets.long_run_box import LongRunBox
    from app.ui.widgets.what_gets_read import gather_levers

    box, view = LongRunBox(), _settings_view(settings, store)
    box.load(settings)
    gather_levers(box, view)
    return box, view


def test_the_settings_levers_are_drawn_in_their_places(settings_and_store):
    from app.ui.presenter.coverage import EMAIL, FILES, MEDIA, PICTURES

    box, view = _gathered(*settings_and_store)
    blocks = {b.title(): b for b in box.findChildren(QGroupBox, "place")}
    assert blocks[FILES].isAncestorOf(view.cloud)
    assert blocks[EMAIL].isAncestorOf(view.pst_box)
    assert blocks[PICTURES].isAncestorOf(view.caption_trickle)
    assert blocks[PICTURES].isAncestorOf(view.people_recognition)
    assert blocks[MEDIA].isAncestorOf(view.media_box)
    assert box._media_note.isHidden()          # no longer "set elsewhere"


def test_a_settings_lever_still_saves_the_way_it_always_did(settings_and_store):
    box, view = _gathered(*settings_and_store)
    saved = []
    view.settings_changed.connect(saved.append)
    view.caption_trickle.setChecked(not view.caption_trickle.isChecked())
    assert saved and "CAPTION_TRICKLE_ENABLED" in saved[-1]


def test_moving_a_settings_lever_rewrites_its_place_sentence(settings_and_store):
    from app.ui.presenter.coverage import EMAIL, FILES, PICTURES

    box, view = _gathered(*settings_and_store)
    view.cloud.setChecked(True)
    assert "downloaded so they can be read" in box.sentences[FILES].text()
    view.pst_backend.setCurrentIndex(view.pst_backend.findData("outlook"))
    assert "read through Outlook" in box.sentences[EMAIL].text()
    view.people_recognition.setChecked(True)
    assert "Faces in photos" in box.sentences[PICTURES].text()
