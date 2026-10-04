"""Help > About Leasha: who made it, which build this is, what it is built on.

2026-10-04, from the brand assessment: the window carried the version on
the splash and nothing else - no entity, no notices - and a released product
needs both. One small dialog: the logo, the name and what it does in one
sentence, the version and build, Leasha Ltd and the web address, and a button
to the third-party notices (`docs/THIRD_PARTY_NOTICES.md`). Read-only; the
only thing it can do is open that file and close.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.version import build_info
from app.ui.theme import theme_colours
from app.ui.tray import assets_dir
from app.ui.widgets.buttons import style_button

__all__ = ["ENTITY", "WEBSITE", "AboutDialog", "lockup_path", "notices_path"]

ENTITY = "Leasha Ltd"
WEBSITE = "leasha.co.uk"
WHAT_IT_DOES = "Finds your files and mail from a plain description. Everything stays on this computer."


def notices_path() -> Path:
    """`docs/THIRD_PARTY_NOTICES.md`, beside `app/` from source and beside
    the executable in a build - the same two places the assets are."""
    return assets_dir().parent / "docs" / "THIRD_PARTY_NOTICES.md"


def lockup_path(colours: dict | None = None) -> Path:
    """The brand's lockup for this ground: `leasha-lockup.png` on a light
    window, `leasha-lockup-reversed.png` (white wordmark) on a dark one -
    the indigo wordmark vanished on the dark theme in the first render."""
    colours = colours if colours is not None else theme_colours()
    window = colours.get("window", "#ffffff").lstrip("#")
    r, g, b = (int(window[i:i + 2], 16) for i in (0, 2, 4))
    dark = (r * 299 + g * 587 + b * 114) // 1000 < 128
    return assets_dir() / ("leasha-lockup-reversed.png" if dark else "leasha-lockup.png")


def build_line(info: dict | None = None) -> str:
    """"Version 0.3.4 (v0.3.4-12-gabc1234)" - the git description only when
    there is one, so a built copy says the version alone."""
    info = info if info is not None else build_info()
    line = f"Version {info.get('version', '?')}"
    described = info.get("git")
    if described and described != info.get("version") and described != f"v{info.get('version')}":
        line += f" ({described})"
    return line


class AboutDialog(QDialog):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Leasha")
        self.setObjectName("aboutDialog")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 24, 36, 16)    # the brand's clear space: the L's height
        layout.setSpacing(8)

        self.logo = QLabel()
        self.logo.setObjectName("aboutLogo")
        self.logo.setAccessibleName("The Leasha logo")
        pixmap = QPixmap(str(lockup_path()))
        if not pixmap.isNull():
            self.logo.setPixmap(pixmap.scaledToWidth(
                220, Qt.TransformationMode.SmoothTransformation))
        else:
            self.logo.setText("Leasha")
        layout.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignHCenter)

        self.blurb = QLabel(WHAT_IT_DOES)
        self.blurb.setWordWrap(True)
        self.blurb.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.blurb)

        self.version = QLabel(build_line())
        self.version.setObjectName("aboutVersion")
        self.version.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.version)

        self.entity = QLabel(f'{ENTITY} · <a href="https://{WEBSITE}">{WEBSITE}</a>')
        self.entity.setObjectName("aboutEntity")
        self.entity.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.entity.setOpenExternalLinks(True)
        self.entity.setToolTip(f"Open {WEBSITE} in your browser")
        layout.addWidget(self.entity)

        row = QHBoxLayout()
        row.addStretch(1)
        self.notices = QPushButton("Third-party notices")
        self.notices.setToolTip("Open the list of the open-source software Leasha is built on, "
                                "and its licences")
        style_button(self.notices)
        self.notices.clicked.connect(self.open_notices)
        row.addWidget(self.notices)
        self.close_button = QPushButton("Close")
        self.close_button.setToolTip("Close this box")
        style_button(self.close_button)
        self.close_button.clicked.connect(self.accept)
        self.close_button.setDefault(True)
        row.addWidget(self.close_button)
        layout.addLayout(row)

    def open_notices(self) -> bool:
        """Open the notices file in whatever reads Markdown here. Returns
        whether there was a file to open; a build without it says so in the
        version line rather than failing silently."""
        path = notices_path()
        if not path.is_file():
            self.version.setText(f"{self.version.text()} - the notices file is not in this copy")
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
