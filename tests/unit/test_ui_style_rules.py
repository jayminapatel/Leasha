r"""One Leasha: the tooltip look and wording, the `/` menus' icons, the icon sizes and the spacing scale.

Layer: L5. UI phase B, wave 3 (2026-10-13). Each rule here is written down in
`docs/UI_STYLE_GUIDE.md`, which points back at the test that holds it - this file.

Static where it can be (a scan of `app/ui`, like `test_tooltips.py`), so it runs headless
and covers files nobody thought to test; offscreen Qt where the rule is about what is drawn.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def _modules() -> list:
    return sorted(p for p in UI.rglob("*.py") if p.name != "__init__.py")


def _rel(path: Path) -> str:
    return path.relative_to(UI).as_posix()


# ---------------------------------------------------------------------------
# 1. One tooltip look
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_one_tooltip_rule_is_drawn_from_the_theme_tokens(scheme):
    """Every tooltip in the window is the raised surface, body ink and the strong border, in both themes."""
    from app.ui import theme

    sheet = theme.stylesheet(scheme)
    rules = re.findall(r"QToolTip\s*\{([^}]*)\}", sheet)
    assert len(rules) == 1, "one QToolTip rule, or two looks"
    colours = theme.PALETTES[scheme]
    rule = rules[0]
    assert f"background: {colours[theme.TOOLTIP['tooltip_bg']]}" in rule
    assert f"color: {colours[theme.TOOLTIP['tooltip_text']]}" in rule
    assert colours[theme.TOOLTIP["tooltip_border"]] in rule
    assert theme.TOOLTIP_SIZES["tooltip_radius"] in rule


def test_every_tooltip_token_names_a_colour_in_both_palettes():
    from app.ui import theme

    for token in theme.TOOLTIP.values():
        for palette in theme.PALETTES.values():
            assert token in palette, token


def test_no_widget_styles_its_own_tooltips():
    """A second `QToolTip { }` rule anywhere else is a second tooltip look."""
    own = [_rel(p) for p in _modules()
           if p.name != "theme.py" and re.search(r"QToolTip\s*\{", p.read_text(encoding="utf-8"))]
    assert not own, f"these files carry their own tooltip style: {own}"


# ---------------------------------------------------------------------------
# 1b. One wording rule
# ---------------------------------------------------------------------------

#: Rich text Qt would draw in a second look: bold runs, line-break tags, entities.
_MARKUP = re.compile(r"<\s*/?\s*(b|i|u|br|p|a|ul|ol|li|span|div|font|html|qt|tt|code|small|big|h\d)\b|&nbsp;|&amp;",
                     re.IGNORECASE)

#: Words that describe the control instead of what it does.
_SELF_DESCRIBING = re.compile(r"^(this (button|control|box|checkbox)|button\b|tooltip\b)", re.IGNORECASE)

#: Tooltips that legitimately break the capital-letter rule, by (file, opening). Each is a claim that has to stay
#: true: the window box's tooltip opens with the format codes it accepts, which are lower case because the
#: format is.
_LOWER_OK = {("widgets/window_box.py", "yyyy ")}


def _tooltips(tree: ast.AST) -> list:
    """`(line, text)` for every literal tooltip: `setToolTip("...")` and a `tip=`/`tooltip=` keyword."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setToolTip" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((node.lineno, arg.value))
        if isinstance(node, ast.keyword) and node.arg in ("tip", "tooltip", "toolTip") \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found.append((node.value.lineno, node.value.value))
    return found


