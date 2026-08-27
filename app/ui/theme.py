"""Colours, and following the system theme rather than imposing one.

Layer: L5

The first version hardcoded a dark palette. On a machine set to light mode that
is not a style choice, it is a bug: the application looks like it belongs to a
different operating system, and on a bright screen in an office it is harder to
read, not easier.

**The palette is one set of names with two sets of values.** Every rule below
refers to a token - `surface`, `text`, `accent` - so the two themes cannot drift
apart, and adding a third (high contrast, say) means adding values rather than
rewriting rules. Qt stylesheets have no variables, so the substitution happens
in Python before the sheet is handed to Qt.

**Detection, not a setting.** Qt 6.5 exposes the OS preference through
`QStyleHints.colorScheme()`, and it changes at runtime when somebody flips the
system switch. There is a manual override for the cases where the OS is wrong
about what somebody wants, but the default is to follow.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "Theme", "palette_for", "stylesheet", "detect_scheme", "SCHEMES",
    "theme_colours",
]

SCHEMES = ("system", "light", "dark")


class Theme:
    LIGHT = "light"
    DARK = "dark"


#: Both palettes, same keys. A token missing from one is a visible bug in that
#: theme only, which is exactly the sort of thing nobody notices for months - so
#: a test asserts the two have identical key sets.
PALETTES: dict[str, dict[str, str]] = {
    Theme.DARK: {
        # **Near-neutral, and deliberately low in chroma.** The old greys were
        # blue-tinted, which reads as a consumer app; a tool somebody keeps open
        # all day should recede. Two steps of lift - window, surface - and one
        # more for a raised control, rather than four barely-distinguishable
        # greys nobody could order by eye.
        "window": "#191a1c",
        "surface": "#1f2023",
        "surface_alt": "#26282b",
        "surface_hover": "#2d2f33",
        "border": "#303236",
        "border_strong": "#3d4045",
        "divider": "#26282b",
        "text": "#e4e6e8",
        "text_dim": "#a0a6ac",
        "text_faint": "#71777e",
        "accent": "#4b8fd4",
        "accent_soft": "#1e3349",
        "accent_text": "#9cc7f0",
        "accent_bar": "#4b8fd4",
        "focus_ring": "#5f9fdd",
        "highlight": "#ffd479",
        "warning": "#d98b5b",
        "selection_text": "#ffffff",
        "scroll": "#3a3d42",
        "scroll_hover": "#4c5057",
    },
    Theme.LIGHT: {
        # Warm-neutral rather than blue-grey, for the same reason as the dark
        # palette: a quieter ground makes the content the only thing with
        # colour in it.
        "window": "#f7f7f8",
        "surface": "#ffffff",
        "surface_alt": "#f0f1f3",
        "surface_hover": "#e8eaed",
        "border": "#dcdee2",
        "border_strong": "#c2c6cc",
        "divider": "#ebecef",
        "text": "#1b1d20",
        "text_dim": "#585e66",
        "text_faint": "#858b93",
        "accent": "#1f6fb2",
        "accent_soft": "#e4eefa",
        "accent_text": "#155a94",
        "accent_bar": "#2f80c9",
        "focus_ring": "#2f80c9",
        "scroll": "#c9ccd1",
        "scroll_hover": "#adb1b8",
        # Darker than the dark theme's, because yellow highlight on white is
        # nearly invisible - the same token needs a different value, which is
        # the whole reason these are two palettes rather than one with a flag.
        "highlight": "#8a5a00",
        "warning": "#a5541f",
        "selection_text": "#0b1620",
    },
}

#: One sheet, written against tokens. `{token}` is substituted before Qt sees it.
_TEMPLATE = """
/* **One type scale, and a dense one.** Sizes were chosen per-widget - 13, 15,
   12, 11, 10 - which is five sizes doing the work of three and no relationship
   between them. 12/13/15 now: 12 for secondary and metadata, 13 for body, 15
   for the two headlines that earn it. A tool somebody keeps open all day wants
   more on screen, not larger letters. */
QWidget {{ background: {window}; color: {text}; font-size: {body}; }}

/* **The search box is the one control that should feel large.** Everything
   else tightens; this stays roomy because it is where every session starts and
   because a cramped input invites cramped queries. */
