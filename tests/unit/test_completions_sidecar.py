r"""Slash menu §4b and §4c: what a shell can answer without starting Python.

A Tab press expects an answer in tens of milliseconds and a cold start of this
application's imports costs hundreds, so the completion path is split: a
sidecar written at the end of every index run covers nearly every press, and a
standalone `suggest.py` answers the rest without importing `app` at all.

Measured on the container this suite runs in, 2026-08-27:

    bare interpreter            14ms
    sqlite3 + json              20ms
    import app                  62ms
    import app.cli             243ms   <- what the fallback must not pay
    suggest.py, end to end      35ms   <- what it does pay

The second copy of the value queries in `suggest.py` is deliberate and is the
price of that number. `test_the_standalone_suggester_agrees_with_the_store`
runs both against one database and compares, which is the honest way to keep
two copies honest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.search.completions import (
    SIDECAR_NAME,
    SOURCES,
    TOP_PER_SOURCE,
    build_sidecar,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from app.storage.sqlite_store import SqliteStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def indexed(tmp_path):
    """A small index with two extensions, one folder and a repository."""
    data = tmp_path / "data"
    (data / "fts").mkdir(parents=True)
    store = SqliteStore(data / "fts" / "knowledge.db").connect()
    for number in range(5):
        store.upsert_file(f"C:/work/report{number}.pdf", parent_dir="C:/work",
                          ext="pdf", size_bytes=1, mtime_ns=1,
                          status="INDEXED", source_kind="file")
    store.upsert_file("C:/work/notes.docx", parent_dir="C:/work", ext="docx",
                      size_bytes=1, mtime_ns=1, status="INDEXED",
                      source_kind="file")
    yield store, data
    store.close()


# --- 4b: the sidecar --------------------------------------------------------


def test_the_sidecar_holds_the_values_and_their_counts(indexed) -> None:
    store, _data = indexed

    built = build_sidecar(store)

    assert built["version"] == 1
    assert built["sources"]["ext"][0] == {"value": "pdf", "count": 5,
                                          "exact": True}


def test_a_source_with_nothing_in_it_is_left_out(indexed) -> None:
    """So a completer can tell "no such source" from "nothing indexed yet" and
    fall back for the first rather than for the second."""
    store, _data = indexed

    built = build_sidecar(store)

    assert "sender" not in built["sources"], "no mail was indexed"
    assert "ext" in built["sources"]


def test_it_is_bounded_per_source(indexed) -> None:
    """A sidecar that grows with the corpus is a file the shell parses on every
    Tab press - which is the cost it exists to avoid."""
    store, _data = indexed

    for rows in build_sidecar(store, limit=3)["sources"].values():
        assert len(rows) <= 3


def test_the_file_is_small(indexed) -> None:
    store, data = indexed

    path = write_sidecar(store, data)

    assert path is not None
    assert path.name == SIDECAR_NAME
    assert path.stat().st_size < 64 * 1024


def test_it_is_written_atomically(indexed) -> None:
    r"""A completer that reads a half-written file raises at the exact moment
    somebody pressed Tab, which reads as the application being broken rather
    than as a race.

    Asserted through the code, because the window this closes is too small to
    hit reliably from a test - and a flaky test of a race is worse than none.
    """
    import ast
    import inspect
    import textwrap

    from app.search import completions

    tree = ast.parse(textwrap.dedent(inspect.getsource(completions.write_sidecar)))
    node = tree.body[0]
    node.body = node.body[1:]
    code = ast.unparse(node)

    assert "os.replace" in code, "the rename is what makes it atomic"
    assert "mkstemp" in code
    assert "dir=str(target.parent)" in code, (
        "a rename across filesystems is not atomic, and the index may be on "
        "another drive from the temp folder")


def test_a_missing_sidecar_reads_as_empty(tmp_path) -> None:
    assert read_sidecar(tmp_path) == {}


def test_a_half_written_sidecar_reads_as_empty(indexed) -> None:
    """Absent, unreadable, truncated and from a future version all mean the
    same thing to a caller: ask the store instead."""
    store, data = indexed
    write_sidecar(store, data)

    sidecar_path(data).write_text("{ half", encoding="utf-8")
    assert read_sidecar(data) == {}

    sidecar_path(data).write_text(json.dumps({"version": 99, "sources": {}}),
                                  encoding="utf-8")
    assert read_sidecar(data) == {}


def test_writing_never_raises(tmp_path) -> None:
    """It runs at the end of a run that may have taken days."""

    class Broken:
        def distinct_value_counts(self, *_a, **_k):
            raise RuntimeError("the store is closed")

    assert write_sidecar(Broken(), tmp_path) is not None


def test_branches_are_deliberately_absent() -> None:
    """They come from git, and answering them would mean walking every checkout
    at the end of every index run - minutes of subprocesses for a menu. The
    REPL answers them in-process, when somebody asks."""
    assert "branch" not in SOURCES


def test_the_run_writes_one(tmp_path) -> None:
    """4b says "at the end of every run", and a sidecar nothing writes is a
    sidecar that is always stale."""
    import ast
    import inspect
    import textwrap

    from app.index import pipeline

    tree = ast.parse(textwrap.dedent(inspect.getsource(pipeline.Pipeline.run)))
    node = tree.body[0]
    node.body = node.body[1:]
    assert "_write_completions" in ast.unparse(node)


# --- 4c: the standalone fallback --------------------------------------------


def _run(args, data: Path, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "suggest.py"), *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "DATA_PATH": str(data)},
    )


def test_the_suggester_answers_from_the_index(indexed) -> None:
    _store, data = indexed

    done = _run(["ext", ""], data)

    assert done.returncode == 0
    assert done.stdout.split() == ["pdf", "docx"]


def test_a_prefix_narrows_it(indexed) -> None:
    _store, data = indexed

    assert _run(["ext", "doc"], data).stdout.split() == ["docx"]


def test_it_says_nothing_rather_than_saying_it_failed(indexed) -> None:
    """A completer offers whatever it is given, so an error message would
    become a completion - which is worse than offering nothing."""
    _store, data = indexed

    for args in (["nosuchsource"], [], ["ext"]):
        done = _run(args, data if args != ["ext"] else Path("/nowhere"))
        assert done.returncode == 0
        assert done.stdout.strip() == ""


def test_it_imports_nothing_from_the_application() -> None:
    r"""**The whole design.** `import app.cli` costs 243ms before it has opened
    anything; the budget for a Tab press is 300ms in total. This file lives
    outside the package and stays there.
    """
    import ast

    # **Import nodes, not lines that look like imports.** The docstring of
    # `suggest.py` carries the measurement table that justifies it, and one of
    # its rows reads "import app  62ms" - so a line scan matches the
    # explanation. Fifth time this project has been caught by that.
    tree = ast.parse((ROOT / "suggest.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])

    assert "app" not in roots, sorted(roots)


def test_it_opens_the_database_read_only() -> None:
    """A completer must never be the thing that locks an index run out of its
    own database."""
    source = (ROOT / "suggest.py").read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "uri=True" in source


@pytest.mark.slow
def test_it_answers_inside_the_budget(indexed) -> None:
    """4c: "measured under 300ms cold on the scale fixture, or the fallback is
    dropped and the sidecar is the whole answer". Measured at 35ms."""
    _store, data = indexed

    best = min(
        (lambda t0: (_run(["ext", ""], data), (time.perf_counter() - t0) * 1000)[1])(
            time.perf_counter())
        for _ in range(3)
    )
    assert best < 300, f"a cold suggest took {best:.0f}ms"


