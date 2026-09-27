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
    "theme_colours", "RADIUS", "BUTTON",
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
        # **Faint, but still readable** (order 0x section 9, 2026-09-27). This
        # was #71777e: 3.6 to 1 on a card and 3.9 on the window, and it is the
        # colour of every result count, hint, table header and "Nothing logged
        # yet" - small text that WCAG AA asks to reach 4.5. Lifted just far
        # enough: 5.1 on a card, 5.4 on the window, still clearly quieter than
        # `text_dim`. Was #71777e, if the old look is ever wanted back.
        "text_faint": "#8a9097",
        # **The accent is the brand navy, lifted for a dark ground.** The old
        # blue read as "any Qt app"; the UI Redesign order (202626160950 §0.1)
        # made the splash's navy the one colour the shell owns. On black the
        # navy itself vanishes, so the dark accent is the same hue raised to a
        # lavender that still reads as the same family beside the rail.
        "accent": "#9d8cf0",
        "accent_soft": "#2a2150",
        "accent_text": "#c3b7ff",
        "accent_bar": "#9d8cf0",
        "focus_ring": "#ab9cf5",
        "highlight": "#ffd479",
        # **The rail is a neutral grey, not the brand navy** (owner direction,
        # 2026-09-16: the rail should read as a Windows-style side pane that
        # follows the theme, not a fixed-colour brand strip). Built from the
        # same surface tokens as everything else so it stays aligned with the
        # rest of the window rather than drifting as its own palette; the
        # selected item keeps a soft accent tint so it still reads as chosen.
        "rail": "#26282b",             # == surface_alt
        "rail_text": "#a0a6ac",        # == text_dim
        "rail_on": "#ffffff",
        "rail_on_text": "#ffffff",     # text on the rail's own hover/selected fill
        # **Text and icons on a solid accent fill** - the "Open" button, a chip's
        # remove cross under the pointer. Near-black here, because the dark
        # accent is a light lavender: the white `rail_on` these used measured
        # 2.8 to 1 on it (order 0x section 9, 2026-09-27), under the 4.5 body
        # text needs. This is 6.5 to 1. `rail_on` keeps its other uses.
        "accent_on": "#15131f",
        "rail_on_bg": "#2a2150",       # == accent_soft
        "rail_hover": "#2d2f33",       # == surface_hover
        # **Kind badges carry the brand stripes** (§0.1-5): the three splash
        # colours, identical in both palettes by design - a badge is a label,
        # and a label that changes hue with the theme is two labels.
        "kind_doc": "#0778d9",
        "kind_mail": "#ff9933",
        "kind_code": "#a1b000",
        "kind_other": "#6b5bd6",
        # A chip is a removable filter drawn on the accent's soft ground.
        "chip_bg": "#2a2150",
        "chip_text": "#c3b7ff",
        # A toast inverts the ground so it is seen without being loud.
        "toast_bg": "#e8e5f2",
        "toast_text": "#15131f",
        # Matched words get a tinted ground behind them (§4c) rather than bold
        # alone; `highlight` stays for the accessible text and older callers.
        "mark": "#4a3a00",
        "mark_text": "#ffe08a",
        "warning": "#d98b5b",
        # **A third state, added for the log.** `warning` was the only
        # not-fine colour, and a log that paints an error the same as a
        # warning cannot answer the question it is open to answer - *which
        # lines are the bad ones*. Muted rather than pillar-box: this sits in
        # a wall of text, and a saturated red at 12px vibrates against a dark
        # ground. See `ui/log_lines.TOKENS`, which names it and never a value.
        "danger": "#d97070",
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
        # Darkened for the same reason as the dark palette's (0x section 9):
        # #858b93 was 3.4 to 1 on white and 3.2 on the window. This is 4.9 and
        # 4.6, and still lighter than `text_dim`. Was #858b93.
        "text_faint": "#6b7178",
        # Navy-derived (202626160950 §1a). Dark enough to be text on white;
        # the soft ground is the same hue at a whisper.
        "accent": "#2b1a7a",
        "accent_soft": "#e9e4fb",
        "accent_text": "#2b1a7a",
        "accent_bar": "#2b1a7a",
        "focus_ring": "#4a37b0",
        # See the dark palette's comment: a neutral grey rail, aligned with
        # the rest of the window's own surface tokens rather than a fixed
        # navy. `rail_on` stays white - it is also used elsewhere (chip
        # removal) for text on a dark accent fill - so the rail's own
        # hover/selected text uses `rail_on_text` instead, dark enough to
        # read on this theme's light grey/light accent fills.
        # (2026-09-27, order 0x section 9: chip removal and the "Open" button
        # now take `accent_on` below, not `rail_on`, and the rail's chosen icon
        # takes `rail_on_text`. `rail_on` is still white in both themes.)
        "rail": "#f0f1f3",             # == surface_alt
        "rail_text": "#585e66",        # == text_dim
        "rail_on": "#ffffff",
        "rail_on_text": "#2b1a7a",     # == accent_text
        # White on the navy accent: 13.7 to 1. See the dark palette's note.
        "accent_on": "#ffffff",
        "rail_on_bg": "#e9e4fb",       # == accent_soft
        "rail_hover": "#e8eaed",       # == surface_hover
        "kind_doc": "#0778d9",
        "kind_mail": "#ff9933",
        "kind_code": "#a1b000",
        "kind_other": "#6b5bd6",
        "chip_bg": "#e9e4fb",
        "chip_text": "#2b1a7a",
        "toast_bg": "#1b1826",
        "toast_text": "#f6f5f9",
        "mark": "#fff0c2",
        "mark_text": "#4a3400",
        "scroll": "#c9ccd1",
        "scroll_hover": "#adb1b8",
        # Darker than the dark theme's, because yellow highlight on white is
        # nearly invisible - the same token needs a different value, which is
        # the whole reason these are two palettes rather than one with a flag.
        "highlight": "#8a5a00",
        "warning": "#a5541f",
        # Darker than the dark theme's, for the same reason the highlight is:
        # the same token needs a different value on a white ground, which is
        # why these are two palettes rather than one with a flag.
        "danger": "#a32020",
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
/* **Text draws on whatever is behind it.** The rule above gives every widget the
   window colour, labels and check boxes included, so on a card (a group box, the
   toast, the rail pill) each one painted a grey box of its own around its text.
   Found on 2026-09-20 by grabbing the real window on Windows at 125%: every
   settings row and the whole of the toast had them. An id rule with its own
   background still wins over this one. */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}

