"""The window must never stop answering. Regression tests for the day it did.

Layer: L5

Reported from a real session: *"the program crashed when i was clicking around,
the thread is stuck, ctrl c does not work in powershell and i had to end task"*.
Nothing had crashed. The UI thread was inside `subprocess.run(timeout=120)`,
which is indistinguishable from a crash from the outside - and Ctrl+C could not
end it either, because Qt's event loop never returns to Python long enough for a
signal handler to run.

None of this can be tested by opening a window: Qt needs a display, and a test
that hangs for two minutes to prove something hangs for two minutes is not a
test anybody will keep. So the checks here are of two kinds.

**Static**: read the view modules as text and assert that no blocking call sits
in a UI-thread method. Crude, and it catches exactly the class of mistake that
caused this - each of these was written because it looked harmless.

**Behavioural**: the logic that was moved out of the widgets is now plain
functions, and those are tested directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def source(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def tree(name: str) -> ast.Module:
    return ast.parse(source(name))


# ---------------------------------------------------------------------------
# Nothing blocking on the UI thread
# ---------------------------------------------------------------------------

def _calls_in(node: ast.AST) -> set[str]:
    """Every dotted call name inside a node, e.g. `subprocess.run`."""
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            base = func.value
            prefix = base.id if isinstance(base, ast.Name) else ""
            found.add(f"{prefix}.{func.attr}" if prefix else func.attr)
        elif isinstance(func, ast.Name):
            found.add(func.id)
    return found


def _worker_bodies(module: ast.Module) -> set[int]:
    """Line numbers inside nested functions - a worker's closure, not the UI."""
    inner: set[int] = set()
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deep in ast.walk(child):
                    if hasattr(deep, "lineno"):
                        inner.add(deep.lineno)
    return inner


@pytest.mark.parametrize("name", sorted(p.name for p in UI.glob("*_view.py")))
def test_no_view_runs_a_subprocess_on_the_ui_thread(name: str) -> None:
    """`subprocess.run` blocks until the child exits. On the UI thread that is a
    window that stops repainting - which is what happened, for up to two minutes,
    every time somebody pressed "Run doctor"."""
    calls = _calls_in(tree(name))
    assert "subprocess.run" not in calls, (
        f"{name} calls subprocess.run directly. Move it into a plain function "
        f"and run it through CallableWorker."
    )
    assert "check_output" not in calls and "subprocess.check_output" not in calls


@pytest.mark.parametrize("name", sorted(p.name for p in UI.glob("*_view.py")))
def test_no_view_sleeps(name: str) -> None:
    """A sleep on the UI thread is a freeze with a timer on it."""
    assert "time.sleep" not in _calls_in(tree(name)), f"{name} sleeps on the UI thread"


def test_no_view_reads_the_store_while_painting() -> None:
    """A `_refreshed`-style method is handed data; it must not fetch it.

    The Graph panel read `top_entities(500)` plus `edges_among` over 96,712
    edges on the UI thread, on every tab switch - and waited on the SQLite lock
    whenever an index run held a write. That panel is gone with the knowledge
    graph, but the rule it broke applies to every view that replaces it, so the
    test outlives the code that failed it.
    """
    for path in sorted(UI.glob("*_view.py")):
        module = tree(path.name)
        for node in ast.walk(module):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.endswith(("_refreshed", "_painted", "_done")):
                continue
            reads = [c for c in _calls_in(node) if c.startswith("_store.")]
            assert not reads, (
                f"{path.name}.{node.name} paints; it must be handed data, not "
                f"fetch it. Found: {reads}"
            )


def test_settings_counts_the_usage_log_without_reading_it() -> None:
    """It was `len(recent_searches(limit=100_000))`: a hundred thousand rows
    fetched, turned into dictionaries and discarded, to produce one number."""
    # Against the calls, not the text - the docstring explaining the fix names
    # the old method, and a test that reads prose is a test that fails on a
    # comment.
    #
    # **Both modules, because the work moved.** The count used to be taken in
    # the view's constructor; M13 moved it to a worker and the sentence it
    # feeds to the presenter, which is where the 250-line guard wants it. The
    # rule did not move - fetching a hundred thousand rows to count them is
    # wrong wherever it is written - so the test follows the rule rather than
    # the file it was first broken in.
    calls = set()
    for module in ("settings_view.py", "presenter.py"):
        calls |= _calls_in(tree(module))

    assert not any(call.endswith("recent_searches") for call in calls), (
        "use count_searches(), which is a COUNT(*) rather than 100,000 rows"
    )
    assert any(call.endswith("count_searches") for call in calls)


# ---------------------------------------------------------------------------
# The doctor logic, now that it is out of the widget
# ---------------------------------------------------------------------------

