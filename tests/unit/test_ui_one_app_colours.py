"""One app, one set of colours: every state colour comes from the theme. 2026-10-12.

Layer: L5

**What was wrong.** A dozen places in `app/ui` painted a state - Ready, Limited,
Cannot read, a dialog's problem line, a diff's added and removed lines - with a hex
literal picked for one theme and never measured in the other. Measured against the
app's own surfaces (dark card `#1f2023`, light card `#ffffff`):

* `#c62828` (every dialog's problem line, "Cannot read") was 2.9:1 on the dark card;
* `#2e7d32` ("Ready") 3.2:1 and `#757575` ("Off") 3.5:1 on the dark card;
* `#e65100` ("Limited", the size-cap warning) 4.3:1 on dark and 3.8:1 on light;
* the commit diff's `#2da44e` and `#e5534b` 3.2:1 and 3.7:1 on white.

Every one under the 4.5:1 that small text needs, in at least one theme, while the
theme already had a measured token for the same meaning. The cure is the theme's
tokens; the guard below is what keeps a new literal from arriving next week.

The luminance arithmetic is written out here rather than imported, so this file
measures the palette with its own ruler (WCAG 2.x relative luminance).
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from app.ui import theme
from app.ui.theme import PALETTES, Theme

ROOT = Path(__file__).resolve().parents[2]


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(hex_colour[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _over(colour: str, alpha: float, ground: str) -> str:
    """`colour` at `alpha` composited onto `ground`, as the screen shows it."""
    mixed = (round(int(colour[i:i + 2], 16) * alpha + int(ground[i:i + 2], 16) * (1 - alpha))
             for i in (1, 3, 5))
    return "#" + "".join(f"{v:02x}" for v in mixed)


@pytest.fixture()
def restore_theme():
    """Tests here switch the current palette; put back what was there."""
    saved = theme.theme_colours()
    yield
    theme._current = saved


# --- the palette ---------------------------------------------------------------------


def test_the_contrast_ruler_agrees_with_the_published_figures():
    """A ruler that is wrong passes everything - pin it to two known values."""
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#767676", "#ffffff"), 2) == 4.54


#: The tokens that carry a state in words: their text has to be read, not just seen.
STATUS_TOKENS = ("success", "warning", "danger", "text_faint")


@pytest.mark.parametrize("theme_name", [Theme.LIGHT, Theme.DARK])
@pytest.mark.parametrize("token", STATUS_TOKENS)
def test_every_status_colour_reads_on_the_card_and_the_window(theme_name, token):
    palette = PALETTES[theme_name]
    for ground in ("surface", "window"):
        ratio = contrast(palette[token], palette[ground])
        assert ratio >= 4.5, (
            f"{theme_name}: {token} {palette[token]} on {ground} {palette[ground]} "
            f"is {ratio:.2f}:1, under the 4.5:1 small text needs")


def test_there_is_a_success_colour_in_both_themes():
    """`success` is new: "Ready" had no token, so it had a literal."""
    assert PALETTES[Theme.LIGHT]["success"] == "#2a7a3b"
    assert PALETTES[Theme.DARK]["success"] == "#5fae73"


def test_the_photo_backdrop_is_the_same_dark_in_both_themes():
    """A photo is judged against black, whatever the window's theme is."""
    for key in ("backdrop", "backdrop_text"):
        assert PALETTES[Theme.LIGHT][key] == PALETTES[Theme.DARK][key], key
    palette = PALETTES[Theme.DARK]
    assert contrast(palette["backdrop_text"], palette["backdrop"]) >= 4.5


def test_the_month_pill_reads_even_over_a_white_photo():
    """The pill is the backdrop at alpha 150 over whatever thumbnail is under it;
    the worst case is a white one."""
    from app.ui.widgets.photo_browser import MONTH_PILL_ALPHA

    palette = PALETTES[Theme.DARK]
    ground = _over(palette["backdrop"], MONTH_PILL_ALPHA / 255, "#ffffff")
    assert contrast(palette["backdrop_text"], ground) >= 4.5


# --- each place that had a literal ------------------------------------------------------