def test_the_standalone_suggester_agrees_with_the_store(indexed) -> None:
    r"""**Two copies of the value queries, held to each other.**

    `suggest.py` restates them because importing the store costs 228ms and the
    whole point of that file is not paying it. Restating them is a drift risk,
    and this is what makes it an honest one rather than a hidden one: both are
    run against the same database and the answers compared.

    Through the subprocess rather than in-process, because the standalone one
    reads `DATA_PATH` from the environment and a test must not set that for
    every test that follows it.
    """
    store, data = indexed

    for source in ("ext", "folder", "repo"):
        theirs = [line for line in _run([source, ""], data).stdout.splitlines()
                  if line]
        mine = store.distinct_values(source, limit=40)
        assert theirs == mine, source


# --- 4a and 4d: the PowerShell completer ------------------------------------


def test_the_script_names_every_filter_the_catalogue_does() -> None:
    r"""**Generated, never hand-written.** A completer typed out by hand is a
    second copy of the grammar, and one catalogue feeding every consumer is
    this project's non-negotiable for commands. Regenerating is the update
    path, and this is what makes that true rather than intended.
    """
    from app.search.commands import COMMANDS
    from app.search.pwsh_completer import completer_script

    script = completer_script(project_path="D:/SearchProject")

    for command in COMMANDS:
        assert f"Name = '{command.name}'" in script, command.name


def test_each_row_carries_its_summary_as_the_tooltip() -> None:
    """The CLI's inline metadata, and the same words the dropdown shows."""
    from app.search.commands import command_for
    from app.search.pwsh_completer import completer_script

    script = completer_script(project_path="D:/SearchProject")
    summary = command_for("type").summary

    assert f"Tip = '{summary}'" in script


def test_the_subcommands_come_from_the_parser() -> None:
    """From `build_parser` rather than a list, for the reason the module
    exists: a hand-written second copy is what drifts."""
    from app.search.pwsh_completer import _subcommands, completer_script

    verbs = _subcommands()
    script = completer_script(project_path="D:/SearchProject")

    assert "search" in verbs and "index" in verbs
    for verb in verbs:
        assert f"'{verb}'" in script, verb