def test_a_healthy_report_reads_as_ready() -> None:
    from app.ui.presenter import doctor_lines

    lines = doctor_lines({"ready": True, "checks": [
        {"name": "Python", "ok": True, "detail": "3.12.4"},
    ]})
    assert lines[0] == "READY"
    assert any("PASS" in line and "Python" in line for line in lines)


def test_a_failure_carries_its_fix() -> None:
    """Every AppError in this project states what to do. A diagnostics panel
    that shows the failure and hides the fix wastes the work of writing it."""
    from app.ui.presenter import doctor_lines

    lines = doctor_lines({"ready": False, "checks": [
        {"name": "FTS5", "ok": False, "detail": "missing", "fix": "reinstall Python"},
    ]})
    assert lines[0] == "NOT READY"
    assert any("FIX: reinstall Python" in line for line in lines)


def test_an_optional_failure_is_a_warning_not_a_failure() -> None:
    """pywin32 missing means no Outlook mailbox. Everything else still works,
    and calling it FAIL sends people chasing a problem they do not have."""
    from app.ui.presenter import doctor_lines

    lines = doctor_lines({"ready": True, "checks": [
        {"name": "pywin32", "ok": False, "optional": True, "detail": "not installed"},
    ]})
    assert any("[WARN]" in line for line in lines)
    assert not any("[FAIL]" in line for line in lines)


def test_a_malformed_report_still_renders() -> None:
    """This is the diagnostics view. A formatter that raises on a missing key
    hides the very output somebody opened it to read."""
    from app.ui.presenter import doctor_lines

    assert doctor_lines({})[0] == "NOT READY"
    assert doctor_lines({"checks": [{}]})


# ---------------------------------------------------------------------------
# Ollama: the 120 seconds that produced nothing
# ---------------------------------------------------------------------------

def test_connect_and_read_timeouts_are_separate() -> None:
    """`requests` applies a single float to both phases. So the 120-second budget
    meant for the model's reply was also spent finding out that nothing was
    listening: one enrichment run sat for 120.09s and processed zero chunks.

    Ollama is a process on this machine. The socket opens in milliseconds or it
    is not going to open.
    """
    from app.llm.ollama import OllamaClient

    connect, read = OllamaClient(timeout=120.0)._budget(120.0)
    assert read == 120.0, "the model still gets its full time to answer"
    assert connect <= 5.0, "but not to answer the phone"


def test_the_connect_timeout_never_exceeds_the_read_budget() -> None:
    """A caller asking for a one-second answer must not wait three to connect."""
    from app.llm.ollama import OllamaClient

    connect, read = OllamaClient()._budget(1.0)
    assert connect <= read


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

def test_the_scroll_helper_resizes_its_widget() -> None:
    """`setWidgetResizable(True)` is the line everybody misses. Without it the
    inner widget keeps its sizeHint forever, so a maximised window shows a
    narrow column of content with a horizontal scrollbar under it."""
    text = (UI / "widgets" / "scroll.py").read_text(encoding="utf-8")
    assert "setWidgetResizable(True)" in text


def test_the_window_maps_views_to_tab_indexes() -> None:
    """A view inside a scroll area is not the widget in the tab, so
    `setCurrentWidget(view)` silently does nothing and
    `tabs.widget(i) is view` is silently False. Both fail without an error,
    which is how adding a scrollbar breaks navigation unnoticed.
    """
    text = source("shell.py")
    assert "_tab_index" in text
    assert "self.rail.setCurrentWidget(" not in text and "self.tabs" not in text, (
        "use self._show(view), which works whether or not the view is wrapped"
    )


def test_the_window_has_a_minimum_size() -> None:
    """Without one Qt shrinks the window until only the tab bar is left, and a
    view with no scroll area then has controls that cannot be reached at all."""
    assert "setMinimumSize(" in source("shell.py")


# ---------------------------------------------------------------------------
# Signals connected once
# ---------------------------------------------------------------------------

def test_the_theme_hook_is_connected_once() -> None:
    """`_apply_theme` connected `colorSchemeChanged` to a lambda that calls
    `_apply_theme`. Every theme change added another connection, so one flick of
    the system switch re-entered the handler once per change ever made - each
    one connecting again. Qt gives no warning for a duplicate connection."""
    text = source("shell.py")
    assert "_theme_hooked" in text, "guard the connection with a flag"

    module = tree("shell.py")
    apply_theme = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_theme"
    )
    guarded = [
        node for node in ast.walk(apply_theme)
        if isinstance(node, ast.If) and "_theme_hooked" in ast.dump(node)
    ]
    assert guarded, "the connect must sit behind the flag, not beside it"


