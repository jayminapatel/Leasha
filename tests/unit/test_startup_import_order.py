"""The <300ms import discipline: what `app.main` imports before splash-show.

Layer: L5

Work order §0.7: "splash visible <300ms from process start (before the lock
wait - only stdlib+Qt imports may precede it)". §4's own checklist names this
directly: "startup: a stub-slowed stage still shows the splash within its
budget (the <300ms import discipline as a test on what `main` imports before
splash-show)."

This is a structural check, not a timing one - timing is flaky in CI/sandbox
environments, and a slow-but-passing timer proves nothing about *why* it was
slow. What can be checked reliably, everywhere, is what actually gets
imported, in source order, before the statement that constructs
`SplashScreen()` - which is the literal thing §0.7 promises.

Layer: L5
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[2] / "app" / "main.py"

# First-party packages allowed to precede splash-show: the lightweight
# app.core.* modules main.py's own top-of-file imports already use (branding,
# errors, logging, config, runlog, run_lock, single_instance, index_move -
# none of which import numpy, onnxruntime, fastembed or lance), plus the
# splash/tray/startup-timing modules the splash sequence itself constructs.
# Everything else under app.index, app.search, app.storage and app.ui.shell
# is exactly the heavier subsystem this budget exists to keep out.
_ALLOWED_FIRST_PARTY_PREFIXES = (
    "app.core",
    "app.ui.splash",
    "app.ui.tray",
    "app.ui.startup_timing",
    "app.extract.ocr",  # device configuration only; see main.py's own comment
)


def _module_root(name: str) -> str:
    return name.split(".")[0]


def _is_allowed(module_name: str) -> bool:
    """True unless `module_name` names a disallowed first-party subsystem.

    Anything that is not rooted at `app` (stdlib, PyQt6, third-party) is out
    of scope for this check - it exists to keep the *heavy first-party*
    subsystems (index/search/storage/the whole UI shell) off the path to
    splash-show, not to police every import in the file.
    """
    if not module_name or _module_root(module_name) != "app":
        return True
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in _ALLOWED_FIRST_PARTY_PREFIXES
    )


def _imports_in(nodes: ast.AST | list[ast.stmt]) -> list[tuple[int, str]]:
    """(lineno, dotted module name) for every Import/ImportFrom under `nodes`."""
    found: list[tuple[int, str]] = []
    walk_target = nodes if isinstance(nodes, ast.AST) else ast.Module(body=list(nodes), type_ignores=[])
    for node in ast.walk(walk_target):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, node.module or ""))
    return found


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"could not find function {name!r} in {MAIN_PY}")


def _find_splash_construction_line(run_window: ast.FunctionDef) -> int:
    """The line of the `SplashScreen(...)` call inside `_run_window`."""
    for node in ast.walk(run_window):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SplashScreen"):
            return node.lineno
    raise AssertionError(
        "could not find a SplashScreen() construction in _run_window - "
        "has splash construction moved or been renamed? This test's whole "
        "premise depends on finding that exact call."
    )


class TestStartupImportOrder:
    """§0.7 / §4: only stdlib+Qt (+ the small app.core/splash family) precede
    splash-show - never app.index, app.search, app.storage or app.ui.shell.
    """

    def test_no_heavy_app_import_precedes_splash_construction(self) -> None:
        """Everything `app.main` imports before `SplashScreen()` is light.

        Checked in actual execution order for the normal startup path:
        module-level imports (which run unconditionally the moment
        `app.main` is imported, before `main()` is ever called), then
        `main()`'s own top-of-function imports, then `_run_window()`'s
        imports up to - but not including - the line that constructs
        `SplashScreen()`. Imports inside other functions (`_fatal`,
        `_make_ctrl_c_work`, and so on) are deliberately excluded: those
        function bodies do not run on the normal startup path before splash-
        show, only on their own error/utility paths.
        """
        source = MAIN_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MAIN_PY))

        run_window = _find_function(tree, "_run_window")
        main_func = _find_function(tree, "main")
        splash_line = _find_splash_construction_line(run_window)

        module_level_imports = [
            (node.lineno, alias.name)
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            (node.lineno, node.module or "")
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
        ]

        main_imports = _imports_in(main_func)
        run_window_imports = [
            (lineno, name) for lineno, name in _imports_in(run_window)
            if lineno < splash_line
        ]

        all_checked = module_level_imports + main_imports + run_window_imports
        assert all_checked, "expected to find at least the module's own top-level imports"

        violations = [
            (lineno, name) for lineno, name in all_checked
            if not _is_allowed(name)
        ]

        assert not violations, (
            "app.main imports the following heavy first-party module(s) "
            f"before SplashScreen() is constructed (line {splash_line} of "
            f"{MAIN_PY.name}), violating the <300ms import discipline in "
            "work order §0.7 (\"only stdlib+Qt imports may precede it\"): "
            f"{violations}. Move these imports to after splash.show()."
        )

    def test_splash_construction_line_is_found(self) -> None:
        """Guard the guard: if this ever stops finding the call, the main
        test above would vacuously pass with an empty search space instead
        of failing loudly - this asserts the line lookup itself works.
        """
        source = MAIN_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MAIN_PY))
        run_window = _find_function(tree, "_run_window")

        line = _find_splash_construction_line(run_window)

        assert line > run_window.lineno