def _labels(tree: ast.AST) -> dict:
    """`{variable: words}` for every control made with its words: `self.go = QPushButton("Go")`."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and node.value.args:
            first = node.value.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                for target in node.targets:
                    key = getattr(target, "attr", None) or getattr(target, "id", None)
                    if key:
                        found[key] = first.value
    return found


def wording_faults(path_name: str, source: str) -> list:
    """Every way a file's literal tooltips break the wording rule (the guide's "Tooltips" section)."""
    tree = ast.parse(source)
    faults = []
    for line, text in _tooltips(tree):
        if not text:
            continue                                   # setToolTip("") clears one: not a tooltip
        where = f"{path_name}:{line}"
        if text != text.strip():
            faults.append(f"{where} starts or ends with white space")
        if _MARKUP.search(text):
            faults.append(f"{where} is rich text - plain words only, so every tooltip looks alike")
        if text[:1].islower() and not any(path_name == f and text.startswith(o) for f, o in _LOWER_OK):
            faults.append(f"{where} starts with a small letter")
        if _SELF_DESCRIBING.match(text):
            faults.append(f"{where} says what the control is, not what it does")
    labels = _labels(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setToolTip" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            owner = node.func.value
            key = getattr(owner, "attr", None) or getattr(owner, "id", None)
            words = labels.get(key, "").replace("&", "").strip().rstrip(".…").lower()
            if words and words == node.args[0].value.strip().rstrip(".…").lower():
                faults.append(f"{path_name}:{node.lineno} only repeats the control's own words")
    return faults


@pytest.mark.parametrize("path", _modules(), ids=_rel)
def test_every_tooltip_follows_the_wording_rule(path):
    """Plain words, a capital letter first, no stray spaces, and something the label does not already say."""
    faults = wording_faults(_rel(path), path.read_text(encoding="utf-8"))
    assert not faults, "\n  ".join(["Tooltips that break the wording rule:", *faults])


def test_the_wording_guard_would_notice_each_fault():
    """A static scan is easy to write so it matches nothing; this one is shown each fault once."""
    source = (
        "self.a = QPushButton('Go')\n"
        "self.a.setToolTip('Go')\n"
        "self.b.setToolTip('<b>Stops</b> the run.')\n"
        "self.c.setToolTip('stops the run.')\n"
        "self.d.setToolTip(' Stops the run.')\n"
        "self.e.setToolTip('This button stops the run.')\n"
        "self.f.setToolTip('Stops the run and keeps what it found.')\n"
        "self.g.setToolTip('')\n")
    faults = wording_faults("x.py", source)
    assert len(faults) == 5, faults
    assert not [f for f in faults if ":7 " in f or ":8 " in f]


# ---------------------------------------------------------------------------
# 2. A Lucide icon for every `/` switch
# ---------------------------------------------------------------------------

def _catalogues() -> dict:
    from app.search.commands import ACTIONS, COMMANDS
    from app.search.gitquery import GIT_COMMANDS
    from app.ui.presenter.contact_commands import CONTACT_COMMANDS
    from app.ui.widgets.code_commands import CODE_CATALOGUE, SEARCH_CATALOGUE

    return {"search": SEARCH_CATALOGUE, "code": CODE_CATALOGUE, "index": COMMANDS, "git": GIT_COMMANDS,
            "actions": ACTIONS, "contacts": CONTACT_COMMANDS}


def test_every_switch_in_every_catalogue_has_a_lucide_icon_that_ships():
    from app.ui.widgets.command_icon import OFFER_PICTURE, VALUE_PICTURE, picture_for
    from app.ui.widgets.icons import ICON_NAMES, icon_file

    missing = [f"{where}: /{command.name}" for where, catalogue in _catalogues().items()
               for command in catalogue if picture_for(command) not in ICON_NAMES]
    assert not missing, f"switches with no Lucide icon in command_icon.PICTURES: {missing}"
    every = {picture_for(c) for cat in _catalogues().values() for c in cat} | {VALUE_PICTURE, OFFER_PICTURE, "undo-2"}
    assert all(icon_file(name) for name in every), sorted(n for n in every if not icon_file(n))


def test_a_contacts_name_is_a_person_and_a_search_name_is_a_file():
    """The same word in two boxes means two things, and the picture says which."""
    from app.search.commands import command_for
    from app.ui.presenter.contact_commands import CONTACT_COMMANDS
    from app.ui.widgets.command_icon import picture_for

    contact_name = next(c for c in CONTACT_COMMANDS if c.name == "name")
    assert picture_for(contact_name) == "user"
    assert picture_for(command_for("name")) == "file"


def test_the_search_slash_menu_draws_the_lucide_icon_of_each_switch(qtbot):
    from PySide6.QtWidgets import QLineEdit

    from app.ui.widgets.code_commands import SEARCH_CATALOGUE, search_matching
    from app.ui.widgets.command_icon import picture_for, text_colour
    from app.ui.widgets.command_popup import CommandPopup
    from app.ui.widgets.icons import ICON_SIZES, icon

    box = QLineEdit()
    qtbot.addWidget(box)
    popup = CommandPopup(box, catalogue=SEARCH_CATALOGUE, matcher=search_matching)
    popup.show_all()
    ink = text_colour(popup.popup()).name()
    model = popup._model
    assert model.rowCount() == len(popup._matches) > 20
    for row, command in enumerate(popup._matches):
        drawn = model.item(row).icon()
        assert not drawn.isNull(), command.name
        assert drawn.cacheKey() == icon(picture_for(command), ink).cacheKey(), command.name
    assert popup.popup().iconSize().width() == ICON_SIZES["menu"]


def test_the_chat_slash_menu_leads_with_the_icon_not_a_character(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPlainTextEdit

    from app.ui.widgets.chat_commands import attach_chat_commands

    edit = QPlainTextEdit()
    qtbot.addWidget(edit)
    edit.show()
    menu = attach_chat_commands(edit)
    edit.setPlainText("/")
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    assert menu._list.count() > 10
    for row in range(menu._list.count()):
        item = menu._list.item(row)
        assert not item.icon().isNull(), item.data(Qt.ItemDataRole.UserRole)
        assert item.text().startswith("/"), item.text()


# ---------------------------------------------------------------------------
# 3. One table of icon sizes
# ---------------------------------------------------------------------------

#: Files that still give an icon size as a number, each named with why. **Owned by the Contacts wave**
#: (`ui/c-contacts`, running beside this one): moved onto `ICON_SIZES` when it lands, not here, so the two do
#: not collide. `person_photo.py`'s 96px is a face picture, not an icon.
_SIZE_LITERALS_OK = {"contacts/parts.py", "contacts/person_photo.py", "contacts_tools/_common.py"}


def _literal_icon_sizes(source: str) -> list:
    """Lines that call `setIconSize(QSize(<int>, <int>))` with numbers written in."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setIconSize" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "QSize" \
                    and arg.args and all(isinstance(a, ast.Constant) for a in arg.args):
                found.append(node.lineno)
    return found


