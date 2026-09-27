r"""No Windows-only call outside `app/core/osbridge/` (work order 0x §1c).

**Why this test is load-bearing.** Leasha is being made to run on a Mac as well
as Windows. The rule that keeps that true is simple to say and easy to break
in one line: every call that only exists on Windows lives in one package,
`app/core/osbridge/`, which has a Mac version beside each Windows one. Anywhere
else, a Windows-only call is a crash (or a silent no-op) waiting for the first
Mac user. This test reads every Python file under `app/` and fails if it finds
one.

**How it reads the code: with Python's own parser (`ast`), not text search.**
`ast.parse` turns a file into a tree of what the code *does*. Comments are not
in that tree at all, and docstrings are recognised and skipped, so a sentence
that *mentions* `os.startfile` or PowerShell (there are many - Leasha
recognises `.ps1` files as content) never counts. Only real calls, imports and
string values do.

**What counts as Windows-only** (one detector each, below):

    ctypes      ctypes.windll / WinDLL / WINFUNCTYPE / oledll
    import      winreg, win32com, win32api, win32con, any win32*, pywintypes,
                pythoncom, msvcrt - by `import`, `from ... import` or
                importlib.import_module("...")
    startfile   os.startfile (exists only in Windows builds of Python)
    shell       subprocess.Popen/run/call/check_call/check_output whose
                program is powershell, pwsh or explorer
    exe         a string value that names a program ending in ".exe"
                (".exe" on its own is a file *extension* and is allowed)
    backslash   a "\\" used to join paths: in os.path.join(...) arguments or
                as the separator of "...".join(...)

**The allow-list may only shrink.** Files that still hold Windows-only code
because they have not been moved yet are listed in `ALLOWED`, each with the
reason and the section that will move it. Two rules keep that list honest:

1. A file *not* on the list with a match fails the first test - new
   Windows-only code has to go into osbridge.
2. A file *on* the list with no match any more fails the second test - once
   the code has moved, the entry must come off, so the list can never quietly
   keep permission for something that is no longer needed.

**Every detector is shown to fire.** `test_each_detector_catches_its_violation`
feeds each one a small piece of offending code, so a detector that has
silently stopped matching (a typo in a name, say) is caught here rather than
by a Mac user.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: The repository root: this file is tests/unit/test_osbridge_guard.py.
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
#: The one package allowed to hold Windows-only calls.
OSBRIDGE = "app/core/osbridge/"

#: Files that may still contain Windows-only calls, and why. **May only shrink.**
#: A path is relative to the repository root, with forward slashes.
ALLOWED: dict[str, str] = {
    # --- Owned by order 0w while it is being built; moved after it merges. ---
    # 2026-09-27: workers.py left this list once 0w merged (open_in_explorer
    # now goes through osbridge.show_in_file_manager).
    "app/extract/email_pst.py":
        "Outlook MAPI through pythoncom/win32com is Windows-only by nature: "
        "there is no Mac Outlook COM to bridge to. On a Mac the reader already "
        "uses libpff for a .pst file when it is installed; the COM half stays "
        "Windows-only",
    # --- Parked hardware-specific Mac work (order 0x §P). ---
    "app/core/compute_profile.py":
        "parked (§P): hardware detection - PowerShell DXGI adapter probe and "
        "kernel32 processor topology; the Mac needs sysctl/system_profiler",
    "app/core/volumes_win.py":
        "parked (§P): Offline Media drive identity (kernel32 volume calls, "
        "PowerShell) - a Mac needs its own volume identity",
    "app/core/deeplink.py":
        "parked (§P): the leasha:// URL handler is registered in the Windows "
        "registry; on a Mac it belongs in the .app bundle's Info.plist",
    "app/ui/hotkey.py":
        "parked (§P): the global hotkey (user32 RegisterHotKey) needs macOS "
        "Accessibility permission",
    "app/ui/selection.py":
        "parked (§P): 'search the selected text' (user32 clipboard/SendInput) "
        "needs macOS Accessibility permission",
    "app/ui/tray.py":
        "parked (§P): taskbar identity (shell32 AppUserModelID, win32com "
        "propsys) - Windows shell integration with no Mac counterpart yet",
    # --- Later sections of order 0x. ---
    "app/extract/lo_session.py":
        "later section: the LibreOffice warm session's Windows job object and "
        "msvcrt pipe handles",
    "app/extract/lo_server.py":
        "later section: the job-object code inside the LibreOffice server",
    "app/core/single_instance.py":
        "later section: the one-copy-only guard uses a Windows named mutex "
        "(kernel32); a Mac needs a file lock instead",
}

# ---------------------------------------------------------------------------
# The detectors
# ---------------------------------------------------------------------------

#: Modules that exist only on Windows. Anything starting `win32` (pywin32's
#: `win32api`, `win32con`, `win32gui`, ...) is caught by prefix as well.
WINDOWS_MODULES = {"winreg", "_winreg", "pythoncom", "pywintypes", "msvcrt",
                   "win32com"}
#: The parts of `ctypes` that exist only on Windows.
CTYPES_WINDOWS = {"windll", "WinDLL", "WINFUNCTYPE", "oledll", "OleDLL"}
#: The functions that start a program in the `subprocess` module.
SUBPROCESS_CALLS = {"Popen", "run", "call", "check_call", "check_output"}
#: Windows-only programs that code has been known to start.
WINDOWS_PROGRAMS = {"powershell", "pwsh", "explorer"}
#: A string that *names a program* ending in `.exe`. Two shapes count:
#:
#: - a bare name, `soffice.exe` - no spaces (a sentence is not a program name);
#: - a path, `C:\Program Files\VLC\vlc.exe` - spaces allowed, because
#:   Windows folders have them, but it must contain a `\` or `/`.
#:
#: Neither may hold a wildcard (`*.exe` is a pattern), and `.exe` on its own is
#: a file *extension* Leasha recognises, not a program.
EXE_NAME = re.compile(
    r"(?i)^(?:[^\s*?<>|\"\\/]*|[^*?<>|\"]*[\\/][^*?<>|\"\\/]*)"
    r"[^\s*?<>|\".\\/]\.exe$")


def _is_windows_module(name: str) -> bool:
    """True for `winreg`, `win32com.client`, `win32api`, ... (by first part)."""
    first = name.split(".")[0]
    return first in WINDOWS_MODULES or first.startswith("win32")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of every string that is only a docstring (or a bare string line).

    A string standing alone as a statement does nothing when the code runs -
    it is documentation - so it must not count as a use of anything.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            if (isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)):
                found.add(id(statement.value))
    return found


def _program_of(argument: ast.expr) -> str | None:
    """The program a subprocess call starts, if it is written out literally.

    `Popen(["explorer", "/select,", path])` and `run("powershell -Command x")`
    both name it in their first argument. A program held in a variable cannot
    be read without running the code, so it is not guessed at.
    """
    first = None
    if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
        if isinstance(argument.elts[0], ast.Constant):
            first = argument.elts[0].value
    elif isinstance(argument, ast.Constant):
        first = argument.value
    if not isinstance(first, str) or not first.strip():
        return None
    # Take the first word, then the last part of a path (`C:\...\pwsh.exe`).
    program = re.split(r"[\\/]", first.strip().split(" ")[0])[-1].lower()
    return program[:-4] if program.endswith(".exe") else program


def find_windows_only(source: str) -> list[tuple[int, str, str]]:
    """Every Windows-only use in one file's source: `(line, kind, detail)`."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        # import winreg / import win32com.client
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_windows_module(alias.name):
                    found.append((node.lineno, "import", alias.name))
        # from win32com import client / from ctypes import WinDLL
        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_windows_module(node.module):
                found.append((node.lineno, "import", node.module))
            for alias in node.names:
                if alias.name in CTYPES_WINDOWS:
                    found.append((node.lineno, "ctypes", alias.name))
        # ctypes.windll.kernel32 / ctypes.WinDLL(...) / os.startfile(...)
        elif isinstance(node, ast.Attribute):
            if node.attr in CTYPES_WINDOWS:
                found.append((node.lineno, "ctypes", node.attr))
            if node.attr == "startfile":
                found.append((node.lineno, "startfile", "os.startfile"))
        # WinDLL(...) after `from ctypes import WinDLL`
        elif isinstance(node, ast.Name):
            if node.id in CTYPES_WINDOWS:
                found.append((node.lineno, "ctypes", node.id))
        # "soffice.exe" - but not docstrings, and not the ".exe" extension
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings and EXE_NAME.match(node.value)):
            found.append((node.lineno, "exe", node.value))

        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (function.attr if isinstance(function, ast.Attribute)
                else function.id if isinstance(function, ast.Name) else "")

        # importlib.import_module("winreg") / __import__("msvcrt")
        if (name in ("import_module", "__import__") and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_windows_module(node.args[0].value)):
            found.append((node.lineno, "import", node.args[0].value))

        # subprocess.Popen(["powershell", ...]) and friends
        if name in SUBPROCESS_CALLS and node.args:
            program = _program_of(node.args[0])
            if program in WINDOWS_PROGRAMS:
                found.append((node.lineno, "shell", program))

        # "\\".join(parts) / os.path.join(root, "a\\b")
        if name == "join" and isinstance(function, ast.Attribute):
            separator = function.value
            if (isinstance(separator, ast.Constant)
                    and isinstance(separator.value, str)):
                if "\\" in separator.value:
                    found.append((node.lineno, "backslash", repr(separator.value)))
            else:
                for argument in node.args:
                    if (isinstance(argument, ast.Constant)
                            and isinstance(argument.value, str)
                            and "\\" in argument.value):
                        found.append((node.lineno, "backslash", repr(argument.value)))
    return found