def test_a_second_index_run_is_refused_before_anything_is_built() -> None:
    r"""Six index runs appeared in seven seconds of ordinary clicking. Each one
    built a Pipeline and an Embedder - loading the ONNX model - only for
    `IndexingView.start` to discard them silently.

    **2026-09-07 UI freeze fix moved the build itself out of `_start_indexing`.**
    `resolve_for_run` (the tuning numbers a Pipeline needs) now runs on a
    worker thread - it was the thing freezing the window on a cold hardware
    cache - so `_start_indexing` only dispatches the resolve and returns; the
    actual `Pipeline`/`Embedder` construction happens a beat later, in
    `_index_resolved`, once the result is back on the GUI thread. The guard
    this test protects still has to hold in *both* places: `_start_indexing`
    must check before it will even dispatch a resolve, and `_index_resolved`
    must check again before it builds anything, since a run could have begun
    elsewhere while the resolve was in flight.
    """
    # Both moved to `IndexController` (work order 202626082352 section 7).
    module = tree("controllers/index_controller.py")
    start = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_start_indexing"
    )
    resolved = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_index_resolved"
    )

    def is_running_line(fn):
        return min(
            (node.lineno for node in ast.walk(fn)
             if isinstance(node, ast.Call) and "is_running" in ast.dump(node.func)),
            default=None,
        )

    # By line number, and against the *call* rather than the import - the local
    # `from app.index.pipeline import Pipeline` sits at the top of the method
    # and costs nothing; constructing one is what loads the model.
    def built_line(fn):
        return min(
            (node.lineno for node in ast.walk(fn)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"Pipeline", "Embedder"}),
            default=None,
        )

    start_guard = is_running_line(start)
    resolved_guard = is_running_line(resolved)
    built = built_line(resolved)

    assert start_guard is not None, (
        "_start_indexing must check whether a run is in flight before it "
        "will even dispatch a resolve worker"
    )
    assert built is None or built_line(start) is None, (
        "Pipeline/Embedder must not be constructed inside _start_indexing "
        "any more - that work is off-thread now, in _index_resolved"
    )
    assert resolved_guard is not None, (
        "_index_resolved must check again whether a run is in flight - a run "
        "could have started elsewhere while the resolve was out on its worker"
    )
    assert built is not None, "this test is watching the wrong names"
    assert resolved_guard < built, (
        "and must check before building anything expensive"
    )


def test_ctrl_c_is_wired_up() -> None:
    """Python does not deliver signals from inside C code. Without a timer
    handing control back to the interpreter, Ctrl+C in the terminal does nothing
    at all and End Task is the only way to stop the application."""
    text = (UI.parent / "main.py").read_text(encoding="utf-8")
    assert "SIGINT" in text
    assert "QTimer" in text, "a signal handler alone never runs while Qt owns the loop"


# ---------------------------------------------------------------------------
# Shutdown must actually stop the timers, not merely appear to
# ---------------------------------------------------------------------------