@pytest.mark.slow
def test_every_icon_size_comes_from_the_one_table():
    loose = [f"{_rel(p)}:{line}" for p in _modules() if _rel(p) not in _SIZE_LITERALS_OK
             for line in _literal_icon_sizes(p.read_text(encoding="utf-8"))]
    assert not loose, ("icon sizes written as numbers - use icons.icon_size(role) / ICON_SIZES: " + ", ".join(loose))


def test_the_size_table_is_the_measured_set_and_its_users_read_it():
    from app.ui.widgets import buttons, number_field, rail
    from app.ui.widgets.icons import ICON_SIZES, icon_size

    assert sorted(set(ICON_SIZES.values())) == [14, 16, 20, 22]
    assert ICON_SIZES["menu"] == ICON_SIZES["list"] == ICON_SIZES["button"] == ICON_SIZES["toolbar"] == 16
    assert buttons.ICON_PX == ICON_SIZES["button"] and rail.ICON_SIZE == ICON_SIZES["rail"]
    assert number_field._ICON_PX == ICON_SIZES["inline"]
    assert icon_size("rail").width() == icon_size("rail").height() == 20
    with pytest.raises(KeyError):
        icon_size("huge")


def test_the_size_guard_would_notice_a_number():
    assert _literal_icon_sizes("b.setIconSize(QSize(16, 16))\nb.setIconSize(icon_size('menu'))\n") == [1]


