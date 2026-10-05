"""The standing rule: background work never freezes the window.

Layer: L5

From the work order, and now a constraint rather than a preference:

> No background work may ever disable, freeze, or grey out the user interface.

`docs/TROUBLESHOOTING.md` used to tell the owner to *wait ten seconds* when the
window went white, and named building the graph and running the environment
check as causes. That entry is deleted, and any remaining white window is a bug
with a reproduction rather than documented behaviour.

These are source-level checks. Qt widgets cannot be built without a display, so
a test cannot watch the event loop stall - but it can catch the four patterns
that cause it, which is where every instance in this project came from.

**Where the worker bodies live.** `app/ui/tasks.py` holds every function that
reads a store, the disk or a subprocess on the presenter's behalf. It is the one
module in `app/ui` allowed to (`WORKER_ONLY`). `app/ui/presenter/` holds
decisions only and is scanned like any view. A view reaches a worker body only
through a worker: it hands the function to a `CallableWorker`, or calls it from
a function that is one. `test_a_worker_body_is_only_called_through_a_worker` is
that rule.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"
MODULES = sorted(p for p in UI.rglob("*.py") if p.name != "__init__.py")


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def calls_in(path: Path) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(source(path))) if isinstance(n, ast.Call)]


def called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def enclosing_function(path: Path, line: int) -> str:
    """Which def a line sits in. Used to allow one named exception."""
    best = ""
    for node in ast.walk(ast.parse(source(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                best = node.name
    return best


def _store_api() -> frozenset[str]:
    """Every public method name on `SqliteStore`."""
    import inspect

    from app.storage.sqlite_store import SqliteStore

    return frozenset(
        name for name, _ in inspect.getmembers(SqliteStore, callable)
        if not name.startswith("_")
    )


# ---------------------------------------------------------------------------
# processEvents
# ---------------------------------------------------------------------------

#: The places it is allowed, and why.
#:
#: `closeEvent` waits for background threads to finish before the stores are
#: torn out from under them. There is no event loop to return to - the window is
#: closing - so the choice is between pumping events and freezing during the one
#: operation nobody will wait out. `_wait_out_minimum_hold`/`_fade_out`
#: (`app/ui/splash.py`) are the same trade for the splash's minimum-hold and
#: fade-out: both are short, deadline-capped waits (`_MAX_HOLD_WAIT_S`,
#: `_FADE_DURATION_S`) that must keep the case-rotation timer and the widget's
#: own repaint running, and there is no window to return control to until the
#: wait is over - mirrors `main.py`'s `_acquire_gui_lock_responsively`, which
#: uses the identical technique for §2c's handover wait (that function is
#: outside `app/ui/`, so this guard never scans it, but the precedent is the
#: same one being named here). Everywhere else it is a symptom of work on
#: the wrong thread, and it reenters the event loop in ways that produce bugs
#: nobody can reproduce.
PROCESS_EVENTS_ALLOWED_IN = {
    "closeEvent", "_drain_workers", "_wait_for_workers",
    "_wait_out_minimum_hold", "_fade_out",
}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_process_events_is_only_used_while_closing(path):
    for node in calls_in(path):
        if called_name(node) != "processEvents":
            continue
        where = enclosing_function(path, node.lineno)
        assert where in PROCESS_EVENTS_ALLOWED_IN, (
            f"{path.name}:{node.lineno} calls processEvents inside {where!r}. "
            "That is work on the wrong thread - move it to a worker."
        )


# ---------------------------------------------------------------------------
# Blocking calls on the UI thread
# ---------------------------------------------------------------------------

#: Never on the UI thread. `subprocess.run` is how `doctor` used to freeze the
#: window for two minutes; `sleep` has no legitimate use in a paint path -
#: with the one named exception in `BLOCKING_ATTRS_ALLOWED_IN` below.
BLOCKING = {"sleep", "waitForFinished", "communicate"}

#: `run` is `workers.run`, which *starts* a worker. `subprocess.run` is the
#: blocking one - distinguished by the attribute it hangs off.
#:
#: **`Popen` and `startfile` were missing from this list**, and the hole let
#: `_open_result` call `open_in_explorer` inline for a week. `explorer /select,`
#: takes a few hundred milliseconds to start; the owner reported opening a
#: result as "too slow" and was right. A guard that names only the obvious
#: blocking call is a guard with a gap the shape of the next bug.
BLOCKING_ATTRS = {
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_output"),
    ("time", "sleep"), ("os", "system"), ("os", "startfile"),
}

#: `(function name) -> which of BLOCKING_ATTRS it may use`. Same shape and
#: same reasoning as `PROCESS_EVENTS_ALLOWED_IN` just above, for the one
#: attribute in `BLOCKING_ATTRS` that has a legitimate, deliberately-bounded
#: use: `_wait_out_minimum_hold`/`_fade_out` (`app/ui/splash.py`) call
#: `time.sleep` between `processEvents()` pumps in a short, deadline-capped
#: loop - the same technique, for the same reason, as `PROCESS_EVENTS_
#: ALLOWED_IN`'s entry for them.
BLOCKING_ATTRS_ALLOWED_IN = {
    ("time", "sleep"): {"_wait_out_minimum_hold", "_fade_out"},
}


#: Modules that exist *to be called from a worker*. `tasks.py` - it holds
#: `doctor_report`, which runs a subprocess deliberately, and every other worker
#: body the presenter used to carry. Blocking is correct there and the
#: guarantee is enforced at the call site instead, by
#: `test_a_long_operation_starts_a_worker` and
#: `test_a_worker_body_is_only_called_through_a_worker`.
#:
#: **`app/ui/presenter/` is not on this list.** It used to be, as one file, and
#: that exemption is exactly what let worker bodies and pure formatters share a
#: module with nothing to tell them apart. The package holds decisions only, so
#: it is scanned like any view.
WORKER_ONLY = {
    "tasks.py",
    "workers.py",
    # "Every line here runs on a worker, which is why it is a module of its own
    # rather than methods on the pane" - its own opening sentence, and the
    # reason the pane hands it a store at all.
    "preview_loader.py",
    # `read_foreground_selection` polls the clipboard for up to `COPY_TIMEOUT_S`
    # while waiting for a synthetic Ctrl+C to answer. Adoptions §4a: called
    # from `shell._offer_foreground_selection` on a `CallableWorker`, precisely
    # so that wait never lands on the UI thread - `MiniSearch.offer_prefill`
    # is where the answer arrives back, a beat after the box was shown.
    "selection.py",
}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_nothing_blocks_the_ui_thread(path):
    if path.name in WORKER_ONLY:
        pytest.skip(f"{path.name} is called from workers by design")
    for node in calls_in(path):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in BLOCKING_ATTRS:
                where = enclosing_function(path, node.lineno)
                assert where in BLOCKING_ATTRS_ALLOWED_IN.get(pair, ()), (
                    f"{path.name}:{node.lineno} calls {pair[0]}.{pair[1]} on the "
                    f"UI thread inside {where!r}. Put it in a CallableWorker."
                )
        if called_name(node) in BLOCKING and not isinstance(func, ast.Name):
            owner = getattr(getattr(func, "value", None), "id", "")
            if owner in ("time", "subprocess"):
                where = enclosing_function(path, node.lineno)
                assert where in BLOCKING_ATTRS_ALLOWED_IN.get((owner, called_name(node)), ()), (
                    f"{path.name}:{node.lineno} blocks the UI thread inside {where!r}"
                )


#: A pool wait that is legitimate because it never runs on the UI thread, keyed
#: by (file, function) so nothing else shares the name. `settle_before_run` lets
#: an index run wait for settings saved just before Start (bug 3a's queue); the
#: test below pins its only caller to the run's own thread.
OFF_THREAD_POOL_WAITS = {("state_writes.py", "settle_before_run")}


def test_settle_before_run_is_only_called_from_the_index_workers_thread():
    """If it were called from the window, the allowance above would be a freeze."""
    callers = []
    for path in MODULES:
        for node in calls_in(path):
            if called_name(node) == "settle_before_run":
                callers.append((path.name, enclosing_function(path, node.lineno)))
    assert callers == [("workers.py", "run")], callers
    source = (UI / "workers.py").read_text(encoding="utf-8")
    body = source[source.index("class IndexWorker"):]
    assert "settle_before_run" in body[:body.index("\ndef ")], (
        "settle_before_run must be called from IndexWorker.run, the run's thread"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_ui_module_waits_on_a_thread_pool_except_when_closing(path):
    """`waitForDone` on the UI thread is a freeze by another name."""
    for node in calls_in(path):
        if called_name(node) != "waitForDone":
            continue
        where = enclosing_function(path, node.lineno)
        if (path.name, where) in OFF_THREAD_POOL_WAITS:
            continue
        assert where in PROCESS_EVENTS_ALLOWED_IN, (
            f"{path.name}:{node.lineno} waits for the thread pool inside "
            f"{where!r}, which freezes the window."
        )


# ---------------------------------------------------------------------------
# Filesystem and database work in a paint path
#
# **The class of bug the named-call list missed.** `subprocess.run` and
# `time.sleep` are obvious. `Path.exists()` is not - it looks free, and it is,
# on a warm local disk. On a network share or a drive that has spun down it
# blocks for seconds, and it was being called *once per row* while filling the
# results model. Twenty stats for a normal page, five hundred for a full one, on
# the UI thread, inside the virtualisation work whose entire purpose was to make
# that list cheap.
#
# A guard that lists only the calls somebody thought of has a gap the shape of
# the next bug. These tests look at *where* a call is instead.
# ---------------------------------------------------------------------------

#: Methods that touch the filesystem. Cheap until they are not.
FILESYSTEM = {"exists", "is_file", "is_dir", "stat", "glob", "rglob", "iterdir"}

#: Methods that reach the database. **Read off `SqliteStore`, not listed here.**
#:
#: This used to be nine names somebody had thought of, and the gap in it was
#: the shape of the next bug: `stats()` was not in it, so three views called
#: three `COUNT(*)` on the UI thread - 93ms measured on a two-million-chunk
#: fixture, around 460ms at ten million - behind a comment calling it cheap.
#: The file above already says a guard listing only the calls somebody thought
#: of has that gap. It was still doing it.
#:
#: Every public method of the store counts now, and a new one is covered the
#: day it is written.
STORE_CALLS = _store_api()

#: Attribute names that hold a store. Matching on the *receiver* as well as the
#: method is what keeps `self.close()` on a widget and `store.close()` apart -
#: the first attempt matched by method name alone and flagged Qt signals.
STORE_RECEIVERS = {"store", "_store", "sqlite", "_sqlite"}

#: Functions that run on a worker by construction, so the calls inside them are
#: fine. Named explicitly rather than inferred, because inferring it is how a
#: genuine violation gets waved through.
OFF_THREAD = {
    "_decorate", "_record_open", "_list_models", "_translate", "_run_doctor",
    "missing_paths", "mail_details", "open_in_explorer",
    # `SELECT 1 FROM files LIMIT 1`. Added *because* `stats()` was being used
    # for this and is three COUNT(*) - see `has_any_files`.
    "_anything_indexed",
    # `value_suggestions` is reached from a keystroke and reads `distinct_values`,
    # which is bounded and index-backed by design - see its docstring. It moved
    # out of the file that was exempt whole, so the exemption is named here.
    "_counted_values",
    # **Deliberately allowed, with the number.** This is `stats()`, about 460ms
    # at ten million chunks, so it is not cheap and this is not an oversight.
    # It runs once, when somebody picks "change the meaning model" from a menu,
    # to fill in how long a rebuild will take. A modal that opens after a short
    # pause reads as the application working; one that opens with a number
    # arriving later reads as one that cannot make up its mind. If the Index
    # Tuning screen gives this a home with progress of its own, move it there.
    "_chunk_count",
    # `IndexController._save_last_index_time` is the `save_last_run` callable
    # `IndexScheduler.notify_finished` hands to a `CallableWorker` - in
    # `scheduler.py`, so the structural `worker_bodies` scan of
    # `index_controller.py` cannot see it. Named once `set_state` stopped being
    # exempt (bug 3a); it was always off the UI thread.
    "_save_last_index_time",
}

#: Keyed reads and writes of `index_state`, allowed **wherever they appear**.
#:
#: One or two rows fetched by primary key from a table with a few dozen in it.
#: The rule is about work that *scales* - a COUNT over the index, a stat per
#: row - and this is not that, whether it happens in a slot or a constructor.
#:
#: **This replaced sixteen function names in `OFF_THREAD`**, one of which was
#: `__init__`. That entry exempted every constructor in the package, which is
#: precisely where M13 lived: `SettingsView.__init__` ran a COUNT(*) and a
#: module import on the UI thread inside `MainWindow.__init__`, and this file
#: said nothing. The distinction that matters is which call it is, not which
#: function it sits in - so it is drawn there now, and a constructor that
#: reaches for anything heavier fails.
#:
#: **Reads only, since bug 3a.** `set_state`/`set_states` were in this set,
#: on the same "one keyed row" reasoning - and that reasoning is right about
#: the *work* and wrong about the *wait*. A write goes through
#: `SqliteStore.write()`, which takes the process-wide `_write_lock` the
#: indexer holds for every batch, so a page switch during an index run froze
#: the window until the batch committed. Reads do not wait: each thread has
#: its own connection and WAL gives it the last committed snapshot. Writes go
#: through `app/ui/state_writes.py` now, and a synchronous one fails below.
KEYED_STATE = {"get_state", "all_state"}

#: Where a synchronous keyed write on the UI thread is still allowed, and why.
#:
#: `MainWindow.closeEvent` saves the window geometry synchronously: the window is about to
#: hide, there is no event loop left to hand a worker's result back to, and
#: `_drain_workers` - called later in the same method - waits (bounded) for
#: every write queued through `state_writes` before the store closes. **This is
#: the only entry.** A second one is a freeze being waved through.
KEYED_WRITE = {"set_state", "set_states"}
KEYED_WRITE_ALLOWED_IN = {("shell.py", "closeEvent")}

#: Painting and model-filling. A blocking call here runs per row.
PAINT_PATHS = {"paint", "sizeHint", "_append", "_rebuild", "data", "_redraw"}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_filesystem_call_in_a_paint_path(path):
    """Per-row work must be arithmetic, not syscalls."""
    tree = ast.parse(source(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in PAINT_PATHS or node.name in OFF_THREAD:
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                assert call.func.attr not in FILESYSTEM, (
                    f"{path.name}:{call.lineno} calls .{call.func.attr}() inside "
                    f"{node.name} - that runs once per row on the UI thread"
                )


def worker_bodies(path: Path) -> set[str]:
    r"""Functions this module hands to a `CallableWorker`.

    **Structural, not a list of names.** `def work()` in `history_pass` and
    `def clear()` in `shell` are both nested functions passed straight to a
    worker, so every call inside them is off the UI thread by construction -
    but naming them in an exemption list would mean the next one is a failure
    until somebody adds it, and adding names to an exemption list to make a
    test pass is how a guard stops guarding.

    `CallableWorker(fn, ...)` and `CallableWorker(self.method, ...)` both count.
    """
    found: set[str] = set()
    for call in calls_in(path):
        if called_name(call) != "CallableWorker" or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Name):
            found.add(first.id)
        elif isinstance(first, ast.Attribute):
            found.add(first.attr)
    return found


def _calls_directly_in(node: ast.AST) -> list[ast.Call]:
    r"""Calls in this function, **not** in functions defined inside it.

    A nested `def work()` handed to a `CallableWorker` runs on a different
    thread from the function that defines it, so attributing its calls to the
    enclosing function reports the opposite of the truth - `history_pass` was
    flagged for a query that is, by construction, the one thing in that file
    guaranteed to be off the UI thread. The nested function is still checked
    on its own.
    """
    out: list[ast.Call] = []
    stack: list[ast.AST] = [node]
    first = True
    while stack:
        current = stack.pop()
        if not first and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                              ast.Lambda, ast.ClassDef)):
            continue
        first = False
        if isinstance(current, ast.Call):
            out.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return out


def _is_store_call(call: ast.Call) -> bool:
    """`store.something()` where `something` is a real store method."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in STORE_CALLS:
        return False
    if func.attr in KEYED_STATE:
        return False
    owner = func.value
    name = (owner.id if isinstance(owner, ast.Name)
            else owner.attr if isinstance(owner, ast.Attribute) else "")
    return name in STORE_RECEIVERS


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_store_call_outside_a_worker(path):
    """A query on the UI thread is a freeze waiting for a busy index."""
    if path.name in WORKER_ONLY:
        pytest.skip(f"{path.name} is called from workers by design")
    tree = ast.parse(source(path))
    off_thread = OFF_THREAD | worker_bodies(path)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in off_thread:
            continue
        for call in _calls_directly_in(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if not _is_store_call(call):
                continue
            if (call.func.attr in KEYED_WRITE
                    and (path.name, node.name) in KEYED_WRITE_ALLOWED_IN):
                continue
            # A call passed *to* CallableWorker is being scheduled, not made.
            scheduled = any(
                isinstance(outer, ast.Call)
                and getattr(outer.func, "id", "") == "CallableWorker"
                and call in ast.walk(outer)
                for outer in ast.walk(node)
                if isinstance(outer, ast.Call)
            )
            assert scheduled, (
                f"{path.name}:{call.lineno} calls .{call.func.attr}() inside "
                f"{node.name} on the UI thread - put it in a CallableWorker"
            )


# ---------------------------------------------------------------------------
# The presenter stays Qt-free
# ---------------------------------------------------------------------------

#: Every Python Qt binding, so a guard written against one cannot pass on another.
QT_BINDINGS = ("PySide6", "PyQt6", "PyQt5", "PySide2")


def test_the_presenter_still_does_not_import_qt():
    """The whole reason the logic is testable. One import here and half of this
    file's guarantees become unverifiable."""
    files = sorted((UI / "presenter").glob("*.py")) + [UI / "tasks.py"]
    assert len(files) > 2, "the presenter package has gone missing"
    for path in files:
        text = source(path)
        # Both bindings (order 202626270238 §2a, 2026-10-05).
        for binding in QT_BINDINGS:
            assert binding not in text, (path.name, binding)


def test_the_model_and_view_option_rules_are_qt_free_too():
    """`view_options.py` and `llm/models.py` hold decisions with a Qt half
    bolted on the bottom. The decisions must stay reachable without a display,
    or they stop being tested."""
    options = source(UI / "view_options.py")
    top = options.split("# The Qt half")[0]
    assert not any(b in top for b in QT_BINDINGS), "Qt reached the decision half of view_options"

    models = (UI.parents[0] / "llm" / "models.py").read_text(encoding="utf-8")
    assert not any(b in models for b in QT_BINDINGS)


# ---------------------------------------------------------------------------
# The worker bodies are in `tasks.py`, and a view reaches them through a worker
#
# **Why a module rather than a comment.** `presenter.py` was one file that was
# exempt from the blocking and store-call checks *as a whole*, because it held
# `doctor_report` and friends. That exempted every formatter beside them too, and
# nothing said which function was which. The bodies are their own module now, so
# the exemption is the size of the thing it is for, and the presenter package is
# held to the same rules as a view.
# ---------------------------------------------------------------------------

PRESENTER = UI / "presenter"
TASKS = UI / "tasks.py"


def _task_functions() -> frozenset[str]:
    """Every function `tasks.py` defines at module level, read off the file."""
    return frozenset(
        node.name for node in ast.parse(source(TASKS)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


#: Read off `tasks.py` rather than listed, for the reason `STORE_CALLS` is: a
#: worker body added tomorrow is covered the day it is written.
TASK_FUNCTIONS = _task_functions()

#: Modules through which a worker body can be reached. The presenter package
#: re-exports them, so `from app.ui.presenter import missing_paths` is the same
#: function as `from app.ui.tasks import missing_paths`.
TASK_SOURCES = ("app.ui.tasks", "app.ui.presenter")


def _task_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """`(names bound to a worker body, names bound to tasks or presenter)`."""
    functions: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in TASK_SOURCES:
                functions |= {alias.asname or alias.name for alias in node.names
                              if alias.name in TASK_FUNCTIONS}
            elif node.module == "app.ui":
                modules |= {alias.asname or alias.name for alias in node.names
                            if alias.name in ("tasks", "presenter")}
        elif isinstance(node, ast.Import):
            modules |= {alias.asname for alias in node.names
                        if alias.asname and alias.name in TASK_SOURCES}
    return functions, modules


def _inline_task_calls(path: Path) -> list[str]:
    """Worker-body calls this module makes outside a worker. The check, as data.

    **Matched on where the name came from, not on its spelling.** `clear_logs`
    is a worker body and also the name of a method on the environment box; a
    call to `self.clear_logs()` is the second and must not be flagged. So a call
    counts only if the name was imported from `tasks` or the presenter, or is
    reached through one of those modules.
    """
    tree = ast.parse(source(path))
    functions, modules = _task_bindings(tree)
    if not functions and not modules:
        return []
    off_thread = OFF_THREAD | worker_bodies(path)
    found: list[str] = []
    scopes: list[tuple[str, ast.AST]] = [("<module>", tree)]
    scopes += [(node.name, node) for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name not in off_thread]
    for where, scope in scopes:
        for call in _calls_directly_in(scope):
            func = call.func
            if isinstance(func, ast.Name) and func.id in functions:
                name = func.id
            elif (isinstance(func, ast.Attribute) and func.attr in TASK_FUNCTIONS
                  and isinstance(func.value, ast.Name) and func.value.id in modules):
                name = func.attr
            else:
                continue
            # A call passed *to* CallableWorker is being scheduled, not made.
            scheduled = any(
                isinstance(outer, ast.Call)
                and getattr(outer.func, "id", "") == "CallableWorker"
                and call in ast.walk(outer)
                for outer in ast.walk(scope)
                if isinstance(outer, ast.Call)
            )
            if not scheduled:
                found.append(f"{where}:{name}")
    return found


def test_the_worker_bodies_are_where_the_rule_says():
    """`tasks.py` is real, is the exempt module, and holds what the presenter's
    own docstrings called worker-only."""
    assert TASKS.is_file()
    assert "tasks.py" in WORKER_ONLY
    for name in ("missing_paths", "mail_details", "decorate_results",
                 "record_open", "doctor_report", "install_package",
                 "read_index_summary", "resolve_open_path", "settings_labels"):
        assert name in TASK_FUNCTIONS, f"{name} is no longer a worker body in tasks.py"


def test_the_presenter_package_is_scanned_like_any_view():
    """It must not inherit `tasks.py`'s exemption by sharing a file name, and
    every one of its modules must be in the set the other checks parametrize."""
    modules = sorted(p for p in PRESENTER.glob("*.py") if p.name != "__init__.py")
    assert len(modules) > 5, "the presenter package has gone missing"
    assert not {p.name for p in modules} & WORKER_ONLY
    assert set(modules) <= set(MODULES)


def test_no_presenter_module_defines_a_worker_body():
    """A function `tasks.py` owns must not be defined a second time in the
    package, where the exemption would not reach it and a copy could drift."""
    for path in PRESENTER.glob("*.py"):
        defined = {node.name for node in ast.parse(source(path)).body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not defined & TASK_FUNCTIONS, (
            f"{path.name} defines {sorted(defined & TASK_FUNCTIONS)}, which tasks.py owns")


def test_the_presenter_package_never_imports_the_worker_bodies():
    """`tasks.py` imports the package, so the package importing `tasks` would be
    a cycle. Only `__init__.py` reaches it, lazily, to keep the old import path
    alive."""
    for path in PRESENTER.glob("*.py"):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(source(path))):
            if isinstance(node, ast.ImportFrom):
                reached = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                reached = [alias.name for alias in node.names]
            else:
                continue
            assert "app.ui.tasks" not in reached, (
                f"{path.name}:{node.lineno} imports app.ui.tasks - the package "
                f"holds decisions and tasks.py depends on it, not the reverse")


def test_every_worker_body_is_still_importable_from_the_presenter():
    """The move must not break `from app.ui.presenter import missing_paths` in
    the forty-odd places that say it."""
    import app.ui.presenter as presenter
    import app.ui.tasks as tasks

    for name in sorted(TASK_FUNCTIONS):
        assert getattr(presenter, name) is getattr(tasks, name), name
        assert name in dir(presenter), name


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_a_worker_body_is_only_called_through_a_worker(path):
    """A view calls `missing_paths` and its siblings from a function it has
    handed to a `CallableWorker` - never inline, where they would be one more
    thing on the interface thread."""
    if path.name == "tasks.py":
        pytest.skip("tasks.py bodies call each other on the same worker")
    offenders = _inline_task_calls(path)
    assert not offenders, (
        f"{path.name} calls worker bodies on the UI thread: {offenders} - "
        "hand the function to a CallableWorker")


# ---------------------------------------------------------------------------
# Long operations start a worker rather than running inline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("module", "method"), [
    ("widgets/environment_box.py", "run_doctor"),
    ("widgets/model_box.py", "refresh"),
    ("widgets/model_box.py", "test"),
    ("indexing_view.py", "refresh_totals"),
    ("files_view.py", "_run"),
    ("mail_view.py", "_run"),
    # Opening a result: `explorer /select,` is slow to start and the existence
    # check before it can block on a sleeping drive.
    # `_open_result` is a two-line adapter onto `_open_path`, which is where
    # the worker is. Naming the adapter here would assert against a docstring.
    ("shell.py", "_open_path"),
    # The Files tab called `open_in_explorer` inline - on a network share, a
    # frozen window - and did a synchronous store read per double-click for a
    # path the row already carried. Both were fixed for search results long
    # before, in `shell._open_path`, and regressed here.
    ("files_view.py", "_open"),
    # Work order 0h §3a: a result set can carry dozens of photos, and
    # decoding even one on the UI thread is the freeze non-negotiable #5
    # forbids - `_load_thumbnails` hands every one of them to its own worker.
    ("widgets/thumbnail_grid.py", "_load_thumbnails"),
    # 2026-10-04, code review: four more that ran on the interface thread -
    # the logs folder's mkdir and Explorer launch, the location dialog's
    # walk of the whole index and its per-keystroke stats, and the stat per
    # path dropped on the window.
    ("widgets/environment_box.py", "_open_folder"),
    ("widgets/index_flows.py", "_measure"),
    ("widgets/index_flows.py", "_check"),
    ("shell.py", "dropEvent"),
    # 2026-09-07 UI freeze: `resolve_for_run` falls through to `compute_
    # profile.detect()` on a cold hardware-profile cache, which shells out
    # to PowerShell twice with 10s/15s timeouts - all of it used to run
    # inline, on the UI thread, at the exact moment somebody clicked Start.
    # Moved to `IndexController` (work order 202626082352 section 7);
    # `MainWindow._start_indexing` is a forwarding method with nothing to prove.
    ("controllers/index_controller.py", "_start_indexing"),
])
def test_a_long_operation_starts_a_worker(module, method):
    r"""Asserted on the *worker*, not the result: the point is that the call
    returns immediately and the window keeps painting, not what it eventually
    produces.

    **Delegation counts, and is checked rather than assumed.** A method that
    hands the job to a named async helper is not doing it inline - but a grep
    for `Worker(` cannot see through the call, and a test that cannot tell
    delegation from blocking will be silenced rather than believed. So a call
    to a helper ending `_async` satisfies this only if that helper is itself
    worker-backed, which is verified below.
    """
    text = source(UI / module)
    body = text.split(f"def {method}(")[1].split("\n    def ")[0]
    delegates = re.findall(r"\b(\w+_async)\(", body)
    if delegates:
        workers = source(UI / "workers.py")
        for helper in set(delegates):
            assert f"def {helper}(" in workers, (
                f"{module}.{method} delegates to {helper}(), which is not in "
                f"workers.py - so nothing here can vouch for it")
            helper_body = workers.split(f"def {helper}(")[1].split("\ndef ")[0]
            # 2026-10-04, code review: one more hop is still delegation -
            # `open_async` wraps a path and hands it to `open_row_async`.
            for onward in set(re.findall(r"\b(\w+_async)\(", helper_body)) - {helper}:
                if f"def {onward}(" in workers:
                    helper_body += workers.split(f"def {onward}(")[1].split("\ndef ")[0]
            assert "Worker(" in helper_body or "run(" in helper_body, (
                f"{module}.{method} delegates to {helper}(), which does its "
                f"work inline - the delegation only moved the block")
        return
    assert "Worker(" in body or "run(" in body, (
        f"{module}.{method} appears to do its work inline"
    )


def test_the_troubleshooting_doc_calls_a_frozen_window_a_bug():
    """It used to document the freeze as expected behaviour - "wait ten
    seconds", naming the graph build and the environment check as legitimate
    causes. That is how a bug becomes a feature nobody fixes.

    Asserted on the substance rather than on a banned phrase: the replacement
    *quotes* the old wording while explaining why it went, and a substring check
    tripped over its own explanation. What matters is that the document now
    tells somebody to report it, not to wait it out.
    """
    doc = UI.parents[1] / "docs" / "TROUBLESHOOTING.md"
    if not doc.is_file():
        pytest.skip("no troubleshooting doc")
    text = doc.read_text(encoding="utf-8").lower()

    assert "should not happen" in text and "it is a bug" in text
    assert "please report it" in text
    # The specific promise the rest of this file enforces.
    assert "off the interface thread" in text


# ---------------------------------------------------------------------------
# Indexing must not compete with the work somebody is waiting for
# ---------------------------------------------------------------------------

def test_indexing_has_its_own_thread_pool():
    """**Being on a worker is not enough.**

    Indexing used `QThreadPool.globalInstance()` - the same pool as every
    search, filename lookup, mail filter and environment check. That pool has
    about one thread per core, and an index run holds a slot for *hours*, so a
    quarter of the interactive capacity was gone for the duration and a burst of
    typing could queue behind it.

    The work was always off the UI thread. It was competing with the work that
    has somebody waiting on it, which is the difference between "runs in the
    background" and "runs in the background and you can tell".
    """
    text = source(UI / "indexing_view.py")
    body = text.split("def __init__")[1].split("\n    def ")[0]
    # Code, not prose. Twice now a substring check has tripped over a comment
    # explaining what the code no longer does - the comment is the *reason* the
    # line changed, so banning the phrase bans its own explanation.
    code = "\n".join(line.split("#")[0] for line in body.splitlines())
    assert "QThreadPool.globalInstance()" not in code
    assert "setMaxThreadCount(1)" in code


def test_the_interactive_views_still_share_the_global_pool():
    """They should: those jobs are short, and a pool each would mean threads
    sitting idle for every tab nobody is using."""
    for name in ("files_view.py", "mail_view.py", "search_view.py"):
        assert "QThreadPool.globalInstance()" in source(UI / name)


def test_progress_reaches_the_screen_through_a_signal():
    """A background job that cannot report is indistinguishable from a hung
    one, which is how "is it doing anything?" becomes a support question."""
    text = source(UI / "indexing_view.py")
    assert "signals.progress.connect" in text


# ---------------------------------------------------------------------------
# A function-local import that shadows a module-level one
#
# **`UnboundLocalError: cannot access local variable 'QTimer'`, and the window
# would not open at all.** `MainWindow.__init__` used `QTimer` at line 207; four
# hundred lines later, still inside the same function, sat a redundant
# `from PySide6.QtCore import QTimer`. Python binds names per *function*, not per
# line, so that import made `QTimer` local for the whole of `__init__` and the
# earlier use referred to a variable that did not exist yet.
#
# The import had been harmless for months. It became fatal the moment somebody
# used the same name earlier in the function - which is the definition of a trap
# rather than a bug: correct today, and waiting.
#
# Five more were found in `app/ui` and two in `app/cli.py`, every one redundant.
# ---------------------------------------------------------------------------

def test_no_function_reimports_a_name_the_module_already_has():
    r"""Every one of these is an `UnboundLocalError` waiting for a caller.

    Local imports are fine and this project uses them deliberately - to break
    cycles, and to keep a heavy module off the start-up path. What is refused is
    a local import of a name the *module* already imports, which buys nothing
    and changes the scope of that name for the entire function.
    """
    import ast
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2] / "app"
    offences: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_level = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name in module_level:
                        offences.append(
                            f"{path.name}:{node.lineno} {function.name}() "
                            f"re-imports {name}")

    assert not offences, (
        "these local imports shadow a module-level name:\n  "
        + "\n  ".join(offences)
        + "\n\nThe module already imports it, so the local import buys nothing "
          "and makes the name local to the whole function - any use of it "
          "earlier in that function raises UnboundLocalError. Delete the local "
          "import."
    )


