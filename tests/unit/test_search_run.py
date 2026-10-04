"""Every surface searches as the Search tab does. 2026-10-04.

Layer: L4 (`app.search.run`) with its callers at L5/L6 and the command line.

The owner: "where ever possible the same code should run for functions so they
are all consistent and standard", and his decision - the command line and the
MCP server search **exactly** as the window's Search tab does: the Settings
search switches, plain-English filter reading, saved searches, one row per
document. Each test here puts the same line through two or more entry points
over one temporary index and asserts the same answer.

The window's half is driven the way the Search tab drives it -
`SavedSearches.expand`, `search_options`, a `SearchWorker` run in place, then
`group_results` - so a change to any of those that is not also a change to
`run_search` turns these red.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import cli
from tests.unit.test_cli_wiring import env_file, parser_for, refuse_model_fetch

MESSAGE = "pst://2024/2097188"
GONE = "C:/gone/boiler manual.txt"


def _ns(year: int, month: int = 6, day: int = 1) -> int:
    from datetime import datetime

    return int(datetime(year, month, day).timestamp()) * 10**9


@pytest.fixture()
def index(tmp_path, monkeypatch):
    """`(env path, settings, path of the real pdf)` over a small index."""
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    refuse_model_fetch(monkeypatch)
    env = env_file(tmp_path)
    settings = load_settings(Path(env))
    settings.fts_db.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "work" / "boiler quote.pdf"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"%PDF-1.4")
    with SqliteStore(settings.fts_db) as store:
        quote = store.upsert_file(str(real), parent_dir=str(real.parent), ext="pdf",
                                  size_bytes=8, mtime_ns=_ns(2023), status="INDEXED",
                                  source_kind="file")
        store.replace_chunks(quote, [
            {"ordinal": 0, "text": "Boiler service quote, spring."},
            {"ordinal": 1, "text": "The boiler quote total is due in May."}])
        gone = store.upsert_file(GONE, parent_dir="C:/gone", ext="txt", size_bytes=0,
                                 mtime_ns=_ns(2020), status="INDEXED", source_kind="file")
        store.replace_chunks(gone, [{"ordinal": 0, "text": "the old boiler manual"}])
        for path, year in (("C:/work/march report.txt", 2017),
                           ("C:/work/may report.txt", 2019)):
            found = store.upsert_file(path, parent_dir="C:/work", ext="txt", size_bytes=10,
                                      mtime_ns=_ns(year), status="INDEXED",
                                      source_kind="file")
            store.replace_chunks(found, [{"ordinal": 0, "text": "the site report"}])
        message = store.upsert_file(MESSAGE, size_bytes=1, mtime_ns=_ns(2024), ext="pst",
                                    source_kind="pst_message", status="INDEXED")
        store.set_message(message, store_path="D:/a.pst", entry_id="2097188",
                          subject="The boiler", sender="Dave", sent_at=_ns(2024) // 10**9)
        store.replace_chunks(message, [{"ordinal": 0, "text": "Dave sent the boiler quote."}])
        store.save_search("reports", "report /oldest", "all")
    return env, settings, str(real)


@contextmanager
def _engine(settings):
    """A keyword-only engine over the temporary index, built as the MCP server
    builds one - no model, no reranker."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore
    from tests.unit.conftest import _NoModel

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as images:
        engine = SearchEngine(store, vectors, _NoModel(), image_vectors=images)
        try:
            yield engine
        finally:
            engine.close()


