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

__all__ = ["interpret_into", "run_interpretation"]


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
        """UI thread: the translation landed. The query goes in the box, then the search."""
        query, note = interpret_message(translation)
        if query is not None:
            set_text(query)          # visible and editable, never hidden
        set_status(note)
        on_done(translation)

    def failed(error: Any) -> None:
        """UI thread: the translator raised. The typed words are searched instead."""
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


def interpret_into(view: Any) -> None:
    r"""Run one interpretation for a search view, and search whatever comes back.

    **The wiring, not the feature** - and here rather than in the view because
    `search_view.py` is held under 250 lines by
    `test_every_qt_view_keeps_its_logic_in_the_presenter`, which fired when the
    kind-word suggestion was added. Every outcome ends in a search; see
    `run_interpretation` for why that is one callback rather than two.

    **Both debounce timers are stopped before dispatching**, and that is the
    part worth keeping written down. Writing the translated query into the box
    fires `textChanged`, which restarts both timers exactly as typing does - so
    interpreting used to run the whole pipeline twice, the second landing 400ms
    later doing identical work. Cancelled here rather than written with signals
    blocked, because the command popup listens to `textChanged` too and needs
    to see it.
    """
    from app.ui.presenter import Tier

    def done(translation: Optional[Any]) -> None:
        """Either outcome: stop both debounce timers, then dispatch the full tier once."""
        for timer in (view._interim_timer, view._full_timer):
            timer.stop()
        if translation is not None:
            view.interpreted.emit(translation)
        view._dispatch(Tier.FULL)

    started = run_interpretation(
        translator=view._translator,
        sentence=view.input.text(),
        button=view.interpret_button,
        pool=view._pool,
        set_text=view.input.setText,
        set_status=view.status.setText,
        on_done=done,
    )
    if not started:
        view._dispatch(Tier.FULL)