/* **The search box is the one control that should feel large.** Everything
   else tightens; this stays roomy because it is where every session starts and
   because a cramped input invites cramped queries. */
QLineEdit {{
    background: {surface}; border: 1px solid {border}; border-radius: {radius_input};
    padding: 7px 10px; font-size: {large}; selection-background-color: {accent};
    selection-color: {selection_text};
}}
QLineEdit:hover {{ border-color: {border_strong}; }}
/* **The one box that is the app** (202626160950 §3). Larger radius, a
   stronger edge, and a shadow-less lift by contrast alone - Qt stylesheets
   have no box-shadow, so the surface colour against the window does the
   lifting. `#searchBox` is the Search page's input; every other QLineEdit
   keeps the rule above. */
#searchBox {{
    border: 1px solid {border_strong}; border-radius: {radius_box};
    padding: 9px 14px;
}}
#searchBox:focus {{ border: 2px solid {focus_ring}; padding: 8px 13px; }}
/* The same box in its opening state, centred and roomy. */
#searchBox[empty_state="true"] {{ padding: 13px 18px; font-size: {large}; }}
#searchBox[empty_state="true"]:focus {{ padding: 12px 17px; }}
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
    background: {surface}; border: 1px solid {border}; border-radius: {radius_control};
    /* Rows own their own separation; a grid of lines is the single most dated
       thing a Qt table does. */
    gridline-color: {divider};
    alternate-background-color: {surface};
}}
/* **Borderless rows** (202626160950 §1b): the painted results list and the
   thumbnail grid sit directly on the window with no frame - the rows are the
   surface. Tables and trees keep their frame because a grid without an edge
   loses its columns. */
