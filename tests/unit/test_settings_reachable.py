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
#:
#: **`indexing.schedule` and `indexing.tuning` renamed 2026-09-05**, pages-reorg
#: order §1d - the surfaces formerly `settings.indexing`/`settings.tuning`.
#: The files did not move: both were always the Indexing page's own controls,
#: never the Settings page's, and the rename only makes that true in the name
#: as well as in the file list, now that the Indexing page has named
#: categories of its own (`app/ui/widgets/category_nav.py`) to say it against.
SURFACE_MODULES = {
    # `search_behaviour_box` too: the six behaviours from the search-experience
    # order's §1 are their own group, because each one needs the effect grid
    # beside it to be honest about what it does per tab.
    # `editor_box` too, and kept apart from `search_behaviour_box` on purpose:
    # that one is the six behaviours and its reset button promises to restore
    # exactly those. Which editor a code result opens in is not a search
    # behaviour and must not be swept up by a button whose tooltip says what
    # it will not touch.
    "settings.search": ("settings_view.py", "widgets/search_box.py",
                        "widgets/search_behaviour_box.py",
                        "widgets/editor_box.py"),
    # Only *when* a run happens - the Indexing page's Schedule shelf.
    # Everything about how fast it goes moved to `indexing.tuning` - see §4 of
    # the index-tuning order.
    "indexing.schedule": ("indexing_settings.py",),
    # The Index Tuning screen - the Indexing page's Tuning shelf. Four group
    # boxes and a mode switch, all under `widgets/` because the page they sit
    # on is at the 250-line guard. `long_run_box` is the Coverage group: it
    # was already the panel for what gets read, so it moved screen rather
    # than being rebuilt.
    "indexing.tuning": ("widgets/tuning_box.py", "widgets/tuning_groups.py",
                        "widgets/long_run_box.py", "widgets/converter_box.py"),
    "settings.reading": ("settings_view.py", "widgets/file_types.py"),
    # `storage_box` too: EMBED_MODEL and EMBED_DIM are Models settings whose
    # flow lives with the index location it invalidates.
    "settings.models": ("settings_view.py", "widgets/model_box.py",
                        "widgets/storage_box.py", "widgets/media_box.py",
                        "widgets/chat_box.py"),
    "settings.storage": ("settings_view.py", "widgets/storage_box.py"),
}

#: Settings rendered as a deliberate flow rather than a plain control, because
#: changing them changes what the index *is*. They still need something on
#: screen, so the flow's own name is what is looked for instead of the key.
#:
#: Not an exemption: a flow that nothing invokes is the same bug in a better
#: costume, and `test_a_flow_is_actually_invoked` asserts the name appears.
FLOWS = {setting.key: setting.flow for setting in reg.SETTINGS if setting.flow}


def _registry_driven_keys(path: Path) -> set[str]:
    """Keys of a widget that builds one control per registry entry.

    `widgets/chat_box.py` does (its docstring says so): it calls
    `setObjectName(setting.key)` and writes `{setting.key: value}`, so a scan
    for string literals cannot see either. What proves the controls exist is
    `test_chat_tab_qt.py::test_the_chat_settings_group_builds_one_control_per_
    declared_setting`, which builds the box and looks each one up by name.
    """
    if path.name == "chat_box.py":
        from app.ui.widgets.chat_box import chat_settings

        return {setting.key for setting in chat_settings()}
    return set()


def _object_names_in(path: Path) -> set[str]:
    """Every string passed to `setObjectName(...)` in one module."""
    if not path.is_file():
        return set()
    found: set[str] = set(_registry_driven_keys(path))
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


# ---------------------------------------------------------------------------
# Item 4: a setting that needs a restart says so, somewhere the person can
# actually see it.
# ---------------------------------------------------------------------------

#: For a destructive setting the restart notice is not on the control - the
#: control is a read-only display, by design (§4 of the work order) - it is in
#: the flow's *outcome* message, built in `shell.py` once the flow has been
#: confirmed. There is no registry field naming "which function finishes this
#: flow", so it is named here; a wrong name fails this test loudly rather than
#: silently passing.
FLOW_HANDLERS_IN_SHELL = {
    "move-index": "_change_index_location",
    "rebuild-vectors": "_change_meaning_model",
}


def _function_source(path: Path, name: str) -> str:
    """The raw text of one `def name(...)`, prose included, for presence checks.

    Text to the next method at the same indent, which is good enough here: the
    question is only "does the word 'restart' appear", not anything about
    structure.
    """
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    marker = f"def {name}("
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("\n    def ")[0]


@pytest.mark.parametrize(
    "setting", [s for s in reg.SETTINGS if s.restart], ids=lambda s: s.key,
)
def test_restart_settings_say_so_somewhere_the_user_can_see(setting):
    """A setting flagged `restart=True` that says nothing about it anywhere is
    the exact failure item 4 exists to catch: a change that appears to work
    and silently does not, until the application is next opened.

    Weak by design, like every text-presence check in this file: it proves the
    word is there, not that the sentence is good. That is still the level of
    rigour that would catch a control losing its restart notice in a refactor,
    or a newly restart-flagged setting shipping with none.
    """
    surface_text = "".join(
        (UI / relative).read_text(encoding="utf-8").lower()
        for relative in SURFACE_MODULES.get(setting.surface, ())
        if (UI / relative).is_file()
    )
    seen = "restart" in surface_text
    handler = FLOW_HANDLERS_IN_SHELL.get(setting.flow) if setting.flow else None

    if not seen and handler:
        # Both flow handlers moved out of `MainWindow` into
        # `SettingsController` (work order 202626082352 section 7); the
        # window's same-named method only forwards, so it is the controller's
        # text that carries the outcome message.
        seen = "restart" in _function_source(
            UI / "controllers" / "settings_controller.py", handler).lower()

    where = f"{setting.surface}" + (
        f" or the settings controller's {handler!r}" if handler else "")
    assert seen, (
        f"{setting.key} is flagged restart=True but the word 'restart' "
        f"appears nowhere on {where} - a change needing a restart would look "
        f"like it took effect"
    )


