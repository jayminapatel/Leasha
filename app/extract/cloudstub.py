r"""Google Workspace pointers: `.gdoc`, `.gsheet`, `.gslides` and friends.

Layer: L2

A `.gdoc` on disk is not a document. It is a few hundred bytes of JSON holding a
URL and a file id - the content lives in Google's servers and was never on this
machine. Drive for Desktop leaves thousands of them in a synced folder.

**They are indexed anyway, and the reason is the failure they otherwise cause.**
Without this, a `.gdoc` is an unsupported type: skipped, invisible, and absent
from every search. Somebody searches for a document they know exists, finds
nothing, and concludes the search is broken. With it, the pointer is findable by
its name and says plainly what it is - *"this is a Google Docs link; its text is
not on this machine"* - which turns a mystery into an answer.

**It never fetches the URL. Not once, not optionally, not behind a flag.**

That is not a performance decision. This application's whole promise is that
nothing leaves the machine, and a fetch would break it silently: opening a Drive
URL is an authenticated request that tells Google what is being indexed and
when. It would also fail on most of them - a synced folder is usually somebody
else's shared file - and it would turn a 100GB local index run into thousands of
network round trips.

A test asserts no network module is importable from this file. Asserted rather
than assumed, because "we would never do that" is exactly the kind of promise
that erodes one convenience at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from app.core.errors import raise_error
from app.extract.base import Document, DocumentBuilder, SourceKind, register

__all__ = ["CloudStubExtractor", "parse_stub", "StubInfo", "KINDS"]

#: A stub is a few hundred bytes. Anything larger is not one, and reading it as
#: JSON would be a waste - most likely somebody renamed a real document.
MAX_STUB_BYTES = 64 * 1024

#: Extension -> what to call it on screen. The name matters more than usual
#: here: the whole value of the row is telling somebody what they have found.
KINDS = {
    ".gdoc": "Google Docs document",
    ".gsheet": "Google Sheets spreadsheet",
    ".gslides": "Google Slides presentation",
    ".gdraw": "Google Drawing",
    ".gform": "Google Form",
    ".gjam": "Google Jamboard",
    ".gsite": "Google Site",
    ".gtable": "Google Table",
    ".glink": "Google Drive shortcut",
}

#: Keys Drive has used for the URL over the years, most specific first. Drive
#: has changed this format more than once and older files keep the old shape.
_URL_KEYS = ("url", "doc_url", "docUrl", "link", "webViewLink")
_ID_KEYS = ("doc_id", "docId", "resource_id", "resourceId", "id")


class StubInfo:
    """What a pointer file actually holds. Any field may be missing."""

    __slots__ = ("url", "doc_id", "email", "raw_keys")

    def __init__(
        self,
        url: str = "",
        doc_id: str = "",
        email: str = "",
        raw_keys: tuple[str, ...] = (),
    ) -> None:
        self.url = url
        self.doc_id = doc_id
        self.email = email
        self.raw_keys = raw_keys

    @property
    def empty(self) -> bool:
        return not (self.url or self.doc_id)


def parse_stub(data: bytes) -> StubInfo:
    """Read a pointer file. Pure, never raises, never fetches anything.

    Returns an empty `StubInfo` for anything unreadable rather than raising, so
    the caller decides whether an unparseable stub is worth a skip - and it is,
    because a `.gdoc` that is not JSON is a renamed file and worth saying so.
    """
    try:
        payload = json.loads(data.decode("utf-8-sig", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return StubInfo()

    if not isinstance(payload, dict):
        return StubInfo()

    def first(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    return StubInfo(
        url=first(_URL_KEYS),
        doc_id=first(_ID_KEYS),
        email=first(("email", "account", "user_email")),
        raw_keys=tuple(sorted(str(key) for key in payload)),
    )


def _doc_id_from_url(url: str) -> str:
    """Pull the file id out of a Drive URL, without parsing the whole thing.

    Drive URLs are `/document/d/<id>/edit`. The id is worth having on its own:
    it is stable across renames and is what somebody pastes to find the file
    again, so it belongs in the indexed text.
    """
    marker = "/d/"
    start = url.find(marker)
    if start == -1:
        return ""
    tail = url[start + len(marker):]
    return tail.split("/")[0].split("?")[0]


class CloudStubExtractor:
    """Index the pointer, say what it is, and never open the network."""

    name = "cloudstub"
    extensions = frozenset(KINDS)
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document naming the pointer, its kind, link and id. Opens no network.

        A file too large to be a stub, or one holding no link or id, is
        `ERR_CLOUD_STUB` - a skip with the keys that were found, so a renamed
        real document is recognisable as one. Reads only.
        """
        kind = KINDS.get(path.suffix.lower(), "Google Drive file")

        try:
            if path.stat().st_size > MAX_STUB_BYTES:
                raise_error(
                    "ERR_CLOUD_STUB", "extract.cloudstub", path=str(path),
                    details=f"{path.name} is too large to be a pointer file",
                )
                return
            data = path.read_bytes()
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.cloudstub",
                        path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.cloudstub",
                        path=str(path), details=str(exc))
            return

        info = parse_stub(data)
        if info.empty:
            raise_error(
                "ERR_CLOUD_STUB", "extract.cloudstub", path=str(path),
                details=(
                    f"{path.name} holds no link or document id. Keys found: "
                    f"{', '.join(info.raw_keys) or 'none'}"
                ),
            )
            return

        yield self._document(path, kind, info)

    def _document(self, path: Path, kind: str, info: StubInfo) -> Document:
        """The indexed text: the name, what it is, and how to reach it.

        **The filename carries all the meaning there is.** "2025 Budget
        Review.gdoc" is the only searchable content this file has, so it goes
        into the text rather than being left to the filename index alone - a
        search for "budget review" should find it the same way it finds a PDF.
        """
        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        title = path.stem

        lines = [
            title,
            f"{kind} — the text of this document is not stored on this machine.",
        ]
        doc_id = info.doc_id or _doc_id_from_url(info.url)
        if info.url:
            lines.append(f"Link: {info.url}")
        if doc_id:
            lines.append(f"Document id: {doc_id}")
        if info.email:
            lines.append(f"Account: {info.email}")

        builder.add("\n".join(lines), label="Cloud pointer")
        builder.meta.update({
            "format": "cloud-stub",
            "cloud_kind": kind,
            "cloud_url": info.url or None,
            "cloud_doc_id": doc_id or None,
            # The flag a result row uses to explain itself: this is why a search
            # for text inside the document did not match it.
            "content_is_remote": 1,
        })
        return builder.build()


register(CloudStubExtractor())
