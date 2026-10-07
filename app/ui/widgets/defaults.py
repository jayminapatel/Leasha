r"""Getting back to a setting's default, which was a one-way door.

Layer: L5

**`.env` always beats the default in `config.py`.** So the moment a setting is
written down it is pinned for ever, and no improved default can ever reach that
machine again. That is not a hypothetical: the installer wrote
`RERANK_MODEL=BAAI/bge-reranker-base` into every install, the code default was
later changed to a model measured **9.2x faster**, and not one machine got it.
The measurement was real, the fix shipped, and it reached nobody.

The backend half has existed since `75c48ae`: `env_writer.apply_values(path,
{"KEY": None})` removes a key, which is the only way to say *"use whatever the
code decides"*. Nothing in the window called it, so the operation existed and
did not.

**Two affordances, deliberately.**

*A button*, because a capability nobody can see is one nobody uses - the lesson
of the `/` menu, where every filter worked for a layer and a half before
anything said so. It names how many settings are pinned, so pressing it is not
a leap in the dark.

*A right-click on any control*, because "all of them" is usually not what
somebody wants. Each control already carries its registry key as its object
name - `test_settings_reachable.py` depends on that - so the menu can be
attached generically rather than a reset being wired into every box by hand,
and a setting added next month gets one without anybody remembering.

**`None`, never the default value.** Writing the default back re-pins it, which
looks identical on screen and is exactly the bug rather than the fix. It is
worth saying twice because the two calls differ by one character.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu, QPushButton, QWidget

from app.core.settings_registry import by_key, keys
from app.ui.qtsip import open_menu

__all__ = ["attach_resets", "restore_button", "pinned_in", "RESET_LABEL"]

RESET_LABEL = "Restore defaults"


def pinned_in(env_file: Any) -> list[str]:
    r"""Registry keys this `.env` pins **and that a reset may remove.**

    **`protected()` is why the second half of that sentence exists.** This
    returned every pinned key, and the restore button sent `{key: None}` for
    all of them - which deleted `DATA_PATH`, `VECTOR_PATH`, `FTS_DB`,
    `CACHE_PATH`, `MODEL_CACHE` and `STATE_PATH` in one press. Those have no
    usable default: `DATA_PATH` is declared `default=""`, so "restore the
    default" means "leave the index location blank", and `load_settings` then
    refuses to start before logging is configured to say why.

    The owner pressed it and the application never opened again. Never raises -
    see `env_writer`.
    """
    from pathlib import Path

    from app.core.env_writer import pinned_keys
    from app.core.settings_registry import protected

    if not env_file:
        return []
    guarded = protected()
    return [key for key in pinned_keys(Path(env_file), keys())
            if key not in guarded]


def _describe(key: str) -> str:
    """"Rerank results (default: on)" - the label somebody recognises, and what
    they would be going back to. A menu entry saying only "Reset" asks them to
    remember what the default was."""
    setting = by_key(key)
    if setting is None:
        return f"Reset {key}"
    default = setting.default
    if isinstance(default, bool):
        shown = "on" if default else "off"
    else:
        shown = str(default)
    return f"Reset “{setting.label}” to its default ({shown})"


def attach_resets(panel: QWidget, on_reset: Any) -> int:
    """Give every control in `panel` that names a registry key a reset menu.

    Returns how many were wired, so a caller can assert on it rather than hope.

    **Found by object name, not by a list.** Every control already carries its
    key that way for `test_settings_reachable.py`, so this stays correct as
    settings are added - and a control that forgets its name fails that test
    rather than quietly losing its reset.

    `on_reset` is called with `{key: None}`, ready for `apply_values`.
    """
    wired = 0
    known = keys()
    for child in panel.findChildren(QWidget):
        key = child.objectName()
        if key not in known:
            continue
        child.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # `k=key` and `w=child` bound now: a lambda closing over the loop
        # variable gives every control the last key, which is the kind of bug
        # that looks like the feature working until you try the second row.
        child.customContextMenuRequested.connect(
            lambda point, k=key, w=child: _show_menu(w, point, k, on_reset))
        wired += 1
    return wired


def _show_menu(widget: QWidget, point: Any, key: str, on_reset: Any) -> None:
    menu = QMenu(widget)
    action = menu.addAction(_describe(key))
    action.setToolTip(
        "Removes the line from .env so the application's own default applies "
        "again - including a better one that arrives in a future version.")
    chosen = open_menu(menu, widget.mapToGlobal(point))
    if chosen is action:
        # **`None` removes. The default *value* would re-pin it**, which looks
        # identical on screen and is the bug rather than the fix.
        on_reset({key: None})


def restore_button(parent: Optional[QWidget], env_file: Any,
                   on_reset: Any) -> QPushButton:
    """The visible affordance: restore every pinned setting at once.

    Disabled, with a reason, when nothing is pinned - rather than present and
    doing nothing, which is how somebody concludes the button is broken.
    """
    button = QPushButton(RESET_LABEL, parent)
    pinned = pinned_in(env_file)

    if pinned:
        button.setToolTip(
            f"{len(pinned)} setting{'s' if len(pinned) != 1 else ''} in this "
            f"install are pinned in .env: {', '.join(pinned)}.\n\n"
            "Restoring removes those lines, so the application's own defaults "
            "apply again - including better ones that arrive in a future "
            "version. Your index, folders and documents are untouched, and "
            "where things are stored is never changed by this.\n\n"
            "Right-click any single control to restore just that one."
        )
        button.clicked.connect(
            lambda _c=False: on_reset({key: None for key in pinned}))
    else:
        button.setEnabled(False)
        button.setToolTip(
            "Nothing is pinned: every setting is already using the "
            "application's own default.")
    return button
