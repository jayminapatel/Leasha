r"""Every splitter keeps the sizes it was dragged to, across restarts.

Layer: L5

**Why this exists** (owner, 2026-10-13, UI phase B). A page's divider - Files' list and preview,
Photos' tree, list and info, Tidy's list and pane, Chat's conversations, each page's side list -
went back to the width it was built with every time the app started. Only Chat's side panel
remembered its width, by code of its own.

**How.** `remember(split, key)` gives one splitter a memory; `remember_all(root, prefix)` gives one
to every splitter under a page or a window, so a splitter added later is covered without a line
of its own. The sizes are one keyed row, `ui:split:<key>`, written as every remembered view
choice is written - on the ordered state writer (`state_writes.save_state`), never on the UI
thread - and read back, once, the first time the splitter is shown with a size to share.

**Only a drag is remembered.** `splitterMoved` is emitted only when somebody drags a handle, not
when a page calls `setSizes` or the preview slides open (the trap the column widths fell into,
`view_options`: a size Qt arrived at is not one somebody chose). The write waits `SETTLE_MS` after
the last move, so one drag is one write.

**A hidden pane keeps its width.** A pane that is away (the preview off, the side list hidden)
has a size of 0; the width it had when last shown is kept, and on restore only the panes on
screen are given theirs, so putting a pane away and restarting never brings it back 0 wide.

`split.setProperty("rememberSizes", False)` leaves a splitter alone; the key is the splitter's
object name, or its place under `root` when it has none.
"""

from __future__ import annotations

import weakref
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QSplitter, QWidget

__all__ = ["KEY_PREFIX", "SETTLE_MS", "remember", "remember_all", "flush_all", "sizes_text",
           "sizes_from_text", "merged", "restored"]

KEY_PREFIX = "ui:split:"
#: Quiet after the last move before the sizes are written: one drag, one write.
SETTLE_MS = 400


def sizes_text(sizes: list[int]) -> str:
    return ",".join(str(max(0, int(s))) for s in sizes)


def sizes_from_text(text: Any) -> list[int]:
    """The saved sizes, or [] for anything that is not a list of whole numbers."""
    try:
        return [max(0, int(part)) for part in str(text or "").split(",") if part.strip()]
    except ValueError:
        return []


def merged(saved: list[int], now: list[int], shown: list[bool]) -> list[int]:
    """What to write after a drag: the sizes on screen, and a hidden pane's last width kept."""
    out = []
    for index, size in enumerate(now):
        keep = saved[index] if index < len(saved) else 0
        out.append(size if shown[index] and size > 0 else keep)
    return out


def restored(saved: list[int], now: list[int], shown: list[bool]) -> Optional[list[int]]:
    """The sizes to put back, or None when the saved row does not fit this splitter."""
    if len(saved) != len(now) or not any(saved[i] for i in range(len(now)) if shown[i]):
        return None
    return [saved[i] if shown[i] and saved[i] > 0 else now[i] for i in range(len(now))]


def _store_for(split: QSplitter, store: Any) -> Any:
    if store is not None:
        return store
    widget: Optional[QWidget] = split
    while widget is not None:                     # the page's, or the window's
        found = getattr(widget, "_store", None)
        if callable(found) and not hasattr(found, "get_state"):
            found = found()                                # Quick search keeps it behind a method
        if found is not None:
            return found
        widget = widget.parentWidget()
    return None


class _Memory(QObject):
    """One splitter's memory: restore on its first real show, save after a drag."""

    def __init__(self, split: QSplitter, key: str, store: Any) -> None:
        super().__init__(split)
        self._split = weakref.ref(split)
        self.key = KEY_PREFIX + key
        self._store = store
        self._restored = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SETTLE_MS)
        self._timer.timeout.connect(self.save)
        split.splitterMoved.connect(lambda _pos, _index: self._timer.start())
        split.installEventFilter(self)

    def _shown(self, split: QSplitter) -> list[bool]:
        return [not split.widget(i).isHidden() for i in range(split.count())]

    def _saved(self, split: QSplitter) -> list[int]:
        store = _store_for(split, self._store)
        try:
            return sizes_from_text(store.get_state(self.key, "")) if store is not None else []
        except Exception:                                  # noqa: BLE001 - a preference
            return []

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's name
        if not self._restored and event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, self, self.restore)       # once laid out
        return False

    def restore(self) -> None:
        split = self._split()
        if split is None or self._restored or sum(split.sizes()) <= 0:
            return                                         # not laid out yet: the next show
        self._restored = True
        sizes = restored(self._saved(split), split.sizes(), self._shown(split))
        if sizes is not None:
            split.setSizes(sizes)

    def flush(self) -> None:
        """Save now if a drag is still settling - the window is closing (review of #78)."""
        if self._timer.isActive():
            self._timer.stop()
            self.save()

    def save(self) -> None:
        from app.ui.state_writes import save_state

        split = self._split()
        if split is None:
            return
        self._restored = True                              # a drag outranks a late restore
        sizes = merged(self._saved(split), split.sizes(), self._shown(split))
        save_state(_store_for(split, self._store), self.key, sizes_text(sizes),
                   component="ui.splitter")


def remember(split: QSplitter, key: str, store: Any = None) -> Optional[_Memory]:
    """Give `split` a memory under `key`. Once per splitter; `store` is found from the page when
    not given."""
    if split.property("rememberSizes") is False or split.property("sizesRemembered"):
        return None
    split.setProperty("sizesRemembered", True)
    memory = _Memory(split, key, store)
    split._size_memory = memory                            # type: ignore[attr-defined]
    if split.isVisible():                                  # already on screen: no Show to wait for
        QTimer.singleShot(0, memory, memory.restore)
    return memory


def flush_all(root: QWidget) -> int:
    """Save every divider still settling after a drag under `root`; how many were saved. Called on
    close before the state writes are drained, so a drag in the last `SETTLE_MS` is not lost."""
    count = 0
    for split in root.findChildren(QSplitter):
        memory = getattr(split, "_size_memory", None)
        if memory is not None and memory._timer.isActive():
            memory.flush()
            count += 1
    return count


def remember_all(root: QWidget, prefix: str, store: Any = None) -> int:
    """Every splitter under `root` that has no memory yet. Returns how many were given one."""
    count, seen = 0, {}
    for split in root.findChildren(QSplitter):
        name = split.objectName() or f"{type(split.parentWidget()).__name__}Split"
        seen[name] = seen.get(name, 0) + 1
        key = f"{prefix}:{name}" if seen[name] == 1 else f"{prefix}:{name}.{seen[name]}"
        if remember(split, key, store) is not None:
            count += 1
    return count
