r"""Messages stored twice in one mail archive, found and removed from the index.

Layer: L3

Order 1j section 3 (2026-10-11), the owner: "remove the duplicates from pst
from the current index". Measured on the owner's index that day, read-only:
3,313 groups held 3,458 extra copies of a message, plus 344 attachment rows
under them, every group inside one archive - an Outlook item that really is
in the `.pst` twice, read twice.

**What "the same message" means (D2).** The same archive, the same text
(`content_hash`) **and** the same subject, sender and sent time. A shared
text alone is not enough: 2,294 texts on the owner's index are shared by
messages that differ in one of the three - an automated mail sent every day.

**Which copy stays (D3).** The lowest id - the first one indexed. A removed
message's attachments go with it; the kept message keeps its own.

**The index only.** Nothing here opens, changes or deletes a mail archive:
the rows go, the `.pst` is exactly as it was.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MailDuplicates", "find_mail_duplicates", "remove_mail_duplicates", "summary_lines"]

#: How many example groups a dry run names.
EXAMPLES = 5


@dataclass
class MailDuplicates:
    """What a removal would take out, and why."""

    #: `(kept id, kept key, removed ids, removed keys, subject, sender, sent_at, archive)`.
    groups: list[tuple] = field(default_factory=list)
    #: Attachment rows under the removed messages.
    attachment_ids: list[int] = field(default_factory=list)

    @property
    def message_ids(self) -> list[int]:
        return [file_id for group in self.groups for file_id in group[2]]

    @property
    def all_ids(self) -> list[int]:
        return sorted(set(self.message_ids) | set(self.attachment_ids))

    def by_archive(self) -> list[tuple[str, int]]:
        counted = collections.Counter()
        for group in self.groups:
            counted[group[7]] += len(group[2])
        return counted.most_common()


def find_mail_duplicates(store: Any) -> MailDuplicates:
    """Every group of copies by D2, and the attachments the extra copies carry. Read-only."""
    rows = store.conn.execute(
        "SELECT f.id, f.path, f.content_hash, m.subject, m.sender, m.sent_at, m.store_path "
        "FROM files f JOIN messages m ON m.file_id = f.id "
        "WHERE f.source_kind = 'pst_message' AND f.content_hash IS NOT NULL "
        "ORDER BY f.id").fetchall()
    grouped: dict[tuple, list] = collections.defaultdict(list)
    for file_id, path, digest, subject, sender, sent_at, archive in rows:
        grouped[(archive, digest, subject, sender, sent_at)].append((int(file_id), str(path)))
    found = MailDuplicates()
    for (archive, _digest, subject, sender, sent_at), copies in grouped.items():
        if len(copies) < 2:
            continue
        (kept_id, kept_key), *extra = copies
        found.groups.append((kept_id, kept_key, [i for i, _k in extra], [k for _i, k in extra],
                             subject, sender, sent_at, archive))
    for group in found.groups:
        for key in group[3]:
            # Ranges on the path, which the index on `path` answers: an
            # attachment's key is its message's key plus "/attachments/".
            low = key + "/attachments/"
            high = low[:-1] + chr(ord("/") + 1)
            found.attachment_ids.extend(int(row[0]) for row in store.conn.execute(
                "SELECT id FROM files WHERE path >= ? AND path < ?", (low, high)))
    return found


def summary_lines(found: MailDuplicates) -> list[str]:
    """The dry run's words: how many, where, and five examples."""
    messages, attachments = len(found.message_ids), len(found.attachment_ids)
    if not messages:
        return ["No message is stored twice in the same archive. Nothing to remove."]
    lines = [f"{messages:,} extra copies of {len(found.groups):,} messages, and {attachments:,} "
             f"attachment rows under them - {messages + attachments:,} index rows in all.",
             "By archive:"]
    lines += [f"  {count:>7,}  {archive}" for archive, count in found.by_archive()]
    lines.append("For example (kept, then removed):")
    for kept_id, kept_key, _ids, keys, subject, sender, _sent, _archive in found.groups[:EXAMPLES]:
        lines.append(f"  {kept_key}  ->  {', '.join(keys)}   \"{(subject or '')[:60]}\" from {sender}")
    lines.append("Only index rows are removed. The mail archives are not touched.")
    return lines


def remove_mail_duplicates(store: Any, vectors: Any, image_vectors: Any, *,
                           run_lock_owner: str, **lock_options: Any) -> int:
    """Remove the extra copies and their attachments: vectors, then rows. Returns how many.

    Found again **under the index run lock** (`forget_folder.forget_ids`), so an
    index run cannot be writing the archive while its rows go.
    """
    from app.index.forget_folder import forget_ids

    return forget_ids(store, vectors, image_vectors,
                      lambda: find_mail_duplicates(store).all_ids,
                      run_lock_owner=run_lock_owner, **lock_options)
