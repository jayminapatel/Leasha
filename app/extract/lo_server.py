r"""The half of the warm LibreOffice session that runs *inside* LibreOffice's own Python.

Layer: L2

**This file is not imported by the application.** `converter.py` starts it as a
child process using the `python.exe` that ships beside `soffice.exe`, because
that is the only interpreter on a Windows machine with LibreOffice's `uno`
bridge in it - the application's own venv has none, and adding one would be a
dependency that cannot be installed from a wheel. So this module may import only
the standard library and `uno`, and knows nothing about the rest of Leasha.

What it does is small. It starts one `soffice` with a **persistent profile** and a
private pipe, connects to it once, and then answers one JSON request per line on
standard input:

    {"id": 7, "in": "C:\\a\\old.ppt", "out": "C:\\tmp\\old.pptx", "filter": "pptx"}

with one JSON line on standard output:

    {"id": 7, "ok": true}            or            {"id": 7, "error": "why"}

Loading a document into a running LibreOffice costs a few hundred milliseconds; a
cold `soffice --convert-to` costs five to ten seconds, almost all of it start-up
and first-run profile creation. That difference is the entire reason this exists.

Everything a person could mind is closed off: documents open **read-only and
hidden**, macros never run, links are never updated, and a document that wants a
password simply fails to load (there is no interaction handler to ask one) and
is reported as an error rather than a dialog on somebody's desktop.

The parent decides when this is stuck and kills the whole process tree; nothing
here tries to be clever about hangs.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

#: Windows: never a console window for the child.
_NO_WINDOW = 0x08000000

#: Filter for each output kind `converter.py` asks for. Bare extensions in a
#: `--convert-to` are resolved by LibreOffice from the document type on the
#: command line; over the API the filter is named, so the table is here.
FILTERS = {
    "pptx": ("Impress MS PowerPoint 2007 XML", ""),
    "csv": ("Text - txt - csv (StarCalc)", "44,34,76,1"),
    "txt": ("Text (encoded)", "UTF8"),
    "docx": ("MS Word 2007 XML", ""),
    # Chosen per document type once it is loaded: see `_pdf_filter`.
    "pdf": ("", ""),
    "xlsx": ("Calc MS Excel 2007 XML", ""),
}


def _quiet() -> None:
    """No operating-system error box for this process or anything it starts.

    The parent already starts this helper in the same mode; setting it here too
    means the guarantee does not depend on the parent having remembered, and
    `soffice` - started next - inherits it.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.WinDLL("kernel32").SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:                                 # noqa: BLE001
            pass


def _start_soffice(soffice: str, profile_url: str, pipe: str) -> subprocess.Popen:
    return subprocess.Popen(
        [soffice, f"-env:UserInstallation={profile_url}",
         "--headless", "--invisible", "--nologo", "--norestore", "--nodefault",
         "--nolockcheck", "--nofirststartwizard",
         f"--accept=pipe,name={pipe};urp;StarOffice.ComponentContext"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW if os.name == "nt" else 0,
    )


def _connect(pipe: str, child: subprocess.Popen, give_up_s: float = 90.0):
    import uno

    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    url = f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext"
    deadline = time.monotonic() + give_up_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"soffice exited with code {child.returncode} before it was ready")
        try:
            context = resolver.resolve(url)
            return context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", context)
        except Exception as exc:                          # noqa: BLE001 - not up yet
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"soffice did not accept a connection: {last}")


def _properties(**values):
    import uno
    from com.sun.star.beans import PropertyValue

    made = []
    for name, value in values.items():
        prop = PropertyValue()
        prop.Name = name
        prop.Value = value
        made.append(prop)
    return tuple(made)


def _pdf_filter(document) -> str:
    """The PDF export filter for whatever kind of document this turned out to be."""
    for service, name in (("com.sun.star.text.TextDocument", "writer_pdf_Export"),
                          ("com.sun.star.sheet.SpreadsheetDocument", "calc_pdf_Export"),
                          ("com.sun.star.presentation.PresentationDocument", "impress_pdf_Export")):
        if document.supportsService(service):
            return name
    return "draw_pdf_Export"


