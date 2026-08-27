r"""`leasha://` — opening Leasha on a search from anywhere else. Adoptions §7a.

Layer: L0 — the parsing and the registry values are pure; the hand-off is one
`index_state` write.

**What it is for.** A shortcut on the desktop, a link in a note, a line in
somebody's own script: `leasha://search?q=safety%20report` opens Leasha with
that search already run. It is also how a saved search leaves the
application - `saved:invoices` in a URL is a one-click standing question.

**The hard part is not the scheme, it is the second copy.** `SingleInstance`
deliberately *refuses* a second window rather than talking to the first: two
copies cannot share one index, and that is the right answer for a
double-clicked shortcut. But it is the wrong answer for a link, because the
person is not asking for a second window, they are asking the one they have
to look something up. Answering "another copy is already running" to a link
is the failure this whole module exists to avoid.

So the second process does not become a window. It **writes the request into
`index_state` and exits**, and the running window picks it up. That is not a
new mechanism: `run_lock.request_stop` already passes an instruction between
two processes through exactly this table, for exactly this reason - *"a flag
rather than a signal because the two processes share nothing else"*.

**Per-user registry, never per-machine.** `HKCU\Software\Classes\leasha`
needs no administrator, is removed cleanly when the user is removed, and
matches where this application already puts everything else about itself.
A per-machine key would need elevation on install and would be left behind
by an uninstall that ran as somebody else.
"""

from __future__ import annotations

import shlex
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

__all__ = [
    "SCHEME",
    "PENDING_KEY",
    "Request",
    "parse",
    "build",
    "registry_values",
    "registry_key",
    "open_command",
    "register",
    "unregister",
    "handover",
    "take_pending",
]

#: The scheme itself. Lowercase, because Windows folds it and a mixed-case
#: registry key would look like a second scheme to anybody reading the tree.
SCHEME = "leasha"

#: Where a request waits for the running window. One `index_state` key, the
#: same channel `run_lock` uses to say "please stop" across processes.
PENDING_KEY = "ui:pending_link"

#: The registry key a per-user scheme lives under.
registry_key = rf"Software\Classes\{SCHEME}"

#: What a `leasha://` URL is allowed to ask for.
#:
#: **One action, and that is a decision.** A URL scheme is an *input from
#: outside the application* - anything on this machine can invoke it, and a
#: link in a document is not a trusted instruction. Search is safe: the worst
#: a hostile link can do is run a search the person can see and did not want.
#: Anything that indexed a folder, changed a setting or opened a file would
#: be an instruction from a stranger, so those are not offered and adding one
#: later is a decision to be argued for on its own.
ACTIONS = ("search",)

#: Longest query accepted from a link. Long enough for any real question,
#: short enough that a link cannot paste a megabyte into the search box.
MAX_QUERY = 500


class Request:
    """What a `leasha://` URL asked for. `action` is always in `ACTIONS`."""

    __slots__ = ("action", "query", "scope")

    def __init__(self, action: str, query: str, scope: str = "") -> None:
        self.action = action
        self.query = query
        self.scope = scope

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Request) and other.action == self.action
                and other.query == self.query and other.scope == self.scope)

    def __repr__(self) -> str:
        return f"Request({self.action!r}, {self.query!r}, {self.scope!r})"


def parse(url: Any) -> Optional[Request]:
    r"""A `leasha://` URL as a `Request`, or None. **Never raises.**

    None for anything that is not one of ours, and for one of ours asking for
    something not in `ACTIONS` - the two are the same answer on purpose,
    because "this link is not for Leasha" and "this link asks Leasha for
    something it does not do" both mean *do nothing*.

    >>> parse("leasha://search?q=safety+report").query
    'safety report'
    """
    text = str(url or "").strip().strip('"')
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except Exception:                            # noqa: BLE001 - see docstring
        return None
    if parts.scheme.lower() != SCHEME:
        return None

    # **`netloc` *or* `path`, because Windows hands over both shapes.**
    # `leasha://search?q=x` puts `search` in the netloc; `leasha:search?q=x` -
    # which a browser address bar will happily produce - puts it in the path.
    # Treating one as the only form is how a scheme works from a shortcut and
    # not from the place people actually paste links.
    action = (parts.netloc or parts.path.lstrip("/")).strip().lower()
    if action not in ACTIONS:
        return None

    values = dict(parse_qsl(parts.query, keep_blank_values=True))
    query = unquote(str(values.get("q", ""))).strip()[:MAX_QUERY]
    if not query:
        return None
    scope = str(values.get("scope", "")).strip().lower()[:20]
    return Request(action, query, scope)


