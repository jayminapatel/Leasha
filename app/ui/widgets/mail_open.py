r"""Open the original of a previewed message: in Outlook, or as a file.

Layer: L5

Order 0y section 4d. The preview shows what the index holds - a message's own
words, without the quoted thread under them. The whole message is one click
away: "Open in Outlook" for a message read out of an Outlook archive, "Open"
for a `.eml` or `.msg` file.

**Only ever on a click.** Nothing in this module runs while a message is being
previewed. Showing a message in Outlook starts Outlook and has it open the
archive, which on a machine whose archives are being indexed is exactly what
must not happen by accident - so the one place that does it
(`osbridge.outlook.show_in_outlook`) is reached from `open_original`, and
`open_original` is reached from a button's `clicked` and nowhere else.

**A seam, so it can be tested without Outlook.** `open_original` takes the
launcher as an argument; the pane passes its `outlook_launcher` attribute, which
is `None` in the application (meaning "the real one", looked up when the click
happens) and a stand-in in every test.

**Off the interface thread.** Starting a program takes seconds;
`open_original_async` runs the body on a worker and routes an error back.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger

__all__ = ["open_original", "open_original_async"]

_log = logger.bind(component="ui.mail.open")


def _open_file(path: str) -> Optional[AppError]:
    """A message file, opened by the program that owns it - the route every
    other "Open" in the window takes (`workers.open_in_explorer`)."""
    from app.ui.workers import open_in_explorer

    return open_in_explorer(path, select=False)


def open_original(target: Any, *, outlook: Optional[Callable[[str, str], None]] = None,
                  open_file: Optional[Callable[[str], Any]] = None) -> Optional[AppError]:
    """Open `target` (a `presenter.mail.OriginalTarget`). **Worker body.**

    Returns an `AppError` rather than raising, as `open_in_explorer` does, so
    the result is what carries a problem. `outlook` and `open_file` are the
    seams; left out, the real ones are used.
    """
    if target is None:
        return None
    if getattr(target, "kind", "") == "file":
        result = (open_file or _open_file)(target.path)
        return result if isinstance(result, AppError) else None

    try:
        if outlook is None:
            from app.core.osbridge.outlook import show_in_outlook as outlook
        outlook(target.entry_id, target.store_path)
    except Exception as exc:                     # noqa: BLE001 - reported, with a way out
        _log.warning("Outlook could not show a message from {}: {}", target.store_path, exc)
        return make_error("ERR_OUTLOOK_OPEN", "ui.mail.open", path=target.store_path,
                          details=f"{type(exc).__name__}: {exc}")
    return None


def open_original_async(target: Any, *, on_error: Any = None, outlook: Any = None) -> None:
    """`open_original` on a worker. **Call from a click handler only.**"""
    from PySide6.QtCore import QThreadPool

    from app.ui.workers import CallableWorker, run

    if target is None:
        return
    worker = CallableWorker(open_original, target, outlook=outlook, component="ui.mail.open")
    if on_error is not None:
        worker.signals.finished.connect(
            lambda error: on_error(error) if error is not None else None)
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)