class _Status:
    def __init__(self, state: str, fix: str = "", detail: str = "") -> None:
        self.state, self.fix, self.detail = state, fix, detail


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize("theme_name", [Theme.LIGHT, Theme.DARK])
def test_the_file_types_status_is_the_themes_colour(qapp, tmp_path, restore_theme, theme_name):
    from PySide6.QtGui import QColor

    from app.ui.widgets.file_types import FileTypesEditor

    class Settings:
        data_path = tmp_path

    editor = FileTypesEditor(Settings())
    theme.stylesheet(theme_name)
    palette = PALETTES[theme_name]
    for state, token in (("ready", "success"), ("degraded", "warning"),
                         ("blocked", "danger"), ("off", "text_faint")):
        item = editor._status_item(_Status(state), {})
        assert item.foreground().color() == QColor(palette[token]), state


def test_the_file_types_status_follows_a_theme_switch(qapp, tmp_path, restore_theme):
    """The table is filled once; a theme switch restyles it without a refill."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QTableWidgetItem

    from app.ui.widgets.file_types import FileTypesEditor

    class Settings:
        data_path = tmp_path

    theme.stylesheet(Theme.DARK)
    editor = FileTypesEditor(Settings())
    editor.table.setRowCount(max(1, editor.table.rowCount()))
    editor.table.setItem(0, 3, editor._status_item(_Status("blocked"), {}))
    assert editor.table.item(0, 3).foreground().color() == QColor(PALETTES[Theme.DARK]["danger"])

    theme.stylesheet(Theme.LIGHT)
    QApplication.sendEvent(editor, QEvent(QEvent.Type.StyleChange))
    assert editor.table.item(0, 3).foreground().color() == QColor(PALETTES[Theme.LIGHT]["danger"])
    assert isinstance(editor.table.item(0, 3), QTableWidgetItem)


def test_the_size_cap_warning_is_the_themes_warning(qapp, restore_theme):
    from app.ui.widgets.file_types import EditFileTypeDialog

    theme.stylesheet(Theme.LIGHT)
    dialog = EditFileTypeDialog(".txt", "text", ["text"], 1 << 20, True)
    assert PALETTES[Theme.LIGHT]["warning"] in dialog.warning.styleSheet()


@pytest.mark.parametrize("theme_name", [Theme.LIGHT, Theme.DARK])
def test_the_commit_diff_is_drawn_in_the_themes_colours(restore_theme, theme_name):
    from app.search.gitsearch import CommitDetail
    from app.ui.commit_preview import commit_html

    theme.stylesheet(theme_name)
    palette = PALETTES[theme_name]
    detail = CommitDetail(commit="a" * 40, subject="s",
                          diff="@@ -1 +1 @@\n-old line\n+new line")
    page = commit_html(detail, "line")
    assert f'<span style="color:{palette["success"]}">+new ' in page
    assert f'<span style="color:{palette["danger"]}">-old ' in page
    assert f'<span style="color:{palette["accent_text"]}">@@' in page
    assert f'background-color:{palette["mark"]}; color:{palette["mark_text"]}' in page


def test_the_contacts_list_starts_in_the_themes_ink(qapp, restore_theme):
    from app.ui.contacts.model import ContactModel

    theme.stylesheet(Theme.LIGHT)
    palette = PALETTES[Theme.LIGHT]
    ink = ContactModel()._ink
    assert ink == {"warning": palette["warning"], "ok": palette["text_dim"],
                   "dim": palette["text_faint"]}


def test_a_bar_with_no_accent_falls_back_to_the_themes(qapp, restore_theme):
    from PySide6.QtGui import QColor

    from app.ui.widgets.shimmer_bar import fill_colours

    theme.stylesheet(Theme.LIGHT)
    start, _end = fill_colours("not a colour")
    assert start == QColor(PALETTES[Theme.LIGHT]["accent_bar"])


def test_the_splash_faint_text_is_the_brands_chrome_on_dark():
    """The splash's faint line was `#9b95c4`, from the retired navy family; the
    brand's chrome-on-dark (brand.json accent3) is the light grey made for it."""
    from app.ui import splash

    assert splash.BRAND_TEXT_FAINT.lower() == "#cdcbd8"
    assert contrast(splash.BRAND_TEXT_FAINT, splash.BRAND_NAVY) >= 4.5
    # A progress bar is two graphics, and each needs 3:1 (WCAG 1.4.11): the empty
    # track against the navy, or the bar's length can't be seen, and the fill
    # against the track, or how far it has got can't be. The dark ground under the
    # blue fill gave 4.5 for the fill and 1.1 for the track - a bar with no visible
    # end. No track colour serves the blue fill both ways, so the fill is white.
    assert contrast(splash.PROGRESS_TRACK, splash.BRAND_NAVY) >= 3.0
    assert contrast(splash.PROGRESS_FILL, splash.PROGRESS_TRACK) >= 3.0


