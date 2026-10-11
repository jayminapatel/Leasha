"""Gmail over its own API: labels stay labels, changes come from `history`.

Layer: L7

Order 1k 5a. Implements `app.accounts.provider.MailProvider` over a `Transport`
(it never opens a socket) and **only reads**: every request is a GET, and no
method here has a verb that could change a mailbox (rule 10; the guard test is
`tests/unit/test_mail_readonly.py`).

**First fetch.** `users.getProfile` is read first to capture the mailbox's
`historyId` *before* anything is listed, then `messages.list` pages through the
mailbox (bounded by `since` as `q=after:<epoch>`), each id fetched with
`messages.get?format=raw`. Anything that arrives while the list is running has a
history record after the captured id, so the first incremental pull picks it up;
a message seen twice is harmless because the engine keys on `remote_id`.

**Incremental.** `history.list?startHistoryId=...` with the four record types the
index cares about. HTTP 404 means Google has dropped that history (it keeps about
a week): `TokenExpired`, and the engine starts a full pull.

**Tokens** are JSON, written with sorted keys:
  `{"v":1,"phase":"list","history_id":H,"since":S|null,"li":N,"page_token":P|null}`
    while a first fetch is part-way (`li` indexes the label being listed);
  `{"v":1,"phase":"history","history_id":H[,"page_token":P]}` afterwards.
Every page's token resumes exactly after that page (rule 4).

**Labels.** `RemoteMessage.labels` carries label *names* (system ids such as
`INBOX` become `Inbox`, `CATEGORY_PROMOTIONS` becomes `Promotions`) and `folder`
is always "": a Gmail message is in many places at once and copying it as a folder
would index it once per label. `UNREAD` is a state, so it sets `seen`, not a label.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote, urlencode

from app.accounts.provider import (HttpResponse, MailPage, RemoteMessage, RemoteMessageGone,
                                   TokenExpired, TokenSource, Transport)
from app.accounts.providers._mailcommon import (b64url_decode, bearer_headers, dump_token,
                                                fail_for_status, load_token)
from app.core.logging import logger

__all__ = ["GmailApiMailProvider", "GMAIL_BASE"]

_log = logger.bind(component="accounts.gmail_api")

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_COMPONENT = "accounts.gmail_api"

#: System label ids have no display name of their own (labels.list returns the
#: id as the name), so the friendly ones are fixed here. UNREAD is absent on
#: purpose: it is a state and sets `RemoteMessage.seen`.
_SYSTEM_NAMES = {
    "INBOX": "Inbox", "SENT": "Sent", "DRAFT": "Drafts", "SPAM": "Spam", "TRASH": "Trash",
    "IMPORTANT": "Important", "STARRED": "Starred", "CHAT": "Chat",
    "CATEGORY_PERSONAL": "Personal", "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotions", "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}
_STATE_ONLY = frozenset({"UNREAD"})
_SPAM_TRASH = frozenset({"SPAM", "TRASH"})
_HISTORY_TYPES = ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]


class GmailApiMailProvider:
    """Read-only mail from one Gmail account through the Gmail API."""

    def __init__(self, transport: Transport, token_source: TokenSource, *,
                 account_name: str = "Gmail", page_size: int = 100,
                 base_url: str = GMAIL_BASE, pace_s: float = 0.05,
                 sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._token_source = token_source
        self._account = account_name
        # messages.list allows 500; 100 keeps one page's worth of raw messages
        # (and the work lost to a crash, rule 4) small.
        self._page_size = max(1, min(int(page_size), 500))
        self._base = base_url.rstrip("/")
        # Each messages.get costs 5 quota units against 250/s per user; a short
        # pause between gets keeps a first fetch of a big mailbox well inside it
        # and leaves room for the contact sync that shares the user's quota.
        self._pace_s = max(0.0, pace_s)
        self._sleep = sleep

    # -- public --------------------------------------------------------------

    def list_folders(self) -> list[str]:
        """Label names, friendly form: what the folder picker offers."""
        _by_id, names = self._labels()
        return sorted(set(names), key=str.lower)

    def pull(self, token: Optional[str], *, folders: Optional[list[str]] = None,
             since: Optional[float] = None, include_spam_trash: bool = False) -> Iterator[MailPage]:
        try:
            state = load_token(token)
            if state is not None and not (state.get("v") == 1 and state.get("phase") in ("list", "history")):
                raise ValueError("unknown token")
        except ValueError as exc:
            raise TokenExpired(str(exc)) from exc
        by_id, _names = self._labels()
        if state is None or state["phase"] == "list":
            yield from self._list_phase(state, by_id, folders, since, include_spam_trash)
        else:
            yield from self._history_phase(state, by_id, folders, include_spam_trash)

    def close(self) -> None:
        """Nothing is held open: every call is a single request."""

    # -- HTTP ----------------------------------------------------------------

    def _get(self, path: str, params: Optional[list[tuple[str, Any]]] = None, *, what: str,
             allow_404: bool = False) -> Optional[Any]:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urlencode(params)
        resp: HttpResponse = self._transport.request(
            "GET", url, headers=bearer_headers(self._token_source()))
        if resp.status == 404 and allow_404:
            return None
        if resp.status != 200:
            fail_for_status(resp, account=self._account, component=_COMPONENT, what=what)
        return resp.json() or {}

    # -- labels --------------------------------------------------------------

    def _labels(self) -> tuple[dict[str, str], list[str]]:
        data = self._get("/users/me/labels", what="labels.list") or {}
        by_id: dict[str, str] = {}
        for label in data.get("labels", []):
            lid = label.get("id", "")
            if lid in _STATE_ONLY or not lid:
                continue
            by_id[lid] = _SYSTEM_NAMES.get(lid) or label.get("name") or lid
        return by_id, list(by_id.values())

    @staticmethod
    def _names_for(label_ids: list[str], by_id: dict[str, str]) -> list[str]:
        return [by_id.get(i) or _SYSTEM_NAMES.get(i) or i for i in label_ids if i not in _STATE_ONLY]

    @staticmethod
    def _ids_for_folders(folders: Optional[list[str]], by_id: dict[str, str]) -> Optional[list[str]]:
        """Label ids for the chosen names, case-insensitively; None when no choice was made."""
        if not folders:
            return None
        wanted = {f.lower() for f in folders}
        return [lid for lid, name in by_id.items() if name.lower() in wanted or lid.lower() in wanted]

    # -- messages ------------------------------------------------------------

    def _fetch(self, mid: str, by_id: dict[str, str]) -> Optional[tuple[RemoteMessage, list[str]]]:
        """One message as `RemoteMessage`, plus its raw label ids; None if it is gone."""
        if self._pace_s:
            self._sleep(self._pace_s)
        data = self._get(f"/users/me/messages/{quote(mid, safe='')}", [("format", "raw")],
                         what="messages.get", allow_404=True)
        if data is None:
            return None
        ids = list(data.get("labelIds", []))
        try:
            internal = float(data.get("internalDate", 0)) / 1000.0
        except (TypeError, ValueError):
            internal = 0.0
        message = RemoteMessage(
            remote_id=data.get("id", mid),
            raw=b64url_decode(data.get("raw", "")),
            folder="",
            labels=self._names_for(ids, by_id),
            received_at=internal,
            seen="UNREAD" not in ids,
            version=str(data.get("historyId", "")),
        )
        return message, ids

    # -- first fetch ---------------------------------------------------------

    def _list_phase(self, state: Optional[dict], by_id: dict[str, str], folders: Optional[list[str]],
                    since: Optional[float], include_spam_trash: bool) -> Iterator[MailPage]:
        total_hint: Optional[int] = None
        if state is None:
            profile = self._get("/users/me/profile", what="users.getProfile") or {}
            history_id = str(profile.get("historyId", ""))
            total = profile.get("messagesTotal")
            total_hint = int(total) if isinstance(total, int) else None
            state = {"v": 1, "phase": "list", "history_id": history_id,
                     "since": int(since) if since else None, "li": 0, "page_token": None}
        history_id = state["history_id"]
        window = state.get("since")
        chosen = self._ids_for_folders(folders, by_id)
        targets: list[Optional[str]] = [None] if chosen is None else chosen
        index = int(state.get("li", 0))
        page_token = state.get("page_token")
        seen_ids: set[str] = set()

        while index < len(targets):
            target = targets[index]
            params: list[tuple[str, Any]] = [("maxResults", self._page_size)]
            if target is not None:
                params.append(("labelIds", target))
            if window:
                params.append(("q", f"after:{int(window)}"))
            if include_spam_trash or target in _SPAM_TRASH:
                # The API leaves spam and trash out unless told; asking for the
                # SPAM label alone would otherwise return nothing.
                params.append(("includeSpamTrash", "true"))
            if page_token:
                params.append(("pageToken", page_token))
            data = self._get("/users/me/messages", params, what="messages.list") or {}

            changes: list[Any] = []
            for item in data.get("messages", []):
                mid = item.get("id", "")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                got = self._fetch(mid, by_id)
                if got is not None:             # deleted since it was listed: nothing to index
                    changes.append(got[0])

            page_token = data.get("nextPageToken")
            if not page_token:
                index += 1
            last = index >= len(targets)
            if last:
                token = dump_token({"v": 1, "phase": "history", "history_id": history_id})
            else:
                token = dump_token({"v": 1, "phase": "list", "history_id": history_id, "since": window,
                                    "li": index, "page_token": page_token})
            yield MailPage(changes, token, done=last, total_hint=total_hint)
            total_hint = None

        if not targets:                          # chosen names matched no label
            yield MailPage([], dump_token({"v": 1, "phase": "history", "history_id": history_id}), done=True)

    # -- incremental ---------------------------------------------------------

    def _history_phase(self, state: dict, by_id: dict[str, str], folders: Optional[list[str]],
                       include_spam_trash: bool) -> Iterator[MailPage]:
        start = str(state["history_id"])
        page_token = state.get("page_token")
        wanted = self._ids_for_folders(folders, by_id)
        while True:
            params: list[tuple[str, Any]] = [("startHistoryId", start)]
            params += [("historyTypes", t) for t in _HISTORY_TYPES]
            params.append(("maxResults", self._page_size))
            if page_token:
                params.append(("pageToken", page_token))
            data = self._get("/users/me/history", params, what="history.list", allow_404=True)
            if data is None:
                raise TokenExpired("Gmail no longer holds the history after that point")

            # Last event per message wins: added-then-deleted must not be fetched,
            # a label change after an add needs only the one fetch.
            verdict: dict[str, str] = {}
            for record in data.get("history", []):
                for key, outcome in (("messagesAdded", "fetch"), ("labelsAdded", "fetch"),
                                     ("labelsRemoved", "fetch"), ("messagesDeleted", "gone")):
                    for entry in record.get(key, []):
                        mid = (entry.get("message") or {}).get("id")
                        if mid:
                            verdict[mid] = outcome

            changes: list[Any] = []
            for mid, outcome in verdict.items():
                if outcome == "gone":
                    changes.append(RemoteMessageGone(mid))
                    continue
                got = self._fetch(mid, by_id)
                if got is None:
                    changes.append(RemoteMessageGone(mid))
                    continue
                message, ids = got
                # A message that went to Spam/Trash, or lost the chosen labels, has
                # left the part of the mailbox the person asked for (5e).
                in_bin = bool(_SPAM_TRASH & set(ids)) and not include_spam_trash
                outside = wanted is not None and not (set(wanted) & set(ids))
                changes.append(RemoteMessageGone(mid) if in_bin or outside else message)

            next_page = data.get("nextPageToken")
            if next_page:
                token = dump_token({"v": 1, "phase": "history", "history_id": start, "page_token": next_page})
                yield MailPage(changes, token, done=False)
                page_token = next_page
                continue
            newest = str(data.get("historyId") or start)
            yield MailPage(changes, dump_token({"v": 1, "phase": "history", "history_id": newest}), done=True)
            return