def test_the_restart_notice_check_can_actually_fail(tmp_path: Path):
    """Otherwise it passes on a broken detector forever."""
    bait = tmp_path / "bait_shell.py"
    bait.write_text(
        "class Bait:\n"
        "    def _has_the_word(self) -> None:\n"
        "        self.statusBar().showMessage('Saved. Restart to apply.')\n"
        "    def _does_not(self) -> None:\n"
        "        self.statusBar().showMessage('Saved.')\n",
        encoding="utf-8",
    )
    assert "restart" in _function_source(bait, "_has_the_word").lower()
    assert "restart" not in _function_source(bait, "_does_not").lower()
    assert _function_source(bait, "_not_declared_at_all") == ""


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
        found |= _registry_driven_keys(path)
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


# ---------------------------------------------------------------------------
# Item 6: no control is orphaned - every signal a settings panel declares has
# a receiver.
#
# `rerank_toggled` and `cloud_toggled` were both declared with `pyqtSignal`,
# emitted, and connected to nothing - the exact shape checked here, generically,
# rather than the two names the review happened to find. `ui:tray_minimise` and
# `ui:tray_close` were the same failure one layer down: read at startup, written
# by nothing. All four are fixed (see `shell.py` and `widgets/window_box.py`),
# and this is what keeps the fix from quietly regressing.
# ---------------------------------------------------------------------------

#: Every module that can declare a signal on behalf of a page in Settings or
#: Indexing. Built from `SURFACE_MODULES` plus the pages and sub-widgets that
#: are wired to those pages but do not themselves carry a registry key -
#: `roots_box.py`, `window_box.py`, `environment_box.py` and the rest of
#: `indexing_view.py`'s own widgets are settings controls even though nothing
#: in them is an `.env` value.
SETTINGS_PANEL_MODULES: tuple[str, ...] = tuple(sorted({
    relative
    for modules in SURFACE_MODULES.values()
    for relative in modules
} | {
    "settings_view.py", "indexing_view.py", "indexing_settings.py",
    "widgets/debug_pane.py", "widgets/roots_box.py", "widgets/window_box.py",
    "widgets/environment_box.py", "widgets/archived_roots.py",
    "widgets/machine_card.py",
}))


def _signals_declared_in(path: Path) -> set[str]:
    """Every `name = pyqtSignal(...)` class attribute in one module.

    Not a grep for `pyqtSignal`: that would count the import line, a comment
    explaining one, or a docstring quoting one. This looks for the assignment
    shape a declaration actually has.
    """
    if not path.is_file():
        return set()
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "pyqtSignal"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found.add(target.id)
    return found


def _connect_targets_in(root: Path) -> set[str]:
    """Every signal name that is the target of a `<something>.<name>.connect(`
    call, anywhere under `root`.

    Same technique `test_header_signal_safety.py` uses to prove nothing
    connects to `sectionResized`: it looks for what the code would *do*, which
    a comment or a docstring cannot fake into passing.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
                continue
            inner = func.value
            if isinstance(inner, ast.Attribute):
                found.add(inner.attr)
    return found


# 2026-09-20: there is no exemption list any more. `history_cleared` was the one
# entry ("Clear search history" emitted it and nothing listened); `MainWindow`
# now connects it to `SavedSearches.forget_recent`, so the search box stops
# offering searches that were just erased - see `test_clearing_the_history_*`
# in `test_window_opens.py`. A new orphan fails here with no place to hide.

def test_every_signal_a_settings_panel_declares_has_a_receiver():
    """The generic form of the `rerank_toggled` / `cloud_toggled` bug.

    Weak by design, like `test_every_control_writes_its_setting_somewhere`
    above: it proves *something* connects to the signal somewhere in `app/ui`,
    not that the right thing does. That is exactly the level of rigour that
    would have caught both known cases - each was connected to nothing at all.
    """
    connected = _connect_targets_in(UI)
    missing: list[str] = []
    for relative in SETTINGS_PANEL_MODULES:
        for name in sorted(_signals_declared_in(UI / relative)):
            entry = f"{relative}: {name}"
            if name not in connected:
                missing.append(entry)
    assert not missing, (
        "these settings-panel signals are declared and emitted, but nothing "
        "anywhere in app/ui calls .connect() on them - the exact shape of the "
        "rerank_toggled/cloud_toggled bug:\n  " + "\n  ".join(missing)
    )


def test_the_orphaned_signal_detector_can_actually_fail(tmp_path: Path):
    """Otherwise it passes on a broken detector forever."""
    bait = tmp_path / "bait_panel.py"
    bait.write_text(
        "from PyQt6.QtCore import pyqtSignal\n"
        "class Bait:\n"
        "    orphaned_signal = pyqtSignal(bool)\n",
        encoding="utf-8",
    )
    assert "orphaned_signal" in _signals_declared_in(bait)
    assert "orphaned_signal" not in _connect_targets_in(tmp_path), (
        "a fresh directory with no .connect() call must not report one")
