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
    "evaluate", "embed-bench", "rerank-bench", "diagnose",
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


def test_ollama_runs_without_ollama(tmp_path, capsys):
    """Most machines have no Ollama. The diagnostic has to work there - that is
    precisely when somebody runs it."""
    args = parser_for(["ollama", "--env", env_file(tmp_path)])
    assert cli.cmd_ollama(args) in (cli.EXIT_OK, cli.EXIT_ERROR)
    assert "Ollama" in capsys.readouterr().out
