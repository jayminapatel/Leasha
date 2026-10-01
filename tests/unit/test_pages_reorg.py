r"""The pages reorg: Settings' five shelves, Indexing's three shelves.

Layer: L5. `docs/WORKORDER-202626271328-pages-reorg.md` §3.

Structure choice (§0.4): a sidebar category list for both pages, built once
as `app/ui/widgets/category_nav.py` so Settings (five categories) and
Indexing (three) share one mechanism rather than each growing its own.

**Deliberately not a `QStackedWidget`** — see that module's docstring. A
stack's size hint is the maximum over every page it holds, so the smallest
category would be forced as tall as the largest one, which is exactly the
kind of layout fault §2b exists to remove. Plain visibility toggling inside
one `QVBoxLayout` is what a hidden page contributes nothing to its parent's
size hint, and that mechanism is also what lets the filter box (§1b) show
several categories at once — "categories auto-expanding to show hits".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

ROOT = Path(__file__).resolve().parents[2]

#: The commit immediately before this order's changes landed. Used only by
#: the verbatim-labels test below, which reads the *pre-reorg* text from git
#: rather than from a hand-copied fixture — "verify, never guess" applies to
#: the test's own baseline as much as to the code it checks.
PRE_REORG_COMMIT = "28e3e9fe6d3d6624524446ce1ee0be8b09435711"

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


def _qt():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _process(app, n: int = 6) -> None:
    for _ in range(n):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 5.0) -> bool:
    """Poll `predicate()` while pumping the event loop.

    A `CallableWorker` runs on a real OS thread and reports back through a
    queued signal, so a fixed number of `processEvents()` calls is a race - a
    slow machine needs more turns than a fast one, not more attempts at
    guessing a number.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def settings_and_store(tmp_path):
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    store = SqliteStore(settings.fts_db).connect()
    yield settings, store
    store.close()


def _settings_view(settings, store):
    from app.ui.settings_view import SettingsView

    _qt()
    return SettingsView(settings, store)


# ---------------------------------------------------------------------------
# §1a / §2a: the categories exist, in order, and nothing is orphaned
# ---------------------------------------------------------------------------

def test_settings_has_five_categories_in_order(settings_and_store):
    from app.ui.settings_view import (
        CATEGORY_APPEARANCE, CATEGORY_MODELS, CATEGORY_SEARCH,
        CATEGORY_STORAGE, CATEGORY_WHATS_INDEXED,
    )

    settings, store = settings_and_store
    view = _settings_view(settings, store)

    assert view._nav.category_names() == [
        CATEGORY_WHATS_INDEXED, CATEGORY_SEARCH, CATEGORY_MODELS,
        CATEGORY_APPEARANCE, CATEGORY_STORAGE,
    ]


def test_every_original_box_lands_inside_some_category(settings_and_store):
    """None of the twelve original group boxes are left outside the nav -
    "new boxes join the category that reads right; none are left orphaned"
    applies just as much to the boxes that already existed."""
    settings, store = settings_and_store
    view = _settings_view(settings, store)

    for widget in (view.roots_box, view.code_types, view.search_box,
                   view.search_behaviour, view.editor_box, view.window_box,
                   view.storage_box, view.models, view.file_types,
                   view.activity, view.environment, view.restore_defaults):
        found = any(
            view._nav.page(name).isAncestorOf(widget)
            for name in view._nav.category_names()
        )
        assert found, f"{widget!r} is not inside any Settings category"


def test_theme_lives_under_appearance(settings_and_store):
    """§0's settled decision #2: theme relocates to Appearance. The control
    itself already moved out of the indexing panel (tuning order 4c-4); what
    this order does is give it the right shelf."""
    from app.ui.settings_view import CATEGORY_APPEARANCE

    settings, store = settings_and_store
    view = _settings_view(settings, store)

    assert view._nav.page(CATEGORY_APPEARANCE).isAncestorOf(view.theme)


def test_indexing_has_its_categories_in_order():
    """Four since 1 October 2026: the owner asked for the indexing levers to be
    grouped by place on a page of their own - *What gets read*. Was three
    (`test_indexing_has_three_categories_in_order`)."""
    from app.ui.indexing_view import (
        CATEGORY_SCHEDULE, CATEGORY_STATUS, CATEGORY_TUNING, CATEGORY_WHAT_GETS_READ,
        IndexingView,
    )

    _qt()
    view = IndexingView()

    assert view._nav.category_names() == [
        CATEGORY_STATUS, CATEGORY_WHAT_GETS_READ, CATEGORY_SCHEDULE, CATEGORY_TUNING]


