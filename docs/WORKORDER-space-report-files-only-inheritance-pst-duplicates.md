# Work order (One thread): the Space Report counts files only, Digital Inheritance shows the sources, and duplicate PST messages leave the index

**Doc version:** 1.1 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/reports/space.py`, `app/reports/inheritance.py`,
`app/storage/sqlite_store.py`, `app/ui/reports_view.py`, `app/cli/`, the PST readers under
`app/extract/`, `app/index/`, `tests/unit/`)
**Status:** §1-§2 BUILT 2026-10-11; §3 waits for the owner's yes after the dry run below; RELEASED by the owner 2026-10-11 ("the space report should not include duplicates
from pst's it should be only for files, and the digital inheritance does not contain anything,
remove the duplicates from pst from the current index.. for all this create a work order and
give it to running thread").

**Where this came from: measured, not guessed.** Read-only queries against the owner's index
(`D:\LeashaIndex\Data\fts\knowledge.db`) on 2026-10-11:

| `source_kind` | Rows | Rows sharing a `content_hash` with another row |
|---|---|---|
| `file` | 135,846 | 26,725 |
| `archive` (members inside `.zip` and the like) | 131,661 | 58,256 |
| `pst_message` (messages and their attachments) | 281,553 | 31,748 |
| `eml` | 741 | 6 |

1. **The Space Report mixes mail in.** `space.py` leaves out only *archived messages*
   (`row_facts.archived_message_sql`), so attachments inside a `.pst` still count as duplicates.
   And `find_source_duplicate_share` and `find_source_uniqueness` filter nothing at all. Only
   `hash_coverage` already reads `source_kind = 'file'`.
2. **Digital Inheritance is empty because of a slash.** `ui:roots` holds `D:/OutlookArchive|D:/Data`
   (forward slashes); `files.path` holds `D:\Data\...`. `local_root_summary` and
   `local_root_folder_counts` match with `LIKE 'D:/Data%'`: **0 rows**, against **135,841** with
   `D:\Data%`. Every source is listed with nothing in it. There are no `volumes` rows, so the local
   roots are the whole report.
3. **The index holds duplicate PST messages.** 9,973 `pst_message` hashes occur more than once:
   **21,245 extra rows**. The one inspected was two items in the same archive
   (`pst://2025/2830052` and `pst://2025/2830692`).

---

## Decisions (defaults the building thread may take; say which were taken at close-out)

- **D1 What "files" means for the Space Report.** Default: `source_kind = 'file'`, real files on
  disk, which a person can delete to get space back. Members inside a `.zip` (`archive`) are left
  out too: no space comes back by deleting one. Mail (`pst_message`, `eml`) is left out entirely.
- **D2 What makes two PST rows "the same message".** Default: the same `content_hash` **and** the
  same subject, sender and sent time, read from the message's own metadata. A shared hash alone
  could merge two different automated mails with the same text. If the metadata is not stored
  per row, say so and stop at the dry run (3a) for the owner's ruling.
- **D3 Which copy stays.** Default: the copy with the lowest `id`, i.e. the first one indexed.
  An attachment stays with the message that is kept.

---

## 1. The Space Report counts files only

> *2026-10-11, built:* `row_facts.disk_file_sql` (`DISK_FILE_KIND = 'file'`, D1 as written), read by all five queries, inner and outer; `archived_message_sql` is no longer used by `space.py`. Catalogued-drive files are `source_kind = 'file'` too, so they stay in.
- [x] **1a** Every query in `space.py` (`find_duplicate_groups`, `total_reclaimable_bytes`,
  `find_near_duplicate_photo_groups`, `find_source_duplicate_share`, `find_source_uniqueness`)
  reads only the rows D1 names. One shared SQL fragment in `row_facts`, not five copies.
> *2026-10-11, built:* `space.SCOPE_SENTENCE`, in the document (so `leasha report space` prints it) and at the end of the on-screen headline (`space_rows.space_headline`).
- [x] **1b** The report says plainly, in one sentence, that it covers files on disk and not mail.
  `leasha report space` prints the same document.
- [x] **1c** Tests: a fixture with a duplicated file, a duplicated PST attachment, a duplicated
  PST message and a duplicated zip member. Only the file appears, in every section and in the
  reclaimable total.

