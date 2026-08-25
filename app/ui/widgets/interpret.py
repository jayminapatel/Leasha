"""Running one query interpretation, and putting the answer where it can be seen.

Layer: L5

Its own module for the reason `search_bar.py` and `model_box.py` are: the search
view had grown past the length a view is allowed to be, and the rule that keeps
views short is the rule that keeps logic out of them. This is a self-contained
feature - a button, a worker, three outcomes - and it reads better whole.

**The translated query goes into the box.** A hard requirement, not a nicety: a
bad translation must be a two-second correction rather than a mystery, and it can
only be corrected if it can be seen. The next search starts from it like any
other text.

**Every outcome ends in a search.** Success searches the translation; a rejected
translation searches the words as typed; an outright failure does too. A button
that sometimes does nothing at all is worse than one that occasionally does the
blunt thing, because you cannot tell the two apart from the outside.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["run_interpretation"]


def run_interpretation(
    *,
    translator: Any,
    sentence: str,
    button: Any,
    pool: Any,
    set_text: Callable[[str], None],
    set_status: Callable[[str], None],
    on_done: Callable[[Optional[Any]], None],
) -> bool:
    """Start one interpretation on a worker. False if there was nothing to do.

    `on_done` is called with the `Translation` on success and `None` when it
    could not run - in both cases the caller searches, which is why there is one
    callback rather than two.

    Off the UI thread, because it costs about a second and a second of frozen
    window is how this application has repeatedly looked broken.
    """
    from app.ui.presenter import interpret_message
    from app.ui.workers import CallableWorker, run

    text = (sentence or "").strip()
    if not text or translator is None:
        return False

    # Disabling the button that started the work is not the forbidden kind of
    # disabling: it stops a second run of the same thing, and every other
    # control in the window stays live throughout.
    button.setEnabled(False)
    set_status("Interpreting…")

    def finished(translation: Any) -> None:
        query, note = interpret_message(translation)
        if query is not None:
            set_text(query)          # visible and editable, never hidden
        set_status(note)
        on_done(translation)

    def failed(error: Any) -> None:
        # `translate` is not supposed to raise - it returns a usable query on
        # every path - but a button that does nothing is worse than one that
        # does the plain thing.
        set_status(str(getattr(error, "message", "Could not interpret that.")))
        on_done(None)

    worker = CallableWorker(translator.translate, text, component="ui.translate")
    worker.signals.finished.connect(finished)
    worker.signals.failed.connect(failed)
    worker.signals.done.connect(lambda: button.setEnabled(True))
    run(pool, worker)
    return True
