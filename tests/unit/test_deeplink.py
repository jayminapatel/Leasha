r"""`leasha://` — opening Leasha on a search from anywhere else.

Layer: L0/L5. Adoptions §7a.

**The hard part is not the scheme, it is the second copy.** `SingleInstance`
deliberately refuses a second window — two cannot share one index — and that
is the right answer for a double-clicked shortcut and the wrong one for a
link. Somebody clicking a link is not asking for a second application; they
are asking the one they have to look something up. So the link process writes
one row and exits, and the running window picks it up.

**A URL is an input from outside the application**, and the second thing this
file guards is that it stays a small one: one action, a length cap, and
nothing that touches a file, a setting or the index. The worst a hostile
`leasha://` link can do is run a search the person can see.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.core.deeplink import (
    ACTIONS, MAX_QUERY, PENDING_KEY, SCHEME, Request, build, handover,
    open_command, parse, register, registry_key, registry_values, take_pending,
    unregister,
)


@pytest.fixture()
def store():
    from app.storage.sqlite_store import SqliteStore

    built = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "link.db").connect()
    yield built
    built.close()


# ---------------------------------------------------------------------------
# Reading a link
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("url", "query"), [
    ("leasha://search?q=safety%20report", "safety report"),
    ("leasha://search?q=safety+report", "safety report"),
    ("LEASHA://Search?q=pumps", "pumps"),
    ('"leasha://search?q=pumps"', "pumps"),          # Windows quotes %1
])
def test_a_link_is_read_whatever_shape_it_arrives_in(url, query):
    assert parse(url).query == query


def test_the_bare_colon_form_works_too():
    r"""`leasha://search?q=x` puts the action in the netloc; `leasha:search?q=x`
    — which a browser address bar will happily produce — puts it in the path.
    Handling one is how a scheme works from a shortcut and not from the place
    people actually paste links."""
    assert parse("leasha:search?q=pumps").query == "pumps"


def test_a_scope_rides_along():
    found = parse("leasha://search?q=invoices&scope=mail")
    assert (found.query, found.scope) == ("invoices", "mail")


def test_a_saved_search_is_a_link():
    """§3's whole point, one step further out: a standing question that lives
    on a desktop rather than inside the application."""
    assert parse(build("saved:invoices")).query == "saved:invoices"


@pytest.mark.parametrize("url", [
    "", None, "https://example.com/search?q=x", "leasha://search",
    "leasha://search?q=", "leasha://search?q=%20%20", "not a url at all",
    "leasha://", "://x",
])
def test_anything_that_is_not_a_search_link_is_none_rather_than_a_crash(url):
    """**Never raises.** This is handed whatever the operating system passes
    on the command line, which is not a controlled input."""
    assert parse(url) is None


@pytest.mark.parametrize("action", ["index", "open", "settings", "delete",
                                    "reset", "shell"])
def test_a_link_cannot_ask_for_anything_but_a_search(action):
    r"""**The load-bearing rule of the whole item.** Anything on this machine
    can invoke a URL scheme, and a link in a document is not a trusted
    instruction. Search is safe: the worst a hostile link does is run a search
    the person can see and did not want. A link that indexed a folder, changed
    a setting or opened a file would be a stranger giving orders.
    """
    assert parse(f"leasha://{action}?q=anything") is None
    assert action not in ACTIONS


def test_only_one_action_exists_at_all():
    """The guard on the guard: an action added without an argument for it
    should have to argue with this test first."""
    assert ACTIONS == ("search",)


def test_a_link_cannot_paste_a_megabyte_into_the_search_box():
    found = parse("leasha://search?q=" + "x" * 5_000)
    assert len(found.query) == MAX_QUERY


def test_a_url_round_trips_through_build_and_parse():
    for query in ("safety report", "a&b", "100% done", "quotes \"here\"",
                  "type:pdf from:dave", "josé"):
        assert parse(build(query)).query == query


def test_building_nothing_produces_nothing():
    assert build("") == "" and build(None) == "" and build("   ") == ""


def test_a_scope_survives_the_round_trip():
    assert parse(build("x", "mail")).scope == "mail"


# ---------------------------------------------------------------------------
# The hand-off — the reason the second copy does not become a window
# ---------------------------------------------------------------------------

def test_a_link_left_for_the_window_comes_back_intact(store):
    assert handover(store, "leasha://search?q=site%20survey&scope=documents")
    found = take_pending(store)
    assert (found.action, found.query, found.scope) == (
        "search", "site survey", "documents")


def test_taking_a_link_clears_it(store):
    r"""**Taken, not read.** A request left in place would be re-run on every
    poll, so the window would replace whatever somebody typed next with the
    same search, for ever. `run_lock.stop_requested` has the same shape for
    the same reason."""
    handover(store, "leasha://search?q=x")
    assert take_pending(store) is not None
    assert take_pending(store) is None


def test_a_query_with_spaces_and_quotes_survives_the_channel(store):
    """It goes through one `index_state` row, so the encoding has to carry a
    query somebody actually typed rather than one that happens to be tidy."""
    handover(store, Request("search", 'the "site survey" report', ""))
    assert take_pending(store).query == 'the "site survey" report'


def test_nothing_waiting_is_not_an_error(store):
    assert take_pending(store) is None
    assert take_pending(None) is None


def test_a_link_that_is_not_one_is_never_written(store):
    assert not handover(store, "https://example.com")
    assert not handover(store, "leasha://index?q=x")
    assert not handover(None, "leasha://search?q=x")
    assert take_pending(store) is None


def test_a_corrupt_row_costs_the_link_and_not_the_window(store):
    """Anything can write to `index_state`; a row this cannot read must be
    dropped rather than raised beside a live window."""
    for rubbish in ("", "   ", "nonsense", "search", "index x", "'unclosed"):
        store.set_state(PENDING_KEY, rubbish)
        assert take_pending(store) is None


def test_it_uses_the_channel_run_lock_already_uses(store):
    r"""Not a new mechanism, and that is the point. `run_lock` already passes
    "please stop" between two processes through this table, *"because the two
    processes share nothing else"* — the same sentence applies here."""
    handover(store, "leasha://search?q=x")
    assert store.get_state(PENDING_KEY)


