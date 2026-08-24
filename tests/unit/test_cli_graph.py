"""Layer 6's CLI entry point.

The graph itself is covered by `tests/integration/test_layer6_acceptance.py`.
What is tested here is the wiring: arguments, the single-instance lock, and what
the command says when there is nothing to show yet - which is the state every
new user hits first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.cli import build_parser, main
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def env(temp_env: Path) -> list[str]:
    return ["--env", str(temp_env)]


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def seed(env_args: list[str], texts: list[str]) -> None:
    """Put chunks in the index the CLI will open."""
    from app.core.config import load_settings

    settings = load_settings(Path(env_args[1]))
    with SqliteStore(settings.fts_db) as store:
        for ordinal, text in enumerate(texts):
            file_id = store.upsert_file(
                f"D:/docs/doc{ordinal}.txt", size_bytes=len(text), mtime_ns=1,
                ext="txt", parent_dir="D:/docs", source_kind="file",
            )
            store.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                            "char_end": len(text), "page": None}])


#: Two clusters, deliberately. A corpus where every entity appears in every
#: chunk has *no* information in it - npmi is 0 by definition - so the graph
#: correctly comes out empty and the test would be measuring nothing.
CORPUS = [
    "Acme Water Ltd completed the HACCP review at Barnsley Dairy.",
    "SCADA and MES at Barnsley Dairy, installed by Acme Water Ltd.",
    "Jenny Okonkwo signed the HACCP audit for Barnsley Dairy with Acme Water Ltd.",
    "Northgate Systems delivered the MES at Leeds Bakery.",
    "Leeds Bakery OEE reporting came from the MES built by Northgate Systems.",
    "Northgate Systems supported the Leeds Bakery go live.",
]


# --- arguments --------------------------------------------------------------

def test_graph_accepts_the_documented_flags() -> None:
    args = build_parser().parse_args([
        "graph", "--rebuild", "--enrich", "--top", "50",
        "--entity", "Acme Water Ltd", "--html", "out.html",
        "--min-weight", "3", "--min-pmi", "0.2", "--quiet",
    ])
    assert args.rebuild and args.enrich and args.quiet
    assert args.top == 50
    assert args.entity == "Acme Water Ltd"
    assert args.html == "out.html"
    assert args.min_weight == 3
    assert args.min_pmi == pytest.approx(0.2)


def test_the_defaults_match_the_builder_defaults() -> None:
    """Two places to change a threshold is one place to forget."""
    from app.graph.builder import GraphBuilder

    args = build_parser().parse_args(["graph"])
    builder = GraphBuilder(store=None)  # type: ignore[arg-type]
    assert args.min_weight == builder.min_weight
    assert args.min_pmi == builder.min_npmi


def test_json_works_after_the_subcommand() -> None:
    """The shape everyone types, and the one that used to be rejected."""
    assert build_parser().parse_args(["graph", "--json"]).json is True


# --- an empty index ---------------------------------------------------------

def test_an_empty_index_says_what_to_do_next(capsys, env: list[str]) -> None:
    code, out, _err = run(capsys, *env, "graph", "--quiet")
    assert code == 0
    assert "Nothing yet" in out
    assert "app.cli index" in out, "the fix should be the command to type"


# --- building ---------------------------------------------------------------

def test_graph_builds_and_lists_what_it_found(capsys, env: list[str]) -> None:
    seed(env, CORPUS)
    code, out, _err = run(capsys, *env, "graph", "--quiet")
    assert code == 0
    assert "Barnsley Dairy" in out
    assert "Entities" in out


def test_json_output_is_machine_readable(capsys, env: list[str]) -> None:
    seed(env, CORPUS)
    code, out, _err = run(capsys, *env, "graph", "--quiet", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["build"]["chunks_processed"] == len(CORPUS)
    assert payload["graph"]["entities"] > 0


def test_show_only_does_not_build(capsys, env: list[str]) -> None:
    seed(env, CORPUS)
    code, out, _err = run(capsys, *env, "graph", "--show-only", "--quiet", "--json")
    assert code == 0
    assert "build" not in json.loads(out)


def test_entity_shows_connections_and_the_documents_behind_them(
    capsys, env: list[str]
) -> None:
    seed(env, CORPUS)
    run(capsys, *env, "graph", "--quiet")
    code, out, _err = run(capsys, *env, "graph", "--show-only", "--entity", "Barnsley Dairy")
    assert code == 0
    assert "connected to" in out
    assert "Seen in:" in out
    assert "doc0.txt" in out


def test_an_unknown_entity_says_so_rather_than_showing_nothing(
    capsys, env: list[str]
) -> None:
    seed(env, CORPUS)
    run(capsys, *env, "graph", "--quiet")
    code, out, _err = run(capsys, *env, "graph", "--show-only", "--entity", "Nowhere Plc")
    assert code == 0
    assert "No entity named" in out


def test_html_writes_a_self_contained_page(capsys, env: list[str], tmp_path: Path) -> None:
    seed(env, CORPUS)
    out_path = tmp_path / "graph.html"
    code, out, _err = run(capsys, *env, "graph", "--quiet", "--html", str(out_path))

    assert code == 0
    assert out_path.exists()
    body = out_path.read_text(encoding="utf-8")
    assert re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', body) == []
    assert "works offline" in out
