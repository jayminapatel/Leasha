"""`embed-bench`, `bench-index`, `bench-pipeline` and `rerank-bench`: what this machine costs."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK, _load
from app.core.config import Settings
from app.core.logging import setup_logging


def cmd_embedbench(args: argparse.Namespace) -> int:
    """Measure what embedding costs on this machine, and say what would help.

    Exists because an estimate was wrong once, expensively. "1.53 passages per
    second" was called twenty times too slow, on the assumption that a small
    model should manage tens per second - true for short sentences, false for
    the 512-token passages this app embeds. A throughput number without the
    sequence length beside it is not a number.
    """
    from app.index.embed_bench import inspect_model, project, providers, run_benchmark
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    cache = Path(settings.model_cache)

    result = inspect_model(cache, providers(_bench_result(settings)))
    print("Embedding on this machine")
    print("=" * 68)
    print(f"  model            {settings.embed_model}")
    if result.model_file:
        print(f"  file             {Path(result.model_file).name}  ({result.model_mb:.0f}MB)")
    if result.precision:
        # Reported from the file size, so it works without `onnx` installed -
        # and as a precision rather than a yes/no, because fp16 is a real answer
        # and "not int8" would have hidden that int8 is still worth having.
        print(f"  precision        {result.precision}")
    if result.weight_types:
        shown = ", ".join(f"{name} {count/1e6:.1f}M" for name, count in
                          sorted(result.weight_types.items(), key=lambda kv: -kv[1]))
        print(f"  weights          {shown}")
    print(f"  providers        {', '.join(result.available_providers) or 'unknown'}")

    if not args.quick:
        print()
        print("  measuring…")
        result = run_benchmark(settings.embed_model, cache, result=result)
        if result.threads:
            print(f"  threads          {result.threads}")

    if result.error:
        print()
        print(f"  ! {result.error}")

    if result.throughput:
        print()
        # **Tokens per second, beside chunks per second.** A corpus is a
        # quantity of text; how it is cut into chunks is a choice. Quoting only
        # chunks/sec hides that the choice changes the total - and it hid it
        # well enough that "halving the chunk size is roughly a wash" was said
        # out loud on the strength of it, which the numbers below disprove.
        print(f"  {'chunk':>8}  {'chunks/sec':>11}  {'tokens/sec':>11}"
              f"  {'range':>18}")
        for tokens, rate in sorted(result.throughput.items()):
            low, high = result.spread.get(tokens, (rate, rate))
            print(f"  {tokens:>8}  {rate:>11.2f}  {tokens * rate:>11,.0f}"
                  f"  {low:>8.2f} - {high:<7.2f}")

        if result.unstable:
            print()
            print(f"  ! The {', '.join(str(t) for t in result.unstable)}-token"
                  " measurement varied by more than a quarter between passes.")
            print("    Something else was using the machine. Close it and run again -")
            print("    the projections below are only as good as this number.")

        rate = result.throughput.get(512) or min(result.throughput.values())
        # The same corpus, cut differently. Everything here is measured on this
        # machine; only the choice of chunk size is hypothetical.
        biggest = max(result.throughput)
        total_tokens = 800_000 * biggest
        if len(result.throughput) > 1:
            print()
            print("  The same 100GB corpus, cut into different chunk sizes:")
            for size in sorted(result.throughput):
                hours = total_tokens / (size * result.throughput[size]) / 3600
                marker = "  <- current" if size == biggest else ""
                print(f"    {size:>3}-token chunks   {hours:>5.0f} hours{marker}")
        chunks = 0
        if settings.fts_db.is_file():
            with SqliteStore(settings.fts_db) as store:
                chunks = int(store.stats()["chunks_total"])
        print()
        print("  At the 512-token rate:")
        for label, count in (("your index now", chunks), ("200K messages", 400_000),
                             ("100GB corpus", 800_000)):
            if count:
                hours = project(count, rate)["hours"]
                unit = f"{hours*60:.0f} min" if hours < 1.5 else f"{hours:.0f} hours"
                print(f"    {label:<18} {count:>9,} chunks   {unit}")

    if args.json:
        print()
        print(json.dumps(result.as_dict(), indent=2))

    print()
    for line in _embed_advice(result):
        print(line)
    return EXIT_OK


def cmd_bench_index(args: argparse.Namespace) -> int:
    r"""Time the whole pipeline here, and remember the answer.

    **The model was only ever half the question.** `embed-bench` says how fast
    the model is; a run whose model is fast and whose disk is slow is bounded
    by the disk, and a tuning screen holding only the model number will
    confidently recommend a graphics card to somebody who needs a different
    drive. This times reading, writing and the model on the same fixed
    workload, so the three are comparable.

    The numbers are stored beside the compute profile, keyed by its
    fingerprint, and that is what turns Defaults into Auto-tune. `--no-save`
    is for measuring somebody else's machine, or for a comparison you do not
    want acting on your settings.
    """
    from app.core.compute_profile import cached_profile
    from app.core.measured import remember
    from app.index.index_bench import run_index_bench
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    devices = ("cpu", "gpu") if args.both else None
    if not args.json:
        print("Timing this machine on a fixed workload. About a minute.",
              flush=True)

    result = run_index_bench(settings, devices=devices)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print()
        print("This machine, on the whole pipeline")
        print("=" * 68)
        print(f"  reading          {result.extract_per_second:,.0f} files a second, per reader")
        print(f"  writing          {result.write_per_second:,.0f} chunks a second")
        for device, rate in result.embed_per_second.items():
            print(f"  meaning ({device})   {rate:,.1f} chunks a second")
        print(f"  took             {result.seconds:.1f}s over "
              f"{result.documents:,} documents and {result.chunks:,} chunks")
        for note in result.notes:
            print(f"  note             {note}")
        if result.error:
            print(f"  STOPPED          {result.error}")

    if args.no_save or result.error:
        return EXIT_OK

    with SqliteStore(settings.fts_db) as store:
        profile = cached_profile(store, settings.data_path)
        stored = remember(store, result.as_measured(profile.fingerprint()))
    if not args.json:
        # **Said either way.** "Saved" is what makes Auto-tune mean something,
        # and a silent failure to save would leave somebody believing their
        # machine had been learned when it had not.
        print("  saved            " + ("yes - Auto-tune will use these"
                                       if stored else
                                       "NO - the index could not be written to"))
    return EXIT_OK


def cmd_bench_pipeline(args: argparse.Namespace) -> int:
    r"""Index a made-up corpus with the real pipeline, and report what it cost.

    Work order 0x item 5a ("a repeatable benchmark") and the measuring tool
    2d needs (the window's stalls while indexing). The corpus comes from
    `app/index/synthetic_corpus.py` and is the same on every machine for the
    same `--size` and `--seed`; the run goes into a throwaway data folder and
    never touches the real index. See `app/index/pipeline_bench.py` for what
    is measured and why, and how it differs from `bench-index`.

    **Deliberately does not call `_load(args)`.** Every other command needs a
    working `.env`; this one must run on a machine that has never been set up
    (a Mac being tried for the first time, CI), so the person's settings are
    only *looked at*, for the downloaded model, and a missing `.env` means
    the fake embedder rather than an error.
    """
    from app.index.pipeline_bench import BenchOptions, format_report, run_pipeline_bench
    from app.index.synthetic_corpus import SIZES

    spec = None
    counts = {name: getattr(args, name) for name in (
        "documents", "mbox_messages", "zip_members", "eml_files")
        if getattr(args, name) is not None}
    if counts or args.seed != 1:
        # Explicit counts start from the named size and change only what was
        # given, so `--size small --mbox-messages 20000` means what it says.
        base = SIZES[args.size]
        spec = replace(base, seed=args.seed, **counts)

    as_json = bool(getattr(args, "json", False))
    folder = Path(args.corpus).expanduser() if args.corpus else None
    temporary_corpus = folder is None
    if temporary_corpus:
        import tempfile

        folder = Path(tempfile.mkdtemp(prefix="leasha-bench-corpus-"))

    def note(text: str) -> None:
        """Progress lines go to stderr, so `--json` stdout stays parseable."""
        print(f"  {text}", file=sys.stderr, flush=True)

    options = BenchOptions(
        corpus_folder=folder, size=args.size, spec=spec,
        embedder=args.embedder, probe=args.probe,
        probe_yield=not args.no_yield, workers=args.workers,
        full_speed=args.full_speed,
        child_process=bool(getattr(args, "child_process", False)),
        read_processes=(True if getattr(args, "read_processes", False) else None),
        env_file=Path(args.env) if getattr(args, "env", None) else None,
        my_settings=args.my_settings, keep=args.keep, on_note=note,
    )
    try:
        report = run_pipeline_bench(options)
    except (ValueError, FileExistsError) as exc:
        # A bad request (wrong size, no model for --embedder real, a folder
        # holding a different corpus) is said in one line, not a traceback.
        print(f"bench-pipeline: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if temporary_corpus and not args.keep:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
        if args.out:
            print(f"\n  JSON written to {args.out}")
    return EXIT_OK


def _bench_result(settings: Settings):
    from app.index.embed_bench import BenchResult

    return BenchResult(model_name=settings.embed_model)


def _embed_advice(result: Any) -> list[str]:
    """What would actually help, given what was measured.

    Deliberately says "nothing to gain here" when that is the answer. Advice
    that always finds something to recommend is advice nobody can act on.
    """
    lines = ["What would help:"]
    gpu = [p for p in result.available_providers
           if any(k in p for k in ("CUDA", "Dml", "DirectML", "ROCm", "CoreML"))]
    if gpu:
        lines.append(f"  * {gpu[0]} is available and is NOT being used. That is the")
        lines.append("    largest single win here - typically five to fifteen times.")
    if result.precision == "int8":
        lines.append("  * The model is ALREADY int8. Quantisation is not a lever here -")
        lines.append("    do not spend time on it.")
    elif result.precision == "fp16":
        lines.append("  * The model is fp16 - already half the size of full precision, so")
        lines.append("    an int8 build is worth roughly another two times rather than the")
        lines.append("    four you would get from fp32. Switching invalidates every stored")
        lines.append("    vector, so it is cheapest while the index is small.")
    elif result.precision == "fp32":
        lines.append("  * The model is full precision. An int8 build is 2-4x on CPU for a")
        lines.append("    small accuracy cost, and switching invalidates every stored")
        lines.append("    vector - so it is cheapest while the index is small.")
    lines += [
        "  * Chunk size is worth about 10-15% over the same text, and the table",
        "    above shows it for THIS machine rather than in the abstract. It was",
        "    claimed here as 1.5x once, from a measurement whose spread the tool",
        "    now warns about. Unless the table shows a wide gap, this is not",
        "    worth a re-index.",
        "  * Fewer chunks beats faster chunks. Quoted replies and signatures are",
        "    already stripped; near-duplicate passages are the next candidate.",
        "  * The cost is per token, so it scales with how much text is indexed,",
        "    not with how many files. Narrowing the index roots is the bluntest",
        "    and most reliable saving available.",
    ]
    return lines


def cmd_rerank_bench(args: argparse.Namespace) -> int:
    """What reranking costs, per model, on this machine.

    Reranking was 8.3 seconds of a 9-second search - 93% of it, against a spec
    budget of 300ms warm. The default model is 1.04GB; the smallest usable one
    is 0.08GB. This measures the difference rather than asserting it, because
    four throughput claims in this project have already been wrong and every one
    was a number quoted without its conditions.
    """
    from app.search.rerank_bench import measure
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    models = [args.model] if getattr(args, "model", None) else []
    if not args.json:
        # **Not on stdout under `--json`.** A preamble in front of the payload
        # makes machine-readable output unparseable, which is the one thing it
        # has to be. Found by a test that ran the command rather than reading it.
        print("Timing a full rerank.")
        if not models:
            # It downloaded 1.4GB of models on the owner's first run without
            # saying so beforehand. Saying so is the least it can do.
            print("First run downloads about 1.4GB for the four candidates.")
            print("Use --model NAME to time only one.")
        print()

    with SqliteStore(settings.fts_db) as store:
        result = measure(
            store, models=models, count=args.count,
            window_chars=args.window, cache_dir=str(settings.model_cache),
            passes=args.passes,
        )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return EXIT_OK

    # The download progress bars write to the same terminal and overwrite the
    # first lines of the table. A blank line and a flush lets them finish.
    sys.stdout.flush()
    print("\n")
    print(f"{result.count} candidates  ·  passages cut to {result.window_chars} "
          f"chars (mean chunk is {result.mean_passage_chars})")
    print()
    print(f"{'model':38} {'size':>8} {'per search':>11} {'per passage':>12}  load")
    print("-" * 82)
    for timing in result.timings:
        if timing.error:
            print(f"{timing.name:38} {timing.size:>8}   {timing.error}")
            continue
        per_passage = timing.median_s / max(1, result.count) * 1000
        flag = "  UNSTABLE" if timing.unstable else ""
        print(f"{timing.name:38} {timing.size:>8} {timing.median_s:>10.2f}s "
              f"{per_passage:>11.0f}ms  {timing.load_s:.1f}s{flag}")

    # **Always, not only when a model loaded.** This is the fact that misled
    # the owner: two runs looked like a comparison and were the same model
    # twice, because `.env` pinned it and nothing on screen said so.
    print()
    print(f"You are currently using: {settings.rerank_model}")

    usable = [t for t in result.timings if t.passes]
    if usable:
        best = min(usable, key=lambda t: t.median_s)
        print(f"Fastest here:            {best.name} at {best.median_s:.2f}s per search.")
        if settings.rerank_model != best.name:
            # **`.env` shadows the shipped default.** Changing a default in the
            # code does nothing for anybody who already has a `.env` - which is
            # everybody who has ever run the installer. Saying "the default is
            # now X" would have been useless advice, and was.
            env = getattr(args, "env", None) or ".env"
            print()
            print(f"  Your {env} pins RERANK_MODEL, so the shipped default does")
            print("  not apply. Edit that line to change it:")
            print(f"      RERANK_MODEL={best.name}")
        print()
        print("Speed is only half the question. `leasha evaluate --builtin` measures")
        print("whether the ordering is still good enough on your own corpus.")
    return EXIT_OK


def add_embed_bench_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_bench = sub.add_parser(
        "embed-bench", parents=[common],
        help="measure what embedding costs on this machine, and what would help")
    p_bench.add_argument("--quick", action="store_true",
                         help="inspect the model but do not time it")
    p_bench.set_defaults(func=cmd_embedbench)


def add_bench_index_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_index_bench = sub.add_parser(
        "bench-index", parents=[common],
        help="time the whole pipeline on this machine - reading, writing and "
             "the model - and remember the answer")
    p_index_bench.add_argument(
        "--both", action="store_true",
        help="time the processor and the graphics card, to find out which is "
             "actually faster here")
    p_index_bench.add_argument(
        "--no-save", action="store_true",
        help="print the numbers without storing them for auto-tuning")
    p_index_bench.set_defaults(func=cmd_bench_index)


def add_bench_pipeline_parser(sub: argparse._SubParsersAction,
                              common: argparse.ArgumentParser) -> None:
    """`bench-pipeline`: see `cmd_bench_pipeline`."""
    from app.index.synthetic_corpus import SIZES

    p = sub.add_parser(
        "bench-pipeline", parents=[common],
        help="index a made-up corpus (documents, a big mbox, a zip) with the "
             "real pipeline into a throwaway folder, and report the time per "
             "stage, memory and - with --probe - how the window keeps up")
    p.add_argument("--corpus", metavar="FOLDER",
                   help="where to make (or reuse) the corpus; default: a "
                        "temporary folder, deleted afterwards")
    p.add_argument("--size", choices=list(SIZES), default="small",
                   help="corpus size (default: %(default)s)")
    p.add_argument("--seed", type=int, default=1,
                   help="corpus seed; same seed and size, same files "
                        "(default: %(default)s)")
    p.add_argument("--documents", type=int, help="override: loose documents")
    p.add_argument("--mbox-messages", type=int, dest="mbox_messages",
                   help="override: messages in the big mbox")
    p.add_argument("--zip-members", type=int, dest="zip_members",
                   help="override: members in the zip")
    p.add_argument("--eml-files", type=int, dest="eml_files",
                   help="override: loose .eml files")
    p.add_argument("--embedder", choices=("auto", "real", "fake"), default="auto",
                   help="real model, a labelled fake, or auto: real if already "
                        "downloaded, never a download (default: %(default)s)")
    p.add_argument("--probe", action="store_true",
                   help="run inside a Qt event loop with the lag monitor's "
                        "heartbeat, and report its lateness while indexing")
    p.add_argument("--no-yield", action="store_true",
                   help="with --probe: do not let the pipeline slow down when "
                        "the window runs late")
    p.add_argument("--workers", type=int, metavar="N",
                   help="extraction workers (default: what the app would pick)")
    p.add_argument("--full-speed", action="store_true",
                   help="as index --full-speed: no CPU ceiling, normal priority")
    p.add_argument("--my-settings", action="store_true",
                   help="use the tuning in your .env instead of the app defaults")
    # Work order 0x §2d: the "after" number. The same corpus and settings,
    # indexed by `app.cli index --events jsonl` in a child process the way the
    # window does it with "Index in a separate process" on.
    p.add_argument("--child-process", action="store_true",
                   help="index in a child process (app.cli index --events jsonl),\n"
                        "as the window does with 'Index in a separate process' on;\n"
                        "with --probe only the heartbeat stays in this process")
    # Work order 0x §5b: the "after" number for reading in processes.
    p.add_argument("--read-processes", action="store_true",
                   help="read files in a process per extraction worker, as\n"
                        "'Read files in separate processes' does")
    p.add_argument("--keep", action="store_true",
                   help="keep the throwaway data folder (and a temporary corpus)")
    p.add_argument("--out", metavar="FILE", help="also write the JSON report here")
    p.set_defaults(func=cmd_bench_pipeline)


def add_rerank_bench_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_rerank = sub.add_parser(
        "rerank-bench", parents=[common],
        # `%%`, not `%`. argparse runs every help string through `%`
        # formatting to expand `%(default)s`, and "93% of" is read as the
        # conversion `% o` - a space-flagged octal - which wants an integer and
        # gets argparse's dict. It crashed the whole top-level `--help`, not
        # just this line, and `rerank-bench --help` kept working because a
        # subparser only formats its own strings.
        help="time reranking per model - it was 93%% of one 9-second search")
    p_rerank.add_argument("--model", help="time only this one")
    p_rerank.add_argument("--count", type=int, default=30,
                          help="candidates to score (default: %(default)s)")
    p_rerank.add_argument("--window", type=int, default=600,
                          help="characters per passage (default: %(default)s)")
    p_rerank.add_argument("--passes", type=int, default=3,
                          help="runs per model, for the spread (default: %(default)s)")
    p_rerank.set_defaults(func=cmd_rerank_bench)
