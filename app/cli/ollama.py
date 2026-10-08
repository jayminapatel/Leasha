"""`ollama`: checking the optional Ollama connection.

Layer: L8 (`app.llm.ollama`; never in the search path - non-negotiable 1)

Exit codes: 1 at the first question that fails, so a script can stop there;
0 only when Ollama answered, and - with `--translate` - produced a query.
"""

from __future__ import annotations

import argparse
import json

from app.cli._common import EXIT_ERROR, EXIT_OK, _load
from app.core.logging import setup_logging


def cmd_ollama(args: argparse.Namespace) -> int:
    """Why is Ollama not working? Four questions, answered separately.

    Each has a different fix, so a single "up / down" would send people looking
    in the wrong place - which is exactly what happened: the service was running,
    `/api/tags` answered, and enrichment then spent 200 seconds discovering that
    the model was not installed.

    **Ollama is optional and has exactly one job: the Interpret button.** It
    turns a sentence into a query, which then goes into the search box for you
    to read and edit. Plain Enter never touches it, so search works perfectly
    with Ollama switched off - and this command says so, because it is the
    natural place to look when "search did not work" and the wrong one.

    It used to type knowledge-graph entities. The graph was removed, and this
    docstring said otherwise for a while, which is its own small lesson about
    diagnostics: a stale one sends people to the wrong place with confidence.
    """
    from app.llm.ollama import OllamaClient

    settings = _load(args)
    setup_logging(settings.log_path)

    # `--model` overrides without editing .env, so a model can be tried before
    # it is committed to. The whole point of the flag: the choice is a speed
    # decision, and a speed decision needs a measurement rather than a guess.
    wanted = getattr(args, "model", None) or settings.ollama_model
    client = OllamaClient(settings.ollama_url, wanted)
    report = client.diagnose()

    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_OK if report["generated"] else EXIT_ERROR

    print(f"Ollama at {report['url']}")
    print(f"Model wanted: {report['model']}")
    print()

    def mark(ok: bool) -> str:
        return "  OK  " if ok else " FAIL "

    print(f"[{mark(report['reachable'])}] something is listening")
    if not report["reachable"]:
        print()
        print("  Ollama is not running, or is on a different address.")
        print("  Start it:      ollama serve")
        print("  Check the URL: OLLAMA_URL in your .env")
        print()
        print("  Search still works. Ollama is only used by the Interpret")
        print("  button, which rewrites a sentence into a query. Typing a")
        print("  query and pressing Enter never touches it.")
        return EXIT_ERROR

    print(f"[{mark(bool(report['models']))}] models installed: "
          f"{', '.join(report['models']) or 'none'}")
    print(f"[{mark(report['model_installed'])}] '{report['model']}' is one of them")
    if not report["model_installed"]:
        print()
        print(f"  Ollama is running but has no '{report['model']}'. Pull it:")
        print(f"    ollama pull {report['model']}")
        print("  Or point OLLAMA_MODEL at one of the models listed above.")
        return EXIT_ERROR

    took = f" in {report['elapsed_s']}s" if report["elapsed_s"] is not None else ""
    print(f"[{mark(report['generated'])}] it answered a trial question{took}")
    if not report["generated"]:
        print()
        print(f"  {report['error']}")
        print("  The model is installed but did not reply within 30 seconds.")
        print("  A first call loads the model into memory and can be slow;")
        print("  try again, and if it persists the model may be too large for")
        print("  this machine.")
        return EXIT_ERROR

    print()
    print(f"Working. Reply: {report.get('reply', '')!r}")

    # **The end-to-end check.** Everything above proves Ollama is alive; none of
    # it proves the one thing the app asks of it. A model can be installed,
    # responsive, and still return prose where a query was wanted - and the
    # translator will then quietly fall back to the raw sentence, which looks
    # like it worked. This runs the real path and prints what came back.
    sentence = getattr(args, "translate", None)
    if sentence:
        from app.search.translate import TRANSLATE_TIMEOUT_S, QueryTranslator

        budget = float(getattr(args, "timeout", 0) or TRANSLATE_TIMEOUT_S)
        print()
        print(f"Interpreting: {sentence!r}  (budget {budget:g}s)")
        # `enabled=True` because typing `--translate` *is* the request. The
        # stored preference governs the button in the window; it would be
        # obtuse for a command that exists to run one translation to refuse
        # because a checkbox elsewhere is unticked.
        result = QueryTranslator(
            client, timeout_s=budget, enabled=True).translate(sentence)
        print(f"  -> {result.query!r}  [{result.elapsed_s:.1f}s]")
        # `changed`, not `used_model`: the latter is False for a cache hit,
        # and a cached translation is a working one. What matters here is
        # whether anything came back that differs from what went in.
        if result.changed:
            cached = " (from cache)" if result.from_cache else ""
            print(f"  [  OK  ] the model produced it{cached}")
        else:
            print("  [ FAIL ] fell back to the raw sentence")
            print()
            # The error's own message and suggestion, not a guess. This command
            # once printed "try a different model" for a *timeout*, which is
            # sometimes right and sometimes hides that the budget is simply too
            # small - and it printed it while the trial question above had just
            # succeeded in under a second.
            if result.error is not None:
                print(f"  [{result.error.code}] {result.error.message}")
                if result.error.details:
                    print(f"  {result.error.details}")
                print()
                print(f"  FIX: {result.error.suggestion}")
            else:
                print(f"  {result.note}")
            print()
            print("  Search is unaffected. Interpret is the only thing that uses")
            print("  Ollama, and when it cannot help it passes your words through.")
            return EXIT_ERROR

    return EXIT_OK


def add_ollama_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_ollama = sub.add_parser(
        "ollama", parents=[common],
        help="check the Ollama connection (optional; only the Interpret button uses it)")
    p_ollama.add_argument(
        "--translate", metavar="SENTENCE",
        help="also run one real translation end to end, and show what came back")
    p_ollama.add_argument(
        "--model", metavar="NAME",
        help="check this model instead of OLLAMA_MODEL - try one before committing to it")
    p_ollama.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="seconds to allow the model for --translate (default: %(default)s)"
             % {"default": "30"})
    p_ollama.set_defaults(func=cmd_ollama)
