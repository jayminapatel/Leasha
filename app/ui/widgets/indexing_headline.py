r"""The Indexing page's "what is happening now" sentence, and where its words
come from.

Layer: L5

Work order 0x §4b: **one plain sentence for what the run is doing right now**,
under the counts line. The counts ("1,204 documents · 3,310 files seen · 12
skipped") say how far it has got; this says what it is busy with, which is the
question somebody glancing at the page is actually asking.

**The words are not written here.** §3 of the same order gives the pipeline a
fixed list of stages (finding files, opening an archive, reading messages,
OCR, embedding batch n of m, ...) and writes the plain words for each in the
Qt-free presenter package, where they can be tested without a window. This
file is only the slot on the page that shows them. Until §3's function
arrives, `now_sentence` uses words the page already has - the 0w phase wording
in `presenter.activity` - and nothing new is invented, so nothing needs
rewording later.

**ADAPTER POINT - §3 plugs in here, in `now_sentence`.** When the presenter
gains its stage-headline function, replace the fallback body with one call to
it (the signature it is expected to take is a progress snapshot and whether a
stop is under way). The page, its label, where the label sits and when it is
cleared all stay as they are; the tests in `test_indexing_page_4.py` check the
slot, not the wording.

2026-09-27: wired. `now_sentence` is now one call to
`presenter.live_progress.now_headline`, which keeps the fallback's rules (say
nothing another line already says) and its 0w words for when nothing more
precise is known. `show_now(view, None)` - which the view calls at start, at
Stop and on failure - also clears the per-reader panel
(`widgets/indexing_workers.py`), so a run that ended never leaves its readers
or a ticking heartbeat on the page.
"""

from __future__ import annotations

from typing import Any

from app.ui.presenter.live_progress import now_headline

__all__ = ["now_sentence", "show_now"]


def now_sentence(stats: Any, *, stopping: bool = False) -> str:
    """The one sentence for what the run is doing now, or "" for none.

    The words and the rules for when there are none are the presenter's: see
    `presenter.live_progress.now_headline`.
    """
    return now_headline(stats, stopping=stopping)


def show_now(view: Any, stats: Any, *, stopping: bool = False) -> None:
    """Paint (or clear, with `stats=None`) the page's "now" line.

    Hidden when it has nothing to say, so an empty line never holds space
    between the counts and the bar.
    """
    text = now_sentence(stats, stopping=stopping)
    view.now_line.setText(text)
    view.now_line.setVisible(bool(text))
    if stats is None:
        # The view's own "nothing is running any more" call sites (start,
        # Stop, failure) reach the per-reader panel through here, because the
        # view is at its line guard. `getattr`: a page built without the panel.
        panel = getattr(view, "workers_panel", None)
        if panel is not None:
            panel.clear()