#resultsList, #thumbnailGrid, #pinnedList, #recentList {{
    border: none; background: transparent;
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
    padding: 3px 4px; border-radius: {radius_control};
}}
/* The native focus rectangle is a black dotted box round the row's text only, and
   looked like a fault beside the rounded selection (Reports, the category lists).
   The selected row gets the theme's ring while the list has the keyboard; the
   transparent border keeps a row the same size with and without it. */
QListWidget, QTreeWidget {{ outline: 0; }}
QListWidget::item {{ border: 1px solid transparent; }}
QListWidget::item:selected:focus {{ border: 1px solid {focus_ring}; }}
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
    border-radius: {radius_control}; padding: 5px 11px; color: {text};
}}
/* (The filled "Open" button's rule, `primary="true"`, became the button
   system's `buttonRole="primary"` below, 2026-09-27.) */
QPushButton:hover {{ background: {surface_hover}; border-color: {border_strong}; }}
QPushButton:pressed {{ background: {surface}; }}
QPushButton:focus {{ border: 1px solid {focus_ring}; }}
QPushButton:checked {{
    background: {accent_soft}; border-color: {accent}; color: {accent_text};
}}
QPushButton:disabled {{
    color: {text_faint}; background: {surface}; border-color: {divider};
}}

/* **The button system** (`widgets/buttons.py`, owner 2026-09-27: "all buttons
   should be consistent throughout the app"). Every action button carries a
   `buttonRole` of primary, secondary or danger, and these rules give all of
   them one height and one padding. The height is a floor on the space inside
   the padding, big enough for the 16px icon every one of them carries, so a
   button with a taller font still grows with it.
   Written after the plain QPushButton rules on purpose: a rule of equal weight
   that comes later wins, which is also why the primary and danger kinds say
   what they look like when disabled - otherwise a greyed-out primary would
   still look filled and pressable. */
QPushButton[buttonRole="primary"], QPushButton[buttonRole="secondary"],
QPushButton[buttonRole="danger"] {{
    padding: {button_pad_y} {button_pad_right} {button_pad_y} {button_pad_left};
    min-height: {button_min_h}; border-radius: {radius_control};
}}
QPushButton[buttonRole="primary"] {{
    background: {accent}; border: 1px solid {accent}; color: {accent_on};
    font-weight: 600;
}}
QPushButton[buttonRole="primary"]:hover {{ background: {focus_ring}; border-color: {focus_ring}; }}
QPushButton[buttonRole="primary"]:pressed {{ background: {accent}; }}
QPushButton[buttonRole="primary"]:focus {{ border-color: {text}; }}
QPushButton[buttonRole="danger"] {{ color: {danger}; }}
QPushButton[buttonRole="danger"]:hover {{ border-color: {danger}; }}
QPushButton[buttonRole="primary"]:disabled, QPushButton[buttonRole="danger"]:disabled {{
    color: {text_faint}; background: {surface}; border-color: {divider};
    font-weight: normal;
}}

/* **The scrollbars were never styled**, so every pane carried the chunky
   native ones with their stepper arrows - the single most dated element on
   screen and the one nobody mentions because it is everywhere. Thin, no
   steppers, and the handle only gains contrast under the pointer. */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
/* **As close to an overlay scrollbar as a stylesheet gets** (202626160950
   §1b): the bar keeps a 10px lane so nothing under it reflows, but the handle
   paints 4px wide inside it and fills the lane only under the pointer. It is
   not a true overlay - Qt Widgets would need a custom widget for that - and
   the order says so rather than claiming otherwise. */
