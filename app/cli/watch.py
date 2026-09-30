r"""`watch`: keep the index up to date as files are saved.

Layer: L3's command-line entry point for work order 0z, item F1.

    python -m app.cli watch                  # the folders saved in Settings
    python -m app.cli watch "D:\Docs"        # or the ones named here
    python -m app.cli watch --for 60         # stop by itself after a minute
    python -m app.cli watch --backend poll   # compare instead of being told

Runs until Ctrl+C (or `--for`). Everything it does is `app/index/
folder_watch.py`; this file loads the settings, opens the stores, and says
what happens - to a person, one line per update, or to the window as one JSON
object per line (`--events jsonl`).

**With `--events jsonl` it is the window's child process**, on the pattern of
`app.cli index --events jsonl` (`app/index/child_run.py`): events on standard
output and nothing else, and the end of standard input - the window closing
its end, or dying - means stop. A watch with no window must not be left
running.

The index is only written while a batch is being applied, under the same run
lock every index run takes; between batches this process holds nothing but
the open stores and a handle on each watched folder.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report, _saved_roots
from app.core.errors import make_error
from app.core.logging import logger, setup_logging

__all__ = ["add_watch_parser", "cmd_watch", "WATCH_EVENT"]

#: The `event` field of every line `--events jsonl` writes.
WATCH_EVENT = "watch"


def _plain(value: Any) -> Any:
    """An event's data as something `json.dumps` accepts."""
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class _Reporter:
    """Says what the watch does: lines for a person, or JSON for the window."""

    def __init__(self, out: Any, *, machine: bool, quiet: bool) -> None:
        self._out = out
        self._machine = machine
        self._quiet = quiet
        self._lock = threading.Lock()
        self.broken = threading.Event()

    def event(self, kind: str, data: dict) -> None:
        if self._machine:
            self._write(json.dumps(
                {"event": WATCH_EVENT, "kind": kind, "t": time.time(), **_plain(data)},
                ensure_ascii=True))
            return
        line = self._words(kind, data)
        if line and not self._quiet:
            self._write(f"{time.strftime('%H:%M:%S')}  {line}")

    def _write(self, line: str) -> None:
        with self._lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError):
                # Nobody is reading: the window has gone.
                self.broken.set()

    @staticmethod
    def _words(kind: str, data: dict) -> str:
        if kind == "watching":
            how = ("told by Windows as files change" if data.get("backend") == "native"
                   else "compared every half minute")
            return f"Watching {data.get('root')} ({how})"
        if kind == "updated":
            parts = []
            if data.get("indexed"):
                parts.append(f"{data['indexed']:,} indexed")
            if data.get("removed"):
                parts.append(f"{data['removed']:,} removed")
            if data.get("skipped"):
                parts.append(f"{data['skipped']:,} skipped")
            if data.get("rescanned"):
                parts.append(f"{data['rescanned']:,} folder(s) looked at in full")
            names = ", ".join(data.get("names") or [])
            return (f"{', '.join(parts) or 'nothing to do'} "
                    f"in {data.get('seconds', 0):.1f}s" + (f": {names}" if names else ""))
        if kind == "busy":
            return (f"{data.get('count', 0):,} change(s) waiting: "
                    f"{data.get('reason', '')}")
        if kind == "problem":
            error = data.get("error")
            if error is None:
                return f"Watching {data.get('root')} again"
            return error.render()
        if kind == "error":
            return data["error"].render()
        if kind == "idle":
            return f"Nothing to watch: {data.get('reason', '')}."
        return ""                                # "pending" is not worth a line


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch the indexed folders and index what changes. Work order 0z F1."""
    from app.cli.index import _private_stdin, _redirect_stdout, build_pipeline_config
    from app.core.run_lock import INDEX_MUTEX_NAME
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.index.folder_watch import BatchIndexer, FolderWatcher, live_roots
    from app.index.resolve import resolve_for_run
    from app.index.walker import PathRules
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    machine = bool(getattr(args, "events", None))
    out = _redirect_stdout() if machine else sys.stdout
    commands = _private_stdin() if machine else None
    reporter = _Reporter(out, machine=machine, quiet=bool(args.quiet))

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.watch")

    named = [Path(root).expanduser() for root in (args.roots or [])]
    roots = named or [Path(root).expanduser() for root in _saved_roots(settings)]
    if not roots:
        return _refuse(reporter, machine, args, make_error(
            "ERR_CONFIG_INVALID", "cli.watch", key="roots",
            reason="no folders to watch",
            suggestion=r'Name them here - app.cli watch "D:\Docs" - or set them '
                       'once on the Settings page, under "Folders to index".'))

    stop = threading.Event()
    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        tuned = resolve_for_run(settings, store)
        if not named or getattr(args, "live_only", False):
            # Folders marked Archive are the owner's "this does not change".
            roots = live_roots(roots, store)
        if not roots:
            reporter.event("idle", {"reason": "every indexed folder is marked Archive"})
            if machine:
                reporter.event("stopped", {"batches": 0, "indexed": 0, "removed": 0})
            return EXIT_OK

        def config(for_roots: list) -> Any:
            return build_pipeline_config(settings, list(for_roots), tuned=tuned, prune=False)

        if getattr(args, "fake_embedder_for_bench", False):
            # For measuring and testing where the real model was never
            # downloaded - the same fake `app.cli index` accepts.
            from app.index.pipeline_bench import FAKE_MODEL_NAME, _fake_encoder

            def embedder() -> Any:
                return Embedder(FAKE_MODEL_NAME, dim=settings.embed_dim,
                                encoder=_fake_encoder(settings.embed_dim))
        else:
            def embedder() -> Any:
                return Embedder.from_settings(settings, threads=tuned.onnx_threads)

        apply = BatchIndexer(
            store, vectors, embedder=embedder, config=config,
            image_embedder=ClipImageEmbedder.from_settings(settings),
            image_vectors=image_vectors,
            lock_name=getattr(args, "lock_name", None) or INDEX_MUTEX_NAME)
        rules = PathRules(config(roots).walk)
        watcher = FolderWatcher(
            roots, apply=apply, rules=lambda: rules,
            backend=args.backend, on_event=reporter.event)

        if commands is not None:
            _stop_when_input_ends(commands, stop)
        watcher.start()
        log.info("watching {} folder(s) for changes", len(roots))
        deadline = (time.monotonic() + float(args.seconds)) if args.seconds else None
        try:
            while not stop.wait(0.25):
                if reporter.broken.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            watcher.stop()

        totals = {"batches": apply.batches, "indexed": apply.total_indexed,
                  "removed": apply.total_removed,
                  "problems": sorted(watcher.problems())}
        if machine:
            reporter.event("stopped", totals)
        elif args.json:
            print(json.dumps(totals, indent=2))
        elif not args.quiet:
            print(f"Stopped. {apply.total_indexed:,} document(s) indexed and "
                  f"{apply.total_removed:,} removed in {apply.batches:,} update(s).")
    return EXIT_OK


def _refuse(reporter: _Reporter, machine: bool, args: argparse.Namespace,
            error: Any) -> int:
    if machine:
        reporter.event("error", {"error": error, "count": 0})
        reporter.event("stopped", {"batches": 0, "indexed": 0, "removed": 0})
        return EXIT_ERROR
    return _report(error, args.json)


def _stop_when_input_ends(stream: Any, stop: threading.Event) -> None:
    """The window's commands: `stop`, or the pipe closing, ends the watch."""

    def read() -> None:
        try:
            for line in stream:
                if line.strip().lower() == "stop":
                    break
        except (OSError, ValueError):
            pass
        stop.set()

    threading.Thread(target=read, name="watch-commands", daemon=True).start()


def add_watch_parser(sub: argparse._SubParsersAction,
                     common: argparse.ArgumentParser) -> None:
    parser = sub.add_parser(
        "watch", parents=[common],
        help="keep the index up to date as files are saved, until stopped")
    parser.add_argument("roots", nargs="*",
                        help="folders to watch (default: the folders saved in "
                             "Settings, less those marked Archive)")
    parser.add_argument("--backend", choices=("auto", "native", "poll"), default="auto",
                        help="auto: be told by the system where it can (Windows), "
                             "compare otherwise; poll: always compare")
    parser.add_argument("--for", dest="seconds", type=float, metavar="SECONDS",
                        help="stop after this long (default: run until Ctrl+C)")
    parser.add_argument("--events", choices=("jsonl",), default=None,
                        help="write one JSON object per line instead of words, and "
                             "stop when standard input ends (used by the window)")
    parser.add_argument("--live-only", action="store_true",
                        help="leave out any named folder that is marked Archive "
                             "(always so for the saved folders)")
    parser.add_argument("--quiet", action="store_true", help="say nothing per update")
    parser.add_argument("--fake-embedder-for-bench", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--lock-name", default=None, help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_watch)
