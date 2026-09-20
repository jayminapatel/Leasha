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

    run-install.cmd             build this tree's own venv (once)
    leasha.cmd                  the window
    leasha.cmd stats            anything else goes to the command line

Before `run-install.cmd` has been run there is no venv here, and `leasha.cmd`
says so rather than starting. To try the tree without installing anything:

    Leasha-staged.cmd           the window, borrowing the staging interpreter
    leasha-cli.cmd stats        the command line, same interpreter

Those two prefer this tree's own `venv\\` the moment one exists, so they keep
working after an install rather than quietly running the interpreter that
staged them.

Configuration lives in `.env` in this folder, and is written by the application
on first run. It is never copied from the development tree: those are somebody
else's paths.

Re-staging replaces only what the previous staging wrote - recorded in
`.staged-manifest.json`. Your `.env`, `venv\\`, `logs\\` and any index folder
here are left alone.
"""

#: Appended to the README when `--with-tests` was used, because at that point
#: the tree is no longer what a customer would receive and saying so matters.
WITH_TESTS_NOTE = """
This tree was staged WITH TESTS (`--with-tests`), so it also holds `tests\\` and
`pyproject.toml`. That is not what ships - it is here so the suite can be run
against the production layout:

    venv\\Scripts\\python.exe -m pytest tests -q

