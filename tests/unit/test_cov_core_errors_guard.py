"""The error contract at a worker's edge: `guard` and `to_app_error` beyond the basics.

Order 1u §4d. `test_errors.py` shows the plain case (an exception becomes `ERR_UNEXPECTED`, an `AppError`
passes through). These pin the rest of what `app.core.errors.guard` and `to_app_error` promise: the
component and the context ride along, a chosen code fills its message from the context, the cause stays
chained, an `AppErrorException` is the very same error whatever code is asked for, an interrupt is not
turned into an error, and the error path never raises an error of its own.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppErrorException, guard, make_error, to_app_error


def _raised(exc):
    """`exc` as it is met in an `except` block: raised, so it has a traceback."""
    try:
        raise exc
    except BaseException as caught:            # noqa: BLE001 - handed back to the test
        return caught


def test_an_unexpected_exception_becomes_err_unexpected_naming_its_component_and_keeping_its_context():
    with pytest.raises(AppErrorException) as caught:
        with guard("indexer.pdf", path="C:/docs/broken.pdf", page=3):
            raise ZeroDivisionError("division by zero")

    err = caught.value.error
    assert err.code == "ERR_UNEXPECTED"
    assert err.component == "indexer.pdf"
    assert "indexer.pdf" in err.message
    assert err.context == {"path": "C:/docs/broken.pdf", "page": 3}
    assert "ZeroDivisionError: division by zero" in err.details
    assert "Traceback" in err.details
    assert isinstance(caught.value.__cause__, ZeroDivisionError), "the original stays chained"


def test_a_chosen_code_fills_its_message_from_the_context():
    err = to_app_error(_raised(OSError("no route")), "accounts.engine", "ERR_SYNC_FAILED",
                       account="Home", reason="OSError: no route")

    assert err.code == "ERR_SYNC_FAILED"
    assert err.component == "accounts.engine"
    assert err.message == "Syncing Home failed (OSError: no route)."
    assert err.suggestion
    assert "OSError: no route" in err.details


def test_guard_passes_the_code_it_was_given():
    with pytest.raises(AppErrorException) as caught:
        with guard("accounts.engine", "ERR_SYNC_FAILED", account="Work", reason="x"):
            raise RuntimeError("down")

    assert caught.value.error.code == "ERR_SYNC_FAILED"
    assert caught.value.error.context == {"account": "Work", "reason": "x"}


def test_an_app_error_passes_through_as_the_same_object_whatever_code_is_asked_for():
    precise = make_error("ERR_FILE_CORRUPT", "indexer.pdf", path="a.pdf", details="xref table broken")
    exc = AppErrorException(precise)

    assert to_app_error(exc, "somewhere.else", "ERR_SYNC_FAILED", account="Home") is precise

    with pytest.raises(AppErrorException) as caught:
        with guard("somewhere.else", "ERR_SYNC_FAILED"):
            raise exc
    assert caught.value is exc
    assert caught.value.error.details == "xref table broken"
    assert caught.value.error.component == "indexer.pdf"


def test_an_interrupt_is_not_turned_into_an_error():
    with pytest.raises(KeyboardInterrupt):
        with guard("ui.index"):
            raise KeyboardInterrupt


@pytest.mark.parametrize("key", ["details", "suggestion", "action_payload"])
def test_a_context_key_named_like_an_error_field_never_makes_the_error_path_raise(key):
    """Found by this test, 2026-10-13: `to_app_error(exc, c, details=...)` (and so `guard(c, details=...)`)
    passed `details` to `make_error` twice and raised a TypeError in place of the error it was reporting.
    `suggestion` and `action_payload` already reached `make_error` once; they are pinned so they stay so."""
    with pytest.raises(AppErrorException) as caught:
        with guard("index.write", **{key: "what the caller knew", "path": "a.txt"}):
            raise ValueError("disk said no")

    err = caught.value.error
    assert err.code == "ERR_UNEXPECTED" and err.component == "index.write"
    assert "ValueError: disk said no" in err.details, "the traceback is never lost"
    assert err.context["path"] == "a.txt"


def test_details_the_caller_gives_are_kept_beside_the_traceback():
    err = to_app_error(_raised(ValueError("disk said no")), "index.write", details="while writing chunk 7")

    assert "while writing chunk 7" in err.details
    assert "ValueError: disk said no" in err.details


def test_a_suggestion_the_caller_gives_replaces_the_registry_one():
    err = to_app_error(_raised(ValueError("x")), "index.write", suggestion="Free some space and retry.")

    assert err.suggestion == "Free some space and retry."
