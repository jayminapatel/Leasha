"""Chat is opt-in and separate: the engine layer's boundaries, checked statically.

Layer: L8b. Non-negotiables 1 (no service in the search hot path), 2 (an LLM only in the
chat path) and the layering rule in `docs/PROJECT_INSTRUCTIONS.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core import settings_registry as reg

ROOT = Path(__file__).resolve().parents[2]
CHAT = sorted((ROOT / "app" / "chat").glob("*.py"))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_chat_package_exists_and_has_every_module_the_docs_promise():
    names = {p.stem for p in CHAT}
    assert {"engine", "router", "plan", "context", "aggregate", "absence", "verify", "prompts",
            "llm", "roles", "config", "sessions", "evaluate", "testing", "types", "text"} <= names


@pytest.mark.parametrize("path", CHAT, ids=lambda p: p.name)
def test_chat_never_imports_the_ui_or_qt(path):
    """`app/chat/` is Qt-free and never imports `app/ui/`: the tab depends on it, not the
    other way round."""
    bad = {m for m in _imports(path)
           if m == "app.ui" or m.startswith(("app.ui.", "PyQt", "PySide", "qtpy"))}
    assert not bad, f"{path.name} imports {sorted(bad)}"


def test_search_never_reaches_chat_or_the_model_layer():
    """Search works with Ollama stopped because nothing under `app/search/` can call the
    chat engine - and only the translator may import `app.llm` (`test_translate.py`)."""
    offenders = []
    for path in (ROOT / "app" / "search").glob("*.py"):
        for module in _imports(path):
            if module == "app.chat" or module.startswith("app.chat."):
                offenders.append((path.name, module))
    assert not offenders, offenders


def test_the_index_layers_never_reach_chat():
    for layer in ("storage", "index", "extract", "core"):
        for path in (ROOT / "app" / layer).glob("*.py"):
            bad = {m for m in _imports(path) if m == "app.chat" or m.startswith("app.chat.")}
            assert not bad, f"{path} imports {sorted(bad)}"


def test_only_the_llm_seam_speaks_http():
    """One module talks to the network; every other chat module is pure or injected."""
    for path in CHAT:
        source = path.read_text(encoding="utf-8")
        if path.name == "llm.py":
            continue
        assert "import requests" not in source and "urllib" not in source, path.name


# --------------------------------------------------------------------------- the settings

CHAT_KEYS = ("CHAT_MODEL", "CHAT_ROUTER_MODEL", "CHAT_PLANNER_MODEL", "CHAT_MAX_ROUNDS",
             "CHAT_VERIFY_STRICTNESS")


def test_every_chat_tunable_is_declared_where_the_ui_will_find_it():
    """Non-negotiable 11. The engine declares these; the tab's author builds the controls
    (`setObjectName("CHAT_...")` on `settings.models`) - see the hand-off in the report."""
    declared = {s.key: s for s in reg.SETTINGS}
    for key in CHAT_KEYS:
        assert key in declared, key
        assert declared[key].surface == "settings.models" and declared[key].group == "Models"
        assert declared[key].help.strip()
    assert declared["CHAT_MAX_ROUNDS"].maximum == 3                    # the order's bound, in the registry too
    assert declared["CHAT_VERIFY_STRICTNESS"].default == 70
    assert declared["CHAT_MODEL"].default == "" and declared["CHAT_ROUTER_MODEL"].default == ""
