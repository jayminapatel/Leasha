"""Mail fetched from an account, written into the index.

Layer: L7

Order 1k 5d, 5e, 5f, 5g and D13. `mail_sync.py` fetches; this module is the only
thing that turns what was fetched into index rows, and the only thing that takes
them out again. Nothing here talks to a server, and nothing writes to a mailbox.

**How a live message is keyed.** A message is an ordinary `files` row whose path is
`account://<account id>/<folder>/<remote id>` (`mail_ingest.account_path`), source
kind `eml`, with an ordinary `messages` row (`store_path` NULL; the v38 columns
`account_id`, `remote_id`, `labels`, `message_id` say what only an account knows).
`eml` rather than a new kind because every list, filter, preview and count already
treats `eml` as mail, and `Pipeline._prune_missing` only ever looks at `file` and
`archive` rows, so a row whose path is not on disk is never pruned (the same
precedent as a message inside an `.mbox`). A file attached to a message is keyed
like a `.pst` one - `<message key>/attachments/<name>`, source kind `pst_message` -
so the Files tab, `attachment_of` and the preview find it unchanged.

**One chunker, one store path.** The text is built by `build_email_document` (the
function `.eml`, `.mbox` and `.pst` mail all pass through), cut by `chunk_document`
(the pipeline's own), and written with the store calls `Pipeline._write_one` makes
(`upsert_file`, `replace_chunks`, `set_message`), inside one `store.batch()` per page.
It is not `Pipeline._write_one` itself: that is welded to an index run (its stats,
its write group, its picture lane), and a sync is not one.

**Words first, meaning after.** A page's messages are committed as `PARTIAL` rows
with their passages - searchable by their words at once. Their vectors come after,
in `embed_pending`, if an embedder was given; if not, or if it fails, the passages
stay `embedded = 0` and the ordinary repair at the start of the next index run
(`Pipeline._drain_unembedded`) fills them in. Either way a sync never fails for want
of a model.

**D13: a live copy and an Outlook copy are one message.** Matched by the normalised
`Message-ID`. The live copy is searched; the archive copy is *taken out of the index*
(its row, passages, vectors and attachments - the way order 1j's `dedupe-mail` takes
extra copies out) and recorded in `message_places` (kind `archive`, ref = its old
`files.path`, with its `store_path` and `entry_id`, so it can still be found in
Outlook). Whichever is indexed second finds the first:

* live copy written, archive copy already indexed - `MailIndexer.apply_page` moves it;
* archive copy read after the live one - `Pipeline._write_one` asks
  `archive_copy_is_covered`, which records the place and leaves the row out, and a
  re-read of the archive meets the recorded `ref` and does the same, so it is not
  resurrected.

If the live copy later goes (deleted at the server, or the account's mail removed),
its places go with it (`ON DELETE CASCADE`) and `reopen_archives` clears the archive's
marker so the next index run reads it again and the copy comes back.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.parse import quote

from app.accounts.mail_ingest import (ACCOUNT_SCHEME, ParsedMessage, account_path,
                                      normalise_message_id, parse_raw)
from app.accounts.provider import RemoteMessage
from app.core.logging import logger
from app.core.row_facts import ATTACHMENT_MARKER

__all__ = ["MailIndexer", "PreparedMessage", "PageResult", "MailSpace", "account_prefix",
           "archive_copy_is_covered", "embedder_function"]

_log = logger.bind(component="accounts.mail_index")

#: The same ceiling the Outlook readers use for one attachment (64 MB), used when
#: the account sets none. Imported lazily; this is the fallback if that cannot be.
_DEFAULT_ATTACHMENT_BYTES = 64 * 1024 * 1024


def account_prefix(account_id: object) -> str:
    """`account://<id>/` - what every row of that account's mail starts with."""
    return f"{ACCOUNT_SCHEME}{quote(str(account_id), safe='')}/"


def embedder_function(embedder: Any) -> Callable[[list[str]], list]:
    """`embed(texts) -> vectors` from the pipeline's `Embedder` (`embed_all`, lazily)."""
    def embed(texts: list[str]) -> list:
        return list(embedder.embed_all(texts))
    return embed


# --------------------------------------------------------------------------
# D13, the half the pipeline asks
# --------------------------------------------------------------------------

