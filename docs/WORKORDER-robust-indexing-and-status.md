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

> **2026-09-30, audited.** A1, A2 and A3 ticked; A4 carried. Read, and run on the Windows laptop:
> `tests/unit/test_file_state.py` and `tests/unit/test_status_column.py`, 46 passed.
>
> - **A1** `app/core/file_state.py` (`derive`, `explain`): the eleven words, each with its sentence,
>   no Qt. Discovered and Reading are live numbers only. **Duplicate has its word and sentence and
>   is never shown on a row**, because the store records no duplicate: identical loose files are
>   each read, and a repeated attachment inside an archive gets no row (it is counted on the
>   archive's line, C4). `DUPLICATE_CODES` is empty, and is where a code goes if that changes.
> - **A2** Search: `tasks.result_statuses`, one `store.file_states` read for the page, on the worker
>   that decorates it; the word is drawn beside the date. Files and Mail: `presenter/rows.py`, from
>   the status and skip code the row already carries. Code: `presenter/code.py`.
> - **A3** `widgets/status_funnel.py`, `tasks.status_funnel_counts`, `SqliteStore.status_counts`.
>   Three indexed statements, not the one grouped query the item names: one `GROUP BY` over status,
>   code and volume reads every row of `files`, and
>   `test_the_funnel_statements_use_the_indexes_they_claim` holds the three plans. **Fixed in this
>   audit:** with "Index in a separate process" on, the line stood still until the run ended; it
>   now reads through the window's own store (`ChildIndexRun.read_store`).
> - **A4, not ticked.** Mail and Files are built (`presenter/rows.capped_total`,
>   `tasks.browse_messages_page`, `tasks.browse_files_page`): "Showing 500 of 12,431 messages",
>   counted to 100,000, and "Showing 200 of 3,412 files", counted to 10,000. **Search is missing**:
>   its line reads "N result(s)" with no total (`presenter/search.status_line`). What would close
>   it: the owner's answer to what the total of a ranked list counts (files holding the words, or
>   also those found by meaning), then a bounded count on the search worker and the same sentence.

- [x] **A1** One Qt-free vocabulary of single words - Discovered, Queued, Reading, Indexed, Skipped,
      Failed, TimedOut, Offline, NameOnly, Duplicate, Deferred - derived from what the store already
      records (status, skip code, volume state), each with a one-sentence explanation.
- [x] **A2** A Status column in every results list (Search, Files, Mail, Code), with the explanation
      as its tooltip. No per-row query on the interface thread.
- [x] **A3** A live funnel on the Indexing page: the count of each status, from one grouped query on
      a worker.
