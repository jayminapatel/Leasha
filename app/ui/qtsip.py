"""The four things Leasha asked of PyQt's `sip`, asked of PySide's `shiboken6`.

Layer: L5

Trial port only (branch `trial/pyside6`).

2026-10-08 note: PySide6 is the shipped toolkit since order 202626270238
(5 October 2026); this module is production code, kept under the name the
call sites use.
"""

from __future__ import annotations

from typing import Any

import shiboken6

__all__ = ["isdeleted", "delete", "unwrapinstance", "transferto", "open_menu"]


def isdeleted(obj: Any) -> bool:
    """True once Qt has destroyed `obj`'s C++ half - the test before touching a
    widget a signal may deliver to late.
    """
    return not shiboken6.isValid(obj)


def delete(obj: Any) -> None:
    """Destroy `obj`'s C++ half now, as `sip.delete` did."""
    shiboken6.delete(obj)


def unwrapinstance(obj: Any) -> int:
    """The C++ pointer as an int, for native calls that want a handle."""
    return int(shiboken6.getCppPointer(obj)[0])


def transferto(obj: Any, owner: Any) -> None:
    """PyQt's "C++ owns this now, whatever Python does". **PySide6 has no
    such call**, so this refuses rather than quietly doing nothing: a no-op
    let the one test that uses it pass without setting up what it tests."""
    raise NotImplementedError("PySide6 cannot hand a wrapper's ownership to C++")


def open_menu(menu: Any, point: Any) -> Any:
    """Pop `menu` up at the global `point` and return the action chosen.

    2026-10-07. Menus were opened as `type(menu).exec(menu, point)` so a test
    could stand in for `QMenu.exec` (PySide6 does not look a replaced class
    attribute up through an instance). **Under PySide6 6.11 that unbound call
    is refused** - `exec` has static overloads, so `(QMenu, QPoint)` matches
    none of them and raises `TypeError` before anything is shown. Every
    right-click menu and the View button did nothing, 128 times in one day's
    log. A stand-in is a plain Python function and is called as before; the
    real thing is called on the instance, which is the only form Qt accepts.
    """
    import types

    stand_in = getattr(type(menu), "exec", None)
    if isinstance(stand_in, (types.FunctionType, types.MethodType)):
        return stand_in(menu, point)
    return menu.exec(point)
