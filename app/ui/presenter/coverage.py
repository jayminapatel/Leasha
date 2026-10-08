r"""What gets read, place by place - one plain sentence each, from the levers.

Layer: L5 presenter. Pure: no Qt, no store, no I/O.

**Owner, 1 October 2026:** *"put all the levers and create a section on
configurable how indexing works in different places based on levers we have"*.
The levers were spread over three pages, and nothing anywhere said "this is
what happens to an email attachment" or "to a zip". The *What gets read* page
groups them by place, and each place says, in a sentence, what the current
settings will do - recomputed as a lever moves, so the effect of a change is
read before a run, not discovered after one.

`what_gets_read` takes anything with the `Settings` field names, as attributes
or as a mapping - the live `Settings`, or a box's `values()` while somebody is
changing it. A missing value is the documented default, so a sentence is never
missing because one field was.

**Every sentence must be true of the code, not only of the label.** Each one
cites what decides it; `test_coverage.py` holds them to the behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["Place", "PLACES", "what_gets_read", "place_sentence"]

FILES = "Files on disk"
EMAIL = "Email"
ATTACHMENTS = "Email attachments"
ZIPS = "Zip archives"
PICTURES = "Pictures and scans"
MEDIA = "Video and audio"
CODE = "Code"

#: The order the page shows them in: where things are, then what they hold.
PLACES = (FILES, EMAIL, ATTACHMENTS, ZIPS, PICTURES, MEDIA, CODE)

#: The documented defaults (`app/core/config.py`), for a field not supplied.
_DEFAULTS: dict[str, Any] = {
    "index_name_only": True, "archive_recheck_days": 30,
    "mail_attachments": "documents", "index_junk_image_filter": True,
    "archive_read_inside": True, "archive_max_mb": 100,
    "index_ocr_mode": "both", "index_ocr_pass": "with-run", "pdf_ocr_pages": 0,
    "video_indexing_enabled": False, "audio_transcription_enabled": False,
    "caption_trickle_enabled": False, "people_recognition_enabled": False,
    "index_watch_folders": False, "pst_backend": "auto", "index_cloud": False,
    "images_due": False,
}

_READ = "Word, Excel, PowerPoint, PDF, text, CSV and HTML are read for their words."


@dataclass(frozen=True)
class Place:
    """One block of the page: where, and what happens there."""

    name: str
    sentence: str


def _value(levers: Any, name: str) -> Any:
    """One lever from a mapping or an object, else its documented default."""
    if isinstance(levers, Mapping):
        found = levers.get(name, levers.get(name.upper()))
    else:
        found = getattr(levers, name, None)
    return _DEFAULTS.get(name) if found is None else found


def _flag(levers: Any, name: str) -> bool:
    """A lever as a bool; the `.env` spellings of true count."""
    value = _value(levers, name)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _files(levers: Any) -> str:
    # `INDEX_NAME_ONLY`: a row for every file, readable or not.
    first = ("Every file is listed and findable by its name, even ones nothing can "
             "open; files Leasha can read are read for their words."
             if _flag(levers, "index_name_only") else
             "Only files Leasha can read are kept; others - videos, programs, "
             "unknown types - are left out of the index entirely.")
    days = int(_value(levers, "archive_recheck_days") or 0)
    # `ARCHIVE_RECHECK_DAYS`: 0 means "only when it changes or you ask".
    second = (f" A folder marked Archive is walked again every {days} days, or when it changes."
              if days else
              " A folder marked Archive is walked again only when it changes or you ask.")
    # `INDEX_WATCH_FOLDERS`: mailboxes and Archive folders wait for a run.
    third = (" New and changed files are added as soon as they are saved, except "
             "mailboxes and Archive folders, which wait for the next run."
             if _flag(levers, "index_watch_folders") else
             " New and changed files are added on the next run.")
    # `ui:index_cloud`: a cloud-only placeholder is downloaded to be read.
    fourth = (" Files kept only in the cloud are downloaded so they can be read."
              if _flag(levers, "index_cloud") else
              " Files kept only in the cloud (OneDrive, SharePoint) are not downloaded.")
    return first + second + third + fourth


def _email(levers: Any) -> str:
    # `email_pst.build_email_document`: headers are written into the text.
    first = ("Each message is read: who sent it, who it went to, the subject and "
             "the text, so a search for a name or a phrase finds it.")
    # `ui:pst_backend` (`settings_controller._apply_pst_backend`).
    backend = str(_value(levers, "pst_backend") or "auto").strip().lower()
    how = {"libpff": " Outlook archives are read straight from the file; Outlook is not needed.",
           "outlook": " Outlook archives are read through Outlook."}.get(
        backend, " Outlook archives are read straight from the file when possible, "
                 "otherwise through Outlook.")
    return first + how


def _attachments(levers: Any) -> str:
    # `app/extract/mail_attachments.py` - `rule()` and its four `MODES`.
    mode = str(_value(levers, "mail_attachments") or "documents").strip().lower()
    junk = (" Signature logos and icons are left out."
            if _flag(levers, "index_junk_image_filter") else "")
    if mode == "names":
        return ("Nothing attached to an email is opened. Every attachment is listed "
                "on the Files tab and findable by its name.")
    if mode == "pictures":
        return (f"{_READ} The text in pictures is read too, which is slow. A zip "
                f"gives its name and the names of the files in it.{junk}")
    if mode == "everything":
        return ("Every attachment Leasha can read is read, pictures included and "
                f"zips unpacked - the slowest choice.{junk}")
    return (f"{_READ} A zip gives its name and the names of the files in it. "
            "Pictures are kept by name and never read.")


def _zips(levers: Any) -> str:
    # `archive.ArchiveExtractor.extract`: `archive_read_inside`, `archive_max_mb`.
    if not _flag(levers, "archive_read_inside"):
        return "Zips are listed by name; nothing inside them is opened."
    size = int(_value(levers, "archive_max_mb") or 0)
    return (f"Zips up to {size:,} MB are opened and the files inside are read; "
            "bigger ones are listed by name. Encrypted or deeply nested files are "
            "listed by name and never opened.")


def _pictures(levers: Any) -> str:
    # `run_setup.pass_for`: the pass decides *when*, the mode decides *what*,
    # and the pass wins. *2026-10-04, code review:* this re-implemented the
    # rule and could not see `images_due` (an "after-run" text pass finished,
    # so the next run is the images pass); it asks `run_setup` now - Qt-free,
    # and importing nothing heavier than logging. `images_due` is optional in
    # `levers`; absent, it is False, as a fresh window's is.
    from types import SimpleNamespace

    from app.index.run_setup import ocr_schedule, ocr_what, pass_for

    asked = SimpleNamespace(index_ocr_pass=_value(levers, "index_ocr_pass"),
                            index_ocr_mode=_value(levers, "index_ocr_mode"))
    when = ocr_schedule(asked)
    what = ocr_what(asked)
    this_run = pass_for(asked, images_due=_flag(levers, "images_due"))
    if when == "after-run" and this_run == "images":
        first = "This run reads only photos and scanned pages."
    elif when == "after-run":
        first = ("Text in photos and scanned pages is read after each run, so "
                 "everything else is searchable first.")
    elif when == "manual":
        first = "Text in photos and scanned pages is read only when you ask."
    elif what == "text":
        first = ("This run reads everything except pictures; the text in photos and "
                 "scanned pages is left for a pictures run.")
    elif what == "images":
        first = "This run reads only photos and scanned pages."
    else:
        first = "Text in photos and scanned pages is read during each run."
    pages = int(_value(levers, "pdf_ocr_pages") or 0)
    # `PDF_OCR_PAGES`: 0 leaves scanned PDFs unread; only the pictures pass uses it.
    second = (f" Scanned PDFs: the first {pages} pages are read." if pages
              else " Scanned PDFs are found by name and any text they hold, not read page by page.")
    # Their switches are drawn in this block (`widgets/what_gets_read.py`).
    third = (" Photos are slowly given a written description."
             if _flag(levers, "caption_trickle_enabled") else "")
    fourth = (" Faces in photos are grouped so they can be named."
              if _flag(levers, "people_recognition_enabled") else "")
    return first + second + third + fourth


def _media(levers: Any) -> str:
    """The video and audio sentence, from the two background switches."""
    video = _flag(levers, "video_indexing_enabled")
    audio = _flag(levers, "audio_transcription_enabled")
    parts = [
        "Videos are read for what is in them, in the background." if video
        else "Videos are listed by name; what is in them is not read.",
        "Speech in recordings is written down, in the background." if audio
        else "Recordings are listed by name; speech is not written down.",
    ]
    return " ".join(parts)


def _code(_levers: Any) -> str:
    # `pipeline` finds a repository by its `.git`; `code_types` is a view choice.
    return ("Code is read like any other file. A folder with a .git in it is "
            "recognised as a repository, so its history can be searched on the "
            "Code tab; which file types the Code tab lists is its own View choice.")


_SENTENCES = {FILES: _files, EMAIL: _email, ATTACHMENTS: _attachments, ZIPS: _zips,
              PICTURES: _pictures, MEDIA: _media, CODE: _code}


def place_sentence(place: str, levers: Any) -> str:
    """The sentence for one place. **Never raises**: a lever that cannot be read
    costs its own sentence a default, never the page."""
    try:
        return _SENTENCES[place](levers)
    except Exception:                                  # noqa: BLE001 - a sentence
        return _SENTENCES[place](_DEFAULTS)


def what_gets_read(levers: Any) -> list[Place]:
    """Every place, in page order, with what the current levers do there."""
    return [Place(place, place_sentence(place, levers)) for place in PLACES]
