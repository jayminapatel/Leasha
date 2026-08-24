"""Layer 0: the error contract.

Every error must carry a code, a plain-English message, a suggestion and an
action type. Nothing may escape a worker boundary as a bare traceback.
"""

from __future__ import annotations

import pytest

from app.core.errors import (
    ERROR_REGISTRY,
    ActionType,
    AppError,
    AppErrorException,
    guard,
    make_error,
    raise_error,
    to_app_error,
)

# The minimum set required by LOCAL_KNOWLEDGE_GRAPH_V2.md's recovery table.
SPEC_CODES = [
    "ERR_MODEL_LOAD",
    "ERR_OLLAMA_DOWN",
    "ERR_FILE_CORRUPT",
    "ERR_FILE_LOCKED",
    "ERR_DISK_SPACE",
    "ERR_DB_LOCKED",
    "ERR_OUTLOOK_MISSING",
    "ERR_ENCODING",
]


@pytest.mark.parametrize("code", SPEC_CODES)
def test_spec_required_codes_are_registered(code: str) -> None:
    assert code in ERROR_REGISTRY, f"{code} is required by the spec's recovery table"


@pytest.mark.parametrize("code", sorted(ERROR_REGISTRY))
def test_every_registered_error_states_problem_and_fix(code: str) -> None:
    """The whole contract in one assertion: what happened, and what to do."""
    spec = ERROR_REGISTRY[code]
    assert spec.message.strip(), f"{code} has no message"
    assert spec.suggestion.strip(), f"{code} has no suggestion"
    assert isinstance(spec.action_type, ActionType)


def test_make_error_fills_templates() -> None:
    err = make_error("ERR_FILE_CORRUPT", "indexer.pdf", path="archive_2024.pst")
    assert "archive_2024.pst" in err.message
    assert err.action_type is ActionType.SKIP_CONTINUE
    assert err.code == "ERR_FILE_CORRUPT"


def test_missing_template_value_does_not_raise() -> None:
    """An error path must never fail because of a missing context key."""
    err = make_error("ERR_FILE_CORRUPT", "indexer.pdf")  # no `path` supplied
    assert isinstance(err, AppError)
    assert "{path}" in err.message  # left intact rather than exploding


def test_unregistered_code_degrades_instead_of_raising() -> None:
    err = make_error("ERR_TOTALLY_MADE_UP", "somewhere")
    assert err.code == "ERR_TOTALLY_MADE_UP"
    assert err.suggestion
    assert "Unregistered error code" in (err.details or "")


def test_skip_continue_errors_are_not_fatal() -> None:
    """A corrupt file must never halt a 100GB run."""
    assert not make_error("ERR_FILE_CORRUPT", "x", path="a.pdf").is_fatal
    assert not make_error("ERR_FILE_LOCKED", "x", path="a.xlsx").is_fatal
    assert make_error("ERR_DISK_SPACE", "x", free_gb=1, drive="C:").is_fatal


def test_guard_converts_unexpected_exception() -> None:
    with pytest.raises(AppErrorException) as caught:
        with guard("indexer.pdf", path="broken.pdf"):
            raise ValueError("some library exploded")

    err = caught.value.error
    assert err.code == "ERR_UNEXPECTED"
    assert err.component == "indexer.pdf"
    assert "ValueError" in (err.details or "")
    assert "some library exploded" in (err.details or "")
    assert err.suggestion


def test_guard_passes_app_errors_through_unchanged() -> None:
    """A precise error must not be flattened into a generic one."""
    with pytest.raises(AppErrorException) as caught:
        with guard("indexer.pdf"):
            raise_error("ERR_FILE_CORRUPT", "indexer.pdf", path="a.pdf")

    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_guard_allows_success() -> None:
    with guard("anything"):
        result = 1 + 1
    assert result == 2


def test_to_app_error_preserves_traceback() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        err = to_app_error(exc, "core.test")
    assert "RuntimeError: boom" in (err.details or "")
    assert "Traceback" in (err.details or "")


def test_render_includes_fix() -> None:
    text = make_error("ERR_DB_LOCKED", "core.single_instance").render()
    assert "ERR_DB_LOCKED" in text
    assert "FIX:" in text


def test_app_error_is_serialisable() -> None:
    """The UI and the JSON CLI both need this."""
    err = make_error("ERR_DISK_SPACE", "index.pipeline", free_gb=3, drive="D:")
    payload = err.model_dump(mode="json")
    assert payload["code"] == "ERR_DISK_SPACE"
    assert payload["action_type"] == "USER_RETRY"
    assert AppError(**payload).message == err.message
