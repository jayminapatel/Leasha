r"""Gather the indexing levers that live on the Settings page onto *What gets read*.

Layer: L5 (UI). Owner, 1 October 2026: *"put all the levers and create a
section on configurable how indexing works in different places"*.

**Shown on one page, still owned by the other.** The Settings page goes on
building these controls, loading them and saving them exactly as it always
has - its signals, its `.env` writes and its restart notices are untouched -
and only where they are drawn moves. That keeps every save path and every test
that builds a control in its own box as it was, and it is why this is a
function the window calls once, rather than a second copy of four boxes.

What moves, and to which place:

* **Files on disk** - "Index cloud-only files".
* **Email** - the Outlook archives box: how archives are read, and Convert.
* **Pictures and scans** - "Describe photos in the background" and
  "Recognise people in photos". The photo description model and the Photo
  Tagger button stay with the models they belong to.
* **Video and audio** - the whole video and recordings box.

"Index files as soon as they are saved" stays on Schedule and "When to read
images" on Tuning: both are about *when* a run reads, which is what those two
pages are for. Their effect is still said in the sentences here.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLabel

from app.ui.presenter.coverage import EMAIL, FILES, MEDIA, PICTURES
from app.ui.view_options import weak_slot

__all__ = ["gather_levers"]


def gather_levers(coverage: Any, settings_view: Any, *, pst_backend: str = "auto") -> None:
    """Draw the Settings page's indexing levers on `coverage`'s page, and keep
    its sentences in step with them. Call once, after both pages exist."""
    coverage.place_in(FILES, settings_view.cloud)
    coverage.place_in(EMAIL, settings_view.pst_box)
    coverage.place_in(PICTURES, settings_view.caption_trickle)
    coverage.place_in(PICTURES, settings_view.people_recognition)
    # **A dated note above it, not an edit to it.** The box's own status
    # lines are released text and still say "In Settings, under 'Videos and
    # recordings'"; that is where it was, and the note says where it is now.
    note = QLabel("Note, 1 October 2026: these settings have moved here, to Indexing, "
                  "What gets read. Where the text below says \"In Settings\", look here.")
    note.setObjectName("mediaMovedNote")
    note.setWordWrap(True)
    coverage.place_in(MEDIA, note)
    coverage.place_in(MEDIA, settings_view.media_box)
    coverage.note_levers({
        "index_cloud": settings_view.cloud.isChecked(),
        "pst_backend": settings_view.pst_backend.currentData() or pst_backend,
        "caption_trickle_enabled": settings_view.caption_trickle.isChecked(),
        "people_recognition_enabled": settings_view.people_recognition.isChecked(),
    })
    # **Weak, so the Settings page never keeps this box alive** - nor calls
    # into one Qt has already deleted, which raised "wrapped C/C++ object has
    # been deleted" in a later test's event loop when these were lambdas.
    settings_view.settings_changed.connect(
        weak_slot(coverage, lambda box, values: box.note_levers(values)))
    settings_view.cloud_toggled.connect(
        weak_slot(coverage, lambda box, on: box.note_levers({"index_cloud": on})))
    settings_view.pst_backend_changed.connect(
        weak_slot(coverage, lambda box, backend: box.note_levers({"pst_backend": backend})))
