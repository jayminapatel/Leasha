"""Every declared setting has a control somebody can actually reach.

Layer: L5

`test_settings_registry.py` proves the registry and `config.py` agree. That is
half the rule: it catches a key with no *declaration*, and cannot catch a
declaration with no *control* - a setting that exists, is documented, is read at
startup, and has nothing on screen that changes it.

That failure has already happened twice in this project. `rerank_toggled` and
`cloud_toggled` were both emitted and connected to nothing, and
`docs/REVIEW-2026-08-25.md` found a search cache declared, described in a
docstring, and passed to no constructor for the life of the project. A thing can
be designed, documented, believed, and never wired up.

**The convention this test enforces:** a control for setting `KEY` calls
`setObjectName("KEY")`. One line per control, it is what a runtime test would
look the widget up by, and - crucially - it is greppable, so this check runs on
a machine with no display, which is where the suite mostly runs.

It is a static check, so it proves a control is *built*, not that it is wired to
the writer. `test_settings_registry.py` covers the round trip; between them the
gap is small and named rather than open and unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core import settings_registry as reg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI = PROJECT_ROOT / "app" / "ui"

#: Which module builds each surface. Declared here rather than in the registry
#: so that `settings_registry.py` stays importable without Qt and free of any
#: knowledge of the UI - it is a declaration of intent, not a wiring diagram.
SURFACE_MODULES = {
    "settings.search": ("settings_view.py", "widgets/search_box.py"),
    # `long_run_box` too: the two settings that only matter at a terabyte -
    # which pass to run, and how long an archive is trusted - are their own
    # group inside the Indexing panel, because `indexing_settings.py` is under
    # the 250-line guard and because neither is worth a thought at 100GB.
    "settings.indexing": ("indexing_settings.py", "widgets/long_run_box.py"),
    "settings.reading": ("settings_view.py", "widgets/file_types.py"),
    # `storage_box` too: EMBED_MODEL and EMBED_DIM are Models settings whose
    # flow lives with the index location it invalidates.
    "settings.models": ("settings_view.py", "widgets/model_box.py",
                        "widgets/storage_box.py"),
    "settings.storage": ("settings_view.py", "widgets/storage_box.py"),
}

#: Settings rendered as a deliberate flow rather than a plain control, because
#: changing them changes what the index *is*. They still need something on
#: screen, so the flow's own name is what is looked for instead of the key.
#:
#: Not an exemption: a flow that nothing invokes is the same bug in a better
#: costume, and `test_a_flow_is_actually_invoked` asserts the name appears.
FLOWS = {setting.key: setting.flow for setting in reg.SETTINGS if setting.flow}


def _object_names_in(path: Path) -> set[str]:
    """Every string passed to `setObjectName(...)` in one module."""
    if not path.is_file():
        return set()
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name != "setObjectName":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.add(argument.value)
    return found


def _names_for(surface: str) -> set[str]:
    names: set[str] = set()
    for relative in SURFACE_MODULES.get(surface, ()):
        names |= _object_names_in(UI / relative)
    return names


def test_every_surface_names_a_module_that_exists():
    """A surface with no module is a group of settings with nowhere to live."""
    for surface in reg.SURFACES:
        modules = SURFACE_MODULES.get(surface)
        assert modules, f"{surface} is declared but no module builds it"
        for relative in modules:
            assert (UI / relative).is_file(), f"{surface} names a missing {relative}"


@pytest.mark.parametrize(
    "setting",
    [s for s in reg.SETTINGS if not s.flow],
    ids=lambda s: s.key,
)
def test_every_plain_setting_has_a_control(setting):
    """The test that catches a setting wired to nothing.

    Give the widget the setting's key as its object name:

        self.workers = QSpinBox()
        self.workers.setObjectName("INDEX_WORKERS")

    One line, and it is what makes the control findable by a runtime test and
    by this one.
    """
    names = _names_for(setting.surface)
    assert setting.key in names, (
        f"{setting.key} is declared in the registry but no control on "
        f"{setting.surface} claims it.\n"
        f"  Add the control in {', '.join(SURFACE_MODULES[setting.surface])} "
        f"and call setObjectName({setting.key!r}) on it.\n"
        f"  Label: {setting.label!r} ({setting.kind})\n"
        f"  If nobody will ever change it, delete it from the registry and make "
        f"it a fixed constant with a comment - that is the other half of the rule."
    )


@pytest.mark.parametrize(
    "setting",
    [s for s in reg.SETTINGS if s.flow],
    ids=lambda s: s.key,
)
def test_a_flow_is_actually_invoked(setting):
    """A destructive setting needs a flow on screen, not merely a declared one.

    Looked up by the flow's name so that a button reading "Move index..." can
    be found without this test knowing what it is labelled.
    """
    names = _names_for(setting.surface)
    assert setting.flow in names or setting.key in names, (
        f"{setting.key} declares the {setting.flow!r} flow, and nothing on "
        f"{setting.surface} offers it.\n"
        f"  Add the control that starts it and call "
        f"setObjectName({setting.flow!r}) on it.\n"
        f"  Changing this setting {setting.help[:80]}..."
    )


#: Settings that reach `.env` by a route other than a panel emitting their key.
#:
#: Each is written by a flow that names it explicitly - see `shell.py`. Listed
#: rather than skipped, so a key added here has to be a decision.
WRITTEN_BY_A_FLOW = {
    "DATA_PATH": "written by the index-location flow",
    "EMBED_MODEL": "written by the rebuild-vectors flow",
    "EMBED_DIM": "set by the meaning model, never typed",
    "RERANK_ENABLED": "applied live and stored as window state",
}


def _stored_names() -> set[str]:
    """Every string that `app/ui` uses as a name it is **saving** under.

    Dictionary keys and the first argument to `set_state`, and nothing else.
    That is the whole point of using `ast` here rather than searching the text:
    `{"RERANK_TOP_N": value}` is a value being written, while
    `getattr(settings, "rerank_top_n", 30)` is the same setting being read, and
    a check that cannot tell them apart passes for a control that only ever
    loads its own default.
    """
    found: set[str] = set()
    for path in sorted(UI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.add(key.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"set_state", "setText"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.add(first.value)
    return found


def test_every_control_writes_its_setting_somewhere():
    """A control that persists nothing is a control that does nothing.

    **This is the half `test_every_plain_setting_has_a_control` cannot see.**
    That one proves a widget was built; it says so in its own docstring. Five
    controls were then added - the two rerank numbers, the rerank model, the
    disk floor and the free-space requirement - each with an object name, each
    passing that test, and **not one of them saved anything**. `SearchBox` even
    had a `values()` method that nothing called.

    Which is precisely U6 in `docs/REVIEW-2026-08-25.md`, reintroduced in the
    commit that fixed U6. A rule enforced at one end only gets broken at the
    other.

    Searching all of `app/ui` rather than the setting's own panel, because the
    panel emits and the window writes, and the two are meant to be separate.
    """
    stored = _stored_names()

    def is_written(key: str) -> bool:
        # Three spellings, because there are two stores. `.env` uses the key as
        # declared; `index_state` uses `ui:` plus the lower-cased name, and the
        # panels build their dictionaries from the `Settings` attribute name.
        # All three are a value being *saved*; none of them is a value being
        # read, which is the distinction that makes this test worth having.
        return any(
            candidate in stored
            for candidate in (key, key.lower(), f"ui:{key.lower()}")
        )

    missing = sorted(
        s.key for s in reg.SETTINGS
        if not is_written(s.key) and s.key not in WRITTEN_BY_A_FLOW
    )
    assert not missing, (
        "these settings have a control that saves nothing:\n  "
        + "\n  ".join(missing)
        + "\n\nThe key must appear somewhere other than its own setObjectName - "
          "in the dict a panel emits, or in the apply_values call that writes "
          "it. A control that changes nothing is the bug U6 was about."
    )


def test_the_flow_exemptions_each_say_which_flow():
    for key, reason in WRITTEN_BY_A_FLOW.items():
        assert len(reason) > 15, f"{key} is exempt without saying how it is written"


def test_no_control_claims_a_key_the_registry_does_not_declare():
    """The reverse: a widget named after a setting that no longer exists is a
    control that silently stopped doing anything when the setting was removed."""
    declared = set(reg.keys()) | set(FLOWS.values())
    for surface, modules in SURFACE_MODULES.items():
        for relative in modules:
            for name in _object_names_in(UI / relative):
                # Only names shaped like a settings key are ours to police;
                # object names are used for styling and lookup elsewhere too.
                if name.isupper() and "_" in name:
                    assert name in declared, (
                        f"{relative} builds a control named {name!r}, which is "
                        f"not in the registry - it changes nothing"
                    )