QLineEdit {{
    background: {surface}; border: 1px solid {border}; border-radius: 5px;
    padding: 7px 10px; font-size: {large}; selection-background-color: {accent};
    selection-color: {selection_text};
}}
QLineEdit:hover {{ border-color: {border_strong}; }}
/* **Two pixels of accent, not one.** A focus ring that only changes hue is
   invisible to somebody who cannot separate those hues, and hard to spot for
   everybody else on a dense screen. Width carries it as well as colour - the
   same reasoning as `#statWarn` and the tab bar. */
QLineEdit:focus {{ border: 2px solid {focus_ring}; padding: 6px 9px; }}
QLineEdit:disabled {{ background: {surface_alt}; color: {text_faint}; }}

/* QListView is named explicitly. The results list stopped being a
   QListWidget when rows became data rather than widgets, and this rule was
   not updated - so the one list people look at most had no surface, no
   border and no radius, while every other list did. */
QListWidget, QListView, QTableWidget, QPlainTextEdit, QTreeWidget, QTreeView {{
    background: {surface}; border: 1px solid {border}; border-radius: 5px;
    /* Rows own their own separation; a grid of lines is the single most dated
       thing a Qt table does. */
    gridline-color: {divider};
    alternate-background-color: {surface};
}}
/* **Hover before selection.** A dense list with no hover state gives no
   feedback that a row is a target at all, which is most of why a table feels
   inert rather than responsive. */
QListWidget::item:hover, QListView::item:hover,
QTableWidget::item:hover, QTreeWidget::item:hover {{
    background: {surface_hover};
}}
QListWidget::item:selected, QListView::item:selected,
QTableWidget::item:selected, QTreeWidget::item:selected {{
    background: {accent_soft}; color: {text};
}}
QListWidget::item, QListView::item, QTreeWidget::item {{
    padding: 3px 4px; border-radius: 3px;
}}
/* A header that reads as a label rather than as a button: no border box, one
   hairline under it, and the small-caps weight tables use to say "this names
   the column, it is not content". */
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {surface}; color: {text_faint};
    font-size: {small}; font-weight: 600;
    border: none; border-bottom: 1px solid {border}; padding: 5px 6px;
}}
QHeaderView::section:hover {{ color: {text_dim}; background: {surface_alt}; }}
QTableWidget {{ selection-background-color: {accent_soft}; }}

QPushButton {{
    background: {surface_alt}; border: 1px solid {border};
    border-radius: 4px; padding: 5px 11px; color: {text};
}}
QPushButton:hover {{ background: {surface_hover}; border-color: {border_strong}; }}
QPushButton:pressed {{ background: {surface}; }}
QPushButton:focus {{ border: 1px solid {focus_ring}; }}
QPushButton:checked {{
    background: {accent_soft}; border-color: {accent}; color: {accent_text};
}}
QPushButton:disabled {{
    color: {text_faint}; background: {surface}; border-color: {divider};
}}

/* **The scrollbars were never styled**, so every pane carried the chunky
   native ones with their stepper arrows - the single most dated element on
   screen and the one nobody mentions because it is everywhere. Thin, no
   steppers, and the handle only gains contrast under the pointer. */
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent; height: 11px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {scroll}; border-radius: 5px; min-height: 28px;
    margin: 2px 3px 2px 3px;
}}
QScrollBar::handle:horizontal {{
    background: {scroll}; border-radius: 5px; min-width: 28px;
    margin: 3px 2px 3px 2px;
}}
QScrollBar::handle:hover {{ background: {scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0; border: none; background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* Menus and tooltips were unstyled too, so both arrived in the platform's own
   colours - a light context menu over a dark window, which reads as a bug. */
QMenu {{
    background: {surface}; border: 1px solid {border_strong};
    border-radius: 6px; padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent_soft}; color: {text}; }}
QMenu::item:disabled {{ color: {text_faint}; }}
QMenu::separator {{ height: 1px; background: {divider}; margin: 4px 8px; }}

QToolTip {{
    background: {surface_alt}; color: {text};
    border: 1px solid {border_strong}; border-radius: 5px;
    padding: 5px 8px; font-size: {small};
}}

QToolButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: 4px; padding: 4px 8px; color: {text_dim};
}}
QToolButton:hover {{ background: {surface_hover}; color: {text}; }}
QToolButton:pressed, QToolButton:checked {{
    background: {accent_soft}; color: {accent_text};
}}
QToolButton:focus {{ border-color: {focus_ring}; }}

