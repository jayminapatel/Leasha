"""What the account connectors share: an HTTP failure as the right error, times, tokens and safe XML.

Order 1u section 4d. `app.accounts.providers._common` (contacts) and `_mailcommon` (mail) had no test that imported
them (static scan 2026-10-13). Every connector's error message, change token and XML reply goes through them.
"""

from __future__ import annotations

import json

import pytest

from app.accounts.provider import HttpResponse
from app.accounts.providers import _common, _mailcommon
from app.core.errors import AppErrorException


def _resp(status, body=b"", **headers):
    return HttpResponse(status, dict(headers), body if isinstance(body, bytes) else body.encode("utf-8"))


# ---------------------------------------------------------------- contacts: a failed answer

@pytest.mark.parametrize("status, code", [
    (401, "ERR_ACCOUNT_SIGNIN_EXPIRED"),
    (429, "ERR_PROVIDER_LIMIT"),
    (503, "ERR_PROVIDER_LIMIT"),
    (403, "ERR_ACCOUNT_REJECTED"),
    (500, "ERR_ACCOUNT_REJECTED"),
])
def test_a_contacts_failure_becomes_the_registered_error_for_its_status(status, code):
    exc = _common.fail(_resp(status, '{"error": {"code": 7, "message": "nope"}}'), "Home", "accounts.google",
                       "listing contacts")

    assert isinstance(exc, AppErrorException)
    err = exc.error
    assert err.code == code and err.component == "accounts.google"
    assert err.context["account"] == "Home"
    assert f"HTTP {status}" in err.context["reason"]
    assert err.details == "7 nope"


def test_check_ok_passes_a_success_and_raises_for_anything_else():
    ok = _resp(204)
    assert _common.check_ok(ok, "Home", "c", "w") is ok
    with pytest.raises(AppErrorException) as caught:
        _common.check_ok(_resp(302), "Home", "c", "w")
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_the_error_text_is_short_and_never_the_whole_body():
    assert _common.error_text(_resp(400, '{"error": "invalid_grant"}')) == "invalid_grant"
    assert _common.error_text(_resp(400, json.dumps({"error": {"message": "x" * 1000}}))) == "x" * 300
    assert _common.error_text(_resp(500, "<html>" + "y" * 1000)) == ("<html>" + "y" * 1000)[:200]
    assert _common.error_text(_resp(500, b"\xff\xfe not utf-8")).endswith("not utf-8")


@pytest.mark.parametrize("text, expected", [
    ("2026-10-13T10:00:00Z", 1_791_885_600.0),
    ("2026-10-13T10:00:00.1234567Z", 1_791_885_600.123456),     # seven fraction digits, as Graph writes
    ("2026-10-13T12:00:00+02:00", 1_791_885_600.0),
    ("2026-10-13T10:00:00", 1_791_885_600.0),                     # no zone is UTC
    ("not a time", 0.0),
    ("", 0.0),
    (None, 0.0),
    (12345, 0.0),
])
def test_contacts_times_are_read_or_zero(text, expected):
    assert _common.parse_time(text) == pytest.approx(expected)


def test_a_contacts_token_round_trips_and_one_that_is_not_ours_is_none():
    state = {"b": 2, "a": [1]}
    token = _common.encode_token(state)
    assert token == '{"a":[1],"b":2}'
    assert _common.decode_token(token) == state
    assert _common.decode_token(None) == {} and _common.decode_token("") == {}
    assert _common.decode_token("opaque-server-token") is None
    assert _common.decode_token("[1, 2]") is None


@pytest.mark.parametrize("body", [
    b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY lol "lol">]><a>&lol;</a>',
    b'<!doctype html><html/>',
    b'<a><!ENTITY x "y"></a>',
])
def test_xml_with_a_doctype_or_an_entity_is_refused_unread(body):
    with pytest.raises(AppErrorException) as caught:
        _common.parse_xml(body)
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_plain_xml_is_read_and_broken_xml_is_a_rejection_not_a_crash():
    root = _common.parse_xml(b'<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>')
    assert root.tag == "{DAV:}multistatus" and len(root) == 1
    with pytest.raises(AppErrorException) as caught:
        _common.parse_xml(b"<a><b></a>")
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_closing_a_transport_never_fails():
    class Breaks:
        def close(self):
            raise OSError("already closed")

    class Counts:
        closed = 0

        def close(self):
            Counts.closed += 1

    _common.close_transport(Breaks())
    _common.close_transport(object())
    _common.close_transport(Counts())
    assert Counts.closed == 1


# ---------------------------------------------------------------- mail: a failed answer

def _mail_code(status, body=""):
    with pytest.raises(AppErrorException) as caught:
        _mailcommon.fail_for_status(_resp(status, body), account="Work", component="accounts.graph",
                                    what="listing folders")
    return caught.value.error


@pytest.mark.parametrize("status, body, code", [
    (401, "", "ERR_ACCOUNT_SIGNIN_EXPIRED"),
    (403, '{"error": "AADSTS65001: consent required"}', "ERR_ACCOUNT_ADMIN_CONSENT"),
    (403, "Need admin approval: admin_consent", "ERR_ACCOUNT_ADMIN_CONSENT"),
    (403, "The admin has not given consent", "ERR_ACCOUNT_ADMIN_CONSENT"),
    (403, "userRateLimitExceeded", "ERR_PROVIDER_LIMIT"),
    (403, "Daily quota exhausted", "ERR_PROVIDER_LIMIT"),
    (403, "Forbidden", "ERR_ACCOUNT_REJECTED"),
    (429, "", "ERR_PROVIDER_LIMIT"),
    (503, "", "ERR_PROVIDER_LIMIT"),
    (404, "", "ERR_ACCOUNT_REJECTED"),
])
def test_a_mail_failure_becomes_the_registered_error_for_its_status_and_body(status, body, code):
    err = _mail_code(status, body)

    assert err.code == code and err.component == "accounts.graph"
    assert err.details.startswith(f"listing folders: HTTP {status}")


def test_a_mail_failure_keeps_at_most_500_characters_of_the_body():
    err = _mail_code(500, "z" * 2000)
    assert err.details == "listing folders: HTTP 500 " + "z" * 500


def test_bearer_headers_carry_the_token_and_any_extra():
    assert _mailcommon.bearer_headers("t0k") == {"Authorization": "Bearer t0k", "Accept": "application/json"}
    assert _mailcommon.bearer_headers("t", {"Prefer": "x"})["Prefer"] == "x"


@pytest.mark.parametrize("raw", [b"", b"a", b"ab", b"abc", b"\xfb\xff\xfe"])
def test_base64url_without_padding_is_read(raw):
    import base64

    unpadded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert _mailcommon.b64url_decode(unpadded) == raw


def test_a_mail_token_is_stable_and_one_that_is_not_ours_is_a_value_error():
    assert _mailcommon.dump_token({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert _mailcommon.load_token(_mailcommon.dump_token({"x": [1]})) == {"x": [1]}
    assert _mailcommon.load_token(None) is None and _mailcommon.load_token("") is None
    for bad in ("not json", "[1]", "3"):
        with pytest.raises(ValueError):
            _mailcommon.load_token(bad)


@pytest.mark.parametrize("text, expected", [
    ("2026-10-13T10:00:00.0000000Z", 1_791_885_600.0),
    ("2026-10-13T10:00:00+00:00", 1_791_885_600.0),
    ("2026-10-13T10:00:00", 1_791_885_600.0),
    ("garbage", 0.0),
    ("", 0.0),
])
def test_mail_times_are_read_or_zero(text, expected):
    assert _mailcommon.epoch_from_iso(text) == pytest.approx(expected)
