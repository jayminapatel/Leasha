r"""Which operating system is this? Asked in one place, answered the same way.

Layer: L0 (part of `app.core.osbridge`)

**Why a function and not a constant.** It would be shorter to write
`IS_WINDOWS = sys.platform == "win32"` once at import time. But then a test
that wants to see what the Mac branch does - by setting `sys.platform` to
`"darwin"` for the length of one test with pytest's `monkeypatch` - would get
nothing, because the constant was fixed before the test started. Reading
`sys.platform` every time we are asked costs a dictionary lookup, and it means
every macOS branch in this package can be run on the Linux machine the tests
use, which is the whole point of work order 0x §1d.

**`sys.platform`, not `os.name`.** `os.name` says `"posix"` for both macOS and
Linux, which cannot tell the two apart. `sys.platform` says `"win32"` (on every
Windows, 64-bit included - the name is historical), `"darwin"` on macOS (Darwin
is the name of the core of macOS) and `"linux"` on Linux.

One exception is kept on purpose: `app/extract/converter.py` has always asked
`os.name == "nt"` through its own `_is_windows()`, and its tests replace that
function. It still does, so those tests and that behaviour are unchanged.
"""

from __future__ import annotations

import sys

__all__ = ["is_windows", "is_macos"]


def is_windows() -> bool:
    """True on Windows. The platform everything here was first written for."""
    return sys.platform == "win32"


def is_macos() -> bool:
    """True on macOS (Apple calls the underlying system "Darwin")."""
    return sys.platform == "darwin"