QScrollBar::handle:vertical {{
    background: {scroll}; border-radius: 2px; min-height: 28px;
    margin: 2px 3px 2px 3px;
}}
QScrollBar::handle:horizontal {{
    background: {scroll}; border-radius: 2px; min-width: 28px;
    margin: 3px 2px 3px 2px;
}}
QScrollBar::handle:vertical:hover {{ margin: 2px 1px 2px 1px; border-radius: 4px; }}
QScrollBar::handle:horizontal:hover {{ margin: 1px 2px 1px 2px; border-radius: 4px; }}
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
    border-radius: {radius_control}; padding: 4px 8px; color: {text_dim};
}}
QToolButton:hover {{ background: {surface_hover}; color: {text}; }}
QToolButton:pressed, QToolButton:checked {{
    background: {accent_soft}; color: {accent_text};
}}
QToolButton:focus {{ border-color: {focus_ring}; }}
/* Icon-only toggles on the Search toolbar (202626160950 §3e): a 28px square
   that fills when on. The label lives in the tooltip and accessible name. */
QToolButton[iconToggle="true"] {{ padding: 5px; min-width: 18px; min-height: 18px; }}

/* **The segmented control** (§3c): one flat strip, the chosen segment lifted
   to the surface colour. A QButtonGroup of checkable QToolButtons, so keyboard
   and screen readers get the radio semantics without a custom class. */
#segmented {{ background: {surface_alt}; border-radius: {radius_control}; }}
#segmented QToolButton {{
    border: none; border-radius: 6px; padding: 4px 11px; margin: 2px 1px;
    color: {text_dim}; font-size: {small};
}}
#segmented QToolButton:checked {{
    background: {surface}; color: {text}; font-weight: 600;
}}
#segmented QToolButton:focus {{ border: 1px solid {focus_ring}; }}

/* **Chips** (§3d): each typed operator drawn as a removable pill. The remove
   button is part of the chip, not a separate control, so one tab stop per
   filter. */
#chip {{
    background: {chip_bg}; color: {chip_text};
    border: none; border-radius: {radius_pill};
    padding: 3px 4px 3px 10px; font-size: {small};
}}
#chip:hover {{ background: {accent_soft}; }}
#chip:focus {{ border: 1px solid {focus_ring}; }}
#chipRemove {{
    background: transparent; border: none; border-radius: {radius_pill};
    color: {chip_text}; padding: 0 5px; font-weight: 600;
}}
#chipRemove:hover {{ background: {accent}; color: {accent_on}; }}

/* **The rail** (§2): navy down the left, one entry per page. Selection is a
   filled pill AND a heavier label, so it survives greyscale. */
#rail {{ background: {rail}; border: none; }}
#rail QToolButton {{
    color: {rail_text}; background: transparent; border: 1px solid transparent;
    border-radius: {radius_control}; padding: 7px 0 5px 0; font-size: {small};
}}
#rail QToolButton:hover {{ background: {rail_hover}; color: {rail_on_text}; }}
#rail QToolButton:checked {{
    background: {rail_on_bg}; color: {rail_on_text}; font-weight: 600;
}}
#rail QToolButton:focus {{ border-color: {rail_text}; }}
#railPill {{
    background: {rail_hover}; color: {rail_text}; border: none;
    border-radius: {radius_control}; padding: 6px 0; font-size: {small};
}}
#railPill:hover, #railPill[selected="true"] {{ background: {rail_on_bg}; color: {rail_on_text}; }}
#railPill:focus {{ border: 1px solid {rail_text}; }}
#railPillHeadline {{ color: {rail_on_text}; font-weight: 600; font-size: {small}; }}
#railPillDetail {{ color: {rail_text}; font-size: {small}; }}
#railPill QProgressBar {{
    background: {rail_on_bg}; border: none; border-radius: 2px;
    max-height: 3px; min-height: 3px;
}}
#railPill QProgressBar::chunk {{
    background: {kind_code}; border-radius: 2px; width: 6px; margin: 0;
}}

/* **The empty Search page** (§3a): the headline is the one use of the
   display size; everything under it is quiet. */
#emptyHeadline {{ font-size: {display}; font-weight: 600; color: {text}; }}
#emptyGreeting {{ color: {text_dim}; font-size: {body}; }}
#suggestion {{
    background: {surface}; color: {text_dim}; border: 1px solid {border};
    border-radius: {radius_pill}; padding: 5px 12px; font-size: {small};
}}
#suggestion:hover {{ color: {text}; border-color: {border_strong}; }}
#suggestion:focus {{ border-color: {focus_ring}; }}
#recentHeading {{
    color: {text_faint}; font-size: {small}; font-weight: 600;
    letter-spacing: 1px;
}}

