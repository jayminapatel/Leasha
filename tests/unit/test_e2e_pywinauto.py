r"""Order 0m section 3 - black-box journeys against the launched app. UIA via
pywinauto, a real window on a real desktop, no more than these.

**Marked `e2e`, excluded from the default run** (`pyproject.toml`'s `addopts`
excludes it alongside `jvm`). Run deliberately, before a release and after any
change to startup, the window or its shutdown:

    venv\Scripts\python.exe -m pytest tests/unit/test_e2e_pywinauto.py -m e2e -v

**It takes the mouse and keyboard for as long as it runs** (a few minutes; the
window's own startup is 12-40 s, and there are two launches). Do not use the
machine meanwhile. It refuses to run while another Leasha window is open - the
window holds a machine-wide single-instance mutex, so a second launch would hand
over to the first and the journeys would be driving the owner's session.

**Never the real index.** See `e2e_support.py`: the app is launched from a
scratch copy of the code with its own `.env`, logs, window state and three
seeded documents; nothing it does can reach `D:\Leasha\Data`.

**Accessible names, not coordinates.** Every element is found by the object name
the code gave it (Qt reports it to UIA as the tail of the automation id) or by
its accessible name - the discipline `test_accessible_names.py` enforces, which
is what makes this possible at all - so a window resize or a theme change does
not break a journey.

The journeys (none opens the real file, so nothing launches Notepad):

  1. launch            the window appears; the pages are on the rail
  2. search            type a word -> a result row named for the file; Esc empties it
  3. preview           select the row, open the preview pane -> the text is shown
  4. pop-out           Pin in a window -> Keep on top really sets the OS flag
  5. settings          the Settings page opens
  6. close             closing after all of the above ends every process; seconds recorded
  7. close mid-search  close with a search in flight; the process must still end

**Flake discipline (3b), enforced not just written**: a journey that fails is
retried once after a screenshot to `outputs/e2e-failures/`; passing on the retry
is a *flake* and is written to `logs/e2e.log`; a second flake of the same
journey in the same calendar month fails the run with "fix it or delete it" - a
flaky e2e suite is worse than none. Timings (window appears, close-to-exit) are
written to the same log.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

pytest.importorskip("pywinauto")
pytest.importorskip("psutil")

from tests.unit import e2e_support as e2e                          # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.windows]

#: The longest a normal close may take before the journey calls it a hang. The
#: measured figure is recorded either way; order 0u section 6d's incident was a
#: process still alive *nine hours* later, so this is a hang detector, not a target.
CLOSE_LIMIT_S = 30.0


# ---------------------------------------------------------------------------
# 3b - retry once, screenshot, count the flake, and refuse a habitual one.
# ---------------------------------------------------------------------------

def run_journey(journey, *args) -> None:
    name = journey.__name__
    try:
        journey(*args)
        return
    except pytest.skip.Exception:
        raise
    except Exception as first:                                     # noqa: BLE001
        e2e.screenshot(e2e.FAILURE_SHOTS / f"{name}-{datetime.now():%Y%m%d-%H%M%S}-attempt1.png")
        earlier = e2e.flakes_this_month(name)
        try:
            journey(*args)
        except Exception:
            e2e.screenshot(e2e.FAILURE_SHOTS / f"{name}-{datetime.now():%Y%m%d-%H%M%S}-attempt2.png")
            raise
        import traceback

        e2e.note({"event": "flake", "journey": name, "error": f"{type(first).__name__}: {first}"[:200],
                  "where": "".join(traceback.format_tb(first.__traceback__)[-2:])[-400:]})
        if earlier >= 1:
            pytest.fail(
                f"{name} has flaked {earlier + 1} times in {datetime.now():%Y-%m}: fix it or delete it "
                f"(order 0m 3b). This time: {type(first).__name__}: {first}")


# ---------------------------------------------------------------------------
# Fixtures: one scratch install per module; one running app shared by journeys
# 1-6 (the last of them closes it); a fresh launch for journey 7. Only one app
# can run at a time - the window holds a machine-wide mutex - so the order matters.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scratch_install(tmp_path_factory):
    if not (e2e.interpreter_dir() / "pythonw.exe").exists():
        pytest.skip("no pythonw.exe beside the interpreter - install the venv first (run-install.cmd)")
    if e2e.other_leasha_window_is_open():
        pytest.skip("a Leasha window is already open on this desktop - close it first (single-instance mutex)")
    return e2e.build_scratch_install(tmp_path_factory.mktemp("e2e"))


@pytest.fixture(scope="module")
def running(scratch_install):
    app = e2e.LaunchedApp(scratch_install)
    try:
        window = app.find_window()
        e2e.note({"event": "timing", "what": "window appears", "seconds": round(app.window_seconds, 1)})
        yield app, window
    finally:
        app.kill()


def _fresh(scratch_install):
    """A launch of its own, for a journey that ends by closing the window."""
    app = e2e.LaunchedApp(scratch_install)
    try:
        yield app, app.find_window()
    finally:
        app.kill()


@pytest.fixture()
def fresh(scratch_install):
    yield from _fresh(scratch_install)


def _home(app, window) -> None:
    """Back to a known state: the Search page, an empty box, no pop-out open."""
    from pywinauto import Desktop

    for other in Desktop(backend="uia").windows(class_name="PreviewWindow"):
        if other.process_id() in app.pids():
            other.close()
    app.click(window, e2e.by_name(window, "Search", "CheckBox"))
    app.type(window, e2e.by_id(window, "searchBox", "Edit"), "^a{BACKSPACE}")
    time.sleep(0.6)


def _type_query(app, window, text: str):
    box = e2e.by_id(window, "searchBox", "Edit")
    app.type(window, box, text, with_spaces=True)
    return box


def _result_row(window, timeout: float = 30.0):
    return e2e.by_name(window, "barnsley-survey.txt", "ListItem", timeout=timeout, prefix=True)


def _preview_open(app, window) -> None:
    toggle = e2e.by_name(window, "Preview pane", "CheckBox")
    if toggle.get_toggle_state() != 1:
        app.click(window, toggle)


# ---------------------------------------------------------------------------
# The journeys
# ---------------------------------------------------------------------------

def test_1_launch_and_the_window_appears(running) -> None:
    run_journey(_launch_journey, running)


def _launch_journey(running) -> None:
    _app, window = running
    assert window.is_visible()
    assert window.window_text() == "Leasha"
    rail = set(e2e.names(window, "CheckBox"))
    # 2026-10-05: Photos joins the rail after Files (the owner's Photos tab).
    for page in ("Search", "Files", "Photos", "Mail", "Code", "Chat", "Offline", "Reports",
                 "Settings"):
        assert page in rail, f"{page!r} is missing from the rail: {sorted(rail)}"
    assert e2e.by_id(window, "searchBox", "Edit").is_visible()


def test_2_a_search_shows_a_result_row_and_escape_empties_it(running) -> None:
    run_journey(_search_journey, running)


def _search_journey(running) -> None:
    app, window = running
    _home(app, window)
    box = _type_query(app, window, e2e.SEARCH_WORD)
    row = _result_row(window)
    assert "TXT" in row.element_info.name          # the accessible name carries the kind too
    # Escape empties the box and the results (the M9 regression, black-box).
    app.focus(window)
    box.type_keys("{ESC}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not (box.get_value() or "").strip():
            break
        time.sleep(0.3)
    assert not (box.get_value() or "").strip(), "Escape did not empty the search box"
    time.sleep(0.8)
    assert not [n for n in e2e.names(window, "ListItem") if n.startswith("barnsley-survey.txt")],         "the result row survived Escape"


def test_3_selecting_a_result_previews_its_text(running) -> None:
    run_journey(_preview_journey, running)


def _preview_journey(running) -> None:
    app, window = running
    _home(app, window)
    _type_query(app, window, e2e.SEARCH_WORD)
    _result_row(window)
    e2e.settled(window)
    app.click(window, _result_row(window))
    _preview_open(app, window)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if "isolation valves" in e2e.preview_text(window):
            return
        time.sleep(0.5)
    name = e2e.by_id(window, "resultName", "Text", timeout=3).element_info.name
    raise AssertionError(f"the preview pane never showed the document's text (it said {name!r})")


def test_4_a_pop_out_keeps_on_top_for_real(running) -> None:
    run_journey(_pop_out_journey, running)


def _pop_out_journey(running) -> None:
    from pywinauto import Desktop

    app, window = running
    _home(app, window)
    _type_query(app, window, e2e.SEARCH_WORD)
    _result_row(window)
    e2e.settled(window)
    app.click(window, _result_row(window))
    _preview_open(app, window)
    app.click(window, e2e.by_name(window, "Pin in a window", "Button"))

    popped = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and popped is None:
        for candidate in Desktop(backend="uia").windows(class_name="PreviewWindow"):
            if candidate.process_id() in app.pids():
                popped = candidate
        time.sleep(0.3)
    assert popped is not None, "Pin in a window opened no pop-out"
    assert "barnsley-survey.txt" in popped.window_text()

    keep = e2e.by_name(popped, "Keep on top", "CheckBox")
    assert not e2e.is_topmost(popped)
    app.click(popped, keep)
    time.sleep(0.6)
    assert e2e.is_topmost(popped), "Keep on top was ticked but the window is not topmost"
    app.click(popped, keep)
    time.sleep(0.6)
    assert not e2e.is_topmost(popped), "Keep on top was cleared but the window is still topmost"
    popped.close()
    time.sleep(0.6)
    assert window.is_visible(), "closing the pop-out closed the main window"


def test_5_settings_opens(running) -> None:
    run_journey(_settings_journey, running)


def _settings_journey(running) -> None:
    app, window = running
    _home(app, window)
    app.click(window, e2e.by_name(window, "Settings", "CheckBox"))
    assert e2e.by_id(window, "settingsFilter", "Edit", timeout=20).is_visible()
    app.click(window, e2e.by_name(window, "Search", "CheckBox"))


def test_6_a_normal_close_after_a_real_session_ends_the_process(running) -> None:
    """Closes the very app journeys 1-5 have been using (a search, a preview, a
    pop-out and Settings later), which is the close that matters. Not retried:
    the app is gone once it has closed, so a second attempt would only be
    the module's own fixture noticing - a failure here is a real hang."""
    app, window = running
    seconds = app.close_and_time(window, timeout=CLOSE_LIMIT_S)
    e2e.note({"event": "timing", "what": "close to exit, after a session", "seconds": round(seconds, 2)})
    assert seconds < CLOSE_LIMIT_S


def test_7_closing_mid_search_still_ends_the_process(fresh) -> None:
    run_journey(_close_mid_search_journey, fresh)


def _close_mid_search_journey(fresh) -> None:
    """The shutdown race, black-box: a search is in flight when the window goes."""
    app, window = fresh
    _type_query(app, window, e2e.SEARCH_WORD + " site survey")
    seconds = app.close_and_time(window, timeout=CLOSE_LIMIT_S)
    e2e.note({"event": "timing", "what": "close to exit, mid-search", "seconds": round(seconds, 2)})
    assert seconds < CLOSE_LIMIT_S
