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
"""

from __future__ import annotations

from typing import Any

from app.ui.presenter.activity import READING_WORDS
from app.ui.presenter.indexing import phase_words

__all__ = ["now_sentence", "show_now"]


def now_sentence(stats: Any, *, stopping: bool = False) -> str:
    """The one sentence for what the run is doing now, or "" for none.

    **Today's fallback, until §3's presenter function is wired in (see the
    module docstring's ADAPTER POINT).** It returns "" whenever another line on
    the page already says what is happening, so the page never says one thing
    twice:

    * stopping or paused - the counts line itself changes to say so;
    * a phase with its own words (loading the model, tidying the index...) -
      the detail line under the bar carries those words already.

    That leaves the ordinary reading of files, which today has no sentence of
    its own anywhere on the page, only counts.
    """
    if stats is None or stopping or getattr(stats, "paused", False) \
            or getattr(stats, "paused_by_person", False):
        return ""
    if phase_words(stats):
        return ""
    return READING_WORDS


def show_now(view: Any, stats: Any, *, stopping: bool = False) -> None:
    """Paint (or clear, with `stats=None`) the page's "now" line.

    Hidden when it has nothing to say, so an empty line never holds space
    between the counts and the bar.
    """
    text = now_sentence(stats, stopping=stopping)
    view.now_line.setText(text)
    view.now_line.setVisible(bool(text))