# ---------------------------------------------------------------------------
# 4. The spacing scale
# ---------------------------------------------------------------------------

#: Files that still write a gap the scale names as a number. **Owned by the Contacts wave** (`ui/c-contacts`,
#: running beside this one), so moved onto `theme.SPACE_*` when it lands rather than here. Never grows.
_SPACING_LITERALS_OK = {
    "contacts/editor.py", "contacts/fields.py", "contacts/index_panel.py", "contacts/bulkbar.py",
    "contacts/enrich_panel.py", "contacts_tools/cleanup_dialog.py", "contacts_tools/duplicates_dialog.py",
    "contacts_tools/_common.py", "widgets/person_card.py",
}

_SPACING_CALLS = {"setSpacing", "addSpacing", "setContentsMargins", "setHorizontalSpacing", "setVerticalSpacing"}


def _scale_numbers(source: str) -> list:
    """Lines where a layout writes in numbers a gap the scale could name in full.

    A call that mixes in a number off the scale (`setContentsMargins(36, 24, 36, 16)`, the About box's brand
    clear space) is left as it is: half in names and half in numbers would read worse than either.
    """
    from app.ui.theme import SPACING

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in _SPACING_CALLS:
            numbers = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, int)]
            if any(numbers) and all(n == 0 or n in SPACING for n in numbers):
                found.append(node.lineno)
    return found


def test_the_scale_is_the_8px_rhythm():
    from app.ui import theme

    assert theme.SPACING == (4, 8, 12, 16, 24)
    assert (theme.SPACE_XS, theme.SPACE_S, theme.SPACE_M, theme.SPACE_L, theme.SPACE_XL) == theme.SPACING
    from app.ui.widgets.buttons import ROW_SPACING
    assert ROW_SPACING == theme.SPACE_S


@pytest.mark.slow
def test_every_gap_the_scale_names_is_written_by_its_name():
    loose = [f"{_rel(p)}:{line}" for p in _modules() if _rel(p) not in _SPACING_LITERALS_OK
             for line in _scale_numbers(p.read_text(encoding="utf-8"))]
    assert not loose, "gaps written as numbers the scale names - use theme.SPACE_*: " + ", ".join(loose)


def test_the_spacing_exemptions_stay_short():
    """Every entry is a file still to move; the list only ever shrinks."""
    assert len(_SPACING_LITERALS_OK) <= 9 and len(_SIZE_LITERALS_OK) <= 3


def test_the_spacing_guard_would_notice_a_number():
    source = "l.setSpacing(8)\nl.setSpacing(SPACE_S)\nl.setContentsMargins(0, 6, 0, 0)\nl.setContentsMargins(0, 4, 0, 0)\n"
    assert _scale_numbers(source) == [1, 4]


# ---------------------------------------------------------------------------
# 5. The rulebook exists and is the authority
# ---------------------------------------------------------------------------

def test_the_style_guide_points_at_the_code_and_the_tests_that_hold_it():
    root = UI.parents[1]
    guide = (root / "docs" / "UI_STYLE_GUIDE.md").read_text(encoding="utf-8")
    for anchor in ("app/ui/theme.py", "app/ui/widgets/buttons.py", "app/ui/widgets/menus.py",
                   "app/ui/widgets/list_keys.py", "app/ui/widgets/icons.py", "app/ui/widgets/command_icon.py",
                   "app/ui/widgets/confirm.py", "tests/unit/test_tooltips.py", "tests/unit/test_button_system.py",
                   "tests/unit/test_menu_system.py", "tests/unit/test_list_keys.py",
                   "tests/unit/test_ui_style_rules.py", "tests/unit/test_presenter.py"):
        assert anchor in guide, anchor
        assert (root / anchor).is_file(), anchor
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "| How the UI looks and behaves | `docs/UI_STYLE_GUIDE.md` |" in claude
