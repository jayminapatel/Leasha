# Work order (One thread, with helpers): robust indexing, one status everywhere, and PST that keeps going

**Doc version:** 1.1 · **Updated:** 2026-09-29 · **Applies to:** app v0.3.3
**Created:** 2026-09-29 · **Layer:** L2/L3/L5 - `app/index/`, `app/extract/pst_libpff.py`, `app/core/`, `app/ui/`
**Thread:** A coordinating thread with helper threads in worktrees, one PR per lane, each merged by the owner
**Status:** RELEASED *(owner, 2026-09-29: "build it all")*

## Why

The owner, 2026-09-29, after reviewing two outside architecture proposals against Leasha:

> *"i want you do use multiple threads and build it all especially as the pst scanning is way slow
> it is running right now and is not reliable this has to have a robust design and in the results
> it must show the single word status engine and this should be visible in the results."*
>
> *"also when searching for mails the search displays maximum 500 but does not tell how much total"*

Choices the owner made in the same conversation:

- **Order:** mixed by date.
- **Where the status shows:** a results column, the Indexing page funnel, and inside PST progress.
- **PST:** read through **libpff**; it stalls or hangs, and it stops or errors.

What this order does **not** do, and why, is recorded in the review that preceded it and summarised
at the end.

## Lane A - one status vocabulary, visible everywhere

- [ ] **A1** One Qt-free vocabulary of single words - Discovered, Queued, Reading, Indexed, Skipped,
      Failed, TimedOut, Offline, NameOnly, Duplicate, Deferred - derived from what the store already
      records (status, skip code, volume state), each with a one-sentence explanation.
- [ ] **A2** A Status column in every results list (Search, Files, Mail, Code), with the explanation
      as its tooltip. No per-row query on the interface thread.
- [ ] **A3** A live funnel on the Indexing page: the count of each status, from one grouped query on
      a worker.
