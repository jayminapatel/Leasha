"""What gets read, place by place - the sentences the page shows.

Layer: L5 presenter (no Qt). `app/ui/presenter/coverage.py`.

Owner, 1 October 2026: the levers are grouped by place, and each place says
what the current settings do there. A sentence that is wrong is worse than
none, because somebody changes a lever on the strength of it - so each one is
checked against the lever values that decide it.
"""

from __future__ import annotations

import pytest

from app.ui.presenter.coverage import (
    ATTACHMENTS, FILES, MEDIA, PICTURES, PLACES, ZIPS, place_sentence, what_gets_read,
)


def test_every_place_has_a_sentence_with_the_defaults():
    places = what_gets_read({})
    assert [p.name for p in places] == list(PLACES)
    assert all(p.sentence.endswith(".") and len(p.sentence) > 30 for p in places)


@pytest.mark.parametrize("mode, has, lacks", [
    ("names", "Nothing attached to an email is opened", "PDF"),
    ("documents", "Pictures are kept by name and never read", "slow"),
    ("pictures", "text in pictures is read too", "never read"),
    ("everything", "zips unpacked", "never read"),
])
def test_the_attachments_sentence_follows_the_lever(mode, has, lacks):
    said = place_sentence(ATTACHMENTS, {"mail_attachments": mode})
    assert has in said and lacks not in said


def test_the_logo_filter_is_mentioned_only_when_pictures_are_read():
    on = {"index_junk_image_filter": True}
    assert "logos" not in place_sentence(ATTACHMENTS, {**on, "mail_attachments": "documents"})
    assert "logos" in place_sentence(ATTACHMENTS, {**on, "mail_attachments": "pictures"})


def test_zips_say_their_size_limit_or_that_nothing_is_opened():
    assert "up to 250 MB" in place_sentence(ZIPS, {"archive_max_mb": 250})
    assert place_sentence(ZIPS, {"archive_read_inside": False}).startswith(
        "Zips are listed by name; nothing inside them is opened")


@pytest.mark.parametrize("when, what, has", [
    ("with-run", "both", "read during each run"),
    ("with-run", "text", "left for a pictures run"),
    ("with-run", "images", "reads only photos"),
    ("after-run", "both", "read after each run"),         # the pass wins over the mode
    ("manual", "images", "only when you ask"),
])
def test_pictures_follow_the_pass_then_the_mode(when, what, has):
    assert has in place_sentence(PICTURES, {"index_ocr_pass": when, "index_ocr_mode": what})


def test_scanned_pdf_pages():
    assert "first 20 pages are read" in place_sentence(PICTURES, {"pdf_ocr_pages": 20})
    assert "not read page by page" in place_sentence(PICTURES, {"pdf_ocr_pages": 0})


def test_files_follow_name_only_recheck_and_watching():
    off = place_sentence(FILES, {"index_name_only": False, "archive_recheck_days": 0,
                                 "index_watch_folders": True})
    assert "Only files Leasha can read are kept" in off
    assert "only when it changes or you ask" in off
    assert "as soon as they are saved, except mailboxes" in off
    assert "every 30 days" in place_sentence(FILES, {})


def test_media_follow_their_switches():
    assert "Videos are listed by name" in place_sentence(MEDIA, {})
    on = place_sentence(MEDIA, {"video_indexing_enabled": True,
                                "audio_transcription_enabled": "true",
                                "caption_trickle_enabled": True})
    assert "Videos are read" in on and "Speech in recordings is written down" in on
    assert "written description" in on


def test_a_settings_object_works_as_well_as_a_mapping(temp_env):
    from app.core.config import load_settings

    settings = load_settings(temp_env)
    assert [p.sentence for p in what_gets_read(settings)] == \
        [p.sentence for p in what_gets_read({})]


def test_an_unreadable_value_costs_only_a_default():
    assert place_sentence(ZIPS, {"archive_max_mb": "lots"}) == place_sentence(ZIPS, {})
