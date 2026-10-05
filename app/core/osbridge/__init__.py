r"""One home for everything that depends on which operating system Leasha runs on.

Layer: L0

**Why this package exists.** Leasha was written for Windows, and Windows-only
calls - `os.startfile`, `explorer /select,`, `ctypes.WinDLL("kernel32")`,
`C:\Program Files\...` lookups - had spread into a dozen modules. Work order 0x
makes the code ready for a Mac as well, and the simplest rule that keeps it
that way is: **every Windows-only call lives here, and nowhere else.** Each
operation below has a Windows version and a macOS/POSIX version side by side,
so anybody reading one can see the other, and a test
(`tests/unit/test_osbridge_guard.py`) fails if a Windows-only call turns up
anywhere else in `app/`.

**Windows is platform one.** The Windows versions were *moved* here from where
they lived, not rewritten: the same calls, the same flags, the same fall-backs
and the same log lines. The old modules (`app/core/priority.py`,
`app/core/winfs.py`, `app/core/media_open.py`, `app/extract/converter.py`,
`app/ui/editors.py`) keep every name they had and hand the work on to this
package, so nothing that calls them had to change.

**macOS is platform two.** Its versions are the best reading of Apple's
documented behaviour, but none of them has been run on a real Mac yet. Each is
marked **(UNCONFIRMED on macOS)** where it is written, and listed for checking
on real hardware. Linux keeps whatever it did before; only developers run
Leasha there.

The operations, and the module each lives in:

    launch.py     open a file with its default app; show it in Explorer/Finder
    priority.py   run a thread or the whole process at lower priority
    programs.py   find an installed program that is not on PATH
    paths.py      the default data folder; how to name the venv's Python
    pathnames.py  which separator a path uses; whether a folder's disk
                  treats `Report.docx` and `report.docx` as one file
    cloudfs.py    is this file a cloud placeholder (OneDrive / iCloud)?
    dirwatch.py   be told when something changes under a folder (imported
                  where it is used, not from this package's top level)

**Cheap to import.** Only the standard library and the app's logger are used,
and anything Windows-only (`ctypes.WinDLL`) is imported inside the function
that needs it, so importing this package can never fail on a Mac and never
slows the window's start-up.
"""

from __future__ import annotations

from app.core.osbridge._platform import is_macos, is_windows
from app.core.osbridge.cloudfs import is_cloud_placeholder
from app.core.osbridge.launch import (
    hidden_console_flags, new_console_flags, open_with_default_app,
    show_in_file_manager,
)
from app.core.osbridge.pathnames import (
    case_sensitive,
    join_under,
    name_of,
    path_key,
    same_path,
    separator_for,
)
from app.core.osbridge.paths import (
    default_data_folder,
    venv_pip_display,
    venv_python_display,
)
from app.core.osbridge.priority import (
    lower_process_priority,
    lower_this_thread,
    restore_this_thread,
)

__all__ = [
    "is_windows", "is_macos",
    "open_with_default_app", "show_in_file_manager", "hidden_console_flags",
    "new_console_flags",
    "lower_this_thread", "restore_this_thread", "lower_process_priority",
    "default_data_folder", "venv_python_display", "venv_pip_display",
    "is_cloud_placeholder",
    "separator_for", "join_under", "case_sensitive", "path_key", "same_path",
]