def test_the_coverage_levers_sit_on_what_gets_read_not_tuning():
    from app.ui.indexing_view import CATEGORY_TUNING, CATEGORY_WHAT_GETS_READ, IndexingView

    _qt()
    view = IndexingView()

    assert view._nav.page(CATEGORY_WHAT_GETS_READ).isAncestorOf(view.tuning.coverage)
    assert not view._nav.page(CATEGORY_TUNING).isAncestorOf(view.tuning.coverage)


def test_schedule_and_tuning_boxes_reach_their_own_pages():
    from app.ui.indexing_view import (
        CATEGORY_SCHEDULE, CATEGORY_TUNING, IndexingView,
    )

    _qt()
    view = IndexingView()

    assert view._nav.page(CATEGORY_SCHEDULE).isAncestorOf(view.schedule_box)
    assert view._nav.page(CATEGORY_TUNING).isAncestorOf(view.tuning)


def test_start_indexing_control_still_works_from_its_new_home():
    """§3's navigation scenario: the Start button is the same object
    `shell.py` already wires up (`self.indexing_view.start_button...`),
    just reachable from the Status shelf instead of the top of a flat page."""
    from app.ui.indexing_view import CATEGORY_STATUS, IndexingView

    _qt()
    view = IndexingView()

    assert view._nav.page(CATEGORY_STATUS).isAncestorOf(view.start_button)
    assert view.start_button.isEnabled()


def test_every_indexing_category_is_reachable_one_at_a_time():
    from app.ui.indexing_view import IndexingView

    _qt()
    view = IndexingView()

    for name in view._nav.category_names():
        view._nav.show_category(name)
        page = view._nav.page(name)
        assert page.isVisibleTo(view), f"{name} did not become visible"
        for other in view._nav.category_names():
            if other != name:
                assert not view._nav.page(other).isVisibleTo(view), (
                    f"{other} stayed visible while {name} was selected")


# ---------------------------------------------------------------------------
# §2b: the layout fix
# ---------------------------------------------------------------------------

def test_status_keeps_its_own_scrolling_schedule_and_tuning_gain_theirs():
    r"""**The diagnosis, pinned rather than only written in prose.** `shell.py`
    wraps Settings externally (`scroll=True`) but Indexing not at all - true
    when the skips panel was the tallest thing on the page, false the moment
    the whole Index Tuning screen (a mode switch, a machine card and four
    more group boxes) landed underneath it with nothing to scroll. `shell.py`
    is outside this order's file scope, so the fix has to hold without that
    line changing: Schedule and Tuning - the two shelves that pushed the old
    page past its height - each carry their own `widgets/scroll.scrollable`
    wrap. Status does not: it already scrolls its own contents (the skips
    panel) and wrapping it again would be the two-scrollbars fault
    `widgets/scroll.py` itself warns about.
    """
    from PyQt6.QtWidgets import QScrollArea

    from app.ui.indexing_view import (
        CATEGORY_SCHEDULE, CATEGORY_STATUS, CATEGORY_TUNING, IndexingView,
    )

    _qt()
    view = IndexingView()

    assert isinstance(view._nav.page(CATEGORY_SCHEDULE), QScrollArea)
    assert isinstance(view._nav.page(CATEGORY_TUNING), QScrollArea)
    assert not isinstance(view._nav.page(CATEGORY_STATUS), QScrollArea)


#: A window nobody would call large, a maximised one, and the minimum this
#: project treats as sensible (the same floor `widgets/scroll.py` uses for
#: its own content width, given a modest height to go with it).
SIZES = {
    "default": (1280, 800),
    "maximised": (1920, 1080),
    "minimum": (1024, 600),
}


@pytest.fixture(scope="module")
def indexing_page():
    from PyQt6.QtWidgets import QApplication

    from app.ui.indexing_view import IndexingView
    from app.ui.theme import stylesheet

    app = QApplication.instance() or QApplication([])
    view = IndexingView()
    # **On the view, not on the application** - Qt cascades it to the children, so the
    # metrics are the same, and nothing walks every widget alive in the process. See
    # `test_no_application_stylesheet.py` for the crash that walk caused three times.
    view.setStyleSheet(stylesheet("light"))
    yield app, view
    view.hide()