- [ ] **A4** A list capped at a limit says so with the total: "Showing 500 of 12,431 messages".
      Mail first (the owner's report), then Files and Search.

## Lane B - a time limit on every file

> **2026-09-30, audited.** B1, B2 and B3 ticked. Read, and run on the Windows laptop:
> `tests/unit/test_file_watch.py` and `tests/unit/test_force_skip_button.py`, 29 passed.
>
> - **B1** `app/index/file_watch.py` (`limit_kind`, `Watchdog._verdict`): text and code 120 s ("Time
>   limit per file"); other single documents ten times that; `.pst`, `.ost`, `.mbox`, `.olm` and
>   `.zip` no total limit, and 600 s of no progress ("Skip a mailbox or archive after no progress
>   for"); video and recordings none. **Fixed in this audit:** "no progress" did not look at
>   `Frame.beat` (C3), so a `.pst` working through one message's attachments that gave no document
>   could be cut off while it was working (`FileWatch.signature`).
> - **B2** the row is SKIPPED with `ERR_FILE_TIMEOUT` and reads TimedOut; a reader process is ended
>   (`ReaderProcess.kill_child`); an in-process reader is interrupted, or left behind and replaced
>   when it is inside native code (`Pipeline._replace_worker`); the next run leaves the file alone.
> - **B3** `widgets/indexing_workers.py`, `indexing_controls.force_skip_reader`, then
>   `Pipeline.force_skip` or `ChildIndexRun.force_skip` (`skip <reader>` to the indexing process;
>   `test_force_skip_through_the_indexing_process` runs a real one).
> - Not seen here: the real window, and a hung Outlook (COM) read (HANDOFF, "Time limits and Force
>   skip").
> - The 29 is a clean run; most runs here were not clean.
>   `test_a_hung_reader_process_is_ended_and_the_thread_moves_on` failed in five of seven runs of
>   its file and in six of seven runs on its own, while five other test runs shared the machine
>   (the processor read 87% to 100% busy whenever it was looked at). It fails the same way on the
>   code as it stood before this audit (three runs of three), so it is the load and not a change.
>   With its 1 s limit a fresh reader process takes longer than the limit to start, so the files
>   after the stuck one time out too: a reader process's start-up counts against its first file.
>   At the shipped 120 s that is nothing; on a busy machine this is not a reliable test.

- [x] **B1** A time limit per file, by kind; for mail archives a limit on *no progress*, never on
      total time.
- [x] **B2** A file past its limit is recorded as `ERR_FILE_TIMEOUT` (status TimedOut); its reader is
      ended (reader processes) or abandoned (in-process), and the run carries on.
- [x] **B3** "Force skip" on each worker line of the Indexing page, in-process and in the separate
      indexing process.

## Lane C - PST that is fast and keeps going (libpff)

> **2026-09-30, audited.** C2, C3, C4 and C5 ticked; C1 carried. Read, and run on the Windows
> laptop: `tests/unit/test_pst_robust.py`, `test_pst_libpff.py`, `test_pst_resilience.py` and
> `test_live_progress.py`, 106 passed. They run on a stand-in for libpff, as written; the real
> library (`pypff` 20231205) is installed here.
>
> - **C1, not ticked.** Measured once, on Linux, on the public 14 MB Enron sample (71 messages, 70
>   attachments; CHANGELOG): about 0.27 s a read with OCR taken out, 20-27 s with it, so OCR was
>   about 80% of the time, and `get_attachment` about 2 ms a call. **Not seen in this audit:** the
>   only `.pst` files on this machine are the owner's, and no command times the libpff path. What
>   would close it: the owner's archive on Windows (HANDOFF, "Your own `.pst` through libpff"),
>   written here as messages a second with its conditions.
> - **C2** `pst_libpff._attachments` (each attachment fetched once), `_readable_type` (a type nothing
>   reads is turned down by name, one answer per extension per archive), the hash taken before the
>   write (a duplicate is never written), `_Scratch` (one folder per archive). **No reader takes
>   bytes** - every one takes a path - so an attachment that is read still passes through one file.
>   Measured against its parent on Linux (CHANGELOG). Two of the three on this laptop, 300
>   attachments of 20 KB a round, best of five rounds, the processor about 93% busy with other test
>   runs: a folder per attachment 2.0 ms each against 1.5 ms in the one folder; a `.url` written
>   and handed to the reader 4.6 ms against 0.013 ms turned down by name. The third, one fetch
>   instead of two, needs a real archive.
> - **C3** `_each_attachment` catches everything around a reader; `_walk_folders` is iterative, with
>   loop, depth (64) and failures-in-a-row (200) guards; `_messages` has the same run-of-failures
>   guard and leaves a message it has already read; `frame.n` and `Frame.beat` move for every item.
> - **C4** `Frame.counts` on the reader's line (`presenter/live_progress.worker_lines`) and one line
>   when the archive ends (`Pipeline._after_container`, `presenter/activity.status_counts_text`).
>   **Built in this audit:** TimedOut was reserved and nothing set it, and an archive cut off by
>   the no-progress limit or a Force skip logged no line at all. It now logs one, with the item it
>   was cut off on: `2019.pst: 1 Indexed · 1 TimedOut`. A Stop or a Pause still logs nothing.
> - **C5** `app/extract/reading.py` and `app/index/held_archives.py`: held on the text pass, read by
>   the pictures pass, once. **libpff only:** through Outlook (MAPI) an attached picture is still
>   read on the text pass (HANDOFF says so).

- [ ] **C1** Measured: messages a second on the libpff path, and where the time goes.
- [x] **C2** Faster: attachments read without a temporary file where the reader can take bytes, and
      repeated property reads cached - each measured against its parent.
- [x] **C3** Reliable: one bad message or attachment costs only that item, with a reason; loop and
      cycle guards on corrupt folders; progress advances for every item, skipped ones included.
- [x] **C4** Per-message status inside PST progress, and at the end of each archive in the activity
      log: `Archive2019.pst: 12,400 Indexed · 3 Failed · 12 Duplicate`.
- [x] **C5** Attachment images wait for the pictures pass, exactly as loose images do.

## Lane D - the junk-image filter (after C)

> **2026-09-30, audited.** D1 to D5 ticked. Read, and run on the Windows laptop:
> `tests/unit/test_junk_images.py` and `tests/unit/test_phash.py`, 60 passed.
>
> - **D1** `junk_images.ImageBook`, kept in `image_hashes` (schema v29) by
>   `app/index/image_book.py`: five sightings and fewer than three words, across archives and runs.
>   Duplicate attachments: within one archive, as the note below decides (`seen_hashes` in
>   `pst_libpff._each_attachment`).
> - **D2** `junk_images.is_inline` and `decorative`; the reason is on the archive's line in the log
>   ("12 decorative pictures") and the count is the row "Pictures in mail not read" on the Indexing
>   page. **libpff only:** the Outlook (MAPI) reader does not see the inline marks, so D2 does not
>   run there; D1, D3 and D4 do (`test_the_outlook_path_applies_d1_and_d3_but_never_d2`).
> - **D3** `junk_images.settle` and `count_words`. **D4** `perceptual_hash` and
>   `ImageBook.looks_like_repeated_logo`, with the hash size of `app/index/phash.py`. **D5**
>   `INDEX_JUNK_IMAGE_FILTER`, "Leave out signature logos and icons in email"
>   (`widgets/long_run_box.py`).
> - Not seen here: a real signature in a modern `.pst`, and the Outlook (MAPI) reader (HANDOFF,
>   "Signature pictures in your own mail").

> **2026-09-29, decided (owner: "you decide"; built in PR #31).** "Duplicate attachments are
> not indexed twice" stays **within one archive**, as before. Across archives it could lose an
> attachment when the archive holding its first copy is re-read or deleted, and the saving is small
> next to D1's. Revisit only with a way to move the kept copy when its archive goes.

- [x] **D1** One hash list for images across every archive and run: an image seen five or more times
      that gave fewer than three words is not read again, and duplicate attachments are not indexed
      twice.
- [x] **D2** Inline, hidden or `cid:` attachments that are also tiny or divider-shaped are recorded by
      name only, with a reason ("decorative image, not read") and a count on the Indexing page.
> **2026-09-29, owner decision.** D3 applies to photos too: short text such as a sign in a photo
> is not kept. Asked because the Enron sample lost one real sign ("ASTEL Heaven") to D3; answer "no"
> to keeping it.

- [x] **D3** Fewer than three words after OCR: the text is not indexed and the hash joins D1.
- [x] **D4** Near-identical logos, by perceptual hash (`app/index/phash.py`).
- [x] **D5** A setting to switch the filter off.

## Lane E - the order things are read in

> **2026-09-30, audited.** E1 to E4 ticked. Read, and run on the Windows laptop:
> `tests/unit/test_read_order.py` and `tests/unit/test_read_order_ui.py`, 27 passed.
>
> - **E1** `app/index/read_order.py` (`sort_key`, `WorkList`) and `Pipeline._produce` /
>   `_read_in_order`. The pictures and media passes are not touched by it. **E2**
>   `widgets/roots_box.py`, kept as `ui:index_first_folders`; `priority_roots` in the window's run,
>   `--first` to the indexing process, and read by `app.cli index` when no `--first` is given. The
>   button is on the folder list (Settings › What's indexed), not on the Indexing page. **E3**
>   `INDEX_ORDER`, "Order files are read in"; `app.cli index --order newest|found`.
> - **E4, on Linux** (commit 3a4a070; 9,002 made-up files dated over 8 years, fake embedder, 2
>   readers, full speed, three runs, as found then newest first): first 1,000 files 13.5-17.5 s,
>   then 6.3-6.7 s; the newest 1,000 all read 67.9-71.2 s, then 6.5-6.9 s; whole run 68.4-72.2 s,
>   then 67.3-68.5 s; rerun with nothing changed 18.1-20.7 s, then 0.8-1.2 s.
> - **E4, on this Windows laptop** (i7-1365U, 12 logical processors, between 87% and 100% busy with
>   other test runs throughout, so treat every figure as indicative; the same shape of corpus,
>   9,002 text files, 21 MB, a fresh throwaway index each run). One run each, the code as merged on
>   2026-09-29: as found - first 1,000 files 19.5 s, the newest 1,000 all read 250.8 s, whole run
>   251.4 s, rerun with nothing changed 207.3 s; newest first - 38.4 s, 41.2 s, 278.4 s, 1.8 s.
> - **Found by that measurement, and fixed in this audit.** Both orders read about 35 files a second
>   whatever the number of readers, because the walker asked the resource governor before every
>   file and one ask costs 26-30 ms on this machine (531 processes; psutil's walk of the process
>   table for child processes) against the 1.76 ms of the Linux sandbox. The machine is now read at
>   most every 0.25 s (`Pipeline._governor_allows`); the person's Pause is still noticed on every
>   file. Two runs each with that change: as found - first 1,000 files 4.8 and 5.9 s, the newest
>   1,000 all read 54.3 and 55.3 s, whole run 54.7 and 56.0 s, rerun 1.9 and 2.0 s; newest first -
>   6.6 and 12.0 s, 7.2 and 12.9 s, 48.4 and 96.1 s (the slower run with the processor at 100%),
>   rerun 1.1 and 4.3 s.
> - Not seen here: the owner's real corpus and the real window (HANDOFF, "Newest first, and
>   "Index this folder first"").

- [x] **E1** Scan, then sort, then read: the owner's chosen folders first; then newest first, mail
      and files mixed; then small before large; pictures and media keep their later passes.
- [x] **E2** "Index this folder first" on the Indexing page, in order, honoured by the window, the
      command line and the separate indexing process.
- [x] **E3** A setting for the order: "newest first (mixed)" or "as found".
- [x] **E4** Measured: time until the first 1,000 files are searchable, total run time, and a
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
> **2026-09-30, F3 built.** A retry is an index run that walks nothing: its only candidates are
> one group of timed-out files, read back from the ledger with one statement on the skip index
> (`SqliteStore.timed_out_groups` / `timed_out_files`; `app/index/timed_out_retry.py`). A group
> is a file type, because the limit is by type. Each file in it is given N times its usual limit
> (4 unless asked otherwise) through the file watch, for that run only - no setting is written. A
> file that times out again stays TimedOut and its sentence says what it was given; one that has
> changed since is read with the usual limit; one that has gone is left to the clean-up. Command
> line first: `app.cli timed-out [TYPE]` lists them, and `app.cli index --retry-timed-out [TYPE]
> [--time-limit-factor N]` reads them again. Window: a "Timed-out files" panel on the Indexing
> page's Status shelf, one row per type with the button, for the window's own run and for "Index
> in a separate process", refused with a sentence while any run holds the run lock. A retry does
> not teach the tuner and is not followed by the images-pass notice. **Not covered:** timed-out
> files on an Offline Media drive - their rows are not paths on disk, and a rescan of that drive
> is what reads them. *UNVERIFIED on the real window* (built and tested offscreen).

- [x] **F3** "Retry with a longer time limit" on a group of timed-out files (needs B).

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
