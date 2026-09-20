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

**Note 2026-09-20 - the leak was measured, and deliberately left.** The obvious follow-up
was an autouse teardown in `tests/conftest.py` deleting every leftover widget between
tests. It was investigated and not done, on three findings:

1. *The leak is real and large.* Counted with a read-only pytest plugin over six Qt-heavy
   files: `test_tuning_screen.py` leaves **6 top-level widgets and ~120 widgets** behind on
   every one of its 13 tests, and 26 of the 238 tests in that subset grew the count.
   So the problem is not imagined, and it is not one careless file.
2. *Nothing walks them any more.* The only process-wide widget walk left anywhere in
   `app/` or `tests/` is `close_windows.py`'s `QApplication.topLevelWidgets()`, which
   iterates **live** objects and closes them - it cannot reach a corpse. The two guards
   above keep `setStyleSheet` from coming back, and a grep for `setPalette`/`setFont`/
   `setStyle` on the application finds none. The mechanism that made the leak fatal is
   gone, not merely avoided.
3. *The cure is riskier than the disease, today.* `tests/unit/conftest.py`'s
   `gui_mainwindow` states plainly that tearing a `MainWindow` down mid-process is itself
   a crash, and 60 unit modules hold module-scoped Qt fixtures a blanket teardown would
   have to leave alone. A per-test snapshot handles those correctly - higher-scoped
   fixtures are built first, so a fixture's window is never new - but it cannot see a
   widget a test parked in a module-level global for a later test, and that failure mode
   is a red suite discovered by somebody else, days later.

Verified after the `setStyleSheet` fix: the fifteen-file combination that reproduced the
crash (`test_timeline_entry_points.py` … `test_ui_aesthetics.py`) now runs **786 passed,
exit 0**. What exists instead of the blanket fixture is an opt-in one,
`no_leaked_widgets` in `tests/unit/conftest.py`, so a module that wants the cleanup can
take it after proving its own file still passes - which is the order this should happen in.
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
