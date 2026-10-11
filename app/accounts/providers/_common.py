"""Small helpers the three contact connectors share.

Layer: L7

Order 1k 4a-4e. Only what would otherwise be copied three times: turning an
HTTP failure into the registered `AppError`, reading an RFC 3339 time, a token
that is JSON in a string, and reading XML without ever expanding an entity.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

from app.accounts.provider import HttpResponse
from app.core.errors import AppErrorException, make_error

__all__ = ["fail", "check_ok", "parse_time", "encode_token", "decode_token", "parse_xml",
           "error_text"]


def error_text(resp: HttpResponse) -> str:
    """A short, safe description of a failed response (never the whole body)."""
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return " ".join(str(err.get(k, "")) for k in ("status", "code", "message")).strip()[:300]
        if err:
            return str(err)[:300]
    return resp.text()[:200].strip()


def fail(resp: HttpResponse, account: str, component: str, what: str) -> AppErrorException:
    """The error for a status the connector has no special meaning for.

    401 is a sign-in that no longer works; 429 and 503 are the provider asking
    for patience (the real Transport has already waited and retried, so seeing
    one here means it gave up); anything else is a refusal worth showing."""
    text = error_text(resp)
    if resp.status == 401:
        code = "ERR_ACCOUNT_SIGNIN_EXPIRED"
    elif resp.status in (429, 503):
        code = "ERR_PROVIDER_LIMIT"
    else:
        code = "ERR_ACCOUNT_REJECTED"
    return AppErrorException(make_error(
        code, component, account=account, reason=f"{what}: HTTP {resp.status}", details=text))


def check_ok(resp: HttpResponse, account: str, component: str, what: str) -> HttpResponse:
    if 200 <= resp.status < 300:
        return resp
    raise fail(resp, account, component, what)


def parse_time(text: Any) -> float:
    """Seconds since the epoch for an RFC 3339 / ISO time, 0.0 when unreadable."""
    if not text or not isinstance(text, str):
        return 0.0
    s = text.strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)               # fromisoformat takes 6 fraction digits
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def encode_token(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def decode_token(token: Optional[str]) -> Optional[dict[str, Any]]:
    """The state in a token, `{}` for None, and None for text that is not ours
    (the caller then raises `TokenExpired`, so the engine does a full pull
    rather than trusting a token it cannot read)."""
    if not token:
        return {}
    try:
        data = json.loads(token)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_xml(body: bytes) -> ET.Element:
    """Parse a server's XML without ever expanding an entity.

    A DOCTYPE is the only way to declare an entity, and no CardDAV or WebDAV
    server needs one, so any body that has one is refused outright. That is
    simpler and safer than a parser setting: the standard-library parser
    expands internal entities (the "billion laughs" shape) by default."""
    low = body.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise AppErrorException(make_error(
            "ERR_ACCOUNT_REJECTED", "accounts.providers", account="the server",
            reason="the reply carried a DOCTYPE, which Leasha refuses",
            details="DOCTYPE in XML reply"))
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise AppErrorException(make_error(
            "ERR_ACCOUNT_REJECTED", "accounts.providers", account="the server",
            reason="the reply was not valid XML", details=str(exc)[:200])) from exc
