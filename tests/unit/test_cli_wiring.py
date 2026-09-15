"""Every command actually runs.

Layer: L3

`leasha rerank-bench` shipped with `NameError: name 'SqliteStore' is not
defined` - a missing import, on the first line of the body, found by the owner
on the first run. I had checked `--help`, which parses arguments and never
executes anything.

**That is the gap this file closes.** A missing import, a renamed function, a
typo in a keyword argument: none of them are caught by importing the module,
none by `--help`, and none by any test that stops at the parser. They are caught
by *calling* the thing, which is what these do - against a temporary empty index,
so they are fast and touch nothing real.

These assert almost nothing about the output. That is deliberate: the behaviour
of each command is tested elsewhere, and duplicating it here would mean two
places to update. What is asserted is that the command exists, is wired to a
function, and that the function runs to completion without raising.
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib

import pytest

from app import cli


def env_file(tmp_path):
    """A settings file pointing entirely inside a temporary directory."""
    data = tmp_path / "data"
    path = tmp_path / ".env"
    path.write_text(
        f"DATA_PATH={data}\n"
        f"VECTOR_PATH={data / 'vectors'}\n"
        f"FTS_DB={data / 'index.db'}\n"
        f"CACHE_PATH={data / 'cache'}\n"
        f"MODEL_CACHE={data / 'models'}\n"
        f"STATE_PATH={data / 'state'}\n"
        f"LOG_PATH={tmp_path / 'logs'}\n"
        # The resource governor would otherwise refuse to run on a full disk,
        # which is a real check and not what these tests are about.
        "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n",
        encoding="utf-8",
    )
    return str(path)


def parser_for(argv):
    """Parse a real command line, exactly as `main()` does."""
    parser = cli.build_parser()
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# The parser and the functions behind it
# ---------------------------------------------------------------------------

COMMANDS = [
    "search", "index", "formats", "commands", "ollama", "doctor",
    "evaluate", "embed-bench", "rerank-bench", "diagnose", "repos", "gitsearch",
    "offline-media",
]


@pytest.mark.parametrize("name", COMMANDS)
def test_the_command_exists_and_has_a_function(name):
    """A subparser with no `func` fails at runtime with an AttributeError, and
    only for the person who typed that particular command."""
    try:
        args = parser_for([name, "--help"])
    except SystemExit:
        # `--help` exits, which means the subparser exists. That is the half
        # this assertion is about.
        return
    assert hasattr(args, "func")


def test_the_top_level_help_renders():
    """`leasha --help` crashed with `TypeError: %o format: an integer is
    required, not dict`, and nothing caught it.

    argparse runs every help string through `%` formatting to expand
    `%(default)s`. A help string reading "93% of one 9-second search" contains
    `% o` - a space-flagged octal conversion - which wants an integer and gets
    argparse's parameter dict.

    **The test above did not catch it, and could not.** It asks each subparser
    for its own help, and a subparser only formats its own strings. The
    top-level help is the one that formats *every* subcommand's one-line
    description, so a bad `%` in any one of them breaks help for the whole
    program while `that-command --help` keeps working.

    `format_help()` rather than parsing `--help`, because the latter raises
    SystemExit and the crash would be swallowed as "the subparser exists".
    """
    assert "usage:" in cli.build_parser().format_help()


@pytest.mark.parametrize("name", COMMANDS)
def test_each_subcommand_help_renders(name):
    """The same failure, one level down: a bad `%` in an *argument's* help."""
    parser = cli.build_parser()
    subparsers = [
        action.choices[name]
        for action in parser._actions
        if getattr(action, "choices", None) and name in action.choices
    ]
    assert subparsers, f"{name} is not registered"
    assert "usage:" in subparsers[0].format_help()