/* **Colour only: a geometry property here stops the height following the font.**

   This said `spacing: 7px; color: {text};`, and "some of the check boxes are
   cut" was reported against it. What is measured, and what is only suspected,
   are worth separating.

   Measured: with `spacing` present a QCheckBox's size hint is a flat **16
   pixels** high whatever font it is given, because a geometry property moves
   the widget onto QStyleSheetStyle's own sizing, which derives the height from
   the check indicator and never looks at the text. Without it the hint is 21
   and tracks the font. On this machine the label needs 15, so the old rule left
   exactly one pixel of slack - and `color` on its own was checked the same way
   and leaves the size hint identical to the unstyled one, so the colour is free
   and only the geometry costs anything.

   Not measured, and not claimed: that this is what the owner saw. Reproducing
   it needs Windows - Segoe UI's metrics at the same 13px are taller than the
   fallback here, and a scaled display is taller again, which would put that one
   pixel of slack under water and would explain why only *some* boxes looked
   wrong. It could not be confirmed on Linux, where the page renders correctly
   either way.

   The rule goes regardless. It buys nothing that colour does not, and a height
   that ignores its own font is wrong whether or not it is today's bug.
   `test_theme.test_a_checkbox_is_tall_enough_for_its_own_label` asserts that
   rule rather than the number, so the next declaration added here cannot
   quietly pin the height again. */
QCheckBox, QRadioButton {{ color: {text}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {text_faint}; }}

QSplitter::handle {{ background: {divider}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {accent}; }}

QStatusBar {{ color: {text_dim}; border-top: 1px solid {divider}; }}
QStatusBar::item {{ border: none; }}

QProgressBar {{
    background: {surface_alt}; border: 1px solid {border};
    border-radius: 5px; text-align: center; color: {text};
}}
/* **A styled chunk with no width renders busy mode as a dead full bar.**
   `setRange(0, 0)` is Qt's indeterminate mode, and it is what an index run
   shows for most of its length - the size of the job is genuinely unknown
   until the walk finishes. Under QStyleSheetStyle a chunk with no `width`
   commonly paints across the whole groove and does not animate, so "we do not
   know yet" looked exactly like "finished, and stuck". Giving the chunk a
   width and a margin restores the moving block, which is the only thing on
   screen saying the run is alive. */
QProgressBar::chunk {{
    background: {accent_bar}; border-radius: 4px;
    width: 18px; margin: 1px;
}}

/* A group's title does the separating, so the box around it can be almost
   nothing. Settings was a page of heavy rectangles; it is a page of sections
   now. */