# --- the guard ------------------------------------------------------------------------

HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})\b")

#: Whole files whose colours are not the window's. Each says why.
ALLOWED_FILES = {
    # Paper: a printed page is white with dark ink whatever the screen's theme.
    "app/ui/contacts_tools/print_dialog.py",
}

#: Module-level names that may hold a literal, with why.
ALLOWED_NAMES = {
    # The brand's own colours on the splash, which is drawn before any theme exists
    # and is the brand's ground in both themes (test_brand_colours pins them).
    ("app/ui/splash.py", "BRAND_NAVY"), ("app/ui/splash.py", "BRAND_STRIPE_GREEN"),
    ("app/ui/splash.py", "BRAND_STRIPE_BLUE"), ("app/ui/splash.py", "BRAND_STRIPE_ORANGE"),
    ("app/ui/splash.py", "BRAND_TEXT_FAINT"), ("app/ui/splash.py", "PROGRESS_TRACK"),
    ("app/ui/splash.py", "PROGRESS_FILL"),
    # White on the brand's text-safe kind fills, the same in both themes by design
    # and measured on every fill by test_brand_colours.
    ("app/ui/result_delegate.py", "BADGE_INK"),
    # The colours a person picks for a tag: user data, stored with the tag.
    ("app/ui/contacts/logic.py", "TAG_COLOURS"),
}

#: `colours.get("token", "#xxxxxx")` and `self._ink("token", "#xxxxxx")`: a theme
#: lookup with a literal default. `theme_colours()` is never empty and both palettes
#: carry every key (test_theme), so the default is never drawn - but there are about
#: ninety of them, and each is harmless, so they are allowed here rather than churned.
#: A literal anywhere else in the call is still caught.
LOOKUPS = {"get", "_ink"}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def hex_literals(source: str, rel: str) -> list[str]:
    """Every hex colour written as a string in `source`, except the allowed ones.

    Parsed, not grepped: a comment or a docstring naming the old value - which this
    house writes on purpose, so the old look can be found - is not a colour in use.
    """
    tree = ast.parse(source)
    skip = _docstrings(tree)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def allowed(node: ast.Constant) -> bool:
        parent = parents.get(id(node))
        if (isinstance(parent, ast.Call) and len(parent.args) >= 2 and parent.args[1] is node):
            func = parent.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in LOOKUPS:
                return True
        walk = parent
        while walk is not None:
            if isinstance(walk, (ast.Assign, ast.AnnAssign)) and parents.get(id(walk)) is tree:
                targets = walk.targets if isinstance(walk, ast.Assign) else [walk.target]
                return any(isinstance(t, ast.Name) and (rel, t.id) in ALLOWED_NAMES
                           for t in targets)
            walk = parents.get(id(walk))
        return False

    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip and HEX.search(node.value) and not allowed(node)):
            found.append(f"{rel}:{node.lineno}: {HEX.search(node.value).group(0)}")
    return found


def test_no_colour_in_the_window_is_a_literal_outside_the_theme():
    offenders = []
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "__pycache__" in path.parts or rel == "app/ui/theme.py" or rel in ALLOWED_FILES:
            continue
        offenders += hex_literals(path.read_text(encoding="utf-8"), rel)
    assert not offenders, (
        "a colour is written as a literal, where no test measures it in either theme. "
        "Use a token from theme_colours() (add one to both palettes in theme.py, with "
        "its contrast, if none fits):\n  " + "\n  ".join(offenders))


def test_the_guard_catches_a_literal_and_ignores_prose():
    """A guard that cannot fail proves nothing."""
    caught = hex_literals('label.setStyleSheet("color: #c62828;")\n', "x.py")
    assert caught == ["x.py:1: #c62828"]
    assert hex_literals('f"color:{a}; background:#ffe066"\n', "x.py")
    assert hex_literals('QColor("#2b2160")\n', "x.py")
    assert hex_literals('X = "#cdcbd8"\n', "app/ui/splash.py")          # not a BRAND_ name
    prose = ('"""Was #757575 on a dark card."""\n'
             '# was #e65100\n'
             'def f():\n    """Old #2e7d32."""\n    return colours.get("text", "#888888")\n'
             'BRAND_NAVY = "#15084B"\n')
    assert hex_literals(prose, "app/ui/splash.py") == []