## 2. Digital Inheritance shows what is there

> *2026-10-11, built:* `sqlite_store._under_root_sql` asks for both spellings and needs a separator after the root - which also stops `D:\Data` taking `D:\Database`, a second fault found in the same query. No other store query matches `ui:roots` with `LIKE`; the CLI readers turn roots into `Path`s.
- [x] **2a** A root matches its files whatever slash it was saved with: compare normalised paths
  in `local_root_summary` and `local_root_folder_counts` (and anything else reading `ui:roots`
  with `LIKE`, found by grep, not assumed). Fix it in the store, not in the report.
> *2026-10-11:* the forward slashes come from Qt: `RootsBox` saves what `QFileDialog.getExistingDirectory` returns. Decision: the stored value is left as it is - nothing to migrate - because the store now matches either form.
- [x] **2b** Find where `ui:roots` gets forward slashes written, and decide whether to store one
  form. If the stored value changes, migrate it; never rewrite it by hand on the owner's index.
- [x] **2c** Tests: roots saved as `D:/Data` and as `D:\Data` both give the same counts; a root
  with no files says so in words rather than showing an empty paragraph.
> *2026-10-11, measured read-only on the owner's index:* `D:/Data` - 135,841 files, 441.3 GB; top folders Company SE 59,840, Company Wipro 24,240, _Media 16,610, Calibre Library 13,408, Company INDEFF 9,511. `D:/OutlookArchive` - 5 files (the archives themselves).
- [x] **2d** On the owner's index (read-only): Digital Inheritance lists `D:\Data` with about
  135,841 files and its top folders. Record the figures in this order.

## 3. Duplicate PST messages leave the index

**This changes the owner's index. Back it up first, show the count, and wait for the owner's yes
before deleting anything.** User files are never touched; only index rows.

> *2026-10-11, dry run (read-only script; the `dedupe-mail` command is not built yet):* D2 is possible - subject, sender and
> sent time are stored per message (`messages`). Of 219,869 PST messages, **3,313 groups hold 3,458 extra copies**, plus
> **344 attachment rows** under them: 3,802 rows in all. Every group is inside one archive. By archive: 2011.pst 1,664,
> 2018.pst 1,224, 2017.pst 263, 2025.pst 91, 2010.pst 71, 2019.pst 45, 2026.pst 28, 2020.pst 27, 2009.pst 8, 2016.pst 8.
> **Not the 21,245 above:** that counted a shared hash alone, attachments included; 2,294 hashes are shared by messages
> that differ in subject, sender or time, and D2 keeps those. Five examples (kept / removed): pst://2026/2687972 /
> 2688004 "Meeting Follow up"; pst://2026/2688036 / 2688068 "Accepted: [External] - Re: Introduction to SI";
> pst://2026/2688164 / 2688196 "FW: Datwyler upgrades"; pst://2008/2108548 / 2108580 "RE: Overtime Approval";
> pst://2026/2702628 / 2702660 "RE: Quotations pending Indeff". **Waiting for the owner's yes.**
- [ ] **3a** A dry run (`leasha dedupe-mail --dry-run`, or the nearest existing command shape)
  lists how many rows D2 would remove, by archive, with five examples. Post the numbers to the
  owner and **stop for their go-ahead**.
- [ ] **3b** On the owner's yes, with no index run holding the run lock: copy `knowledge.db` and
  the vector store aside, then remove the extra rows from `files`, the full-text index, the vector
  store and every table keyed by the file id, in one transaction per store. Run `reembed --check`
  (order 1h) and a full-text integrity check afterwards.
- [ ] **3c** The next index run must not bring them back: the PST readers skip a message whose
  D2 key is already indexed in that archive. Test it with a fixture archive holding a message
  twice.
- [ ] **3d** Search and the Mail tab show one copy where they showed two; record the before and
  after counts in this order.

## Close-out

Tick each box only when built and tested; 3a-3b need the laptop and the owner. Update this
order's Status line, the register row (`docs/ORDER_REGISTER.md`, 1j) and `HANDOFF.md`, and name
which of D1-D3 were taken as written.