/* **The inspector** (§5): the preview pane's facts header. Labels faint,
   values plain; a hairline under the block. */
#inspectorFacts {{ border-bottom: 1px solid {divider}; }}
#factLabel {{ color: {text_faint}; font-size: {small}; }}
#factValue {{ color: {text}; font-size: {small}; }}

/* **Toasts** (§6): inverted ground, one line, bottom-centre. The dot carries
   the level alongside the text, never instead of it. */
#toast {{
    background: {toast_bg}; color: {toast_text};
    border: none; border-radius: {radius_control}; padding: 8px 14px;
    font-size: {small};
}}
/* The label is a child of the toast, so it needs the toast's text colour said
   outright: the `QWidget` rule at the top gives it the page's, which on an
   inverted ground is dark on dark in one theme and light on light in the other. */
#toastText {{ color: {toast_text}; background: transparent; font-size: {small}; }}
#toastDot {{ border-radius: 4px; min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; }}
/* Info uses the text colour, not the accent: the accent is navy in the light
   theme and the toast is navy, so the dot vanished. */
#toastDot[level="info"] {{ background: {toast_text}; }}
#toastDot[level="warning"] {{ background: {warning}; }}
#toastDot[level="danger"] {{ background: {danger}; }}

/* The menu bar (§7a): flat, in the window colour, so on Windows it reads as
   part of the title area rather than as a grey strip from 2009. On macOS Qt
   moves it to the system bar and this rule never paints. */
QMenuBar {{ background: {window}; color: {text_dim}; border: none; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 8px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {surface_hover}; color: {text}; }}

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

QProgressBar {{
    background: {surface_alt}; border: 1px solid {border};
    border-radius: {radius_input}; text-align: center; color: {text};
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

/* **A group is a card** (202626160950 §1b, §8a). The title sits inside the
   card's top edge rather than breaking its border, which is what turns a
   "section" into a "card" without moving a single control - the whole of
   the Settings and Indexing restyle is this rule and the tokens. */
QGroupBox {{
    border: 1px solid {border}; border-radius: {radius_box};
    margin-top: 6px; padding-top: 22px; background: {surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 12px; top: 12px; padding: 0 2px;
    color: {text_dim}; font-size: {small}; font-weight: 600;
    background: {surface};
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
    border-radius: {radius_control}; padding: 4px 8px; color: {text};
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
    border: 1px solid {border}; border-radius: {radius_control};
    padding: 6px 10px; font-size: {small};
}}

/* **CategoryNav** (§8b) takes the rail's selection language - a filled pill
   and a heavier label - so the two navigations read as one family. */
/* `outline: 0` drops the native black focus rectangle, which was drawn round the
   text alone with the icon outside it. The row gets the theme's own ring when
   the list has the keyboard instead, so where focus is stays visible. */
#categorySidebar {{ border: none; background: transparent; outline: 0; }}
#categorySidebar::item {{
    padding: 6px 10px; border: 2px solid transparent; border-radius: {radius_control};
}}
#categorySidebar::item:selected:focus {{ border: 2px solid {focus_ring}; }}
#categorySidebar::item:selected {{
    background: {accent_soft}; color: {accent_text}; font-weight: 600;
}}

/* `index_stats` sets one of these two on every value it shows, and only
   `statValue` had a rule - so a figure the code had decided was worth warning
   about rendered identically to one that was fine. The warning was computed,
   assigned, and invisible. Weight as well as colour, because colour alone is
   not a signal everybody receives. */
#statValue {{ color: {text}; font-weight: 600; }}
#statWarn {{ color: {warning}; font-weight: 700; }}
#statLabel {{ color: {text_faint}; font-size: {small}; }}

