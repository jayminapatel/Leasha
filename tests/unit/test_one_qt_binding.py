"""Leasha speaks PySide6, and only PySide6.

Order 202626270238, 2026-10-05.

The laptop's environment still has PyQt6 installed beside PySide6 after the
move, so a stray `from PyQt6 ...` would import and run there, and break only on
a clean install - GitHub's, or the first person given an installer. This reads
the source instead of trusting the environment. `archive/` is old code nobody
runs and is left as it was.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OTHER_BINDINGS = {"PyQt6", "PyQt5", "PySide2", "sip"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    return found


def test_no_code_imports_another_qt_binding() -> None:
    files = [p for top in ("app", "tests", "tools", "scripts")
             for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts]
    files += [ROOT / "doctor.py", ROOT / "suggest.py"]
    assert len(files) > 300, "the walk found almost nothing"
    wrong = {str(p.relative_to(ROOT)): sorted(_imports(p) & OTHER_BINDINGS)
             for p in files if p.is_file() and _imports(p) & OTHER_BINDINGS}
    assert not wrong, f"another Qt binding imported: {wrong}"


def test_requirements_name_pyside6_and_not_pyqt() -> None:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lines = [line.split("#")[0].strip() for line in text.splitlines()]
    assert any(line.startswith("PySide6==") for line in lines)
    assert not any(line.lower().startswith(("pyqt", "pyside2")) for line in lines)
