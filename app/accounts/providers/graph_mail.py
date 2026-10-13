"""Microsoft 365 and Outlook.com mail over Microsoft Graph, one delta per folder.

Layer: L7

Order 1k 5b. Implements `app.accounts.provider.MailProvider` over a `Transport`
and **only reads**: every request is a GET (rule 10; guard test in
`tests/unit/test_mail_readonly.py`).

**Folders.** `/me/mailFolders` and each folder's `childFolders` give the tree; a
folder is named by its path ("Inbox/Projects"), which is `RemoteMessage.folder`.
Deleted Items and Junk Email (found through their well-known aliases, with their
sub-folders) are left out unless `include_spam_trash`. `folders` names paths
exactly (case-insensitive) and then nothing else is excluded or looked up.

**Changes.** `/me/mailFolders/{id}/messages/delta` per folder, following
`@odata.nextLink` until `@odata.deltaLink`. An item carrying `@removed` is
`RemoteMessageGone`. A new or changed item has its MIME fetched with
`GET /me/messages/{id}/$value` (the raw RFC 822 bytes), up to `workers` at once - Graph
allows four requests in flight per mailbox, and one at a time left a first fetch waiting
on the network for most of its run. The page keeps Graph's order. HTTP 410 on any delta
is `TokenExpired`.

**Ids.** `remote_id` is `"<folderId>/<messageId>"`. Graph reports a move as
"removed" from the old folder's delta and "added" in the new one; if both used the
bare message id, a removal that happens to be read *after* the add would delete a
message that is still there. Qualifying the id by folder makes a move one
`RemoteMessageGone` and one new message that never collide, whatever order the
folders are read in. `Prefer: IdType="ImmutableId"` is sent as well so the message
part survives other changes.

**Token** (JSON, sorted keys): `{"v":1,"f":{folderId:{"delta":link}},"cur":
{"folder":folderId,"next":link}|null}` - `f` holds each finished folder's delta link
and `cur` the folder part-way through with its next page, so every page is
resumable (rule 4).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

from app.accounts.provider import (HttpResponse, MailPage, RemoteMessage, RemoteMessageGone,
                                   TokenExpired, TokenSource, Transport)
from app.accounts.providers._mailcommon import (bearer_headers, dump_token, epoch_from_iso,
                                                fail_for_status, load_token)
from app.core.logging import logger

__all__ = ["GraphMailProvider", "GRAPH_BASE"]

_log = logger.bind(component="accounts.graph_mail")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_COMPONENT = "accounts.graph_mail"

#: Only what the index needs; the body itself comes from `$value`.
_SELECT = "id,receivedDateTime,isRead,categories,changeKey"

#: Fallback when the well-known lookup fails (a server that answers 404 to the
#: alias): top-level names Outlook uses in English.
_BIN_NAMES = frozenset({"deleted items", "junk email"})

#: Graph's limit on requests in flight for one mailbox.
MAX_WORKERS = 4


def remote_id(folder_id: str, message_id: str) -> str:
    return f"{folder_id}/{message_id}"


class GraphMailProvider:
    """Read-only mail from one Microsoft account through Graph."""

    def __init__(self, transport: Transport, token_source: TokenSource, *,
                 account_name: str = "Microsoft", page_size: int = 50,
                 base_url: str = GRAPH_BASE, pace_s: float = 0.02, workers: int = MAX_WORKERS,
                 sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._token_source = token_source
        self._account = account_name
        self._page_size = max(1, min(int(page_size), 1000))
        self._base = base_url.rstrip("/")
        # Graph throttles per mailbox (about 10,000 requests per 10 minutes) and
        # a first fetch makes one request per message; a short pause keeps it clear.
        self._pace_s = max(0.0, pace_s)
        self._sleep = sleep
        self._workers = max(1, min(int(workers), MAX_WORKERS))
        self._pool: Optional[ThreadPoolExecutor] = None

    # -- public --------------------------------------------------------------

    def list_folders(self) -> list[str]:
        return [f["path"] for f in self._tree()]

    def pull(self, token: Optional[str], *, folders: Optional[list[str]] = None,
             since: Optional[float] = None, include_spam_trash: bool = False) -> Iterator[MailPage]:
        try:
            state = load_token(token)
            if state is not None and not (state.get("v") == 1 and isinstance(state.get("f"), dict)):
                raise ValueError("unknown token")
        except ValueError as exc:
            raise TokenExpired(str(exc)) from exc

        selected = self._select(self._tree(), folders, include_spam_trash)
        keep = {f["id"] for f in selected}
        finished: dict[str, dict[str, str]] = {}
        current: Optional[dict[str, str]] = None
        total_hint: Optional[int] = None
        if state is None:
            total_hint = sum(int(f["total"]) for f in selected) or None
        else:
            # A folder no longer selected (or deleted) leaves the token; its
            # messages are the engine's to retire, as they are not listed here.
            finished = {k: v for k, v in state["f"].items() if k in keep}
            cur = state.get("cur")
            if isinstance(cur, dict) and cur.get("folder") in keep and cur.get("next"):
                current = {"folder": cur["folder"], "next": cur["next"]}

        if not selected:
            yield MailPage([], dump_token({"v": 1, "f": {}, "cur": None}), done=True)
            return

        window = None
        if since:
            from datetime import datetime, timezone                        # noqa: PLC0415
            window = datetime.fromtimestamp(int(since), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for index, folder in enumerate(selected):
            fid = folder["id"]
            is_last = index == len(selected) - 1
            if current and current["folder"] == fid:
                url: Optional[str] = current["next"]
            elif fid in finished:
                url = finished[fid]["delta"]
            else:
                url = self._first_delta_url(fid, window)
            while url:
                data = self._json(url, what="messages.delta", gone_is_expired=True)
                next_link = data.get("@odata.nextLink")
                delta_link = data.get("@odata.deltaLink")
                if not next_link and not delta_link:
                    # Graph always ends a delta with one of the two; without either
                    # there is nothing to resume from, so start over.
                    raise TokenExpired("Graph returned a delta page with no link to continue from")
                changes = self._changes(data.get("value", []), folder)
                if next_link:
                    current = {"folder": fid, "next": next_link}
                    url = next_link
                else:
                    finished[fid] = {"delta": delta_link}
                    current = None
                    url = None
                token = dump_token({"v": 1, "f": finished, "cur": current})
                yield MailPage(changes, token, done=is_last and not next_link, total_hint=total_hint)
                total_hint = None

    def close(self) -> None:
        """Let the download threads go (each kept its own connection)."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    # -- HTTP ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Immutable ids so a message keeps its id when it is moved or changed;
        # the page size is a request, Graph may return fewer.
        return bearer_headers(self._token_source(), {
            "Prefer": f'odata.maxpagesize={self._page_size}, IdType="ImmutableId"'})

    def _request(self, url: str) -> HttpResponse:
        return self._transport.request("GET", url, headers=self._headers())

    def _json(self, url: str, *, what: str, gone_is_expired: bool = False,
              allow_404: bool = False) -> Any:
        resp = self._request(url)
        if resp.status == 410 and gone_is_expired:
            raise TokenExpired("Graph no longer holds that delta state")
        if resp.status == 404 and allow_404:
            return None
        if resp.status != 200:
            fail_for_status(resp, account=self._account, component=_COMPONENT, what=what)
        return resp.json() or {}

    # -- folders -------------------------------------------------------------

    def _children(self, url: str, parent_path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while url:
            data = self._json(url, what="mailFolders")
            for item in data.get("value", []):
                name = item.get("displayName") or item.get("id", "")
                path = f"{parent_path}/{name}" if parent_path else name
                out.append({"id": item["id"], "path": path, "total": item.get("totalItemCount", 0)})
                if item.get("childFolderCount"):
                    out.extend(self._children(
                        f"{self._base}/me/mailFolders/{quote(item['id'], safe='=-_')}/childFolders?$top=100", path))
            url = data.get("@odata.nextLink") or ""
        return out

    def _tree(self) -> list[dict[str, Any]]:
        return self._children(f"{self._base}/me/mailFolders?$top=100", "")

    def _select(self, tree: list[dict[str, Any]], folders: Optional[list[str]],
                include_spam_trash: bool) -> list[dict[str, Any]]:
        if folders:
            wanted = {f.lower() for f in folders}
            return [f for f in tree if f["path"].lower() in wanted]
        if include_spam_trash or not tree:
            return tree
        bins: set[str] = set()
        for alias in ("deleteditems", "junkemail"):
            data = self._json(f"{self._base}/me/mailFolders/{alias}?$select=id", what="mailFolders",
                              allow_404=True)
            if data and data.get("id"):
                bins.add(data["id"])
        paths = {f["path"] for f in tree if f["id"] in bins}
        if not paths:
            paths = {f["path"] for f in tree if "/" not in f["path"] and f["path"].lower() in _BIN_NAMES}
        return [f for f in tree
                if not any(f["path"] == p or f["path"].startswith(p + "/") for p in paths)]

    # -- messages ------------------------------------------------------------

    def _first_delta_url(self, folder_id: str, window: Optional[str]) -> str:
        url = f"{self._base}/me/mailFolders/{quote(folder_id, safe='=-_')}/messages/delta?$select={_SELECT}"
        if window:
            # Delta accepts one filter only, on receivedDateTime; it is kept in
            # the delta link Graph hands back, so later pulls need not repeat it.
            url += f"&$filter=receivedDateTime%20ge%20{window}"
        return url

    def _changes(self, items: list[dict[str, Any]], folder: dict[str, Any]) -> list[Any]:
        latest: dict[str, dict[str, Any]] = {}
        for entry in items:
            if entry.get("id"):
                latest[entry["id"]] = entry          # last word on a message wins
        wanted = list(latest.items())
        if self._workers > 1 and sum(1 for _m, e in wanted if "@removed" not in e) > 1:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(self._workers, thread_name_prefix="graph-mail")
            # map keeps the order and raises the first failure, as the loop did.
            return list(self._pool.map(lambda item: self._change(item[0], item[1], folder), wanted))
        return [self._change(mid, entry, folder) for mid, entry in wanted]

    def _change(self, mid: str, entry: dict[str, Any], folder: dict[str, Any]) -> Any:
        rid = remote_id(folder["id"], mid)
        if "@removed" in entry:
            return RemoteMessageGone(rid)
        if self._pace_s:
            self._sleep(self._pace_s)
        resp = self._request(f"{self._base}/me/messages/{quote(mid, safe='=-_')}/$value")
        if resp.status == 404:                       # deleted between the delta and now
            return RemoteMessageGone(rid)
        if resp.status != 200:
            fail_for_status(resp, account=self._account, component=_COMPONENT, what="message $value")
        return RemoteMessage(
            remote_id=rid,
            raw=resp.body,
            folder=folder["path"],
            labels=list(entry.get("categories") or []),
            received_at=epoch_from_iso(entry.get("receivedDateTime", "")),
            seen=bool(entry.get("isRead")),
            version=str(entry.get("changeKey", "")),
        )
