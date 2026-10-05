r"""The answer body: markdown that is still being written (pytest-qt, offscreen).

Layer: L5 (a widget on its own, no window, no engine)

`AnswerBody` is what the Chat tab's assistant bubble hands the model's text to
every ~40 ms. These tests check what a reader would see - by the document's own
formats and blocks, not by pixels - and, above all, that **streaming is calm**:
widgets are only ever added at the end, finished segments are the same objects
at the end as they were in the middle, an open code fence never flickers into
prose, and a bubble never gets shorter while text is only being appended.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QUrl                # noqa: E402
from PySide6.QtGui import (                                                # noqa: E402
    QGuiApplication, QTextCharFormat, QTextListFormat, QTextTable, QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QToolButton                    # noqa: E402

from app.ui import theme                                                 # noqa: E402
from app.ui.widgets import chat_markdown as cm                           # noqa: E402
from app.ui.widgets.chat_markdown import (                               # noqa: E402
    AnswerBody, render_markdown_html, split_segments, strip_markers,
)

pytestmark = pytest.mark.gui

SUPER = QTextCharFormat.VerticalAlignment.AlignSuperScript
RECEIPT = "leasha-receipt:"

SAMPLE = """# The deposit terms

Here is **what I found** about the deposit, and _why it matters_[1]. It is `£1,200` [1][2].

## What the agreement says

- Protected within 30 days [1, 2]
- Returned within *10 days*
  - less any agreed deductions
- ~~Due on the 5th~~ due on the 1st

1. Book the inspection.
2. Run this:

   ```python
   def f(x):
       return x[1]
   ```

3. Wait for the window to close.

> Ask them to put it in writing.

| Item | Amount |
|------|--------|
| Deposit | £1,200 |
| Fee | £250 |

That is everything.
"""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(params=["light", "dark"])
def palette(request, monkeypatch):
    """Each theme in turn, and the module's current palette put back after."""
    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES[request.param]))
    return request.param


@pytest.fixture()
def light(monkeypatch):
    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES["light"]))


def _pump(qtbot, n: int = 3) -> None:
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture()
def body(qtbot, light):
    widget = AnswerBody()
    qtbot.addWidget(widget)
    widget.resize(520, 200)
    widget.show()
    _pump(qtbot)
    return widget


def _fragments(view):
    """(text, char format) for every formatted run in a prose view's document."""
    out = []
    block = view.document().begin()
    while block.isValid():
        text = block.text()
        for run in block.textFormats():
            # A copy: under PySide6 `run.format` belongs to the list `textFormats()`
            # made, and is freed with it (2026-10-05, the PySide6 trial).
            out.append((text[run.start:run.start + run.length], QTextCharFormat(run.format)))
        block = block.next()
    return out


def _format_of(view, word: str) -> QTextCharFormat:
    for text, fmt in _fragments(view):
        if word in text:
            return fmt
    raise AssertionError(f"{word!r} not found in {[t for t, _ in _fragments(view)]}")


def _height(body) -> int:
    return body.sizeHint().height()


def _prose(body, index: int = 0):
    return body.prose_views()[index]


def _code(body, index: int = 0):
    return body.code_views()[index]


def _anchor_point(view, word: str) -> QPoint:
    """Where to click to hit `word` in the view."""
    doc = view.document()
    found = doc.find(word)
    assert not found.isNull(), word
    cursor = view.cursorRect(found)
    return QPoint(cursor.center().x() - 1, cursor.center().y())


# ---------------------------------------------------------------------------
# What renders
# ---------------------------------------------------------------------------

def test_bold_italic_inline_code_and_strike_render(body):
    body.set_text("A **bold** word, an *italic* one, `code` and ~~gone~~.")
    view = _prose(body)
    assert view.toPlainText() == "A bold word, an italic one, code and gone."
    assert _format_of(view, "bold").fontWeight() >= 600
    assert _format_of(view, "italic").fontItalic()
    assert _format_of(view, "code").fontFixedPitch()
    assert _format_of(view, "gone").fontStrikeOut()