class _FakeTimer:
    def __init__(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class _FakeView:
    """A view with the timers named as the real ones are."""

    def __init__(self) -> None:
        self._generation = 0
        self._shown_generation = 0
        self._interim_timer = _FakeTimer()
        self._full_timer = _FakeTimer()
        # **Must be left alone, and the name is the point.**
        #
        # This was `not_a_timer`, which reads as "not a timer" to a person
        # and ends in `_timer` to `str.endswith` - so the suffix rule
        # stopped it, correctly, and the test failed for a year of
        # afternoons looking like a bug in `stop_timers`. The production
        # code was right the whole time; the fixture was named to trip it.
        self.refresh_handle = _FakeTimer()


def test_shutdown_stops_every_timer_a_view_owns():
    """U1, and the reason it went unnoticed for so long.

    `search_view.shutdown()` asked for `_typing_timer`, `_idle_timer` and
    `_timer`. Its timers are `_interim_timer` and `_full_timer`. Every name
    missed, `getattr(view, name, None)` returned None three times, and shutdown
    stopped nothing - so the debounce timers kept running into teardown and
    could start a search against a closing store. The call looked correct at
    both ends and did nothing at all.

    Names are found by suffix now, so a rename cannot silently disarm it.
    """
    # `stop_timers` is pure Python, but it lives beside QRunnable and the
    # module imports Qt - so a headless machine skips rather than errors.
    pytest.importorskip("PyQt6.QtCore", exc_type=ImportError)
    from app.ui.workers import stop_timers

    view = _FakeView()
    stop_timers(view)

    assert not view._interim_timer.running
    assert not view._full_timer.running
    assert view.refresh_handle.running, (
        "an attribute that is not named like a timer was stopped anyway")


def test_shutdown_stales_anything_still_in_flight():
    """A result landing after the window starts closing must be dropped, which
    is what the generation counters are for."""
    # `stop_timers` is pure Python, but it lives beside QRunnable and the
    # module imports Qt - so a headless machine skips rather than errors.
    pytest.importorskip("PyQt6.QtCore", exc_type=ImportError)
    from app.ui.workers import stop_timers

    view = _FakeView()
    stop_timers(view)

    assert view._generation == 1
    assert view._shown_generation == 1


def test_a_view_naming_its_timers_explicitly_still_works():
    """The argument list is an optimisation, not a promise - both paths stop."""
    # `stop_timers` is pure Python, but it lives beside QRunnable and the
    # module imports Qt - so a headless machine skips rather than errors.
    pytest.importorskip("PyQt6.QtCore", exc_type=ImportError)
    from app.ui.workers import stop_timers

    view = _FakeView()
    stop_timers(view, "_interim_timer")

    assert not view._interim_timer.running
    assert not view._full_timer.running, "the unnamed one must stop too"


def test_every_view_with_timers_stops_them_on_shutdown():
    """Static: a view that grows a timer and forgets `shutdown` puts the race
    straight back, and nothing would fail until somebody closed the window at
    the wrong moment."""
    import ast
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "app" / "ui"
    for path in sorted(ui.glob("*_view.py")):
        source = path.read_text(encoding="utf-8")
        if "QTimer(" not in source:
            continue
        tree = ast.parse(source)
        names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "shutdown" in names, (
            f"{path.name} creates a QTimer but has no shutdown() to stop it"
        )
        assert "stop_timers" in source, (
            f"{path.name}.shutdown must call stop_timers - see workers.py"
        )


# ---------------------------------------------------------------------------
# Nothing runs on a background thread until construction is over
#
# The rule is stated in `MainWindow.__init__` and it was earned: a worker
# opening SQLite while the main thread was inside `_apply_theme` - which
# re-polishes every widget in the tree - produced a **Windows access violation**
# with no Python exception, no traceback and no window. The faulthandler dump
# named `_apply_theme` on one thread and `read_index_summary -> stats ->
# _new_connection` on another.
#
# `refresh_totals`, `_warm_translator` and `_warm_models` were moved to
# `_start_background_work`, on the next turn of the event loop, and the crash
# stopped. Then the watch timer for a run in another process was added, started
# from `__init__`, and put the application straight back into the same race -
# reported as "the program crashed when it started and it is unresponsive when
# it opens".
#
# A comment did not hold the line. This does.
# ---------------------------------------------------------------------------

def _init_body_calls() -> set[str]:
    """Calls made *directly* by `MainWindow.__init__`.

    Lambdas are skipped deliberately: a call inside one is connected to a
    signal, so it happens when that signal fires rather than during
    construction, which is the whole distinction being tested.
    """
    import ast
    from pathlib import Path as _P

    source = (_P(__file__).resolve().parents[2] / "app" / "ui" / "shell.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))

    init = next(
        node for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name == "MainWindow"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )

    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Lambda(self, node):        # noqa: N802 - ast's name
            return                           # deferred to a signal; not our concern

        def visit_Call(self, node):          # noqa: N802 - ast's name
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name:
                found.add(name)
            self.generic_visit(node)

    Visitor().visit(init)
    return found


def test_construction_starts_no_background_work():
    r"""**The rule the window's own comment states, enforced.**

    Anything that opens the store or takes a thread belongs in
    `_start_background_work`, which runs on the next turn of the event loop with
    the widget tree complete and Qt idle.
    """
    called = _init_body_calls()

    forbidden = {
        "run": "starts a QRunnable on a thread pool",
        "refresh_totals": "opens the store on a worker",
        "_poll_external_run": "starts a worker that reads the store and a mutex",
        "_warm_translator": "starts a worker",
        "_warm_models": "starts a worker",
        "_scan_corpus": "walks the filesystem on a worker",
    }
    offences = sorted(f"{name} - {why}" for name, why in forbidden.items()
                      if name in called)

    assert not offences, (
        "MainWindow.__init__ starts background work:\n  " + "\n  ".join(offences)
        + "\n\nMove it into _start_background_work. A worker touching SQLite "
          "while __init__ is still running - and _apply_theme still to come - "
          "is an access violation with no Python exception and no window."
    )


def test_the_watch_timer_is_built_in_init_but_started_later():
    """Built early so nothing can forget it; started late so nothing can race."""
    called = _init_body_calls()

    assert "setInterval" in called, "the watch timer should be configured in __init__"
    assert "_poll_external_run" not in called, "and polled only once running"
