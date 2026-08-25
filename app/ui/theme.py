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

__all__ = ["Theme", "palette_for", "stylesheet", "detect_scheme", "SCHEMES"]

SCHEMES = ("system", "light", "dark")


class Theme:
    LIGHT = "light"
    DARK = "dark"


#: Both palettes, same keys. A token missing from one is a visible bug in that
#: theme only, which is exactly the sort of thing nobody notices for months - so
#: a test asserts the two have identical key sets.
PALETTES: dict[str, dict[str, str]] = {
    Theme.DARK: {
        "window": "#1e1f22",
        "surface": "#232428",
        "surface_alt": "#2b2d31",
        "border": "#3a3d42",
        "border_strong": "#4a4d52",
        "text": "#e6e6e6",
        "text_dim": "#a9b0b8",
        "text_faint": "#7d838b",
        "accent": "#5b9dd9",
        "accent_soft": "#33415c",
        "accent_bar": "#4a7fb5",
        "highlight": "#ffd479",
        "warning": "#d98b5b",
        "selection_text": "#ffffff",
    },
    Theme.LIGHT: {
        "window": "#f6f7f9",
        "surface": "#ffffff",
        "surface_alt": "#eef0f3",
        "border": "#d3d7dd",
        "border_strong": "#b9bfc7",
        "text": "#1c1f23",
        "text_dim": "#5a616a",
        "text_faint": "#868d96",
        "accent": "#1f6fb2",
        "accent_soft": "#d6e6f5",
        "accent_bar": "#2f80c9",
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
QWidget {{ background: {window}; color: {text}; font-size: 13px; }}

QLineEdit {{
    background: {surface}; border: 1px solid {border}; border-radius: 6px;
    padding: 8px; font-size: 15px; selection-background-color: {accent};
    selection-color: {selection_text};
}}
QLineEdit:focus {{ border-color: {accent}; }}

QListWidget, QTableWidget, QPlainTextEdit {{
    background: {surface}; border: 1px solid {border}; border-radius: 6px;
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {accent_soft}; color: {text};
}}
QHeaderView::section {{
    background: {surface_alt}; color: {text_dim};
    border: none; border-bottom: 1px solid {border}; padding: 6px;
}}

QPushButton {{
    background: {surface_alt}; border: 1px solid {border_strong};
    border-radius: 5px; padding: 6px 12px;
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:disabled {{ color: {text_faint}; border-color: {border}; }}

QProgressBar {{
    background: {surface_alt}; border: 1px solid {border};
    border-radius: 5px; text-align: center; color: {text};
}}
QProgressBar::chunk {{ background: {accent_bar}; border-radius: 4px; }}

QGroupBox {{
    border: 1px solid {border}; border-radius: 6px;
    margin-top: 10px; padding-top: 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {text_dim}; }}

QTabBar::tab {{
    background: transparent; padding: 8px 14px; color: {text_dim};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {text}; border-bottom-color: {accent}; }}
QTabWidget::pane {{ border: none; }}

QComboBox, QSpinBox, QTimeEdit {{
    background: {surface}; border: 1px solid {border};
    border-radius: 5px; padding: 4px 8px;
}}

#resultPath {{ font-weight: 600; color: {accent}; }}
/* The name is the headline now - a browser result, not a printed report. It
   reads as primary text rather than as a link, because it is the thing being
   named and not the thing being navigated to. */
#resultName {{ font-weight: 600; color: {text}; font-size: 15px; }}
/* A short text tag rather than an icon font: text survives dark mode, high-DPI
   and a missing font file, none of which is worth paying for yet. */
#resultKind {{
    color: {text_faint}; font-size: 10px; font-weight: 700;
    border: 1px solid {text_faint}; border-radius: 3px;
    padding: 1px 4px; margin-right: 6px;
}}
#resultMeta {{ color: {text_faint}; font-size: 11px; }}
#resultMissing {{ color: {warning}; font-size: 11px; }}
#resultSnippet {{ color: {text}; }}
#resultSnippet b {{ color: {highlight}; font-weight: 700; }}
#searchStatus, #resultsSummary, #indexDetail {{ color: {text_faint}; font-size: 11px; }}
#indexHeadline, #graphHeadline {{ font-size: 15px; font-weight: 600; }}
#indexTotals {{ color: {text_dim}; font-size: 12px; }}
#skipHeading {{ font-weight: 600; }}
#skipFix {{ color: {text_dim}; }}
#skipExamples {{ color: {text_faint}; font-size: 11px; }}
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


def stylesheet(preference: str = "system", *, detected: Optional[str] = None) -> str:
    """The full Qt stylesheet for a preference."""
    return _TEMPLATE.format(**palette_for(preference, detected=detected))
