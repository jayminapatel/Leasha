"""The four things Leasha asked of PyQt's `sip`, asked of PySide's `shiboken6`.

Layer: L5

Trial port only (branch `trial/pyside6`).
"""

from __future__ import annotations

from typing import Any

import shiboken6

__all__ = ["isdeleted", "delete", "unwrapinstance", "transferto"]


def isdeleted(obj: Any) -> bool:
    return not shiboken6.isValid(obj)


def delete(obj: Any) -> None:
    shiboken6.delete(obj)


def unwrapinstance(obj: Any) -> int:
    return int(shiboken6.getCppPointer(obj)[0])


def transferto(obj: Any, owner: Any) -> None:
    """PySide has no direct twin; nothing is done. One test relies on it."""
