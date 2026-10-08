"""Nothing below `app/ui` imports from it, and the CLI never needs Qt.

Layer: L0

The rule is in `docs/PROJECT_INSTRUCTIONS.md` and the leasha skill: `app/ui/`
may call the layers below it; nothing below may import from `app/ui/`. Until
the 2026-10-08 review three places did - `app/serve/mcp.py` for three row
helpers, `app/index/pipeline_bench.py` for the lag monitor, and
`app/core/osbridge/startmenu.py` for two constants - and nothing said so,
because the only layering test covered `app/chat`. The helpers moved down;
this test keeps them there.

`app/cli` is the one documented exception: it imports `app.ui.presenter` and
`app.ui.tasks` by the owner's 2026-10-04 decision ("same code"), both Qt-free
by design. The letter of the rule bends for it; the spirit - no Qt in a CLI
process - is what the last test holds, by importing every CLI module with
PySide6 made unimportable.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: Every package below the UI. `shell` is a terminal front end over the
#: presenter and `main.py` is the composition root; both are allowed to know
#: the UI exists, so neither is here.
BELOW_THE_UI = ("core", "storage", "extract", "ort", "index", "search", "chat",
                "serve", "reports", "llm")

#: What `app/cli` may take from `app/ui`: the Qt-free decision layer, nothing else.
CLI_ALLOWED = ("app.ui.presenter", "app.ui.tasks")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _ui_imports(path: Path) -> set[str]:
    return {m for m in _imports(path) if m == "app.ui" or m.startswith("app.ui.")}


@pytest.mark.parametrize("package", BELOW_THE_UI)
def test_nothing_below_the_ui_imports_from_it(package):
    offenders = {str(p.relative_to(ROOT)): sorted(_ui_imports(p))
                 for p in (APP / package).rglob("*.py") if _ui_imports(p)}
    assert not offenders, offenders


def test_the_three_moves_of_2026_10_08_stayed_moved():
    """The helpers the MCP server and the bench needed live below the UI now;
    the UI modules re-export them so no caller changed."""
    from app.core import lag_monitor as core_monitor
    from app.core.row_facts import join_chunks, shown_date_ns
    from app.search.marks import file_row_context
    from app.ui import lag_monitor as ui_monitor
    from app.ui import preview_loader, tasks
    from app.ui.presenter import facts

    assert facts.shown_date_ns is shown_date_ns
    assert preview_loader.join_chunks is join_chunks
    assert tasks.file_row_context is file_row_context
    assert ui_monitor.LagMonitor is core_monitor.LagMonitor
    assert ui_monitor.install is core_monitor.install


def test_the_cli_takes_only_the_qt_free_decision_layer_from_the_ui():
    """The owner's 2026-10-04 exception, held to its own terms."""
    offenders = {}
    for path in (APP / "cli").glob("*.py"):
        bad = {m for m in _ui_imports(path) if not m.startswith(CLI_ALLOWED)}
        if bad:
            offenders[path.name] = sorted(bad)
    assert not offenders, offenders


def test_every_cli_module_imports_without_qt():
    """`app.cli` must run on a machine, or in a process, with no PySide6 at all:
    `leasha-cli.exe`, the installer's model step, a scheduled run. Importing
    every CLI module with PySide6 made unimportable is the only test that can
    say the presenter exception has not let Qt in through a side door."""
    modules = sorted(f"app.cli.{p.stem}" for p in (APP / "cli").glob("*.py")
                     if p.stem != "__init__")
    code = (
        "import sys\n"
        "for name in ('PySide6', 'PyQt6', 'PyQt5', 'PySide2'):\n"
        "    sys.modules[name] = None\n"      # makes `import PySide6` raise ImportError
        "import importlib\n"
        "import app.cli\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                            text=True, timeout=300)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-2000:]
