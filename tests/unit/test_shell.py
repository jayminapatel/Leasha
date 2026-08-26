r"""Slash menu §4g: `leasha shell`, and the rule it lives under.

**The REPL makes no decision of its own.** Every rule it applies comes from the
presenter, every result it prints comes from the CLI's renderer, and every
filter it parses comes from `parse_query`. So most of what is asserted here is
*sameness* - that the terminal and the window answer the same questions with
the same words - rather than behaviour this package invented.

The inertness cases are re-asserted through the REPL completer deliberately.
They pass in the window because `slash_context` decides them; the point of
running them again here is to prove the terminal asks the same function rather
than a second one that happens to agree today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("prompt_toolkit", reason="the REPL needs prompt_toolkit")

from prompt_toolkit.document import Document      # noqa: E402

from app.search.commands import COMMANDS          # noqa: E402
from app.shell.completer import LeashaCompleter, command_rows   # noqa: E402
from app.storage.sqlite_store import SqliteStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def store(tmp_path):
    opened = SqliteStore(tmp_path / "index.db").connect()
    for number in range(4):
        opened.upsert_file(f"C:/work/report{number}.pdf", parent_dir="C:/work",
                           ext="pdf", size_bytes=1, mtime_ns=1,
                           status="INDEXED", source_kind="file")
    opened.upsert_file("C:/home/notes.docx", parent_dir="C:/home", ext="docx",
                       size_bytes=1, mtime_ns=1, status="INDEXED",
                       source_kind="file")
    yield opened
    opened.close()


def complete(completer: LeashaCompleter, text: str):
    return list(completer.get_completions(Document(text, len(text))))


# --- 4g-1: the dropdown, and when it does not open --------------------------


def test_a_slash_opens_the_command_menu(store) -> None:
    got = complete(LeashaCompleter(store), "/ty")

    assert got[0].text == "type:"
    assert got[0].display_text == "/type"


def test_a_settled_name_opens_the_value_menu(store) -> None:
    got = complete(LeashaCompleter(store), "type:")

    assert [c.text for c in got][:2] == ["pdf", "docx"]


@pytest.mark.parametrize("text", ["D:/docs", "12/03", "http://example.com",
                                  "plain words", ""])
def test_the_menu_stays_shut_on_everything_else(store, text: str) -> None:
    r"""**The same tested reason as the window.** `12/03` and `D:\docs` are
    somebody's text, and a search box that silently rewrites what was typed is
    one they stop trusting. This passes because `slash_context` decides it -
    which is the thing being asserted."""
    assert complete(LeashaCompleter(store), text) == []


def test_the_completer_asks_the_presenter_rather_than_deciding(store) -> None:
    """A second copy of the trigger rule is the drift this order forbids."""
    source = (ROOT / "app" / "shell" / "completer.py").read_text(encoding="utf-8")
    assert "slash_context" in source
    assert "startswith('/')" not in source.replace('"', "'")


# --- 4g-2: rows say the same things the window's rows say -------------------


def test_a_value_row_carries_its_count(store) -> None:
    got = complete(LeashaCompleter(store), "type:")

    assert "4 files" in got[0].display_text
    assert got[0].text == "pdf", "the count must not reach the query"


def test_a_date_row_carries_its_resolved_range(store) -> None:
    got = complete(LeashaCompleter(store), "after:30")

    assert "(since" in got[0].display_text


def test_the_description_column_is_the_catalogue_summary(store) -> None:
    from app.search.commands import command_for

    got = complete(LeashaCompleter(store), "type:")

    assert got[0].display_meta_text == command_for("type").summary


def test_command_rows_carry_the_catalogue_glyph() -> None:
    """`command_icon`'s field, in the description rather than the text - a
    completion carrying an icon would put the icon in the query."""
    rows = dict(command_rows("type"))

    assert rows["type"].startswith(next(c.icon for c in COMMANDS
                                        if c.name == "type"))


def test_what_the_window_shows_and_what_the_repl_shows_are_one_call(
    store
) -> None:
    """The drift test 4g-2 asks for: both go through `value_rows`."""
    from app.ui.presenter import value_rows, value_suggestions

    counts: dict = {}
    found = value_suggestions(store, "type", "", counts=counts)
    theirs = value_rows("type", [counts.get(v, v) for v in found])

    mine = [c.display_text for c in complete(LeashaCompleter(store), "type:")]

    assert mine == [row.rstrip() for row in theirs]


# --- 4g-3: scoping, which is why the REPL exists ----------------------------


def test_the_settled_tokens_narrow_the_values(store) -> None:
    r"""`repo:leasha branch:<Tab>` in a terminal, with what a fixture can have.

    `type` is scoped by `repo` and by nothing else - 1c's mapping - so this
    uses the relationship that exists rather than one that reads plausibly.
    `path:` deliberately does **not** narrow `/type`, and asserting that it did
    would have been asserting a bug.
    """
    repo_id = store.upsert_repo("C:/code/leasha", kind="git", name="leasha")
    store.upsert_file("C:/code/leasha/main.py", parent_dir="C:/code/leasha",
                      ext="py", size_bytes=1, mtime_ns=1, status="INDEXED",
                      source_kind="file", repo_id=repo_id)

    inside = [c.text for c in complete(LeashaCompleter(store),
                                       "repo:leasha type:")]
    everywhere = [c.text for c in complete(LeashaCompleter(store), "type:")]

    assert inside[0] == "py", inside
    assert "pdf" in everywhere and "pdf" not in inside[:1]


def test_the_empty_fallback_applies_unchanged(store) -> None:
    """1e in a terminal: never an empty menu when unscoped values exist."""
    completer = LeashaCompleter(store)

    got = [c.text for c in complete(completer, "repo:nothing type:")]

    assert got, "an empty menu is indistinguishable from a broken one"
    assert completer.note, "and falling back silently is the same failure"


# --- 4g-5: the toolbar ------------------------------------------------------


def test_the_toolbar_says_what_this_filter_wants(store) -> None:
    from app.search.commands import command_for
    from app.shell.repl import toolbar_text

    text = toolbar_text(LeashaCompleter(store),
                        document=Document("type:p", len("type:p")))

    assert command_for("type").value_hint in text


def test_the_toolbar_resolves_a_date_being_typed(store) -> None:
    from app.shell.repl import toolbar_text

    text = toolbar_text(LeashaCompleter(store),
                        document=Document("after:30d", len("after:30d")))

    assert "(since" in text


def test_the_toolbar_carries_the_fallback_note(store) -> None:
    from app.shell.repl import toolbar_text

    completer = LeashaCompleter(store)
    complete(completer, "repo:nothing type:")

    assert "showing all values" in toolbar_text(completer)


def test_the_toolbar_never_raises(store) -> None:
    """It is drawn on every keystroke; an exception there ends the session."""
    from app.shell.repl import toolbar_text

    class Awkward:
        note = property(lambda self: 1 / 0)

    assert isinstance(toolbar_text(LeashaCompleter(store), document=None), str)


# --- 4g-6: history lives beside the index -----------------------------------


def test_history_is_stored_beside_the_index_not_the_repo(tmp_path) -> None:
    """A repository that gets re-cloned, or a project folder the installer
    rebuilds, must not take somebody's history with it."""
    from app.shell.repl import history_path

    class Settings:
        data_path = tmp_path / "Leasha"

    assert history_path(Settings()).parent == tmp_path / "Leasha"


