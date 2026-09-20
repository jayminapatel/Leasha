"""`evaluate`: measuring whether plain sentences find the right documents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report
from app.core.errors import make_error
from app.core.logging import setup_logging


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Does a plain sentence find the right document? Ask twenty and count.

    Two corpora, and the difference matters.

    **`--builtin`** uses a small corpus with known answers, shipped with the
    application. It answers "does the mechanism work" and "did today's change
    break something", and it needs nothing but the code.

    **The default** runs against your own index, from a file of your own
    questions - one per line, `sentence | fragment-of-the-wanted-path`. That is
    the measurement that actually matters, because a real archive has
    near-duplicates, inconsistent naming and years of drift that no fixture
    reproduces. Every number from the built-in corpus is optimistic.

    Recall is reported **split by whether the sentence carried a constraint**.
    One number cannot distinguish "search is bad" from "search is fine at topics
    and blind to constraints", and those have completely different fixes.
    """
    if getattr(args, "chat", False):
        # Work order 202626270611 section 4: the Chat engine's own measurement.
        from app.chat.evaluate import run_cli

        return run_cli(args)

    from app.search.evaluate import evaluate

    settings = _load(args)
    setup_logging(settings.log_path)

    if args.builtin:
        return _evaluate_builtin(args, evaluate)

    questions = _read_questions(Path(args.questions)) if args.questions else []
    if not questions:
        print("Give a file of questions, or use --builtin.")
        print()
        print("  One per line:   sentence | part-of-the-wanted-path")
        print("  For example:    the safety report Dave sent | leeds-safety")
        print()
        print(r"  venv\Scripts\python.exe -m app.cli evaluate --questions mine.txt")
        print(r"  venv\Scripts\python.exe -m app.cli evaluate --builtin")
        return EXIT_ERROR

    from app.index.embedder import Embedder
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        engine = SearchEngine(
            store, vectors,
            Embedder.from_settings(settings),
            # **`from_settings` also fixes an inconsistency worth naming.**
            # Three of the five construction sites left `RERANK_TOP_N` and
            # `RERANK_WINDOW_CHARS` at the module defaults, so two controls in
            # Settings applied in the window and not on the command line. One
            # constructor means one answer.
            reranker=Reranker.from_settings(settings),
            # Work order 0h §1c: the third retrieval lane, same as every
            # other real search entry point in this file.
            image_vectors=image_vectors,
            clip_text_embedder=vector.clip_text_embedder_from_settings(settings),
        )

        def search(query: str) -> list[str]:
            return [result.path for result in engine.search(query, limit=args.k).results]

        translate = None
        if args.interpret:
            from app.llm.ollama import OllamaClient
            from app.search.translate import QueryTranslator

            translator = QueryTranslator(
                OllamaClient(settings.ollama_url, settings.ollama_model)
            )
            if not translator.available():
                print("Ollama is not answering, so --interpret would measure nothing.")
                print(r"  Check it: venv\Scripts\python.exe -m app.cli ollama")
                return EXIT_ERROR
            translate = lambda sentence: translator.translate(sentence).query  # noqa: E731

        report = evaluate(
            questions, search, k=args.k,
            mode="your index, interpreted" if args.interpret else "your index",
            translate=translate,
        )

    for line in report.lines():
        print(line)
    if args.json:
        print()
        print(json.dumps(report.as_dict(), indent=2))
    return EXIT_OK


def _read_questions(path: Path) -> list:
    """`sentence | expected-path-fragment | optional-constraint` per line."""
    from app.search.evaluate import Question

    questions = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"Could not read {path}: {exc}")
        return []

    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print(f"  line {number} ignored - expected 'sentence | path-fragment'")
            continue
        questions.append(Question(
            sentence=parts[0], expects=parts[1],
            constraint=parts[2] if len(parts) > 2 else "",
        ))
    return questions