def test_underscore_emphasis_is_italic_not_underline(body):
    body.set_text("Say _this_ and __that__ but leave snake_case_name alone.")
    view = _prose(body)
    assert _format_of(view, "this").fontItalic()
    assert not _format_of(view, "this").fontUnderline()
    assert _format_of(view, "that").fontWeight() >= 600
    assert "snake_case_name" in view.toPlainText()


def test_headings_are_bold_and_only_modestly_larger(body):
    body.set_text("# Big\n\n## Medium\n\n### Small\n\nBody text")
    view = _prose(body)
    base = view.font().pointSizeF()
    sizes = {}
    block = view.document().begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        if level:
            runs = block.textFormats()             # kept: `.format` lives in it
            fmt = QTextCharFormat(runs[0].format)
            assert fmt.fontWeight() >= 600
            sizes[level] = fmt.fontPointSize()
        block = block.next()
    assert set(sizes) == {1, 2, 3}
    assert sizes[1] > sizes[2] > sizes[3] > base
    assert sizes[1] <= base * 1.3                 # not a web page's <h1>


def test_bullets_numbers_and_nesting(body):
    body.set_text("- a\n- b\n  - nested\n\n1. one\n2. two")
    doc = _prose(body).document()
    styles = []
    depth = {}
    block = doc.begin()
    while block.isValid():
        lst = block.textList()
        if lst is not None:
            styles.append(lst.format().style())
            depth[block.text()] = lst.format().indent()
        block = block.next()
    assert QTextListFormat.Style.ListDisc in styles or QTextListFormat.Style.ListCircle in styles
    assert styles.count(QTextListFormat.Style.ListDecimal) == 2
    assert depth["nested"] > depth["a"]


