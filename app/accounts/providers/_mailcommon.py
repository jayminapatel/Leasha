"""Small helpers the three mail connectors share.

Layer: L7

Kept in one private module so the HTTP connectors (Gmail, Graph) turn a bad status
into the same `AppError` the same way, and so the IMAP connector and the HTTP ones
agree on how a "folder:validity:uid"-style id is built. Nothing here touches a
network or a mailbox.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.accounts.provider import HttpResponse, is_server_trouble
from app.core.errors import raise_error

__all__ = ["fail_for_status", "bearer_headers", "b64url_decode", "dump_token",
           "load_token", "epoch_from_iso"]


def bearer_headers(token: str, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def b64url_decode(text: str) -> bytes:
    """Gmail sends `raw` as base64url without padding; `base64` insists on it."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def dump_token(state: dict[str, Any]) -> str:
    # sort_keys so a token is byte-for-byte stable: the engine compares them to
    # notice "nothing moved" and a test can assert one.
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def load_token(token: Optional[str]) -> Optional[dict[str, Any]]:
    """None for no token. A token this code did not write is `ValueError`; the
    caller turns that into `TokenExpired`, which makes the engine start over."""
    if not token:
        return None
    try:
        state = json.loads(token)
    except ValueError as exc:
        raise ValueError("unreadable change token") from exc
    if not isinstance(state, dict):
        raise ValueError("unreadable change token")
    return state


_FRACTION = re.compile(r"(\.\d{6})\d+")


def epoch_from_iso(text: str) -> float:
    """An ISO-8601 time as Unix seconds; 0.0 when absent or unreadable.

    Graph writes seven fractional digits ("...00.0000000Z"), which older
    `fromisoformat` rejects, so the fraction is cut to microseconds first.
    """
    if not text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(_FRACTION.sub(r"\1", text.replace("Z", "+00:00")))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def fail_for_status(resp: HttpResponse, *, account: str, component: str, what: str) -> None:
    """Raise the `AppError` for a non-success HTTP answer (rule 2: say what to do).

    The Transport already retried 429/503 with `Retry-After`, so one that reaches
    here means the provider is still asking us to wait: `ERR_PROVIDER_LIMIT`, which
    the engine backs off from rather than counts as a failure of the account.
    """
    status = resp.status
    body = resp.text()[:500]
    detail = f"{what}: HTTP {status} {body}".strip()
    if status == 401:
        raise_error("ERR_ACCOUNT_SIGNIN_EXPIRED", component, account=account, details=detail)
    if status == 403:
        lowered = body.lower()
        if "aadsts65001" in lowered or "admin_consent" in lowered or ("consent" in lowered and "admin" in lowered):
            raise_error("ERR_ACCOUNT_ADMIN_CONSENT", component, account=account, details=detail)
        if "ratelimit" in lowered or "quota" in lowered:
            raise_error("ERR_PROVIDER_LIMIT", component, account=account,
                        reason=f"HTTP {status}", details=detail)
        raise_error("ERR_ACCOUNT_REJECTED", component, account=account,
                    reason=f"HTTP {status}", details=detail)
    if status in (429, 503):
        raise_error("ERR_PROVIDER_LIMIT", component, account=account,
                    reason=f"HTTP {status}", details=detail)
    if is_server_trouble(resp):                           # the provider's own trouble (review CS-7)
        raise_error("ERR_ACCOUNT_TROUBLE", component, account=account,
                    reason=f"HTTP {status}", details=detail)
    raise_error("ERR_ACCOUNT_REJECTED", component, account=account,
                reason=f"HTTP {status}", details=detail)