def test_the_sidecar_is_read_before_python_is_started() -> None:
    """The whole design in one ordering: a file first, a process only when the
    file has no answer."""
    from app.search.pwsh_completer import completer_script

    script = completer_script(project_path="D:/SearchProject")
    sidecar_at = script.index("completions.json")
    fallback_at = script.index("suggest.py")

    assert sidecar_at < fallback_at


def test_a_quote_in_a_summary_cannot_break_the_script() -> None:
    """Somebody's apostrophe must not end a PowerShell string."""
    from app.search.pwsh_completer import completer_script

    class Awkward:
        name = "odd"
        source = "ext"
        summary = "Dave's files"

    script = completer_script(commands=[Awkward()], subcommands=("search",),
                              project_path="D:/x")

    assert "Tip = 'Dave''s files'" in script


def test_installing_is_idempotent(tmp_path) -> None:
    """Somebody who runs it twice must not get two completers."""
    from app.search.pwsh_completer import install_into

    profile = tmp_path / "profile.ps1"
    profile.write_text("Set-Alias ll Get-ChildItem\n", encoding="utf-8")

    first = install_into(profile, tmp_path / "gen.ps1")
    assert first is not None
    profile.write_text(first, encoding="utf-8")

    assert install_into(profile, tmp_path / "gen.ps1") is None


def test_removing_puts_the_profile_back_exactly(tmp_path) -> None:
    """A profile is somebody's own file. This takes out what it put in and
    touches nothing else."""
    from app.search.pwsh_completer import install_into

    profile = tmp_path / "profile.ps1"
    original = "Set-Alias ll Get-ChildItem\nfunction prompt { 'PS> ' }\n"
    profile.write_text(original, encoding="utf-8")

    profile.write_text(install_into(profile, tmp_path / "gen.ps1"),
                       encoding="utf-8")
    back = install_into(profile, tmp_path / "gen.ps1", remove=True)

    assert back == original


def test_removing_when_it_was_never_there_changes_nothing(tmp_path) -> None:
    from app.search.pwsh_completer import install_into

    profile = tmp_path / "profile.ps1"
    profile.write_text("Set-Alias ll Get-ChildItem\n", encoding="utf-8")

    assert install_into(profile, tmp_path / "gen.ps1", remove=True) is None


def test_the_profile_gets_a_dot_source_not_the_whole_script(tmp_path) -> None:
    """So regenerating after a catalogue change does not need the profile
    edited again - the shape `add-to-path.ps1` uses."""
    from app.search.pwsh_completer import install_into

    profile = tmp_path / "profile.ps1"
    profile.write_text("", encoding="utf-8")

    added = install_into(profile, tmp_path / "leasha-completions.ps1")

    assert "Register-ArgumentCompleter" not in added
    assert ". '" in added


def test_the_tab_completer_offers_unscoped_values() -> None:
    r"""Deliberate, and the order says why: conditioning on the half-typed
    query would mean parsing it inside a PowerShell script block - a second
    parser in a second language. Scoped completion in a terminal is what
    `leasha shell` is for.
    """
    from app.search.pwsh_completer import completer_script

    script = completer_script(project_path="D:/SearchProject")

    assert "within" not in script
    assert "scoped" not in script.lower()


# --- 4d: the installer asks, once, and never assumes -------------------------

INSTALLER = (ROOT / "install.ps1").read_text(encoding="utf-8")


def test_the_installer_offers_it_as_a_question() -> None:
    """A PowerShell profile is somebody's own file. Writing to it uninvited is
    the kind of thing that gets an application uninstalled."""
    assert "Tab completion (optional)" in INSTALLER
    assert "Set it up? [y/N]" in INSTALLER, "the default must be no"


def test_it_is_skipped_when_the_extras_are() -> None:
    """`-SkipOptional` already means "do not ask me about the extras", and
    `-Preflight` checks without changing anything - so neither may write to a
    profile."""
    block = INSTALLER[INSTALLER.index("Tab completion (optional)") - 400:
                      INSTALLER.index("Tab completion (optional)")]
    assert "-not $SkipOptional" in block
    assert "-not $Preflight" in block


def test_declining_says_how_to_do_it_later() -> None:
    """A skip that leaves somebody without the way back is a skip they cannot
    undo."""
    assert "completions install" in INSTALLER


def test_the_installer_still_parses() -> None:
    """`run-install.cmd` parse-checks before running. This is the cheapest
    approximation available without PowerShell."""
    import re

    stripped = re.sub(r"#.*", "", INSTALLER)
    assert stripped.count("{") == stripped.count("}")
    assert stripped.count("(") == stripped.count(")")
    assert INSTALLER.count('@"') == INSTALLER.count('"@')
