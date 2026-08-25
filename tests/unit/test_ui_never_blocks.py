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
"""

from __future__ import annotations

import ast
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


# ---------------------------------------------------------------------------
# processEvents
# ---------------------------------------------------------------------------

#: The one place it is allowed, and why.
#:
#: `closeEvent` waits for background threads to finish before the stores are
#: torn out from under them. There is no event loop to return to - the window is
#: closing - so the choice is between pumping events and freezing during the one
#: operation nobody will wait out. Everywhere else it is a symptom of work on
#: the wrong thread, and it reenters the event loop in ways that produce bugs
#: nobody can reproduce.
PROCESS_EVENTS_ALLOWED_IN = {"closeEvent", "_drain_workers", "_wait_for_workers"}


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
#: window for two minutes; `sleep` has no legitimate use in a paint path.
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


#: Modules that exist *to be called from a worker*. `presenter.py` - it
#: holds `doctor_report`, which runs a subprocess deliberately. Blocking is
#: correct there and the guarantee is enforced at the call site instead, by
#: `test_a_long_operation_starts_a_worker`.
WORKER_ONLY = {"presenter.py", "workers.py"}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_nothing_blocks_the_ui_thread(path):
    if path.name in WORKER_ONLY:
        pytest.skip(f"{path.name} is called from workers by design")
    for node in calls_in(path):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            assert pair not in BLOCKING_ATTRS, (
                f"{path.name}:{node.lineno} calls {pair[0]}.{pair[1]} on the UI "
                "thread. Put it in a CallableWorker."
            )
        if called_name(node) in BLOCKING and not isinstance(func, ast.Name):
            owner = getattr(getattr(func, "value", None), "id", "")
            assert owner not in ("time", "subprocess"), (
                f"{path.name}:{node.lineno} blocks the UI thread"
            )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_ui_module_waits_on_a_thread_pool_except_when_closing(path):
    """`waitForDone` on the UI thread is a freeze by another name."""
    for node in calls_in(path):
        if called_name(node) != "waitForDone":
            continue
        where = enclosing_function(path, node.lineno)
        assert where in PROCESS_EVENTS_ALLOWED_IN, (
            f"{path.name}:{node.lineno} waits for the thread pool inside "
            f"{where!r}, which freezes the window."
        )


# ---------------------------------------------------------------------------
# The presenter stays Qt-free
# ---------------------------------------------------------------------------

def test_the_presenter_still_does_not_import_qt():
    """The whole reason the logic is testable. One import here and half of this
    file's guarantees become unverifiable."""
    text = source(UI / "presenter.py")
    assert "PyQt6" not in text
    assert "from PyQt" not in text


def test_the_model_and_view_option_rules_are_qt_free_too():
    """`view_options.py` and `llm/models.py` hold decisions with a Qt half
    bolted on the bottom. The decisions must stay reachable without a display,
    or they stop being tested."""
    options = source(UI / "view_options.py")
    top = options.split("# The Qt half")[0]
    assert "PyQt6" not in top, "Qt reached the decision half of view_options"

    models = (UI.parents[0] / "llm" / "models.py").read_text(encoding="utf-8")
    assert "PyQt6" not in models


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
    ("shell.py", "_open_result"),
])
def test_a_long_operation_starts_a_worker(module, method):
    """Asserted on the *worker*, not the result: the point is that the call
    returns immediately and the window keeps painting, not what it eventually
    produces."""
    text = source(UI / module)
    body = text.split(f"def {method}(")[1].split("\n    def ")[0]
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