def archive_copy_is_covered(store: Any, key: str, meta: dict, *, has_text: bool = True) -> bool:
    """Is the Outlook message (or attachment) at `key` another place of a live message?

    True means "leave it out of the index": its message is already recorded as a
    second place of an account's copy, or its `Message-ID` matches one - in which
    case the place is recorded now. An attachment follows its message (a message is
    always handed over before its attachments). False for everything else, which is
    nearly every message. Called by `Pipeline._write_one` for every `pst_message`;
    two indexed lookups, and none at all when the message carries no Message-ID.
    """
    if ATTACHMENT_MARKER in key:
        return store.place_ref_known("archive", key.split(ATTACHMENT_MARKER, 1)[0])
    if store.place_ref_known("archive", key):
        return True
    nid = normalise_message_id(str(meta.get("message_id") or ""))
    if not nid or not has_text:
        return False
    live = store.live_copy_of(nid)
    if live is None:
        return False
    store.add_message_place(live, "archive", key, store_path=meta.get("store_path"),
                            entry_id=meta.get("entry_id"))
    return True


# --------------------------------------------------------------------------
# What is prepared (no database) and what a page did
# --------------------------------------------------------------------------

@dataclass
class _Piece:
    """One row to write: the message, or a file attached to it."""
    key: str
    chunks: list[dict[str, Any]]
    source_kind: str
    ext: str
    size: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedMessage:
    account_id: int
    remote_id: str
    folder: str
    key: str
    nid: str                                  # normalised Message-ID, "" when it has none
    labels: list[str]
    received_at: float
    sent_at: Optional[int]
    raw_size: int
    pieces: list[_Piece]                      # [0] is the message
    nameless_skipped: int = 0

    @property
    def attachments(self) -> int:
        return len(self.pieces) - 1


@dataclass
class PageResult:
    added: int = 0
    changed: int = 0
    deleted: int = 0
    attachments: int = 0
    moved_archive_copies: int = 0             # D13: archive copies taken out behind a live one
    released_archives: int = 0
    embedded: int = 0
    pending_meaning: int = 0
    file_ids: list[int] = field(default_factory=list)


@dataclass
class MailSpace:
    """What an account's mail takes in the index (10q.5). Counted, then estimated."""
    messages: int = 0
    attachments: int = 0
    passages: int = 0
    embedded: int = 0
    text_bytes: int = 0
    estimated_bytes: int = 0                  # text + keyword index + vectors, a stated estimate

    @property
    def bytes(self) -> int:
        return self.estimated_bytes


# --------------------------------------------------------------------------
# The indexer
# --------------------------------------------------------------------------

