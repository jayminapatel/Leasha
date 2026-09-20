r"""Nothing sets a stylesheet on the whole application - not the app, not a test.

Layer: L0 - repo hygiene, stdlib only.

**The crash this prevents.** Three suite runs on 2026-09-20 died with `0xC0000005` and
then `0xC0000374`, always inside one rail test that passes perfectly well on its own, and
at `-j 4` and again at `-j 3` with 14.8 GB free - so it was not memory, though that was
the first (wrong) answer. The test called `QApplication.setStyleSheet`, which makes Qt
re-polish **every** widget alive in the process. Tests build real widgets and let Python
drop them without `deleteLater`, so on a long run that walk eventually reaches a C++
object that is already gone, and the process dies pointing at whichever test was
unlucky. Reproduced deliberately with the fourteen files that precede it - and neither
half of those crashes alone, which is the tell: it is the *number* of leaked widgets,
not one culprit file.

**The application itself never does this**, which is why the window has never crashed
this way: `MainWindow._apply_theme` calls `self.setStyleSheet(...)`, so Qt re-polishes
that window's own tree - widgets its parent owns and keeps alive - rather than every
widget in the process. `test_the_app_themes_its_window_not_the_application` below is what
keeps that true, because switching it to the application-wide call would be a one-word
change with a crash at the end of it.

The leak itself is not fixed. This makes the leak harmless rather than fatal, which is
the honest description: a test that wants themed metrics styles the widget it is testing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `something.setStyleSheet(...)` where `something` is an application, not a widget.
APP_STYLESHEET = re.compile(
    r"\b(?:qapp|app|application|qt_app|QApplication\.instance\(\)|qApp)\s*\.setStyleSheet\s*\(",
    re.IGNORECASE,
)


def _python_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_test_styles_the_whole_application():
    offenders = []
    for path in _python_files(ROOT / "tests"):
        if path.name == Path(__file__).name:
            continue                                    # this file quotes the pattern
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if APP_STYLESHEET.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()}")
    assert not offenders, (
        "a test sets a stylesheet on the whole application, which re-polishes every widget "
        "alive in the process and dies on the first one an earlier test leaked - see this "
        "module's docstring. Style the widget under test instead:\n  " + "\n  ".join(offenders))


def test_no_application_code_styles_the_whole_application():
    offenders = []
    for path in _python_files(ROOT / "app"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if APP_STYLESHEET.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()}")
    assert not offenders, (
        "application code sets a stylesheet on the whole application; theme the window "
        "instead, as `MainWindow._apply_theme` does:\n  " + "\n  ".join(offenders))


def test_the_app_themes_its_window_not_the_application():
    """The positive half: the theme really is applied, and to the window."""
    shell = (ROOT / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
    assert "self.setStyleSheet(stylesheet(" in shell, (
        "MainWindow no longer themes itself with self.setStyleSheet - if the theme moved, "
        "check it did not move to the application, and update this test's reasoning")


def test_the_guard_would_catch_the_line_that_crashed():
    """A guard that cannot fail proves nothing - these are the real shapes."""
    for line in ('    qapp.setStyleSheet(theme.stylesheet("light"))',
                 "app.setStyleSheet(sheet)",
                 "QApplication.instance().setStyleSheet(sheet)",
                 "qApp.setStyleSheet(sheet)"):
        assert APP_STYLESHEET.search(line), line
    for line in ("rail.setStyleSheet(sheet)", "self.setStyleSheet(sheet)",
                 "self.problem.setStyleSheet('color: #c62828;')"):
        assert not APP_STYLESHEET.search(line), f"false positive on {line}"
