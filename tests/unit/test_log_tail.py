r"""Following today's application log file for the System log tab. Qt-free.

Layer: L0

The Indexing cockpit's System log tab (2026-10-14) shows the application log
*file*, because an index run in its own process logs there and never into the
window's in-memory ring. What these pin, on a temporary folder:

* the first read takes only the end of a long file, starting at a whole line;
* each later read returns only the lines appended since, and a line still
  being written waits for its newline;
* at midnight the rest of yesterday's file is read, then today's from its start;
* a file that was replaced (shorter than where reading had got to) is read again
  from its start, and a missing file is not an error;
* lines come out in the console's shape, so the pane colours a warning from the
  indexing process exactly as one from the window.
"""

from __future__ import annotations

from datetime import date

from app.core.log_tail import LogTail, console_form, log_file_for
from app.ui.log_lines import WARN, level_of

DAY1 = date(2026, 10, 14)
DAY2 = date(2026, 10, 15)


def _line(n: int, level: str = "INFO", message: str = "") -> str:
    return (f"2026-10-14 09:30:{n % 60:02d}.123 | {level: <8} | index.pipeline | - | "
            f"app.index.pipeline:run:{n} | {message or f'line {n}'}\n")


def _append(path, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(text)


def test_a_file_line_becomes_the_console_line_the_pane_colours() -> None:
    line = _line(5, "WARNING", "Could not read D:\\a.pdf").rstrip("\n")
    shown = console_form(line)
    assert shown == "09:30:05 WARNING  index.pipeline Could not read D:\\a.pdf"
    assert level_of(shown) == WARN
    # A traceback's continuation line is left alone.
    assert console_form("  File \"x.py\", line 3") == "  File \"x.py\", line 3"


def test_only_new_lines_are_returned(tmp_path) -> None:
    path = log_file_for(tmp_path, DAY1)
    _append(path, _line(1) + _line(2))
    tail = LogTail(tmp_path, today=lambda: DAY1)
    first = tail.poll()
    assert [line.split()[-1] for line in first] == ["1", "2"]
    assert tail.poll() == []

    _append(path, _line(3))
    assert [line.split()[-1] for line in tail.poll()] == ["3"]
    assert [line.split()[-1] for line in tail.lines()] == ["1", "2", "3"]
    assert tail.lines(2) == tail.lines()[-2:]


def test_a_line_still_being_written_waits_for_its_end(tmp_path) -> None:
    path = log_file_for(tmp_path, DAY1)
    _append(path, _line(1))
    tail = LogTail(tmp_path, today=lambda: DAY1)
    tail.poll()
    half = _line(2)
    _append(path, half[:20])
    assert tail.poll() == []
    _append(path, half[20:])
    assert [line.split()[-1] for line in tail.poll()] == ["2"]


def test_the_first_read_takes_the_end_of_a_long_file_from_a_whole_line(tmp_path) -> None:
    path = log_file_for(tmp_path, DAY1)
    _append(path, "".join(_line(n) for n in range(1, 2001)))
    tail = LogTail(tmp_path, today=lambda: DAY1, first_read=4096)
    lines = tail.poll()
    assert 0 < len(lines) < 100
    assert lines[-1].endswith("line 2000")
    # The first kept line is a whole one - never the tail end of a cut line.
    assert all(line[:8].count(":") == 2 for line in lines)


def test_midnight_finishes_yesterday_then_reads_today_from_the_start(tmp_path) -> None:
    today = [DAY1]
    first = log_file_for(tmp_path, DAY1)
    _append(first, _line(1))
    tail = LogTail(tmp_path, today=lambda: today[0])
    tail.poll()
    # Just before midnight a last line lands in yesterday's file ...
    _append(first, _line(2, message="last of the day"))
    # ... and after it the new day's file has its first lines.
    second = log_file_for(tmp_path, DAY2)
    _append(second, _line(3, message="first of the day") + _line(4))
    today[0] = DAY2
    new = tail.poll()
    assert [line.split("index.pipeline ", 1)[1] for line in new] == [
        "last of the day", "first of the day", "line 4"]
    assert tail.path == second
    _append(second, _line(5))
    assert [line.split()[-1] for line in tail.poll()] == ["5"]


def test_a_missing_file_is_nothing_yet_and_read_whole_when_it_appears(tmp_path) -> None:
    tail = LogTail(tmp_path, today=lambda: DAY1)
    assert tail.poll() == []
    _append(log_file_for(tmp_path, DAY1), _line(1) + _line(2))
    assert len(tail.poll()) == 2


def test_a_replaced_file_is_read_again_from_its_start(tmp_path) -> None:
    path = log_file_for(tmp_path, DAY1)
    _append(path, _line(1) + _line(2) + _line(3))
    tail = LogTail(tmp_path, today=lambda: DAY1)
    tail.poll()
    path.write_text(_line(9), encoding="utf-8")
    assert [line.split()[-1] for line in tail.poll()] == ["9"]


def test_an_unreadable_folder_never_raises(tmp_path) -> None:
    tail = LogTail(tmp_path / "no" / "such", today=lambda: DAY1)
    assert tail.poll() == [] and tail.lines() == []