class MailIndexer:
    """Writes fetched mail into an open `SqliteStore` (and its vector store), and removes it.

    `vectors` / `image_vectors` are the pipeline's stores (only `delete_by_file_ids`,
    and `add` for text, are used); `embed(texts) -> vectors` gives the passages their
    meaning (`embedder_function(pipeline.embedder)`). All three are optional: without
    them mail is searchable by its words and the next index run adds the meaning.
    """

    def __init__(self, store: Any, *, vectors: Any = None, image_vectors: Any = None,
                 embed: Optional[Callable[[list[str]], Sequence[Any]]] = None, embed_batch: int = 64,
                 attachment_mode: str = "documents", max_attachment_bytes: Optional[int] = None,
                 vector_dim: int = 384, run_lock_owner: Optional[str] = None,
                 run_lock_options: Optional[dict] = None) -> None:
        self.store = store
        self.vectors = vectors
        self.image_vectors = image_vectors
        self.embed = embed
        self.embed_batch = max(1, int(embed_batch))
        self.attachment_mode = attachment_mode
        self.max_attachment_bytes = max_attachment_bytes
        self.vector_dim = vector_dim
        self.run_lock_owner = run_lock_owner
        self.run_lock_options = dict(run_lock_options or {})

    # ------------------------------------------------------------ preparing
    def prepare(self, account_id: int, msg: RemoteMessage, *, attachments: bool = True,
                attachment_limit: Optional[int] = None,
                attachment_mode: Optional[str] = None) -> Optional[PreparedMessage]:
        """Parse and chunk one fetched message. No database; may be slow (attachments),
        so the caller does it outside a transaction. None for a message with no text at all."""
        from app.extract.base import SourceKind
        from app.extract.email_files import build_email_document

        parsed = parse_raw(msg.raw)
        key = account_path(account_id, msg.folder, msg.remote_id)
        conversation = parsed.references[0] if parsed.references else (parsed.message_id or None)
        document = build_email_document(
            Path("account-mail"), subject=parsed.subject, sender=parsed.sender,
            recipients=parsed.recipients, sent_at=parsed.sent_at, conversation=conversation,
            body=parsed.body_text, attachments=[name for name, _data, _type in parsed.attachments],
            source_kind=SourceKind.EML)
        document.virtual_path = key
        chunks = _passages(document)
        if document.is_empty or not chunks:
            return None
        meta = dict(document.meta)
        meta["message_id"] = parsed.message_id
        pieces = [_Piece(key, chunks, SourceKind.EML, "eml", len(msg.raw), meta)]
        if attachments and parsed.attachments:
            pieces += self._attachment_pieces(
                key, msg.raw, parsed, limit=attachment_limit if attachment_limit else
                (self.max_attachment_bytes or _attachment_ceiling()),
                mode=attachment_mode or self.attachment_mode)
        return PreparedMessage(
            account_id=int(account_id), remote_id=msg.remote_id, folder=msg.folder, key=key,
            nid=normalise_message_id(parsed.message_id), labels=list(msg.labels or ()),
            received_at=float(msg.received_at or 0), sent_at=parsed.sent_at, raw_size=len(msg.raw),
            pieces=pieces)

    def _attachment_pieces(self, message_key: str, raw: bytes, parsed: ParsedMessage, *,
                           limit: int, mode: str) -> list[_Piece]:
        """The rows for a message's attachments by the one rule every mail reader shares
        (`mail_attachments.rule`): contents, a zip's member names, or the name alone."""
        from app.extract import mail_attachments as rules
        from app.extract.archive import attachment_key
        from app.extract.base import SourceKind, extract as extract_path
        from app.extract.source_types import indexed_ext

        inline = _inline_picture_names(raw)
        seen: set[str] = set()
        out: list[_Piece] = []
        for name, data, _ctype in parsed.attachments:
            if name in inline:
                continue                       # a signature logo: not something anybody attached
            kind = rules.rule(name, mode)
            digest = hashlib.blake2b(data, digest_size=16).hexdigest()
            documents: list[Any] = []
            if kind == rules.NAMES_INSIDE:
                if digest in seen:
                    continue
                documents = [rules.name_only_document(name, message_key, inside=rules.names_inside(data),
                                                      digest=digest, backend="account")]
            elif kind == rules.NAME_ONLY or len(data) > limit:
                documents = [rules.name_only_document(name, message_key, digest=digest, backend="account")]
            else:
                if digest in seen:
                    continue                   # the same bytes are already indexed with this message
                documents = self._read_attachment(name, data, message_key, digest, extract_path,
                                                  attachment_key, rules)
            seen.add(digest)
            for doc in documents:
                chunks = _passages(doc)
                if not chunks:
                    continue
                doc_meta = dict(doc.meta)
                doc_meta.setdefault("attachment_of", message_key)
                doc_meta.setdefault("attachment_name", name)
                doc_meta.setdefault("attachment_size", len(data))
                out.append(_Piece(doc.key, chunks, SourceKind.PST_MESSAGE, indexed_ext(Path(name)),
                                  len(data), doc_meta))
        # Two attachments with the same name and bytes would share a key; the first wins.
        unique: dict[str, _Piece] = {}
        for piece in out:
            unique.setdefault(piece.key, piece)
        return list(unique.values())

    @staticmethod
    def _read_attachment(name: str, data: bytes, message_key: str, digest: str, extract_path: Any,
                         attachment_key: Any, rules: Any) -> list[Any]:
        """Read an attachment through the registry; an unreadable one is kept by name."""
        from app.extract.base import SourceKind

        with tempfile.TemporaryDirectory(prefix="leasha_attach_") as scratch:
            target = Path(scratch) / Path(name).name
            try:
                target.write_bytes(data)
                documents = list(extract_path(target))
            except Exception as exc:                          # noqa: BLE001 - one bad attachment (rule 3)
                _log.debug("attachment {} kept by name: {}", name, exc)
                return [rules.name_only_document(name, message_key, digest=digest, backend="account")]
            for doc in documents:
                doc.virtual_path = attachment_key(message_key, name, doc.virtual_path, target)
                doc.source_kind = SourceKind.PST_MESSAGE
                doc.meta.setdefault("attachment_of", message_key)
                doc.meta.setdefault("attachment_name", name)
                doc.meta.setdefault("attachment_size", len(data))
                doc.meta.setdefault("content_hash", digest)
            return documents or [rules.name_only_document(name, message_key, digest=digest, backend="account")]

    # --------------------------------------------------------------- a page
    def apply_page(self, account_id: int, prepared: Sequence[PreparedMessage],
                   gone_remote_ids: Iterable[str] = (), *,
                   in_transaction: Optional[Callable[[Any], None]] = None) -> PageResult:
        """Write a page's messages and take its gone ones out, **in one transaction**.

        `in_transaction(conn)` runs inside that transaction - the syncer saves the page's
        resume token there, so the messages and the token are written together or not at
        all (rule 4). Vectors of what is deleted go first (a crash leaves vectors for rows
        that still exist, re-deleted next time); the new passages' vectors are written
        after the commit, so the words are searchable before the meaning (5g).
        """
        store = self.store
        result = PageResult()
        written_keys = {p.key for p in prepared}
        gone = [r for r in dict.fromkeys(str(x) for x in gone_remote_ids)]
        gone_files = store.message_file_ids_for_remote(account_id, gone)

        doomed: dict[int, None] = {}
        released_files: list[int] = []
        for remote_id, file_id in gone_files.items():
            record = store.get_file_by_id(file_id)
            if record is None or record.path in written_keys:
                continue                      # re-delivered in this very page: it stays
            doomed[file_id] = None
            released_files.append(file_id)
            for att in store.attachment_file_ids(record.path):
                doomed[att] = None
        # Attachments a re-delivered message no longer has.
        for p in prepared:
            existing = store.get_file(p.key)
            if existing is None:
                continue
            keep = {piece.key for piece in p.pieces}
            for row in store.conn.execute(
                    "SELECT id, path FROM files WHERE path >= ? AND path < ?",
                    store._path_range(p.key + ATTACHMENT_MARKER)):
                if str(row["path"]) not in keep:
                    doomed[int(row["id"])] = None
        # D13 (archive first): the Outlook copies of the messages written here.
        copies: dict[int, list[dict]] = {}
        for p in prepared:
            if not p.nid:
                continue
            found = store.archive_copies_of(p.nid)
            if found:
                copies[id(p)] = found
                for copy in found:
                    doomed[copy["file_id"]] = None
                    for att in store.attachment_file_ids(copy["path"]):
                        doomed[att] = None
                    result.moved_archive_copies += 1
        released = store.places_of_files(released_files)

        self._delete_vectors(list(doomed))
        pending: list[tuple[int, int, str]] = []
        indexed_now: list[int] = []
        with store.batch() as conn:
            for p in prepared:
                self._write(p, result, pending, indexed_now, copies.get(id(p), ()))
            for file_id in doomed:
                store.delete_file(file_id)
            result.deleted = len(released_files)
            if in_transaction is not None:
                in_transaction(conn)
        if released:
            result.released_archives = store.reopen_archives([r["store_path"] for r in released if r["store_path"]])
        result.pending_meaning = len({fid for _c, fid, _t in pending})
        result.embedded = self.embed_pending(pending)
        return result

    def _write(self, p: PreparedMessage, result: PageResult, pending: list, indexed_now: list,
               archive_copies: Iterable[dict]) -> None:
        """One prepared message's rows, inside the page's transaction."""
        from app.index.pipeline import _text_digest      # noqa: PLC2701 - one definition of the digest
        from app.storage.filters import sent_at_ns
        from app.storage.sqlite_store import FileStatus

        store = self.store
        stamp_s = p.received_at or p.sent_at or time.time()
        mtime_ns = int(stamp_s * 1_000_000_000)
        parent = f"{account_prefix(p.account_id)}{quote(p.folder, safe='')}"
        existing = store.get_file(p.key) is not None
        message_id = 0
        for index, piece in enumerate(p.pieces):
            file_id = store.upsert_file(
                piece.key, size_bytes=piece.size, mtime_ns=mtime_ns, content_hash=_text_digest(piece.chunks),
                status=FileStatus.PARTIAL, source_kind=piece.source_kind, parent_dir=parent, ext=piece.ext,
                taken_at_ns=sent_at_ns(p.sent_at) if index == 0 else None, taken_at_is_hint=False)
            ids = store.replace_chunks(file_id, piece.chunks)
            to_embed = set(getattr(ids, "to_embed", ids))
            if ids and not to_embed:
                store.mark_indexed_many([file_id])          # every passage already has its vector
            pending += [(cid, file_id, chunk["text"]) for cid, chunk in zip(ids, piece.chunks, strict=True)
                        if cid in to_embed]
            if index == 0:
                message_id = file_id
                m = piece.meta
                store.set_message(
                    file_id, subject=m.get("subject"), sender=m.get("sender"),
                    recipients=m.get("recipients"), sent_at=m.get("sent_at"),
                    conversation=m.get("conversation"), has_attach=int(m.get("has_attach", 0) or 0),
                    quoted_removed=m.get("quoted_removed"))
                store.set_account_message(file_id, account_id=p.account_id, remote_id=p.remote_id,
                                          labels=p.labels, message_id=p.nid)
                result.file_ids.append(file_id)
            else:
                result.attachments += 1
        if existing:
            result.changed += 1
        else:
            result.added += 1
        for copy in archive_copies:                         # D13: the Outlook copy is the second place
            store.add_message_place(message_id, "archive", copy["path"], store_path=copy.get("store_path"),
                                    entry_id=copy.get("entry_id"))

    # ------------------------------------------------------------- meaning
    def embed_pending(self, pending: list[tuple[int, int, str]]) -> int:
        """Give these passages their vectors, a whole file at a time. Returns how many.

        Never raises: a model that will not load, or a vector store that refuses, leaves the
        passages `embedded = 0` and the files `PARTIAL` - searchable by their words, and
        filled in by the next index run's repair.
        """
        if not pending or self.embed is None or self.vectors is None:
            return 0
        done = 0
        try:
            for batch in _whole_file_batches(pending, self.embed_batch):
                vectors = list(self.embed([text for _c, _f, text in batch]))
                if len(vectors) != len(batch):
                    _log.warning("the model returned {} vectors for {} passages; left for the next run",
                                 len(vectors), len(batch))
                    break
                files = list(dict.fromkeys(fid for _c, fid, _t in batch))
                except_kept = getattr(self.vectors, "delete_by_file_ids_except", None)
                embedded_ids = getattr(self.store, "embedded_chunk_ids", None)
                keep: set[int] = set()
                if except_kept is not None and embedded_ids is not None:
                    keep = set(embedded_ids(files)) - {cid for cid, _f, _t in batch}
                if keep:
                    except_kept(files, keep)
                else:
                    self.vectors.delete_by_file_ids(files)
                written = self.vectors.add(chunk_ids=[c for c, _f, _t in batch],
                                           file_ids=[f for _c, f, _t in batch], vectors=vectors)
                if written is not None and written < len(batch):
                    _log.error("wrote {} vectors for {} passages; the rest stay PARTIAL", written, len(batch))
                    break
                with self.store.batch():
                    self.store.mark_embedded(c for c, _f, _t in batch)
                    self.store.mark_indexed_many(files)
                done += len(batch)
        except Exception as exc:                              # noqa: BLE001 - the words are already searchable
            _log.warning("mail meaning not added yet ({}); the next index run will fill it in", exc)
        return done

    # ------------------------------------------------------------- removing
    def _delete_vectors(self, file_ids: list[int]) -> None:
        if not file_ids:
            return
        for side in (self.vectors, self.image_vectors):
            if side is not None:
                side.delete_by_file_ids(file_ids)

    def forget(self, file_ids: Iterable[int]) -> int:
        """Vectors first, then rows, for these files. The index only."""
        ids = list(dict.fromkeys(int(i) for i in file_ids))
        self._delete_vectors(ids)
        with self.store.batch():
            for file_id in ids:
                self.store.delete_file(file_id)
        return len(ids)

    def remove_account(self, account_id: int) -> int:
        """Every message and attachment of this account out of the index (10q.5, 1f).

        Returns the rows removed. Archive copies hidden behind these messages are brought
        back (D13) by clearing their archive's marker. Under the index run lock when the
        indexer was given an owner for it, so an index run cannot embed what is going.
        """
        store = self.store
        prefix = account_prefix(account_id)

        def find() -> list[int]:
            return store.file_ids_under_prefix(prefix)

        message_files = [row["file_id"] for row in store.account_message_rows(account_id)]
        places = store.places_of_files(message_files)
        if self.run_lock_owner:
            from app.index.forget_folder import forget_ids

            count = forget_ids(store, self.vectors, self.image_vectors, find,
                               run_lock_owner=self.run_lock_owner, **self.run_lock_options)
        else:
            count = self.forget(find())
        if places:
            store.reopen_archives([p["store_path"] for p in places if p["store_path"]])
        _log.info("took {} row(s) of account {} out of the index; nothing at the account was touched",
                  count, account_id)
        return count

    def prune_unchosen(self, account_id: int, chosen: Sequence[str]) -> int:
        """Take out the account's messages that are in none of the `chosen` folders or labels
        (5e: a message that leaves the chosen set is followed). Gmail-style mail is judged by its
        labels, folder-style mail by its folder; a message with neither is left alone."""
        from app.accounts.mail_ingest import split_account_path

        wanted = {c.casefold() for c in chosen}
        if not wanted:
            return 0
        doomed: list[int] = []
        released: list[int] = []
        for row in self.store.account_message_rows(account_id):
            try:
                _acc, folder, _rid = split_account_path(row["path"])
                labels = json.loads(row["labels"] or "[]")
            except (ValueError, TypeError):
                continue
            names = ({folder.casefold()} if folder else set()) | {str(x).casefold() for x in labels}
            if not names or names & wanted:
                continue
            doomed.append(int(row["file_id"]))
            released.append(int(row["file_id"]))
            doomed += self.store.attachment_file_ids(row["path"])
        places = self.store.places_of_files(released)
        self.forget(doomed)
        if places:
            self.store.reopen_archives([p["store_path"] for p in places if p["store_path"]])
        return len(released)

    # ---------------------------------------------------------------- space
    def space_used(self, account_id: int) -> MailSpace:
        """What this account's mail takes in the index: counts, and an estimate in bytes
        (the passages' text, about as much again for the keyword index, and one vector of
        `vector_dim` floats for each embedded passage)."""
        counted = self.store.account_mail_space(account_id, account_prefix(account_id))
        estimate = counted["text_bytes"] * 2 + counted["embedded"] * self.vector_dim * 4
        return MailSpace(messages=counted["messages"], attachments=counted["attachments"],
                         passages=counted["passages"], embedded=counted["embedded"],
                         text_bytes=counted["text_bytes"], estimated_bytes=estimate)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _passages(document: Any) -> list[dict[str, Any]]:
    """One document's text cut into passages - `Pipeline._passages`, on the same chunker."""
    from app.extract import chunk_document

    return [{"ordinal": ordinal, "text": c.text, "page": c.page, "char_start": c.char_start,
             "char_end": c.char_end, "label": c.label}
            for ordinal, c in enumerate(chunk_document(document))]