QGroupBox {{
    border: 1px solid {divider}; border-radius: 6px;
    margin-top: 12px; padding-top: 12px; background: {surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {text_dim}; font-size: {small}; font-weight: 600;
}}

/* Tabs drawn as tabs.

   The first version was an underline: transparent tabs with a coloured bar
   under the selected one. It is a clean look and it was the wrong one here,
   because these are not sections of one page - Search, Files, Mail and Code are
   four different tools that happen to share a window, and an underline reads as
   emphasis rather than as separation. A tab with an edge says "this is a
   surface, and behind it are others".

   The pane's `top: -1px` is what joins the two: it pulls the page up under the
   tab bar so the selected tab's bottom edge and the page's top edge are the
   same line. The selected tab then paints that line in the page colour and
   appears to open into it, which is the whole trick. Without the negative
   offset the tab floats a pixel above its own page and every tab looks
   unselected.

   **Every brace in this template is doubled**, comments included - the sheet
   goes through `str.format` to substitute the palette, and a single brace in
   prose is a KeyError at startup rather than a styling problem. Which is what
   happened while this block was being written.

   **The accent is on top, not underneath.** Shape and colour both change when a
   tab is selected, and the accent bar survives at the top edge - so the state
   is not carried by hue alone. Same reasoning as `#statWarn` further down. */
QTabWidget::pane {{
    border: 1px solid {border};
    border-top-left-radius: 0; border-top-right-radius: 0;
    border-bottom-left-radius: 6px; border-bottom-right-radius: 6px;
    top: -1px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: {surface_alt}; color: {text_dim};
    border: 1px solid {border};
    border-top: 2px solid transparent;
    border-top-left-radius: 5px; border-top-right-radius: 5px;
    /* Tighter than it was, and the label carries the weight instead. Four
       tools in a row of chunky tabs reads as a website's navigation; four
       compact ones read as panes of one application. */
    padding: 5px 14px; margin-right: 2px; margin-top: 3px;
    font-size: {small}; font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {text}; background: {surface}; }}
QTabBar::tab:selected {{
    background: {window}; color: {text};
    border-top-color: {accent};
    /* Painted in the page colour, which is what erases the line between the
       tab and the page below it. */
    border-bottom-color: {window};
    margin-top: 0px;
}}
QTabBar::tab:disabled {{ color: {text_faint}; }}
/* Keyboard focus must be visible on its own. Ctrl+Tab moves between these and
   the selection colour alone is not enough to say where the focus went. */
QTabBar::tab:focus {{ border-color: {accent}; border-top-color: {accent}; }}

QComboBox, QSpinBox, QTimeEdit {{
    background: {surface}; border: 1px solid {border};
    border-radius: 4px; padding: 4px 8px; color: {text};
}}
QComboBox:hover, QSpinBox:hover, QTimeEdit:hover {{ border-color: {border_strong}; }}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus {{ border-color: {focus_ring}; }}
QComboBox:disabled, QSpinBox:disabled, QTimeEdit:disabled {{
    color: {text_faint}; background: {surface_alt};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {surface}; border: 1px solid {border_strong};
    border-radius: 5px; padding: 3px;
    selection-background-color: {accent_soft}; selection-color: {text};
}}

#resultPath {{ font-weight: 600; color: {accent}; }}
/* The name is the headline now - a browser result, not a printed report. It
   reads as primary text rather than as a link, because it is the thing being
   named and not the thing being navigated to. */
#resultName {{ font-weight: 600; color: {text}; font-size: {large}; }}
/* A short text tag rather than an icon font: text survives dark mode, high-DPI
   and a missing font file, none of which is worth paying for yet. */
/* A chip rather than an outline: a filled shape at 12px reads as a label at a
   glance, where a 10px outlined one reads as a smudge until you look at it. */
#resultKind {{
    color: {text_dim}; font-size: {small}; font-weight: 600;
    background: {surface_alt}; border: none; border-radius: 3px;
    padding: 1px 6px; margin-right: 6px;
}}
#resultMeta {{ color: {text_faint}; font-size: {small}; }}
#resultMissing {{ color: {warning}; font-size: {small}; }}
#resultSnippet {{ color: {text}; }}
/* The snippet is painted by `result_delegate`, not laid out by Qt, so no
   stylesheet rule can reach the matched words - `#resultSnippet b` never
   applied to a single one. Matches were signalled by weight alone, which is
   the one cue somebody who cannot distinguish them has no substitute for.
   The delegate reads `highlight` from `theme_colours()` and draws it. */
#resultSnippet b {{ color: {highlight}; font-weight: 700; }}
#searchStatus, #resultsSummary, #indexDetail {{ color: {text_faint}; font-size: {small}; }}
#indexHeadline, #graphHeadline {{ font-size: {large}; font-weight: 600; }}
#indexTotals {{ color: {text_dim}; font-size: {small}; }}
#skipHeading {{ font-weight: 600; }}
#skipFix {{ color: {text_dim}; }}
#skipExamples {{ color: {text_faint}; font-size: {small}; }}
/* The hint under a setting, and the tree's `N of M` line. One rule, because
   they are the same thing: a quiet sentence explaining the control above it. */
#settingsHint {{ color: {text_faint}; font-size: {small}; }}
/* A notice is not an error. It sits on the accent's soft ground so it reads as
   information the application is volunteering, rather than as a failure. */
#noticeBar {{
    background: {accent_soft}; color: {text};
    border: 1px solid {border}; border-radius: 4px;
    padding: 6px 10px; font-size: {small};
}}

/* `index_stats` sets one of these two on every value it shows, and only
   `statValue` had a rule - so a figure the code had decided was worth warning
   about rendered identically to one that was fine. The warning was computed,
   assigned, and invisible. Weight as well as colour, because colour alone is
   not a signal everybody receives. */