# ---------------------------------------------------------------------------
# The registry, checkable on a machine that has none
# ---------------------------------------------------------------------------

def test_the_scheme_registers_per_user_and_never_per_machine():
    r"""`HKCU`, so no administrator is needed to install and nothing is left
    behind for the next user of the machine. The same reasoning that put the
    index in `%LOCALAPPDATA%`."""
    assert registry_key == rf"Software\Classes\{SCHEME}"

    # **`winreg.HKEY_LOCAL_MACHINE`, not the bare word.** The module argues in
    # prose for *not* using the machine-wide hive, so a naive grep matches its
    # own explanation - which is the trap that made a guard in this suite pass
    # over the very thing it was written to forbid, once before.
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "core" / "deeplink.py").read_text(encoding="utf-8")
    assert "winreg.HKEY_LOCAL_MACHINE" not in source
    assert "winreg.HKEY_CURRENT_USER" in source


def test_the_values_are_the_four_windows_needs():
    values = registry_values(open_command(r"C:\Apps\Leasha\leasha.cmd"))
    assert values[""] == f"URL:{SCHEME} search"
    # Its *presence* is what makes Windows treat the key as a scheme. Empty
    # is correct and is not a missing value.
    assert "URL Protocol" in values and values["URL Protocol"] == ""
    assert values[r"shell\open\command"] == (
        r'"C:\Apps\Leasha\leasha.cmd" open "%1"')


def test_the_command_is_quoted():
    r"""The default install path has no space in it and a person who moved the
    folder will have one, and an unquoted command breaks at the first."""
    assert open_command(r"C:\Program Files\Leasha\leasha.cmd").startswith('"')
    assert open_command("").strip() == ""