def _evaluate_builtin(args: argparse.Namespace, evaluate: Any) -> int:
    """The shipped corpus. Keyword only by default; `--rerank` for the lot.

    Keyword-only is the half that needs no model, so it runs anywhere and
    measures the same thing every time - which is exactly why it cannot see a
    reranker or embedding change. `--rerank` builds the real engine for that,
    and says so in the heading so the two are never confused.

    The vector half is what the
    `--questions` mode against a real index exercises.
    """
    import tempfile

    from app.search import keyword
    from app.search.commands import expand_slashes
    from app.search.query import parse_query
    from app.storage.sqlite_store import SqliteStore

    try:
        from tests.fixtures.evaluation import CORPUS, QUESTIONS, load_into
    except ImportError:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.evaluate", key="tests",
            reason="the built-in corpus ships with the tests, which are not installed",
            suggestion="Run from a source checkout, or use --questions with your own.",
        ), args.json)

    folder = Path(tempfile.mkdtemp())
    with SqliteStore(folder / "evaluate.db") as store:
        load_into(store)

        # **`--rerank` is the only way to measure a reranker change.**
        #
        # Without it this calls the keyword retriever directly - no vectors, no
        # fusion, no cross-encoder - which is a perfectly good measurement of
        # BM25 and completely blind to the thing it was reached for. The owner
        # ran it to judge a reranker swap on my advice, and it could not have
        # detected one: the numbers came back identical because they measure a
        # stage the reranker never touches.
        mode = "built-in corpus, keyword only"
        # A flag that names a reranker and then does not use one is a setting
        # that silently does nothing - the failure this project keeps hitting.
        if getattr(args, "rerank_model", None):
            args.rerank = True
        if args.rerank:
            settings = _load(args)
            # **Comparing two models must not require editing a config file.**
            # The instruction "put this line in .env" was pasted into PowerShell
            # as a command, which is a fair reading of a line in a code block -
            # and even done correctly it is an edit, a save and a reread between
            # every measurement. One flag is the whole comparison.
            if getattr(args, "rerank_model", None):
                settings = settings.model_copy(update={"rerank_model": args.rerank_model})
            from app.index.embedder import Embedder
            from app.search.engine import SearchEngine
            from app.search.rerank import Reranker
            from app.storage.vector_store import VectorStore

            vectors = VectorStore(folder / "vectors", dim=settings.embed_dim)
            vectors.connect()
            embedder = Embedder.from_settings(settings)

            # **The vectors have to be built or this is not the full pipeline.**
            #
            # `load_into` writes to SQLite only, so the first version of this
            # ran keyword + an *empty* vector store + rerank, warned "no vector
            # hits" on all twenty questions, and still called itself "full
            # pipeline" in the heading. That is a label the measurement did not
            # earn, and the warnings were the evidence sitting right there.
            #
            # Twenty-one documents, so this costs a second.
            chunks = list(store.conn.execute(
                "SELECT id, file_id, text FROM chunks ORDER BY id"))
            if chunks:
                vectors.add(
                    chunk_ids=[row["id"] for row in chunks],
                    file_ids=[row["file_id"] for row in chunks],
                    vectors=list(embedder.embed_all([row["text"] for row in chunks])),
                )
                store.mark_embedded(row["id"] for row in chunks)

            engine = SearchEngine(
                store, vectors,
                embedder,
                # `enabled=True` explicitly: this path measures the reranker,
                # so the switch that turns it off for searching must not turn
                # off the thing being measured.
                reranker=Reranker.from_settings(settings, enabled=True),
                log_usage=False,
            )
            # The model is named because `.env` overrides the shipped default,
            # and two runs of the same model look exactly like two runs of
            # different ones if the heading does not say.
            mode = f"built-in corpus, full pipeline, {settings.rerank_model}"

            def search(query: str) -> list[str]:
                response = engine.search(expand_slashes(query), limit=args.k, rerank=True)
                return [result.path for result in response.results]
        else:
            def search(query: str) -> list[str]:
                parsed = parse_query(expand_slashes(query))
                return [hit["path"] for hit in keyword.search(store, parsed, limit=args.k)]

        report = evaluate(
            QUESTIONS, search, k=args.k, mode=mode,
            note=f"{len(CORPUS)} documents. A small clean corpus with no "
                 "near-duplicates - every number here is optimistic. Your own "
                 "questions against your own index are the measurement that counts.",
        )

    for line in report.lines():
        print(line)
    if args.json:
        print()
        print(json.dumps(report.as_dict(), indent=2))
    return EXIT_OK


def add_evaluate_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_eval = sub.add_parser(
        "evaluate", parents=[common],
        help="measure whether plain sentences find the right documents")
    p_eval.add_argument("--questions", metavar="FILE",
                        help="your own: 'sentence | part-of-the-wanted-path' per line")
    p_eval.add_argument("--builtin", action="store_true",
                        help="use the shipped corpus with known answers (no model needed)")
    p_eval.add_argument("--interpret", action="store_true",
                        help="translate each sentence with Ollama first, to measure the gain")
    p_eval.add_argument(
        "--rerank-model", metavar="NAME",
        help="compare a different reranker without editing .env, e.g. "
             "Xenova/ms-marco-MiniLM-L-6-v2 - implies --rerank")
    p_eval.add_argument(
        "--rerank", action="store_true",
        help="run the full pipeline instead of keyword only - the only way to "
             "measure a reranker or embedding change")
    p_eval.add_argument("--k", type=int, default=1, metavar="N",
                        help="count a hit if the document is in the top N (default 1 - "
                             "did it come FIRST? higher numbers flatter the result)")
    p_eval.add_argument(
        "--chat", action="store_true",
        help="measure the Chat engine on the shipped fixture: citation validity, "
             "extractive correctness, exact counts, honest absence, latency")
    p_eval.add_argument(
        "--chat-model", metavar="NAME",
        help="with --chat: the Ollama model that answers (default: the configured one)")
    p_eval.add_argument(
        "--chat-fake", action="store_true",
        help="with --chat: use the deterministic FakeLLM instead of a real model")
    p_eval.add_argument(
        "--chat-ids", metavar="L01,A03",
        help="with --chat: only these question ids (a real model is slow)")
    p_eval.add_argument(
        "--chat-conversation", action="store_true",
        help="with --chat: also play the scripted multi-turn conversations (greeting, "
             "general question, follow-ups, an archive question, 'shorter', regenerate) "
             "and print the transcript")
    p_eval.add_argument(
        "--chat-conversation-only", action="store_true",
        help="with --chat: play only the scripted conversations")
    p_eval.add_argument(
        "--chat-runs", type=int, default=1, metavar="N",
        help="with --chat: run the question set N times, so latency comes with its spread")
    p_eval.set_defaults(func=cmd_evaluate)