def _window(settings, text: str) -> list[str]:
    """The Search tab's steps, as `SearchView._dispatch` and `_on_results` take
    them: expand in the box, a `SearchWorker`, then one group per document."""
    from app.search.policy import preferences
    from app.search.run import load_saved
    from app.ui.presenter import Tier, group_results, search_options, to_rows
    from app.ui.saved_box import SavedSearches
    from app.ui.workers import SearchWorker

    with _engine(settings) as engine:
        saved = SavedSearches(None)
        saved._took(load_saved(engine.store))
        query = saved.expand(text)
        options = search_options(Tier.FULL, scope="all", rerank=False, surface="search",
                                 preferences=preferences(settings), declined=())
        worker = SearchWorker(engine, query, tier=Tier.FULL, generation=1, **options)
        landed: list = []
        worker.signals.finished.connect(landed.append)
        worker.signals.failed.connect(lambda error: pytest.fail(str(error)))
        worker.run()
        response = landed[0][1]
        terms = list(response.parsed.terms) if response.parsed else []
        return [group.path for group in group_results(to_rows(response.results, terms))]


def _cli(env, text: str, capsys) -> dict:
    capsys.readouterr()
    cli.cmd_search(parser_for(["search", text, "--json", "--limit", "50", "--env", env]))
    return json.loads(capsys.readouterr().out)


def _mcp(settings, text: str) -> dict:
    from app.serve.mcp import IndexTools
    from tests.unit.conftest import _NoModel

    return IndexTools(settings, lambda: (_NoModel(), None, None)).search(text, limit=50)


def _shared(settings, text: str):
    from app.search.policy import preferences
    from app.search.run import run_search

    with _engine(settings) as engine:
        return run_search(engine, text, preferences=preferences(settings), limit=50)


# -- the Search tab, the command line, the MCP server and the shell agree -----

@pytest.mark.parametrize("text", [
    "boiler",                       # one document matching twice is one row
    "report from 2017",             # plain English read as a date filter
    "saved:reports",                # a saved search, with a slash inside it
    "report /oldest",               # a slash command
    "mail about the boiler",        # plain English read as a type
])
def test_the_search_tab_the_command_line_and_mcp_find_the_same_documents(
        qapp, index, capsys, text):
    env, settings, _real = index
    window = _window(settings, text)
    assert window, f"the fixture finds nothing for {text!r}, so this proves nothing"
    assert [r["path"] for r in _cli(env, text, capsys)["results"]] == window
    assert [r["path"] for r in _mcp(settings, text)["results"]] == window
    assert [d.best.path for d in _shared(settings, text).documents] == window


def test_the_filters_are_read_the_same_way_and_the_switch_turns_them_off(qapp, index, capsys):
    env, settings, _real = index
    on = _window(settings, "report from 2017")
    assert [Path(p).name for p in on] == ["march report.txt"]
    assert _cli(env, "report from 2017", capsys)["applied"][0]["label"] == "in 2017"
    assert _mcp(settings, "report from 2017")["read_as"] == ["in 2017"]

    # The Settings switch "filter chips" off: every surface keeps the words.
    with open(env, "a", encoding="utf-8") as handle:
        handle.write("SEARCH_AUTO_CHIPS=false\n")
    from app.core.config import load_settings

    off_settings = load_settings(Path(env))
    off = _window(off_settings, "report from 2017")
    assert off != on
    assert [r["path"] for r in _cli(env, "report from 2017", capsys)["results"]] == off
    assert [r["path"] for r in _mcp(off_settings, "report from 2017")["results"]] == off
    assert _mcp(off_settings, "report from 2017")["read_as"] == []


def test_one_row_per_document_with_its_best_passage_everywhere(qapp, index, capsys):
    env, settings, real = index
    payload = _cli(env, "boiler", capsys)
    rows = [r for r in payload["results"] if r["path"] == real]
    assert len(rows) == 1 and rows[0]["matches"] == 2
    found = [r for r in _mcp(settings, "boiler")["results"] if r["path"] == real]
    assert len(found) == 1 and found[0]["matches"] == 2
    assert found[0]["passage"] in {"Boiler service quote, spring.",
                                   "The boiler quote total is due in May."}
    assert [r["rank"] for r in payload["results"]] == list(range(1, len(payload["results"]) + 1))