def scan_tree(app_root: Path, repo_root: Path) -> dict[str, list[tuple[int, str, str]]]:
    """Every file under `app_root` (outside osbridge) with a Windows-only use.

    Keys are paths relative to `repo_root` with forward slashes, the form
    `ALLOWED` uses, so the answer is the same on Windows and on a Mac.
    """
    result: dict[str, list[tuple[int, str, str]]] = {}
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        if relative.startswith(OSBRIDGE):
            continue
        hits = find_windows_only(path.read_text(encoding="utf-8"))
        if hits:
            result[relative] = hits
    return result


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def test_no_windows_only_call_outside_osbridge() -> None:
    """The rule itself: Windows-only calls live in `app/core/osbridge/`."""
    offenders = {path: hits for path, hits in scan_tree(APP, ROOT).items()
                 if path not in ALLOWED}
    assert not offenders, (
        "Windows-only calls outside app/core/osbridge/ (work order 0x §1c). "
        "Put the Windows version and a Mac version of the operation in "
        "osbridge and call that instead:\n"
        + "\n".join(f"  {path}:{line}  {kind}  {detail}"
                    for path, hits in offenders.items()
                    for line, kind, detail in hits))


def test_every_allow_listed_file_still_needs_its_entry() -> None:
    """An entry whose file is clean (or gone) must come off: the list only shrinks."""
    found = scan_tree(APP, ROOT)
    stale = sorted(path for path in ALLOWED if path not in found)
    assert not stale, (
        "These files no longer contain a Windows-only call (or no longer "
        "exist). Remove them from ALLOWED in test_osbridge_guard.py:\n  "
        + "\n  ".join(stale))


