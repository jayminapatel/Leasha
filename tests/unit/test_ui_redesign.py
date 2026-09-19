r"""UI Redesign (work order 202626160950) - the Qt-free half of §9.

Everything in this file runs without a display and without PyQt6: the
tokens, the pill's words, the chips' arithmetic, the badge colours, the
inspector's facts, and two source-shaped guards (§9c) plus the verbatim
walk (§9d). The Qt half - the rail, the toast, the delegate, the
responsiveness scenario - lives in `test_ui_redesign_qt.py`, which skips
itself where PyQt6 is absent.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "app" / "ui"

#: The commit immediately before this order's first change. §9d reads the
#: pre-order strings from git rather than from a hand-copied fixture.
PRE_ORDER_COMMIT = "12ed8c8"


# ---------------------------------------------------------------------------
# §9a - tokens
# ---------------------------------------------------------------------------

def test_both_palettes_carry_every_redesign_token():
    from app.ui.theme import PALETTES, Theme

    wanted = {"rail", "rail_text", "rail_on", "rail_on_bg", "rail_hover",
              "kind_doc", "kind_mail", "kind_code", "kind_other",
              "chip_bg", "chip_text", "toast_bg", "toast_text", "mark", "mark_text"}
    for scheme in (Theme.LIGHT, Theme.DARK):
        missing = wanted - set(PALETTES[scheme])
        assert not missing, f"{scheme} lacks {sorted(missing)}"
    assert set(PALETTES[Theme.LIGHT]) == set(PALETTES[Theme.DARK])


def test_kind_badges_are_the_same_in_both_themes():
    """A badge is a label; a label that changes hue with the theme is two."""
    from app.ui.theme import PALETTES, Theme

    for token in ("kind_doc", "kind_mail", "kind_code", "kind_other"):
        assert PALETTES[Theme.LIGHT][token] == PALETTES[Theme.DARK][token]


def test_the_stripes_are_the_splash_colours():
    from app.ui import splash
    from app.ui.theme import PALETTES, Theme

    dark = PALETTES[Theme.DARK]
    assert dark["kind_doc"].lower() == splash.BRAND_STRIPE_BLUE.lower()
    assert dark["kind_mail"].lower() == splash.BRAND_STRIPE_ORANGE.lower()
    assert dark["kind_code"].lower() == splash.BRAND_STRIPE_GREEN.lower()


@pytest.mark.parametrize("scheme", ["system", "light", "dark"])
def test_the_sheet_renders_with_no_token_left_unsubstituted(scheme):
    from app.ui.theme import stylesheet

    sheet = stylesheet(scheme, detected="light", base_pt=9.0)
    assert not re.findall(r"\{[a-z_]+\}", sheet)
    for selector in ("#rail", "#railPill", "#segmented", "#chip", "#toast",
                     "#searchBox", "#emptyHeadline", "#inspectorFacts",
                     "#categorySidebar", "QMenuBar", "QGroupBox::title"):
        assert selector in sheet, selector
    assert "QStatusBar" not in sheet, "the status bar is gone ([FINALISE 1])"


def test_the_display_size_follows_the_system_font():
    from app.ui.theme import font_sizes

    assert font_sizes(9.0)["display"] == "19.5pt"                # 26px at 96dpi
    assert float(font_sizes(18.0)["display"][:-2]) == pytest.approx(39.0, rel=0.02)


def test_the_radius_scale_is_not_a_palette_token():
    from app.ui.theme import PALETTES, RADIUS, Theme

    assert set(RADIUS) == {"radius_input", "radius_control", "radius_box", "radius_pill"}
    assert not set(RADIUS) & set(PALETTES[Theme.DARK])


# ---------------------------------------------------------------------------
# §2d - the pill's words
# ---------------------------------------------------------------------------

def test_pill_text_covers_every_state():
    from app.ui.rail_state import FAILED, FINISHED, IDLE, RUNNING, pill_text

    assert pill_text(RUNNING, indexed=1234).headline == "Indexing"
    assert pill_text(RUNNING, indexed=1234).detail == "1,234 so far"
    assert pill_text(RUNNING, indexed=5, paused=True).headline == "Paused"
    assert pill_text(FINISHED, indexed=9, stopped_early=True).headline == "Stopped"
    assert pill_text(FINISHED, documents=39306).detail == "39,306 files"
    assert pill_text(IDLE, documents=None).detail == ""      # nobody has counted
    assert pill_text(FAILED, error="Disk full").detail == "Disk full"


def test_pill_fraction_is_none_when_the_total_is_unknown():
    from app.ui.rail_state import pill_fraction

    assert pill_fraction(5, 0) is None
    assert pill_fraction(5, 10) == 0.5
    assert pill_fraction(50, 10) == 1.0
    assert pill_fraction("x", 10) is None


# ---------------------------------------------------------------------------
# §3d / §9e - chips are a view of the box
# ---------------------------------------------------------------------------

def test_typing_slash_type_pdf_is_one_chip():
    from app.ui.chips_logic import chips_for

    chips = chips_for("boiler /type pdf")
    assert [c.label for c in chips] == ["type: pdf"]


def test_removing_a_chip_edits_only_its_span():
    from app.ui.chips_logic import chips_for, without

    text = "boiler /type pdf /from dave quote"
    chips = chips_for(text)
    assert [c.label for c in chips] == ["type: pdf", "from: dave"]
    assert without(text, chips[0]) == "boiler /from dave quote"
    assert without(text, chips[1]) == "boiler /type pdf quote"


def test_chips_remove_independently():
    from app.ui.chips_logic import chips_for, without

    text = "a /type pdf /from dave"
    first = chips_for(text)[0]
    after = without(text, first)
    assert [c.label for c in chips_for(after)] == ["from: dave"]
    assert without(after, chips_for(after)[0]) == "a"


def test_valueless_switches_and_colon_forms_read_as_typed():
    from app.ui.chips_logic import chips_for

    labels = [c.label for c in chips_for("/newest report -ext:docx")]
    assert labels == ["newest", "not type: docx"]


def test_a_path_is_not_a_chip():
    from app.ui.chips_logic import chips_for

    assert chips_for("report about /var/log 12/03") == []


# ---------------------------------------------------------------------------
# §4a - badge colours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, token", [
    ("pdf", "kind_doc"), ("docx", "kind_doc"), ("xlsx", "kind_doc"),
    ("email", "kind_mail"), ("py", "kind_code"), ("ts", "kind_code"),
    ("jpg", "kind_other"), ("", "kind_other"),
])
def test_badge_token_per_kind(kind, token):
    from app.ui.kind_badge import badge_token

    assert badge_token(kind) == token


def test_badge_word_is_kind_tag_verbatim():
    from app.ui.kind_badge import badge_word
    from app.ui.presenter import kind_tag

    for kind in ("pdf", "email", "py", "unknownthing"):
        assert badge_word(kind) == kind_tag(kind)


# ---------------------------------------------------------------------------
# §5a - inspector facts
# ---------------------------------------------------------------------------

def test_preview_facts_show_only_what_the_row_carries():
    from types import SimpleNamespace

    from app.ui.inspector import preview_facts

    row = SimpleNamespace(kind="pdf", mtime_ns=0, folder="", page=None)
    assert preview_facts(row) == (("Kind", "PDF"),)
    row = SimpleNamespace(kind="pdf", mtime_ns=1_700_000_000 * 10**9,
                          folder="Documents", page=3)
    labels = [label for label, _ in preview_facts(row)]
    assert labels == ["Kind", "Modified", "Folder", "Page"]
    assert preview_facts(None) == ()


# ---------------------------------------------------------------------------
# §9c - source-shaped guards
# ---------------------------------------------------------------------------

def _code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_shell_no_longer_owns_a_tab_widget():
    lines = _code_lines(UI / "shell.py")
    assert not any("self.tabs" in line for line in lines)
    assert not any("QTabWidget(" in line for line in lines)


def test_nothing_under_app_ui_calls_the_status_bar():
    offenders = []
    for path in UI.rglob("*.py"):
        if any(".statusBar()" in line for line in _code_lines(path)):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def _icon_names() -> tuple:
    """`ICON_NAMES` read from the source, so this runs without PyQt6."""
    tree = ast.parse((UI / "widgets" / "icons.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "ICON_NAMES" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("ICON_NAMES not found")


def test_every_shipped_icon_exists_and_the_licence_ships_with_them():
    folder = ROOT / "assets" / "icons"
    assert (folder / "LICENSE").is_file()
    names = _icon_names()
    assert len(names) >= 18
    missing = [n for n in names if not (folder / f"{n}.svg").is_file()]
    assert not missing, missing
    for name in names:
        text = (folder / f"{name}.svg").read_text(encoding="utf-8")
        assert 'stroke="currentColor"' in text, name


def test_no_module_reaches_into_the_icon_folder_except_the_loader():
    """§1c: one function loads an icon. (`preview_loader` previewing a
    person's own .svg file is a different thing and is not caught here.)"""
    offenders = []
    for path in UI.rglob("*.py"):
        if path.name == "icons.py":
            continue
        if any("/ \"icons\"" in line or "assets/icons" in line or "icons/" in line
               for line in _code_lines(path)):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_the_rail_labels_are_the_tab_titles_verbatim():
    """§2b: the strings the window passes are the strings they always were."""
    source = (UI / "shell.py").read_text(encoding="utf-8")
    for title in ("Search", "Files", "Mail", "Code", "Offline Media",
                  "Reports", "Indexing", "Settings"):
        assert f'"{title}"' in source, title
    assert '"Drives"' not in source, "the mockup's 'Drives' must not be built"


# ---------------------------------------------------------------------------
# §9d - every pre-order string still exists verbatim
# ---------------------------------------------------------------------------

_TEXT_CALLS = {"setToolTip", "setPlaceholderText", "setText", "addItem",
               "showMessage", "notify", "setWindowTitle"}
_TEXT_CTORS = {"QLabel", "QCheckBox", "QPushButton", "QGroupBox", "QAction",
               "QToolButton"}


def _ui_strings(source_text: str) -> set[str]:
    tree = ast.parse(source_text)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        if name not in _TEXT_CALLS and name not in _TEXT_CTORS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.strip():
                    found.add(arg.value)
    return found


def _pre_order_text(relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{PRE_ORDER_COMMIT}:{relative}"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        pytest.skip(f"git cannot show {PRE_ORDER_COMMIT}:{relative} here")
    return result.stdout


@pytest.mark.parametrize("relative", [
    "app/ui/shell.py",
    "app/ui/widgets/search_bar.py",
    "app/ui/widgets/result_tools.py",
    "app/ui/widgets/preview.py",
    "app/ui/widgets/window_box.py",
    "app/ui/widgets/category_nav.py",
    "app/ui/first_contact.py",
    "app/ui/results_view.py",
    "app/ui/search_view.py",
    "app/ui/indexing_view.py",
])
def test_every_pre_order_label_tooltip_and_message_still_exists_verbatim(relative):
    """The standing rule, enforced: a superset is fine, a change is not."""
    before = _ui_strings(_pre_order_text(relative))
    after = _ui_strings((ROOT / relative).read_text(encoding="utf-8"))
    if relative == "app/ui/shell.py":
        # The settings and index handlers moved out of `MainWindow` into
        # `app/ui/controllers/` (work order 202626082352 section 7). The
        # strings did not go anywhere - they are the same strings, in the
        # files the handlers now live in - so "still exists verbatim" is asked
        # of the shell and its controllers together, not of the shell alone.
        for controller in sorted((UI / "controllers").glob("*.py")):
            after |= _ui_strings(controller.read_text(encoding="utf-8"))
    missing = before - after
    assert not missing, (
        f"{relative}: these pre-order strings are gone or changed:\n  "
        + "\n  ".join(sorted(missing)))


def test_the_status_bar_messages_all_became_toasts():
    """§6b, counted: every string `showMessage` carried is now a `notify`."""
    before_src = _pre_order_text("app/ui/shell.py")
    before = [n for n in ast.walk(ast.parse(before_src))
              if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "showMessage"]
    # The shell and the controllers carved out of it (order 202626082352
    # section 7) together: a `notify` that moved is still a `notify`.
    after = []
    for path in [UI / "shell.py", *sorted((UI / "controllers").glob("*.py"))]:
        after_src = path.read_text(encoding="utf-8")
        after += [n for n in ast.walk(ast.parse(after_src))
                  if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "notify"]
    assert len(before) >= 30, "the pre-order shell had dozens of these"
    assert len(after) >= len(before), (len(before), len(after))


def test_first_contact_greeting_is_unchanged_and_now_used():
    """§3a: the existing greeting occupies the slot the mockup invented copy for."""
    before = _pre_order_text("app/ui/first_contact.py")
    assert "documents ready to search." in before
    from app.ui.first_contact import greeting
    assert greeting(39306) == "39,306 documents ready to search."
    home = (UI / "widgets" / "search_home.py").read_text(encoding="utf-8")
    assert "greeting(" in home
    assert "Nothing leaves it" not in home
