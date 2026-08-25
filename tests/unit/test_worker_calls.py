"""A worker must be handed arguments the function it calls will accept.

Layer: L5

Found by reading, not by running: `record_open` takes three arguments and was
being given four. Every click on a result raised `TypeError` inside the worker,
which caught it - as it must, or a failed background task takes the window with
it - and `record_open` swallows failures too, because a click is never worth
blocking on. **Two correct safety nets in a row turned a wrong call into
silence**, and not one result-open was ever recorded. Everything Layer 10 is
meant to learn from is derived from those rows, and the table was empty by
construction for the life of the feature.

Neither net should be removed; both are right. What was missing is the check
that the call was ever plausible, which the interpreter cannot do for a callable
passed by reference and no test could see because nothing failed.

**Static, and deliberately so.** Executing the click path needs a window, a
store and a search; matching a call against a signature needs neither, so this
runs everywhere the suite runs and costs milliseconds.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"

#: Constructors whose first argument is the callable and whose remaining
#: positional arguments are forwarded to it.
FORWARDING = {"CallableWorker"}


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _import_sources(path: Path) -> dict[str, str]:
    """`{imported name: module it came from}` for one file.

    **The import is followed rather than the calling module imported.** Every
    view imports Qt at module scope, so importing one needs a Qt that can load -
    which a headless machine may not have. The functions handed to workers live
    in `presenter.py`, which imports no Qt at all and is kept that way by its
    own test, so resolving through the import statement lets this run anywhere.
    """
    sources: dict[str, str] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                sources[alias.asname or alias.name] = node.module
    return sources


def _resolve(sources: dict[str, str], symbol: str):
    """The function a name refers to, or None when it cannot be resolved.

    Only names imported into the calling module are checked. A locally built
    lambda or a method on an object is out of scope - the bug this exists for
    was a module-level function passed by name, which is also the common case.
    """
    module_name = sources.get(symbol)
    if not module_name:
        return None
    try:
        module = importlib.import_module(module_name)
    except Exception:                            # noqa: BLE001 - Qt may be absent
        return None
    target = getattr(module, symbol, None)
    return target if inspect.isfunction(target) else None


def _forwarded_calls():
    """Every `CallableWorker(fn, *args)` in `app/ui`, as (file, line, fn, argc)."""
    found = []
    for path in sorted(UI.rglob("*.py")):
        sources = _import_sources(path)
        for node in _calls(path):
            if _name_of(node.func) not in FORWARDING or not node.args:
                continue
            target = _name_of(node.args[0])
            if not target:
                continue
            found.append((path, node.lineno, sources, target, node.args[1:], node.keywords))
    return found


CASES = _forwarded_calls()


def test_there_are_worker_calls_to_check():
    """A refactor that renames the worker must not silently empty this test."""
    assert CASES, "no CallableWorker(...) calls found - has it been renamed?"


@pytest.mark.parametrize(
    "path,line,sources,target,args,keywords",
    CASES,
    ids=[f"{p.name}:{ln}:{t}" for p, ln, _s, t, _a, _k in CASES],
)
def test_a_worker_is_called_with_arguments_it_accepts(
    path, line, sources, target, args, keywords
):
    function = _resolve(sources, target)
    if function is None:
        pytest.skip(f"{target} could not be resolved to a module-level function")

    #: `component=` is consumed by the worker itself and never forwarded.
    forwarded_keywords = {
        kw.arg for kw in keywords if kw.arg and kw.arg != "component"
    }
    signature = inspect.signature(function)

    try:
        signature.bind_partial(*(None,) * len(args), **dict.fromkeys(forwarded_keywords))
    except TypeError as exc:
        pytest.fail(
            f"{path.name}:{line} runs {target} on a worker with "
            f"{len(args)} positional argument(s), which it will not accept.\n"
            f"  {target}{signature}\n"
            f"  {exc}\n\n"
            "The worker catches this and the task vanishes, so nothing fails "
            "visibly - it simply never happens."
        )