# --- 4g-7: one renderer, one grammar ----------------------------------------


def test_results_print_through_the_cli_renderer() -> None:
    """4g's rule: "no second renderer". It was inline in `cmd_search` until
    this needed it, and extracting it is what makes the rule true."""
    from app.shell import repl

    source = (ROOT / "app" / "shell" / "repl.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.module for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and node.module}

    assert "app.cli" in names
    assert "print_response" in source
    assert not hasattr(repl, "_print_result_line"), (
        "a second renderer has appeared in the shell")


def test_help_prints_the_catalogue_not_a_copy_of_it() -> None:
    source = (ROOT / "app" / "shell" / "repl.py").read_text(encoding="utf-8")
    assert "help_lines" in source


def test_a_search_that_raises_does_not_end_the_session(store) -> None:
    """A session that dies on one bad search is a session nobody trusts with a
    long one."""
    from app.shell.repl import _run_one

    class Angry:
        def search(self, _text):
            raise RuntimeError("the store went away")

    assert _run_one(Angry(), "anything") is None


# --- 4g-8: the guards -------------------------------------------------------


@pytest.mark.parametrize("module", ["completer.py", "repl.py", "__init__.py"])
def test_no_shell_module_imports_a_widget(module: str) -> None:
    r"""The presenter is Qt-free on purpose, and a terminal reaching into a
    widget module would make that untrue by accident."""
    tree = ast.parse((ROOT / "app" / "shell" / module).read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "app.ui.widgets" not in node.module, node.module
            assert not node.module.startswith("PyQt"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("PyQt"), alias.name


def test_the_completer_offers_exactly_what_the_catalogue_names() -> None:
    """Parity with `COMMANDS`, which is 4g-8's first requirement."""
    offered = {name for name, _description in command_rows("")}

    assert offered == {command.name for command in COMMANDS}


def test_every_alias_finds_its_command() -> None:
    """`/ext` and `/kind` are `type`. A menu that knows only canonical names
    makes the aliases undiscoverable, which is how they went unused before."""
    for command in COMMANDS:
        for spelling in command.spellings:
            found = dict(command_rows(spelling))
            assert command.name in found, (command.name, spelling)
