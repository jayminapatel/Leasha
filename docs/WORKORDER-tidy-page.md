# Work order (One thread): Tidy - find what is indexed, and clear copies, similar photos and leftovers

**Doc version:** 1.2 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0
**Thread:** One thread (`app/tidy/`, `app/core/osbridge/recycle.py`, `app/reports/`, `app/cli/tidy.py`,
`app/cli/report.py`, `app/storage/migrations.py`, `app/storage/sqlite_store.py`, `app/ui/tidy_view.py`,
`app/ui/reports_view.py`, `app/ui/presenter/tidy.py`, `app/ui/controllers/tidy_controller.py`, `app/ui/widgets/tidy_*.py`,
`app/ui/shell.py`, `tests/unit/`, the user guide and technical reference)
**Status:** BUILT 2026-10-11, all 14 (on `feat/tidy-1q`); RELEASED by the owner 2026-10-11 ("option a, rename to Tidy, write the order and build it also for
duplicates there should be a method to do it manually too ?"), after the thread's design for the page.

**Where this came from.** The owner, 2026-10-11: "Remove digital inheritance, i dont see the value in it no more,
so that report tabs will have space report only ... think of that as a place where i find what is indexed,
duplicate remover, similar pictures remover etc i.e. mechanism to manage all files comprehensively to streamline
what we keep it will act on local files". The Space Report (order 0n) already finds exact copies by content hash
and near-identical photos by pHash; it could only say so. This order lets the owner act on what it finds.

**What must not change.** Indexing, search and every report stay read-only. Mail is never touched. A catalogued
drive (Offline Media) is never touched. Nothing is ever deleted outright: a file Tidy removes goes to the Windows
Recycle Bin, and Tidy can put it back. Nothing moves until the owner has seen the whole plan and said yes.

---

## Decisions (defaults the building thread may take; say which were taken at close-out)

- **D1 What Tidy may change** (the owner's option (a)). Files on this computer only - `source_kind = 'file'`,
  `volume_id IS NULL`, on a fixed local drive - moved to the Recycle Bin, one at a time, never deleted outright.
  Never a message, a file inside a `.zip`, or a row of a catalogued drive.
- **D2 Never the last copy.** An exact copy is removed only if another copy in its group is kept, is on this
  computer and, at that moment, still reads to the same content hash. A copy on a catalogued drive is not counted
  as the one kept: a drive in a drawer is not a copy anybody can check. In a set of similar photos at least one is
  always kept.
- **D3 Look again before moving.** Each file's size and modified time are read again, and an exact copy's content
  hash too (`walker.content_hash`, the indexer's own). A file that changed since it was indexed is left where it is
  and counted, as order 1l does for mail.
- **D4 Who chooses what stays.** A keep rule per kind, remembered: **Leasha picks** (photos: the most pixels, then
  the sharpest; copies: not in Downloads, a temp or a recycle folder, then the oldest), **Oldest**, **Newest**, and
  **I'll choose** - nothing is marked until the owner clicks. A click on any file overrides the rule for its set.
- **D5 By hand, from anywhere.** Files' right-click gains "Find copies of this..." (the owner's "a method to do it
  manually"), which opens Tidy on that file's copies, or its similar photos for a picture, with nothing marked.
- **D6 A record, and the way back.** Every move is a row in `tidy_journal` (schema v39): when, which batch, the path,
  size, content hash, and where in the Recycle Bin it went. **Put back** returns a batch to where it was, and leaves
  any file whose old place is now taken by another file.
- **D7 A big batch asks twice.** More than 500 files, or more than 20 GB, in one go names both numbers and asks again
  before anything moves - the contact sync's rule (order 1k) applied to files.
- **D8 The page.** The rail's "Reports" becomes **"Tidy"** (the owner, 2026-10-11), icon `wand-sparkles`. Its left
  list is `SideList` (order 1p): Overview, Clean up, Keep safe, Report, History. The right side shows the picked
  entry with icon tabs - Compare, Copies, Details, Keep rule.
- **D9 Digital Inheritance goes.** Its module, its CLI report, its tests and its user-guide section. The helpers the
  Space Report and the timeline borrowed from it (`report_generated_at`, `data_timestamp_sentence`, the size words)
  move to `app/reports/common.py` first.
- **D10 Leftovers and large files are chosen, not found.** Partial downloads (`.crdownload`, `.part`, `.partial`),
  Office owner files (`~$*`), `.tmp`, and installers (`.msi`, `.msix`, `.exe`) in a Downloads folder; large files are
  the biggest on this computer. These usually exist once, so D2 cannot protect them: Leasha marks none of them by
  itself, whatever the keep rule.

---

## 1. The engine (L4, CLI first - non-negotiable 8)

> *2026-10-11, built:* `app/reports/common.py`; `app/tidy/find.py`, `keep.py`, `remove.py`; `app/core/osbridge/recycle.py`;
> `app/cli/tidy.py`; migration `_v39_tidy_journal`. **D3 changed in one place:** the index row is *not* dropped when a file
> moves - the watcher or the next index run forgets it, exactly as for a file deleted in Explorer (one deletion path,
> vectors included), and `find.LOCAL` hides every journalled path that has not been put back. The keep rule's tie-break
> was split (folder, then a copy-like name) after the page was grabbed from a scratch index under `AppData\Temp`, where
> "Leasha picks" kept "Tax return 2024 - Copy.pdf". Large files are 100 MB and over (`LARGE_MIN_BYTES`): with no floor
> a small index listed every file. Empty folders, in the design, were not built - the index holds files, not folders.
- [x] **1a** `app/reports/common.py` holds the shared helpers (D9); Digital Inheritance removed from `app/reports/`,
  `leasha report`, the Reports list and the tests.
- [x] **1b** `app/tidy/find.py`: what is indexed (by kind and by place, with sizes); exact copies and similar photos
  on this computer, with any copies elsewhere named but never offered; large files; leftovers (D10); files that
  exist only once. Reads only.
- [x] **1c** `app/tidy/keep.py`: the keep rules (D4), Qt-free; a photo's pixels and sharpness read from the file
  on a worker. Reads only.
- [x] **1d** `app/tidy/remove.py` and `app/core/osbridge/recycle.py`: a plan checked by D1-D3 and D7, each file sent
  to the Recycle Bin and found there afterwards, the journal row written (D6), the index row dropped.
- [x] **1e** Put back (D6): a batch read from the journal, each file returned from the Recycle Bin to its path.
- [x] **1f** `leasha tidy copies|similar|large|leftovers` lists with the keep rule's choice; `--remove` prints the
  plan and moves only with `--yes`; `leasha tidy history` and `leasha tidy put-back <batch>`.

## 2. The page

> *2026-10-11, built:* `app/ui/tidy_view.py`, `widgets/tidy_set_pane.py`, `widgets/tidy_parts.py`, `presenter/tidy.py`;
> `ReportsView` is now the Space Report pane inside Tidy. **D8 changed in one place:** the four views are the app's
> `SegmentedControl` over a stack, not a `QTabWidget` - the window has no tab widget (its pages are the rail's, guarded by
> `test_ui_redesign_qt`). A saved rail order naming "Reports" puts Tidy in that slot (`rail_order.RENAMED`). Sets load
> 200 at a time, the most room first; the heading says how many there are in all.
- [x] **2a** The rail's Reports becomes Tidy (D8); the page's `SideList` with counts read on a worker.
- [x] **2b** Compare (photos side by side, the kept one marked, a click to keep another), Copies (every path, with
  the kept one starred), Details, Keep rule (D4) - each tab with its icon.
- [x] **2c** Marked files collect across sets ("Marked: 37 - 1.8 GB"); **Review and remove...** shows the plan,
  asks (twice for D7), moves on a worker and says what moved and what was left.
- [x] **2d** Report: the Space Report as it is today, with Export. History: each batch, with Put back.
- [x] **2e** Files' right-click "Find copies of this..." (D5).

## 3. Tests and documents

> *2026-10-11:* `test_tidy_engine.py` (18) and `test_tidy_page.py` (11); twenty existing test files moved from Reports and
> Inheritance to Tidy. **3b on the owner's laptop:** a file made in the thread's scratch folder went to the real Recycle
> Bin in 0.68 s (`C:\$Recycle.Bin\<SID>\$R7MLR79.txt`) and was put back, `$R` and `$I` both gone afterwards;
> `test_the_real_recycle_bin_takes_a_file_and_gives_it_back` repeats it on every Windows run. None of the owner's files
> was touched. The guide's Tidy picture was grabbed from a scratch index (`D:\Demo\leasha-guide`, the guide tool's
> store, is not on this laptop).
- [x] **3a** Tests: the last copy is never offered; a changed file is left; "I'll choose" marks nothing; leftovers
  are never marked by a rule; a big batch asks twice; nothing outside D1 is ever in a plan; put back; the finders
  run with the database read-only; the Recycle Bin faked.
- [x] **3b** On the owner's laptop: one round trip through the real Recycle Bin, on a file the thread made itself in
  its scratch folder. **Removing the owner's files is the owner's press of the button**, never a thread's.
- [x] **3c** The user guide's Reports section becomes Tidy; the technical reference, README and glossary say the
  same; a dated note above non-negotiable 10 records the owner's third exception.

## Close-out

Tick each box only when built and tested. Update this order's Status line, the register row
(`docs/ORDER_REGISTER.md`, 1q) and `HANDOFF.md`, and name which of D1-D10 were taken as written.