def test_missing_files_are_marked_as_the_results_list_marks_them(qapp, index, capsys):
    from app.ui.tasks import decorate_results

    env, settings, real = index
    with _engine(settings) as engine:
        response = engine.search("boiler")
        window_missing = decorate_results(engine.store, response.results)["missing"]
    assert window_missing == {GONE}
    for results in (_cli(env, "boiler", capsys)["results"], _mcp(settings, "boiler")["results"]):
        status = {r["path"]: r["status"] for r in results}
        assert status[GONE] == "missing" and status[real] == "ok" and status[MESSAGE] == "ok"


def test_an_offline_drive_is_marked_the_same_way_as_the_list_marks_it(monkeypatch):
    from types import SimpleNamespace

    import app.index.offline_media as offline_media
    from app.search.marks import OFFLINE, status_marks
    from app.ui.tasks import offline_volume_marks

    class Store:
        def get_volume(self, volume_id):
            return SimpleNamespace(name="Backup", last_scanned_at=0)

    monkeypatch.setattr(offline_media, "connected_volumes", lambda _store: {})
    rows = [SimpleNamespace(file_id=7, path="V:/a.txt", volume_id=3)]
    assert status_marks(Store(), rows) == {7: OFFLINE}
    assert set(offline_volume_marks(Store(), rows)) == {7}


def test_mail_cards_come_from_the_one_batched_read(qapp, index):
    found = _mcp(index[1], "boiler")["results"]
    mail = next(r for r in found if r["path"] == MESSAGE)["mail"]
    assert mail["subject"] == "The boiler" and mail["from"] == "Dave" and mail["sent"]


def test_the_shell_expands_what_the_search_tab_expands(index, capsys):
    from app.search.policy import preferences
    from app.shell.repl import _run_one

    _env, settings, _real = index
    seen: list = []
    with _engine(settings) as engine:
        response = _run_one(engine, "report /oldest",
                            lambda r, _t, found=None: seen.append(found),
                            preferences=preferences(settings))
    assert response.parsed.sort == "oldest"
    assert [d.best.path for d in seen[0].documents] == [
        d.best.path for d in _shared(settings, "report /oldest").documents]


# -- name search: the Files tab, `app.cli files`, MCP `find_files` ------------

def test_files_search_is_the_files_tab_search_everywhere(index, capsys):
    from app.search.policy import preferences
    from app.storage.sqlite_store import SqliteStore
    from app.ui.tasks import browse_files_typed

    env, settings, _real = index
    for line in ("boiler", "report from 2017"):
        with SqliteStore(settings.fts_db) as store:
            tab = [row["path"] for row in browse_files_typed(
                store, line, limit=50, preferences=preferences(settings))["rows"]]
        assert tab, line
        capsys.readouterr()
        cli.cmd_files(parser_for(["files", *line.split(), "--json", "--env", env]))
        assert [row["path"] for row in json.loads(capsys.readouterr().out)["matches"]] == tab
        from app.serve.mcp import IndexTools

        tools = IndexTools(settings, lambda: pytest.fail("a name search loaded a model"))
        assert [f["path"] for f in tools.find_files(line, limit=50)["files"]] == tab


def test_find_files_names_each_file_and_keeps_a_zero_size(index):
    from app.serve.mcp import IndexTools

    tools = IndexTools(index[1], lambda: pytest.fail("model"))
    files = {f["path"]: f for f in tools.find_files("boiler")["files"]}
    assert files[GONE]["name"] == "boiler manual.txt"
    assert files[GONE]["size_bytes"] == 0
    assert all(f["name"] for f in files.values())


# -- the pieces the window and the rest share --------------------------------

def test_the_saved_box_and_run_expand_a_saved_search_identically():
    from app.search.run import expand_query
    from app.search.saved import SavedSearch
    from app.ui.saved_box import SavedSearches

    saved = (SavedSearch("weekly", "/type pdf leeds", "mail"),)
    scopes: list = []
    box = SavedSearches(None, on_scope=scopes.append)
    box._took(saved)
    for typed in ("saved:weekly survey", "/saved weekly", "saved:nobody x", "/newest x"):
        assert box.expand(typed) == expand_query(typed, saved).query
    assert expand_query("saved:weekly", saved) == ("type:pdf leeds", "mail", ("weekly",))
    assert scopes and scopes[0] == "mail"