# ---------------------------------------------------------------------------
# The guard, checked against the two bugs it let through
#
# The 2026-08-26 review's closing point: "a rule worth stating is worth a test
# that fails when new code breaks it" - and this file passed while M11 and M13
# shipped. These run the checks against source written for the purpose, so the
# guard is tested rather than trusted.
# ---------------------------------------------------------------------------


def _offenders(path: Path) -> list[str]:
    """Store calls this module makes outside a worker. The check, as data."""
    tree = ast.parse(source(path))
    off_thread = OFF_THREAD | worker_bodies(path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in off_thread:
            continue
        for call in _calls_directly_in(node):
            if _is_store_call(call):
                if (call.func.attr in KEYED_WRITE
                        and (path.name, node.name) in KEYED_WRITE_ALLOWED_IN):
                    continue
                found.append(f"{node.name}:{call.func.attr}")
    return found


def _module(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_view.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_guard_catches_a_constructor_that_reads_the_store(tmp_path) -> None:
    """M13, exactly: a COUNT(*) in a view's `__init__`.

    `__init__` used to be in OFF_THREAD, which exempted every constructor in
    the package - so this shipped, on the UI thread, inside MainWindow's own
    constructor, against a rule that window's docstring states.
    """
    path = _module(tmp_path, "class V:\n"
                             "    def __init__(self, store):\n"
                             "        self.n = store.count_searches()\n")
    assert _offenders(path) == ["__init__:count_searches"]


def test_the_guard_still_allows_a_keyed_setting_in_a_constructor(tmp_path) -> None:
    """The reason `__init__` was exempted in the first place is real."""
    path = _module(tmp_path, "class V:\n"
                             "    def __init__(self, store):\n"
                             "        self.mode = store.get_state('ui:pst', 'auto')\n")
    assert _offenders(path) == []


def test_the_guard_catches_a_keyed_write_in_a_slot(tmp_path) -> None:
    """Bug 3a, exactly: `_remember_page` saved `ui:page` with `set_state` on
    the UI thread, and waited for the indexer's batch to do it."""
    path = _module(tmp_path, "class W:\n"
                             "    def _remember_page(self, index):\n"
                             "        self._store.set_state('ui:page', 'Files')\n"
                             "    def _tray(self):\n"
                             "        self._w._store.set_states({'ui:tray': 'on'})\n")
    assert sorted(_offenders(path)) == ["_remember_page:set_state", "_tray:set_states"]


def test_the_guard_allows_a_queued_keyed_write(tmp_path) -> None:
    """`state_writes.save_state` is how a slot saves a choice now."""
    path = _module(tmp_path, "class W:\n"
                             "    def _remember_page(self, index):\n"
                             "        save_state(self._store, 'ui:page', 'Files')\n")
    assert _offenders(path) == []


def test_the_close_allowance_is_for_the_main_window_only(tmp_path) -> None:
    """`closeEvent` in `shell.py` may write synchronously; one anywhere else
    - a pop-out, a dialog - still has an event loop to hand a worker to."""
    body = ("class W:\n"
            "    def closeEvent(self, event):\n"
            "        self._store.set_states({'ui:window_geometry': 'x'})\n")
    shell = tmp_path / "shell.py"
    shell.write_text(body, encoding="utf-8")
    assert _offenders(shell) == []
    assert _offenders(_module(tmp_path, body)) == ["closeEvent:set_states"]


def test_the_guard_catches_a_store_method_nobody_listed(tmp_path) -> None:
    """`stats()` was not in the hand-written nine, so three views called three
    COUNT(*) on the UI thread and this file said nothing."""
    path = _module(tmp_path, "class V:\n"
                             "    def _draw(self, store):\n"
                             "        return store.stats()\n")
    assert _offenders(path) == ["_draw:stats"]


def test_the_guard_does_not_flag_a_worker_body(tmp_path) -> None:
    """A nested function handed to a CallableWorker runs somewhere else."""
    path = _module(tmp_path, "class V:\n"
                             "    def _go(self, store):\n"
                             "        def work():\n"
                             "            return store.stats()\n"
                             "        run(pool, CallableWorker(work))\n")
    assert _offenders(path) == []


def test_the_guard_does_not_flag_a_same_named_method_on_something_else(
    tmp_path
) -> None:
    """`self.close()` on a widget is not `store.close()`.

    The first version of the receiver check matched on method name alone and
    flagged Qt signals, timers and file handles - a guard that cries wolf gets
    an exemption list bolted to it and then guards nothing.
    """
    path = _module(tmp_path, "class V:\n"
                             "    def _shut(self):\n"
                             "        self.close()\n"
                             "        self.timer.stats()\n")
    assert _offenders(path) == []


def test_the_guard_catches_a_view_calling_a_worker_body_inline(tmp_path) -> None:
    """The boundary, broken: a slot that calls `missing_paths` itself."""
    path = _module(tmp_path, "from app.ui.presenter import missing_paths\n"
                             "class V:\n"
                             "    def _paint(self, rows):\n"
                             "        return missing_paths(rows)\n")
    assert _inline_task_calls(path) == ["_paint:missing_paths"]


def test_the_guard_catches_it_through_the_tasks_module_too(tmp_path) -> None:
    path = _module(tmp_path, "from app.ui import tasks\n"
                             "class V:\n"
                             "    def _go(self):\n"
                             "        return tasks.doctor_report()\n")
    assert _inline_task_calls(path) == ["_go:doctor_report"]


def test_the_guard_allows_a_worker_body_run_by_a_worker(tmp_path) -> None:
    """Handed over by name, called inside a nested function that is handed over,
    and called inside the arguments of the `CallableWorker` itself."""
    path = _module(tmp_path, "from app.ui.tasks import missing_paths, mail_details\n"
                             "class V:\n"
                             "    def _go(self, store, rows):\n"
                             "        run(pool, CallableWorker(missing_paths, rows))\n"
                             "        def work():\n"
                             "            return mail_details(store, rows)\n"
                             "        run(pool, CallableWorker(work))\n")
    assert _inline_task_calls(path) == []


def test_the_guard_does_not_flag_a_method_that_shares_a_worker_bodys_name(
    tmp_path
) -> None:
    """`EnvironmentBox.clear_logs` starts a worker for `tasks.clear_logs`. The
    method and the function are two things with one name."""
    path = _module(tmp_path, "from app.ui.presenter import clear_logs\n"
                             "class V:\n"
                             "    def _button(self):\n"
                             "        self.clear_logs()\n"
                             "    def clear_logs(self):\n"
                             "        run(pool, CallableWorker(clear_logs, self.folder))\n")
    assert _inline_task_calls(path) == []


def test_the_guard_ignores_a_name_that_never_came_from_tasks(tmp_path) -> None:
    path = _module(tmp_path, "def missing_paths(rows):\n"
                             "    return rows\n"
                             "def _go(rows):\n"
                             "    return missing_paths(rows)\n")
    assert _inline_task_calls(path) == []


def test_an_image_is_never_decoded_from_a_path_in_a_view():
    """M11: `QPixmap(path)` reads and decodes on the calling thread.

    The pane may build a QPixmap from a QImage a worker decoded - that is the
    one thing QPixmap has to do on the UI thread. What it may not do is hand
    QPixmap a filename.
    """
    for path in MODULES:
        for call in calls_in(path):
            if called_name(call) != "QPixmap" or not call.args:
                continue
            argument = call.args[0]
            assert not isinstance(argument, ast.Constant), (
                f"{path.name}:{call.lineno} builds a QPixmap from a literal path"
            )
            name = getattr(argument, "id", "") or getattr(argument, "attr", "")
            assert "path" not in name.lower() and "file" not in name.lower(), (
                f"{path.name}:{call.lineno} builds a QPixmap from {name} - if "
                f"that is a path it decodes on this thread. Decode to a QImage "
                f"on a worker and use QPixmap.fromImage()."
            )
