r"""Syntax colouring for the preview pane. No library, and that is deliberate.

Layer: L5

**Pygments was the obvious answer and is the wrong one here.** It is a large
dependency carrying six hundred lexers to solve a problem that, in a preview
pane, is "make the strings and the comments a different colour so the eye can
find the shape of the thing". This is a table and a dozen regexes, has no
install step, cannot fail at import, and never becomes a reason the application
will not start on a machine with no network.

**It is not a parser and does not pretend to be.** A regex highlighter gets
nested escapes and some template syntax wrong, and the failure mode is one word
in the wrong colour in a pane nobody edits in - not a wrong search result, not a
corrupted file. That trade is acceptable *because* this is a read-only preview;
the same code behind an editor would be a bug factory.

**Colours come from the palette, not from constants.** A hard-coded `#008000`
comment green is unreadable on a dark background, which is exactly the failure
`test_forced_theme_stays_readable` exists to catch. Every colour here is mixed
from the widget's own palette, so a forced dark theme, a forced light theme and
the system default all stay legible without three sets of constants.

The grammars themselves are in `app/ui/grammars.py`, which imports no Qt - a
regex that over-matches has to be catchable by a test, and a test that needs a
display is a test that does not run.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat

from app.ui.grammars import BLOCK_COMMENTS, combined, language_for

__all__ = ["CodeHighlighter", "language_for"]


class CodeHighlighter(QSyntaxHighlighter):
    """Colours one `QTextDocument` according to one grammar.

    Re-pointed rather than rebuilt when the preview changes file: constructing a
    highlighter per selection leaks a rule set for every row arrowed past, and
    `setLanguage` is the whole of what needs to change.
    """

    def __init__(self, document: Any, palette: QPalette,
                 language: Optional[str] = None) -> None:
        super().__init__(document)
        self._current: Optional[str] = None
        self._formats = _formats_for(palette)
        self._scanner: Optional[QRegularExpression] = None
        self._roles: list[str] = []
        self._block: Optional[tuple[QRegularExpression, QRegularExpression]] = None
        self.setLanguage(language)

    @property
    def language(self) -> Optional[str]:
        """The grammar in force, or None for plain text."""
        return self._current

    def setLanguage(self, language: Optional[str]) -> None:   # noqa: N802 - Qt's naming
        """Swap the rule set. Rehighlights, because the old colours are wrong."""
        if language == self._current and self._scanner is not None:
            return                       # arrowing through files of one kind
        self._current = language
        self._recompile()
        pair = BLOCK_COMMENTS.get(language or "")
        self._block = (
            (QRegularExpression(pair[0]), QRegularExpression(pair[1]))
            if pair else None
        )
        self.rehighlight()

    def setPalette(self, palette: QPalette) -> None:          # noqa: N802 - Qt's naming
        """Follow a theme change. The document keeps its text; the colours move."""
        self._formats = _formats_for(palette)
        self._recompile()
        self.rehighlight()

    def _recompile(self) -> None:
        """Build the one combined scanner for the current grammar (`grammars.combined`)."""
        pattern, roles = combined(self._current)
        self._scanner = QRegularExpression(pattern) if pattern else None
        self._roles = roles

    def highlightBlock(self, text: str) -> None:              # noqa: N802 - Qt's hook
        """One scan, first alternative wins - see `grammars.patterns_for`.

        Painting rule by rule and letting the last one win cannot express what
        is wanted: a keyword inside a string and a `//` inside a string both
        have to lose to the string, and under last-wins one of them always won.
        """
        if self._scanner is not None:
            found = self._scanner.globalMatch(text)
            while found.hasNext():
                match = found.next()
                for index, role in enumerate(self._roles, start=1):
                    start = match.capturedStart(index)
                    if start >= 0:
                        self.setFormat(start, match.capturedLength(index),
                                       self._formats[role])
                        break
        self._paint_block_comment(text)

    def _paint_block_comment(self, text: str) -> None:
        """`/* ... */` spanning lines, via Qt's per-block state.

        Without this a comment opened on one line stops colouring at the newline
        and everything under it is painted as code - which is precisely the case
        a reader most needs the colour for.
        """
        if self._block is None:
            self.setCurrentBlockState(0)
            return
        opener, closer = self._block
        form = self._formats["comment"]

        start = 0 if self.previousBlockState() == 1 else -1
        if start < 0:
            found = opener.match(text)
            start = found.capturedStart() if found.hasMatch() else -1

        while start >= 0:
            ended = closer.match(text, start)
            if not ended.hasMatch():
                self.setCurrentBlockState(1)
                self.setFormat(start, len(text) - start, form)
                return
            finish = ended.capturedEnd()
            self.setFormat(start, finish - start, form)
            following = opener.match(text, finish)
            start = following.capturedStart() if following.hasMatch() else -1
        self.setCurrentBlockState(0)


def _formats_for(palette: QPalette) -> dict[str, QTextCharFormat]:
    """Five roles, mixed from the palette so any theme stays readable.

    Lightness is chosen from the *background*: light ink on a dark ground, dark
    ink on a light one. Asserting a hex value instead is how a highlighter ends
    up invisible under a forced theme, which is a bug the reader cannot work
    around and cannot report clearly.
    """
    ground = palette.color(QPalette.ColorRole.Base)
    dark = ground.lightness() < 128
    lightness = 190 if dark else 80

    def role(hue: int, saturation: int, *, italic: bool = False,
             bold: bool = False, faded: bool = False) -> QTextCharFormat:
        colour = QColor.fromHsl(hue, saturation, lightness)
        if faded:
            colour = _blend(colour, ground, 0.35)
        form = QTextCharFormat()
        form.setForeground(colour)
        if italic:
            form.setFontItalic(True)
        if bold:
            form.setFontWeight(QFont.Weight.DemiBold)
        return form

    plain = QTextCharFormat()
    plain.setForeground(palette.color(QPalette.ColorRole.Text))
    return {
        "keyword": role(210, 120, bold=True),      # blue
        "string": role(20, 130),                   # warm
        "comment": role(120, 60, italic=True, faded=True),
        "number": role(280, 110),                  # violet
        "constant": role(340, 120),                # rose
        "plain": plain,
    }


def _blend(colour: QColor, towards: QColor, amount: float) -> QColor:
    """`colour` moved `amount` of the way to `towards`. For the faded roles."""
    keep = 1.0 - amount
    return QColor(
        int(colour.red() * keep + towards.red() * amount),
        int(colour.green() * keep + towards.green() * amount),
        int(colour.blue() * keep + towards.blue() * amount),
    )