def test_the_allow_list_names_real_paths_in_the_right_form() -> None:
    """Forward slashes, relative to the repository, under `app/`, not osbridge."""
    for path, reason in ALLOWED.items():
        assert path.startswith("app/") and "\\" not in path, path
        assert not path.startswith(OSBRIDGE), path
        assert reason.strip(), f"{path} needs a reason"


def test_osbridge_itself_is_where_the_windows_calls_now_are() -> None:
    """The moved code really is in osbridge - the scan would find it there.

    Guards against the exclusion hiding a scanner that finds nothing at all.
    """
    bridge = APP / "core" / "osbridge"
    kinds = {kind for path in bridge.glob("*.py")
             for _line, kind, _detail in find_windows_only(path.read_text(encoding="utf-8"))}
    assert {"ctypes", "startfile", "shell", "exe"} <= kinds


@pytest.mark.parametrize("kind,source", [
    ("ctypes", "import ctypes\nk = ctypes.windll.kernel32\n"),
    ("ctypes", "import ctypes\nk = ctypes.WinDLL('kernel32')\n"),
    ("ctypes", "from ctypes import WinDLL\n"),
    ("ctypes", "import ctypes\nF = ctypes.WINFUNCTYPE(None)\n"),
    ("import", "import winreg\n"),
    ("import", "from win32com import client\n"),
    ("import", "import win32api\n"),
    ("import", "import win32con\n"),
    ("import", "import pywintypes\n"),
    ("import", "import pythoncom\n"),
    ("import", "import msvcrt\n"),
    ("import", "import importlib\nm = importlib.import_module('winreg')\n"),
    ("startfile", "import os\nos.startfile('a.pdf')\n"),
    ("shell", "import subprocess\nsubprocess.run(['powershell', '-Command', 'x'])\n"),
    ("shell", "import subprocess\nsubprocess.Popen(['explorer', '/select,', 'a'])\n"),
    ("shell", "import subprocess\nsubprocess.check_output('pwsh -NoProfile')\n"),
    ("exe", "PROGRAM = 'soffice.exe'\n"),
    ("exe", "PROGRAM = r'C:\\Program Files\\VLC\\vlc.exe'\n"),
    ("backslash", "import os\np = os.path.join(root, 'a\\\\b')\n"),
    ("backslash", "p = '\\\\'.join(['C:', 'Users'])\n"),
])
def test_each_detector_catches_its_violation(kind: str, source: str) -> None:
    """Each detector fires on a deliberate violation (kept here, never in `app/`)."""
    kinds = {found_kind for _line, found_kind, _detail in find_windows_only(source)}
    assert kind in kinds, f"the {kind!r} detector missed: {source!r}"


