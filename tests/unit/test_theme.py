"""Colours, and the promise to follow the operating system.

Layer: L5

The first version hardcoded a dark palette. On a light-mode machine that is not
a style choice, it is a bug - the application looks like it belongs to a
different operating system, and it is harder to read on a bright screen rather
than easier. It took a person opening the window to notice, which is exactly
what the presenter/view split exists to minimise; a stylesheet is one of the few
things that genuinely cannot be checked any other way. These tests cover as much
of it as is checkable without a display.
"""

from __future__ import annotations

import re

import pytest

from app.ui.theme import PALETTES, SCHEMES, Theme, palette_for, stylesheet


def test_both_themes_define_exactly_the_same_tokens():
    """A token present in one palette and missing from the other is a visible
    bug in that theme only - the kind nobody notices for months, because
    whoever wrote it was looking at the other one."""
    light = set(PALETTES[Theme.LIGHT])
    dark = set(PALETTES[Theme.DARK])
    assert light == dark, f"only in light: {light - dark}; only in dark: {dark - light}"


def test_every_token_is_a_colour():
    for name, palette in PALETTES.items():
        for token, value in palette.items():
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{name}.{token} = {value!r}"


def test_the_two_themes_are_actually_different():
    """A copy-paste that left both identical would pass every other test here."""
    assert PALETTES[Theme.LIGHT] != PALETTES[Theme.DARK]


def test_light_text_is_dark_and_dark_text_is_light():
    """The one invariant that makes a theme readable at all."""
    def brightness(hex_colour: str) -> int:
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
        return (r * 299 + g * 587 + b * 114) // 1000

    assert brightness(PALETTES[Theme.LIGHT]["text"]) < 100
    assert brightness(PALETTES[Theme.LIGHT]["window"]) > 200
    assert brightness(PALETTES[Theme.DARK]["text"]) > 200
    assert brightness(PALETTES[Theme.DARK]["window"]) < 100


def test_the_highlight_is_readable_in_both_themes():
    """Search-term highlighting is yellow-on-dark in the dark theme, which on
    white is nearly invisible. The same token needs a different value - which is
    the whole reason these are two palettes rather than one with a flag."""
    def brightness(hex_colour: str) -> int:
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
        return (r * 299 + g * 587 + b * 114) // 1000

    assert brightness(PALETTES[Theme.LIGHT]["highlight"]) < 160, "too pale on white"
    assert brightness(PALETTES[Theme.DARK]["highlight"]) > 140, "too dark on black"


# -- resolving a preference --------------------------------------------------

@pytest.mark.parametrize("preference", SCHEMES)
def test_every_offered_scheme_resolves_to_a_palette(preference):
    assert palette_for(preference, detected=Theme.LIGHT)


def test_system_follows_what_was_detected():
    assert palette_for("system", detected=Theme.LIGHT) is PALETTES[Theme.LIGHT]
    assert palette_for("system", detected=Theme.DARK) is PALETTES[Theme.DARK]


def test_an_explicit_choice_overrides_the_system():
    """Somebody who wants dark on a light machine must get dark."""
    assert palette_for("dark", detected=Theme.LIGHT) is PALETTES[Theme.DARK]
    assert palette_for("light", detected=Theme.DARK) is PALETTES[Theme.LIGHT]


def test_an_unknown_preference_falls_back_rather_than_failing():
    """A stored setting from a future version must not stop the window opening."""
    assert palette_for("neon", detected=Theme.LIGHT) is PALETTES[Theme.LIGHT]
    assert palette_for("", detected=None)


# -- the sheet itself --------------------------------------------------------

@pytest.mark.parametrize("preference", SCHEMES)
def test_the_stylesheet_has_no_unsubstituted_tokens(preference):
    """Qt stylesheets have no variables, so substitution happens in Python.

    A `{token}` that survives is not an error Qt reports - it silently ignores
    the whole rule, and the result is one widget that looks wrong for reasons
    nothing explains.
    """
    sheet = stylesheet(preference, detected=Theme.DARK)
    leftovers = re.findall(r"\{[a-z_]+\}", sheet)
    assert leftovers == [], f"unsubstituted: {leftovers}"


def test_the_stylesheet_keeps_its_literal_braces():
    """Qt rules are `{ ... }`, and the template doubles them for `str.format`.

    Getting this wrong produces a sheet Qt discards in full - the window opens
    completely unstyled, which looks like the theme failing to load.
    """
    sheet = stylesheet("dark")
    assert "QWidget {" in sheet
    assert "QProgressBar::chunk {" in sheet
    assert "{{" not in sheet


def test_the_two_sheets_differ():
    assert stylesheet("light") != stylesheet("dark")


def test_detection_never_raises_without_an_application():
    """It is called before anything is certain to exist, and a theme is not
    worth failing to start over."""
    from app.ui.theme import detect_scheme

    assert detect_scheme(None) in (Theme.LIGHT, Theme.DARK)


# -- the tab bar, and the trap in the template --------------------------------

def test_every_brace_in_the_template_is_doubled():
    """The sheet goes through `str.format`, so a single brace anywhere in it -
    including inside a CSS comment - is a KeyError when the window is built.

    It is not a styling bug and it does not degrade: the application fails to
    start, with a traceback naming a token nobody wrote. This happened while
    the tab rules below were being written, which is why it is now a test.
    """
    import re

    from app.ui.theme import Theme, stylesheet

    for scheme in (Theme.LIGHT, Theme.DARK):
        sheet = stylesheet(scheme)
        left = re.findall(r"\{[a-z_]+\}", sheet)
        assert left == [], f"{scheme}: unsubstituted tokens {left}"
        # Doubled braces are how the template escapes CSS. If any survive into
        # the output, one was written singly somewhere and Qt gets a sheet with
        # literal braces in it.
        assert "{{" not in sheet and "}}" not in sheet


def test_the_selected_tab_is_not_signalled_by_colour_alone():
    """Shape, background and an accent edge all change. The same reasoning as
    `#statWarn`: a state carried by hue is a state some people never receive."""
    from app.ui.theme import Theme, stylesheet

    for scheme in (Theme.LIGHT, Theme.DARK):
        sheet = stylesheet(scheme)
        assert "QTabWidget::pane" in sheet
        assert "QTabBar::tab:selected" in sheet
        assert "border-top-color" in sheet
        assert "QTabBar::tab:focus" in sheet, "focus must be visible on its own"


def test_the_pane_is_pulled_under_the_tab_bar():
    """Without the negative offset the selected tab floats a pixel above its
    own page and every tab reads as unselected."""
    from app.ui.theme import stylesheet

    assert "top: -1px" in stylesheet("dark")