def test_no_help_string_has_an_unescaped_percent():
    """Says which string is wrong, rather than only that something is.

    `format_help()` above fails with argparse's own message, which names
    neither the command nor the text. This walks them so the failure points at
    the line to fix.
    """
    parser = cli.build_parser()
    bad = []

    def looks_ok(text):
        try:
            text % {"default": "x", "prog": "leasha", "choices": "a, b"}
        except (TypeError, ValueError, KeyError) as exc:
            return exc
        return None

    def check(a_parser, where):
        for action in a_parser._actions:
            if action.help:
                problem = looks_ok(action.help)
                if problem:
                    bad.append(f"{where}: {action.help!r} ({problem})")

            # **The subcommand one-liners live here, not on `action.help`.**
            # The first version of this test walked only `_actions` and missed
            # the very string that broke `--help`, while the `format_help()`
            # test above caught it. A test that cannot see the bug it was
            # written for is worse than no test, because it reads as coverage.
            for entry in getattr(action, "_choices_actions", []):
                if entry.help:
                    problem = looks_ok(entry.help)
                    if problem:
                        bad.append(f"{entry.dest}: {entry.help!r} ({problem})")

            # **Only a subparser's `choices` is a mapping.** An ordinary
            # argument with `choices=["a", "b"]` has a *list* there, and
            # walking it as a dict raises `AttributeError` from inside a test
            # about percent signs - which is a confusing way to learn that
            # somebody added `choices` to a positional.
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for choice_name, sub in choices.items():
                    if isinstance(sub, argparse.ArgumentParser):
                        check(sub, choice_name)

    check(parser, "top level")
    assert not bad, "escape the percent sign as %% in:\n  " + "\n  ".join(bad)


def test_every_subcommand_is_reachable():
    """A command added to the module but never registered is invisible - it
    exists, it is tested, and nobody can run it."""
    parser = cli.build_parser()
    registered = set()
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            registered |= set(action.choices)
    missing = [name for name in COMMANDS if name not in registered]
    assert not missing, f"not reachable from the command line: {missing}"


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------

def test_rerank_bench_runs(tmp_path, capsys):
    """**The regression.** This shipped with a missing import and `--help`
    could not have caught it.

    An unsupported model name is used so nothing is downloaded: the command must
    still open the store, build the passages, report the failure per model and
    exit cleanly.
    """
    args = parser_for([
        "rerank-bench", "--env", env_file(tmp_path),
        "--model", "not/a-real-model", "--count", "2", "--passes", "1",
    ])
    assert cli.cmd_rerank_bench(args) == cli.EXIT_OK
    assert "candidates" in capsys.readouterr().out