def _whole_file_batches(pending: list[tuple[int, int, str]], size: int) -> Iterable[list[tuple[int, int, str]]]:
    """Batches of at least `size` passages that never split a file (a file's vectors are
    replaced as a set, so splitting would delete what the first half wrote)."""
    by_file: dict[int, list[tuple[int, int, str]]] = {}
    for item in pending:
        by_file.setdefault(item[1], []).append(item)
    batch: list[tuple[int, int, str]] = []
    for items in by_file.values():
        if batch and len(batch) >= size:
            yield batch
            batch = []
        batch += items
    if batch:
        yield batch


def _attachment_ceiling() -> int:
    try:
        from app.extract.email_pst import MAX_ATTACHMENT_BYTES

        return int(MAX_ATTACHMENT_BYTES)
    except Exception:                                         # noqa: BLE001
        return _DEFAULT_ATTACHMENT_BYTES


def _inline_picture_names(raw: bytes) -> set[str]:
    """File names of pictures a message shows inline (a signature logo, a spacer): not attachments."""
    names: set[str] = set()
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
        for part in message.walk():
            if (part.get_content_maintype() == "image" and part.get_content_disposition() == "inline"
                    and part.get_filename()):
                names.add(part.get_filename())
    except Exception:                                         # noqa: BLE001 - a listing, not a read
        pass
    return names
