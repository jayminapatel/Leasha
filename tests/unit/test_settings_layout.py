r"""Nothing on the Settings page is smaller than the words inside it.

Layer: L5

> *"look at alignment of things on settings page some of the check boxes are
> cut"* - the owner

**The page is 3,100 pixels tall and must never be asked to fit.** That is the
finding this file locks down. Left to itself in a window shorter than its
minimum, Qt does not clip the bottom of a `QVBoxLayout` and add a scrollbar - it
*squeezes*, taking height from whichever children have the weakest minimums
until the layout fits. Measured here: an unwrapped `SettingsView` given 1,400
pixels lays out nine of its controls at **zero height**, including the two
checkboxes at the bottom of the long-run box. Not clipped by a pixel. Not on
screen at all.

`shell.py` avoids that by wrapping the page in `widgets/scroll.scrollable`,
whose `setWidgetResizable(True)` gives the inner widget its *minimum* size hint
and scrolls the difference. So the arrangement under test here is the wrapped
one, because an unwrapped `SettingsView` is not a thing the application ever
builds - and a test that failed against one would be reporting a fault in
itself.

**What it therefore guards is that the wrapping keeps working.** Somebody
removing that tab from the scroll list, or a future page that manages its own
scrolling, would put those nine controls back at zero height, and no existing
test would notice: the size hints are all correct, every widget is constructed,
and nothing raises. It only shows up once a real layout runs on a real page,
which is the same gap `test_window_opens.py` exists to close.

**Not the whole of the report.** The owner's words were about a page they were
looking at on Windows, and clipping that depends on font metrics cannot be
reproduced here - see the note on the `QCheckBox` rule in `theme.py` for what
was and was not established about that. This checks the part that can be checked
anywhere: **is every control big enough to read the words inside it?**
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


ENV = """\
DATA_PATH={d}
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_ENABLED=false

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""

#: A window nobody would call large, deliberately. The page needs about 3,100
#: pixels of height, so at 800 the scroll area is doing all the work - which is
#: precisely the condition being tested. A tall window would pass either way.
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800


@pytest.fixture(scope="module")
def settings_page(tmp_path_factory):
    """The Settings page as `shell.py` actually builds it: inside a scroll area.

    **Wrapped, and that is the point.** A bare `SettingsView` sized to a window
    is not an arrangement the application ever produces, and measuring one gives
    nine zero-height controls that say nothing about the shipped page.

    Module-scoped for the same reason `test_window_opens` shares its window:
    building and tearing down Qt widget trees repeatedly in one process is a
    property of the harness rather than of the application, and it crashes.
    """
    from PySide6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.ui.settings_view import SettingsView
    from app.ui.theme import stylesheet
    from app.ui.widgets.scroll import scrollable

    root = tmp_path_factory.mktemp("settings")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance() or QApplication([])

    store = SqliteStore(settings.fts_db).connect()
    view = SettingsView(settings, store)
    # **The real stylesheet.** Qt's default sizes these widgets differently, so
    # a page checked without it is a page nobody sees. On the view rather than on
    # the application: Qt cascades it to the children, so the metrics are identical,
    # and nothing walks every widget alive in the process - see
    # `test_no_application_stylesheet.py` for the crash that walk caused three times.
    view.setStyleSheet(stylesheet("light"))
    area = scrollable(view)
    area.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    area.show()
    for _ in range(4):
        app.processEvents()

    yield app, view

    store.close()


def test_the_page_is_taller_than_any_window_and_is_therefore_wrapped():
    r"""The premise everything below rests on, asserted rather than assumed.

    If the Settings page ever became short enough to fit a window, the tests
    below would keep passing while proving nothing - and if the scroll wrapper
    were dropped, they would fail for a reason nobody would connect to it. This
    states both halves: the page genuinely does not fit, and `shell.py` genuinely
    wraps it.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "ui" / "shell.py"
    text = source.read_text(encoding="utf-8")
    assert "wrap_if_needed" in text, (
        "shell.py no longer wraps its tabs; a page taller than the window will "
        "be squeezed rather than scrolled, and its smallest controls vanish")



def _readable(widget) -> tuple[int, int]:
    """`(height it has, height its own text needs)`."""
    from PySide6.QtGui import QFontMetrics

    return widget.height(), QFontMetrics(widget.font()).height()


def test_every_checkbox_on_the_settings_page_can_be_read(settings_page):
    r"""**The two that were zero pixels high.**

    Every checkbox with a label, measured after layout. The rule is asserted
    rather than a pixel count: a test pinning heights to 21 would pass against a
    wrong stylesheet that happened to say 21, and would fail the next time the
    type scale moved for a good reason. What must always hold is that a control
    is at least as tall as the text in it.
    """
    from PySide6.QtWidgets import QCheckBox

    _app, view = settings_page
    cut = []
    for box in view.findChildren(QCheckBox):
        if not box.text() or not box.isVisibleTo(view):
            continue
        have, need = _readable(box)
        if have < need:
            cut.append(f"{box.text()[:48]!r}: {have}px for text needing {need}px")

    assert not cut, "checkboxes too short to read:\n  " + "\n  ".join(cut)


def test_every_checkbox_is_wide_enough_for_its_own_label(settings_page):
    """The other half of "cut": a label elided at the right edge.

    Qt truncates rather than overflows, so a checkbox that is too narrow reads
    as a sentence that stops mid-word - which looks like the text is wrong
    rather than the layout. The allowance is the indicator plus its gap; the
    exact figure does not matter, only that the label is not being eaten.
    """
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QCheckBox

    _app, view = settings_page
    #: Indicator plus the gap between it and the text. Generous rather than
    #: exact - this is looking for labels losing words, not for a pixel.
    INDICATOR_ALLOWANCE = 24

    narrow = []
    for box in view.findChildren(QCheckBox):
        if not box.text() or not box.isVisibleTo(view):
            continue
        needed = QFontMetrics(box.font()).horizontalAdvance(box.text())
        if box.width() < needed + INDICATOR_ALLOWANCE:
            narrow.append(f"{box.text()[:48]!r}: {box.width()}px for {needed}px of text")

    assert not narrow, "checkbox labels are being cut off:\n  " + "\n  ".join(narrow)


def test_no_control_is_laid_out_at_zero_height(settings_page):
    """A control with no height is not a small control, it is a missing one.

    Broader than the two tests above and deliberately so: the checkboxes are
    what got reported, but the mechanism - a stylesheet property overriding a
    size hint - applies to anything the sheet styles. Nothing that is meant to
    be on the page should be zero pixels of it.
    """
    from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPushButton, QSpinBox

    _app, view = settings_page
    missing = []
    for kind in (QCheckBox, QComboBox, QLineEdit, QPushButton, QSpinBox):
        for widget in view.findChildren(kind):
            if not widget.isVisibleTo(view):
                continue
            if widget.height() <= 0:
                label = getattr(widget, "text", lambda: "")() or widget.objectName()
                missing.append(f"{kind.__name__} {label!r}")

    assert not missing, "laid out at zero height:\n  " + "\n  ".join(missing)