def test_a_github_table_is_a_real_table(body):
    body.set_text("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
    tables = [f for f in _prose(body).document().rootFrame().childFrames()
              if isinstance(f, QTextTable)]
    assert len(tables) == 1
    assert (tables[0].rows(), tables[0].columns()) == (3, 2)
    assert tables[0].format().border() > 0


def test_blockquote_and_rule(body):
    body.set_text("> a quoted line\n\n---\n\nafter")
    kinds = [type(w).__name__ for w in body.segment_widgets()]
    assert kinds == ["_Prose", "_Rule", "_Prose"]
    assert "a quoted line" in _prose(body, 0).toPlainText()
    assert _prose(body, 1).toPlainText() == "after"


def test_a_setext_underline_is_not_a_rule(body):
    body.set_text("Heading\n---\n\ntext")
    assert [type(w).__name__ for w in body.segment_widgets()] == ["_Prose"]


def test_markup_in_an_answer_is_shown_not_obeyed(body):
    body.set_text("Use <b>bold</b> tags and a <script>x</script>.")
    assert "<b>bold</b>" in _prose(body).toPlainText()
    assert "<script>" in _prose(body).toPlainText()


def test_links_are_links_with_a_pointing_hand(body):
    body.set_text("See [the site](https://example.com/a) now.")
    view = _prose(body)
    assert _format_of(view, "the site").anchorHref() == "https://example.com/a"
    assert view.openLinks() is False
    assert view.isReadOnly()
    assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------

def test_a_fenced_block_is_a_framed_widget_with_its_language_and_a_copy_button(body):
    body.set_text("Before\n\n```python\nprint('hi')\n    x = 1\n```\n\nAfter")
    assert [type(w).__name__ for w in body.segment_widgets()] == ["_Prose", "_CodeBlock", "_Prose"]
    block = _code(body)
    assert block.language.text() == "python"
    assert block.copy_button.text() == "Copy"
    assert block.copy_button.accessibleName() == "Copy code"
    assert block.accessibleName() == "Code block"
    assert body.code_blocks() == ["print('hi')\n    x = 1"]
    assert block.view.toPlainText() == "print('hi')\n    x = 1"
    assert block.view.isReadOnly()
    assert "font-family" in block.styleSheet()              # monospace, by the block's own sheet


def test_a_block_without_a_language_says_code(body):
    body.set_text("```\nplain\n```")
    assert _code(body).language.text() == "code"


def test_copy_puts_the_exact_code_on_the_clipboard_and_says_so(body, qtbot, monkeypatch):
    monkeypatch.setattr(cm, "COPIED_MS", 40)
    body.set_text("```js\nlet a = [1];\n\tif (a) {\n}\n```")
    block = _code(body)
    block._revert.setInterval(40)
    QGuiApplication.clipboard().setText("before")
    with qtbot.waitSignal(body.code_copied) as caught:
        qtbot.mouseClick(block.copy_button, Qt.MouseButton.LeftButton)
    assert caught.args == ["let a = [1];\n\tif (a) {\n}"]
    assert QGuiApplication.clipboard().text() == "let a = [1];\n\tif (a) {\n}"
    assert block.copy_button.text() == "Copied"
    qtbot.waitUntil(lambda: block.copy_button.text() == "Copy", timeout=2000)


def test_each_block_copies_its_own_code(body, qtbot):
    body.set_text("```\none\n```\n\ntext\n\n```\ntwo\n```")
    first, second = body.code_views()
    qtbot.mouseClick(second.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "two"
    qtbot.mouseClick(first.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "one"
    assert body.code_blocks() == ["one", "two"]


def test_a_code_block_inside_a_list_item_splits_the_list_and_keeps_counting(body):
    body.set_text("1. First\n2. Run this:\n\n   ```sh\n   ls -l\n   ```\n\n3. Third\n4. Fourth")
    kinds = [type(w).__name__ for w in body.segment_widgets()]
    assert kinds == ["_Prose", "_CodeBlock", "_Prose"]
    assert body.code_blocks() == ["ls -l"]                  # the list indent is gone
    tail = _prose(body, 1).document()
    assert tail.toPlainText().split("\n")[0] == "Third"
    assert tail.begin().textList().format().start() == 3    # "3." not "1."


def test_tildes_and_longer_fences(body):
    body.set_text("~~~\na\n~~~\n\n````\nb\n```\nc\n````")
    assert body.code_blocks() == ["a", "b\n```\nc"]


def test_markers_inside_code_are_code(body):
    body.set_text("Say `a[1]` then:\n\n```\nx[2] = [3]\n```", linkable={1, 2, 3})
    assert "a[1]" in _prose(body).toPlainText()
    assert body.code_blocks() == ["x[2] = [3]"]
    assert not any(f.isAnchor() for _, f in _fragments(_prose(body)))


# ---------------------------------------------------------------------------
# Source markers
# ---------------------------------------------------------------------------

def test_a_linkable_marker_is_a_raised_link_showing_the_reader_number(body):
    body.set_text("The deposit is £1,200 [3].", show_number=lambda n: n + 10, linkable={3})
    view = _prose(body)
    fmt = _format_of(view, "13")
    assert fmt.isAnchor() and fmt.anchorHref() == RECEIPT + "13"
    assert fmt.verticalAlignment() == SUPER
    assert "[3]" not in view.toPlainText()


def test_a_marker_without_a_source_is_raised_but_not_a_link(body):
    body.set_text("It says so [7].", linkable={3})
    view = _prose(body)
    fmt = _format_of(view, "7")
    assert fmt.verticalAlignment() == SUPER
    assert not fmt.isAnchor()


def test_adjacent_and_listed_markers_are_each_drawn(body):
    body.set_text("A [1][2] and B [1, 3].", linkable={1, 2, 3})
    view = _prose(body)
    links = [f.anchorHref() for _, f in _fragments(view) if f.isAnchor()]
    assert links == [RECEIPT + n for n in ("1", "2", "1", "3")]
    assert view.toPlainText().startswith("A 1,2 and B 1,3")


def test_a_marker_still_arriving_is_held_back(body):
    for tail in ("Here it is [", "Here it is [1", "Here it is [1,", "Here it is [1, 2"):
        body.set_text(tail, linkable={1, 2})
        assert _prose(body).toPlainText() == "Here it is", tail
    body.set_text("Here it is [1]", linkable={1})
    assert _prose(body).toPlainText() == "Here it is 1"     # drawn, raised, once it is whole
    body.set_text("see [1", final=True)                  # nothing more is coming
    assert "[1" in _prose(body).toPlainText()


def test_a_markdown_link_that_looks_like_a_marker_stays_a_link(body):
    body.set_text("Open [1](https://example.com) now.", linkable={1})
    assert _format_of(_prose(body), "1").anchorHref() == "https://example.com"


def test_strip_markers_leaves_code_alone():
    raw = "Yes [1][2]. Use `a[1]` and see [3, 4] here.\n\n```\nx[1]\n```\n\n- item [2]"
    assert strip_markers(raw) == "Yes. Use `a[1]` and see here.\n\n```\nx[1]\n```\n\n- item"


def test_copy_all_text_is_the_markdown_without_markers(body):
    raw = "**Bold** claim [1].\n\n```py\nx = [1]\n```"
    body.set_text(raw, linkable={1})
    assert body.markdown() == raw
    assert body.copy_all_text() == "**Bold** claim.\n\n```py\nx = [1]\n```"


# ---------------------------------------------------------------------------
# Clicks and hovers
# ---------------------------------------------------------------------------

def test_clicking_a_marker_emits_the_display_number(body, qtbot):
    body.set_text("A claim here [4] and more text after it.", show_number=lambda n: n * 2,
                  linkable={4})
    view = _prose(body)
    with qtbot.waitSignal(body.receipt_activated, timeout=2000) as caught:
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=_anchor_point(view, "8"))
    assert caught.args == [8]


def test_a_marker_that_is_not_linkable_never_activates(body):
    got = []
    body.receipt_activated.connect(got.append)
    body.set_text("Claim [4].", linkable={4})
    view = _prose(body)
    view.anchorClicked.emit(QUrl(RECEIPT + "9"))          # a link the model made up
    view.anchorClicked.emit(QUrl(RECEIPT + "x"))
    assert got == []
    view.anchorClicked.emit(QUrl(RECEIPT + "4"))
    assert got == [4]


def test_other_links_are_reported_not_opened(body):
    seen = []
    body.link_activated.connect(seen.append)
    body.set_text("[site](https://example.com/x) and [file](file:///c:/secret.txt)")
    view = _prose(body)
    view.anchorClicked.emit(QUrl("https://example.com/x"))
    view.anchorClicked.emit(QUrl("file:///c:/secret.txt"))
    assert seen == ["https://example.com/x"]


def test_hovering_a_marker_and_leaving(body):
    seen = []
    body.receipt_hovered.connect(seen.append)
    body.set_text("Claim [5].", show_number=lambda n: n + 1, linkable={5})
    view = _prose(body)
    view.highlighted.emit(QUrl(RECEIPT + "6"))
    view.highlighted.emit(QUrl())
    view.highlighted.emit(QUrl(RECEIPT + "6"))
    QApplication.sendEvent(view, QEvent(QEvent.Type.Leave))
    assert seen == [6, 0, 6, 0]


def test_the_wheel_is_left_to_the_conversation(body):
    body.set_text("```\nline\n```\n\ntext")
    for widget in (_prose(body).viewport(), _code(body).view.viewport()):
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        event.setAccepted(True)
        QApplication.sendEvent(widget, event)
        assert not event.isAccepted()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _stream(body, text, step=1, **kwargs):
    """Feed `text` in `step`-character increments; yield after each."""
    for end in range(step, len(text) + step, step):
        body.set_text(text[:end], **kwargs)
        yield end


def test_streaming_char_by_char_is_calm(body, qtbot):
    counts = []
    heights = []
    kinds_seen = []
    early = {}
    for end in _stream(body, SAMPLE, linkable={1, 2}):
        counts.append(len(body.segment_widgets()))
        kinds_seen.append([type(w).__name__ for w in body.segment_widgets()])
        if end % 7 == 0:
            _pump(qtbot, 1)
            heights.append(_height(body))
        # (d) a segment stays the same object once it exists
        for index, widget in enumerate(body.segment_widgets()):
            early.setdefault(index, widget)
            assert early[index] is widget, f"segment {index} was rebuilt at char {end}"
    _pump(qtbot)
    # (b) the count only grows while text only grows
    assert all(b >= a for a, b in zip(counts, counts[1:])), counts
    # a segment's kind never changes once it exists (no prose <-> code flicker)
    for before, after in zip(kinds_seen, kinds_seen[1:]):
        assert after[:len(before)] == before
    assert heights == sorted(heights), "the bubble got shorter while text was appended"
    # (e) the same text, one shot, reads the same
    other = AnswerBody()
    qtbot.addWidget(other)
    other.resize(520, 200)
    other.show()
    other.set_text(SAMPLE, linkable={1, 2})
    _pump(qtbot)
    assert body.plain_text() == other.plain_text()
    assert body.code_blocks() == other.code_blocks() == ["def f(x):\n    return x[1]"]


def test_an_open_fence_is_one_code_segment_and_closing_it_makes_no_second(body):
    lead = "Here you go:\n\n"
    fence = "```python\nprint(1)\nprint(2)\n```"
    code_widgets = set()
    for end in range(len(lead) + 3, len(lead + fence) + 1):      # from the opening ``` on
        body.set_text((lead + fence)[:end])
        codes = [w for w in body.segment_widgets() if isinstance(w, cm._CodeBlock)]
        assert len(codes) == 1, (end, (lead + fence)[:end])
        code_widgets.add(id(codes[0]))
        assert [type(w).__name__ for w in body.segment_widgets()][0] == "_Prose"
    assert len(code_widgets) == 1
    assert body.code_blocks() == ["print(1)\nprint(2)"]
    assert body.plain_text().count("print(1)") == 1
    body.set_text(lead + fence + "\n\nDone.")
    assert [type(w).__name__ for w in body.segment_widgets()] == ["_Prose", "_CodeBlock", "_Prose"]


def test_a_half_typed_fence_never_shows_its_backticks_as_prose(body):
    body.set_text("Look:\n\n``")
    assert _prose(body).toPlainText() == "Look:"
    body.set_text("Look:\n\n```")
    assert [type(w).__name__ for w in body.segment_widgets()] == ["_Prose", "_CodeBlock"]
    body.set_text("Look:\n\n```py\nx\n``")            # and half of the closing one
    assert body.code_blocks() == ["x"]
    body.set_text("Look:\n\n```py\nx\n```")
    assert body.code_blocks() == ["x"]


def test_code_grows_in_place_without_rebuilding(body):
    body.set_text("```\nabc")
    block = _code(body)
    body.set_text("```\nabc\ndef")
    body.set_text("```\nabc\ndef\n")
    assert _code(body) is block
    assert body.code_blocks() == ["abc\ndef"]            # a bare newline is not a line yet
    body.set_text("```\nabc\ndef\nghi")
    assert body.code_blocks() == ["abc\ndef\nghi"]


def test_a_table_arriving_row_by_row(body):
    rows = ["| Name | Qty |", "|------|-----|", "| Tea | 2 |", "| Milk | 1 |"]
    seen = []
    for k in range(1, len(rows) + 1):
        body.set_text("Table:\n\n" + "\n".join(rows[:k]))
        tables = [f for f in _prose(body).document().rootFrame().childFrames()
                  if isinstance(f, QTextTable)]
        seen.append(tables[0].rows() if tables else 0)
    assert seen[0] == 0                                   # a header alone is just a line
    assert seen[1:] == [1, 2, 3]
    assert len(body.segment_widgets()) == 1


def test_a_table_cut_mid_row_does_not_break(body):
    body.set_text("| A | B |\n|---|---|\n| 1 |")
    tables = [f for f in _prose(body).document().rootFrame().childFrames()
              if isinstance(f, QTextTable)]
    assert tables and tables[0].rows() == 2


def test_an_unclosed_bold_is_bold_from_its_first_word(body):
    body.set_text("This is **very impor")
    view = _prose(body)
    assert view.toPlainText() == "This is very impor"
    assert _format_of(view, "very impor").fontWeight() >= 600
    body.set_text("This is **very important** and *quite")
    assert view.toPlainText() == "This is very important and quite"
    assert _format_of(view, "quite").fontItalic()
    body.set_text("Ends with **")
    assert body.plain_text() == "Ends with"
    body.set_text("Ends with **done**")
    assert body.plain_text() == "Ends with done"


def test_an_unclosed_backtick_is_code_and_a_lone_one_is_held_back(body):
    body.set_text("Run `git sta")
    assert _format_of(_prose(body), "git sta").fontFixedPitch()
    assert _prose(body).toPlainText() == "Run git sta"
    body.set_text("Run `")
    assert _prose(body).toPlainText() == "Run"


def test_a_bullet_star_is_not_an_unclosed_emphasis(body):
    body.set_text("* one\n* two\n* thr")
    assert _prose(body).toPlainText() == "one\ntwo\nthr"
    body.set_text("2 * 3 = 6 and more")
    assert body.plain_text() == "2 * 3 = 6 and more"


def test_a_half_written_link_shows_its_label(body):
    body.set_text("See [the docs](https://exam")
    assert _prose(body).toPlainText() == "See the docs"
    body.set_text("See [the docs](https://example.com) now")
    assert _format_of(_prose(body), "the docs").anchorHref() == "https://example.com"


def test_an_unchanged_call_does_no_work(body, monkeypatch):
    body.set_text("Some **text**.\n\n```\ncode\n```")
    calls = []
    monkeypatch.setattr(cm._Prose, "setMarkdown", lambda self, md: calls.append(md))
    monkeypatch.setattr(cm._CodeBlock, "_set_view_text", lambda *a: calls.append("code"))
    body.set_text("Some **text**.\n\n```\ncode\n```")
    body.set_text("Some **text**.\n\n```\ncode\n```")
    assert calls == []


def test_only_the_tail_is_touched_while_it_grows(body, monkeypatch):
    body.set_text("First paragraph.\n\n```\nfixed\n```\n\nSecond")
    touched = []
    real = cm._Prose.setMarkdown
    monkeypatch.setattr(cm._Prose, "setMarkdown",
                        lambda self, md: (touched.append(md), real(self, md))[1])
    body.set_text("First paragraph.\n\n```\nfixed\n```\n\nSecond paragraph")
    assert touched == ["Second paragraph"]


def test_a_finished_segment_keeps_its_selection_and_scroll(body):
    body.set_text("Pick some of this text.\n\n```\nx\n```\n\nMore")
    view = _prose(body, 0)
    cursor = view.textCursor()
    cursor.setPosition(5)
    cursor.setPosition(9, cursor.MoveMode.KeepAnchor)
    view.setTextCursor(cursor)
    selected = view.textCursor().selectedText()
    body.set_text("Pick some of this text.\n\n```\nx\n```\n\nMore and more and more")
    assert _prose(body, 0) is view
    assert view.textCursor().selectedText() == selected == "some"


def test_a_long_run_is_chunked_and_streams_as_appends(body, qtbot):
    para = "Sentence with **bold** and a marker [1] that runs on and on. " * 6
    text = "\n\n".join(f"## Part {i}\n\n{para}\n\n- one {i}\n- two {i}" for i in range(8))
    assert len(split_segments(text)) > 3
    first = {}
    counts = []
    for end in _stream(body, text, step=13, linkable={1}):
        counts.append(len(body.segment_widgets()))
        for index, widget in enumerate(body.segment_widgets()):
            assert first.setdefault(index, widget) is widget
    assert all(b >= a for a, b in zip(counts, counts[1:]))
    other = AnswerBody()
    qtbot.addWidget(other)
    other.resize(520, 200)
    other.show()
    other.set_text(text, linkable={1})
    assert body.plain_text() == other.plain_text()


def test_chunk_boundaries_do_not_split_a_list(body):
    item = "word " * 80
    text = f"- {item}\n\n- {item}\n\n- {item}\n\n- {item}\n\n- {item}"
    assert [s.kind for s in split_segments(text)] == ["prose"]
    numbered = f"1. {item}\n\n2. {item}\n\n3. {item}\n\n4. {item}\n\n5. {item}"
    assert len(split_segments(numbered)) == 1
    # ... and a boundary that is chosen is never taken back as the text grows
    text = ("word " * 300) + "\n\n1"
    assert len(split_segments(text)) == 1                 # "1" may become "1. item"
    assert len(split_segments(text + ". item")) == 1
    assert len(split_segments(text + "0 people came")) == 2


def test_final_lets_the_bubble_settle_to_its_exact_height(body, qtbot):
    text = "Line one\n\n- a\n- b\n- **c"
    for _ in _stream(body, text):
        pass
    _pump(qtbot)
    body.set_text(text, final=True)
    _pump(qtbot)
    height = _height(body)
    other = AnswerBody()
    qtbot.addWidget(other)
    other.resize(520, 200)
    other.show()
    other.set_text(text, final=True)
    _pump(qtbot)
    assert abs(height - _height(other)) <= 2


# ---------------------------------------------------------------------------
# Size and edge cases
# ---------------------------------------------------------------------------

def test_empty_and_reset(body):
    body.set_text("")
    assert body.segment_widgets() == [] and body.plain_text() == "" and body.markdown() == ""
    assert body.code_blocks() == [] and body.copy_all_text() == ""
    body.set_text("text\n\n```\nx\n```")
    assert len(body.segment_widgets()) == 2
    body.set_text("")
    assert body.segment_widgets() == []
    body.set_text("   \n\n  ")
    assert body.segment_widgets() == []
    body.set_text("again")
    assert body.plain_text() == "again"
    body.clear()
    assert body.segment_widgets() == []


def test_a_very_long_word_wraps_and_makes_no_horizontal_scroll(body, qtbot):
    word = "x" * 1500
    body.resize(300, 100)
    _pump(qtbot)
    body.set_text(f"Look {word} and https://example.com/{'a/' * 300} end")
    _pump(qtbot)
    view = _prose(body)
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert view.document().size().width() <= view.width() + 1
    assert view.horizontalScrollBar().maximum() == 0
    assert body.sizeHint().width() <= 320                 # it does not push the bubble wider


def test_a_long_code_line_scrolls_inside_the_block_only(body, qtbot):
    body.set_text("```\n" + "y" * 400 + "\n```")
    _pump(qtbot)
    block = _code(body)
    assert block.view.horizontalScrollBar().maximum() > 0
    assert block.view.height() > block.view.fontMetrics().lineSpacing()   # room for the bar
    assert body.sizeHint().width() <= 540


def test_height_follows_content_and_width(body, qtbot):
    body.set_text("Short.")
    _pump(qtbot)
    short = _height(body)
    body.set_text("Short.\n\n" + "A much longer paragraph of words. " * 30)
    _pump(qtbot)
    tall = _height(body)
    assert tall > short * 3
    body.resize(260, 200)
    _pump(qtbot, 5)
    narrow = _height(body)
    assert narrow > tall                                  # re-wrapped at the new width
    body.resize(520, 200)
    _pump(qtbot, 5)
    assert abs(_height(body) - tall) <= 2


def test_tabs_are_shown_but_copied_as_typed(body):
    body.set_text("```\n\tindented\n```")
    assert body.code_blocks() == ["\tindented"]
    assert "\t" not in _code(body).view.toPlainText()


def test_pure_html_helper_and_segments():
    html = render_markdown_html("**b** [1]\n\n```py\nx<1>\n```", linkable={1})
    assert "font-weight:700" in html and 'href="leasha-receipt:1"' in html
    assert "<pre" in html and "x&lt;1&gt;" in html
    assert [s.kind for s in split_segments("a\n\n```\nb\n```\n\n---\n\nc")] == \
        ["prose", "code", "rule", "prose"]
    assert split_segments("```\nb")[0].closed is False
    assert split_segments("a\r\n\r\n```\r\nb\r\n```")[1].text == "b"


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

def test_both_themes_build_and_take_their_own_colours(qtbot, palette):
    body = AnswerBody()
    qtbot.addWidget(body)
    body.resize(520, 200)
    body.show()
    body.set_text(SAMPLE, linkable={1, 2})
    _pump(qtbot)
    colours = theme.PALETTES[palette]
    view = _prose(body)
    assert view.palette().color(view.palette().ColorRole.Link).name() == colours["accent_text"]
    assert view.palette().color(view.palette().ColorRole.Text).name() == colours["text"]
    block = _code(body)
    assert colours["surface_alt"] in block.styleSheet()
    assert colours["border"] in block.styleSheet()
    code_fmt = _format_of(view, "£1,200")
    assert code_fmt.background().color().name() == colours["surface_alt"]
    link = next(f for _, f in _fragments(view) if f.isAnchor())
    assert link.foreground().color().name() == colours["accent_text"]
    table = next(f for f in _prose(body, 1).document().rootFrame().childFrames()
                 if isinstance(f, QTextTable))
    assert table.format().borderBrush().color().name() == colours["border_strong"]


def test_a_theme_switch_repaints_an_answer_already_on_screen(qtbot, monkeypatch):
    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES["dark"]))
    body = AnswerBody()
    qtbot.addWidget(body)
    body.resize(520, 200)
    body.show()
    body.set_text("A [1] and `code`\n\n```\nx\n```", linkable={1})
    block = _code(body)
    assert theme.PALETTES["dark"]["surface_alt"] in block.styleSheet()
    dark_link = next(f for _, f in _fragments(_prose(body)) if f.isAnchor())
    assert dark_link.foreground().color().name() == theme.PALETTES["dark"]["accent_text"]

    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES["light"]))
    body.refresh_theme()
    assert _code(body) is block                           # same widgets, new colours
    assert theme.PALETTES["light"]["surface_alt"] in block.styleSheet()
    link = next(f for _, f in _fragments(_prose(body)) if f.isAnchor())
    assert link.foreground().color().name() == theme.PALETTES["light"]["accent_text"]
    assert body.code_blocks() == ["x"]


def test_the_window_stylesheet_changing_reaches_the_body(qtbot, monkeypatch):
    """The real path: `MainWindow._apply_theme` sets a new sheet on an ancestor."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES["dark"]))
    holder = QWidget()
    qtbot.addWidget(holder)
    column = QVBoxLayout(holder)
    body = AnswerBody()
    column.addWidget(body)
    holder.resize(500, 200)
    holder.show()
    body.set_text("```\nx\n```")
    assert theme.PALETTES["dark"]["surface_alt"] in _code(body).styleSheet()
    holder.setStyleSheet(theme.stylesheet("light"))       # sets `_current` to light, too
    _pump(qtbot)
    assert theme.PALETTES["light"]["surface_alt"] in _code(body).styleSheet()
    monkeypatch.setattr(theme, "_current", dict(theme.PALETTES["dark"]))


def test_accessible_names(body):
    body.set_text("Text\n\n```\nx\n```")
    assert body.accessibleName() == "Answer"
    assert _prose(body).accessibleName()
    assert _code(body).accessibleName() == "Code block"
    assert isinstance(_code(body).copy_button, QToolButton)
    assert _code(body).copy_button.accessibleName() == "Copy code"
