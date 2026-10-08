"""The one entry point of the packaged build: `Leasha.exe` and `leasha-cli.exe`.

Layer: L9 (packaging, order 202626082213)

Both programs are this file. `Leasha.exe` (no console) opens the window.
`leasha-cli.exe` (a console program) is what the window starts its own children
with - `app.core.osbridge.stdio.own_python()` - so it answers the three ways
the window calls Python, exactly as `python.exe` would:

    leasha-cli.exe -m app.cli index ...     run a module
    leasha-cli.exe -c "<code>" ...          run a line of code (model downloads)
    leasha-cli.exe path\\to\\doctor.py ...    run a script (the health check)

Anything else given to `leasha-cli.exe` is a command for `app.cli`, so
`leasha-cli.exe search "boiler"` works from a terminal the same way
`python -m app.cli search "boiler"` does from source.
"""

from __future__ import annotations

import multiprocessing
import runpy
import sys
from pathlib import Path


def _run_module(name: str, args: list[str]) -> None:
    """`-m NAME`, as `python -m` does it. `alter_sys=True` so `sys.argv[0]` and
    `sys.modules["__main__"]` look as they would under the real interpreter -
    `app.cli` reads `sys.argv[1:]` when no argv is passed in."""
    sys.argv = [name, *args]
    runpy.run_module(name, run_name="__main__", alter_sys=True)


def _run_code(code: str, args: list[str]) -> None:
    """`-c CODE`: the window's model downloads run a line of code in a child
    (`own_python() -c ...`), so the console program has to accept it too."""
    sys.argv = ["-c", *args]
    exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102


def _run_script(path: str, args: list[str]) -> None:
    """A script by path - `doctor.py` from the installer's post-install check.
    The script's own folder is `_internal`, so its `Path(__file__)` logic
    finds `.env` exactly as it does in a checkout."""
    sys.argv = [path, *args]
    runpy.run_path(path, run_name="__main__")


def main() -> int:
    # First, before anything reads argv: a frozen child started by
    # `multiprocessing` re-enters this executable with its own arguments, and
    # `freeze_support` must see them before the dispatch below does.
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    # Which program this is, from the executable's own name: both are built
    # from this one file (leasha.spec), and the name is the only difference
    # PyInstaller leaves between them.
    console = Path(sys.executable).stem.lower() == "leasha-cli"
    if len(args) >= 2 and args[0] == "-m":
        _run_module(args[1], args[2:])
    elif len(args) >= 2 and args[0] == "-c":
        _run_code(args[1], args[2:])
    elif args and args[0].lower().endswith(".py"):
        _run_script(args[0], args[1:])
    elif console:
        _run_module("app.cli", args)
    else:
        from app.main import main as window

        return int(window() or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