def test_a_saved_search_holding_a_slash_command_keeps_it(index):
    r"""Fixed 2026-10-04: a search is saved as the box held it, so `report
    /oldest` run as `saved:reports` reached the parser with `/oldest` still a
    slash - the sort dropped, "oldest" searched for as a word."""
    found = _shared(index[1], "saved:reports")
    assert found.expanded == _shared(index[1], "report /oldest").expanded
    assert found.response.parsed.sort == "oldest"
    names = [Path(d.best.path).name for d in found.documents]
    assert names == ["march report.txt", "may report.txt"]


def test_the_list_and_the_documents_group_by_one_rule():
    from app.search.engine import SearchResult
    from app.search.run import documents
    from app.ui.presenter import group_results, to_rows

    def hit(file_id, rank):
        return SearchResult(chunk_id=rank, file_id=file_id, path=f"C:/d/{file_id}.txt",
                            text=f"passage {rank}", score=1.0 / rank, rank=rank)

    results = [hit(1, 1), hit(2, 2), hit(1, 3), hit(-4, 4), hit(-5, 5), hit(2, 6)]
    listed = group_results(to_rows(results, []))
    grouped = documents(results, marks=False)
    assert [g.file_id for g in listed] == [d.best.file_id for d in grouped] == [1, 2, -4, -5]
    assert [g.match_count for g in listed] == [d.matches for d in grouped] == [2, 2, 1, 1]


def test_the_rerank_switch_is_one_value_everywhere(index, monkeypatch, capsys):
    from app.search.rerank import Reranker
    from app.search.run import rerank_wanted
    from app.storage.sqlite_store import SqliteStore

    env, settings, _real = index
    assert rerank_wanted(settings, None) is False                 # .env's default
    with SqliteStore(settings.fts_db) as store:
        store.set_state("ui:rerank_enabled", "on")
        assert rerank_wanted(settings, store) is True             # the box wins
    asked: list = []
    real = Reranker.from_settings.__func__

    def recording(cls, settings, **overrides):
        asked.append(overrides.get("enabled"))
        return real(cls, settings, **{**overrides, "enabled": False})

    monkeypatch.setattr(Reranker, "from_settings", classmethod(recording))
    _cli(env, "boiler", capsys)
    assert asked == [True], "the command line did not follow the window's Rerank box"
    main = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")
    assert "rerank_wanted(settings, store)" in main


def test_the_list_tabs_read_with_the_windows_switches(qapp):
    from PyQt6.QtWidgets import QWidget

    from app.ui.widgets.chips import ChipRow

    window = QWidget()
    window.search_preferences = {"auto_chips": False}
    row = ChipRow(window)
    row.declined.add(("date", "2017"))
    assert row.reading() == {"declined": (("date", "2017"),),
                             "preferences": {"auto_chips": False}}
    assert ChipRow().reading()["preferences"] is None
    for name in ("files_view.py", "mail_view.py", "code_view.py"):
        source = (Path(__file__).resolve().parents[2] / "app" / "ui" / name).read_text(
            encoding="utf-8")
        assert "**self.chips.reading()" in source, name


def test_every_surface_calls_the_one_search():
    root = Path(__file__).resolve().parents[2] / "app"
    for name in ("cli/search.py", "shell/repl.py", "serve/mcp.py", "ui/widgets/mini_search.py"):
        assert "run_search(" in (root / name).read_text(encoding="utf-8"), name
    assert "search_once(" in (root / "ui" / "workers.py").read_text(encoding="utf-8")
    assert "find_files(" in (root / "cli" / "search.py").read_text(encoding="utf-8")
    assert "search_files_by_name" not in (root / "serve" / "mcp.py").read_text(encoding="utf-8")
