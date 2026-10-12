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
from PySide6.QtWidgets import QPushButton, QWidget

from app.core.settings_registry import by_key, keys

__all__ = ["attach_resets", "restore_button", "pinned_in", "show_default", "RESET_LABEL"]

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
    from app.core.settings_registry import protected

    wired = 0
    # Not the locations: `env_writer` refuses to remove them, so a menu offering
    # "Reset the index location" would be a reset that silently does nothing.
    known = set(keys()) - protected()
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
    """The right-click menu on one control. The reset sends `{key: None}` so
    the line is removed from `.env`, never the default written back.
    """
    from app.ui.widgets import menus

    menu = menus.build(widget, [menus.Item(
        _describe(key), icon="rotate-ccw",
        tip="Removes the line from .env so the application's own default applies "
            "again - including a better one that arrives in a future version.")])
    [action] = menu.actions()
    chosen = menus.show(menu, widget, point)
    if chosen is action:
        # **`None` removes. The default *value* would re-pin it**, which looks
        # identical on screen and is the bug rather than the fix.
        on_reset({key: None})
        # And the control shows the default now, not the old value until a restart.
        show_default(widget.parentWidget(), [key])


def show_default(panel: Optional[QWidget], keys_shown: Any) -> int:
    """Put each control in `panel` named by one of `keys_shown` back to its default.

    Returns how many changed. **Shown, not written**: signals are blocked, because
    a control that announced the change would write the default back and re-pin
    it. The line is already gone from `.env`; this is so the screen agrees, rather
    than showing the old value until the next start.

    There is no reload of the Settings page from `.env` - each box has its own
    `load` with its own shape - so this works the same way `attach_resets` does,
    by object name, from the registry's default. A control of a kind it does not
    know is left as it was.
    """
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QSpinBox

    from app.core.settings_registry import protected

    if panel is None:
        return 0
    # A location is never reset (`env_writer` refuses it), so it is never blanked.
    wanted = set(keys_shown) - protected()
    done = 0
    owners: dict[int, tuple[Any, set]] = {}
    for child in panel.findChildren(QWidget):
        setting = by_key(child.objectName()) if child.objectName() in wanted else None
        if setting is None:
            continue
        default = setting.default
        blocked = child.blockSignals(True)
        try:
            if isinstance(child, QAbstractButton) and child.isCheckable():
                child.setChecked(bool(default))
            elif isinstance(child, QSpinBox) and isinstance(default, (int, float)):
                child.setValue(int(default))
            elif isinstance(child, QComboBox):
                found = child.findData(default)
                if found < 0:
                    found = child.findText(str(default))
                if found < 0:
                    continue
                child.setCurrentIndex(found)
            elif isinstance(child, QLineEdit):
                child.setText("" if default is None else str(default))
            else:
                continue
            done += 1
            owner = child.parentWidget()
            while owner is not None and not callable(getattr(owner, "defaults_shown", None)):
                owner = owner.parentWidget()
            if owner is not None:
                owners.setdefault(id(owner), (owner, set()))[1].add(setting.key)
        finally:
            child.blockSignals(blocked)
    # The signals were blocked, so a box that keeps its own record of what it last sent, or greys one
    # control by another, never heard of the change: tell it (CodeRabbit on #68 - an untick after a
    # reset was taken for no change and not saved).
    for owner, keys in owners.values():
        owner.defaults_shown(sorted(keys))
    return done


def _say_pinned(button: QPushButton, pinned: list[str]) -> None:
    """The button's state and tooltip for what is pinned now."""
    button.setEnabled(bool(pinned))
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
    else:
        button.setToolTip(
            "Nothing is pinned: every setting is already using the "
            "application's own default.")


def restore_button(parent: Optional[QWidget], env_file: Any,
                   on_reset: Any) -> QPushButton:
    """The visible affordance: restore every pinned setting at once.

    Disabled, with a reason, when nothing is pinned - rather than present and
    doing nothing, which is how somebody concludes the button is broken.

    **It asks first, with the count.** One press changed every category at once
    with no question, and Enter on a focused button is a press. Cancel is the
    default.

    **The list is kept current, not read once.** It was computed when the page
    was built, so after a press the button still offered the settings it had
    just removed, and a setting changed afterwards was not offered at all.
    `.env` is written on a worker, so re-reading the file straight after would
    race it: the list is updated from what is sent instead - `note_written`,
    which the Settings page calls with every change.
    """
    from app.core.settings_registry import protected

    button = QPushButton(RESET_LABEL, parent)
    pinned = pinned_in(env_file)
    known, guarded = set(keys()), protected()

    def note_written(values: dict) -> None:
        for key, value in values.items():
            if value is None and key in pinned:
                pinned.remove(key)
            elif value is not None and key in known and key not in guarded \
                    and key not in pinned:
                pinned.append(key)
        _say_pinned(button, pinned)

    def press() -> None:
        from app.ui.widgets.confirm import confirm

        if not pinned:
            return
        count = f"{len(pinned)} setting{'s' if len(pinned) != 1 else ''}"
        if not confirm(
                button.window() if button.window() is not button else None,
                RESET_LABEL, f"Restore {count} to the application's own defaults?",
                RESET_LABEL, danger=False,
                informative=("They are: " + ", ".join(pinned) + ".\n\nYour index, "
                             "folders and documents are untouched, and where things "
                             "are stored is never changed by this.")):
            return
        removed = {key: None for key in pinned}
        on_reset(removed)
        show_default(parent, removed)
        note_written(removed)

    button.note_written = note_written          # type: ignore[attr-defined]
    button.clicked.connect(lambda _c=False: press())
    _say_pinned(button, pinned)
    return button
