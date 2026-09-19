"""The progress line `index` and `scan` both draw."""

from __future__ import annotations

import sys
from typing import Any


class ProgressLine:
    """A single console line that redraws in place, and yields to log output.

    **The problem this solves.** A `\r` progress line and a logger writing to the
    same console fight each other: a warning lands on top of the progress line,
    the carriage return then overwrites the warning, and the result is a mangled
    line that stops updating - which reads exactly like "it stopped working".
    That was the report.

    So anything that writes to the console goes through `interrupt()`, which
    wipes the line first, lets the message land on its own, and repaints. It is
    the same discipline `pip` and `apt` use for the same reason.

    Not a curses dependency and not ANSI cursor codes: one carriage return and
    some spaces, which behaves identically in a plain console, in Windows
    Terminal, and when the output is piped to a file.
    """

    def __init__(self, enabled: bool = True, width: int = 118) -> None:
        self.enabled = enabled and sys.stdout.isatty()
        self.width = width
        self._text = ""

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        self._text = text[: self.width]
        print("\r" + self._text.ljust(self.width), end="", flush=True)

    def clear(self) -> None:
        """Wipe the line so something else can print on it."""
        if self.enabled and self._text:
            print("\r" + " " * self.width + "\r", end="", flush=True)

    def repaint(self) -> None:
        if self.enabled and self._text:
            print("\r" + self._text.ljust(self.width), end="", flush=True)

    def finish(self) -> None:
        self.clear()
        self._text = ""


def _console_sink(progress: ProgressLine):
    """A loguru sink that never lands on top of the progress line."""
    def write(message: Any) -> None:
        progress.clear()
        print(str(message).rstrip(), file=sys.stderr, flush=True)
        progress.repaint()

    return write
