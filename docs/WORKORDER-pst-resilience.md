# Work order (One thread): PST resilience

**Doc version:** 1.0 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (`app/extract/pst_libpff.py`, `app/extract/email_pst.py`,
`app/extract/base.py`, `app/core/errors.py`, `app/cli/index.py`)
**Status:** RELEASED by the owner 2026-09-20 - asked for in the same request that asked for
the work, so §1-§3 are built in the session that wrote it. What is still open is named in
§4-§6.

**The request, as made.** *"The indexing of Outlook files should continue even if the file is
open or the PST is slightly corrupted."* Followed by: *"this especially happens when the PST is
accessed directly without Outlook"* - which is the libpff path, and is where every fix below
lands first.

**The audit that preceded it** (read, not run, against the code as of `4ce4a13`) found five
places where a bad archive cost more than it should. Each is now an item.

---

## 0. THE EVIDENCE - read this before changing anything

**What was measured, on this machine, 2026-09-20** (`pypff` 20231205, Windows 11), by holding
a file with `CreateFileW` at each sharing mode and calling `pypff.file().open`:

| Situation | What libpff did |
|---|---|
| Nothing holds the file, it is not a PST | `OSError` ... `invalid file signature` |
| Held **exclusively** (share none) | `OSError` ... `with error: The process cannot access the file because it is being used by another process` |
| Held read+write, share **read** only | opened the file, then failed on the header (so: the lock did not stop it) |
| Held read+write, share read+write | the same |

Two things follow, and one design idea did not survive it.

- A lock and a damaged file **are distinguishable**, by that message. `looks_locked` in
  `app/extract/base.py` is written from it, not from a guess.
- libpff already opens files another program holds with write sharing. **A retry through
  Python's own file handle (`open_file_object`) was drafted and dropped**: it adds nothing,
  because both sides already open at the same sharing level. Only a genuinely exclusive hold
  stops libpff, and it stops Python's `open` as well.

**What was not measured, and must be.** Outlook was not running, and no `.pst` turned up in
the usual places (`Documents\Outlook Files`, `AppData\Local\Microsoft\Outlook`, `D:\SearchData`,
`D:\Leasha`) - only three `.ost` files, which are Outlook's own. So how Outlook holds a `.pst`
it has attached - exclusive or shared - is **unmeasured**. §1e is that measurement.

**Why the code was wrong, in one line each:**

1. Any failure to open an archive was `ERR_FILE_CORRUPT`. That is a *settled* skip: it is never
   retried until the file changes. A file that was merely open in another program was dropped
   from the index for good. A lock (`ERR_FILE_LOCKED`) is retried on every pass
   (`pipeline._locked_candidates`).
2. libpff was tried, and if it was refused nothing else was: not Outlook, which is the very
   program holding the file and can read it.
3. `_to_document` had no guard. Every accessor inside it is guarded, but the document builder
   is not, and one exception ended the generator - every message after it never read.
4. A folder whose message count failed was `continue`d with no record, and a folder whose
   sub-folder list failed lost its whole subtree the same way.
