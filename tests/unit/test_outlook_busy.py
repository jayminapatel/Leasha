"""Outlook saying "busy" is waited out, and is never a bug in Leasha.

2026-10-05, the owner's run of twenty `.pst` archives through Outlook: three
were skipped as "An unexpected error occurred ... This is a bug" in the first
eight seconds, each on `com_error (-2147418111, 'Call was rejected by callee.')`
from the first call a reader makes. These fail on the code before the fix.

No Outlook is needed: the session is built without its constructor and given a
namespace that refuses a set number of calls. **UNVERIFIED against a real
Outlook** - that needs the owner's machine and a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import email_pst


class Rejected(Exception):
    """Shaped like `pywintypes.com_error`: the HRESULT is the first argument."""

    def __init__(self, hresult: int = -2147418111) -> None:
        super().__init__(hresult, "Call was rejected by callee.", None, None)


class _Store:
    def __init__(self, path: str) -> None:
        self.FilePath = path
        self.DisplayName = Path(path).stem

    def GetRootFolder(self):                       # noqa: N802 - Outlook's name
        return object()


class _Namespace:
    """Refuses the first `refusals` calls, then answers."""

    def __init__(self, refusals: int, stores=()) -> None:
        self.refusals = refusals
        self._stores = list(stores)
        self.added: list[str] = []
        self.calls = 0

    def _maybe_refuse(self) -> None:
        self.calls += 1
        if self.refusals > 0:
            self.refusals -= 1
            raise Rejected()

    @property
    def Stores(self):                              # noqa: N802 - Outlook's name
        self._maybe_refuse()
        return list(self._stores)

    def AddStore(self, path: str) -> None:         # noqa: N802 - Outlook's name
        self._maybe_refuse()
        self.added.append(path)

    def RemoveStore(self, _root) -> None:          # noqa: N802 - Outlook's name
        pass


def _session(namespace: _Namespace) -> email_pst.Win32ComSession:
    session = email_pst.Win32ComSession.__new__(email_pst.Win32ComSession)
    session._namespace = namespace
    session._attached = []
    session._com_initialised = False
    return session


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr("time.sleep", waited.append)
    return waited


def test_a_refusal_that_passes_is_waited_out(_no_waiting):
    namespace = _Namespace(refusals=3)
    session = _session(namespace)
    session.attach(Path("D:/mail/2026.pst"))
    assert namespace.added == [str(Path("D:/mail/2026.pst"))]
    assert _no_waiting == list(email_pst.OUTLOOK_BUSY_WAITS_S[:3])


def test_a_refusal_that_never_passes_is_a_lock_the_next_run_retries():
    namespace = _Namespace(refusals=10_000)
    session = _session(namespace)
    with pytest.raises(AppErrorException) as raised:
        session.attach(Path("D:/mail/2026.pst"))
    error = raised.value.error
    assert error.code == "ERR_FILE_LOCKED", "the one code a later run reads again by itself"
    assert "Outlook was busy" in error.suggestion
    assert "rejected" in (error.details or "").lower()
    assert namespace.calls == len(email_pst.OUTLOOK_BUSY_WAITS_S) + 1


def test_tidying_up_never_replaces_the_error_the_read_ended_on():
    namespace = _Namespace(refusals=10_000)
    session = _session(namespace)
    session._attached = ["d:/mail/2026.pst"]
    session.close()                                # raised `com_error` before
    assert session._attached == []


def test_anything_else_is_raised_at_once_and_only_busy_counts_as_busy(_no_waiting):
    assert email_pst.is_outlook_busy(Rejected()) is True
    assert email_pst.is_outlook_busy(Rejected(-2147417846)) is True
    assert email_pst.is_outlook_busy(Rejected(-2147221233)) is False
    assert email_pst.is_outlook_busy(ValueError("no")) is False
    calls = []

    def broken():
        calls.append(1)
        raise ValueError("not Outlook being busy")

    with pytest.raises(ValueError):
        email_pst.when_outlook_answers(broken)
    assert calls == [1] and _no_waiting == []


def test_an_archive_outlook_already_has_is_not_added_twice():
    namespace = _Namespace(refusals=1, stores=[_Store("D:\\mail\\2026.pst")])
    session = _session(namespace)
    session.attach(Path("D:\\mail\\2026.pst"))
    assert namespace.added == []