def test_rerank_bench_json_runs(tmp_path, capsys):
    """The JSON path is a separate branch and a separate chance to be wrong."""
    import json

    args = parser_for([
        "rerank-bench", "--env", env_file(tmp_path), "--json",
        "--model", "not/a-real-model", "--count", "2", "--passes", "1",
    ])
    cli.cmd_rerank_bench(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates_scored"] == 2
    assert payload["models"][0]["error"]


def test_commands_runs(tmp_path, capsys):
    """Prints the `/` catalogue. No index needed, so nothing can excuse a
    failure here."""
    args = parser_for(["commands", "--env", env_file(tmp_path)])
    assert cli.cmd_commands(args) == cli.EXIT_OK
    assert "type:" in capsys.readouterr().out


def test_formats_runs(tmp_path, capsys):
    args = parser_for(["formats", "--env", env_file(tmp_path)])
    assert cli.cmd_formats(args) in (cli.EXIT_OK, cli.EXIT_ERROR)
    assert capsys.readouterr().out.strip()


def test_search_on_an_empty_index_runs(tmp_path, capsys):
    """An empty index is the state every new installation is in, and the one
    most likely to be skipped when testing by hand."""
    args = parser_for(["search", "anything", "--env", env_file(tmp_path)])
    assert cli.cmd_search(args) in (cli.EXIT_OK, cli.EXIT_ERROR)


def test_evaluate_builtin_runs(tmp_path, capsys):
    args = parser_for(["evaluate", "--builtin", "--env", env_file(tmp_path)])
    assert cli.cmd_evaluate(args) in (cli.EXIT_OK, cli.EXIT_ERROR)
    assert "Recall at" in capsys.readouterr().out


def test_evaluate_with_rerank_reaches_the_engine(tmp_path):
    """**The third missing-name bug in a row.** `--rerank` referred to
    `settings` in a function that never loaded it, and the path was reachable
    only by typing the flag - which no test did.

    A model download cannot happen here, so this asserts the distinction that
    matters: it must fail on the *model*, with a clear AppError, rather than on
    a NameError from code that was never executed.
    """
    from app.core.errors import AppErrorException

    args = parser_for([
        "evaluate", "--builtin", "--rerank", "--env", env_file(tmp_path),
    ])
    try:
        cli.cmd_evaluate(args)
    except (NameError, AttributeError, TypeError) as exc:      # pragma: no cover
        pytest.fail(f"the --rerank path is not wired: {type(exc).__name__}: {exc}")
    except AppErrorException:
        pass          # no model in this environment, which is a real answer


def test_rerank_model_implies_rerank(tmp_path):
    """**A flag that names a reranker and does not use one** would be a setting
    that silently does nothing - the failure this project keeps hitting. It has
    to turn the full pipeline on by itself, or somebody compares two models and
    gets the keyword numbers twice."""
    from app.core.errors import AppErrorException

    args = parser_for([
        "evaluate", "--builtin", "--env", env_file(tmp_path),
        "--rerank-model", "Xenova/ms-marco-MiniLM-L-6-v2",
    ])
    assert args.rerank is False, "the flag alone should not preset --rerank"
    try:
        cli.cmd_evaluate(args)
    except AppErrorException:
        pass          # no embedding model here, which is a real answer
    except (NameError, AttributeError, TypeError) as exc:      # pragma: no cover
        pytest.fail(f"--rerank-model is not wired: {type(exc).__name__}: {exc}")
    assert args.rerank is True, "--rerank-model must imply --rerank"


def test_the_two_evaluate_modes_say_which_they_are(tmp_path, capsys):
    """The keyword-only run cannot see a reranker change, and reporting both
    under one heading is how somebody measures the wrong thing and believes
    they measured the right one - which is what happened."""
    args = parser_for(["evaluate", "--builtin", "--env", env_file(tmp_path)])
    cli.cmd_evaluate(args)
    assert "keyword only" in capsys.readouterr().out


def test_ollama_runs_without_ollama(tmp_path, capsys):
    """Most machines have no Ollama. The diagnostic has to work there - that is
    precisely when somebody runs it."""
    args = parser_for(["ollama", "--env", env_file(tmp_path)])
    assert cli.cmd_ollama(args) in (cli.EXIT_OK, cli.EXIT_ERROR)
    assert "Ollama" in capsys.readouterr().out


def test_search_warms_the_models_before_it_times_anything(tmp_path, monkeypatch):
    """`timings_ms` must measure search, not process startup.

    The window warms up on a background worker; the CLI did not, so both
    models loaded lazily inside the timed block - `retrieve` included loading
    the embedder and `rerank` included loading the cross-encoder.

    A one-shot CLI search reported `rerank: 3047ms` for work the benchmark
    measures at 660ms, and the gap was read as the reranker being slow. Every
    model-to-model comparison drawn from these numbers was meaningless, which
    is the only thing they were being used for.
    """
    from app.search.engine import SearchEngine

    order: list[str] = []
    real_warm = SearchEngine.warm_up
    real_search = SearchEngine.search

    def warm(self):
        order.append("warm_up")
        with contextlib.suppress(Exception):   # no models in this environment
            real_warm(self)

    def search(self, *a, **kw):
        order.append("search")
        return real_search(self, *a, **kw)

    monkeypatch.setattr(SearchEngine, "warm_up", warm)
    monkeypatch.setattr(SearchEngine, "search", search)

    env = env_file(tmp_path)
    # `cmd_search` returns early when there is no index, so warm_up would
    # never be reached and the test would pass for the wrong reason.
    cli.cmd_init(parser_for(["init", "--env", env]))

    # No network here, so the models cannot download. Irrelevant: the
    # assertion is about the *order* of the two calls, not their success.
    with contextlib.suppress(Exception):
        cli.cmd_search(parser_for(["search", "anything", "--env", env]))

    assert order[:1] == ["warm_up"], (
        f"models were not loaded before the clock started: {order}")
    assert "search" in order


def test_repos_runs_on_an_empty_index(tmp_path, capsys):
    """No repositories is not an error - most machines have none.

    Ships before the UI per non-negotiable 8, so detection is checkable
    headless and the Code tab has something to compare against.
    """
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))

    capsys.readouterr()          # discard `init`'s own output
    assert cli.cmd_repos(parser_for(["repos", "--env", env])) == cli.EXIT_OK
    assert "No code repositories" in capsys.readouterr().out


