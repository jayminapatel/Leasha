"""Headless entry point.

Layer: L0

Every layer ships a CLI before it ships UI, so it can be tested without Qt.
Layer 0 provides `stats`, `doctor` and `lock`; Layer 2 adds `extract`; `index`
and `search` are declared now and fail with an honest ERR_NOT_IMPLEMENTED naming
the layer that delivers them, rather than pretending or crashing.

    python -m app.cli stats
    python -m app.cli stats --json
    python -m app.cli init
    python -m app.cli doctor
    python -m app.cli diagnose        # troubleshooting bundle

    python -m app.cli extract "D:\\Docs\\report.pdf" --chunks
    python -m app.cli extract "D:\\Docs" --limit 200
    python -m app.cli extract "D:\\Docs" --json > extraction.json

    python -m app.cli models list                 # every model Leasha needs
    python -m app.cli models download all         # or: models download search rerank

`--json` and `--env` work either side of the subcommand: argparse would normally
demand them first, which is not the order anyone types.

`extract` is read-only: it opens no store and writes nothing. It exists so that
extraction can be checked against real documents rather than only against
synthetic fixtures, and so "why did that file not come back in a search?" has an
answer that does not need a debugger.

Exit codes:
    0  success
    1  an AppError occurred - and, for `search`, nothing found (so a script
       can tell an empty answer from a successful one; 2026-10-08)
    2  the feature is not built yet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from app.cli._common import EXIT_ERROR, EXIT_OK, _report, make_console_safe
from app.cli.bench import (
    add_bench_index_parser,
    add_bench_pipeline_parser,
    add_embed_bench_parser,
    add_rerank_bench_parser,
)
from app.cli.desktop import add_completions_parser, add_open_parser, add_shortcut_parser
from app.cli.evaluate import add_evaluate_parser
from app.cli.extract import add_convert_parser, add_extract_parser
from app.cli.formats import add_formats_parser
from app.cli.index import add_index_parser, add_reembed_parser
from app.cli.media import add_media_parser
from app.cli.models import add_models_parser
from app.cli.photos import add_photos_parser
from app.cli.mcp_server import add_mcp_parser
from app.cli.maintenance import (
    add_diagnose_parser,
    add_doctor_parser,
    add_init_parser,
    add_lock_parser,
    add_move_index_parser,
    add_stats_parser,
)
from app.cli.offline_media import add_offline_media_parser
from app.cli.ollama import add_ollama_parser
from app.cli.report import add_report_parser
from app.cli.timeline import add_timeline_parser
from app.cli.watch import add_watch_parser, cmd_watch  # noqa: F401
from app.cli.repos import add_gitsearch_parser, add_repos_parser
from app.cli.scan import add_scan_parser
from app.cli.search import (
    add_commands_parser,
    add_files_parser,
    add_search_parser,
    add_shell_parser,
)
from app.core.branding import SHORT_DESCRIPTION
from app.core.config import log_dir_for
from app.core.errors import AppErrorException
from app.core.logging import log_app_error
from app.core.runlog import start_run

# The window, the shell and the tests import these from `app.cli`.
# isort: split
from app.cli._common import EXIT_NOT_IMPLEMENTED, ROOTS_STATE_KEY, _saved_roots  # noqa: F401
from app.cli._progress import ProgressLine, _console_sink  # noqa: F401
from app.cli.bench import (  # noqa: F401
    cmd_bench_index,
    cmd_bench_pipeline,
    cmd_embedbench,
    cmd_rerank_bench,
)
from app.cli.desktop import cmd_completions, cmd_open, cmd_shortcut  # noqa: F401
from app.cli.evaluate import cmd_evaluate  # noqa: F401
from app.cli.extract import _write_extract_json, cmd_convert, cmd_extract  # noqa: F401
from app.cli.formats import cmd_formats  # noqa: F401
from app.cli.index import (  # noqa: F401
    _print_enrichment_counts,
    _print_vector_coverage,
    cmd_index,
    cmd_reembed,
)
from app.cli.media import cmd_media  # noqa: F401
from app.cli.models import cmd_models  # noqa: F401
from app.cli.photos import cmd_photos  # noqa: F401
from app.cli.maintenance import (  # noqa: F401
    cmd_diagnose,
    cmd_doctor,
    cmd_init,
    cmd_lock,
    cmd_move_index,
    cmd_stats,
    semantic_search_warnings,
)
from app.cli.offline_media import cmd_offline_media  # noqa: F401
from app.cli.ollama import cmd_ollama  # noqa: F401
from app.cli.report import cmd_report  # noqa: F401
from app.cli.repos import _scan_for_repos, cmd_gitmeasure, cmd_gitsearch, cmd_repos  # noqa: F401
from app.cli.scan import cmd_scan  # noqa: F401
from app.cli.search import (  # noqa: F401
    cmd_commands,
    cmd_files,
    cmd_search,
    cmd_shell,
    print_response,
)

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description=f"{SHORT_DESCRIPTION}\n\nHeadless entry point.",
    )
    parser.add_argument("--env", help="path to an alternative .env file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    # 2026-10-04: what Help > About Leasha says, from the command line (#8).
    from app.core.version import version as _version
    parser.add_argument("--version", action="version", version=f"Leasha {_version()}",
                        help="print the version and exit")

    # `--json` and `--env` are global, which in argparse means "before the
    # subcommand" - so `app.cli extract PATH --json` fails with an unhelpful
    # "unrecognized arguments". Nobody types them in that order, so they are
    # accepted after the subcommand too.
    #
    # default=SUPPRESS is what makes this safe: without it the subparser writes
    # its own default over whatever the global flag already parsed, and
    # `app.cli --json extract PATH` would silently stop being JSON.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--env", default=argparse.SUPPRESS,
                        help="path to an alternative .env file")

    sub = parser.add_subparsers(dest="command", required=True)

    add_stats_parser(sub, common)
    add_init_parser(sub, common)
    add_move_index_parser(sub, common)
    add_doctor_parser(sub, common)
    add_diagnose_parser(sub, common)
    add_lock_parser(sub, common)
    add_extract_parser(sub, common)
    add_scan_parser(sub, common)
    add_index_parser(sub, common)
    add_watch_parser(sub, common)
    add_convert_parser(sub, common)
    add_files_parser(sub, common)
    add_gitsearch_parser(sub, common)
    add_repos_parser(sub, common)
    add_offline_media_parser(sub, common)
    add_report_parser(sub, common)
    add_timeline_parser(sub, common)
    add_evaluate_parser(sub, common)
    add_embed_bench_parser(sub, common)
    add_bench_index_parser(sub, common)
    add_bench_pipeline_parser(sub, common)
    add_reembed_parser(sub, common)
    add_commands_parser(sub, common)
    add_ollama_parser(sub, common)
    add_shell_parser(sub, common)
    add_open_parser(sub, common)
    add_completions_parser(sub, common)
    add_shortcut_parser(sub, common)
    add_rerank_bench_parser(sub, common)
    add_formats_parser(sub, common)
    add_media_parser(sub, common)
    add_photos_parser(sub, common)
    add_search_parser(sub, common)
    add_mcp_parser(sub, common)
    add_models_parser(sub, common)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    # **Opened before the command runs, not inside it.** A run that fails at
    # configuration - the single most common way this application has gone
    # wrong on the owner's machine - is exactly the run worth having a file
    # for, and by the time `_load` raises it is too late to start one.
    # `log_dir_for` answers "which folder" without validating anything, so a
    # broken `.env` still gets logged rather than losing its own evidence.
    log_dir = log_dir_for(Path(args.env) if getattr(args, "env", None) else None)
    run = start_run(log_dir, getattr(args, "command", "cli"),
                    argv=list(argv) if argv is not None else sys.argv[1:])
    # 2026-10-09: a native fault in this process - PyMuPDF overnight, ONNX
    # Runtime at midday, neither catchable in Python - leaves the Python stack
    # of the thread it was on in `logs/crash/index-crash.log` for the index
    # command and `cli-crash.log` for any other (`app.core.crash_guard`). Here,
    # at the real entry, and not inside `cmd_index`: the handler keeps its file
    # open for the life of the process, and a test that calls a command
    # in-process must not pin a file in pytest's temp folder.
    from app.core.crash_guard import catch_native_crashes

    catch_native_crashes(log_dir, "index" if getattr(args, "command", "") == "index" else "cli")

    code = EXIT_ERROR
    try:
        code = int(args.func(args))
        return code
    except AppErrorException as exc:
        # Logging may not be configured yet (a bad .env fails before setup),
        # so print unconditionally and log only on a best-effort basis.
        try:
            log_app_error(exc.error)
        except Exception:  # noqa: BLE001
            pass
        code = _report(exc.error, getattr(args, "json", False))
        return code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        code = EXIT_ERROR
        return code
    except BaseException as exc:
        # Recorded, then re-raised unchanged. The sink never sees an exception
        # nobody caught, so without this the run log would end at whatever line
        # happened to be logged last - the least useful place to stop.
        run.unhandled(exc)
        code = "crash"
        raise
    finally:
        path = run.finish(code)
        # Named on the console only when it is worth opening. A line printed
        # after every successful `search` is furniture within a day, and
        # furniture is what people stop reading.
        if code != EXIT_OK or run.errors:
            print(f"\nRun log: {path}", file=sys.stderr)
