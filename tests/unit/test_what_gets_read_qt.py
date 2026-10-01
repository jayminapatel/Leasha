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