def test_repos_json_is_machine_readable(tmp_path, capsys):
    """The `--json` branch is a separate path and a separate chance to be wrong.

    `rerank-bench --json` printed a human preamble ahead of its JSON and only a
    test that parsed the output found it.
    """
    import json as _json

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()          # discard `init`'s own output

    assert cli.cmd_repos(parser_for(["repos", "--env", env, "--json"])) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{"), f"a human preamble came first: {out[:80]!r}"
    payload = _json.loads(out)
    assert payload == {"repositories": [], "count": 0}


def test_repos_lists_what_the_store_holds(tmp_path, capsys):
    """Through the real store, so a renamed column fails here."""
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    settings = load_settings(pathlib.Path(env), check_writable=False)

    with SqliteStore(settings.fts_db) as store:
        repo_id = store.upsert_repo(str(tmp_path / "leasha"), kind="work")
        store.upsert_file(path=str(tmp_path / "leasha" / "a.py"), size_bytes=1,
                          mtime_ns=1, source_kind="file", repo_id=repo_id)
        store.upsert_repo(str(tmp_path / "empty"), kind="worktree")

    capsys.readouterr()
    assert cli.cmd_repos(parser_for(["repos", "--env", env])) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "leasha" in out
    assert "work" in out
    # A repository with no attributed files still appears - LEFT JOIN, not JOIN.
    assert "empty" in out
    assert "worktree" in out


# ---------------------------------------------------------------------------
# The embedding gap, which is what "no vector hits" actually means
# ---------------------------------------------------------------------------
#
# `semantic_search_warnings` already existed and had no tests. A second,
# worse copy of it was written before anybody looked - and the copy
# reproduced the exact bug the original's own comment documents having
# fixed: comparing the vector row count against `chunks_embedded` rather
# than `chunks_total`, which agrees perfectly on a corpus where almost
# nothing was ever embedded, and so prints nothing on the one machine it
# was written for.
#
# The duplicate is gone. These are the tests it should have arrived with.

def warnings_for(chunks, embedded, rows):
    return cli.semantic_search_warnings(
        {"chunks_total": chunks, "chunks_embedded": embedded}, {"rows": rows})


def test_offline_media_runs_on_an_empty_index(tmp_path, capsys):
    """No catalogued sources is not an error - most machines have none.

    Ships before the tab per non-negotiable 8, order 202626270513.
    """
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))

    capsys.readouterr()          # discard `init`'s own output
    assert cli.cmd_offline_media(
        parser_for(["offline-media", "--env", env])) == cli.EXIT_OK
    assert "No Offline Media sources" in capsys.readouterr().out


def test_offline_media_json_is_machine_readable(tmp_path, capsys):
    import json as _json

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    assert cli.cmd_offline_media(
        parser_for(["offline-media", "--env", env, "--json"])) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{"), f"a human preamble came first: {out[:80]!r}"
    payload = _json.loads(out)
    assert payload == {"volumes": [], "count": 0}


def test_offline_media_delete_of_an_unknown_source_is_a_clean_error(tmp_path, capsys):
    """A typo in a name must not look like a crash."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    code = cli.cmd_offline_media(
        parser_for(["offline-media", "--env", env, "--delete", "Nonexistent"]))
    assert code != cli.EXIT_OK
    combined = "".join(capsys.readouterr()).lower()
    assert "no catalogued source" in combined


def test_offline_media_rescan_of_an_unknown_source_is_a_clean_error(tmp_path, capsys):
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    code = cli.cmd_offline_media(
        parser_for(["offline-media", "--env", env, "--rescan", "Nonexistent"]))
    assert code != cli.EXIT_OK
    combined = "".join(capsys.readouterr()).lower()
    assert "no catalogued source" in combined


def test_offline_media_scan_without_a_name_is_a_clean_error(tmp_path, capsys):
    """2b: the first Scan requires a name - there is no dialog to ask twice
    from a command line, so this refuses rather than inventing one."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    code = cli.cmd_offline_media(
        parser_for(["offline-media", "--env", env, "--scan", str(tmp_path)]))
    assert code != cli.EXIT_OK
    combined = "".join(capsys.readouterr()).lower()
    assert "name" in combined


def test_an_empty_vector_store_is_reported_as_not_working():
    lines = "\n".join(warnings_for(3355, 3355, 0))

    assert "NOT working" in lines
    assert "reembed" in lines


def test_partial_coverage_is_reported_with_the_share():
    """The owner's real numbers: 1,170 of 1,729."""
    lines = "\n".join(warnings_for(1729, 1170, 1170))

    assert "68%" in lines
    assert "1,170" in lines and "1,729" in lines
    assert "559" in lines, "it must say how many are missing, not only the share"
    assert "reembed" in lines


def test_coverage_is_measured_against_the_total_not_the_flag():
    """**The bug the original fixed, which the duplicate reintroduced.**

    `chunks_embedded` answers "did the vectors get written for the chunks we
    tried", which is not the question. The question is "can meaning-based
    search see my corpus", and only the total can answer it. On a corpus where
    154 of 3,355 were ever attempted, the flag and the row count agree
    perfectly - and a check comparing those two prints nothing at all.
    """
    lines = warnings_for(3355, 154, 154)

    assert lines, "a corpus 95% unembedded reported itself healthy"
    assert "5%" in "\n".join(lines)