def test_registering_off_windows_says_so_rather_than_pretending():
    """False, not a crash and not a silent success. A scheme that could not
    be registered is a link that does not work; it is not an install that
    failed."""
    import sys

    if sys.platform.startswith("win"):           # pragma: no cover - owner's box
        pytest.skip("this asserts the non-Windows answer")
    assert register(r"C:\Apps\Leasha\leasha.cmd") is False
    assert unregister() is False


def test_registering_nothing_is_refused_before_it_reaches_the_registry():
    assert register("") is False


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_the_cli_leaves_the_link_for_the_window(tmp_path, monkeypatch):
    r"""**The outcome, end to end.** `leasha open leasha://search?q=...` is
    what Windows runs, and what it has to do is leave a query somewhere the
    window will find it — not start a second application.
    """
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    environment = {
        "DATA_PATH": str(tmp_path), "INDEX_PATH": str(tmp_path / "i"),
        "LOG_PATH": str(tmp_path / "l"), "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(root),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "open",
         "leasha://search?q=pump%20station"],
        cwd=str(root), env=environment, capture_output=True, text=True,
        timeout=120)
    assert result.returncode == 0, result.stderr

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    monkeypatch.setenv("DATA_PATH", str(tmp_path))
    monkeypatch.setenv("INDEX_PATH", str(tmp_path / "i"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "l"))
    settings = load_settings()
    with SqliteStore(settings.fts_db) as opened:
        assert take_pending(opened).query == "pump station"


def test_the_cli_refuses_a_link_that_is_not_ours(tmp_path):
    """With a sentence naming what a link looks like, rather than silence."""
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "open", "https://example.com/x"],
        cwd=str(root), text=True, capture_output=True, timeout=120,
        env={"DATA_PATH": str(tmp_path), "INDEX_PATH": str(tmp_path / "i"),
             "LOG_PATH": str(tmp_path / "l"), "PATH": "/usr/bin:/bin",
             "PYTHONPATH": str(root)})
    assert result.returncode != 0
    assert f"{SCHEME}://search?q=" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# The window's side
# ---------------------------------------------------------------------------

def test_the_watcher_picks_the_link_up_and_clears_it(store):
    r"""**It rides the run watcher rather than bringing a timer.** Polling the
    database every second for the life of every session, so that a link
    somebody clicks once a week arrives instantly, is not a trade this
    codebase makes anywhere else.
    """
    from app.ui.presenter import _read_external_run

    handover(store, "leasha://search?q=leeds")
    payload = _read_external_run(store)
    assert payload["link"].query == "leeds"
    assert _read_external_run(store)["link"] is None


def test_the_watcher_still_answers_when_there_is_no_link(store):
    from app.ui.presenter import _read_external_run

    payload = _read_external_run(store)
    assert payload["link"] is None
    assert set(payload) == {"locked", "record", "link", "front_requested"}


def test_a_broken_store_costs_the_watcher_nothing(store):
    """It runs on a timer for as long as the window is open."""
    from app.ui.presenter import _read_external_run

    class _Awkward:
        def __getattr__(self, name):
            raise RuntimeError("this database is having a day")

    payload = _read_external_run(_Awkward())
    assert payload == {
        "locked": False, "record": None, "link": None, "front_requested": False}


def test_the_watcher_also_picks_up_a_front_request(store):
    r"""Same watcher, same channel (`run_lock.FRONT_STATE_KEY`), for §ii's
    "the box is hard to get to": a second launch that found the window
    already open, rather than a link."""
    from app.core.run_lock import request_front
    from app.ui.presenter import _read_external_run

    request_front(store)
    payload = _read_external_run(store)
    assert payload["front_requested"] is True
    assert _read_external_run(store)["front_requested"] is False, (
        "taken, not read - or the window fronts itself again next poll")