- [ ] **A4** A list capped at a limit says so with the total: "Showing 500 of 12,431 messages".
      Mail first (the owner's report), then Files and Search.

## Lane B - a time limit on every file

- [ ] **B1** A time limit per file, by kind; for mail archives a limit on *no progress*, never on
      total time.
- [ ] **B2** A file past its limit is recorded as `ERR_FILE_TIMEOUT` (status TimedOut); its reader is
      ended (reader processes) or abandoned (in-process), and the run carries on.
- [ ] **B3** "Force skip" on each worker line of the Indexing page, in-process and in the separate
      indexing process.

## Lane C - PST that is fast and keeps going (libpff)

- [ ] **C1** Measured: messages a second on the libpff path, and where the time goes.
- [ ] **C2** Faster: attachments read without a temporary file where the reader can take bytes, and
      repeated property reads cached - each measured against its parent.
- [ ] **C3** Reliable: one bad message or attachment costs only that item, with a reason; loop and
      cycle guards on corrupt folders; progress advances for every item, skipped ones included.
- [ ] **C4** Per-message status inside PST progress, and at the end of each archive in the activity
      log: `Archive2019.pst: 12,400 Indexed · 3 Failed · 12 Duplicate`.
- [ ] **C5** Attachment images wait for the pictures pass, exactly as loose images do.

## Lane D - the junk-image filter (after C)

> **2026-09-29, decided (owner: "you decide"; built in PR #31).** "Duplicate attachments are
> not indexed twice" stays **within one archive**, as before. Across archives it could lose an
> attachment when the archive holding its first copy is re-read or deleted, and the saving is small
> next to D1's. Revisit only with a way to move the kept copy when its archive goes.

- [ ] **D1** One hash list for images across every archive and run: an image seen five or more times
      that gave fewer than three words is not read again, and duplicate attachments are not indexed
      twice.
- [ ] **D2** Inline, hidden or `cid:` attachments that are also tiny or divider-shaped are recorded by
      name only, with a reason ("decorative image, not read") and a count on the Indexing page.
> **2026-09-29, owner decision.** D3 applies to photos too: short text such as a sign in a photo
> is not kept. Asked because the Enron sample lost one real sign ("ASTEL Heaven") to D3; answer "no"
> to keeping it.

- [ ] **D3** Fewer than three words after OCR: the text is not indexed and the hash joins D1.
- [ ] **D4** Near-identical logos, by perceptual hash (`app/index/phash.py`).
- [ ] **D5** A setting to switch the filter off.

## Lane E - the order things are read in

- [ ] **E1** Scan, then sort, then read: the owner's chosen folders first; then newest first, mail
      and files mixed; then small before large; pictures and media keep their later passes.
- [ ] **E2** "Index this folder first" on the Indexing page, in order, honoured by the window, the
      command line and the separate indexing process.
- [ ] **E3** A setting for the order: "newest first (mixed)" or "as found".
- [ ] **E4** Measured: time until the first 1,000 files are searchable, total run time, and a
      no-change rerun - before and after.

## Lane F - later, in this order

> **2026-09-30, F1 built.** Off by default, pending the owner's check on his own folders: the
> switch is "Index files as soon as they are saved" on the Indexing page's Schedule shelf
> (`INDEX_WATCH_FOLDERS`).
>
> *What was built.* `app/index/folder_watch.py` (Qt-free): a source per indexed folder, a buffer
> that waits for each path to go quiet (2 s; 15 s at most for a path that never does), and
> `BatchIndexer`, which asks the disk what is true and hands the files to the ordinary pipeline
> through a new `PipelineConfig.candidate_source` - the same classify, read, embed and write path
> as any run, no second indexer. On Windows the source is `ReadDirectoryChangesW` through pywin32
> (`app/core/osbridge/dirwatch.py`, one handle per folder, 64KB buffer); elsewhere, and for a
> folder Windows will not watch, the folder is listed and compared, never more than a tenth of
> the time. An overflow, a folder that came back, or more than 2,000 paths waiting under one
> folder becomes "look at the whole folder": a walk of it, with the clean-up pass limited to it
> (`PipelineConfig.prune_under`). A batch takes the index-run lock only while it writes and waits
> in the buffer while a run holds it, while on battery (the owner's setting) or after an error -
> kept, never dropped. `walker.PathRules` answers the walker's exclusions for one path; a test
> walks a tree both ways and fails if they differ. Command line first: `app.cli watch`. The window
> runs that command as a child process (`app/index/watch_child.py`, `app/ui/folder_watch.py`),
> so nothing is read or embedded in the window's process; closing the window ends it.
>
> *Decisions.* (1) Mailboxes (`.pst`, `.ost`, `.olm`, `.mbox`) are left to the ordinary run - one
> that Outlook has open changes every few seconds; a new one is recorded by name. (2) Cloud
> placeholders are never downloaded by the watch, whatever the folder's opt-in. (3) Folders
> marked Archive are not watched. (4) Starting the watch walks nothing: what changed while it was
> off is the ordinary run's job. (5) A watched batch does not replace "the last run" record, and
> skips the work that grows with the whole index (backlog catch-up, forced vector compaction,
> the completions file) - `PipelineConfig.light`.
>
> *Tests.* `tests/unit/test_folder_watch.py` (31: gathering, overflow, exclusions, the lock,
> battery, deletes, the real Windows notification), `tests/integration/
> test_folder_watch_acceptance.py` (9: a real file saved, edited, renamed and deleted through the
> real watcher and pipeline, both sources; folders; rescan; mailboxes; locked files),
> `tests/unit/test_cli_watch.py` (4, one starting the real child process),
> `tests/unit/test_folder_watch_ui.py` (13: the setting, the switch pressed in the real window,
> the child's supervision).
>
> *Measured* 2026-09-30 on the owner's laptop (12 logical processors, Windows 11), temporary
> folders and a temporary index, **with other test runs on the machine at the time** (system CPU
> 30% at the start, 100% at the end) and the fake embedding model, so the meaning model's own
> time is not in these figures: save to findable by words, Windows notifications, median 2.19 s
> over 11 files (2.14-2.24 s; 2.0 s of it is the quiet time, the batch itself 0.20 s); by
> comparison with a 2 s interval, 3.5 s - at the default 30 s interval, up to about half a
> minute. Idle cost of watching 4,000 folders holding 20,000 files: Windows notifications 0.06 s
> of processor time in 60 s (0.10% of one core), no memory growth; the whole `app.cli watch`
> process idle over 3,000 folders 0.016 s in 60 s, 134MB resident with no model loaded;
> comparison 7.9 s in 123 s (6.4% of one core, each listing 6-9 s on the loaded machine), 13MB.
>
> *Known cost, measured.* While the watch is on, a folder **above** an indexed folder cannot be
> renamed or moved (Windows answers "Access is denied"); the indexed folder itself can. The
> setting's help text says so. **Not verified:** the owner's real folders, a network share, a
> Google Drive or OneDrive folder, and the real model's time per file.

- [x] **F1** Watch the indexed folders for changes, so a file saved a moment ago can be found.
- [ ] **F2** Mail results grouped by conversation, with the parent message shown when only an
      attachment matched (overlaps order 0y §4c).
- [ ] **F3** "Retry with a longer time limit" on a group of timed-out files (needs B).

## Not in this order, by decision

A local model reading every query (non-negotiable 1); secret or personal-data scanning with
quarantine or redaction (the owner's 2026-08-28 decision "If we can index, we will index");
replacing duplicates with links (non-negotiable 10); skipping images by file name, by file size alone,
by position in an email, or by colour variance (they drop real content, or the text detector already
answers better); BLAKE3 (blake2b is already used).

## Definition of done

Every box ticked or carried with a dated note; each lane's PR with its tests, the suite compared
against `main`, a CHANGELOG entry and a HANDOFF Windows-checklist line; measurements with their
conditions.