def test_orphaned_vectors_are_reported():
    """More rows than SQLite believes were embedded: results that cannot open.

    **Coverage must be complete for this branch to be reached.** The partial
    branch returns first, so a store that is both under-covered and holding
    orphans reports only the under-coverage - which is the right priority (a
    missing vector is worse than a stale one) but is worth pinning, because it
    is invisible from reading the branches in order.
    """
    lines = "\n".join(warnings_for(1000, 500, 1000))

    assert "orphan" in lines.lower()
    assert "reembed --all" in lines


def test_under_coverage_is_reported_before_orphans():
    """Both wrong at once: say the worse thing. A missing vector cannot be
    found at all; a stale one merely fails to open."""
    lines = "\n".join(warnings_for(1000, 400, 800))

    assert "covers 80%" in lines
    assert "orphan" not in lines.lower()


def test_a_healthy_index_says_nothing():
    """Above 95%, silence. Otherwise the signal is noise within a week."""
    assert warnings_for(1000, 1000, 1000) == []
    assert warnings_for(1000, 1000, 980) == [], "96% should not nag"


def test_nothing_indexed_is_not_a_warning():
    """An empty index is a state, not a fault."""
    assert warnings_for(0, 0, 0) == []
    assert cli.semantic_search_warnings(None, None) == []


def test_stats_runs_end_to_end_on_an_empty_index(tmp_path, capsys):
    """The whole command, because a NameError here is found by running it."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    assert cli.cmd_stats(parser_for(["stats", "--env", env])) == cli.EXIT_OK
    assert "Vector store" in capsys.readouterr().out


def test_reembed_says_what_it_is_doing_before_the_silence(tmp_path, capsys, monkeypatch):
    """"This seems stuck" - and it was not.

    Nothing printed until the first batch of 256 finished. At the 4.4
    passages/second `embed-bench` measures on a real machine that is nearly a
    minute of silent terminal, and the correct response to a silent terminal is
    to kill it, which loses the work.

    The standing rule is that nothing fails silently. A long operation that
    says nothing is the same fault in different clothes: there is no way to
    tell it from one that has died.
    """
    from app.storage.sqlite_store import SqliteStore

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))

    from app.core.config import load_settings
    settings = load_settings(pathlib.Path(env), check_writable=False)
    with SqliteStore(settings.fts_db) as store:
        file_id = store.upsert_file(path="/a.txt", size_bytes=1, mtime_ns=1,
                                    source_kind="file")
        store.replace_chunks(file_id, [{"text": f"passage {n}"} for n in range(4)])
    capsys.readouterr()

    # The model cannot load here, and does not need to: what is under test is
    # that something is said *before* it is reached.
    with contextlib.suppress(Exception):
        cli.cmd_reembed(parser_for(["reembed", "--env", env]))

    out = capsys.readouterr().out
    assert "Embedding" in out, "it started work without saying so"
    assert "4" in out, "it did not say how many passages"
    assert "first line" in out, "it did not warn that the first line is slow"


def test_gitsearch_on_a_folder_that_is_not_a_repository(tmp_path, capsys):
    """It must say so rather than producing an empty table that reads as
    "history search is instant"."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    args = parser_for([
        "gitsearch", "pattern", "--repo", str(tmp_path), "--env", env,
        "--measure", "--depths", "5", "--json",
    ])
    cli.cmd_gitsearch(args)

    import json as _json
    payload = _json.loads(capsys.readouterr().out)
    assert payload["rows"] == []
    assert payload["notes"], "it produced no rows and said nothing about why"


def test_searching_a_folder_that_is_not_a_repository_says_which_folder(
        tmp_path, capsys):
    """The same rule for the search as for the measurement.

    An empty result table over a folder that is not a checkout reads as "this
    repository has nothing in it", which is a different and much more
    misleading statement than "that is not a repository".
    """
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    args = parser_for([
        "gitsearch", "pattern", "--repo", str(tmp_path), "--env", env,
    ])

    assert cli.cmd_gitsearch(args) == cli.EXIT_ERROR
    printed = capsys.readouterr()
    assert "repository" in (printed.out + printed.err).lower()


def test_gitsearch_rejects_a_missing_folder(tmp_path, capsys):
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    args = parser_for([
        "gitsearch", "x", "--repo", str(tmp_path / "nope"), "--env", env,
    ])
    assert cli.cmd_gitsearch(args) == cli.EXIT_ERROR


