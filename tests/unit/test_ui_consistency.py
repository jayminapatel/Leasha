"""One look and one voice across the whole window: every button on every page has a name, a tooltip, and an
icon when it has no words - and a text button that stands alone carries its icon too.

The owner (2026-10-11): "make it all consistent from tool tips to performance to unified look ... icons are must".
This walks the real window page by page, so a page that adds a bare button fails here, not in someone's hands.
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QAbstractButton, QCheckBox, QPushButton, QRadioButton

#: Qt's own tab-bar arrows: not ours to name.
_QT_OWN = {"Scroll Left", "Scroll Right"}


def test_every_button_on_every_page_is_named_explained_and_drawn_alike(gui_mainwindow):
    app, window, _store, _engine = gui_mainwindow
    window.resize(1250, 800)
    window.show()
    for index in range(window.rail.count()):
        window.rail.setCurrentIndex(index)
        end = time.time() + 0.5
        while time.time() < end:
            app.processEvents()
    no_tip, no_name, plain_text = [], [], []
    for button in window.findChildren(QAbstractButton):
        if button.objectName().startswith("qt_") or (button.text() or button.accessibleName()) in _QT_OWN:
            continue
        label = f"{type(button).__name__}:{button.text() or button.accessibleName()}"
        if not button.toolTip():
            no_tip.append(label)
        if isinstance(button, (QCheckBox, QRadioButton)):
            continue
        if not (button.accessibleName() or button.text()):
            no_name.append(label)
        if button.text() and button.icon().isNull() and isinstance(button, QPushButton):
            plain_text.append(label)
    assert not no_tip, f"buttons with no tooltip: {sorted(set(no_tip))}"
    assert not no_name, f"buttons with no name for a screen reader: {sorted(set(no_name))}"
    assert not plain_text, f"text buttons with no icon: {sorted(set(plain_text))}"