def _convert(desktop, source: str, target: str, kind: str) -> None:
    import uno

    if kind not in FILTERS:
        raise ValueError(f"no filter known for '{kind}'")
    filter_name, filter_options = FILTERS[kind]
    document = desktop.loadComponentFromURL(
        uno.systemPathToFileUrl(source), "_blank", 0,
        _properties(Hidden=True, ReadOnly=True, UpdateDocMode=0,
                    MacroExecutionMode=0, AsTemplate=False))
    if document is None:
        raise RuntimeError("LibreOffice could not open the file (damaged, or password protected)")
    try:
        if kind == "pdf":
            filter_name = _pdf_filter(document)
        options = {"FilterName": filter_name}
        if filter_options:
            options["FilterOptions"] = filter_options
        document.storeToURL(uno.systemPathToFileUrl(target), _properties(**options))
    finally:
        try:
            document.close(True)
        except Exception:                                 # noqa: BLE001 - already gone
            try:
                document.dispose()
            except Exception:                             # noqa: BLE001
                pass


def _descendants(pid: int) -> list[int]:
    """Every process below `pid`, by the Windows process snapshot (no psutil here)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    children: dict[int, list[int]] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    found: list[int] = []
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def _terminate(pid: int) -> None:
    if os.name != "nt":
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0001, False, pid)                 # PROCESS_TERMINATE
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)


def _stop_child(child: subprocess.Popen) -> None:
    """Make sure LibreOffice - launcher *and* the `soffice.bin` under it - is gone."""
    try:
        for pid in _descendants(child.pid):
            _terminate(pid)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    except Exception:                                     # noqa: BLE001
        pass


def _say(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _watch_stdin(requests: "queue.Queue[str]", child: subprocess.Popen) -> None:
    """Read requests; when the parent goes away, take LibreOffice with us.

    **The parent-death watchdog.** Standard input is a pipe from the parent, and
    the operating system closes its write end when the parent dies - however it
    dies. End of input therefore means "nobody is listening", and it is acted on
    *at once*, even in the middle of a conversion, by killing LibreOffice and
    exiting. Without this a conversion in progress at the moment the parent
    was killed would run to completion (or forever) with nobody to receive it.
    (The parent also puts every child in a kill-on-close job object; this is the
    second, independent guarantee, and the only one on a platform without jobs.)
    """
    try:
        for line in sys.stdin:
            requests.put(line)
    except Exception:                                     # noqa: BLE001 - pipe broke: same thing
        pass
    _stop_child(child)
    os._exit(0)


def main(argv: list[str]) -> int:
    _quiet()
    soffice, profile_url = argv[1], argv[2]
    pipe = f"leasha_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    child = _start_soffice(soffice, profile_url, pipe)
    try:
        desktop = _connect(pipe, child)
    except Exception as exc:                              # noqa: BLE001
        _say({"fatal": str(exc)})
        _stop_child(child)
        return 2
    _say({"ready": True, "soffice_pid": child.pid})

    requests: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_watch_stdin, args=(requests, child), daemon=True).start()

    while True:
        line = requests.get().strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if request.get("quit"):
            break
        started = time.monotonic()
        try:
            _convert(desktop, request["in"], request["out"], request["filter"])
            _say({"id": request.get("id"), "ok": True,
                  "s": round(time.monotonic() - started, 3)})
        except Exception as exc:                          # noqa: BLE001 - one file, not the session
            message = f"{type(exc).__name__}: {exc}"[:400]
            _say({"id": request.get("id"), "error": message})
            # A dead connection is the session's, not the file's: LibreOffice
            # crashed (some filters do) and every later request would fail the
            # same way in a fraction of a second. Say so and stop, so the parent
            # starts a fresh one instead of blaming file after file.
            if "DisposedException" in message or "ConnectException" in message:
                _say({"fatal": "the connection to LibreOffice was lost"})
                _stop_child(child)
                return 4
        if child.poll() is not None:
            _say({"fatal": "soffice exited"})
            return 3

    try:
        desktop.terminate()
    except Exception:                                     # noqa: BLE001
        pass
    try:
        child.wait(timeout=10)
    except Exception:                                     # noqa: BLE001
        pass
    _stop_child(child)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