/* **Chat** (order 202626270611 section 3). Bubbles are surfaces, not badges:
   the person's own question sits on the accent's soft ground, the answer on
   the ordinary surface, and nothing in either warns about itself. */
#chatBubbleUser {{
    background: {accent_soft}; color: {text};
    border: 1px solid {border}; border-radius: {radius_box};
}}
/* The assistant's words sit on the page itself, as they do in any chat: no box round
   them (2026-09-20). Its quiet row of actions sits underneath. */
#chatBubbleAnswer {{
    background: transparent; color: {text};
    border: none;
}}
#chatAction {{
    background: transparent; color: {text_faint}; border: none;
    padding: 2px 8px; border-radius: {radius_control}; font-size: {small};
}}
#chatAction:hover {{ background: {surface_alt}; color: {text}; }}
#chatWebPrompt {{
    background: {accent_soft}; color: {text};
    border: 1px solid {border}; border-radius: {radius_control};
}}
#chatWebChip {{
    border: 1px solid {border}; border-radius: {radius_pill};
    padding: 2px 10px; color: {text_faint}; background: transparent;
}}
#chatWebChip:checked {{ background: {accent_soft}; color: {text}; border-color: {text_faint}; }}
#chatNarration, #chatFooter {{ color: {text_faint}; font-size: {small}; }}
#chatEmpty, #chatShelfEmpty, #chatSourcesEmpty {{ color: {text_faint}; }}
#chatSourcesHeading {{ font-weight: 600; }}
#chatNotice {{
    background: {accent_soft}; color: {text};
    border: 1px solid {border}; border-radius: {radius_control};
    padding: 6px 10px;
}}
#chatPassage {{
    background: {surface_alt}; color: {text};
    border: 1px solid {border}; border-radius: {radius_control};
    padding: 6px 10px;
}}
#chatChip {{
    background: {chip_bg}; color: {chip_text};
    border-radius: {radius_pill};
}}
#chatChip QToolButton {{ color: {chip_text}; }}
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
SCALE: dict = {"small": 1.0, "body": 13.0 / 12.0, "large": 15.0 / 12.0,
               # One more step (202626160950 §1e), for the single headline
               # on the empty Search page and nothing else. 26px at the
               # default machine; it follows "Make text bigger" like the rest.
               "display": 26.0 / 12.0}

#: **Corner radii, as a scale rather than a number per rule.** The redesign
#: (202626160950 §1a) uses four: inputs keep the 5px they had, controls and
#: rows take 8, the search box and cards take 10, chips are pills. Named so
#: the sheet says which it means; kept out of `PALETTES` because those are
#: colours and a test says so.
RADIUS: dict[str, str] = {
    "radius_input": "5px",
    "radius_control": "8px",
    "radius_box": "10px",
    # **Not 999px: Qt draws no rounding at all when the radius is more than
    # half the widget's height** - it does not clamp the way a browser does.
    # 999px therefore gave every "pill" square corners: the suggested
    # searches, the filter chips, the chat chips (seen in the 2026-09-27
    # review, order 0x section 9, and checked on plain buttons: 13px rounds a
    # 26px button, 14px leaves it square). The smallest pill measured is 22px
    # tall, so 11px is the most that rounds every one of them; taller ones get
    # softly rounded ends rather than full semicircles, which is still a pill
    # to the eye. Qt stylesheets cannot say "half the height".
    "radius_pill": "11px",
}

#: **Every action button's size, in one place** (the button system,
#: `widgets/buttons.py`). 4px above and below, and a floor of 18px inside that
#: - room for the 16px icon - makes a 28px button with its 1px border, the
#: height the old plain buttons were at the Windows default font. 10px before
#: the icon and 12px after the words: the icon's own transparent margin makes
#: the two sides look equal. Sizes, not colours, so kept out of `PALETTES`.
BUTTON: dict[str, str] = {
    "button_pad_y": "4px",
    "button_pad_left": "10px",
    "button_pad_right": "12px",
    "button_min_h": "18px",
}

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
    return _TEMPLATE.format(**colours, **font_sizes(base_pt), **RADIUS, **BUTTON)
