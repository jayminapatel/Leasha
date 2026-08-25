r"""Build the tree an installed copy of Leasha actually has, and run from it.

Layer: L0 (tooling; nothing in `app/` imports this)

Asked for: *"organize a structure under Leasha which are the master files as if
they are installed in final environment.. i.e. production and run like that so
that can be tested too"*.

**The problem it solves.** `D:\SearchProject` is a development checkout: source,
tests, fixtures, work orders, scratch files and a two-gigabyte venv in one tree,
and every command is run as `venv\Scripts\python.exe -m app.cli`. Nobody has
ever run this the way it will be installed, so the install layout is unverified -
and packaging is already a known risk (`HANDOFF.md` open question 4: PyInstaller
against the ONNX runtime and Qt plugins).

**Staged, never hand-maintained.** A second copy of the source that somebody
edits is a copy that drifts, and a drifted production tree is *worse* than none
because it is trusted. So this is a script: it deletes what it made last time
and copies again, and `test_staging.py` asserts that the manifest still covers
everything the application imports - so a package added next month cannot be
silently left out of the install.

**The venv is not copied.** It is two gigabytes of PyTorch, ONNX and Qt, and
copying it per run would make this a thing nobody uses. The launchers point at
an interpreter given on the command line - by default the development venv -
and set the working directory to the staged tree, which is what makes the
application resolve *its own* files:

    project_root() is `Path(__file__).resolve().parents[2]`

so `Leasha\app\core\config.py` puts the root at `Leasha\`. Configuration, logs
and the diagnostic bundle all follow from there without a single path being
passed in. That property is exactly what this exercise is checking, and it is
checked by running rather than by reading.

    venv\Scripts\python.exe scripts\stage.py
    venv\Scripts\python.exe scripts\stage.py --dest D:\Leasha --check

`--check` runs the staged copy: `--version`, `doctor --quick`, `commands`,
`formats`. A staging that produces a tree nobody has started is a staging that
proves nothing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["SHIPPED", "EXCLUDED", "stage", "main"]

#: What an installed copy has. Directories are copied whole, minus `EXCLUDED`.
#:
#: **A list, not "everything except".** An allowlist fails towards a tree that
#: is missing something - which shows up immediately, as an import error on the
#: first run. A denylist fails towards shipping the owner's `.env`, the test
#: fixtures and 100GB of scratch files, which shows up much later and much worse.
SHIPPED: tuple[str, ...] = (
    "app",                      # the application
    "config",                   # extractors.toml
    "assets",                   # icons, for the window and the tray
    "VERSION",                  # read by app.core.version
    "doctor.py",                # `app.cli doctor` shells out to it by path
    "requirements.txt",         # so the tree can rebuild its own venv
    "install.ps1",              # and re-run the installer against itself
    "run-install.cmd",
    "leasha.cmd",               # the CLI launcher the installer puts on PATH
)

#: Never staged, wherever they appear. Every one of these has a reason:
#:
#: `.env` holds the owner's paths and is **written by the application, never
#: copied** - non-negotiable 11. `__pycache__` is compiled against a different
#: absolute path. `tests` and `fixtures` are not part of a product. `logs` and
#: the index are runtime state and belong to the machine, not the release.
EXCLUDED: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "tests", "fixtures", "venv", ".venv", ".git", "logs", "node_modules",
    ".env", ".env.local",
})

#: Written into the staged tree so somebody opening it knows what it is - and,
#: more usefully, knows not to edit it.
README = """\
Leasha - staged install
=======================

This folder is what an installed copy of Leasha contains: the application, its
configuration, its icons, and nothing else. No tests, no fixtures, no work
orders, no scratch files.

**It is generated. Do not edit anything in here.**

    scripts\\stage.py

rebuilds it from the development checkout and deletes whatever was here before.
An edit made in this folder is an edit that will vanish without warning, and -
worse - one that makes this tree disagree with the source it came from.

To run it:

    Leasha.cmd                  the window
    leasha-cli.cmd stats        the command line

Both use the interpreter that was named when this tree was staged. A real
install has its own `venv\\` here instead; that is the one difference between
this and a shipped copy, and it is deliberate - a two-gigabyte copy per run is
a tool nobody would use.