@pytest.mark.parametrize("size_name", sorted(SIZES))
def test_no_indexing_control_is_laid_out_at_zero_height(indexing_page, size_name):
    """The same regression class `test_settings_layout.py` guards against on
    Settings, run here at all three sizes §2b's acceptance names: default,
    maximised, and the minimum sensible size."""
    from PyQt6.QtWidgets import (
        QCheckBox, QComboBox, QLineEdit, QPushButton, QSpinBox,
    )

    app, view = indexing_page
    width, height = SIZES[size_name]
    view.resize(width, height)
    view.show()
    _process(app)

    missing = []
    for name in view._nav.category_names():
        view._nav.show_category(name)
        _process(app)
        page = view._nav.page(name)
        for kind in (QCheckBox, QComboBox, QLineEdit, QPushButton, QSpinBox):
            for widget in page.findChildren(kind):
                if not widget.isVisibleTo(view):
                    continue
                if widget.height() <= 0:
                    label = getattr(widget, "text", lambda: "")() or widget.objectName()
                    missing.append(f"{name}: {kind.__name__} {label!r}")

    assert not missing, (
        f"at {size_name} ({width}x{height}), laid out at zero height:\n  "
        + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------------
# §1b: the filter box
# ---------------------------------------------------------------------------

def test_filter_shows_matches_and_hides_the_rest(settings_and_store):
    """The order's own illustration types "memory" - but every setting whose
    label or tooltip names memory lives on the Indexing/Tuning surface
    (§0's own text keeps Tuning on the Indexing page), so no Settings-page
    control can match it today. "rerank" is a real Settings-page match and
    exercises the identical mechanism; the all-words-fail case is its own
    test below, using the order's literal word."""
    from app.ui.settings_view import CATEGORY_SEARCH, CATEGORY_WHATS_INDEXED

    settings, store = settings_and_store
    view = _settings_view(settings, store)
    app = _qt()

    rerank = view.findChild(object, "RERANK_ENABLED")
    hotkey = view.findChild(object, "MINI_SEARCH_HOTKEY")
    assert rerank is not None and hotkey is not None

    view.filter_box.setText("rerank")
    _process(app)

    assert rerank.isVisibleTo(view), "the matching setting was hidden"
    assert not hotkey.isVisibleTo(view), "a non-matching setting stayed visible"
    assert view._nav.page(CATEGORY_SEARCH).isVisibleTo(view), (
        "a category holding a hit was hidden")
    assert not view._nav.page(CATEGORY_WHATS_INDEXED).isVisibleTo(view), (
        "a category with no hits stayed visible - filtering did nothing")


def test_clearing_the_filter_restores_everything(settings_and_store):
    """The hidden setting's visibility is reset even though its category
    (Search) is not the one the sidebar was last showing - `_reveal_all_
    controls` un-hides every control regardless of which page it is on,
    and `restore_single_view` then shows only the sidebar's own category."""
    from app.ui.settings_view import CATEGORY_SEARCH

    settings, store = settings_and_store
    view = _settings_view(settings, store)
    app = _qt()

    hotkey = view.findChild(object, "MINI_SEARCH_HOTKEY")
    view.filter_box.setText("rerank")
    _process(app)
    view.filter_box.setText("")
    _process(app)

    assert not view.filter_empty.isVisibleTo(view)
    view._nav.show_category(CATEGORY_SEARCH)
    assert hotkey.isVisibleTo(view), "the hidden setting never came back"


def test_a_filter_with_no_matches_says_so_in_plain_words(settings_and_store):
    """The order's own word: "memory" matches nothing on the Settings page
    today (see the note on the test above), which makes it the honest
    example for the zero-match state rather than a stand-in for one."""
    settings, store = settings_and_store
    view = _settings_view(settings, store)
    app = _qt()

    # 2026-09-19: "memory" stopped being a no-match word when the video and
    # audio panel arrived - its model help says "and more memory", so the
    # filter now (correctly) finds it. A word no setting could ever contain
    # keeps this test about the empty state and not about the wording of help.
    view.filter_box.setText("zqxjvw")
    _process(app)

    assert view.filter_empty.isVisibleTo(view)
    assert view.filter_empty.text().strip(), "the empty state says nothing"


def test_the_sidebar_is_disabled_while_filtering_and_reenabled_after(settings_and_store):
    settings, store = settings_and_store
    view = _settings_view(settings, store)
    app = _qt()

    view.filter_box.setText("rerank")
    _process(app)
    assert not view._nav.sidebar.isEnabled()

    view.filter_box.setText("")
    _process(app)
    assert view._nav.sidebar.isEnabled()


# ---------------------------------------------------------------------------
# §1c: the last-open category is remembered
# ---------------------------------------------------------------------------

def test_first_run_opens_the_first_category(settings_and_store):
    from app.ui.settings_view import CATEGORY_WHATS_INDEXED

    settings, store = settings_and_store
    view = _settings_view(settings, store)

    assert view._nav.current_category() == CATEGORY_WHATS_INDEXED


def test_last_category_is_remembered_across_a_simulated_relaunch(settings_and_store):
    """Two `SettingsView`s against the same store, one after the other - the
    same shape a relaunch has: a fresh window, the same on-disk state."""
    from app.ui.settings_view import CATEGORY_MODELS, SettingsView

    settings, store = settings_and_store
    app = _qt()

    first = _settings_view(settings, store)
    first._nav.show_category(CATEGORY_MODELS)
    # Queued on the ordered state writer since bug 3a - wait for it.
    from app.ui.state_writes import pool
    assert pool().waitForDone(5000)
    assert store.get_state("ui:settings_category") == CATEGORY_MODELS

    second = SettingsView(settings, store)
    second.refresh_slow_labels()

    assert _wait_until(
        app, lambda: second._nav.current_category() == CATEGORY_MODELS), (
        "the remembered category never arrived")


def test_settings_view_construction_never_touches_the_store(monkeypatch, settings_and_store):
    """M13, re-checked against the new code specifically: building the view
    must not read `ui:settings_category` (or anything else) before the
    window is running - only `refresh_slow_labels`, called post-construction
    exactly as `shell.py` already calls it, may."""
    settings, store = settings_and_store
    calls = []
    real_get_state = store.get_state

    def _tracked(*args, **kwargs):
        calls.append(args)
        return real_get_state(*args, **kwargs)

    monkeypatch.setattr(store, "get_state", _tracked)
    _settings_view(settings, store)

    assert not calls, f"the store was read during construction: {calls}"


# ---------------------------------------------------------------------------
# §3: labels, descriptions and tooltips moved verbatim
# ---------------------------------------------------------------------------

#: Constructors and setter calls whose string argument is something a person
#: reads - a label, a description, a tooltip - never a registry key or an
#: object name, which are policed by `test_settings_reachable.py` instead.
_TEXT_CALLS = {"setToolTip", "setPlaceholderText", "setText", "addItem"}
_TEXT_CTORS = {"QLabel", "QCheckBox", "QPushButton", "QGroupBox"}


def _ui_strings(source_text: str) -> set[str]:
    import ast

    tree = ast.parse(source_text)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        if name not in _TEXT_CALLS and name not in _TEXT_CTORS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.strip():
                    found.add(arg.value)
    return found


def _pre_reorg_text(relative: str) -> str:
    # **`encoding="utf-8"` explicitly** - the source files carry real
    # ellipses (U+2026) and em-dashes, and `text=True` alone decodes with
    # the platform's default (cp1252 on Windows), which turns every one of
    # them into `â€¦`-shaped garbage and makes every string in the file look
    # "changed" whether it moved or not.
    result = subprocess.run(
        ["git", "show", f"{PRE_REORG_COMMIT}:{relative}"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("relative", [
    "app/ui/settings_view.py",
    "app/ui/indexing_view.py",
    "app/ui/indexing_settings.py",
])
def test_every_pre_reorg_label_and_tooltip_still_exists_verbatim(relative):
    r"""§3's before/after walk, read from the commit just before this order's
    changes (`PRE_REORG_COMMIT`) rather than from a hand-copied fixture -
    "verify, never guess" applies to the test's own baseline too.

    A superset is expected and fine: the filter box (§1b) is new wording for
    a new control, which the standing rule never covered. What must never
    happen is a *pre-existing* string disappearing or changing by even a
    character - a relocation quietly rewording something on the way.
    """
    before = _ui_strings(_pre_reorg_text(relative))
    after = _ui_strings((ROOT / relative).read_text(encoding="utf-8"))

    missing = before - after
    assert not missing, (
        f"{relative}: these pre-reorg strings are gone or changed:\n  "
        + "\n  ".join(sorted(missing))
    )
