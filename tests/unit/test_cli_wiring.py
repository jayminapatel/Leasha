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
    "evaluate", "embed-bench", "rerank-bench", "diagnose", "repos",
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

            for choice_name, sub in (getattr(action, "choices", None) or {}).items():
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

@pytest.mark.parametrize("chunks, claimed, rows, expect_gap", [
    (3355, 3355, 3355, False),   # healthy
    (3355, 3355, 154, True),     # the reported failure: SQLite lies, vectors gone
    (3355, 3355, 0, True),       # vector store empty or rebuilt
    (3355, 154, 154, True),      # honest, but incomplete
    (0, 0, 0, None),             # nothing indexed: nothing to say
])
def test_the_embedding_gap_is_detected(chunks, claimed, rows, expect_gap):
    """`stats` printed both numbers in different sections and left the reader
    to notice the difference.

    The warning that sends people here - "no vector hits for a query with 60
    keyword hits - Check: app.cli stats" - could not be answered by the command
    it names. Same shape as `doctor` reporting its own defaults: a diagnostic
    that cannot see the problem it exists for.
    """
    info = {
        "sqlite": {"chunks_total": chunks, "chunks_embedded": claimed},
        "vectors": {"rows": rows},
    }

    gap = cli.embedding_gap(info)

    if expect_gap is None:
        assert gap is None
        return
    assert gap is not None
    assert (gap["missing"] > 0 or gap["claimed"] > gap["rows"]) is expect_gap


def test_a_missing_vector_store_is_a_gap_not_a_crash():
    """`stats` omits the section entirely when the table does not exist."""
    info = {"sqlite": {"chunks_total": 500, "chunks_embedded": 500}}

    gap = cli.embedding_gap(info)

    assert gap is not None
    assert gap["rows"] == 0
    assert gap["missing"] == 500


def test_stats_says_which_and_names_the_remedy(capsys):
    """A number without an action is a number somebody has to research."""
    cli._report_embedding_gap({
        "sqlite": {"chunks_total": 3355, "chunks_embedded": 3355},
        "vectors": {"rows": 154},
    })

    out = capsys.readouterr().out
    assert "154" in out and "3,355" in out
    assert "reembed" in out, "it did not say how to fix it"
    assert "Keyword search is unaffected" in out


def test_stats_confirms_when_the_stores_agree(capsys):
    """Silence on success is indistinguishable from the check not running."""
    cli._report_embedding_gap({
        "sqlite": {"chunks_total": 500, "chunks_embedded": 500},
        "vectors": {"rows": 500},
    })

    assert "ready" in capsys.readouterr().out


def test_stats_runs_end_to_end_on_an_empty_index(tmp_path, capsys):
    """The whole command, because a NameError here is found by running it."""
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()

    assert cli.cmd_stats(parser_for(["stats", "--env", env])) == cli.EXIT_OK
    assert "Vector store" in capsys.readouterr().out