#statValue {{ color: {text}; font-weight: 600; }}
#statWarn {{ color: {warning}; font-weight: 700; }}
#statLabel {{ color: {text_faint}; font-size: {small}; }}
"""


def detect_scheme(app: Optional[object] = None) -> str:
    """The operating system's preference, or dark if it cannot be read.

    `QStyleHints.colorScheme()` arrived in Qt 6.5. Older builds, and any future
    change to the enum, fall through to a working default rather than raising -
    a theme is not worth failing to start over.
    """
    try:
        from PyQt6.QtCore import Qt  # noqa: PLC0415
        from PyQt6.QtGui import QGuiApplication  # noqa: PLC0415

        instance = app or QGuiApplication.instance()
        if instance is None:
            return Theme.DARK
        scheme = instance.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return Theme.LIGHT
        if scheme == Qt.ColorScheme.Dark:
            return Theme.DARK
    except Exception:                       # noqa: BLE001 - see the docstring
        pass
    return Theme.DARK


def palette_for(preference: str, *, detected: Optional[str] = None) -> dict[str, str]:
    """Resolve a preference ("system" / "light" / "dark") to actual colours."""
    if preference in PALETTES:
        return PALETTES[preference]
    return PALETTES.get(detected or Theme.DARK, PALETTES[Theme.DARK])


#: The palette the sheet was last built from.
#:
#: **Anything that paints itself must read this.** A `QStyledItemDelegate` draws
#: with a `QPainter` and never sees the stylesheet, so it has no way to know what
#: the rest of the window looks like. The obvious substitute - the widget's
#: `QPalette` - is the operating system's, because nothing here sets one; on a
#: light-mode machine with the theme forced to dark that painted near-black text
#: onto a near-black background, and the results list alone was unreadable while
#: every styled widget looked right.
_current: dict[str, str] = dict(PALETTES[Theme.DARK])


def theme_colours() -> dict[str, str]:
    """The tokens the current sheet was built from. Never empty."""
    return dict(_current)


#: The type scale, as multiples of the system font rather than as pixels.
#:
#: **§6b: text has to follow "Make text bigger".** The seventeen rules in the
#: template were pixel sizes, which Qt scales for DPI and does *not* scale for
#: the accessibility setting - so somebody who had turned their text up got a
#: window that ignored them. That matters for every older relative this
#: product now targets, and it was the one accessibility item the 25 August
#: review left partial.
#:
#: **The multipliers are derived, not chosen.** At 96 DPI one point is 4/3 of
#: a pixel and the Windows default font is 9pt, which is 12px. The existing
#: scale of 12/13/15px is therefore 1.0, 1.083 and 1.25 times the system font
#: - so on a default machine these reproduce today's window exactly, and on a
#: machine with larger text they grow with it.
SCALE: dict = {"small": 1.0, "body": 13.0 / 12.0, "large": 15.0 / 12.0}

#: Used when there is no `QApplication` to ask - a test, or a stylesheet built
#: before the app exists. 9pt is the Windows default.
DEFAULT_POINT_SIZE = 9.0


def base_point_size() -> float:
    """The system font size in points. **Never raises.**

    A pixel-sized font reports `pointSizeF() == -1`, which is a real state on
    some Linux themes; the fallback covers it rather than propagating a
    negative into every rule in the sheet.
    """
    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            size = float(app.font().pointSizeF())
            if size > 0:
                return size
    except Exception:                              # noqa: BLE001 - a lookup
        pass
    return DEFAULT_POINT_SIZE


def font_sizes(base: Optional[float] = None) -> dict:
    """`{small, body, large}` as `pt` strings for the sheet."""
    point = float(base if base and base > 0 else base_point_size())
    return {name: f"{round(point * factor, 1)}pt"
            for name, factor in SCALE.items()}


def stylesheet(preference: str = "system", *, detected: Optional[str] = None,
               base_pt: Optional[float] = None) -> str:
    """The full Qt stylesheet for a preference.

    Records the palette it used, so `theme_colours()` and the sheet can never
    describe different themes.

    `base_pt` is for tests and for a caller that already knows the size; left
    out, the system font is asked.
    """
    global _current

    colours = palette_for(preference, detected=detected)
    _current = dict(colours)
    return _TEMPLATE.format(**colours, **font_sizes(base_pt))
