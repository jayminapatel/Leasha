"""An answer, drawn as markdown that is still being written.

Layer: L5 view

The Chat tab's assistant bubbles show what the model wrote: bold and italic,
headings, bullet and numbered lists, tables, inline code and fenced code blocks
with a Copy button on each. `AnswerBody` is that body, one widget the bubble
hands the raw text to every ~40 ms while tokens arrive.

**Streaming without flicker is the design, not a polish.**

* The text is cut into *segments*: prose (rendered by `QTextDocument.setMarkdown`
  into a read-only, frameless, auto-height `QTextBrowser`) and fenced code (a
  framed widget with a language label, a Copy button and a monospace view).
* `set_text` keeps the segment widgets alive and updates them in place: same
  index and same kind means "give it the new content" (skipped outright when
  it is unchanged); a widget is only ever added or removed at the END. A code
  fence that has opened and not yet closed is a code segment already, so it
  never flickers into prose and back.
* Only the tail is unsettled. Half a marker (`[1`), half a fence (```` `` ````),
  an unclosed `**` or a lone backtick are handled for display only - held back
  or closed - so the text above the growing tail never changes and a line
  somebody is reading does not move.
* While a prose segment is the tail its height only grows; `final=True` (or the
  next segment arriving) lets it settle to its exact height.

Source markers (`[3]`, `[1][2]`, `[1, 2]`) become small raised numbers; a
number that has a source behind it (`linkable`) is a link, the rest are plain
raised text. Inside code they are code, and untouched.

Colours are the theme's tokens (`app.ui.theme.theme_colours()`), read when the
widget is built and again whenever the window's stylesheet changes, so a theme
switch repaints an answer that is already on screen.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QGuiApplication, QPalette,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat, QTextFrame, QTextFrameFormat, QTextOption, QTextTable,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget,
)

from app.ui.theme import RADIUS, font_sizes, theme_colours

__all__ = [
    "AnswerBody", "render_markdown_html", "strip_markers", "split_segments",
    "prepare_prose", "Segment", "COPIED_MS",
]

#: How long the Copy button says "Copied" before it goes back to "Copy".
COPIED_MS = 1500
COPY_TEXT = "Copy"
COPIED_TEXT = "Copied"
#: The scheme of a source marker's link; never a real destination.
_RECEIPT = "leasha-receipt:"

#: Heading size, as a multiple of body text. Modest on purpose: this is a chat
#: message, not a web page, so h1 is only a step above the prose around it.
_HEADING_SCALE = {1: 1.25, 2: 1.15, 3: 1.08}
#: Space between paragraphs, in px; list items sit closer together.
_PARAGRAPH_GAP = 9.0
_LIST_GAP = 3.0
_INDENT_PX = 22.0
_CODE_SCALE = 0.95

# ---------------------------------------------------------------------------
# Pure text handling (no Qt)
# ---------------------------------------------------------------------------

#: `[3]`, `[1, 2]` - the same shape the engine writes (`presenter.chat.MARKER`),
#: allowing a list. `[1](url)` is a markdown link, not a source marker.
_MARKER = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?!\()")
#: `_emphasis_` and `__strong__`. Qt's markdown reader draws both as underline,
#: which is not what anybody who typed them meant; they become `*` and `**`.
#: Intra-word (`snake_case`) and path-like (`/a_b_/`) underscores are left alone.
_UNDER_STRONG = re.compile(r"(?<![\w/.:=-])__(?=\S)([^\n]+?)(?<=\S)__(?![\w/=-])")
_UNDER_EM = re.compile(r"(?<![\w/.:=_-])_(?=[^\s_])([^\n_]+?)(?<=[^\s_])_(?![\w/=_-])")
#: A marker still arriving at the very end of the text.
_OPEN_MARKER_TAIL = re.compile(r"\[[\d,\s]{0,24}$")
#: Inline code, one line: `x`, ``x`y``. Markers inside are code, not markers.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([^\n]+?)(?<!`)\1(?!`)")

_FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$")
_FENCE_CLOSE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
#: A thematic break on its own line (`---`, `***`, `___`, `- - -`).
_RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")


@dataclass
class Segment:
    """One run of the message: prose, or one fenced code block."""

    kind: str                # "prose" | "code" | "rule"
    text: str
    lang: str = ""
    closed: bool = True      # a code segment whose closing fence has arrived
    joined: bool = False     # prose only: continues the prose segment above it


def _fence_roles(lines: list[str]) -> list[str]:
    """Each line's role: prose / open / code / close. One place that knows
    what a fence is, shared by segmenting and marker stripping."""
    roles: list[str] = []
    fence: Optional[tuple[str, int]] = None
    for line in lines:
        if fence is None:
            match = _FENCE_OPEN.match(line)
            if match:
                roles.append("open")
                fence = (match.group(2)[0], len(match.group(2)))
            else:
                roles.append("prose")
        else:
            match = _FENCE_CLOSE.match(line)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]:
                roles.append("close")
                fence = None
            else:
                roles.append("code")
    return roles


def _dedent(line: str, width: int) -> str:
    """Drop up to `width` leading blanks (a fence nested in a list item)."""
    cut = 0
    while cut < width and cut < len(line) and line[cut] in " \t":
        cut += 1
    return line[cut:]


#: Prose is re-rendered whole on every tick, so a very long run is cut into
#: chunks at paragraph boundaries and only the last one is redone. A chunk is at
#: least this many characters.
CHUNK_CHARS = 700
_BLOCK_CONTINUES = re.compile(r"^(\d+[.)]\s|[-*+]\s|>|\||\s)")


def _chunk_prose(text: str) -> list[str]:
    """Cut a long prose run at safe blank lines: the next line starts a new
    block at column 0 (not a list item, quote, table row or indented line, all
    of which can continue what is above). Boundaries never move as text grows."""
    if len(text) < 2 * CHUNK_CHARS:
        return [text]
    lines = text.split("\n")
    pieces: list[str] = []
    start = size = 0
    for i, line in enumerate(lines):
        size += len(line) + 1
        if line.strip() or size < CHUNK_CHARS:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        # A last line still being typed ("1", "-") may yet turn out to be a list
        # item; a boundary must never appear and then vanish, so wait for it.
        settled = j < len(lines) - 1 or (j == len(lines) - 1 and len(lines[j].strip()) >= 5)
        if settled and not _BLOCK_CONTINUES.match(lines[j]):
            piece = "\n".join(lines[start:i]).strip("\n")
            if piece.strip():
                pieces.append(piece)
            start, size = j, 0
    tail = "\n".join(lines[start:]).strip("\n")
    if tail.strip():
        pieces.append(tail)
    return pieces or [text]


def split_segments(raw: str) -> list[Segment]:
    """Cut the message into prose and fenced-code segments, in order.

    An opened fence with no closing fence yet is a code segment with
    `closed=False`. A partly typed closing fence on the last line (```` `` ````
    inside a block that needs three) is not code and is left out, so the block
    does not flash a stray backtick before it closes.
    """
    lines = (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    roles = _fence_roles(lines)
    segments: list[Segment] = []
    prose: list[str] = []
    code: list[str] = []
    lang = ""
    indent = 0
    fence: tuple[str, int] = ("`", 3)
    pending_dedent = 0

    def flush_prose() -> None:
        nonlocal pending_dedent
        if prose:
            text = "\n".join(_dedent(l, pending_dedent) for l in prose) if pending_dedent \
                else "\n".join(prose)
            text = text.strip("\n").rstrip()
            if text.strip():
                for k, piece in enumerate(_chunk_prose(text)):
                    segments.append(Segment("prose", piece, joined=k > 0))
            prose.clear()
        pending_dedent = 0

    last = len(lines) - 1
    for index, (line, role) in enumerate(zip(lines, roles)):
        if role == "prose":
            # A rule needs a blank (or the start) above it; under a line of text
            # `---` makes that line a heading instead.
            if _RULE.match(line) and (index == 0 or not lines[index - 1].strip()):
                flush_prose()
                segments.append(Segment("rule", ""))
            else:
                prose.append(line)
        elif role == "open":
            flush_prose()
            match = _FENCE_OPEN.match(line)
            assert match is not None
            indent = len(match.group(1))
            fence = (match.group(2)[0], len(match.group(2)))
            lang = match.group(3)[:24]
            code = []
        elif role == "code":
            partial = (index == last and re.fullmatch(
                r"[ \t]*" + re.escape(fence[0]) + r"{1,%d}" % max(1, fence[1] - 1), line))
            if not partial:
                code.append(_dedent(line, indent))
        else:                                             # close
            segments.append(Segment("code", "\n".join(code), lang, True))
            code = []
            pending_dedent = indent
    if roles and roles[-1] in ("open", "code"):
        # Still inside a fence. A trailing blank means "a new line is starting";
        # do not draw it until something is on it.
        if code and code[-1] == "":
            code.pop()
        segments.append(Segment("code", "\n".join(code), lang, False))
    else:
        flush_prose()
    return segments


def strip_markers(raw: str) -> str:
    """The markdown without its source markers - what a message-level Copy
    should put on the clipboard. Code (fenced or inline) is left exactly as it
    is. The blank before a marker goes with it, so "in the lease [1]." reads
    "in the lease." rather than "in the lease .".
    """
    lines = (raw or "").replace("\r\n", "\n").split("\n")
    roles = _fence_roles(lines)

    def clean(chunk: str) -> str:
        return re.sub(r"(?<=\S)[ \t]*" + _MARKER.pattern, "", chunk)

    out: list[str] = []
    for line, role in zip(lines, roles):
        if role != "prose":
            out.append(line)
            continue
        parts: list[str] = []
        pos = 0
        for match in _CODE_SPAN.finditer(line):
            parts.append(clean(line[pos:match.start()]))
            parts.append(match.group(0))
            pos = match.end()
        parts.append(clean(line[pos:]))
        out.append("".join(parts))
    return "\n".join(out)


def _autoclose(text: str) -> str:
    """Make the unfinished end of the LAST paragraph safe to draw.

    `**bold` with no closer would show its asterisks until the closer arrives
    and then snap to bold. Closing it for display only means the text is bold
    from its first word. An opener with nothing after it yet is dropped
    instead of closed (an empty `****` is worse than nothing); a lone backtick
    at the very end likewise.
    """
    start = text.rfind("\n\n") + 2 if "\n\n" in text else 0
    para = text[start:]
    stack: list[str] = []
    drop_from = -1
    i, n = 0, len(para)
    while i < n:
        ch = para[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and para[j] == "`":
                j += 1
            run = para[i:j]
            close = para.find(run, j)
            while close != -1 and close + len(run) < n and para[close + len(run)] == "`":
                close = para.find(run, close + len(run) + 1)
            if close == -1:
                if not para[j:].strip():
                    drop_from = i
                else:
                    stack.append(run)
                break
            i = close + len(run)
            continue
        if ch in "*~":
            j = i
            while j < n and para[j] == ch:
                j += 1
            width = j - i
            tokens = [ch * 2] if width == 2 else [ch] if width == 1 else [ch * 2, ch]
            if ch == "~" and width == 1:
                tokens = []
            can_open = j < n and not para[j].isspace()
            can_close = i > 0 and not para[i - 1].isspace()
            bullet = ch == "*" and width == 1 and para[:i].strip() == "" and j < n \
                and para[j] == " "
            if not bullet:
                for token in tokens:
                    if token in stack and can_close:
                        del stack[len(stack) - 1 - stack[::-1].index(token)]
                    elif can_open:
                        stack.append(token)
                    elif j >= n and can_close is False:
                        drop_from = i
            i = j
            continue
        i += 1
    if drop_from >= 0:
        para = para[:drop_from].rstrip() if not stack else para[:drop_from]
    closers = "".join(reversed(stack))
    return text[:start] + para + closers


def _swap_markers(chunk: str, show_number: Callable[[int], int],
                  linkable: frozenset) -> str:
    """Escape `<` (an answer quoting `<b>` must not become markup) and turn
    each source marker into a raised number, a link when it has a source."""
    chunk = chunk.replace("<", "&lt;")
    chunk = _UNDER_EM.sub(r"*\1*", _UNDER_STRONG.sub(r"**\1**", chunk))
    matches = list(_MARKER.finditer(chunk))
    if not matches:
        return chunk
    out: list[str] = []
    last = 0
    for k, match in enumerate(matches):
        out.append(chunk[last:match.start()])
        numbers = [int(x) for x in re.split(r"\s*,\s*", match.group(1).strip())]
        pieces = []
        for n in numbers:
            shown = show_number(n)
            pieces.append(f'<sup><a href="{_RECEIPT}{shown}">{shown}</a></sup>'
                          if n in linkable else f"<sup>{shown}</sup>")
        out.append("<sup>,</sup>".join(pieces))
        if k + 1 < len(matches) and matches[k + 1].start() == match.end():
            out.append("<sup>,</sup>")
        last = match.end()
    out.append(chunk[last:])
    return "".join(out)


def prepare_prose(text: str, *, tail: bool = False, final: bool = False,
                  show_number: Callable[[int], int] = lambda n: n,
                  linkable: Iterable[int] = ()) -> str:
    """One prose segment, ready for `setMarkdown`.

    `tail` marks the segment still being written: a marker arriving (`[1`) is
    held back, a lone backtick line (the start of a fence) is held back, and
    an unfinished `**`, `*`, `~~` or backtick is closed for display. `final`
    switches all of that off - the text is what it is.
    """
    can_link = frozenset(linkable)
    if tail and not final:
        text = _OPEN_MARKER_TAIL.sub("", text)
        lines = text.split("\n")
        if lines and re.fullmatch(r"[ \t]*(`{1,2}|~{1,2})", lines[-1]):
            lines.pop()
            text = "\n".join(lines)
        text = re.sub(r"\[([^\[\]\n]*)\]\([^)\s]*$", r"\1", text)      # half a link
        text = text.rstrip()
        text = _autoclose(text)
    parts: list[str] = []
    pos = 0
    for match in _CODE_SPAN.finditer(text):
        parts.append(_swap_markers(text[pos:match.start()], show_number, can_link))
        parts.append(match.group(0))
        pos = match.end()
    parts.append(_swap_markers(text[pos:], show_number, can_link))
    return "".join(parts)


def render_markdown_html(raw: str,
                         show_number: Callable[[int], int] = lambda n: n,
                         linkable: Iterable[int] = ()) -> str:
    """The whole message as HTML, the way the widget would draw it (a pure
    function of the text - for tests and for anything that wants the markup).
    Code blocks come out as `<pre>`."""
    import html as _html

    segments = split_segments(raw)
    out: list[str] = []
    for index, seg in enumerate(segments):
        if seg.kind == "code":
            out.append(f'<pre class="code" data-lang="{_html.escape(seg.lang)}">'
                       f"{_html.escape(seg.text)}</pre>")
            continue
        doc = QTextDocument()
        doc.setMarkdown(prepare_prose(
            seg.text, tail=index == len(segments) - 1, show_number=show_number,
            linkable=linkable))
        html = doc.toHtml()
        start = html.find("<body")
        start = html.find(">", start) + 1 if start >= 0 else 0
        end = html.rfind("</body>")
        out.append(html[start:end if end >= 0 else len(html)].strip())
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Fonts and colours
# ---------------------------------------------------------------------------

_MONO_CHOICES = ("Cascadia Mono", "Cascadia Code", "Consolas", "Menlo",
                 "DejaVu Sans Mono", "Courier New")


def _mono_family() -> str:
    try:
        have = set(QFontDatabase.families())
    except Exception:                                       # noqa: BLE001 - no fonts yet
        have = set()
    for name in _MONO_CHOICES:
        if name in have:
            return name
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _pt(font: QFont) -> float:
    size = font.pointSizeF()
    return size if size > 0 else 9.0


def _colours() -> dict[str, str]:
    return theme_colours()


# ---------------------------------------------------------------------------
# The prose widget
# ---------------------------------------------------------------------------

def _tables(frame: QTextFrame) -> Iterable[QTextTable]:
    for child in frame.childFrames():
        if isinstance(child, QTextTable):
            yield child
        yield from _tables(child)


class _Prose(QTextBrowser):
    """Markdown, read-only, no frame, as tall as its text and no taller."""

    anchor_hovered = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("answerProse")
        self.setAccessibleName("Answer text")
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self.document().setIndentWidth(_INDENT_PX)
        self.document().setUndoRedoEnabled(False)
        self.viewport().setAutoFillBackground(False)
        self.highlighted.connect(self._highlighted)
        self._key: object = None
        self._height = 0
        self._growing = False
        self.setFixedHeight(0)

    # -- content -------------------------------------------------------------
    def show_markdown(self, markdown: str, colours: dict[str, str], stamp: int, *,
                      growing: bool, joined: bool = False) -> bool:
        """Draw `markdown`; False (and no work) when nothing would change.
        `stamp` changes whenever the theme's colours do."""
        key = (markdown, stamp, joined)
        if key == self._key:
            return False
        if self._key is None or self._key[1] != stamp:
            self._paint_palette(colours)
        self._key = key
        self._growing = growing
        self.setMarkdown(markdown)
        self._style_document(colours, joined)
        self.fit()
        return True

    def settle(self) -> None:
        """No longer the tail: take exactly the height the text needs."""
        if self._growing:
            self._growing = False
            self.fit()

    def _paint_palette(self, c: dict[str, str]) -> None:
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
        pal.setColor(QPalette.ColorRole.Link, QColor(c["accent_text"]))
        pal.setColor(QPalette.ColorRole.LinkVisited, QColor(c["accent_text"]))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(c["accent_soft"]))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(c["text"]))
        pal.setColor(QPalette.ColorRole.Dark, QColor(c["border_strong"]))
        pal.setColor(QPalette.ColorRole.Mid, QColor(c["border_strong"]))
        self.setPalette(pal)
        self.setStyleSheet(
            "QTextBrowser#answerProse { background: transparent; border: none; "
            f"color: {c['text']}; selection-background-color: {c['accent_soft']}; "
            f"selection-color: {c['text']}; }}")

    def _style_document(self, c: dict[str, str], joined: bool = False) -> None:
        """What the markdown importer does not do the way a chat message wants:
        modest headings, tight lists, a comfortable paragraph gap, theme colours
        on links and code, and framed tables."""
        doc = self.document()
        self.ensurePolished()                     # the sheet's font size, not the default
        base = _pt(self.font())
        mono = _mono_family()
        tables = list(_tables(doc.rootFrame()))
        spans = [(t.firstPosition(), t.lastPosition()) for t in tables]
        text_dim = QBrush(QColor(c["text_dim"]))
        accent = QBrush(QColor(c["accent_text"]))
        code_bg = QBrush(QColor(c["surface_alt"]))
        edits: list[tuple[int, int, QTextCharFormat]] = []
        block_edits: list[tuple[int, QTextBlockFormat]] = []

        first_pos = doc.begin().position()
        last_pos = doc.lastBlock().position()
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            level = bf.headingLevel()
            in_list = block.textList() is not None
            quote = bf.hasProperty(QTextFormat.Property.BlockQuoteLevel)
            in_table = any(a <= block.position() <= b for a, b in spans)
            new = QTextBlockFormat(bf)
            if in_table:
                new.setTopMargin(0.0)
                new.setBottomMargin(0.0)
            elif level:
                new.setTopMargin(_PARAGRAPH_GAP + 3)
                new.setBottomMargin(3.0)
            elif in_list:
                new.setTopMargin(_LIST_GAP)
                new.setBottomMargin(_LIST_GAP)
            elif quote:
                new.setTopMargin(_PARAGRAPH_GAP)
                new.setBottomMargin(_PARAGRAPH_GAP)
                new.setLeftMargin(12.0)
                new.setRightMargin(0.0)
            elif bf.topMargin() > 0 or bf.bottomMargin() > 0:
                new.setTopMargin(_PARAGRAPH_GAP)
                new.setBottomMargin(_PARAGRAPH_GAP)
            if quote:
                new.setBackground(code_bg)
            if block.position() == first_pos and not in_table:
                # The bubble owns the outer space. A chunk that continues the
                # prose above keeps a little air over a heading: the layout's own
                # spacing supplies the paragraph gap.
                new.setTopMargin(4.0 if joined and level else 0.0)
            if block.position() == last_pos and not in_table:
                new.setBottomMargin(0.0)
            block_edits.append((block.position(), new))

            for fr in block.textFormats():
                fmt = fr.format
                delta = QTextCharFormat()
                touched = False
                if level:
                    scale = _HEADING_SCALE.get(level, 1.0)
                    delta.setFontPointSize(base * scale)
                    delta.setProperty(QTextFormat.Property.FontSizeAdjustment, 0)
                    delta.setFontWeight(QFont.Weight.Bold)
                    touched = True
                if fmt.isAnchor():
                    href = fmt.anchorHref()
                    delta.setForeground(accent)
                    delta.setFontUnderline(not href.startswith(_RECEIPT))
                    touched = True
                elif fmt.fontFixedPitch():
                    delta.setFontFamilies([mono])
                    delta.setFontPointSize(base * _CODE_SCALE)
                    delta.setBackground(code_bg)
                    delta.setForeground(QBrush(QColor(c["text"])))
                    touched = True
                elif quote:
                    delta.setForeground(text_dim)
                    touched = True
                if touched and fr.length > 0:
                    edits.append((block.position() + fr.start, fr.length, delta))
            block = block.next()

        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for pos, length, delta in edits:
            cursor.setPosition(pos)
            cursor.setPosition(pos + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(delta)
        for pos, fmt in block_edits:
            cursor.setPosition(pos)
            cursor.setBlockFormat(fmt)
        cursor.endEditBlock()
        for table in tables:
            fmt = table.format()
            fmt.setBorder(1.0)
            fmt.setBorderBrush(QBrush(QColor(c["border_strong"])))
            fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
            fmt.setCellPadding(5.0)
            fmt.setCellSpacing(0.0)
            fmt.setTopMargin(4.0)
            fmt.setBottomMargin(_PARAGRAPH_GAP - 2)
            table.setFormat(fmt)
            for col in range(table.columns()):
                cell = table.cellAt(0, col)
                head = cell.format()
                head.setBackground(QBrush(QColor(c["surface_alt"])))
                cell.setFormat(head)

    # -- height --------------------------------------------------------------
    def fit(self) -> None:
        width = self.width()          # no frame, no scroll bars: the viewport's width
        if width <= 0:
            return
        doc = self.document()
        doc.setTextWidth(width)
        wanted = int(math.ceil(doc.size().height()))
        if self._growing and wanted < self._height:
            wanted = self._height
        if wanted != self._height:
            self._height = wanted
            self.setFixedHeight(wanted)
            self.updateGeometry()

    def resizeEvent(self, event) -> None:                   # noqa: N802 - Qt name
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            # A new width re-wraps everything; an exact height is right again.
            self._growing = False
        self.fit()

    def sizeHint(self) -> QSize:                            # noqa: N802
        return QSize(240, max(self._height, 1))

    def minimumSizeHint(self) -> QSize:                     # noqa: N802
        return QSize(40, max(self._height, 1))

    # -- interaction ---------------------------------------------------------
    def wheelEvent(self, event: QWheelEvent) -> None:       # noqa: N802
        event.ignore()                                      # the conversation scrolls

    def _highlighted(self, url: QUrl) -> None:
        self.anchor_hovered.emit(url.toString() if url.isValid() else "")

    def leaveEvent(self, event: QEvent) -> None:            # noqa: N802
        self.anchor_hovered.emit("")
        super().leaveEvent(event)


# ---------------------------------------------------------------------------
# The code block widget
# ---------------------------------------------------------------------------

class _CodeText(QPlainTextEdit):
    """The code itself: monospace, no wrap, scrolls sideways only if it has to."""

    def wheelEvent(self, event: QWheelEvent) -> None:       # noqa: N802
        if event.angleDelta().x() or (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            super().wheelEvent(event)
        else:
            event.ignore()


class _CodeBlock(QFrame):
    """A fenced block: language label and Copy on top, the code below."""

    copied = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("answerCode")
        self.setAccessibleName("Code block")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._code = ""
        self._shown = ""
        self._lang = None
        self._key: object = None

        self.language = QLabel("code")
        self.language.setObjectName("answerCodeLang")
        self.copy_button = QToolButton()
        self.copy_button.setObjectName("answerCodeCopy")
        self.copy_button.setText(COPY_TEXT)
        self.copy_button.setAccessibleName("Copy code")
        self.copy_button.setToolTip("Copy this code")
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.copy_button.clicked.connect(self.copy)
        self.view = _CodeText()
        self.view.setObjectName("answerCodeText")
        self.view.setAccessibleName("Code")
        self.view.setReadOnly(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setUndoRedoEnabled(False)
        self.view.document().setDocumentMargin(2)
        self.view.setTabStopDistance(self.view.fontMetrics().horizontalAdvance(" ") * 4)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        head = QHBoxLayout()
        head.setContentsMargins(10, 4, 4, 0)
        head.addWidget(self.language)
        head.addStretch(1)
        head.addWidget(self.copy_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(1, 1, 1, 1)
        column.setSpacing(0)
        column.addLayout(head)
        body = QHBoxLayout()
        body.setContentsMargins(8, 0, 8, 6)
        body.addWidget(self.view)
        column.addLayout(body)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.setInterval(COPIED_MS)
        self._revert.timeout.connect(self._label_back)
        self._height = 0

    # -- content -------------------------------------------------------------
    def code(self) -> str:
        return self._code

    def show_code(self, text: str, lang: str, colours: dict[str, str],
                  stamp: int) -> bool:
        key = (text, lang, stamp)
        if key == self._key:
            return False
        restyle = self._key is None or self._key[2] != stamp
        self._key = key
        if restyle:
            self._restyle(colours)
        if lang != self._lang:
            self._lang = lang
            self.language.setText(lang or "code")
        if text != self._code:
            old_shown = self._shown
            self._code = text
            self._shown = text.replace("\t", "    ")
            self._set_view_text(old_shown, self._shown)
        self.fit()
        return True

    def _set_view_text(self, old: str, new: str) -> None:
        doc = self.view.document()
        if old and new.startswith(old):
            # Append only, through a cursor of our own, so a selection somebody
            # made in the block (and the widget's own cursor) is left alone.
            delta = new[len(old):]
            if delta:
                cursor = QTextCursor(doc)
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(delta)
        else:
            self.view.setPlainText(new)

    def _restyle(self, c: dict[str, str]) -> None:
        small = font_sizes()["small"]
        body_pt = float(font_sizes()["body"][:-2])
        self.setStyleSheet(
            f"QFrame#answerCode {{ background: {c['surface_alt']}; "
            f"border: 1px solid {c['border']}; border-radius: {RADIUS['radius_control']}; }}"
            f"QLabel#answerCodeLang {{ background: transparent; border: none; "
            f"color: {c['text_faint']}; font-size: {small}; }}"
            f"QToolButton#answerCodeCopy {{ background: transparent; "
            f"border: 1px solid transparent; border-radius: 6px; padding: 1px 8px; "
            f"color: {c['text_dim']}; font-size: {small}; }}"
            f"QToolButton#answerCodeCopy:hover {{ background: {c['surface_hover']}; "
            f"color: {c['text']}; border-color: {c['border']}; }}"
            f"QToolButton#answerCodeCopy:focus {{ border-color: {c['focus_ring']}; }}"
            f"QPlainTextEdit#answerCodeText {{ background: transparent; border: none; "
            f"color: {c['text']}; selection-background-color: {c['accent_soft']}; "
            f"selection-color: {c['text']}; font-family: \"{_mono_family()}\"; "
            f"font-size: {round(body_pt * _CODE_SCALE, 1)}pt; }}")
        self.view.ensurePolished()
        self.view.setTabStopDistance(self.view.fontMetrics().horizontalAdvance(" ") * 4)

    # -- height --------------------------------------------------------------
    def fit(self) -> None:
        view = self.view
        lines = self._shown.count("\n") + 1
        fm = view.fontMetrics()
        height = fm.lineSpacing() * lines + int(view.document().documentMargin() * 2) + 2
        widest = max((fm.horizontalAdvance(line) for line in self._shown.split("\n")),
                     default=0) + int(view.document().documentMargin() * 2) + 4
        if widest > view.viewport().width() > 0:
            height += view.horizontalScrollBar().sizeHint().height()
        if height != self._height:
            self._height = height
            view.setFixedHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event) -> None:                   # noqa: N802
        super().resizeEvent(event)
        self.fit()

    # -- copy ----------------------------------------------------------------
    def _label_back(self) -> None:
        self.copy_button.setText(COPY_TEXT)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self._code)
        self.copy_button.setText(COPIED_TEXT)
        self._revert.start()
        self.copied.emit(self._code)


class _Rule(QFrame):
    """A horizontal rule (`---`), drawn from the theme's border colour."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("answerRule")
        self.setAccessibleName("Divider")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._stamp = -1

    def show_rule(self, colours: dict[str, str], stamp: int) -> None:
        if stamp != self._stamp:
            self._stamp = stamp
            self.setStyleSheet(
                f"QFrame#answerRule {{ background: {colours['border_strong']}; border: none; }}")


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------

class AnswerBody(QWidget):
    """The assistant's message: prose and code blocks, updated in place."""

    receipt_activated = Signal(int)      # the reader-facing number of a clicked marker
    receipt_hovered = Signal(int)        # 0 when the pointer leaves
    link_activated = Signal(str)         # any other http(s) link
    code_copied = Signal(str)            # a code block's text, when Copy is used

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatAnswerBody")
        self.setAccessibleName("Answer")
        self._raw = ""
        self._items: list[QWidget] = []
        self._segments: list[Segment] = []
        self._colours = _colours()
        self._stamp = 0
        self._last_show: Callable[[int], int] = lambda n: n
        self._last_link: frozenset = frozenset()
        self._shown_numbers: set[int] = set()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        # The window sheet paints every QWidget in the window colour; an answer
        # sits on the bubble's surface and must let it show.
        self.setStyleSheet("QWidget#chatAnswerBody { background: transparent; }")

    # -- the one entry point -------------------------------------------------
    def set_text(self, raw: str, *,
                 show_number: Callable[[int], int] = lambda engine_n: engine_n,
                 linkable: Iterable[int] = frozenset(), final: bool = False) -> None:
        """Show `raw`, the markdown so far. Cheap to call again and again.

        `final=True` says nothing more is coming: no tail is held back or
        closed, and every segment settles to its exact height.
        """
        self._raw = raw or ""
        can_link = frozenset(linkable)
        self._last_show, self._last_link = show_number, can_link
        fresh = _colours()
        if fresh != self._colours:
            self._colours = fresh
            self._stamp += 1
        segments = split_segments(self._raw)
        self._segments = segments
        self._shown_numbers = {show_number(n) for n in can_link}
        colours, stamp = self._colours, self._stamp
        last = len(segments) - 1

        for index, seg in enumerate(segments):
            widget = self._items[index] if index < len(self._items) else None
            if widget is not None and self._kind(widget) != seg.kind:
                self._drop_from(index)
                widget = None
            if widget is None:
                widget = self._new(seg.kind)
                # Born at the width it will have, so its first height is right and
                # does not ratchet up from a narrow guess.
                widget.resize(max(self.width(), 1), max(widget.height(), 1))
                self._items.append(widget)
                self._layout.addWidget(widget)
            if isinstance(widget, _CodeBlock):
                widget.show_code(seg.text, seg.lang, colours, stamp)
            elif isinstance(widget, _Rule):
                widget.show_rule(colours, stamp)
            else:
                markdown = prepare_prose(
                    seg.text, tail=index == last, final=final,
                    show_number=show_number, linkable=can_link)
                widget.show_markdown(markdown, colours, stamp,
                                     growing=index == last and not final,
                                     joined=seg.joined)
                if index != last or final:
                    widget.settle()
        if len(self._items) > len(segments):
            self._drop_from(len(segments))

    def settle(self) -> None:
        """Nothing more is coming: make every segment exactly as tall as it is."""
        for widget in self._items:
            if isinstance(widget, _Prose):
                widget.settle()

    def clear(self) -> None:
        self.set_text("")

    # -- what a caller or a test can ask -------------------------------------
    def plain_text(self) -> str:
        parts: list[str] = []
        for widget in self._items:
            if isinstance(widget, _CodeBlock):
                parts.append(widget.code())
            elif isinstance(widget, _Prose):
                parts.append(widget.toPlainText())
        return "\n\n".join(parts)

    def markdown(self) -> str:
        return self._raw

    def code_blocks(self) -> list[str]:
        return [w.code() for w in self._items if isinstance(w, _CodeBlock)]

    def copy_all_text(self) -> str:
        return strip_markers(self._raw)

    def segment_widgets(self) -> list[QWidget]:
        """The live segment widgets in order (identity is stable while streaming)."""
        return list(self._items)

    def prose_views(self) -> list[QTextBrowser]:
        return [w for w in self._items if isinstance(w, _Prose)]

    def code_views(self) -> list[QFrame]:
        return [w for w in self._items if isinstance(w, _CodeBlock)]

    # -- building and removing -----------------------------------------------
    @staticmethod
    def _kind(widget: QWidget) -> str:
        return ("code" if isinstance(widget, _CodeBlock)
                else "rule" if isinstance(widget, _Rule) else "prose")

    def _new(self, kind: str) -> QWidget:
        if kind == "code":
            return self._new_code()
        if kind == "rule":
            return _Rule(self)
        return self._new_prose()

    def _new_prose(self) -> _Prose:
        view = _Prose(self)
        view.anchorClicked.connect(self._anchor_clicked)
        view.anchor_hovered.connect(self._anchor_hovered)
        return view

    def _new_code(self) -> _CodeBlock:
        block = _CodeBlock(self)
        block.copied.connect(self.code_copied.emit)
        return block

    def _drop_from(self, index: int) -> None:
        for widget in self._items[index:]:
            self._layout.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        del self._items[index:]

    # -- links ---------------------------------------------------------------
    def _receipt_number(self, href: str) -> Optional[int]:
        if not href.startswith(_RECEIPT):
            return None
        try:
            number = int(href[len(_RECEIPT):])
        except ValueError:
            return None
        return number if number in self._shown_numbers else None

    def _anchor_clicked(self, url: QUrl) -> None:
        href = url.toString()
        number = self._receipt_number(href)
        if number is not None:
            self.receipt_activated.emit(number)
        elif url.scheme().lower() in ("http", "https"):
            self.link_activated.emit(href)

    def _anchor_hovered(self, href: str) -> None:
        number = self._receipt_number(href)
        self.receipt_hovered.emit(number if number is not None else 0)

    # -- theme ---------------------------------------------------------------
    def refresh_theme(self) -> None:
        """Redraw with the theme's current tokens (a theme switch, or a call
        after the stylesheet was replaced)."""
        if _colours() == self._colours:
            return
        self.set_text(self._raw, show_number=self._last_show, linkable=self._last_link)

    def changeEvent(self, event: QEvent) -> None:           # noqa: N802
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            if getattr(self, "_items", None) and _colours() != self._colours:
                self.refresh_theme()
        super().changeEvent(event)