Configuration lives in `.env` in this folder, and is written by the application
on first run. It is never copied from the development tree: those are somebody
else's paths.
"""


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED}


def stage(source: Path, dest: Path, python: Path) -> list[str]:
    """Copy the shipped tree into `dest`. Returns what was written.

    `dest` is **deleted first**. That is the whole point of it being generated:
    a stale file left over from a previous layout is the thing that makes a
    staged tree lie about what ships.
    """
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if dest == source:
        raise SystemExit("--dest cannot be the development tree itself.")

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    written: list[str] = []
    for name in SHIPPED:
        origin = source / name
        if not origin.exists():
            # Named but absent is worth saying: it means this list and the
            # repository have drifted, which is the failure staging exists to
            # make visible rather than the one it should hide.
            written.append(f"  MISSING  {name}")
            continue
        target = dest / name
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=_ignore)
        else:
            shutil.copy2(origin, target)
        written.append(f"  {name}")

    (dest / "README.txt").write_text(README, encoding="utf-8")
    _launchers(dest, python)
    # Created rather than copied: an installed copy has an empty log folder,
    # not the development tree's history.
    (dest / "logs").mkdir(exist_ok=True)
    return written


def _launchers(dest: Path, python: Path) -> None:
    r"""Two `.cmd` files: the window, and the command line.

    **`cd /d "%~dp0"` first.** Everything the application finds - `.env`,
    `logs\`, `config\` - is resolved from its own location, and a launcher that
    inherits whatever directory the shortcut happened to start in is how a
    staged tree quietly reads the development tree's configuration. That is
    precisely the confusion this exercise is meant to remove.
    """
    quoted = str(python)
    (dest / "Leasha.cmd").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"\"{quoted}\" -m app.main %*\r\n",
        encoding="utf-8")
    (dest / "leasha-cli.cmd").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"\"{quoted}\" -m app.cli %*\r\n",
        encoding="utf-8")


#: What `--check` runs. Every one is read-only, needs no index and no model, and
#: between them they touch configuration, the version file, the extractor
#: registry and the command catalogue - which is most of what a wrong layout
#: breaks.
CHECKS: tuple[tuple[str, ...], ...] = (
    ("--version",),
    ("commands",),
    ("formats",),
    ("doctor", "--quick"),
)


def check(dest: Path, python: Path) -> int:
    """Run the staged copy. **This is the part that proves anything.**

    Staging a tree nobody starts proves that files were copied, which was never
    in doubt. Running it proves the application finds its own configuration,
    its own icons and its own extractors from a folder it has never been in.
    """
    failures = 0
    for arguments in CHECKS:
        printed = " ".join(arguments)
        completed = subprocess.run(
            [str(python), "-m", "app.cli", *arguments],
            cwd=str(dest), capture_output=True, text=True, check=False,
        )
        # `doctor` exits non-zero when something on the machine is wrong, which
        # is not a staging failure - it is the staged copy working. Only a
        # crash counts here.
        broken = completed.returncode != 0 and "Traceback" in completed.stderr
        status = "FAILED" if broken else "ok"
        print(f"  {status:<7} app.cli {printed}")
        if broken:
            failures += 1
            print("          " + completed.stderr.strip().splitlines()[-1])
    return failures


def main(argv: "list[str] | None" = None) -> int:
    here = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build the tree an installed copy of Leasha has.")
    parser.add_argument("--dest", default=str(here / "Leasha"),
                        help="where to stage it (default: Leasha\\ beside the source)")
    parser.add_argument("--python", default=sys.executable,
                        help="the interpreter the launchers use "
                             "(default: the one running this)")
    parser.add_argument("--check", action="store_true",
                        help="run the staged copy afterwards - the part that "
                             "proves the layout works")
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    print(f"Staging {here}  ->  {dest}")
    for line in stage(here, dest, Path(args.python)):
        print(line)
    print(f"\n  {dest}\\Leasha.cmd        the window")
    print(f"  {dest}\\leasha-cli.cmd    the command line")

    if not args.check:
        print("\n  Pass --check to run it. A staged tree nobody starts proves "
              "only that files were copied.")
        return 0

    print("\nRunning the staged copy:")
    failures = check(dest, Path(args.python))
    if failures:
        print(f"\n  {failures} check(s) crashed. The staged layout is wrong, "
              f"not the machine.")
        return 1
    print("\n  It runs from its own folder, with its own configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