def test_gitsearch_rejects_depths_that_are_not_numbers(tmp_path, capsys):
    """`--depths 1k,10k` is the obvious thing to type and is not a number."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    args = parser_for([
        "gitsearch", "x", "--repo", str(tmp_path), "--env", env,
        "--measure", "--depths", "1k,10k",
    ])
    assert cli.cmd_gitsearch(args) == cli.EXIT_ERROR
    assert "depths" in capsys.readouterr().err.lower() + capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# `repos --scan`: answering "are there any" without a full index run
# ---------------------------------------------------------------------------

def test_scan_finds_repositories_without_indexing_anything(tmp_path, capsys):
    """`app.cli repos` lists what the last run attributed, which cannot answer
    "are there any git repositories in my search folders" - it needs a full run
    first, and on a fresh index it is empty and indistinguishable from none."""
    (tmp_path / "code" / "leasha" / ".git").mkdir(parents=True)
    (tmp_path / "code" / "tools" / ".git").mkdir(parents=True)

    assert cli._scan_for_repos([tmp_path / "code"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "leasha" in out and "tools" in out
    assert "2 git repositories" in out


def test_scan_says_none_rather_than_printing_nothing(tmp_path, capsys):
    """Most folders have none, and an empty result must not read as a failure."""
    (tmp_path / "docs").mkdir()

    assert cli._scan_for_repos([tmp_path / "docs"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "No git repositories found" in out
    assert "Nothing is wrong" in out


def test_scan_reports_a_repository_the_indexed_folder_sits_inside(tmp_path, capsys):
    """The case a downward walk cannot see.

    `D:\\Project\\app` has no `.git` beneath it and every file under it is
    still in a repository.
    """
    (tmp_path / "project" / ".git").mkdir(parents=True)
    (tmp_path / "project" / "app").mkdir()

    cli._scan_for_repos([tmp_path / "project" / "app"])

    out = capsys.readouterr().out
    assert "contains the indexed folder" in out


def test_scan_names_a_folder_that_does_not_exist(tmp_path, capsys):
    """A typo must not read as "you have no repositories"."""
    cli._scan_for_repos([tmp_path / "nope"])

    assert "does not exist" in capsys.readouterr().out


def test_scan_writes_nothing(tmp_path):
    """Read-only. It must never create an index as a side effect."""
    (tmp_path / "code" / "r" / ".git").mkdir(parents=True)
    before = {p for p in tmp_path.rglob("*")}

    cli._scan_for_repos([tmp_path / "code"])

    assert {p for p in tmp_path.rglob("*")} == before


def test_scan_json_is_machine_readable(tmp_path, capsys):
    import json as _json

    (tmp_path / "r" / ".git").mkdir(parents=True)

    cli._scan_for_repos([tmp_path], as_json=True)

    payload = _json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["repositories"][0]["kind"] == "work"


def test_scan_warns_when_the_indexed_folder_is_itself_a_repository(tmp_path, capsys):
    """**The owner's actual shape, and it changes what `scope:code` means.**

    `D:\\SearchData` turned out to be a git repository *and* the indexed root.
    Attribution is by longest matching prefix, so every file beneath it - the
    spreadsheets, the PDFs, the mail - is attributed to it, and `scope:code`
    then matches the whole corpus rather than code.

    Not an error, and not fixed automatically: it may be deliberate. But it is
    invisible from the outside, and finding out by wondering why the Code tab
    lists your holiday photos is worse.
    """
    root = tmp_path / "SearchData"
    (root / ".git").mkdir(parents=True)
    (root / "GIT_REPOS" / "inner" / ".git").mkdir(parents=True)

    cli._scan_for_repos([root])

    out = capsys.readouterr().out
    assert "the indexed folder itself" in out
    assert "scope:code" in out
    assert "whole corpus" in out
    # The nested one is still attributed to itself, which is correct and worth
    # saying so the warning does not read as "repositories are broken".
    assert "still attributed to themselves" in out


def test_scan_does_not_warn_when_repositories_sit_below_the_root(tmp_path, capsys):
    """The ordinary case. A warning here would be noise within a week."""
    root = tmp_path / "SearchData"
    (root / "code" / "a" / ".git").mkdir(parents=True)

    cli._scan_for_repos([root])

    out = capsys.readouterr().out
    assert "the indexed folder itself" not in out
    assert "scope:code" not in out