def build(query: Any, scope: str = "") -> str:
    r"""The URL for a search, ready to paste into a shortcut.

    >>> build("safety report")
    'leasha://search?q=safety%20report'
    """
    from urllib.parse import quote

    text = str(query or "").strip()[:MAX_QUERY]
    if not text:
        return ""
    url = f"{SCHEME}://search?q={quote(text, safe='')}"
    return f"{url}&scope={quote(scope, safe='')}" if scope else url


def registry_values(command: Any) -> dict[str, str]:
    r"""The four values that register the scheme, keyed by sub-path.

    Returned as data rather than written here, so what the installer writes
    can be read in a test on a machine with no registry at all - which is
    every machine this suite runs on except the owner's.

    `URL Protocol` is the value whose *presence* makes Windows treat the key
    as a scheme; it is empty by design and is not a mistake.
    """
    text = str(command or "").strip()
    return {
        "": f"URL:{SCHEME} search",
        "URL Protocol": "",
        r"shell\open\command": f'{text} "%1"' if text else "",
    }


def open_command(executable: Any) -> str:
    r"""The command Windows runs for a `leasha://` link.

    Quoted, because the installer's own default path -
    `%LOCALAPPDATA%\Programs\Leasha` - has no space in it but a person who
    moved it will have one, and an unquoted command breaks at the first.
    """
    path = str(executable or "").strip().strip('"')
    return f'"{path}" open' if path else ""


def register(executable: Any) -> bool:
    r"""Write the per-user scheme into the registry. False off Windows.

    **`HKEY_CURRENT_USER`, never `HKEY_LOCAL_MACHINE`.** The per-user key
    needs no administrator, goes away when the profile does, and sits beside
    everything else this application owns - the index defaults to
    `%LOCALAPPDATA%\Leasha` for the same reasons. A per-machine key would
    need elevation to install and would be left behind by an uninstall run as
    somebody else.

    Never raises: a scheme that could not be registered is a link that does
    not work, and it must not be an install that failed.
    """
    values = registry_values(open_command(executable))
    if not values.get(r"shell\open\command"):
        return False
    try:
        import winreg                                        # Windows only
    except ImportError:
        return False
    try:
        for sub, value in values.items():
            path = rf"{registry_key}\{sub}" if sub and "\\" in sub else registry_key
            name = "" if "\\" in sub or not sub else sub
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except OSError:
        return False
    return True


def unregister() -> bool:
    r"""Remove the scheme. False if it was not there, or off Windows.

    **The other half, written at the same time as the first.** A scheme left
    behind by an uninstall points at an executable that is gone, so every
    `leasha://` link on the machine fails with a Windows error naming a path
    nobody recognises - and nothing on the machine says where it came from.
    """
    try:
        import winreg                                        # Windows only
    except ImportError:
        return False
    removed = False
    # Deepest first: `DeleteKey` refuses a key that still has children.
    for sub in (r"shell\open\command", r"shell\open", "shell", ""):
        path = rf"{registry_key}\{sub}" if sub else registry_key
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            removed = True
        except OSError:
            continue
    return removed


def handover(store: Any, request: Any) -> bool:
    """Leave a request for the running window. True if it was written.

    **Never raises.** This runs in a process that is about to exit, and a
    link that cannot be delivered should print a sentence rather than a
    traceback into somebody's browser.
    """
    found = request if isinstance(request, Request) else parse(request)
    if store is None or found is None:
        return False
    try:
        store.set_state(PENDING_KEY, _encode(found))
    except Exception:                            # noqa: BLE001 - see docstring
        return False
    return True


def take_pending(store: Any) -> Optional[Request]:
    """The waiting request, if there is one, **and clear it**.

    Taken rather than read: a request left in place would be re-run on every
    poll, so the window would keep replacing whatever somebody typed next
    with the same search. `run_lock.stop_requested` has the same shape and
    the same reason.

    Never raises: this runs on a timer beside a live window.
    """
    if store is None:
        return None
    try:
        raw = store.get_state(PENDING_KEY)
        if not raw:
            return None
        store.set_state(PENDING_KEY, "")
        return _decode(str(raw))
    except Exception:                            # noqa: BLE001 - see docstring
        return None


def _encode(request: Request) -> str:
    """A request as one line. `shlex` so a query with spaces survives."""
    return shlex.join([request.action, request.query, request.scope])


def _decode(text: str) -> Optional[Request]:
    """One line back into a request, or None if it is not one."""
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if len(parts) < 2 or parts[0] not in ACTIONS or not parts[1].strip():
        return None
    return Request(parts[0], parts[1][:MAX_QUERY],
                   parts[2] if len(parts) > 2 else "")