Re-stage without the flag to get a tree that matches a real install.
"""


#: Added by `--with-tests`, so the suite can be run against the production tree.
#:
#: `pyproject.toml` is not optional here: it carries the pytest configuration -
#: `--basetemp`, the strict markers, the `jvm` exclusion - and without it the
#: suite runs with different settings in the staged tree than in development,
#: which makes any difference in the results meaningless.
TEST_EXTRAS: tuple[str, ...] = ("tests", "pyproject.toml")

#: Record of what the last staging wrote, kept in the staged tree.
#:
#: **This file is why a re-stage no longer deletes the whole destination.** See
#: `stage()`.
MANIFEST = ".staged-manifest.json"


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED}


def _ignore_keeping_fixtures(_directory: str, names: list[str]) -> set[str]:
    """For `tests/`, where `fixtures` is content rather than something to skip."""
    keep = {"tests", "fixtures"}
    return {name for name in names if name in EXCLUDED and name not in keep}


def _remove(target: Path) -> None:
    """Delete a file or folder that an earlier staging put there.

    **A read-only file must not stop a re-stage.** `shutil.copy2` keeps the
    read-only attribute, so a shipped file that is read-only in the source (one
    of the logo files is) arrives read-only in the destination, and on Windows
    `rmtree` then refuses it with `WinError 5: Access is denied` - the second
    stage over an existing install failed for as long as any such file shipped.
    Found 2026-09-20 by `test_staging.py`, which had been failing on the
    developer's own machine. The file is made writable and the delete retried.
    """
    import os
    import stat

    def make_writable_and_retry(function, path, *_error):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    if target.is_dir() and not target.is_symlink():
        if sys.version_info >= (3, 12):
            shutil.rmtree(target, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(target, onerror=make_writable_and_retry)
    elif target.exists():
        try:
            target.unlink()
        except PermissionError:
            os.chmod(target, stat.S_IWRITE)
            target.unlink()


def _previous(dest: Path) -> list[str]:
    """What the last staging wrote here, or [] if this tree is new or foreign."""
    import json

    try:
        data = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
        return [str(name) for name in data["wrote"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def stage(source: Path, dest: Path, python: Path, with_tests: bool = False) -> list[str]:
    r"""Copy the shipped tree into `dest`. Returns what was written.

    **Only what a previous staging wrote is removed, never the whole folder.**

    It used to be `shutil.rmtree(dest)`, on the reasoning that a stale file from
    an old layout makes a staged tree lie about what ships. That reasoning is
    right and the remedy was catastrophic: the moment `--dest D:\Leasha` is a
    real installation, that folder also holds `.env`, a `venv\`, the logs, and -
    if the index was moved there, which is the documented thing to do -
    `D:\Leasha\Data`, a hundred gigabytes that took hours to build. Re-staging a
    fixed typo would have deleted all of it, silently, with no confirmation.

    So each staging records what it wrote in `.staged-manifest.json`, and the
    next one deletes exactly that and nothing else. Stale files from an old
    layout are still removed - they are in the previous manifest - and anything
    the manifest does not name is somebody's data and is left alone.

    A destination that was never staged (no manifest) is treated as foreign:
    nothing is deleted, and existing files are overwritten in place.
    """
    import json

    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if dest == source:
        raise SystemExit("--dest cannot be the development tree itself.")

    # A destination inside the source would copy the tree into itself.
    if dest.is_relative_to(source) and dest.parent == source and dest.name in SHIPPED:
        raise SystemExit(f"--dest cannot be {dest}: it is part of the source tree.")

    for name in _previous(dest):
        target = dest / name
        try:
            _remove(target)
        except OSError as exc:
            raise SystemExit(f"could not replace {target}: {exc}")

    dest.mkdir(parents=True, exist_ok=True)

    names = SHIPPED + (TEST_EXTRAS if with_tests else ())
    written: list[str] = []
    produced: list[str] = []

    for name in names:
        origin = source / name
        if not origin.exists():
            # Named but absent is worth saying: it means this list and the
            # repository have drifted, which is the failure staging exists to
            # make visible rather than the one it should hide.
            written.append(f"  MISSING  {name}")
            continue
        target = dest / name
        if target.exists():
            # Only reachable for a foreign destination - a staged one had its
            # previous manifest removed above.
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if origin.is_dir():
            ignore = _ignore_keeping_fixtures if name == "tests" else _ignore
            shutil.copytree(origin, target, ignore=ignore)
        else:
            shutil.copy2(origin, target)
        written.append(f"  {name}")
        produced.append(name)

    (dest / "README.txt").write_text(
        README + (WITH_TESTS_NOTE if with_tests else ""), encoding="utf-8")
    _launchers(dest, python)
    produced.extend(["README.txt", STAGED_WINDOW, STAGED_CLI])

    # Created rather than copied: an installed copy has an empty log folder,
    # not the development tree's history. **Not recorded in the manifest** - a
    # re-stage must not delete the logs of a running installation.
    (dest / "logs").mkdir(exist_ok=True)

    (dest / MANIFEST).write_text(
        json.dumps({"wrote": sorted(produced), "with_tests": with_tests}, indent=2),
        encoding="utf-8")

    return written


#: The generated launchers. **Neither may collide with a shipped name, and
#: `Leasha.cmd` did.**
#:
#: Windows filenames are case-insensitive, so the generated `Leasha.cmd` and the
#: shipped `leasha.cmd` are one file - and whichever is written last wins. The
#: generated one always was. That silently replaced the real launcher, which
#: checks for a local venv, prints "Leasha is not installed yet" when there is
#: none, and passes arguments through to the CLI, with a three-line stub
#: hardcoding whatever interpreter happened to run the staging script.
#:
#: On a production tree at `D:\Leasha` with its own venv, that meant the
#: launcher ran the *development* interpreter from `D:\SearchProject\venv` -
#: reading the development tree's packages while claiming to be a clean install,
#: which is the exact confusion staging exists to remove. A test now asserts the
#: collision cannot come back.
STAGED_WINDOW = "Leasha-staged.cmd"
STAGED_CLI = "leasha-cli.cmd"


def _launcher_body(python: Path, module: str) -> str:
    r"""A launcher that prefers the tree's own venv, then the staged interpreter.

    **`cd /d "%~dp0"` first.** Everything the application finds - `.env`,
    `logs\`, `config\` - is resolved from its own location, and a launcher that
    inherits whatever directory the shortcut happened to start in is how a
    staged tree quietly reads the development tree's configuration.

    The venv check is what makes one file work for both cases: a staged tree
    with no venv borrows the interpreter it was staged with, and the same tree
    after `run-install.cmd` uses its own without being regenerated.
    """
    return (
        "@echo off\r\n"
        "REM Generated by scripts/stage.py. Do not edit - it is rewritten.\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if exist \"venv\\Scripts\\python.exe\" (\r\n"
        f"  venv\\Scripts\\python.exe -m {module} %*\r\n"
        ") else (\r\n"
        f"  \"{python}\" -m {module} %*\r\n"
        ")\r\n"
        "exit /b %ERRORLEVEL%\r\n"
    )


def _launchers(dest: Path, python: Path) -> None:
    """The two generated `.cmd` files: the window, and the command line."""
    (dest / STAGED_WINDOW).write_text(_launcher_body(python, "app.main"), encoding="utf-8")
    (dest / STAGED_CLI).write_text(_launcher_body(python, "app.cli"), encoding="utf-8")


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
    parser.add_argument("--with-tests", action="store_true",
                        help="also stage tests\\ and pyproject.toml, so the "
                             "suite can be run against the production layout. "
                             "Not what ships.")
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    print(f"Staging {here}  ->  {dest}")
    previous = _previous(dest)
    if dest.exists() and not previous:
        # A folder nobody staged before. Say so rather than quietly writing into
        # it - this is the case where somebody typed the wrong path.
        print(f"  note: {dest} exists and was not staged by this script. "
              "Nothing there will be deleted; matching names are overwritten.")
    for line in stage(here, dest, Path(args.python), with_tests=args.with_tests):
        print(line)
    print(f"\n  {dest}\\leasha.cmd            the window (uses this tree's venv)")
    print(f"  {dest}\\{STAGED_WINDOW}   the window, without installing first")
    print(f"  {dest}\\{STAGED_CLI}    the command line")
    if args.with_tests:
        print(f"  {dest}\\tests             staged too - run pytest from there")

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