5. Skipped items went to the log and nowhere else, so an archive that was 60% indexed looked
   complete. `drain_busy_folders` (Outlook's skipped folders) was called only by the CLI - in
   the app, that record was written and never read.

---

## 1. A locked archive is a lock, not corruption

- [x] **1a. Classify the open failure.** `looks_locked(exc)` (`app/extract/base.py`):
  `PermissionError`, Windows codes 32/33, or the message libpff carries. `read_archive` and
  `export_to_eml` both use it, so `app.cli convert` gets the right code too.
- [x] **1b. The same on the Outlook path.** `Win32ComSession.attach` reported every refusal as
  `ERR_FILE_CORRUPT`; a lock is now `ERR_FILE_LOCKED`.
- [x] **1c. Fall back to Outlook.** `PstExtractor.extract`: on `ERR_FILE_LOCKED` from libpff,
  read the archive through Outlook. Three conditions, each with a test - **only before the
  first message** (a fallback part-way through indexes the archive twice), **only in `auto`**
  (a forced backend was a choice), and **if Outlook is missing or refuses, report the lock**,
  not the second failure, because that is the honest reason and it is retried.
- [x] **1d. Real-library tests.** `test_pst_resilience.py`: an exclusively held file reports
  `ERR_FILE_LOCKED` and garbage reports `ERR_FILE_CORRUPT`, against the real `pypff` and a real
  `CreateFileW` hold. They skip off Windows and without libpff.
- [ ] **1e. The real Outlook case - owner-run.** With Outlook **running** and a `.pst`
  attached, run `venv\Scripts\python.exe -m app.cli extract "<that.pst>" --limit 50`.
  Expected: either libpff reads it (Outlook shares it - fine) or the log says
  `is held open; trying Outlook instead` and the messages arrive. Whichever it is, write the
  answer in the note above §0: it is the one fact this order could not measure.

## 2. A few bad messages cost a few messages

- [x] **2a. The conversion is inside the guard.** `_messages()` wraps fetch **and**
  `_to_document`. *Honest scope:* the accessors were already individually guarded, so this
  closes the builder call and anything a future accessor adds - not a case that was known to
  fire. The test breaks `build_email_document` to prove the walk continues.
- [x] **2b. A folder that cannot be counted or listed is recorded**, not shrugged off, and
  `_walk_folders` reports through the same `_Report`.
- [x] **2c. Nothing readable is corruption, not "no text".** If every message failed, the
  archive raises `ERR_FILE_CORRUPT` naming the first failures, instead of the pipeline
  reporting `ERR_NO_TEXT_LAYER` and sending the owner to the wrong place.

## 3. Say what was skipped

- [x] **3a. `ERR_PST_PARTIAL`** (new, appended to `ERROR_REGISTRY`): "Only part of '{path}'
  could be read: 3 messages and 1 folder could not be read (412 were)." It rides as a warning on
  the archive's last message (`with_closing_warning`), so the existing `warned_by_code` counts
  it. Holding one message back is the whole cost.
- [x] **3b. Outlook's skipped folders join the same warning.** `_busy_warning` drains
  `_BUSY_FOLDERS`, which nothing but the CLI ever read.
- [x] **3c. The run summary says it.** `app/cli/index.py` prints a `Partial` line, in the
  shape of the existing `Pictures` one.
- [ ] **3d. The Indexing tab does not show it yet.** `warned_by_code` is persisted in
  `last_run_stats` but only the CLI reads it. Wire the count into the tab's summary, with a
  pytest-qt scenario (`WORKORDER-CONVENTIONS.md` §5b). Not started: the tab's summary layout
  needs reading first.
- [ ] **3e. A warning on an *unchanged* last message is lost.** Found while building this.
  `_consume` skips a message that is already current (`pipeline.py:2512`) before the warning
  loop in `_write_one` (`pipeline.py:3472`). So on an incremental run whose last message has not
  changed, `ERR_PST_PARTIAL` is not counted (the per-message log lines still are). Fix by
  counting a document's warnings before the `_already_current` check. Not done: it is inside
  the consumer, which is load-bearing, and wants its own test.

## 4. Retry what was missed - needs a decision

- [ ] **4a. Should a partial read be retried on the next pass?** Today an archive that finishes
  with skips gets its "unchanged" marker like a clean one, and only `--force` (or Outlook
  touching the file, which moves its mtime) re-reads it. For a transient cause (Outlook was
  busy on a folder) a retry is right; for real damage it re-reads the whole archive every run
  to skip the same messages. **Owner's call.** If yes: withhold the marker when a partial
  warning was raised *and* the cause was `ERR_OUTLOOK_BUSY`, never for damage.

## 5. Considered and not built

Recorded so the next session does not re-derive them.

- **Retry through `open_file_object`.** Measured (§0): no gain.
- **Copy the archive to a temp file and read the copy.** A copy needs the same read access
  that just failed, and a shadow copy (VSS) needs administrator rights. Neither helps the
  exclusive-hold case, which is the only one that fails.
- **Orphan items.** `pypff` exposes `get_number_of_orphan_items` / `get_orphan_item`, which
  could recover messages from a damaged folder tree. **Not built, on two grounds:** orphans
  include data that was deleted and never unlinked, so recovering them would index mail the
  owner threw away (the reason `Deleted Items` is skipped by default), and there is no damaged
  archive here to test against. Revisit with a real damaged `.pst` and the owner's answer on
  deleted mail.
- **`_BUSY_FOLDERS` is process-global**, shared by every extraction worker. Two workers reading
  archives through Outlook at once could each take the other's folder list. Existing, and not
  made worse by this order; the fix is to key it by store. Open as **5a**:
  - [ ] **5a. Key `drain_busy_folders` by store path.**

## 6. Verification

- [x] **6a. Focused tests.** `tests/unit/test_pst_resilience.py` (26) plus the existing
  `test_pst_libpff.py`, `test_email_pst.py`, `test_errors.py` - green.
- [ ] **6b. The whole suite** - `venv\Scripts\python.exe scripts\run_suite.py -j 4`, compared
  against `HANDOFF.md`'s "Known red" families. Tick when the failures are all in those.
- [ ] **6c. A real damaged archive - owner-run.** Take a copy of a `.pst`, damage it (change a
  few bytes in the middle with a hex editor), index the copy. Expected: the run finishes, the
  readable messages are searchable, the `Partial` line appears, `logs\` names what was missed.
  This is the only test of "slightly corrupt" against a real file; every one above uses a fake
  archive, because nobody has a damaged PST to test with.