@pytest.mark.parametrize("source", [
    '"""We used to call os.startfile and powershell here."""\n',
    "# ctypes.windll.kernel32 in a comment\nx = 1\n",
    "EXTENSIONS = {'.exe', '.ps1'}\n",
    "PATTERN = '*.exe'\n",
    "MESSAGE = 'Run venv\\\\Scripts\\\\python.exe -m app.cli diagnose'\n",
    "import os\np = os.path.join(root, 'Programs')\n",
    "p = '/'.join(['a', 'b'])\n",
    "import subprocess\nsubprocess.run(['git', 'status'])\n",
    "def f():\n    '''Not os.startfile either.'''\n    return 1\n",
])
def test_mentions_and_portable_code_are_not_violations(source: str) -> None:
    """Comments, docstrings, file extensions and sentences are not calls."""
    assert find_windows_only(source) == []


def test_a_new_file_with_a_violation_fails_the_guard(tmp_path: Path) -> None:
    """End to end on a throwaway tree: a violation outside osbridge is reported,
    the same violation inside osbridge is not, and nothing touches `app/`."""
    app = tmp_path / "app"
    (app / "core" / "osbridge").mkdir(parents=True)
    (app / "feature").mkdir()
    (app / "feature" / "opener.py").write_text(
        "import os\n\ndef go(p):\n    os.startfile(p)\n", encoding="utf-8")
    (app / "core" / "osbridge" / "launch.py").write_text(
        "import os\n\ndef go(p):\n    os.startfile(p)\n", encoding="utf-8")

    found = scan_tree(app, tmp_path)

    assert list(found) == ["app/feature/opener.py"]
    assert found["app/feature/opener.py"][0][1] == "startfile"
