# Changelog

**Doc version:** 4.28 · **Updated:** 2026-09-27 · **Applies to:** app v0.3.3

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows the scheme in `docs/VERSIONING.md`.

> **Restructured 2026-08-30.** Thirty-six sections had been written *above* the
> `## [Unreleased]` heading, and above this preamble - outside the Keep a Changelog
> structure entirely. Run as it stood, step 7 of the release checklist would have
> promoted the older content under `[Unreleased]` to the new version and left the
> newest six hundred lines orphaned above it. Nothing was reworded; the heading and
> this preamble moved up so that everything unreleased now sits beneath them, and
> those thirty-six headings dropped from `##` to `###` so they nest under it rather
> than sitting beside it. Heading text is untouched.

## [Unreleased]

### The owner's feedback, 2026-09-27 - search that finds mail by year, and an index run you can watch

**Things that were wrong**

- **Switching pages while an index ran froze the whole window.** Every click on the rail
  saved "last page" to the database on the window's own thread. That save waited for the
  indexer to finish whatever it was writing. It now happens in the background, one save at a
  time and in order, and so do all the other remembered choices (theme, column widths,
  settings). In a test holding the indexer's lock, a page switch took 2 seconds before and
  under a tenth of a second after. A run started straight after you change a setting waits
  for that setting to be saved first.
- **"mail from 2017" found no 2017 mail.** Two reasons. Dates in a search compared each
  file's modified date, and every message in an Outlook archive carried the archive's own
  date. So a `.pst` touched last week made every letter in it "last week". Mail is now dated
  by when it was sent, for new and already-indexed mail alike (schema v27 fills it in on first
  open: 167 ms for 60,000 messages on a test index). And the words were only ever *offered*
  as filters beside the results. On the Search tab, "mail", a year, or a sender Leasha knows
  now become filters straight away, each shown as a chip you can remove. What you typed is
  never changed. "invoice 2017" keeps 2017 as a word, because a document's file date is
  often just the day it was copied.
- **The progress bar sat still for whole stretches of a run.** Getting the search model
  ready, working out which folders to read, catching up on the last run and tidying the index
  afterwards sent no progress at all. In those stretches the bar now shows it is busy, and
  one line says what is happening.

**New**

- **A live log on the Indexing page**, headed "What the run is doing". Every line has its
  time: each step, each large archive or video as it is opened, pauses and why, warnings, and
  how the run ended. Scroll up to read, and it stays where you left it. The run's notices
  now show the time they happened, and `app.cli index` prints the same lines.
- **A run that did not finish says so.** After a crash, a power cut or the app being ended,
  the Indexing page says when the last run stopped, roughly how far it had got, and that
  starting again carries on with nothing lost. A normal Stop or Pause is not reported this
  way. `app.cli stats` says the same.
- **A mail archive carries on where it stopped.** An interrupted `.pst` read by Leasha's own
  reader restarts at the folder it was in, not the first message. Its place is saved at least
  every 30 seconds. The page lists any archive a run stopped inside. Archives read through
  Outlook still start again from the top, skipping what is already indexed. Outlook does not
  promise the order of its folders, and attaching an archive changes its date.

### Start no longer invites a second click during a slow hardware detection

- Clicking Start while Leasha is still working out how many workers to use
  (a cold or invalidated hardware-profile cache) could, if the every-four-
  second background check for an index running elsewhere happened to land
  in that same window, briefly show the button as clickable again even
  though nothing had actually started yet. It now stays disabled for the
  whole detection, honestly, with no gap.

### The closing pass, 2026-09-20 - three bugs that said nothing, and a button that was missing

Every one of the first three was invisible: the wrong answer looked exactly like a right one,
which is why they survived so long and why each now has a guard that fails loudly.

**Silent failures, found by running the thing rather than reading it**

- **`/newest` and `/oldest` did nothing from the command line.** `app/cli/search.py` handed the
  raw query to the engine while every other caller - the window, the Code and Repos views, the
  evaluation - expanded slash commands first, so the sort parsed to empty and was dropped. You
  got relevance order and no warning. Found by checking the dates of what came back on the real
  index; fixed and verified there.
- **`--interpret` was accepted with `--builtin` and ignored.** Translation is what turns "the
  email from Chris" into `from:chris`. It now runs, and refuses loudly when Ollama cannot answer
  rather than quietly reporting untranslated numbers as translated ones.
- **The Chat ship floor was measured against no vectors at all.** `evaluate --chat` built its
  engine with keyword search only, so every question whose answer has to be found by meaning
  failed before the model was asked - and that was reported as the model's quality. Re-measured
  with the real stack: **84.0%, not 73.9%**. The 85% floor was NOT lowered to meet it. The
  report now prints which retrieval it used, because this repository had already made the same
  mistake once on the search evaluation.

**The crash that killed three test runs**

Not memory - that was the first answer and it was wrong. `QApplication.setStyleSheet` re-polishes
every widget alive in the process, and the suite leaks widgets, so the walk eventually reached one
already freed. Four sites fixed, a guard added for `app/` and `tests/`, and the application itself
was confirmed never to do it (it themes its window, not the application). A deferred call with no
owner is the same shape: `QTimer.singleShot` with a **lambda** fires into destroyed widgets while a
bound method is cancelled - measured, not assumed - so `app/ui/later.py` ties them to an owner. The
worst was chat **Stop**, which waits four seconds.

**New**

- **A Pause button on the Indexing page**, asked for by the owner. It holds the run; Stop still
  ends it. Because it runs through the governor as one more reason to be waiting, the page can say
  *which* pause is in force - yours, or the machine's. `app.cli index --pause-file PATH` for the
  command line.
- **`.doc` reads its WordArt** - DRAFT watermarks, signature banners, plant tag numbers: 39 of 388
  real documents gained 168 words that were invisible before. Text inside embedded spreadsheets and
  drawings is **counted and named** in the run summary rather than declined to LibreOffice, because
  LibreOffice cannot recover it either: declining cost seconds a file and returned the same words.
  Median 4.0 ms a file, down from 8.7 ms.
- **The extractor registry loads on first read**, not on import: 38 fewer modules before the window.

**Also**

- A results refresh no longer loses the selected row; an offline result keeps its drive; database
  waits are bounded; a folder chosen as an Offline Media source says what to choose instead.
- The suite runner sizes itself to free memory, and says so when an explicit `-j` will not fit.
- A hotkey test had asserted the non-Windows answer on Windows since it was written, failing every
  run on the one platform this ships on. It was a missing guard, not the environment.

### The outstanding-work pass, 2026-09-20 - what was fixed, built and decided

Twelve agents worked in parallel from the lists in `HANDOFF.md` and the order register; every
item below has a test that fails without it or a measured number. Orders carry the detail.

**Fixed**

- **`pump /oldest` (one symbol-shaped word plus `/oldest` or `/newest`) came back in relevance
  order with no notice.** An explicit date sort now wins.
- **A file-type browse (`type:pdf`, no words) took 80 ms at 200,000 files; it takes 1.4 ms.**
  A type with no matches stays instant; a rare type pays a small bounded probe first.
- **A worker that raised an exception could leave an index run polling for ever.** The run now
  finishes, counts the file as failed, and says so.
- **A stop in the middle of a batch waited for the whole batch.** It is bounded now.
- **Closing the main window did not end the app while a pop-out or log window was open.**
  Every window it owns now closes with it. (One cause of the "window closed, process ran for
  hours" report; the 2026-09-17 nine-hour case itself is still unexplained.)
- **A conversion whose LibreOffice was killed at shutdown started a fresh LibreOffice and held
  the worker for the file's whole timeout.** It fails at once now.
- **Results: the selected row was lost when the interim results were replaced by the full
  ones,** so the preview said "Nothing selected". A drive-backed (offline) result never carried
  its drive, so it previewed, opened and revealed as an ordinary missing file. Both fixed.
- **A database connection could be waited for without limit.** Bounded now, with a plain
  `ERR_DB_BUSY`. Choosing a folder as an Offline Media source says "Choose the drive itself,
  not a folder on it".
- **Search history cleared in Settings was still offered in the search box** (an unconnected
  signal). **The theme was applied twice at start-up; an F5 or a dropped file before the
  deferred pages existed was silently skipped; Escape with the find bar open did the wrong
  thing; the empty pinned panel took a third of the page; the rail clipped "Reports" at
  1024x600 and 125%.** All fixed. A stray timer on a destroyed results view is now held weakly.
- **Building 2,000 result rows: 231,087 text measurements became 32,541** (about 2.6x faster in
  the measured build), and a page paints about 27% faster than before the redesign.
- **The Space Report said "No duplicate files were found." when nothing had been compared; the
  inheritance map dropped each drive's description; a deep timeline page cost 22x a first page.**
- **`.numbers` dropped every table's strings after the first; `.xlsx` stripped `x005F_` from
  inline strings; short videos returned no pictures; the CLI crashed printing text a cp1252
  console cannot draw; the face-duplicate threshold was wrong on real faces; conversations never
  saved to the real store; the Chat roles grid reset a saved model name.** All fixed.
- **The `.key` and `.pub` converter rules used a Writer filter and failed 8 of 8 files each**
  (the same mistake the `.ppt` rule had). Both now read in-process.

**Built**

- **The Life Timeline** - Reports, "Browse your timeline" (and `leasha timeline`): year, month,
  photos as a band, everything else as rows, offline items badged, from any result's date.
- **Chat is a conversation.** Local sources first, real multi-turn memory, streamed replies,
  rendered markdown, copy / regenerate / edit-and-resend, a plain persona; a question your
  files cannot answer says so and gives a separate "Not from your files" answer. **Optional web
  augmentation, off by default** (a scope exception recorded in `PROJECT_INSTRUCTIONS.md`):
  the web is only asked when the files come up thin or you ask, only a short visible query is
  sent, never a file name or passage; Wikipedia works from here, the other providers are
  marked unverified.
- **Video and audio** read through PyAV (no ffmpeg install), queued as a background backlog
  after everything else, open at the matching moment in VLC / mpv / MPC-HC / PotPlayer, with
  per-frame pictures and faces on frames. Off by default (about 12x real time on the processor
  for speech; a 46 GB video folder is hours), each switch in Settings.
- **Read in-process, in milliseconds instead of seconds:** `.doc` (3 ms) and `.ppt` (11 ms)
  against 5-10 s through a cold LibreOffice, each falling back to LibreOffice whenever the file's
  own text totals disagree with what was read; `.pub`, `.key`, `.pages`, `.numbers` (previously
  failing or 6-13 s); `.mobi`/`.azw` (never indexed before); faster `.docx`, `.pptx`, `.xlsx`.
  LibreOffice is now a warm, crash-limited fallback with a private profile, no OS error box and
  no orphan processes. `docs/EXTRACTION_SPEED.md` has the measured table.
- **The window's index run lowers its own threads, not the whole process** (merged from the
  UI-responsiveness branch), plus a UI lag monitor. The command line still lowers the process.
- **Test automation:** real-app UI journeys, an order-by-order scenario coverage table, a
  measured nightly scale run with pinned floors, and an opt-in scheduled-task installer
  (`scripts\install-nightly.ps1 -WhatIf`, never registered by us).

**Decided (owner delegated; each is recorded in its order)**

- Packaging: PyInstaller one-folder, per-user, the installer asks where the index goes, no update
  check inside the app, Windows 11 and 10 22H2, unsigned until the repository is public.
- **The PySide6 migration must come before any packaged release** (PyQt6 6.11.0's own metadata
  reads GPL-3.0-only; the project is MIT).
- A partial PST read is retried on the next pass only when the cause was transient.
- Chat stays RELEASED; the Life Timeline hold is lifted; a folder is not a valid Offline Media
  source; video/audio stay off by default.

### Docs

- `ACTIVE_WORK.md` retired as a tracker (the register is the tracker); the 0q session handoff is
  renamed out of `WORKORDER-*` so its checkboxes stop counting as open orders; five orders'
  status lines were backfilled and four register rows recounted; `PROJECT_INSTRUCTIONS.md`,
  `README.md`, `docs/THIRD_PARTY_NOTICES.md` and `docs/EXTRACTION_SPEED.md` added or updated.

### The window, looked at properly - text in its box, room at the edges

Found by grabbing the real window on Windows at 125% scaling, which the offscreen
captures had never shown. Order `202626160950` has the six findings in full.

- **The indexing button on the left now shows its words.** "Up to date" read "p to dat";
  it now wraps onto two lines. "Needs attention" and "Indexing" fit too.
- **The notification box at the bottom matches the theme.** Its text sat in a mismatched
  box (light in the dark theme, dark in the light one) and its dot was invisible on the
  light theme.
- **No more grey boxes behind labels.** Every settings row, the Tuning page and the toast
  had one.
- **Search results are no longer cut off at the right** when the preview is open, and the
  sideways scrollbar under them is gone.
- **Room at the edges**: the Indexing page (the stats card and "Reset index..." ran to the
  window edge), inside Chat, and the Offline Media page's spread-out text. The black focus
  box round list items is now the theme's own ring.
- **Taskbar**: the running button already showed the icon. Windows is now also told which
  icon and command a *pinned* Leasha should use; unpin it and pin it again to pick it up.
- `tools/bench_results_paint.py`, for the order's last open measurement.

### PST resilience - an archive that is held open, or slightly damaged

Work order `pst-resilience`. Most of it is on the direct (libpff) path, where the failures were.

- **A `.pst` held open by another program is now a lock, not "corrupt".** It was reported as
  `ERR_FILE_CORRUPT`, which is never retried, so the archive dropped out of the index for good.
  It is now `ERR_FILE_LOCKED`, retried on every pass. Told apart by the message libpff gives,
  measured on Windows 11 with `pypff` 20231205.
- **With the backend on Auto, a held archive is read through Outlook instead.** Only before the
  first message was read (a later fallback would index it twice), and never when libpff was
  chosen by hand. If Outlook is not there, the lock is what gets reported.
- **A message that fails costs that message.** Conversion is inside the same guard as the fetch,
  a folder whose messages cannot be counted is recorded rather than skipped silently, and an
  archive where nothing reads is reported as damaged rather than as "no text".
- **What was skipped is now counted.** New `ERR_PST_PARTIAL` warning, the CLI run summary's
  `Partial` line, and Outlook's skipped folders (which nothing in the app ever read) folded
  into the same warning.
- Not yet: the Indexing tab does not show the count, and a warning on an unchanged last
  message is not counted on an incremental run. Both are open items in the order.

### Docs

- `HANDOFF.md` 6.8, `ORDER_REGISTER.md` 1.36 (row 0v), new `WORKORDER-pst-resilience.md` 1.0,
  `WORKORDER-202626160950-ui-redesign.md` 1.4 (the 2026-09-20 note).

## [0.3.3] - 2026-09-19

### Indexing that works - why a full index said "55 days", and what was done about it

The Indexing tab reported about 55 days for 114,614 files. The estimate was honest;
the speed was not. Found by running the indexer on 90 real documents, not by reading it.

- **The embedding model was given one processor thread instead of four.** The tuning
  arithmetic charged each of four extraction workers a whole core, though they were busy
  about 6% of the time, while embedding was 87% of a real run. Automatic now gives the
  model 4 threads on a machine like this one. Every 4-thread test run beat the 1-thread
  run (222-587 s against 1,055 s for the same 90 documents); this laptop varies about
  2.5x run to run, so treat the gain as "roughly 2x to 5x", not a promise.
- **A benchmark that timed nothing chose a 512-passage batch.** It timed a generator it
  never ran, stored a rate of 106,666,662 a second, and the tuning code read that as "fast
  enough to double the batch". An impossible rate is now ignored when read, including the
  one already stored. One model call on a processor is capped at 32 passages: about 10%
  faster and a third of the memory (4.4 GB down to 1.25 GB).
- **Every old `.ppt` presentation was skipped** (79 of 79 in one index). LibreOffice was
  asked for a plain-text export that only word processors have. Presentations are now
  converted to `.pptx` and read like any other. **Files already recorded as skipped need
  one `app.cli index --retry-skipped`** - a settled skip is never retried on its own.
- **Closing the window left the indexing running.** The window said it had closed in
  0.0 s and the run carried on with no window - for minutes once, for nine hours another
  time. Closing now waits for the run, and a stopped run stops at its next file. As a
  backstop, a process still alive 30 s after a close writes every thread's stack to the
  log, and one still alive after 300 s is ended.
- **The graphics-card build of onnxruntime could be replaced by the processor build**
  by the documented face-detection install command, which left `onnxruntime` unpinned.
  Pinned. (On this machine's integrated graphics DirectML measured no faster than the
  processor, so nothing was lost - but it was silent.)
- **The int8 embedding model is now on** for this install (`EMBED_QUANTISED`): 1.85x
  faster, and it changes which passages come out nearest, so its vectors must never be
  mixed with the standard model's - going back means a full rebuild.

Known, not fixed: why the window's event loop stays alive after a close while a run is in
progress (the next occurrence will log its stacks); a stop that arrives mid-batch still
waits for that batch; and `main` is **not green** - the whole suite was not run for this
release (far too slow to finish in a session), and at least 3 archive-path tests, 1 in
`test_speed_work` and 2 in `test_converter_discovery` fail identically on the commit
before this work. Work order 0u records all of it.


### The window has a new shape - one box to start, a rail down the side

- Open Leasha and there is one thing on screen: a box that asks what you
  are looking for, with a few example searches under it and your recent
  ones. Start typing and it becomes the results page. The eight tabs
  across the top are gone; the pages are a column of icons down the left,
  with Settings at the bottom and a small indexing pill that shows
  progress without you having to go and look.
- Every filter you type (`/type pdf`, `/from dave`, `/newest`) appears as a
  chip under the box; click its × to take it out of the search.
- Results show what kind of thing each one is as a coloured badge - blue
  for documents, orange for mail, green for code - and the words that
  matched are marked on a tinted ground rather than only in bold. The
  preview pane now leads with the facts about the file (kind, date,
  folder, page) and offers "Show in folder" beside "Open".
- Messages that used to appear in a strip along the bottom now appear as
  a short notice over the results, one at a time, and are read out to a
  screen reader. There is a menu bar - File, Edit, View, Go, Help - that
  lists every keyboard shortcut. "Animate panels when they open and close"
  is a new Appearance switch, off unless you turn it on.
- Under the hood: the same layout works unchanged on macOS (native title
  bar, no Windows-only chrome, `Ctrl` reads as `⌘`), icons are from Lucide
  (ISC) with the licence beside them, and nothing that used to say
  something says it differently. Work order 202626160950; `tools/grab_ui.py`
  (0m §0) captures every page to PNG for review.

### The Space Report on the Reports page, and the idle-time benchmark

- The Space Report now appears on the Reports page beside Digital
  Inheritance, reads the same as `leasha report space`, and exports to
  PDF. (`WORKORDER-space-report-and-idle-tune-ui-wiring` §1)
- The first time the computer is idle and plugged in with nothing
  indexing, Leasha times it once and moves Index Tuning from Defaults to
  Auto on its own, then says so. It never does this while a run is in
  progress, never on battery, and never over a Manual or Auto choice you
  already made. (§2; order 0b §5e)

### A new report shows what's taking up space twice, and what only exists once

- The Space Report, next to the Digital Inheritance map, tells you two
  things about your files: how much room you'd get back by keeping one
  copy of everything that's duplicated across your drives, with the
  biggest duplicates named - and which files exist on only one source,
  headline first: "372 files exist nowhere else but 'Old WD'."
- The second half is the one worth reading even if you never read
  another report: if that one drive is lost, so is that content, and now
  you know before it happens rather than after.

### A new install tunes itself, quietly, the first time the computer is idle

- You never have to find the tuning screen for Leasha to run well on your
  machine. The first time it's idle - not while you're indexing, not on
  battery - Leasha times itself once and switches from its cautious
  defaults to settings tuned for your computer specifically. No question,
  no dialog - just a line afterwards saying it happened.
- Choosing Manual yourself is never overridden - this only ever upgrades
  the starting point, never a choice you've actually made.

### A file you can already search by word now says so while it's still learning what it means

- Between a file being read and Leasha finishing work out what it's about
  (a step that runs a little behind extraction), it used to be listed as
  "not indexed yet" - which was never true; it was already findable by
  word. It now says, plainly, that it's findable by word and still
  learning what it means.

### When meaning-based search comes up empty, it now says whether that's temporary

- If a search finds word matches but nothing by meaning, and your index
  is still partway through learning what your files mean, Leasha now says
  so - "still embedding, 60% done" - instead of leaving you to wonder
  whether something is broken.

### Leasha can now write out a plain map of everything it knows about, for someone who isn't you

- A new Reports section (its own tab, not a search) holds the Digital
  Inheritance report - one document listing every source Leasha has
  indexed, local folders and catalogued drives alike, with names, sizes,
  what's in each, and where a drive was last seen if it's offline.
- Export it as a printable PDF. Choose which sources go in first - a
  source can be left out of the map entirely, private even from this.
- Every report states the moment it was drawn from, honestly: "from the
  index as of last run, <date>".
- Nothing here is ever written back to the index or to your files - a
  report only ever reads.

### The Photo Tagger now asks, instead of only ever guessing silently or not at all

- When Leasha isn't quite sure a new photo is someone you've already named,
  it now asks - a small "Is this Daddy?" chip with the photo, right there
  on the Photo Tagger page. Say yes and it joins that person; say no and
  it goes back to being unsorted, and Leasha won't guess that one again.
- A confident match still joins automatically, exactly as before - this is
  only for the ones in between.

### A search that also names the file wins the tie - and the slower second-pass ranking step is off until it has earned its place

- If a word in your search also sits in a file's own name - "the pump
  station drawings" now favours `pump-station-drawings.pdf` over a report
  that only mentions pumps in passing - that file is nudged slightly ahead.
  It only ever breaks a near-tie: a much better match elsewhere still wins.
- Reranking - the extra pass that re-orders the best few results - is now
  off by default. Measured against a 300ms search budget it was costing
  seconds rather than milliseconds; turn it back on any time from the
  search bar's own switch or Settings if you would rather wait for it.

### A file stored online-only by OneDrive or Google Drive is now findable, never silently missing or silently downloaded

- Indexing a folder synced by OneDrive Files On-Demand or Google Drive for
  Desktop no longer leaves online-only files invisible. Each one now gets a
  row by its name, folder and date - findable and, if you ask why it has no
  content, told plainly that it is stored online only, with the fix named.
- Its content is still never read without asking - opening it is still what
  downloads it, exactly as before.
- Open a file so Windows fetches it for real, and the next indexing run
  reads its contents automatically - no re-scan, no setting to remember.
  Windows freeing the space back up afterwards costs nothing already
  found: the content stays searchable.
- A catalogued removable drive or network share can now be detached into an
  archived record - a name and a free-text location, such as "LTO-7 tape
  B-0042, fire safe" - once it is written off and put away. The catalogue
  stays searchable forever; nothing on the source itself is touched, and
  nothing already indexed is affected.
- Rescanning a network share whose server was renamed or moved can now be
  told it is the same source, rather than being catalogued a second time -
  `app.cli offline-media --scan ... --same-as "Old Name"` - and a Scan at a
  new, unrecognised address is offered the match if its top-level folders
  look like an already-catalogued source, without ever assuming so on its
  own.
- A drive or share can be marked as a sequential medium (a tape) at Scan
  time, so a content scan of it says plainly that it reads end to end
  before starting.

### The Offline Media tab: catalogue a drive once, find it forever - and the /on operator

- New **Offline Media** tab: every catalogued drive, its status (online as
  its current letter, or last seen, or locked), size, file count and last
  Scan date. Three verbs and nothing else - **Scan**, **Rescan**, **Delete**
  - matching the order's fully-manual model: nothing about a drive is ever
    touched without one of these three being pressed.
- The first Scan of a new drive asks for a name and an optional
  description; Delete states exactly how many files it will remove from
  the index and the sentence that matters - *"This removes the catalogue
  from Leasha's index. Nothing on the drive itself is touched."*
- A BitLocker-locked drive now reads as **Locked**, not as simply
  unplugged.
- Opening a search result on a catalogued drive that is currently plugged
  in now works - it used to try to open the drive's internal, letter-free
  storage key directly and report the file missing. An offline one now
  says which drive it is on and when it was last scanned, in its tooltip.
- New search filter: `on:`/`volume:`/`drive:` (aliases of one operator),
  the same shape `repo:` already has - `on:"Projects 2019"` restricts a
  search to one catalogued source, `-on:` excludes it. Offered on every
  tab.

### A network share now catalogues the same way a drive does, and never hangs when it is offline

- `app.cli offline-media` now catalogues a network share by the share
  itself, not by whichever drive letter it happens to be mapped to today -
  the same identity guarantee a removable drive already had.
- Scanning a share that is not reachable right now - the server is off, or
  Windows has not signed in to it - says so plainly within a few seconds,
  rather than sitting for however long Windows itself would wait before
  giving up.
- A share scanned before a server is retired stays searchable for as long
  as the index exists, exactly like an unplugged drive.

### A removable drive can now be catalogued once and found forever, even unplugged

- `app.cli offline-media` catalogues a removable drive by its own identity -
  never the letter it happens to be plugged in as, which Windows changes on
  its own. Scan a drive once, give it a name, and every file on it stays
  findable by what it says, whether the drive is plugged in or sitting in a
  drawer.
- Unplugging a catalogued drive never removes what was found on it. Only
  the Offline Media tab's own Delete does that, and it says exactly how
  many files it is removing from the index before it touches anything -
  the drive itself is never written to.
- A drive tidied up - files moved to new folders - rescans without
  re-reading what has not changed: Leasha notices the same file at a new
  location and repairs its record rather than reading it again.
- The tab this belongs to, and the rest of what the drive-in-a-drawer order
  promises, are not built yet - this is the storage layer and the command
  line underneath them, first.

### Added: Florence-2 tags and a caption for photos with no readable text (order 0i, items 1a/1b)

- **A photo-class image - the OCR ladder's "no text found" half - now gets one Florence-2 pass**
  instead of being left unsearchable: a brief caption and object tags, written as a labelled
  "AI description" segment through the existing chunk/FTS/embed pipeline, exactly parallel to
  how OCR's own "Text read from the image" segment already works. Document-class images
  (the ladder found text) are untouched - they still go through the specialist OCR engine only.
- **torch and transformers were not installed**, despite the work order's own text assuming
  they were. Installed for real (`torch==2.14.0+cpu`, `transformers==4.49.0` - pinned below
  5.0 after the first version tried broke Florence-2's own modeling code with a real,
  reproduced `AttributeError`), plus `einops`/`timm`, which Florence-2's remote code needs and
  which the order never mentioned. See `requirements.txt`'s "Optional: Florence-2 photo
  tagging" section for the full reasoning, and the dated note on 0i's §1 for the honestly
  measured CPU budget (11.36s/image against the order's ~100-300ms target - a gap the order's
  own text anticipated with "the thread MAY swap to an ONNX port later for speed").
- Soft dependency throughout: a machine without torch/transformers gets exactly today's
  behaviour (an untagged photo-class image is simply skipped, as before).

### Added: `/shows` - Florence-2's tag vocabulary, browsable and filterable (order 0i, item 1c)

- **New `file_tags` table (schema v19)**, one row per (file, tag), populated whenever a
  Florence-2 pass (0i 1a/1b) produces tags for a photo. `distinct_values`/`distinct_value_counts`
  can now answer `/shows` with real, counted values the way `/repo` and `/type` already do -
  free text inside a chunk cannot be grouped or counted cheaply behind a keystroke, which is
  why this needed a real table rather than reading the indexed words back out.
- `shows:dog,cat` and `-shows:dog` both work, matching `repo:`'s own comma-separated, ORed
  grammar - deliberate consistency rather than a special case for tags.
- Found and fixed along the way: `SqliteStore.set_file_tags` did not deduplicate
  (`["Dog","Park","dog"]` stored three rows, not two), and `ParsedQuery.has_filters` did not
  know about `shows`, so a `/shows` search would have under-reported whether a filter was
  active. Both fixed, both covered by `tests/unit/test_photo_tags.py`.

### Added: one unified enrichment-backlog mechanism, per-kind counts in the run summary (order 0i, item 2a)

- **`Pipeline._run_enrichment_drains`** is now the one place every backlog kind registers
  through, with `IndexStats.enrichment_counts` (present even at zero, so "ran, found nothing"
  reads differently from "did not run") and a new `Backlog` line in `app.cli index`'s summary
  ("filled 214 vector(s) repaired" etc, per kind).
- **The order's own text claimed three existing ad-hoc drains; only one was real.**
  `unembedded_chunk` migrated for real (its internal logic untouched, only the call site moved
  into the new loop - nothing that already pinned it through `Pipeline.run()` had to change).
  `ocr_pending` already existed but as a requeue woven into the walker (`_no_text_layer_
  candidates`/`_candidates`), not a drain - left in that shape rather than restructured, and
  made visible with an additive count instead. `image_tag` ("untagged images") did not exist in
  any form and is not built this session - what should count as "untagged" is a real product
  question the order does not answer.
- **Found and fixed a real, pre-existing bug along the way**: `_drain_unembedded` measured
  `len(pending)` *after* calling `_embed_pending`, which clears its own argument in place for
  its normal caller's benefit - so `stats.vectors_repaired` has silently reported 0 for every
  real repair since the function was written. The repair itself always worked; only its own
  count was wrong. Fixed by capturing the length before the call.

### Added: the enrichment backlog respects battery/CPU pacing like an ordinary run (order 0i, item 2b)

- The unembedded-chunk repair now calls the resource governor's `wait_while_throttled` at every
  batch boundary, the same call the ordinary indexing loop already makes per file - so it will
  not run a laptop's fan flat out just because it is "only" a repair pass.
- Not built: a dedicated idle-only trigger that runs enrichment with no index run active at all.
  No such infrastructure exists anywhere in this codebase yet; a configured scheduled index
  already delivers the "smarter while you sleep" story for every drain, since a scheduled run
  is still a run - a trigger that runs enrichment *without* one is a real design question this
  order's text does not answer, left open rather than guessed at.

### Added: folder-year era hints for photos with no EXIF at all (order 0i, item 4b)

- **A scanned print with no EXIF now gets a date guess from its folder or file name**
  ("Diwali 2004", "Summer_1999") instead of falling straight to the file's copy-date mtime -
  `app/extract/era_hints.py`, ranked between EXIF (a fact) and mtime (the fallback of last
  resort). New `files.taken_at_is_hint` column (schema v20) records which kind a date is, so
  work order 0512's future batch-era override can find and override only the guesses.
- "Takeout sidecar" date reading, named in this item's own ranking text, does not exist
  anywhere in this codebase - checked, not assumed. The ranking built is EXIF > era hint > mtime.
- Found while verifying: a "no text" photo can take either write path (`_write_one` or
  `_record_skip`) depending on whether Florence-2 tagging (0i 1a/1b) succeeds on it - both now
  correctly compute and store the era hint either way.
- Also fixed two hardcoded `CURRENT_VERSION == 18` assertions in `test_exif_date_wiring.py`
  that this item's new migrations (v19, v20) broke - the same trap its sibling
  `test_phash_column_migration.py` already documents and fixes the same way.
- A separate finding recorded for the record: this machine's `ResourceLimits.pause_on_battery`
  defaults to `True`, and on battery power any real `Pipeline.run()` test with no override
  blocks indefinitely - a pre-existing, whole-suite fragility, not fixed here, but worth knowing
  before mistaking a slow real-model test for a hang.

### Added: `/place` - offline reverse geocoding from a photo's EXIF GPS (order 0i, item 4a)

- **A photo's GPS coordinates now resolve to a real place name** ("London", "Leeds") entirely
  offline, from a dataset bundled inside `reverse_geocoder` - new `app/extract/exif.py::read_gps`
  and `app/extract/places.py::reverse_geocode`, a new `files.place` column (schema v21), and a
  `/place` operator (aliases `/near`, `/location`) with real, counted values.
- **Measured, not assumed: the package's own convenience function was the wrong call.**
  `reverse_geocoder.search()` defaults to a multiprocessing pool, which on Windows spawns a
  fresh child process per call and reparses the bundled dataset each time - seconds of cost,
  repeated. `RGeocoder(mode=1)`, loaded once and reused, measured at 2.16s first lookup then
  0.0s after.
- Proof includes the item's own required test: a real lookup still succeeds with `socket.connect`
  blocked, proving zero network syscalls directly rather than by inspection.

### Fixed: the startup splash could sit on top of every other window on the screen, and the taskbar showed no icon for Leasha

- **The splash screen was set to stay on top of everything, not just the window loading
  underneath it.** `WindowStaysOnTopHint` pins a window above the rest of the desktop for as
  long as it exists, not only for as long as it is meant to be visible. The splash is only
  ever meant to be on top of Leasha's own window while that window loads; it should never be
  able to sit above the browser, email, or anything else once that moment has passed. Removed
  - a normal window already paints above whatever else is on screen while it holds focus,
  without needing to be pinned there.
- **Leasha's taskbar button carried no icon.** Running from source, with no installer or
  Start Menu shortcut yet (that is L9, not started - see `HANDOFF.md`), Windows had nothing
  to identify the app's taskbar entry by except the shared Python interpreter it runs under -
  so the icon set on the window itself never reached the taskbar button. Leasha now gives
  its process its own identity before any window is created, which is what the taskbar
  actually keys the icon on.

### Fixed: one graphics-driver failure used to end the whole indexing run - now it carries on using the processor

- **A real event, from the user's own machine on 2026-09-08, later the same morning as the
  fix below.** Half an hour into an indexing run, the graphics driver reported an internal
  error - its own words were that its state was "probably suspect" and the application should
  not continue. Leasha's reply was to end the indexing run on the spot, and then say nothing
  further: the window stayed open for hours looking as though indexing was just very slow,
  when in fact it had stopped at that moment. The fix below, shipped earlier the same morning,
  did not catch this: it recognised three specific driver error codes, and this was a fourth
  from the same family.
- **This is fixed, in three parts.** Leasha now recognises the whole family of graphics-driver
  error codes, not a hand-picked few. When one happens during indexing, the batch that failed
  is tried once more on the processor rather than being given up on - so the run continues,
  more slowly, and searches still work. And once the driver has failed in a session, every
  part of Leasha that might use the graphics card (the meaning model, picture-reading,
  result reordering, and the picture model) uses the processor for the rest of that session
  instead of asking the same driver again - even if the settings say "use the graphics card",
  because the driver has said it should not be trusted. Restarting Leasha gives the graphics
  card a fresh chance.
- **What you will see.** A single warning in the log in plain words when it happens, the
  run log recording that the meaning model then ran on the processor, and a slower rate from that
  point. If the processor also fails on the same batch - which would be a genuine fault, not
  a driver hiccup - the run still stops loudly, with a message that says what happened rather
  than telling you to delete the model cache.

### Fixed: indexing kept pausing itself for "other programs" that were its own converters, and a busy machine could make Leasha say there was no graphics card

- **A real run, from the user's own machine on 2026-09-08.** Between 06:06 and 06:28 the
  resource governor paused and resumed indexing about 25 times, each pause saying "the machine
  is busy (81-95% CPU used by other programs)". Indexing crawled. But a good part of that CPU
  was Leasha's own: the programs it starts to convert files (LibreOffice for old Office
  formats, the drawing converters, the RTF converter) run as separate processes, and their
  work was being counted as somebody else's. So Leasha paused because of its own converter,
  waited for it to finish, resumed, started the next converter, and paused again.
- **This is fixed.** The converters' CPU now counts as Leasha's own, exactly as its main
  process already did, so the governor only yields to programs that are genuinely not Leasha.
- **And when it does pause, the log now says who is busy.** On the way into a CPU pause, the
  run log names the three programs using the most CPU (for example `busiest right now -
  MsMpEng.exe 41%, ollama.exe 22%, chrome.exe 9%`), in the same units as the pause message.
  This takes about half a second, at a moment when indexing is stopping anyway, and is never
  done more than once every thirty seconds. The next time this happens, the log will answer
  "busy with what" instead of anybody having to guess between antivirus, Ollama, Windows
  Search and OneDrive.
- **Separately, at 06:06 the same run said picture-reading was "on the processor, because no
  display adapter was detected"** - on a machine with a graphics card, while the meaning
  model in the same process was already running on it. The check that asks Windows which
  graphics cards are present had simply not finished in time under all that load, and an
  unfinished check was being reported exactly like "there is no graphics card". Now the
  difference is kept: when the check cannot run, Leasha reuses the answer it already had from
  earlier in the session or from the stored machine profile (and says so in the log:
  `graphics card check timed out after 15s - using the last known answer (1 adapters)`); when
  there is no earlier answer, the sentence is the truthful "the graphics card check could not
  run" rather than a claim about the hardware. A timed-out check also no longer overwrites
  the stored machine profile with one that has no graphics card in it. The check is not
  given longer to run - a longer wait under load is still a wait that can fail.

### Fixed: a graphics driver hiccup used to turn off meaning search, picture-reading and result reordering for the rest of a run

- **A real event, from the user's own machine on 2026-09-08.** The graphics driver briefly
  reported the graphics card as unavailable mid-run - the sort of thing a driver reset, the
  machine waking from sleep, or heavy system load can cause, and which often clears up within
  seconds. Leasha did not know the difference between that and a genuinely broken or corrupted
  model, so it said "delete the model cache and let it download again" - which would not have
  helped, because nothing was wrong with the downloaded model.
- **Worse, none of the three affected parts of Leasha - the meaning model, picture-reading (OCR),
  and search result reordering - tried again on their own.** Each one kept using the same
  session the graphics card had already dropped, for the rest of the run, even though the card
  usually recovers on its own moments later. Meaning search, OCR and reordering could all end up
  silently switched off for the remainder of a long indexing run or a whole session, over one
  brief hiccup.
- **This is fixed.** Leasha now recognises this specific kind of graphics-driver event, says so
  in plain language rather than pointing at the model, and starts a fresh session on the very
  next attempt - which may land back on the graphics card once the driver has recovered, or fall
  back to the processor if it has not. A genuinely broken or corrupted model still gets the
  original "delete the model cache" guidance; only this one, recognisable cause is treated
  differently. Picture-reading also now says something the first time this happens, rather than
  nothing at all - previously it was recorded at a logging level nobody would ever see, so a
  scanned document could quietly lose all its text for the rest of a run with no sign anything
  was wrong.

### Fixed: indexing could crash when the graphics card was doing two things at once

- **A real crash, reproduced from `logs\crash\crash.log`.** On 2026-09-07, an
  index run crashed while the meaning model was mid-calculation on the graphics
  card at the exact moment the picture-reading (OCR) model was independently
  building its own session on the same card. Nothing was wrong with either
  model on its own - three parts of Leasha (the meaning model, OCR, and search
  reranking) each decide for themselves whether to use the graphics card, and
  nothing before this stopped two of them from touching it at the same instant.
  That is now stopped: whichever one of the three is using the graphics card
  has it to itself for as long as it needs it, and the other two simply wait
  their turn rather than colliding.
- **This can make indexing slower on a machine using the graphics card for more
  than one of these at once** - the picture-reading model and the meaning model
  can no longer calculate on it at the same time, where before they sometimes
  could. A crash outranks that: a slower run that finishes beats a fast one
  that corrupts partway through. Nothing changes on a machine using the
  processor instead, or using the graphics card for only one of the three -
  there was nothing to collide with there and nothing is now waited on either.
  The exact slowdown depends on how much the three previously overlapped on
  your machine's graphics card, which was not something this build sandbox
  (no graphics card at all) could measure honestly - see `HANDOFF.md`'s known
  issues for what was and was not measured.

### Clicking Start Indexing no longer freezes the window

- On some machines - the first run, after a driver or hardware change, or if
  the last check of your hardware did not save properly - Leasha needs a
  moment to look at your machine again before it can start. That check used
  to happen before the window could respond to anything else, so the whole
  application looked frozen at the exact moment you clicked Start. It now
  happens in the background: the Start button disables itself and the status
  bar says "Checking your hardware…" while it works, and the window stays
  responsive throughout. On most machines, most of the time, this check is
  fast enough that you will not notice it either way.

### Changing a limit on the Indexing screen now actually applies straight away

- Worker count, the memory ceiling, the CPU cap, the free-space floor and the
  tuning mode are meant to take effect for the next run in the same session,
  without restarting Leasha - the screen has said as much for a while. It
  never actually worked: the change was saved correctly, but nothing made it
  reach the run itself until you closed and reopened the application. It now
  reaches the very next run, exactly as the screen already promised.

### The loading screen now genuinely comes up before anything heavier starts

- What you see has not changed - the loading screen already appeared
  quickly on every launch measured on the owner's machine. What stood
  behind that was wrong: several of the heavier parts of the search engine,
  and the whole main window, were being loaded before the loading screen
  itself was shown, contradicting a promise written into the loading
  screen's own code. They now load after it is on screen, where a moment's
  delay has something on screen to explain it. Work order 0r §4.
- Added the test coverage that was missing for this: one check drives the
  loading screen through every one of its rotating messages and its
  fade-and-close sequence in a single pass, rather than checking each piece
  on its own; another checks, whenever the startup file changes, that
  nothing heavier than the loading screen itself is ever loaded before it
  is shown.

### The window appears sooner - Mail and Code fill in a beat later

- Opening Leasha used to build every tab - Search, Files, Mail, Code,
  Indexing, Settings - before the window could appear at all. Mail and Code
  now finish building just after the window is already on screen, so there
  is less to build before you can see and use it. Nothing about either tab
  changes once it appears - same order, same contents, same shortcuts.
- Measured in the build sandbox (not a real machine, so not the final word):
  constructing the window dropped from a median of 662ms to roughly
  180-410ms across repeated measurements. The owner's own <1.5s
  window-visible target from Work order 0r §2b still needs verifying on the
  real machine - that number is recorded, with the exact command to
  re-measure it, in the work order itself.
  Work order 202626271601 §2b (partial - Files, Indexing and Settings
  remain built up front; a fuller pass is flagged there for later).

### You can look at a drawing without opening AutoCAD

- A `.dwg` used to preview as "No preview for this type" - a filename and
  nothing else, which is no help at all when three revisions of the same
  drawing are sitting in the results together. Now every drawing shows the
  AutoCAD version that wrote it, and if you have LibreDWG installed, pinning
  one in its own window offers **Show simplified view**: the lines and the
  text, drawn here, enough to tell which drawing it is. Rotate, zoom and
  print all work on it, like any other pinned page.
- Drawn once and kept, so opening the same drawing again is instant. Nothing
  is written to the drawing itself, ever.
- Leasha does not ship the converter and never installs one. If LibreDWG is
  not on the machine, the preview says so and where to get it, rather than
  quietly showing nothing. **Drawing previews** in the row of switches above
  the results turns the whole thing off.
  Work order 0e §5c, which closes that order.

### A photo taken in 2006 is found by `before:2010`, however many times it has been copied since

- `before:` and `after:` now use a photograph's own date - the one the camera
  recorded when the shutter fired - instead of the date the file happened to
  land on the current drive. A picture taken in 2006 and copied between
  machines three times since had been filed under the date of the last copy,
  so searching the year it was actually taken did not find it. Work order 0f
  §3a.
- A photo with no date inside it, or one whose date cannot be read, still uses
  its file date exactly as before, and so does every document, spreadsheet and
  email in the index - nothing else changes. A corrupt photo costs only its own
  date and never interrupts a run.
- **A bug found while fixing this, worth naming because it looks identical
  from the outside:** where a photo carried more than one date, the wrong one
  was being preferred - the "last modified" stamp that photo software rewrites
  whenever it saves, rather than the original shutter date. A 2006 photograph
  opened and re-saved in 2019 therefore read as 2019 even once its dates were
  being read at all. The shutter date now wins.
- **A known gap, flagged rather than silently left:** this is the filter half.
  Result rows still *show*, and `/newest` still *sorts by*, the file's copy
  date - so a photo can be found by `before:2010` and still display 2019.
  Display and sort change result ordering everywhere at once and are being
  done as their own measured piece of work; the storage they need is in place.
- **That gap is now closed.** A result's date, where it sits in a `/newest` or
  `/oldest` list, and which copy of a repeated photo is shown up front, all
  use the same shutter date the filter already did. A photograph shot in 2006
  and copied several times since now shows 2006 and sits where 2006 belongs in
  a newest-first list, instead of wherever its most recent copy happened to
  land.
- Existing indexes gain the new date column automatically on next open (schema
  v18); photos already indexed pick up their dates as later runs re-touch them.

### Typing part of a filter's value no longer closes the window

- Typing a colon and then the beginning of a value - `type:pd`, `saved:inv` -
  could shut Leasha down without a word, as soon as the index had a count to
  show for what was being typed. The `/` menu filters the values it fetched
  against what you have typed so far, and it could not read the ones that
  arrived carrying their count; the failure happened deep enough inside Qt
  that the whole window went with it. Found by the first test that ever typed
  a partial value into a real search box. Work order 0s, the scenario sweep.


### A result that matched several times can now be opened up with one click on its arrow

- When a document matched in several places, its row already showed a small
  arrow and said "matched in 5 places" - but clicking the arrow itself did
  nothing; only double-clicking the whole row opened it up. The arrow now
  works on its own, a single click, exactly where it looks like it should.
  The whole row still opens it too, the way it always has. Work order 0q
  item 2a.

### Portrait photos preview the right way up

- A photo taken holding the camera sideways carries an EXIF tag recording
  that; until now nothing read it, so the single-item preview pane showed
  every such photo lying on its side regardless of how it was actually
  held. `read_orientation()` (`app/extract/exif.py`) existed already but was
  never called from anywhere - work order 0f §3b. Now every image the
  preview pane decodes, including HEIC/HEIF, is corrected before it is
  shown.
- **A known gap, flagged rather than silently left:** the "pin in its own
  window" pop-out preview decodes images through a separate code path
  (`app/ui/render_page.py`) that this fix does not reach - a photo pinned to
  its own window can still preview sideways. Flagged as a follow-up.

### Wall photos cost milliseconds now, and a scanned page inside an ordinary report is no longer skipped

- The OCR ladder's detection-only rung is built: before reading a photo in
  full, Leasha now checks - in a fraction of a second - whether it has any
  text at all. A photo of a wall or a beach records "no text found (checked)"
  in well under a second; a photo of a receipt or a whiteboard still reads
  properly, because the check only ever decides whether to read something,
  never that something should be skipped.
- A scanned page tucked inside an otherwise ordinary, born-digital PDF - a
  report with one scanned appendix, say - is no longer silently left out.
  Leasha now gives that one page the same chance any other photo gets,
  without paying to read the pages around it that already have real text.
- **A known gap, found and not silently left**: a photo's EXIF date - the
  date the camera actually took it - is read correctly but does not yet
  reach search. `after:`/`before:` still go by the file's copy date, so a
  twenty-year-old photo copied onto a new drive can still turn up under the
  wrong year. Fixing it properly needs a bit more plumbing than this round of
  work covered, so it is recorded rather than quietly worked around.
- How blank a photo must look before it is treated as a scanned page was a
  number fixed in the code. It is now a setting - Index Tuning, Coverage
  group - in plain words, so a whiteboard or a snowy photo that keeps getting
  read as a document can be told apart from one. Left alone, nothing changes:
  the setting's own default is exactly what Leasha always did. Work order 0f
  §2e.

### Photos can now be found by describing them, not just by filename

- Work order 0h's CLIP image lane reaches an actual search box for the first
  time: type a plain description - "kids blowing out birthday candles" - and
  a matching photo can now appear even when nothing about its filename or
  (non-existent) text says so. Previously built and tested in isolation only;
  this wires it into `SearchEngine.search()` itself, as a third retrieval
  lane fused in alongside keyword and meaning-based search via the same RRF
  fusion those two already use, weighted equally (1.0, as the order
  specifies for v1).
- Runs everywhere a real search already runs - the window, `app.cli search`,
  `app.cli shell`, `app.cli evaluate` - and stays off, exactly as before,
  wherever the lane's two pieces are not supplied. A broken picture lane
  degrades with its own notice rather than silence or being confused with
  meaning-based search breaking (they fail independently).
- **Also wired: indexing.** `app.cli index` now actually writes the CLIP
  vectors this lane searches - previously nothing in the application did;
  only tests ever constructed the pieces that write them.
- **A known gap, not silently left**: indexing started from the window
  itself does not yet write CLIP vectors - only `app.cli index` does, for
  now. A photo indexed from the window will not be found by description
  until that second site gets the same wiring. See work order 0h §1c.
- Two real bugs were caught and fixed while wiring this in, not assumed
  away: a photo result's id could have crashed the very first search that
  found one (a namespaced id that a plain `int()` cast could not parse), and
  the new notice code had no plain-English form for the everyday search tab
  - both caught by the existing test suite before landing, not after.

### Photo results can now show as thumbnails, and a pop-out photo has next/previous

- A "Thumbnail grid" switch beside the results list (off by default, like
  every switch here) swaps the list for a grid of thumbnails on results
  that have photos in them. The list is still what opens first; asking for
  thumbnails is a click away and stays remembered. Work order 0h §3a.
- Every thumbnail is decoded in the background - scrolling never waits on a
  photo, and a photo that cannot be read shows a placeholder rather than an
  empty gap or an error. Orientation is corrected automatically, so a
  portrait photo from a phone shows upright rather than sideways.
- The pop-out photo viewer now has next and previous (the left/right arrow
  keys), so browsing a set of photos - from the grid, or from a single
  photo pinned out of the results list - no longer means closing the window
  and opening the next one by hand. Work order 0h §3b.
- "More like this" is a right-click action for the first time - on a
  passage, and on a photo, **and both now work**: the UI half (this entry)
  and the backend half (the entry below) were built by two independent,
  concurrent pieces of work that each named the other's missing half as a
  known gap - closed by wiring the right-click action to whichever of
  `SearchEngine.similar_to`/`find_similar_images` a row's kind actually has
  a vector for, once both halves had landed. Work order 0h §2d.

### Leasha can now tell when two photos are the same picture, even when the bytes differ

- Every photo now gets a perceptual fingerprint alongside its CLIP vector,
  computed during the same images pass - free of charge for anything that
  already went through the OCR ladder. Unlike an exact byte match, this
  survives a recompression, a resize, or a re-save: a WhatsApp-compressed
  copy of a photo and its full-resolution original still read as the same
  picture, which a byte-for-byte comparison could never say. Work order 0h
  §2a.
- **Near-identical photos now fold into one row**, the same way older
  versions of a document already do - newest shown, "N similar photos" one
  click away. A burst of near-duplicate shots, or the same photo indexed
  twice from two different drives, no longer clutters a results page with
  several rows that are really one photo. Work order 0h §2b.
- **Drop or paste a photo into the search box and find it** - and its
  relatives - in the index. A result says plainly whether it is *this exact
  photo* or merely *similar*, so a degraded copy someone was sent can be
  traced straight back to wherever the full-resolution original actually
  lives. Work order 0h §2c.
- **"More like this" now has a working backend for photo results too** -
  the same right-click action for a passage now has something real to call
  for a photo row as well, not only a passage, and, once merged alongside
  the UI half that names the same feature above, is actually wired to it.
- **One gap remains, named rather than left to be rediscovered**: reverse
  image search (§2c) needs a CLIP vision-tower model at the point a real
  search is built, and that wiring has not landed in `app/main.py` or
  `app/cli.py` yet - the backend works, proven against real recompressed
  photos, but nobody typing into a real window or running a real command
  can reach it today.
- `imagehash`, this feature's one new dependency, was checked before use
  and turned out not to already be installed, despite an earlier note
  saying otherwise - installed and pinned rather than assumed.

### CLIP image embedding now uses the graphics card when one is usable, the processor otherwise

- `ClipImageEmbedder` (the image-vector lane, work order 0h §1) gained the
  same DirectML-with-CPU-fallback seam `Embedder` (the text model) and the
  reranker already share: `device=`/`profile=`/`problems=`, `backends.choose`
  and `backends.with_fallback`. `from_settings` reads the same `embed_device`
  setting as the other two, so one control governs all three rather than a
  second one nobody would think to change alongside it.
- **Why:** the same measurement recorded in the 0h work order found CLIP
  image embedding running 7-11x over its ~50-150ms/image budget
  (1,675.6ms/image single-call, 1,003.9ms/image batched) on this machine,
  CPU-only, with no GPU path exercised at all - flagged there as a
  recommended follow-up rather than done at the time.
- A GPU that is present, advertised, and then refuses the graph on first use
  falls back to the processor with a visible notice rather than failing the
  embed - H4's discipline, exactly as it already worked for the text model.
  A machine with no display adapter, or with one but no `onnxruntime-directml`
  installed, goes straight to the processor with no notice at all, since
  nothing was tried and failed.
- **Re-measured after `onnxruntime-directml` was installed into this venv**,
  same day: CPU steady-state dropped to **69.1ms/image** (from 1,675.6ms -
  see the caveat below on why that magnitude of change should not be fully
  trusted), GPU (DirectML, Iris Xe) to **37.5ms/image** single-call - both
  now comfortably inside the original 50-150ms budget. Oddly, the GPU
  *loses* on a batch of 8 (97.1ms/image vs the CPU's 75.7ms/image) - a real,
  reported result rather than a smoothed-over one, and since an index run
  always calls in batches of 16, it genuinely is not clear from this
  fixture alone that `auto` should prefer the GPU here at all.
- **Honestly flagged:** the ~24x CPU speedup between the two measurements
  has two entangled causes - no concurrent background test sweep this time
  (the first measurement named one as a likely confound) and the
  `onnxruntime` package itself changing version, 1.29.0 to 1.24.4, as a
  side effect of installing the DirectML wheel (both wheels provide the
  same `onnxruntime` import name and cannot coexist, so the DirectML
  install replaces the plain one - the same swap `install.ps1` already does
  for an accepted GPU install per work order 0114 §2c). Which cause did
  most of the work is not known and not claimed - full detail and both raw
  tables are in the 0h work order.
- Five new tests in `tests/unit/test_clip_embedder.py` prove the wiring
  itself (not `backends.choose`/`with_fallback`'s own correctness, already
  covered in `test_backends.py`): the CPU path passes no `providers` kwarg
  at all and stays byte-for-byte what it was; `auto` on a GPU-capable
  profile asks for `["DmlExecutionProvider", "CPUExecutionProvider"]`; a
  GPU that raises on first use falls back and the fallback is recorded in
  `problems`; a profile with no adapter reaches the processor with `problems`
  left empty. All 98 tests across the CLIP lane, `test_backends.py` and
  `test_embedder.py` pass.

### Search results can now be dragged, pinned and read on a timeline

- A result can be dragged straight out of the list into Explorer, an email
  or anywhere else that takes a file - no more finding it a second time in a
  folder. A result whose file has moved or been deleted since it was
  indexed simply does not drag; a mail message drags nothing, never the
  whole mailbox it lives in.
- A new "Pin" option on a result's right-click menu gathers it into a
  pinned panel beside the results, which keeps what you gather across
  several searches until you clear it yourself - a new search never
  empties it. From there: open everything at once, copy every path, or
  drag the whole set out together.
- A thin band above the results now shows when they cluster in time -
  click a period to add it to the search, using the same after:/before:
  wording you could already type by hand.
- All three are individually switched off from a row of checkboxes above
  the results, each with a tooltip saying what it does.

### Measured: query-translation latency on the owner's hardware

- **Median 2.47s** across 5 representative sentences ("the safety report Dave
  sent about Leeds before the audit", "photos from the school trip last
  summer", "invoices from acme corp last quarter", "that email from sarah
  about the budget meeting", "pdf files mentioning the new contract terms"),
  against the model actually configured on this machine (`qwen2.5:1.5b` via
  the persisted `ui:ollama_model` setting — not the code-level `mistral`
  default, which nobody here uses).
- One of the five paid a cold-start model-load penalty (15.31s); the other
  four, once the model was resident, ran 0.78s–2.48s. Real first-use-of-a-
  session latency is closer to the 15s figure; every subsequent translation
  in the same session sees the sub-3s numbers. Both are reported rather than
  averaging them away, since a user's actual experience is "slow once, fast
  after," not a single flat number.
- Two of the five sentences were rejected outright by the model this run
  ("photo"/"invoice" offered as file types that do not exist) - correctly
  caught by the existing invented-operator rejection, falling back to raw-
  text search rather than a broken filter. Worth noting for whoever tunes
  prompt wording next: a smaller model rejects more often than a larger one
  would, which is a real trade-off of running `qwen2.5:1.5b` over `mistral`.

### Settings now has five shelves instead of one long scroll

- Settings used to be twelve boxes stacked on one page, longest first,
  shortest last, with nothing telling you which was which until you
  scrolled past it. It now opens onto five named places — What's indexed,
  Search, Models & AI, Appearance, Storage & maintenance — picked from a
  list down the left, the same way a code editor lets you jump between
  its own settings.
- A box at the top finds any setting by typing part of its name, wherever
  it lives — type "shortcut" and the mini-search hotkey turns up without
  you needing to know which of the five places it was in.
- Settings remembers which of the five you had open last time, so it
  reopens there rather than back at the start every time.
- Nothing on any of the five was reworded on the way — every label and
  every explanation reads exactly as it did before.

### Indexing splits into Status, Schedule and Tuning, and stops squeezing controls off the bottom of the page

- The Indexing page used to run the run controls, the skipped-files list,
  the schedule and the whole Index Tuning screen down one page with
  nothing to scroll it once all of that no longer fit — so the bottom of
  the page, including several tuning controls, was simply unreachable on
  anything but a tall window.
- It now opens onto three places, picked from the same kind of list
  Settings uses: **Status** (what's running, what's in the index, what got
  skipped), **Schedule** (when a run happens on its own) and **Tuning**
  (everything that decides how fast a run goes). Schedule and Tuning now
  scroll on their own when a smaller window needs them to, so nothing at
  the bottom of either is ever cut off again.

### Fixed: a folder Leasha cannot find is no longer skipped in silence

- **If one of your folders had moved, been renamed, or was on a drive that
  was not plugged in, Leasha skipped it and said nothing at all.** No warning,
  no note, no count — and the run then reported success.
- With one folder set up, that meant the whole thing: an index run that looked
  at nothing, finished, and showed zeroes. From the outside it reads as "it
  did not index my mail", because it did not.
- Leasha now names every folder it could not read, and says which is missing
  and which is shut out by a setting. It says so even when the run indexed
  plenty from your other folders — that case was invisible before, because
  every number on the page looked healthy.
- A run that found nothing at all now also explains which of the three
  situations it is in: no folders set up, all of them marked as archives, or
  the folders were read and held nothing.

### Fixed: Leasha forgetting when it last indexed

- **The moment an index run finished, the step that records the time failed**
  — every time, since the scheduler was written. It never got as far as
  writing anything down.
- So after closing and reopening, Leasha had no idea when it had last
  indexed, and worked out the next scheduled run from nothing.
- Also fixed: printing a picture from a popped-out preview window failed the
  same way.
- Neither was ever visible, because both went to a console Leasha does not
  have. The error reporting added yesterday caught the first one within
  hours; a sweep for the same kind of mistake found the second.

### Fixed: Leasha closing itself for no apparent reason

- **Leasha could vanish while you were using it** — no message, no warning,
  the window simply gone. Windows recorded eight of these on 26 and 27 August,
  every one in the same place.
- It came from the part of Leasha that remembers how wide you like your
  columns. It was being told about every single column re-size, including the
  hundreds that Leasha does itself, *and it was being told while the table was
  still mid-rebuild.* Now and again the two collided and the program stopped
  dead.
- That listening has been removed entirely. Leasha now simply checks the
  column widths a few times a second, quietly, when nothing else is happening.
- **Dragging a column still remembers the width**, and rather better than
  before: it can now tell the difference between a column you dragged, a
  column Leasha fitted to its contents, and a column that moved because you
  resized the window. It used to guess at that, and the guess was wrong often
  enough to be reported as "it forgets my columns".
- This is the third attempt at this fault. The first two made it rarer instead
  of fixing it, which is why the code and its tests now carry the whole story.

### When it crashes, it now says so

- **A crash used to leave nothing behind at all.** No message, no error, no
  file — the log simply stopped mid-line. Leasha closed twice like this on
  27 August and there was literally nothing to read afterwards.
- The reason was small and unlucky: the crash reporter wrote its report to the
  console, and Leasha is started without one. It gave up at that point and
  never got as far as writing the file. So the report that exists for exactly
  this situation was switched off by exactly this situation.
- Now the file is written first, and the console is a bonus when there is one.
  A crash leaves `logs\crash\crash.log` naming the line it died on, and
  ordinary faults are written into the normal log before the window goes.
- Warnings from the graphics toolkit are recorded too. They used to go to the
  same console that is not there.

### Search from anywhere with a shortcut

- **Press Ctrl+Alt+L in any application** and a small search box appears.
  Type, press Enter, the document opens, the box is gone.
- Escape or clicking away closes it, and it never keeps what you typed.
- Change the shortcut in Settings → Search, or switch it off. If another
  program is already using the combination, Leasha says so rather than
  leaving you with a key that does nothing.
- Press Enter with nothing found and the full window opens with your search
  already in it.


### The search-anywhere box now opens around what you had selected

- **Select some text anywhere, press the shortcut, and it is already in the
  box** — highlighted, so typing replaces it in one go. Nothing is searched
  until you say so; it only saves you the retyping.
- No selection, and the box opens exactly as it always has: empty, ready to
  type.
- Reading the selection never delays the box appearing — it opens first, and
  fills in a moment later if there was anything to read.
- Switch it off separately from the shortcut itself, in Settings → Search,
  if you would rather the box never look at what else you were doing.

### Live counts under the search-anywhere box

- **"7 files · 2 emails" appears under the results**, so you can tell at a
  glance what kind of thing you are looking at before opening anything.
- Press Tab to step through the kinds and narrow the list to just one of
  them; press it again to cycle to the next, and once more to see everything
  again.
- Nothing is searched again to produce this — it counts what was already
  found. A result set that is only one kind shows no counts at all, since
  there is nothing to choose between.

### Pin a document in its own window

- **"Pin in a window"** on any preview opens that document in a window of its
  own. Pin as many as you like — two drawings side by side, or the mail you
  are answering while you search for what it mentions.
- **Searching again never changes a pinned window.** That is the point of
  pinning it.
- **Rotate a sideways scan once and it opens that way for ever.** The file
  itself is never changed.
- Zoom with Ctrl+wheel, fit it to the window, print it — or print to PDF to
  save a copy.
- **Ctrl+F finds text inside a preview**, in the pinned window and in the
  panel beside your results.
- Keep any pinned window on top while you work in something else.


### The activity log, in colour and in its own window

- **Warnings and errors stand out.** The level word is still on the line, so
  it works whether or not you can tell the colours apart.
- **Double-click an error that names a file** and it opens that file.
- **Open the log in its own window** and keep it on top, so you can watch an
  index run while working in something else. It stays in Settings as well.
- The window remembers its size and whether you pinned it.


### Click into an empty search box and it offers your own searches

- **The last few things you looked for, and the searches you saved.** Pick one
  and it runs.
- Newest first. Slash commands are never offered back — those are a mechanism,
  not something you were looking for.
- A saved search goes in as a reference, so it still re-runs live.
- Switch it off in Settings → Search if you are on a screen other people can
  see. That hides your history; it does not hide the searches you saved.


### leasha:// links

- **A link can open Leasha on a search**: `leasha://search?q=safety%20report`
  in a shortcut, a note, or anything else that opens links.
- If Leasha is already open it runs the search in the window you have, rather
  than refusing because a copy is running.
- Set it up with `leasha open register`, or say yes when the installer asks.
  `leasha open unregister` takes it back out.
- A link can only ever run a search. It cannot index a folder, open a file or
  change a setting, on purpose.


### Click any column heading to sort

- **Every list sorts now**, not just Mail — Files, the Code results, the file
  list on the Code tab.
- Sizes sort by size and dates by date, never by how they happen to be
  written: "3 KB" no longer comes after "10 KB".
- Search results are ordered by best match. Click a heading to sort, click it
  again to reverse, and **click it a third time to get the best-match order
  back** — nothing is lost by having a look.
- Refreshing a list keeps the order you put it in.
- **Every heading now sits over its column the way the column reads.** A
  right-aligned column of sizes had a centred heading above it.


### Spreadsheet results say which row

- **A hit in a spreadsheet now says where it is**: *Sheet 'Q3 Costs' · near
  B14*, instead of "page 3".
- In a workbook with forty thousand rows in it, "page 3" is somewhere to start
  looking. The row is the answer.
- It says *near* rather than *at* on purpose: it is the row the matching
  passage starts on, and a passage covers several rows.
- Works for both `.xlsx` and older `.xls` files. Existing spreadsheets pick it
  up next time they are indexed.


### Searches you can name and keep

- **Save a search under a name and run it again later.** It re-runs there and
  then, so anything indexed since you saved it turns up too - it is a standing
  question, not a photograph of an answer.
- Type `/saved` in the search box and the names come up, with how many times
  you have run each, the ones you use most at the top.
- Rename them, delete them. Nothing is ever saved for you and nothing suggests
  that you should - the list is yours and stays as short as you keep it.
- The scope you were searching in is saved too, so a saved mail search comes
  back as a mail search.
- Add words after it: `saved:invoices leeds` runs your saved search and looks
  for leeds inside it.

### Fixed: results never showed the date

- **Every document result showed no date at all.** The row was built without
  it, so where the date should have been there was nothing - for as long as
  the feature has existed. Emails were unaffected, which is why it went
  unnoticed: they take their date from the message.
- In a folder with fifteen years of work in it, the date is often the only
  thing telling two results apart.

### A result found by meaning says so

- **A result with none of your words in it now says "meaning match".** It
  looks like a mistake otherwise - and it is not one, it is the search
  understanding what you meant.
- Only that kind of result is marked. Putting a badge on every row would hide
  the one that needs it.
- It is a word, not a colour, so it works for everybody.

### Ask any result why it is there

- **Every result can now explain itself in plain words**: which of your words
  are in it, whether it was found by meaning rather than by matching, whether
  it is recent, whether you have opened it before, whether it is where
  something is defined, and how many copies were folded into the row.
- **Facts only — it never invents a score.** No percentages, no "relevance
  ratings". Everything it says is something you could check yourself, because
  a number you cannot check is a number you would believe anyway.
- Nothing to say means it says nothing, rather than padding.
- Switch it off like any other behaviour.

### Fixed: correcting a misspelling did not actually change the search

- **The spelling correction announced itself and then did nothing.** Typing a
  misspelled word showed "also looked for 'volcano'" above an empty page: the
  correction reached the message and never reached the query. Now it finds the
  document.
- Found by writing the acceptance test the whole feature exists for - a child
  typing a word wrong and looking for her homework - which checks what she
  would see rather than what the code reports about itself.

### The window follows your text size

- **If you have made text bigger in Windows, Leasha now grows with it.** Every
  size in the window was fixed in pixels, which follows your screen's
  resolution and ignores the setting that says you want larger letters.
- Nothing changes on a machine with the standard text size: the same window,
  the same density. The sizes are now expressed relative to your system font
  rather than nailed down, so the two agree.

### Code results know how to open in your editor, at the line

- **A code result is a place, not a document.** Showing you the folder that
  contains line 512 makes you search for it again inside your editor. Leasha
  now knows how to open it where you found it.
- **Only editors actually on your machine are offered**, so nothing in the
  list can be picked and then fail every time you click. Ten are recognised;
  anything else is one line of configuration.
- Editors that do not put themselves on the PATH are still found - Windows
  installs several of them somewhere else, and looking only at the PATH is how
  an application tells you to install something you already have.

### More like this

- **Ask for other passages that read like one you are looking at.** Finding
  things by meaning has been in Leasha since the beginning and has never been
  something you could point at - it only ever happened invisibly, behind a
  search box.
- It never offers you back the thing you asked about, and by default it does
  not offer you the next page of the same document either. You asked what
  *else* is like this.
- Costs nothing to run: the passage was already turned into numbers when it
  was indexed, so this reads those back rather than doing the work again.

### Searching for a name finds where it is defined

- **The file that defines a thing used to come last.** Search for a class or
  function name and you got every file that calls it first, because those
  mention it repeatedly and the definition appears once. On a five-file test
  the defining file ranked fifth, below a document that merely talked about it.
  It now ranks first.
- **It only reorders, and only for a single name.** Search a sentence and
  nothing changes; search an ordinary word and nothing changes either, because
  no definition can match in ordinary prose.
- A definition it recognises wrongly - a comment shaped like one - can move up
  past the files that only use the name, but can never overtake a real
  definition. That is by construction, not by luck.

### Paste an error message and get the files that contain it

- **Pasting a traceback used to return everything.** The box split it on
  punctuation and asked for any document containing "object", or "has", or
  "no". Now the words are searched for in order, which is what makes a pasted
  line findable - on a test corpus that went from four results, two of them
  irrelevant, to exactly the two files that contain the line.
- **It says so, and says how to undo it.** Searching for words in order is a
  narrowing, so it is named on the page with the way back.
- **Hard to trigger on purpose.** A traceback or a `file.py:512` is enough on
  its own; anything else has to be long and carry two separate signs of being
  code. Typing a real error from memory is still an ordinary search, because
  the order you remember is rarely the order it was written in.
- **Fixed on the way: quoting a phrase with a word like `getUserName` in it
  broke the search entirely** - the whole query failed rather than returning
  anything. Quoted phrases now match exactly what you quoted.

### £40,000 and 40000 and 40k are the same amount, and search knows it now

- **Type an amount any way you like and you find every document that mentions
  it.** Before this, five documents all naming the same figure could not find
  each other: searching `40000` found one of them, `40,000` found two, and
  `40000.00` found none at all - not even the invoice containing those exact
  characters.
- Nothing needs re-indexing. What changed is the question, not your index.
- **A year is left alone on purpose.** Searching 2024 must not start matching
  every document with a 2 and a 24 near each other.
- Numbers under a thousand and very long numbers - order references, phone
  numbers - are left alone too. They are not amounts, and treating them as
  amounts would widen your search for nothing.

### Plain sentences become filters, without needing a model

- **Type "the invoice Dave sent me last year" and Leasha offers you the
  filters it recognised** — from Dave, PDF files, that year — as chips beside
  what you typed. Until now noticing the word "pdf" needed a 4GB language
  model that most machines do not have.
- **What you typed is never changed.** The chips sit next to your words and
  each is one click; a wrong one costs a glance, not a page of results.
- **It only offers filters your own documents can satisfy.** A name becomes a
  sender filter only if that person is in your index, and "spreadsheet" only
  becomes a filter if you have spreadsheets. So the failure is a chip that
  does not appear, never one that quietly empties the page.
- **A first name matching two people is refused**, because picking one of them
  for you would give you a page that looks complete and is not.
- **"The report Dave sent me" and "what did I send to Priya" now mean
  opposite things**, which they should - the same verb, and only the word
  order says who sent it.
- **Vague dates are left alone on purpose.** "About six months ago" means
  different things to different people, and a wrong date filter hides
  documents without saying so. Years, months and "last year" are read; the
  rest stay as words.
- **"Emails with something attached" now finds them.** The attachment filter
  has always worked and no plain sentence had ever produced it - it could only
  be reached by typing an operator, which is exactly what the everyday tab
  exists to not require. Asking for it in words scored zero on our own test
  set before this.
- Off on the Code tab and behind the Interpret button elsewhere, exactly as
  the search behaviour table says.

### Your own recent searches, offered back

- **The box will offer what you searched for before** — the last six, without
  the near-identical refinements that come from typing the same thing four
  ways, and without the `/` commands, which are a mechanism rather than
  something you were looking for.
- Shown in the words you typed them in. Handing them back in a case you did
  not use reads as a correction.
- Reading that history happens off the interface thread, so a busy index
  cannot make clicking into the search box feel slow.

### Eight versions of the same letter, shown as one row

- **The same document saved eight times now takes one line**, with the newest
  shown and "3 older versions" one click away. Fifteen years of a working life
  makes `report.docx`, `report v2.docx`, `report FINAL.docx` and the copy
  dragged to the desktop in 2019, and the one you want is the last one.
- **The same file in two folders folds too**, because identical bytes are
  identical bytes - "1 identical copy elsewhere".
- **Nothing is hidden.** A fold shows the newest and keeps the rest one click
  away, and the underlying list is untouched - expanding a fold gives back
  exactly the page you would have seen.
- **`chapter 1` and `chapter 2` never fold.** A bare number is not a version
  marker; `v2`, `final`, `draft`, `rev 3`, `(2)` and dates are. Folding two
  chapters together would hide half a book, which is a worse failure than
  showing you a long list.
- **Among equally good matches, the newer one comes first** - a nudge inside
  relevance, not the date sort that /newest already does and already announces.
  It never hides an older document, it only orders them.
- **How strong that nudge is was measured, not chosen**, and the measurement
  overruled the first answer: the value written in on judgement would have cost
  five points of accuracy on the twenty test sentences. The one it fixes is
  "what did I send to Priya", which used to return an invoice template and now
  returns the mail actually sent to her.
- A file whose date cannot be read counts as old, not new. The other way round
  floats every unreadable archive to the top of every search.

### When nothing matches, it tells you what it let go of

- **A search that finds nothing now tries once more without the thing that was
  narrowing it**, and says so on the page. Quotes around a half-remembered
  sentence, an `AND` you typed, a file-type filter left on from your last
  search - each is dropped in turn until something comes back.
- **Never silently.** "Nothing contains the exact phrase "volcano flavoured
  bread" - these match its words instead." Results that answer a slightly
  different question are honest only while the difference is on screen, and
  the label is the first thing you read rather than a footnote.
- **One thing at a time.** Two filters dropped together would show you results
  from everywhere in every format, and you would have lost the thread of your
  own search. One is named, one is dropped, and the sentence stays a sentence.
- **Off on the Code tab**, where an exact phrase that matches nothing is the
  true answer and its words scattered over three files is noise.
- **What we found on the way, again worth writing down:** the plan was to drop
  your rarest word and search again. It cannot work - words are already joined
  with "or", so nothing-at-all means every word missed and there is no word to
  drop to. What empties a page that had something to find is an instruction,
  not a word, so instructions are what get relaxed. The result is closer to
  what the feature was for.

### A misspelt word finds the document anyway

- **A word your documents have never contained is quietly checked against the
  words they do contain**, and the closest one is searched for instead. Type
  "homwork" and you get your homework, with "also looked for 'homework'" above
  the results - because the alternative is an empty page, and an empty page
  tells an eight-year-old her essay is gone rather than that she typed a letter
  wrong.
- **Only a word that matched nothing at all.** A word your documents do contain
  is never second-guessed, however odd it looks - if you wrote it, finding what
  you wrote is the entire job. So the worst thing this can do is turn a search
  that was going to find nothing into one that finds something.
- **It reads your own documents, not a dictionary.** A surname, a project code
  or a Welsh place name corrects to what you actually write, and no word list
  ships with the app.
- **Three behaviours, one per tab.** The everyday tab corrects and says so; the
  Files tab asks "Did you mean…?" and changes nothing; the Code tab does
  neither, because `recieve_handler` may be precisely what is in the codebase.
- **What we found on the way, worth writing down:** the example this was built
  for - "volcanoe" - already worked. The word index stems it to "volcano" and
  matches directly, so it never reaches the correction at all. That makes this
  feature narrower and better aimed than planned: everything reaching it is a
  genuine miss that stemming has already failed on.
- Known limit, stated rather than discovered later: candidates are found by the
  first letters, so a typo in the **first two** is not corrected - "vlocano"
  still finds nothing. The common slip is a doubled or transposed letter later
  in the word, and fixing the rest would cost a scan of the whole vocabulary on
  every keystroke.

### Search now knows which tab it is on, and what that tab is allowed to do

- **Six switches for what search may do on your behalf**: fix an obvious
  spelling, try again with fewer words, offer filters it recognises, prefer
  recent documents, fold older versions together, explain in plain words. All
  on, each one switch-off-able.
- **A grid shows what each tab actually does with them**, because six switches
  alone would be a lie by omission: "Fix obvious spelling" is on, and it still
  does nothing on the Code tab, where a misspelt identifier may be exactly what
  is in the codebase.
- **A global switch can only turn a behaviour off, never force it on.** That is
  what lets six settings do the work of twenty-four - off means off everywhere,
  on means "follow this tab's contract" - and it is why spelling correction
  stays away from code without anybody having to remember to keep it there.
- **"Reset search behaviour to defaults" is one click**, and its tooltip says
  what it will not touch. Support at a distance depends on that button: "press
  that and tell me what happens" is one sentence instead of six.
- **The same notice, in the register the tab asked for.** "Searching by meaning
  is off at the moment, so these are word matches only" on the first tab;
  today's wording, naming the command that fixes it, on the power tabs. Same
  code, same fact, different reader.
- **No view branches on which tab it is.** That was the alternative, and each
  of the four would have written the rule twice - once where it was decided and
  once where it was almost decided.
- Caught by an existing guard: importing something called `translate` into
  `engine.py` silently defeated the test that keeps the retrieval path away
  from the query translator. Renamed, and the reason is now written where the
  next person will look.

### The tuning controls now change the run, not just the screen

- **Repeated text is embedded once.** Signatures, disclaimers and letterheads
  repeat across thousands of documents and every copy cost a full pass through
  the model. Measured on twenty documents sharing one confidentiality notice:
  80 passages, of which the model needs to see 22. All 80 vectors are still
  stored - identical text gives an identical vector, so this is arithmetic
  avoided rather than a trade-off taken. The run says how many it saved.
- **The tuning mode reaches the run.** The panel resolved `0` to `Auto (4)` for
  display and the run read the literal `0` - so switching modes changed what
  was shown and nothing about what happened. One function now answers for both,
  so they cannot drift.
- **"Threads per model call" reaches the model.** Left alone, the runtime takes
  every core, which is right for a benchmark and wrong during a run where the
  file readers already hold several and the two multiply into a machine slower
  than it started.
- **"When to read images" works**: during the run, after it, or only when you
  ask. After-run is said, never started - a second pass over a scanned corpus
  is hours, and launching it unasked is the kind of surprise that gets an
  application uninstalled.
- **A third settings guard.** One test proves a setting is declared, another
  that a control exists - neither proved the value *does* anything, which is
  the bug this project has shipped three times. The new guard caught seven
  inert settings, all added by the tuning screen a day earlier. All seven are
  now wired; the guard stands so the eighth cannot happen quietly.
- Found by that guard's sibling test: **a machine that could not be examined
  was treated as a machine with one core**, so somebody's six file-readers
  became one on any box detection could not read - in the code path that
  actually runs the index.

### Leasha now learns what your computer is good at

- **Every run says where its time went** - waiting for files, writing, working
  out meaning, storing vectors. This is the measurement the rest of the speed
  work is gated on: every idea in that list sounds plausible and at most two of
  them are worth building on any given corpus.
- **What gets timed is the critical path**, not worker time. Four readers busy
  for a minute is four worker-minutes and one wall minute, and a percentage
  built from the first is meaningless. What the run *waits* for is the honest
  measure - and it is also the useful one, because it is exactly what more
  readers would fix.
- **`leasha bench-index`** times reading, writing and the model on one fixed
  workload, in about a minute, and remembers the answer. The model was only
  ever half the question: a run whose model is fast and whose disk is slow is
  bounded by the disk, and a screen holding only the model number will
  confidently recommend a graphics card to somebody who needs a drive.
- **Auto-tune uses what was measured; Defaults uses what the specification
  implies.** That is the whole of the difference, and it is why Defaults stays
  reproducible from the box's numbers alone.
- **In Auto, an adjustment applies itself and says so in plain words** -
  "Indexing sped up: most of the last run was spent waiting for files to be
  read, so more of them are read at once now." Nobody should have to interpret
  a measurement to get the benefit of it. In Manual it waits for you.
- **The rates are keyed to the machine that produced them.** A different
  computer, an app update, three runs of disagreement, or nothing measured yet,
  and they are ignored - so the failure mode of this whole feature is "no
  better than before", never "wrong".
- Found while testing it: **a drifting run was overwriting the baseline it
  drifted from**, so the goalposts moved to meet each slow afternoon and one
  bad run permanently redefined normal. The baseline is now replaced only by a
  run that agrees with it.
- Found while testing it: `Embedder.from_settings(settings, device="cpu")` was
  a `TypeError` - two values for one argument - which is exactly the call the
  new benchmark makes to time both processors. An override that cannot
  override is not an override.

### One Index Tuning screen, on the page where you watch the run

- **Everything that decides how fast a run goes is in one place**, beside the
  progress bar rather than three tabs away. These numbers interact - workers
  and model threads multiply, a batch is memory and so is the ceiling that
  governs it - and spread across three panels somebody raises one control, gets
  a slower run, and has nowhere to see why.
- **Defaults · Auto-tune · Manual.** Defaults uses what the machine's
  specification implies; Auto-tune refines that with what past runs measured;
  Manual lets you set anything, within what the machine allows. **Switching
  back keeps your manual values stored but inert**, so trying it costs nothing.
- **A control shows the number it chose**: `Auto (4)`, not `0`. "0 means we
  decided something and are not telling you what" is the settings-screen
  failure this whole piece of work exists to end.
- **A machine card in plain words** - "10 cores (2 fast, 8 efficient) / 12
  threads · 32 GB · SSD · no graphics card" - with Re-detect and Benchmark now.
  Both run off the interface thread, because detection shells out to PowerShell
  and can take seconds on a sleeping disk.
- **Whether the graphics card is actually faster is not knowable from the
  specification sheet**, so there is a button that measures it - and the result
  says when the machine was too busy for the number to be trusted.
- **Four new strategy controls**, each one only a control because its right
  answer depends on the corpus: make text searchable first, bulk-load the word
  index, embed repeated text once, and when to read images.
- **A floor larger than the disk was settable.** Both free-space figures are
  now bounded by what the index drive actually has, with the reason printed
  under them.
- **Every ceiling says what happens when it is reached** - pauses, stops, or
  warns - because a ceiling whose consequence is unstated is one people set far
  too high out of fear, and then it protects nothing.
- **A footer shows where the last run's time went**, so Manual mode is tunable
  by evidence rather than folklore.
- Appearance left the indexing panel, where it was neither an indexing setting
  nor findable by anybody looking for one, and joined the Window group.
- Found by running it: **opening the window rewrote `.env`.** Narrowing a spin
  box's range clamps its value, a clamp emits a change, and the writer duly
  saved a number nobody chose - on a machine whose cores could not be detected,
  somebody's six workers became one just by looking at the screen. Two tests
  now stand over that.

### The machine is looked at, and one setting decides which processor runs the models

- **Leasha now knows what it is running on.** Cores are counted by kind - two
  performance cores and eight efficiency ones read differently from ten of the
  same - alongside memory, AVX2, whether the index drive is solid-state, and
  which display adapters exist. Detection only; every field is a fact and none
  of them is a decision.
- **Each tunable has a per-machine envelope** on top of its absolute limits, and
  every bound carries the sentence explaining it. Eight indexing workers is
  legal in the abstract and absurd on a dual-core laptop.
- **Extraction workers are counted, inference threads are weighted.** The first
  version weighted both and produced two workers where four had been running -
  a silent halving, justified by an efficiency-core estimate borrowed from the
  wrong kind of work. Extraction waits on the disk as much as it computes.
- **"Run models on" in Settings**: automatic, processor, or graphics card. One
  choice for the meaning model, the reranker and OCR, because a machine where
  two of the three used the graphics card is one nobody could account for.
- **The graphics card is greyed with its reason showing** when it cannot be
  used, and the two reasons read differently: no adapter is a hardware fact, no
  DirectML provider is one `pip install` away.
- **A graphics card that fails mid-load falls back to the processor and says
  so.** A driver mid-update or a device in use is an ordinary Windows
  situation, and none of them may end an index run.
- **The run log records which processor each model actually used** - what ran,
  not what was asked for.
- Switching processors **does not invalidate an index**: the same model gives
  the same vectors either way, so this costs a restart and nothing else.
- Three of the five places that built a reranker left the "results to rerank"
  and "text per result" settings at their defaults, so two Settings controls
  applied in the window and not on the command line. There is one constructor
  now, and one answer.

### The `/` menu answers the query you are building — and the CLI gets one too

- **Values are narrowed by what is already typed.** `repo:leasha branch:` offers
  that checkout's branches; `type:pdf from:` offers the people who sent PDFs.
  Which filters may narrow which is a field on the catalogue, so the dropdown,
  the CLI help and the model prompt cannot disagree.
- **Rows carry a count** — `pdf   12,431 files` — shown only when it is exact,
  and **dates show what they resolve to**: `30d   (since 28 Jul 2026)`.
- **`last month` could not be picked before.** Choosing it inserted
  `after:last month`, which parses as `after:last` — not a date, so no filter —
  plus a search for the word "month". Values with spaces are quoted now.
- **A kind word opens its extensions** as a second page, and the date menu ends
  with `custom…`, because typing a date by hand always worked and nothing said
  so.
- **`leasha shell`** — an interactive session with a real dropdown, scoped
  completion, counts, resolved dates and your history beside the index.
- **Tab completion in PowerShell**: `leasha completions install --path $PROFILE`.
  It reads a small file the indexer writes, so a Tab press starts no process at
  all; the fallback that does answers in 35ms.

### Privacy defaults - a per-account index and an offer that adds nothing

- **The index defaults to `%LOCALAPPDATA%\Leasha`**, which Windows ACLs to one
  account. Two people on one machine get two private indexes and nothing had to
  be built to separate them. Any other location is still accepted.
- **An existing install is left exactly as it is.** The installer reads `.env`
  for a `DATA_PATH` before it offers anything and, finding one, says so and
  asks nothing - no prompt, no warning, no migration.
- **A first run offers folders and adds none.** Documents, Desktop, Downloads
  and Pictures appear as one-click adds with an "add all four"; the offer
  disappears once any folder is present, so an existing install never sees it.
  Nothing outside your own Windows profile is ever suggested.
- **The shared-computer paragraph** is in the README and on the installer's
  index-location step, and a test holds the two copies to each other.
- **`doctor` says where the index is and who can read it** - one factual line,
  which always passes: a shared location is a choice, not a fault.

### Review remediation, sections 6 and 8

- **Fifteen dead knowledge-graph methods removed** from `SqliteStore` - 303
  lines that nothing in `app/`, `tests/` or `scripts/` called.
- **`filters.py` escapes `%` and `_` like the rest of the storage layer.** A
  literal `%` was a wildcard there and a literal everywhere else: `name:Q1_2024`
  matched `Q1x2024`, and `name:50%` matched everything. The escaping now lives
  in `app/storage/like.py`, which both modules import.
- **Plain text is read in blocks with a ceiling on the text one file may
  produce.** The streaming alone saves nothing - measured, 43.2MB either way,
  because `str.join` holds the pieces and the result at once - but it is what
  lets the ceiling bind before a 2GB file has already been allocated.
- **The embedder normalises in numpy**: 10.05ms to 0.88ms per batch of 256,
  agreeing with the old arithmetic to 1e-12. float64, not float32, because
  float32 changed two answers.
- **Performance floors** for the chunker, the keyword path and the filename
  lookup - the category whose absence let a quadratic chunker and a linear
  keyword path both ship green.
- **The UI-thread scanner reads the store's API instead of a list of names**,
  and found a live fault while doing it: `stats()` is three `COUNT(*)`, about
  460ms at ten million chunks, and ran on the UI thread in three places. The
  Code tab now asks `has_any_files()`, and the status bar counts on a worker.
- **The converter's real-conversion tests no longer skip on Windows.** They
  were gated on `shutil.which("soffice")`, which is None there because
  LibreOffice is not on PATH - the exact shadow the `.doc` bug lived in.

Still open in that order: the reranker's place on the critical path, which
needs a measurement from a machine that can download the model, and the
structural splits, which the owner deferred.

### Review remediation, section 5 - core, extract, CLI and storage

- **An environment variable now overrides a setting the .env file never
  mentions.** Only keys already written down were overridden, so
  `set INDEX_WORKERS=6` did nothing in exactly the case somebody reaches for
  one. `SETTING_KEYS` is the canonical list and a test holds it to the loader.
- `RERANK_TOP_N` and `RERANK_WINDOW_CHARS` go through `_as_int`, so a typo
  names its key instead of arriving as a ValueError traceback.
- **A nested archive is no longer charged for itself and for its contents.** A
  zip inside a zip spent its bytes twice, so an archive of archives read about
  half of what the setting promised and called the remainder too large.
- `extract --json --chunks --full` streams each record as it is read. It held
  every chunk's text until the last file, so the report needed as much memory
  as the corpus it described.
- Resetting the index empties the FTS tables with `'delete-all'`, with the
  content triggers lifted for the duration and replayed from `sqlite_master`
  afterwards. It was one trigger-driven deletion per chunk, each carrying the
  chunk's whole text, to reach a table about to be empty.
- **Two characters plus a filter no longer discards the two characters.**
  `/type pdf` and "q" answered with every PDF, so the list did not change as
  you typed. An empty box still means everything, and a short query with no
  filter is still refused.
- A `--fast` pass clears the content hash it did not compute rather than
  leaving the previous contents' hash beside the new size and mtime. The case
  that is not merely wasteful is a file restored to an older version: the stale
  hash matches, the row reads as unchanged, and the index goes on serving text
  that is no longer in the file.
- Deleted `config/settings.json`, a UTF-16 `"DummyApp"` scaffold read by
  nothing.

Not done, and why: seeding fresh databases at `CURRENT_VERSION` would skip the
migrations that build mail search. See the work order.


### Fixed — §3: wrong answers that looked exactly like right ones

Every item in this section returned a plausible page of results for a question
nobody asked, with nothing on screen to say so.

**`-type:pdf` returned only PDFs.** `\b` matched the operator with the minus
still outside it, the minus was then dropped as punctuation, and every negated
filter arrived as its exact opposite. `-from:noreply`, `-name:draft`,
`-repo:tools`, `-"annual report"` — all of them, all inverted. Now honoured
end to end, in the parser and in the SQL.

**Excluding a repository nearly excluded the corpus.** `repo_id NOT IN (...)` is
NULL for every file that belongs to no repository, and NULL is not true — so the
obvious clause would have hidden everything outside the named checkout. `OR
f.repo_id IS NULL` is the whole difference between the fix and a much larger
version of the bug being fixed.

**`before:2024` excluded all of 2024 but New Year's Day.** A partial date names a
*period*, and which edge is meant depends on which side of the range it sits;
both resolved to the first day. `after:2024 before:2024` — a range that reads as
"everything in 2024" — matched exactly one day. A reversed range is also
re-resolved rather than merely swapped, so `after:2025 before:2024` now covers
the same span as the correctly-ordered form instead of collapsing to two days.

**`from:josé` could never match `JOSÉ@…`, and no query could fix it.** Measured:
`SELECT 'JOSÉ@x' LIKE '%josé%'` is 0, and SQLite's own `lower('JOSÉ@x')` is
`'josÉ@x'` — `LIKE` and `LOWER()` both fold ASCII only. Meanwhile FTS5's
`unicode61` tokeniser folds correctly, so the two halves of one search disagreed
about the same name. **Schema v14** stores Python-folded copies of sender,
recipients and subject; the filters read `COALESCE(sender_lc, sender)` so an
interrupted backfill degrades to the old behaviour rather than losing rows.

**A hung retriever ended searching for the session.** The pool has two workers
and `result()` had no timeout, so one wedged LanceDB scan took a worker for ever,
the next search took the other, and everything after waited behind both. Bounded
now, with the surviving half served and the failure said out loud.

**The wildcard cache never noticed an index run.** Its comment asserted that the
vocabulary could not change underneath it; the window indexes without restarting,
so it plainly could. Dropped on a generation change.

**Two permanent latches replaced with budgets.** One rerank failure disabled
reranking until restart. Worse, one OCR engine load failure latched — and then
every image was recorded `ERR_NO_TEXT_LAYER`, which is a claim about the *file*:
a corpus of scanned documents came back as thousands of blank photographs, in
exactly the skip code the OCR pass takes its work from, so it would never look at
them again. That is now `ERR_OCR_UNAVAILABLE`, which says what actually happened.

**The cache question is settled after two reviews: built, not deleted.** The
machinery was complete and correct — index generation in the key, copies handed
out on read, both halves degrading to no-cache on error — and simply unreachable,
because `cache=` was passed at none of the four constructions. A bounded
in-memory LRU is the default now; `cache=False` switches it off. The key was
fixed first: it lowercased the raw query, so `pump AND valve` and `pump and
valve` — different searches by design, since operators are capitals — shared one
entry. The raw text is out of the key entirely now; the parse identifies the
search.

### Fixed — §2 of the review-remediation order: everything the scale run needs

Eight of the eleven items. The three left — H5, H6 and H11 — are search-side and
UI-side, so none of them blocks an index run.

**H9 — the chunker was quadratic, and it is now measured both ways.** `_atomise`
asked `any(... for position in paragraph_starts)` per word: a scan of every
paragraph in the document, for every word in it. The review measured 20k words at
4.2s and 80k at 73s, so a 1MB log spent minutes there and a 10MB one hung the
worker outright. Sorted starts plus `bisect_right` — the idiom `extract/base.py`
already used, one file away — makes it O(W log P): **80k words went from 73s to
0.14s**, and 100k now chunks in 0.18s against a 2s floor in the suite. That floor
is the real fix: this survived every release because nothing timed anything.

**H7 — pruning was one LanceDB dataset version per file.** Deleting a 10,000-file
folder produced 10,000 versions, which is exactly the fragmentation
`COMPACT_EVERY_ROWS` exists to prevent, plus 10,000 write transactions at the end
of a run. Batched at 2,000, vectors before SQLite so an interruption can only ever
leave vectors for rows that still exist.

**H8 — a deleted archive left its contents searchable for ever.** `_prune_missing`
only iterated `source_kind="file"`, and no other deletion path exists for archive
rows, so removing a 30GB `.pst` left 200,000 messages in the index, every one of
them opening to nothing.

**The first version of that fix deleted the members of healthy archives**, and the
existing suite caught it immediately. `source_kind="archive"` is not "the archive
file" — it is every row that came *out* of one, the marker and each member alike.
Containers are decided first now and members inherit the answer. An archive whose
**parent folder** has also vanished is left alone: that is an unmounted share, not
a deletion, and one wrong call there is 200,000 rows.

**M6 — vector coverage could only ever fall.** An archive's marker was written
while up to `embed_batch` of its own messages sat in `pending_vectors` with chunks
committed and no vectors. Any interruption in that window left the archive
permanently marked complete with a hole, and nothing drained it —
`iter_unembedded` was reachable only through `app.cli reembed`, a command nobody
runs because nothing ever says it is needed. The flush now precedes the marker,
**and every run repairs what earlier runs lost**, reporting how many chunks it
filled in.

**M8 — the ANN index trained on the consumer thread.** Minutes at 6.4M rows and
tens of minutes at 12.8M, triggered from `add()` whenever a batch crossed a growth
threshold, with every extraction worker blocked behind bounded queues and nothing
on screen saying a pause had begun. Nothing needs that index mid-run; `Pipeline.run`
already builds it at the end.

**M16 — `list(folder.items())` held every message body in a PST folder at once**,
and 100,000-message Inboxes are the corpus this exists for. Iterated lazily, with
the mid-enumeration failure handling kept: Outlook closing part-way through is
ordinary, and everything read before that point is now kept rather than lost with
the batch.

**M17 — three copies of every path in the corpus.** The walker, `_candidates` and
`_produce` each held every lowercased path — roughly a gigabyte apiece at five
million files — to answer one question between them. One shared set now.

**M18 — files the walk could not `stat` vanished completely.** No row, no skip, no
count, no log line: the same silent absence `NAME_ONLY` was built to eliminate,
arriving through a different door. Most are files deleted between the listing and
the stat, which is ordinary — but on stock Windows **every path over 260
characters lands there too**, so a deep tree can lose thousands of files with no
number moving anywhere. Counted by reason, because "4,812 files could not be read"
is not actionable and "4,812 paths over 260 characters" names the setting.

One existing test was rewritten rather than satisfied.
`test_a_file_is_not_marked_indexed_before_its_vectors_exist` collected every
message status seen during any embedding call and required that none was ever
`INDEXED` — true only because embedding happened twice per run. M6's extra flush
broke it while *strengthening* the invariant it describes. It now asserts the rule
itself: anything claiming INDEXED must have `chunks.embedded` set on every chunk,
which is precisely the state the original bug produced.

### Fixed — §1 of the review-remediation work order: correctness and data safety

`docs/WORKORDER-202626082352-review-remediation.md` §1, all eight items, each with the
test that would have caught it in `tests/unit/test_review_2026_08_26.py`.

**H4 — a broken embedding model took the whole search down.** Both boundaries in
`vector.search` caught `Exception` and then re-raised `AppErrorException`, which is exactly
what `Embedder.embed` raises for a model that is missing, corrupt or half-downloaded. So the
one failure most worth degrading through was the one exempted from degrading: hybrid search
failed outright and threw away the keyword hits computed alongside it. It now returns `[]`
and carries the reason out through a `problems` sink, which becomes a `NOTICE_NO_VECTORS`
naming the cause — machinery that existed all along and could never fire for this.

**H10 — `.doc` conversion was dead on Windows while Settings said it worked.**
`convert()` used bare `shutil.which`; `resolve_binary` exists precisely because LibreOffice
never puts itself on `PATH` there. Every *reporting* path used the right one and the path
that *executes* did not, so `doctor` said "route enabled" and every conversion raised
`ERR_CONVERTER_MISSING`. The same fault had been found and fixed once before — where it was
reported rather than where it ran. A test now refuses any `shutil.which` call outside
`resolve_binary`.

**H1 — every skipped file was re-parsed on every run, for ever.** `_classify` returned
`UNCHANGED` only for `INDEXED`, so a SKIPPED row with identical date and size fell through,
was fully re-parsed, failed identically and was rewritten — every incremental pass. A corpus
with 100k scanned PDFs pays hours nightly to rediscover known failures. The existence of
`_locked_candidates` was the evidence it was unintended.

**Not the allow-list of skip codes the work order specified, and it could not be.** The same
code means opposite things depending on the pass: `ERR_NO_TEXT_LAYER` is a settled answer
during a text pass and is precisely the work during an OCR one. So a skip is settled unless
the pass says otherwise. The first attempt relied on a `retry` flag set by the two
re-queueing functions, and **switched OCR off completely** — a held image reaches the images
pass through the ordinary walk, with no re-queue function to set any flag. `test_ocr_passes`
said so within seconds. `--retry-skipped` is the escape hatch for when the *machine* changed
rather than the file, and settled files are counted into `settled_by_code` so a corpus of
unreadable PDFs cannot quietly vanish from every summary.

**H2 — migration v10 permanently dropped two indexes.** `DROP TABLE files` takes its indexes
and the recreate list held six of the eight, so any database carried through v10 lost
`idx_files_mtime` and `idx_files_source_kind` for good — the first being what v5 documents as
the **6.30ms → 0.04ms** fix for date-ordered and date-filtered searches. It survived because a
*fresh* database never runs that rebuild, so every test starting from an empty file saw eight
indexes. v10 is fixed, and **schema v13** repairs the databases already damaged, with an
`ANALYZE` — recreating an index the planner has been told is useless changes only disk usage.

**H3 — migration v7 loaded the whole corpus into RAM.** One `fetchall()` over `chunks`,
inside `connect()`: tens of gigabytes at 20-30M chunks, so the upgrade exhausted memory
before the window opened, with no backup and a database stuck between versions. Now keyset
pagination — not `OFFSET`, which gets quadratically slower — with logged progress.
**A second fault surfaced while testing it**: the store opens connections in autocommit, so
`executemany` over a batch was five thousand separate transactions and ten thousand rows did
not finish inside a minute. Each batch is one explicit transaction now. The old version had
the same fault, hidden behind the larger one.

**M7 — `quoted_removed` was never written.** Schema v12 added the column, extraction measured
it, the mail preview was built to read it, and the one function that writes message metadata
did not list the key. NULL for every message ever indexed; the feature could not have worked
on any corpus. One word in a tuple.

**M1 — the shutdown guard had never run.** It sat inside `if use_cache and self.cache is not
None:`, and no cache is ever configured, so the guard documenting a thrice-reported crash was
unreachable. It is the first statement of `search()` now.

**M9 — clearing the search box left the results on screen.** `_dispatch` has always had a
branch that clears the list and the status line, and typing could never reach it:
`_maybe_dispatch` dispatches only when `tier_for` returns something other than `Tier.NONE`,
and `tier_for("")` returns exactly `Tier.NONE`. An empty box is now handled as the
instruction it is.

### Documentation — the state documents said things that were no longer true

`HANDOFF.md` 4.3 → 4.4. Its headline read *"Layers 0-6 code-complete. 1582 tests passing"*,
and both halves were wrong: **L6 was removed, not completed**, and the suite is at 3,890
collected. Its "what is next" list still had three items at the top that have since been
delivered. The index location was recorded as `D:\KnowledgeGraphData`; it is `D:\Leasha\Data`.

That kind of drift is the specific danger this project already legislates against — it is why
`ensure_log_dirs` rewrites its generated README when it no longer matches, rather than writing
it once. A state document nobody has corrected is not neutral: somebody acts on it.

The list is now grouped by what is actually blocking, and it opens by pointing at
`docs/REVIEW-2026-08-26.md` — five review passes over the current tree, **11 High and 20
Medium findings**, each checked against `file:line` before publication.

**The first draft of that section said the opposite** and had to be corrected in the same
sitting: it claimed nothing outstanding was code that had not been written, and that
everything left was a measurement, a run or a decision. That was written before the review
was read. It was wrong in the most misleading direction available — it would have sent the
next person to packaging while `.doc` conversion silently fails on the platform that ships
(H10), a broken embedding model takes the *whole* search down instead of degrading to
keyword (H4), and a database migrated through v10 quietly loses the two indexes that make
"newest first" fast (H2).

`BUILD_SPEC_V2.md` 2.9 → 2.10. **Layer 0's four acceptance boxes and Layer 1's five were
never ticked**, though every one has had a passing named test in
`tests/integration/test_layer{0,1}_acceptance.py` for months — so the spec claimed two
unfinished layers. Each box now names the test that proves it. The two boxes that are
genuinely open stay open: L4's first-search-under-3s needs the real ONNX load, and all four of
L9's are untouched. Layer 9 now says why it has not started and points at its work order,
whose own §7 forbids building until five [FINALISE] questions are answered.

`README.md` and `LOCAL_KNOWLEDGE_GRAPH_V2.md`: **Applies to** moved to v0.3.3 with no doc
version bump. Per `docs/VERSIONING.md`, that field records the version a document was last
*checked against* — both were read and nothing in them is wrong, and bumping a doc version
for a review that changed nothing would be its own small untruth.

### Fixed — five of the six things reported at once

> *"i rest the index the index size remained the same, look at alignment of
> things on settings page some of the check boxes are cut, there needs to be a
> button to clear logs, change model has only one model, the code tab is
> screwed it does not have a search box or nothing, when you close the app it
> lingers for a while because if i start it it says it is running"*

**The Code tab was hiding its own search box.** `_show_state` called
`input.setVisible(False)` whenever `repos_list` returned no repositories, so the
page lost the one control it exists for and explained nothing. Worse than
cosmetic: that list is *also* empty when the query fails — the worker's failure
is swallowed deliberately, a heading being no reason for an error dialog — so a
database problem presented as a page with nothing on it. The box now always
stays; the list is what disappears, with a sentence in its place. The two
buttons that need repositories are disabled rather than removed, and their
tooltips say which of the two things has happened.

**The window was not lingering, it was closing — and the log could not say so.**
Read from the run files rather than guessed: pid 30200's event loop did not
return until 21:20:59, and the two launches at 21:20:47 and 21:20:58 were
refused inside that interval while the one at 21:21:00 opened normally. The
single-instance lock is held for the whole of shutdown, correctly, because the
stores are still open — but the window has already gone from the screen, so
trying again is the natural thing to do. Two changes: a start now **waits**
twelve seconds for a closing copy to let go before refusing, and `closeEvent`
**times every stage and logs the total**, so the next slow shutdown names itself
instead of being inferred a week later. `ERR_DB_LOCKED` was rewritten to match —
after a twelve-second wait, *"close the other copy"* is the thing the person has
already done, so it now covers the case where no window can be seen at all.

**Resetting the index kept the file size.** `clear_index` did call `VACUUM`, so a
test of the mechanism would have passed. What it did not do was checkpoint the
write-ahead log: in WAL mode a large `DELETE` leaves every removed page sitting
in `knowledge.db-wal`, and the file group ends up no smaller — sometimes larger.
`reclaim_space` now checkpoints, vacuums, and checkpoints again, **reports what
it freed**, and logs a failure at `warning` with a fix rather than swallowing it
at `debug`. The status bar says how many bytes came back, because the headline
figure on the Indexing page is the whole data folder — which includes the model
cache, and that survives a reset by design.

**A button to clear the logs**, in Settings → Environment, beside a line saying
how much is there. An allow-list of suffixes rather than "everything in this
folder": `LOG_PATH` is a location somebody chooses, and `README.txt` describes
the layout. Files this session is writing to are named by `open_log_files()` and
kept — on Windows they cannot be unlinked, and on POSIX deleting one leaves
loguru writing into a file with no directory entry.

**The model list showed one model, and was right.** One model was installed. But
a dropdown with a single row cannot be told apart from a probe that failed, and
nothing on the panel said which it was. Known-good alternatives are now listed
greyed out, carrying `ollama pull …` — the same treatment embedding models
already had on that control. `"1 models installed."` is gone with it.

**The checkbox rule in `theme.py` was pinning the height.** Measured:
`spacing: 7px` moves a `QCheckBox` onto `QStyleSheetStyle`'s sizing, which
derives the height from the check indicator and never looks at the text — a flat
16 pixels whatever font is set, against a label needing 15. One pixel of slack.
Removed, so the height follows the font again (21 pixels for the same label);
`color` alone was measured to leave the size hint identical to the unstyled one.
**Not claimed as the owner's bug**: reproducing that needs Windows font metrics,
and the page renders correctly here either way. The rule goes regardless — a
height that ignores its own font is wrong whether or not it is today's fault.

New tests: `test_the_owners_seven.py` states each report as the rule it was
(twelve, all failing against the previous commit), and `test_settings_layout.py`
lays out the real Settings page and checks that nothing on it is smaller than
the words inside it. `test_tray`'s close-order guard was rewritten to read
statements rather than search for the literal string `"indexing_view.stop()"`,
which broke the moment the call was rearranged without the rule changing.

### Fixed — slow and crashing on first load (issue #1), and column widths, third time

**Issue #1, "Unresponsive Application"** — *"The screen is slow and unresponsive
on first loading up. The application also crashed a few times."* Both symptoms,
one cause, and it was mine.

`MainWindow.__init__` states a rule forty lines from where I broke it:
*"**Nothing runs on a background thread until construction is over.**"* It was
earned — a worker opening SQLite while the main thread is inside `_apply_theme`,
which re-polishes every widget in the tree, produced a **Windows access
violation** with no Python exception, no traceback and no window. That is why
`refresh_totals` and the two warm-ups were moved to `_start_background_work` on
the next turn of the event loop.

The watch timer for a run in another process was then started from `__init__`,
and `_poll_external_run` starts exactly such a worker. Straight back into the
race the application had already been debugged out of once. The timer is built
in `__init__` and started in `_start_background_work` now, and a test enforces
the rule that a comment could not.

### Fixed — the column widths, and this time the log said it outright

Third report, and the owner had to state the rule plainly: *"when you first
launch it should auto fit to content if no previous history else remember column
widths.. this i have told you multiple times."*

The log answered it in five lines:

    column seen width saved as 140
    column size width saved as 140
    column kind width saved as 140
    column repo width saved as 140
    column name width saved as 140

Five columns, five drags, **one number** — and 140 is `MIN_COLUMN_CAP_PX`.
`_bound_to_table` had `max(MIN_COLUMN_CAP_PX, min(width, room))` in it: a
*floor*, forcing every stored width up to 140. The width was saved and restored
faithfully, and wrong, which from the outside is identical to not being saved.

A width somebody dragged to needs no floor — a 40px column showing an icon is a
choice. Widths are stored exactly as dragged now, and the only bound is a
ceiling applied at *restore*, judged against the window then on screen rather
than the one that happened to be open during the drag.

**All three attempts were the same mistake in three costumes**: a guard I wrote
overruling the person it was meant to serve, silently. `setStretchLastSection`,
then `column_cap`, then this floor. Each time the test I added checked the
mechanism I had just changed rather than the rule, so each fix shipped green and
the report came back unchanged. The tests now assert the owner's two sentences,
and one of them refuses any arithmetic at all between the drag and the store.


### Fixed — the window would not open, and nothing had ever tried to open it

Reported by typing `leasha` and getting nothing. Two faults, both in
`MainWindow.__init__`, both mine from this session, and **neither caught by a
green suite of two thousand tests** — because nothing in it had ever constructed
`MainWindow`. Every UI test builds a single view or greps a source file.

    UnboundLocalError: cannot access local variable 'QTimer'

The trap, and worth stating in full: `__init__` used `QTimer` at line 207, and
four hundred lines later — still inside the same function — sat a redundant
`from PyQt6.QtCore import QTimer`. **Python binds names per function, not per
line**, so that import made `QTimer` local for the whole of `__init__` and the
earlier use referred to a variable that did not exist yet. The import had been
harmless for months; it became fatal the moment somebody used the same name
earlier in the same function. Seven more of that shape were found across `app/`,
every one redundant, every one waiting for the same trigger. A test now refuses
the shape outright.

    NameError: name '_read_external_run' is not defined

The second was simpler and just as invisible: the worker bodies moved to the
presenter, and the import that should have followed them was written against an
import block `shell.py` does not have — so the edit silently did nothing.

**`tests/unit/test_window_opens.py` is the real fix.** It builds the window,
turns the event loop, and selects every tab. All three fail on the parent commit
with the exact errors above.

One property of the harness worth recording: building and tearing down
`MainWindow` repeatedly in one process **segfaults inside Qt** — a window owns
threads, timers and a tray icon, and Python's collector does not destroy the C++
side in the order Qt expects. The fixture is module-scoped and deliberately does
not close the window. That is a fact about the test process, not the
application, which builds one window and keeps it.


### Added — `.gitattributes`, and a correction about `big.pst`

**There was no `.gitattributes` at all**, which means every file's line endings
were whatever each machine's `core.autocrlf` happened to be. Invisible with one
developer; with two it is a diff of the whole repository the first time somebody
saves a file. And this project has one rule that is *encoding* sensitive:
PowerShell 5.1 assumes the system code page for a file with no BOM, so a
BOM-less `.ps1` containing one pasted em dash fails at parse time with no error
anybody can act on. It killed the installer once already. The `.ps1` files are marked
`-text` so Git never converts them at all — the bytes committed are the bytes
checked out, on any machine with any `core.autocrlf`.

**The first attempt at that line was wrong and said so loudly**, which is the
useful part. `working-tree-encoding=UTF-8BOM` is the textbook answer, and it
expects the *stored* blob to be BOM-less with Git adding the BOM on checkout —
but these files were committed with their BOMs, so Git could not round-trip them
and every `git status` printed `failed to encode 'install.ps1'`. Satisfying the
mechanism would have meant renormalising the stored bytes of the four scripts
whose encoding is the thing being protected. `-text` gets the guarantee wanted
without touching them.

Adding it caused **no churn** — checked rather than assumed, because a
renormalising `.gitattributes` on an existing tree is exactly the thing that
produces a thousand-file diff nobody can review.

**And a correction.** The previous entry called `big.pst` *"a real mailbox"* and
kept it on disk on that basis. It is not: it is 5,242,880 bytes of the single
letter `x` — a synthetic fixture, referenced by nothing, and `HANDOFF-github.md`
had already established this by measurement. It is deleted. Worth recording
because the reasoning was right and the fact was wrong: I was careful with
somebody's data and had not checked whether it was data.

Also: `git gc` collected the loose objects left by the interrupted git
operations of 2026-08-26 — 536 loose objects and three packs down to none and
one, and the repository from 2,746KB to 2,491KB.


### Changed — the project folder, tidied, and the staged tree brought back in step

Asked for as *"organise the local files and folders they have to be of
professional grade and remove things we dont need"*.

**A 5MB `.pst` was committed and referenced by nothing.** Not a fixture, not
named in any test — a real mailbox sitting in the repository root and carried in
every clone. It is untracked and gitignored now, and **left on disk**, because it
is the owner's file: getting it out of git is the fix, deleting somebody's
mailbox is not. It stays in history; rewriting that is a separate decision.

Removed from the working folder, all of it already gitignored and all of it
regenerable: two JVM crash dumps (`hs_err_pid*.log`), Visual Studio's
`UpgradeLog.htm` and its `Backup\` copy of the solution, `.vs\`, and four cache
folders. The `.gitignore` now names the crash dumps explicitly — they were
already caught by `*.log`, and naming them means the next person to find one
knows what it is instead of deleting a file that might have mattered.

Both logos moved into `assets\` beside the icons, renamed to say what they are.
`CODE_REVIEW.md` joined the other documents in `docs\`.

**`BUILD_SPEC_V2.md`, `HANDOFF.md`, `LOCAL_KNOWLEDGE_GRAPH_V2.md` and
`GitSearch.txt` stay at the root**, deliberately. Each is named by path in tests
and referenced from twenty-odd places; moving them would be churn with breakage
attached and no gain beyond a shorter listing.

**And the staged tree was rebuilt.** `Leasha\` — the gitignored run-as-installed
copy — held **no `walk_complete` at all**, which is the whole of an earlier
progress-bar fix: in that copy the bar reaches ~97% within seconds and stays
there. Anyone launching `Leasha\leasha.cmd` was running code from before several
sessions of fixes and seeing bugs that had already been repaired. It now carries
the run lock, the debug pane and the rest.


### Changed — a quieter, denser look, and tooltips that were only half there

Asked for as *"make the look modern professional and intitutive"*, and settled
as **quiet and dense, like a pro tool** rather than roomy and soft.

**The palette dropped its chroma.** The greys were blue-tinted, which reads as a
consumer app; a tool somebody keeps open all day should recede and let the
content be the only thing with colour in it. Two clear steps of lift — window,
surface — and one more for a raised control, instead of four greys nobody could
put in order by eye.

**One type scale.** Sizes had been chosen per widget — 10, 11, 12, 13, 15 —
which is five doing the work of three with no relationship between them. It is
12 for metadata, 13 for body, 15 for the two headlines that earn it. The search
box stays large: it is where every session starts, and a cramped input invites
cramped queries.

**Three things had never been styled at all**, and they were the most dated
elements on screen precisely because they were everywhere. The scrollbars still
had the native steppers; menus and tooltips arrived in the platform's own
colours, so a light context menu opened over a dark window. Thin overlay
scrollbars, and both menus and tooltips now belong to the application.

Also: hover states on every row (a dense list with no hover gives no sign a row
is a target at all), a two-pixel focus ring rather than a hue change (width
carries it for anybody who cannot separate the hues), tighter buttons and tabs,
and group boxes that read as sections rather than as a page of heavy rectangles.

**And the tooltips.** Presumed to be everywhere — *"i presume tool tips are used
every where to be helpful"* — and measured at **55 of 100** interactive
controls. The gaps were not the harmless ones: **Start indexing**, the most
important button in the application, had none, and neither did any of the three
radio buttons deciding what happens to an existing index, whose consequences are
days of re-indexing apart. Every one now says what pressing it will *do*, which
is a different sentence from what it *is*, and a test counts them so the answer
stays a measurement. A `QLineEdit` is exempt: a placeholder is visible without
hovering, which is strictly better.

The 250-line view guard fired three times during this and was right each time.
The Indexing page's buttons became `widgets/index_controls.py` — where the copy
saying what each promises belongs anyway — and the debug pane became its own
group box rather than something Settings assembles around it.


### Fixed — the git tree listed every repository whatever you typed

`WORKORDER-202626081801` §2. *"when search criteria is typed in git view it
should only show gits that have documents which match not all gits"*.

`show_repos` was handed the full list and drew it. Type a term matching files in
one checkout and the other three sat there as though they matched too — and a
tree is read as *"these are the repositories that have what you asked for"*,
which made it the most misleading pane in the application. The same fault as
`code_type_filter` and the empty-list states in the code-tab order, and worse
here because of what a tree implies.

**The count comes from the same query the list ran**, which is the whole design.
`repos_with_matches` composes `file_filter_sql` exactly as `browse_files` does —
same filter, different projection — so if that definition changes both move
together. A separately-worded count is how a tree and a list come to disagree.

It is deliberately **uncapped** where the list is capped at 500: *"does this
repository hold a match"* is not a question about the first 500 rows. And both
halves are fetched in **one worker**, so nothing reaches the store on the
keystroke path and a branch scope is still answered from the listing already
fetched — never a fresh `git ls-tree`.

A repository with no match is **hidden, not greyed** — a tree of four with three
inert rows is the noise being removed — and the count above it says `2 of 4
repositories match`, because a tree that has silently shrunk is as bad as one
that silently shows everything.

### Fixed — mail previews now show the message rather than the index

`WORKORDER-202626081801` §3. *"also the mails dont preview properly"*. Three
things had happened to a message before it reached the pane, and none of them
had been decided — they fell out of reusing the indexed text for display.

**No headers.** From, To, Sent and Subject are columns in the table and the
preview had none of them, so a message read on its own had no context at all.
They are on the row already, so this is presentation rather than a new query.

**The quoted thread was gone, silently.** `strip_quoted` removes quoted replies
and signatures at index time — correctly, or a thread quoted twenty times is
indexed twenty times — so a reply previewed as though it had been sent with no
context. The amount removed has been *measured* since quoting was built and only
ever logged: the overnight run reported *"stripped 38,609 chars"* against a
message, into a file nobody reads while looking at that message. Schema v12
stores it, and the pane says *"Quoted reply and signature removed — 38,609
characters"* on its notice line. **`None` is not zero**: a message indexed before
v12 does not know, and claiming nothing was removed would be an invention.

**Chunk boundaries were paragraph breaks.** `"\n\n".join(...)` put a blank line
at every seam, so a long message read as arbitrarily broken paragraphs in places
decided by a 512-token window. Chunks are contiguous slices, so joining them
with nothing restores the text as extracted; a single newline goes in only where
a trimmed seam would otherwise run two words together.


### Fixed — one lock was doing two jobs, and the smaller one was winning

Reported as *"if leasha is open i cant get another cli interface to start
indexing"*. `app/main.py` held `SingleInstance` for the entire lifetime of the
window and `app.cli index` took the same mutex, so having Leasha open made
indexing from a terminal impossible.

The refusal was correct in form and wrong in scope. What cannot overlap is two
**writers** — two pipelines against one LanceDB table corrupt it — and that
hazard lasts as long as a *run*, not as long as a *window*. A window that is
merely open is a reader.

Two locks now. `GUI_MUTEX_NAME` still refuses a second window; `INDEX_MUTEX_NAME`
is held by `app.cli index`, by `app.cli reembed`, and by the window's own run
alike, for exactly the length of the run. Whoever asks second is told who has it
and since when, under its own error code — *"an index run is already in
progress"* is a different sentence from *"another copy of the application is
running"*, and the second one told people to close a window they did not need to
close.

**The mutex is the authority; the record beside it is only a description.** A
process that dies has its mutex released by the operating system and leaves its
row in `index_state` behind, so a record without a lock is a stale record and
never a reason to refuse. Reading it the other way round would mean a crash
during indexing locked the feature until somebody found the right table to edit.

### Added — the window shows a run it did not start, and can stop it

The state the split created: an index genuinely under way with nothing in the
window's process knowing about it. The bar sat at zero, Start stayed enabled,
and pressing it produced a lock error for something the window should simply have
been showing.

Half the plumbing already existed and had never been connected. `cursor:last_path`
and `cursor:indexed` were written on every checkpoint and read by nothing outside
the test suite — the docstring said *"progress for the UI"* and no UI had ever
read it. The run now publishes a snapshot of its whole `IndexStats` under one key
and the window polls it every four seconds. One JSON blob rather than a spread of
keys, because a reader in another process can otherwise catch a half-written set
and draw a bar from one instant's numerator and another's denominator.

Stop reaches across processes too: a flag in the database, polled by the runner
at its next checkpoint. A request rather than a kill — terminating the process
would leave the vector store mid-write, which is the one thing the lock exists to
prevent.

### Fixed — three more things wrong with the progress bar

Reported as *"the progress bar was not working properly"*, and worth reading in
order of likelihood.

**Most likely: the installed tree was stale.** `Leasha\` — the gitignored
run-as-installed copy — had **no `walk_complete` at all**, which is the whole of
an earlier bar fix. In that copy the denominator is `stats.seen`, which the
bounded 256-deep work queue keeps a hair above `done`, so the bar reaches ~97%
within seconds and sits there. Anyone launching `Leasha\leasha.cmd` saw exactly
that. Re-staged, and worth checking which launcher was in use before reading any
of the below as the cause.

**A stopped run finished at 100%.** `pipeline.run` returns normally after
`request_stop()`, so `finished` fires for a stop exactly as it does for a
completed run — and `_on_finished` set the bar full unconditionally, next to a
headline saying the run had been stopped. The panel contradicted itself. The
identical fault had already been found and fixed in `_on_failed` and not here.

**Busy mode looked like a frozen full bar.** `setRange(0, 0)` is what an index
shows for most of its length, because the size of the job is unknown until the
walk ends. Under `QStyleSheetStyle` a styled `::chunk` with no `width` paints
across the whole groove and does not animate, so *"we do not know yet"* was
indistinguishable from *"finished, and stuck"*.

And the reason it was indeterminate at all: **nothing in the window could produce
a total.** `app.cli scan` was the only writer of that number, so a GUI-started
run had `total_estimate == 0` every time. There is a **Scan first** button on the
Indexing page now. It reads no file contents, so it takes no run lock.

**Every previous progress-bar test was a string grep of the view or a call to
`progress_for` with a hand-made dataclass — nothing had ever instantiated the
widget.** Three bar bugs have now shipped past a green suite. The new tests build
the real thing.

### Changed — the window opens without a console, and Settings can say why

`leasha.cmd` launched the window with `python.exe`, the console-subsystem binary,
so Windows attached a terminal to every session: an empty black rectangle behind
the application that nobody could close without killing Leasha with it.
`pythonw.exe` removes it, with a fallback to `python.exe` so an installation
missing pythonw still opens.

The console was doing real work, though, and could not simply be deleted. Under
pythonw `sys.stderr` is `None` — and `logger.add(None)` is not a no-op, it is a
failure that takes the whole logging setup with it, so the window would have
started with no file log either. Guarded, and the running commentary now goes to
a ring in memory that **Settings → Recent activity** shows, fifty lines,
scrolling, with Copy. Not a log viewer: the question it answers is *"is anything
happening"*, and everything past that is what the log file and `app.cli` are for.


### Fixed — column widths, again, and this time it was the cap overruling them

Reported in the same words a second time: *"the ui still does not remember
column widths"*. The last fix corrected two real faults — `setStretchLastSection`
owning the last column, and a "Fit columns to contents" that did nothing — and
the widths still did not stick, because a third thing was overruling them.

**`column_cap` is 40% of the viewport, and it was being applied to widths
somebody had dragged.** On a 900px table sharing its width with a preview pane
the viewport is around 625px, so the cap is 250px: a column dragged to 321 was
*stored* as 250 and *restored* as 250. Measured on a real table — the drag saved
`folder=321` and the column came back 250 on the next launch. Every link in the
chain was working. The width was saved. It was then trimmed, twice, silently.

The cap exists for a good reason and it is a reason about **fitting**:
`resizeColumnsToContents` over a corpus of long Windows paths produced a Name
column that ate the row, which is a measurement nobody asked for. Applying the
same ceiling to a deliberate drag is a different act — it overrules a choice, and
the only thing the person sees is a column that will not stay where they put it.

So the cap now governs automatic fitting only. A column with a saved width is
exempt from it, at both ends: stored as dragged, restored as stored. What still
bounds a chosen width is the table itself — a column dragged on a wide monitor
and restored on a narrow one can end up wider than the window and unreachable,
which is a usability floor rather than a matter of taste.

**The existing test should have caught this and did not**, which is worth as
much as the fix. `test_a_dragged_width_survives_a_relaunch` drives the whole
chain — drag, save, close, reopen, fill, restore — and passes, because it drags
to 320px on a 900px table, which sits just under that table's cap. Right shape,
one wrong number. The new tests derive the width from `column_cap` itself
(`cap + 120`) so they cannot be satisfied by a value the cap happens to allow,
and two of the four fail on the parent commit.


### Fixed — the embedding gap, and it was a window rather than a bug

Task #50, open since the coverage line was added and diagnosed backwards the
whole time. On the owner's index **154 of 3,355 passages had a vector** —
meaning-based search silently doing a twentieth of its job. Every previous fix
was to the *reporting*: `stats` and `doctor` learned to compare the two stores
and say so. Nothing had looked at how the two stores came to disagree.

**`_write_one` committed a file's chunks and deleted that file's old vectors
immediately, while the replacement vectors were deferred** to `_embed_pending` —
up to 256 chunks and one lazy ONNX model load later. Every abort inside that
window was pure, uncounted loss: a model that would not load, a window closed
mid-run, the disk floor, an exception in a progress callback.

What turned a batch-sized fault into a corpus-sized one is that **it
compounded**. A file whose flush never happened stays `PENDING`, so the next run
picks it up, reaches the delete, and destroys the vectors of everything it
re-reaches *before* failing in the same place. Each run left coverage lower than
it found it — and `--force`, the natural thing to try on seeing a gap, put every
file in the corpus on that path.

The delete now happens inside the flush, immediately after `embed_all` and
immediately before the add, so at every instant a file has either its old
vectors or its new ones. The original reasoning for the placement was about lock
contention — the LanceDB delete is slow and the SQLite write lock is held for
the whole batch — and that reasoning was right; it is preserved, the delete is
simply later. The one case the flush cannot reach, a document that chunks to
nothing and so never joins the batch, deletes its own vectors rather than
orphaning them.

**The model loads before the walk starts.** It loaded lazily on the first
`embed()` call, which is inside the flush, so `ERR_MODEL_LOAD` killed the run
having already orphaned a batch — and since those files stay `PENDING`, the next
run reached the same place and orphaned another. `warm_up` has existed since
Layer 4 so the first *search* would not pay the load; indexing never called it.
A model that cannot load now costs nothing written at all.

**Reporting can no longer cost the flush.** `_checkpoint` and `on_progress` were
unguarded, and the CLI's progress line prints a *filename* to a Windows console
— so one path outside cp1252 was a `UnicodeEncodeError` that escaped `_consume`
before the final flush. A character in a filename cost the whole pending batch
its vectors.

**And the run says so now.** `IndexStats` had a `chunks` counter and no vector
counter, so a run that wrote 3,355 chunks and 0 vectors reported `-> 3,355
chunks` and stopped — success, by every measure the run produced. The gap was
only ever discoverable afterwards, by a diagnostic nobody runs against a run that
said it worked. `vectors` and `embed_failures` are on the stats and in `--json`,
and the summary prints coverage **only when the two disagree**: a line reading
"3,355 of 3,355" on every healthy run is the noise that teaches people to skim.

Two smaller things found in the same sweep. `reembed` checked `if written:`
rather than the count, so a short write marked the whole 256-chunk batch
embedded — the exact bug `78aa392` fixed in the pipeline, still standing on the
path people run *to repair* it. And `VectorStore.drop()` reset two of its four
cached counts, so the run after a `reembed --all` believed an empty table held
thousands of rows and re-enabled the per-document delete that `db17d1c` removed.

Eleven tests, driving the real pipeline rather than the module — the lesson from
the archive work, where twenty-nine passing unit tests sat beside an extractor
that could not run at all. Seven of the eleven fail on the parent commit. The
load-bearing one is `test_a_failed_run_does_not_leave_less_coverage_than_it_found`:
the ratchet is what made this 95%, and a fix that stopped the loss without
stopping the ratchet would look correct on a green first run and rot exactly as
before.


### Added — `/newest`, and the two tabs that were sorting by relevance in silence

`WORKORDER-202626081059` F6 and F7, and the rest of the search-quality order.

**Nothing in this application could answer "which is the latest one".** Every
result list was ranked by relevance and there was no way to ask for anything
else, on a corpus where the newest version of a document is very often the
question. `/newest` and `/oldest` are one command with three spellings — `/sort
date` still works — and the sort happens **after** reranking, so the set is
still the best matches and only the order changes. The notice says so, because
a list that stops being ranked without saying it looks like broken ranking.

The interesting half was the tabs. The engine sorts the fused hits, which covers
Search; Files, Code and Mail never reach the engine — they read the store
directly — so `/newest` parsed cleanly there and then did nothing whatsoever.
`browse_files` and `browse_messages` honour it now. That is the contract set
when the switch vocabulary was unified — *a tab offers what it can honour, no
more and no less* — and a switch that parses and then changes nothing is the
kind of quiet lie that costs the other forty switches their credibility too.

A sort is deliberately **not** a filter. `has_filters` decides whether a query
with no text is worth running, and counting `/newest` would turn a bare one into
"everything, newest first" — a listing, not a search. The test that checks every
command's example is copy-pasteable now checks the sort field for this one.

### Added — the window says when a search was degraded, and offers the fix

Same order, F6/F7. `SearchResponse.notices` has existed since repositories were
added and the only place it reached was a log file, so every degraded search —
a wildcard that hit its cap, a stemmed expansion, an ANN index that answered
nothing — looked like an ordinary disappointing result.

Three things now surface above the results. A **kind-word chip**: typing "get me
all excel files" offers `type:excel` as a link, because the word for the file
type is in the sentence and the filter is one click away. An **Interpret hint**,
shown only where it would pay — a long query, no operators already, a kind word
in it — because a button whose value is invisible until pressed does not get
pressed, and a hint on every search is a hint nobody reads. And the engine's own
notices, drawn.

Suggestions are applied **only on a click**. Appending a filter to somebody's
query on their behalf is the behaviour that makes a search box feel possessed.

### Added — OCR where it pays: `--only-ocr` retries the rows the text pass wrote

`WORKORDER-202626081052-ocr-strategy` §4–§6. §3's measurement — one folder,
timed, on the owner's machine — is the owner's and no unit test stands in for it.

Whether a PDF needs OCR is not knowable from its extension. `reads_by_ocr` asks
the resolved extractor and answers False for every `.pdf` — correctly, because
most have a text layer — so the images pass narrowed its walk to `.png`/`.jpg`
and never revisited a scanned manual, however many times somebody ran
`--only-ocr`. The text pass had already found them and said so, one
`ERR_NO_TEXT_LAYER` row each; the images pass reads those rows back. An indexed
query over a few hundred rows, not a second walk of 1.5TB.

A picture-heavy `.pptx` was warned with the same code as a scanned PDF. They
read identically to a person and mean opposite things to the indexer — one is
work to retry, the other is work this strategy explicitly declines at 150 hours
for logos and icons — so sharing the code would have had `--only-ocr` queue
every infographic in the corpus. `ERR_MOSTLY_PICTURES` now, counted into the run
summary, which makes *"412 decks are mostly images"* a number rather than a grep.

`PDF_OCR_PAGES` is in Settings, defaulting to 0; the environment variable still
wins so one run can be given a different budget. A budget rather than a switch:
at ~3.6s a page, twenty pages is about a minute a document and covers the title,
contents and introduction, where all-or-nothing is the sixty-hour column. The
progress line says "reading with OCR" during that pass — OCR moves at seconds
per page where the text pass moves at hundreds of files a minute, and a run that
looks stalled gets killed.

### Added — reading inside `.zip` archives, with every guard the cost needs

`WORKORDER-zip-archives` §3. §1a made every archive findable by name; this makes
what is inside one findable by its contents. The §6 gate — *do this after the
first full index* — was waived by the owner.

The shape was not invented: `.pst` already solved "one file on disk producing
thousands of documents", so `source_kind`, `virtual_path` as `container/member`,
per-document digests and `unchanged_documents` are all reused. `zipfile` is in
the standard library, so `.zip` costs no dependency; `.7z` and `.rar` do, and
stay out of scope until `scan` says the corpus holds enough of them.

Most of this is guards, for the reason §4 opens with — *every one of these is a
way an archive takes down a run that would otherwise have finished*. Bombs are
refused from the central directory, where both sizes are in the header, with a
size floor because a 5KB log of one repeated line compresses a thousand to one
and is harmless. One byte budget per archive, shared with everything nested
inside it. Depth 2, absolute rather than adaptive — zip quines exist. Encrypted
members read from the flag bits before anything is attempted. Traversal names
refused rather than sanitised, and checked again where the bytes land. Members
extracted one at a time and deleted immediately, through a context manager so an
early return cannot skip it.

Three new `SKIP_CONTINUE` codes: one bad archive is a line in a report, never the
end of a five-day index. `ARCHIVE_READ_INSIDE` and `ARCHIVE_MAX_MB` in Settings,
because off by default is wrong and on by default is dangerous.

**Two bugs that twenty-nine passing unit tests could not see**, both found by
driving the real pipeline. The extractor was registered as a class rather than an
instance, so every call was an unbound `extract(path)` — the feature could not
run at all, while the tests, which call `read_archive` directly, all passed. And
members written with `source_kind='file'` were indexed and then deleted in the
same run, because `_prune_missing` removes any file row whose path is not on
disk and `backup.zip/q3/report.docx` never is: four documents indexed, one chunk,
nothing findable, no error anywhere. Both now have tests that drive the pipeline
rather than the module.

### Fixed — repository attribution can be undone, and the Code tab says what it hides

`WORKORDER-202626081149-code-tab`. The owner reported a UI symptom — *"in git
view i dont see the files"* — and investigating it produced a data-integrity
finding.

A copy of this project's own `.git` had been dragged into `D:\SearchData`, a
document archive, with its working tree emptied. Detection found the `.git` and
adopted the folder, so **1,179 of 2,677 indexed files — 44% of the corpus** —
were attributed to a repository. `scope:code` is `repo_id IS NOT NULL`, so "Code
only" matched the whole archive.

Attribution was a one-way door, by three independent mechanisms, and all three
are fixed. Nothing pruned `repos` — `prune_repos` now does, but only for roots
the walk actually reached, because a repository on an unmounted drive has not
disappeared. Nothing ever set `files.repo_id` back to NULL — `forget_repo` does,
keeping every row, chunk and vector, since what a file loses is only the claim
that it is code. And `COALESCE(excluded.repo_id, files.repo_id)` meant even
`index --force` could not clear one; that guard is right on its own, so it stays
and `NO_REPO` overrides it. A defensive guard that cannot be overridden is not a
guard.

An ignore list, because forgetting is otherwise undone a minute later — finding
a `.git` **is** how a repository is registered, so "I have looked at this and it
is not a checkout" has to be recorded where detection reads. Reversible with
`repos --remember`.

`app/index/repo_health.py` flags a repository whose indexed files are
overwhelmingly not code, or whose tree reads as deleted. It reports rather than
refuses: a real checkout full of documentation must not silently stop being code.

Separately, `DEFAULT_PRESET` is `build`, which excludes `.md`, `.txt`, `.json`,
`.yml` and `.csv` — so `README.md`, `package.json` and `requirements.txt` were
filtered off the Code tab on a fresh install with nothing on screen saying a
filter was active. The summary names the preset and the arithmetic now, and the
three kinds of empty — no indexed files, all hidden by the filter, the query
excluded them — each have their own sentence instead of rendering identically as
nothing.

### Added — wildcards that work, and three parsing faults behind them

`WORKORDER-202626081106` and `WORKORDER-202626081059` F1, F4 and F5. One pass
over the parser, because they edit the same two regexes.

A trailing `*` was always real FTS5 prefix matching; the other two things people
type failed silently. `*voice` had its star dropped and searched for "voice" — a
wildcard search that looks like it worked — and `inv?ice` split into `inv` and
`ice`, two unrelated words.

`fts5vocab` costs nothing: a virtual table over the term dictionary FTS5 already
stores, so migration 11 adds it to an existing index instantly, with no rebuild
and no disk. A wildcard becomes a LIKE over the vocabulary and the matching terms
become an ordinary OR group, so ranking, fusion, filters and the symbols column
are untouched.

**The design constraint was found by testing, not by reading.** The vocabulary
holds Porter stems, so `LIKE '%voice'` matches nothing at all — the stored form
is `voic`. Fragments are stemmed by asking FTS5 itself through a scratch table
with the same tokenizer, rather than by putting a second Porter implementation in
Python that would agree with SQLite until the day it did not. Suffix wildcards
are therefore approximate — `*voice` also reaches `invoicing` — and the notice
says so.

Bounded on four sides, because a search box that can be made to hang is not a
search box: 200 terms ordered by document frequency so the cap keeps the useful
ones, a floor of two literal characters, a time budget, and a per-session cache.
Wildcards never reach the embedder — `*voice` is not a sentence.

Three parsing faults fixed alongside. Thirteen instruction words — find, show,
get, latest, newest — went into the FTS expression as content, so *"find a
project execution plan"* searched for `find`, which in an archive of project
documents matches thousands of files and drags the ranking with it; they are a
second list rather than more stopwords, applied only while something else
survives, because a stopword is never worth searching for alone and `latest`
frequently is. `_TERM` excluded underscore, so `DF_` searched for `DF` and
`__init__.py` searched for `init` and `py` — which matters more since this
application went from 34 source types to 405. And the no-vectors warning had
fired sixty times in the owner's log and been wrong sixty times: `vector.search`
returns `[]` for three reasons and two of them are ordinary. It now requires that
something was actually embedded. A warning that is wrong sixty times out of sixty
trains everyone to ignore it, and this one guards a real failure.

### Fixed — column widths, and the menu item that was supposed to restore them

Reported together, and they turned out to be two faults in one file: *"the
column width of the columns in the tabs reset every launch"* and *"the autofit
menu function does not work"*.

**"Fit columns to contents" did nothing at all.** `_apply_widths` gated its
re-measure on `not FITTED or prefs.widths`. The menu item clears `widths` — so
once a table had fitted itself, the whole condition went false and the one line
that fits anything was skipped. It cleared the saved preference and left the
columns exactly as dragged: measured on a real table as `[250, 47, 588]` before
and `[250, 47, 588]` after. The preference and the screen then disagreed, which
is worse than either. Fitting is now an explicit request that resets the table's
own flag, rather than a state the restore code had to infer from an empty tuple.

**The last column could never keep a width.** `setStretchLastSection(True)` was
unconditional, and Qt recomputes that column on every layout — so a width
dragged there was overwritten within the same repaint, and a saved one was
overwritten on restore. On a table whose last column is the one worth widening,
that is the whole of "it does not remember my columns". Stretching is the right
default and the wrong override, so it now holds only until somebody takes
control. `_cap_columns` already made the same exemption; the two now agree.

Also fixed, as a side effect of the same condition: a single dragged column made
**every** subsequent fill re-measure **every** column — a full re-layout per
keystroke on a debounced list, fighting the drag that created the preference.
The widths came out identical either way, which is why it went unnoticed; the
test counts the calls rather than the pixels.

The remaining unknown is stated rather than guessed at. Whether a real drag on
Windows reaches the recorder depends on `QApplication.mouseButtons()`, and no
offscreen test can hold a mouse button down — every link either side of it is
covered. Both branches now log at DEBUG, so the next run distinguishes "never
saved" from "saved and then lost".

### Added — the search box reaches repository history, not just the index

The other half of *"the main search searches every thing no matter what"*, and
the half the owner had to say twice: *"it is not just a switch union it is union
of all data as source too"*.

The search box now offers **all 41 switches** — the eleven index ones and every
git switch whose spelling the index does not already claim — and a query
carrying a repository-only switch (`/history`, `/branch`, `/author`, `/class`…)
runs against the repositories in the index and folds the results into the same
list, labelled `repository history · leasha · a1b2c3d4 · Dave · 2024-06-01`.

**Three existing guards shaped where this lives, and each was right.**

- `SearchEngine` must stay git-free — it is what answers a keystroke — so
  federation is `app/search/federate.py`, beside the engine rather than inside
  it, and the caller joins the two.
- Nothing on the typing path may import `gitsearch`, so `federate` imports it
  *inside the function*. That is not a way past the guard; it is what makes the
  guard true, and a test asserts that importing `federate` does not pull git in.
- Views are held under 250 lines. `search_view.py` stood at 247, so the guard
  fired the moment this went in — correctly. The decisions moved to
  `presenter.git_pass` and `federated_summary`, the worker to
  `widgets/history_pass.py`, and the construction that was never view logic to
  `search_bar.build_controls` / `build_toolbar` and
  `results_view.build_results_pane`.

It runs on the **full tier only** and only when a git switch was typed, as a
**second** worker: the index answers in milliseconds and history in seconds, so
its rows are appended to what is already on screen rather than replacing it —
`ResultsView.append_results`, which never moves the scroll position. Repositories
are capped at eight, because twenty checkouts is twenty subprocesses.

**Two bugs found by testing rather than reasoning.** The first version of
`wants_git` compiled its pattern into `_TOKEN`, a name `gitquery` already used
further down the module; the later definition won, and the function answered "no
git switches here" for every line ever typed — a feature that silently never
ran. And `after`/`before` are aliases of git's `/since` and `/until`, so an
ordinary date filter resolved to a repository switch and would have forked
`git log` on every search carrying a date. Spellings the index claims now belong
to the fast engine, the same precedence the Code menu already used.

`app.cli search` federates identically — a feature added for one entry point is
added for the others.

### Fixed — one switch vocabulary, and one definition of what a switch means

Reported from the window: *"the switches Search should have all switches, files
should have all switches (files, Mail and Code), mail should have mail switches,
code same switches has files — this is not the case, and the results should be
same across but only applicable to the tab — this is not the case."*

Both halves had one cause. **Each tab wrote its own filtering**, so each had its
own idea of which switches existed. Files honoured `type:` and a name and
dropped the other nine one function above the store call. Code honoured three —
and silently ignored two of those, because `CodeRoute` carried `text`, `repo`
and `extensions` and the dropdown offered `/name` and `/path` anyway. Mail knew
its five columns and nothing about the file a message came from. The menus were
trimmed to match, which made the gap read as deliberate rather than missing.

`app/storage/filters.py` now holds `file_filter_sql` — moved out of
`keyword._filter_sql`, which was private to Layer 4 and therefore reachable only
by the generic search. It is the single definition of what every switch means
against `files`, and all four surfaces compose it. **A tab decides which rows it
is about and never what `size:>1mb` means**, which is exactly "same across, but
only applicable to the tab".

What that buys, concretely:

- **Files** honours all eleven. `/from dave` is not a mail search here — it
  narrows to the mail *files* on disk whose sender matches, which is a question
  about files.
- **Files matches contents as well as names and folders**, asked for directly.
  `SqliteStore.browse_files` unions the filename index with BM25 over chunks, so
  the same words typed in Files and in Search no longer give two different sets.
  `/name` narrows back to filenames with **no mode flag and no second code
  path** — it is an ordinary conjunctive filter on the basename, so a row that
  matched only on its contents cannot satisfy it and the content half falls away
  on its own.
- **Code** is one query rather than two. The tree's selection is injected into
  the parse as `repos`, so a repository picked on the left and one typed as
  `/repo` reach the filter by the same route; `scoped("code")` is what keeps the
  tab about repositories.
- **Mail** keeps its own columns for the mail fields — the trigram header index
  is faster, and `after:`/`before:` belong on `sent_at` rather than the file's
  mtime, which for a PST is one date shared by every message inside it. The
  file-level switches are appended to the join it already had.

**A bug the union nearly hid.** SQLite refuses `MIN(bm25(...))` — an auxiliary
function is only defined on a row of the FTS query itself — and the refusal
arrives as `OperationalError`, which the fallback caught. The tab went on
returning name-only matches while looking entirely healthy. Each half is now
scored in its own subquery, and the fallback is narrowed to a genuinely missing
table so it cannot hide this again.

Also fixed: `test_t9_migrating_v5_to_v6_preserves_every_file_row` asserted the
schema lands at 6, so it failed the moment NAME_ONLY took it to 10 — for a
reason unrelated to what it tests. It asserts `CURRENT_VERSION` now, which also
makes it the one test carrying a populated pre-v6 database all the way forward.

### Added — `scan` reports what is inside the archives, without opening any of it

`docs/WORKORDER-zip-archives.md` §6 refuses to let section 3 be built on a
guess: *"Not because it is unimportant — because the cost is unknown… Deciding
before that number exists is guessing, and guessing at 1.5TB is expensive."*
This is the number.

`app.cli scan` now counts archives and reads the **central directory** of a
sample of them. A zip carries, at its tail, a list of every member with its
compressed and uncompressed size — so *"what is in the 240GB of `.zip` in this
corpus"* costs a seek per archive rather than a read of 240GB. Nothing is
decompressed anywhere in the module, and the report says so, because the claim
is unusual enough to be worth stating.

The report gives archive count and size on disk, how many were listed, the
estimated member count and uncompressed size across the corpus, **how many of
those members are types something here can already read** — the single figure
section 3 turns on — and the commonest member types. Members are routed by
`tier_for`, the same function that routes a file on disk, so the estimate
cannot drift from what an index run would actually do.

`.7z`, `.rar`, `.tar`, `.cab` and `.iso` are counted and never opened: reading
them means taking a dependency, and whether that is worth it is a separate
decision that deserves its own number. `.docx`, `.xlsx`, `.odt` and `.epub` are
deliberately **not** counted — they are zip containers with extractors already,
and folding them in would make the answer "most of your corpus" on every corpus
in the world.

**Three guards, none decorative.** `infolist()` builds an object per entry
eagerly, so an archive declaring forty million members costs tens of gigabytes
to *list* — no decompression, nothing for a ratio check to catch. The declared
count is therefore read from the end-of-central-directory record, following the
ZIP64 locator when present, and refused before `zipfile` is handed the file at
all. A member declaring an expansion over 200:1 is counted apart rather than
added to the corpus total. And a corrupt archive is one line in a report with
its reason kept, never the end of a scan of 600GB.

The ratio guard needed a size floor, found by testing rather than reasoning: a
5KB log of one repeated line compresses a thousand to one, and so does a
zero-padded header. Judged on ratio alone, the measurement built to answer *"how
much is locked up in archives"* was quietly excluding the ordinary contents of
every archive it looked at. A bomb is dangerous because of what it expands
*to*, so the expansion now qualifies it.

### Added — every file in an indexed folder is findable by name

`docs/WORKORDER-zip-archives.md` §1a, which the order calls *"the larger half"*:
*"the files search should include all files, not just the ones we have read the
content of — all files in the search folder."*

A `.zip`, an `.mp4`, an `.exe`, a 40GB disk image, a zero-byte marker: each
produced **no row at all**. Not indexed, not in the skip ledger, nothing
anywhere recording that it had been passed over. Invisible is the worst of the
three possible answers — somebody who can see the file in Explorer and cannot
find it in Leasha concludes the index is broken, and they are not wrong.

Now the walk yields them and the pipeline writes a name, path, size and date.
One INSERT on top of a `stat` the walk already performed. **Nothing is opened,
hashed, extracted or embedded**, which is the whole reason this is affordable
at 1.5TB.

A new status, `NAME_ONLY`, carries the distinction. Not `INDEXED` — a row
claiming its contents were read while holding no chunks is the exact failure
that made `--force` necessary. Not `SKIPPED` — nothing went wrong; there is no
reader for a `.mp4` and there was never going to be. The summary says so in
those words, with the extensions broken out, because *"30% of your corpus is
`.dwg`"* is actionable and *"a lot went unread"* is not.

**The dangerous half was the migration, not the feature.** `files.status`
carries a CHECK constraint, so admitting a new value means rebuilding the
table — and `chunks`, `messages` and `entity_mentions` reference `files(id)`
with `ON DELETE CASCADE` while this store runs with `PRAGMA foreign_keys = ON`.
Schema v10 disables foreign keys for the rebuild, copies `id` so every existing
reference stays valid, recreates the indexes, and turns them back on in a
`finally`. It is executed statement by statement rather than through
`executescript`, which implicitly commits and would have dropped the
surrounding transaction — found by *testing* the rebuild rather than reading
it. `tests/unit/test_name_only.py` asserts the chunk, message and FTS counts
are identical either side.

**Two defects the tests caught, both about reading a file we promised not to
read.** `has_changed` pays for a hash when a file was touched recently, and a
`NAME_ONLY` row holds no hash to compare against — so `fresh != None` was true
every time and a 4GB `.mp4` copied in this morning was read end to end on the
next pass, while every count still looked correct. Unreadable candidates are
now settled on mtime and size alone, above that call. The mirror of it: a row
being `NAME_ONLY` must *not* mean settled the way `INDEXED` does, because a
file is name-only for reasons that change — the size ceiling gets raised, an
extractor gets added for its type — and trusting the row would leave it
name-only for ever with nothing saying why.

`INDEX_NAME_ONLY` turns it off, in Settings → Indexing. The objection it
answers is a real one: two million video files on a media drive.

### Added — a repository pane on the Code tab, and a branch is a place the index cannot go

Asked for as *"a git view option by which the view changes to a two pane view
git on the left and the file list on the right"*, with an invitation to propose
something better than a folder tree — which this takes, because a folder tree
duplicates Explorer.

The left pane is repositories, and under each: **Working tree**, **Branches**,
**Recent commits**. Selecting one scopes the list on the right.

**The reason it earns its place is one fact.** The index holds the *working
tree*: a file deleted on `main` but alive on a feature branch has no row in
`files`, and one that only ever existed on a branch never had one. "List the
files as of this branch" is a question only git can answer — `git ls-tree` — so
this is not a filter over names dressed up as a pane. Selecting a commit lists
what it touched, with `M`/`A`/`D` intact, through the reader `/changed` already
uses.

It also makes `/branch`, `/history` and `/commit` **discoverable**. They have
worked since the git backend landed and were reachable only by knowing to type
them; this codebase keeps finding features that were built, shipped and
invisible.

**The switches compose with the scope** — *"dont forget the code switches apply
there too"*. The tree says where to look, the box says what to look for, and
neither overrides the other: selecting `main` and typing `/type cs order` means
"`.cs` files matching order, as of main". A typed `/type` still beats the
configured code types, on both engines, so a branch and the working tree filter
identically. Both end as the same row shape, so the table, the preview and the
row menu never learn which engine answered.

**A guard reshaped this feature, and was right to.** The first version fetched
the branch inside the function that runs on the typing debounce — so every
keystroke shelled out to `git ls-tree`.
`test_nothing_that_runs_on_a_keystroke_imports_this` refused it. The listing
does not change while somebody types, so the pane now reads it **once per
selection**, on a worker, and typing filters it in memory: correct, and far
faster than the version that was rejected.

The summary line names the scope — *"2 files · leasha · branch main · 1
repository, 2 indexed files"* — because a list narrowed to a branch with nothing
saying so is the same failure as an archive skipped in silence: the numbers look
ordinary and mean something else.

### Added — the Code tab lists code, not everything that sits beside it

*"on the code search ... at the moment its bringing files which are not code"*.
`code_files` narrows on `repo_id` and `source_kind` and **nothing else**, so the
tab listed every file *located* in a repository: the PDFs, spreadsheets, images
and logs that happen to live in the folder. `app.cli repos` has warned about
exactly this since repositories were added — *"`scope:code` will match your
whole corpus rather than just code"* — and it finally got reported from the
window.

**A new config under Folders to index**, with a preset and the sixteen ecosystem
groups from `source_types.py` behind it:

- *Source code only* — the language groups, 311 types
- **Source, config and build files** — the default, 382 types; adds `.csproj`,
  `.tf`, `Dockerfile`, `Makefile`
- *Source, config, build and documentation* — adds `.md`, `.csv`, `.json`
- *Everything in the repository* — what it did before, kept
- *Chosen types…* — the groups as checkboxes

**It changes what is listed, never what is indexed**, and the panel says so in
as many words. That distinction is the whole design: there are now two
file-type editors on one Settings page, and the other one *does* decide what is
read. Somebody who confuses them either loses search coverage or wonders why
unticking changed nothing. A test asserts the wording, and another asserts the
module calls no configuration writer at all.

Two details worth keeping: an explicit `/type cs` still beats the setting,
because naming a type is an instruction and the setting is only what to do when
nobody has; and "everything" resolves to *no filter* rather than to every known
extension, so a file type the catalogue has never heard of is still listed.

### Fixed — the suite goes green, and two of the twelve were real bugs

Twelve tests had been failing long enough to be treated as the baseline, which
is how a real failure hides: nobody reads a list they have learned to expect.
Worked through one at a time.

**"Use Ollama" appeared to do nothing** — reported from the window, with a log
full of *"Interpreting is switched off. Turn it on in Settings to have a
sentence rewritten as a query"*, said to somebody standing in Settings with it
switched on, pressing Test. `QueryTranslator` defaults to `enabled=False`,
because search must not reach the network when the feature is off, and the
Test probe built one with the default — so **the button could never once have
worked**. Each press built a fresh translator, so the "logged once per
translator" guard did not dedupe them either: every press produced another
identical line advising the thing already done.

**The rebuild-vectors dialog could re-embed against the wrong model.** Its model
box is editable so any model can be named, but typing does not move
`currentIndex`, and `chosen_model()` read `currentData()` — so after picking a
preset and then typing something else it still returned *the preset*. This is
the dialog that invalidates every vector and re-embeds the corpus: it would have
spent those hours on a model nobody chose, then written it to `.env`. The text
is the authority now, and the item data is consulted only when the text is still
exactly an item's display.

The rest were the tests being stale rather than the code:

* three RTF failures were `striprtf` missing from this sandbox — it is pinned in
  `requirements.txt` and the tests pass with it installed;
* `test_file_menu` passed `build_menu(QWidget(), …)`, a temporary with no Python
  reference, so Qt deleted the menu with its parent before the assertions ran;
* `test_launcher` demanded the installer write `FTS_DB` and `VECTOR_PATH`, which
  it deliberately stopped doing — pinning them made `DATA_PATH` meaningless,
  because moving the index then moved nothing. The test now asserts the
  opposite, with a note not to put them back;
* `test_scaffold` blamed the generator for an unsorted import list, and the file
  really was unsorted: `plaintext` sat below the alphabetical block.

### Fixed — `/type` after 34 file types became 405

Two findings from `WORKORDER-inbound-ui-fixes.md`, both created by fixing
something else, which is the usual way.

**The configured tail now needs a prefix before it appears** (§3). `/type` gained
a third source — every format currently switched on, so a type enabled in the
file-types editor is offered before anything of that type has been indexed. That
was sized against 34 text extensions. It is 405 now, and one ceiling for both
halves inverts the ordering the menu was built around: a corpus holding perhaps
forty types would carry a tail of three hundred and sixty-five behind it.

Raising the number does not fix it, and neither does lowering it — **the tail has
no frequency to sort by.** Nothing has been indexed, so it comes out
alphabetically, and the first twelve of 405 are `abap, ada, adb, ads…`: not a
shortlist, just the front of an alphabet, sitting above `pdf` in the one menu
that exists to answer "what can I filter by". So the tail is offered only once
two characters are typed — which is exactly the gesture somebody makes to check
that a format they just switched on is really there, and that check is the only
reason the tail exists.

**`Dockerfile` and `Makefile` can be filtered for, not just found** (§4).
`NAMED_FILES` made them indexable — a repository indexed without them is missing
the file that says how it is built — and in doing so made them unfilterable:
`files.ext` was `''` for every one, `distinct_values` skips those rows, and
`type:` matches on that column. They were in the index, searchable by content,
and could not be narrowed to, offered in the menu, or named in a query at all.
`files.ext` now holds the *name* for a named file, so `makefile` and
`dockerfile` arrive through the ordinary frequency-sorted path rather than
through a hand-maintained group that would drift within a month.

`ext` is derived and an unchanged file is not rewritten, so a corpus indexed
before this keeps its empty values until `app.cli index --force`. Said out loud
because "the setting did not work" is what it otherwise looks like.

### Changed — the view-line cap, and the rule about `git add -A`

`mail_view.py` had **one line** of headroom under the 250-line guard, and a rule
with one line of headroom is a rule about to be broken by the next feature, at
the moment when the pressure to raise the number is highest and the reasoning
worst. Its summary line moved to `presenter.mail_summary` (249 → 242).
`search_view.py` stays at 247: what is left there is wiring, not formatting, and
forcing an extraction to satisfy a metric is what the rule exists to prevent.
The next lever is splitting `presenter.py`, which is approaching 2,400 lines and
becoming the place everything goes to avoid a cap elsewhere.

`WORKORDER-CONVENTIONS.md` §0 named `test_the_presenter_still_imports_no_qt`,
which is not a test. Three more rows named a module rather than the test inside
it. All eight rows now name the test and the file it lives in, and
`test_the_load_bearing_tests_all_exist` parses the table and checks them — a
table of load-bearing tests that names them approximately cannot notice the day
one is deleted, which is the only day it matters.

§5 said *never* `git add -A`. That was written for two threads, where it swept
up the other's half-finished state. With one thread there is nothing to sweep
up, and on 2026-08-25 a `git reset` discarded a day's work across eleven files —
three finished, tested UI changes among them — which survived only because a
working copy happened to exist outside the repository. Checkpointing with `-A`
before anything that touches the whole tree is now the rule; named-file commits
remain the rule for delivering work. A rule quietly broken every day is worse
than one that was changed on purpose.

### Added — the terabyte work order: measure it, then do less of it

`docs/WORKORDER-terabyte-scale.md`, in full. The corpus this was designed for
was 100GB; it is 600GB today and 1.5TB expected. **The design scales and the
schedule does not** — IVF_PQ, compaction, the file indexes and resumability are
all present and correct, and 600GB is still roughly five days.

So none of this makes indexing faster. All of it either does less work, or says
honestly how much work is left.

**`app.cli scan ROOT...` — count the corpus before indexing it.** Every estimate
about a long run comes from the mix of file types, and nobody had counted the
mix; the five-day figure came from one 97-second sample. It reports total files
and bytes, a breakdown by type biggest-first, which of the four readers would
take each one, how much is inside `.git`, and how much is scanned images. It
only `stat`s, so it finishes in minutes on 600GB — the one exception is a random
sample of PDFs, opened to read the text layer of their first two pages, because
"how much of this is photographs?" cannot be answered any other way and it is
the most expensive fact about the corpus. `--mb-per-minute` turns the byte count
into hours; **without it no time is estimated at all**, because a guessed rate is
how a five-day estimate becomes fifteen. The result is saved, so the next index
run has a real percentage from its first tick instead of a bar that spins for a
week.

**Archival roots — the highest-value item in the order.** The corpus is
historic, so indexing it is paid once and *re-walking* it is paid for ever:
millions of `stat()` calls that discover, again, that a fifteen-year archive is
still fifteen years old, plus a prune pass that stats every row in the database
to confirm no file has been deleted. Each folder in Settings is now **Live** or
**Archive** — per folder, never globally, because a corpus is nearly always both.
An archive is walked once and then checked with **one `stat` on the folder
itself**: creating, renaming or deleting anything at the top level moves that
directory's mtime, which catches the ordinary case for a single syscall.
`ARCHIVE_RECHECK_DAYS` (30) is the backstop for a change no cheap check can see,
and "Rescan archived folders now" forces a full walk. Rows under a skipped
archive are not pruned *and not stat'd*, which is most of the saving.

**Skip cheaply, never silently.** A skipped root reports itself on the Indexing
page and on the command line with its file count and the date of its last full
pass, because a folder deliberately left alone is otherwise indistinguishable
from one that was never indexed — and somebody who reaches that conclusion
deletes their index and starts a fortnight over.

**OCR is now a separate pass**, because at 3.6 seconds a page it is not a
feature of an index run, it is the schedule: 100,000 scanned pages is 100 hours
on its own. `index --skip-ocr` indexes everything readable without OCR and
*queues* the images; `--only-ocr` picks up exactly that queue, walking only the
image types rather than the whole corpus again. Settings has the same as a
three-way choice. The held files carry `ERR_OCR_HELD` — "held for the images
pass", a `SKIPPED` row rather than a `FAILED` one — so 40,000 of them read as a
queue rather than as 40,000 broken files. The point is that **search becomes
useful after the first pass**, in a day or two rather than a fortnight.

**Reporting built for days rather than minutes.** Throughput is now reported
over the **last fifteen minutes** as well as since the start: a lifetime average
barely moves after seventy hours, so a run that has slowed to a crawl still
reports the number it managed on the first morning. The ETA is allowed to say
*"time remaining unknown — run `app.cli scan` for a real estimate"*. A long run
writes one summary line a day to the run log — files, bytes, skips by cause,
hours, windowed rate — so a week-long run can be reviewed in under a minute
rather than by reading a million progress lines. Resume-after-kill was verified
by actually killing one, not by reading the code.

### Fixed — four settings that were adjustable, saved, and inert

Each of these had a control, a default, a tooltip and no effect. All four were
found by working through §4 of the order, which asks for the numbers to be
*verified rather than assumed*.

* **The indexing ceilings never reached the indexer.** Memory, workers, CPU and
  the free-space floor were written to `index_state` under `ui:index_memory_mb`
  and friends — keys nothing anywhere read. `limits_from_settings` reads
  `Settings`, which is built from `.env`. Six controls with real consequences,
  none of which had any: somebody who raised the memory ceiling and saw no
  change would reasonably conclude the governor was broken. They now go through
  `.env` like every other setting, and the in-memory `Settings` is updated too,
  so the next run in the same session uses them.
* **`EMBED_BATCH` was 256 in the pipeline and 64 in the embedder**, and
  `embed_all` re-split every gathered batch down to its own number — so the
  constant documented as "the single biggest throughput lever in the whole
  pipeline" reached the model as a quarter of itself, and raising it did
  nothing. One definition now, at 256, and the pipeline aligns the embedder it
  is given.
* **`REQUIRED_FREE_GB` was read by no code at all**, while its help text said it
  was "checked before a run starts". It now produces a notice at minute one of a
  run rather than a surprise at hour sixty — advisory, not a refusal, because
  nobody knows what an index for a given corpus costs until it is built and a
  refusal would be enforcing a guess. `MIN_FREE_GB` remains the floor that
  actually stops a run.
* **FTS5's own index has never been merged.** `PRAGMA optimize` is called on
  close and is the query planner's statistics — a different thing. FTS5 keeps a
  segment per batch of inserts and every query touches all of them, so keyword
  search over a week-long run gets slower in proportion, permanently, with
  nothing to explain it. Merged now after any run that added 10,000 chunks.

Defaults changed with them: `INDEX_MEMORY_MB` 1500 → **4000** (the models are
~1GB resident before any work, so the old ceiling was nearly all baseline and
the governor oscillated), `REQUIRED_FREE_GB` 150 → **300**.

### Fixed — `log_dir_for` created `D:\SearchProject\logs` as a folder name

The fourth time this family of mistake has been found here, and caught by
`test_no_stray_paths` — the outcome tripwire written after the third, precisely
because guarding one door is never enough. `load_settings` refuses a Windows
`LOG_PATH` off Windows, but the run log is opened *before* it, by design, so
that a configuration failure still gets logged. That path had no guard, so every
single command created a directory whose *name* was a drive letter and
backslashes.

### Changed — the IVF cap and the retrain rule now say what they are doing

`num_partitions` is `sqrt(rows)` capped at 4096, so the cap starts to bind at
about **16.8 million vectors** — which 1.5TB reaches. Past that, partitions grow
instead of multiplying and search slows, or loses recall, in proportion. The cap
is **not moved**: §6 asks for latency and recall to be measured at 5M, 10M and
20M rows first. What changed is that it stops being silent — one line in the log
the first time the heuristic is no longer being followed.

The retrain rule ("when rows double") now slows to **four times** above the cap,
for a reason rather than a preference: below the cap a rebuild changes the
index's shape, above it the partition count is pinned and a rebuild only
reassigns vectors to the same centroids — at the sizes where a rebuild costs the
most and lands mid-run.

### Added — 389 source and code types, from 34, all on by default

Asked for: *"do the research and add all types of code files from microsoft,
oracle etc, and others which are stored and configure them and select by
default"*. The plain-text reader claimed thirty-four extensions — the languages
somebody happened to think of — so a repository of PL/SQL packages, COBOL
copybooks or SSIS packages was largely unindexed with nothing to say so.

**All of these are plain text, which is the whole reason the list can be this
long.** No parser, no dependency: a `.pkb` and a `.csproj` and an `.rpgle` are
read exactly as a `.txt` is, so the cost of one more is a line in a set.

The ones worth naming, because they are the ones a general list misses:

* **Oracle** — `.pks` and `.pkb`. A package's specification and body are
  separate files, so a shop that keeps them that way had *none* of its stored
  procedure code indexed while only `.sql` was read. Plus `.prc`, `.fnc`,
  `.trg`, `.tps`, SQL*Loader `.ctl`, FNDLOAD `.ldt`/`.lct`, and `.ora`.
* **Microsoft** — not just `.cs` and `.vb` but the project system: `.csproj`,
  `.props`, `.targets`, `.sln`, `.resx`, `.xaml`, `.config`. And the BI stack:
  `.dtsx` holds an SSIS package's connection strings and SQL, `.rdl` an SSRS
  report's — "which package writes that table" is otherwise unanswerable
  without opening each one in Visual Studio.
* **IBM** — `.cbl`, `.rpgle`, `.sqlrpgle`, `.jcl`, `.pli`, and `.cpy`, the
  copybooks where the record layouts live.
* **Industrial control** — `.st`, `.scl`, `.awl`, `.l5x`. Included because of
  what this application is for: a plant's logic lives in these, they are plain
  text, and no general-purpose indexer would think to include them.

**Grouped by ecosystem, because a flat set of four hundred is unreviewable** —
nobody can tell from it whether `.pkb` is missing. `app.cli formats --groups`
prints them under sixteen headings, so somebody who knows Oracle can read
twenty lines and correct them.

**Files with no extension are now indexed too.** `Path("Makefile").suffix` is
`""`, and so is `Path(".gitignore").suffix` — Python reads a leading dot as the
start of the stem — so neither could be routed by extension at all, and a rule
listing `".gitignore"` as one would never have matched anything while looking
entirely correct. They are matched on the whole name instead: `Makefile`,
`Dockerfile`, `CMakeLists.txt`, `Jenkinsfile`, `go.mod` and the common dotfiles.

**What is deliberately absent** is documented beside the list: anything usually
binary (the NUL sniff catches a mistake, but a type that is *usually* binary
would make that honest report the common case), generated output (`.map`,
`.lock`, `.sum` — real text, enormous, nothing anybody searches for), and three
entries removed during review that could never have worked or were simply wrong:
`.dacpac.xml` is not a suffix, `.spv` is SPSS *output* and binary, and GX Works
project files are binary rather than text exports.

### Fixed — staging to a real folder no longer deletes what is living in it

Staging to `D:\Leasha` as a production location, and two things in `stage.py`
would have destroyed data there.

**`shutil.rmtree(dest)`.** Staging deleted the whole destination first, on the
sound reasoning that a stale file from an old layout makes a staged tree lie
about what ships. Harmless while the destination was a scratch folder beside the
source. The moment it is a real installation, that folder also holds `.env`, a
`venv\`, the logs — and, if the index was moved there, which is the documented
thing to do, `D:\Leasha\Data`. **Re-staging to fix a typo would have deleted a
hundred gigabytes, silently, with no confirmation.**

Each staging now records what it wrote in `.staged-manifest.json`, and the next
one deletes exactly that. Stale files from an old layout are still removed —
they are named in the previous manifest — and anything the manifest does not
name is somebody's data and is left alone. A destination that was never staged
is treated as foreign: nothing is deleted, colliding names are overwritten, and
it says so. A corrupt manifest fails towards deleting nothing.

**`Leasha.cmd` and `leasha.cmd` are the same file on Windows.** Staging copied
the shipped `leasha.cmd` — which checks for a venv, prints "Leasha is not
installed yet", and passes arguments through to the CLI — and then wrote a
generated `Leasha.cmd` over the top of it, because Windows filenames are
case-insensitive. What survived was a three-line stub hardcoding whatever
interpreter ran the staging script. On a production tree with its own venv,
`leasha.cmd` ran the **development** interpreter from `D:\SearchProject\venv`,
reading the development tree's packages while presenting as a clean install.

The generated launchers are now `Leasha-staged.cmd` and `leasha-cli.cmd`, and
both prefer the tree's own `venv\` when one exists, falling back to the staging
interpreter. So one file works before and after `run-install.cmd`. A test
asserts no generated name collides case-insensitively with a shipped one.

### Added — `stage.py --with-tests`

Stages `tests\` and `pyproject.toml` alongside the application, so the suite can
run against the production layout rather than only against the development
checkout. `pyproject.toml` is not optional: it carries the pytest configuration,
and without it the staged run uses different settings, which makes any
difference in the results meaningless. The README in the staged tree says when
it was built this way, because at that point it is not what ships.

### Fixed — moving the index now actually moves it

Asked to move the index to `D:\Leasha\Data`, and found the feature wired up to
nothing. **Two separate defects, either of which loses an index.**

**The move that never happened.** Settings offered "Move or change index
location…", the dialog wrote the new `DATA_PATH` into `.env`, recorded
`index:pending_move`, and the status bar said *"the app will move the index the
next time it starts"*. Nothing anywhere read that key — one occurrence in the
tree, the write itself. So the files stayed put while the configuration was
repointed at an empty folder, and an intact index became unreferenced. Same
category the August review named: documented, believed, never wired up.

**The five keys that outrank `DATA_PATH`.** `install.ps1` pinned every
subdirectory absolutely — `VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE`,
`STATE_PATH`. `.env` always beats a default, so **changing `DATA_PATH` alone
changed nothing at all**: vectors, database, cache and models kept resolving to
the old drive, and the only visible effect was Settings displaying a path the
application was not using.

New `app/core/index_move.py` does the whole thing as one operation:

- **Files first, `.env` last.** A failed copy leaves the configuration pointing
  at the old location, which is intact, so the application still starts. Writing
  `.env` first and then failing is precisely what orphans an index. A test pins
  the ordering by making `shutil.move` throw.
- **The derived keys are removed, not rewritten** — `env_writer` already treated
  `None` as "delete this line", and this is exactly the case it was written for.
  They derive from `DATA_PATH` afterwards, so this is the last time anyone has
  to think about them.
- **Refusals happen before anything is touched**: moving onto an existing index,
  into a subfolder of itself, or adopting a folder that holds no index. None of
  those is something to discover forty gigabytes in.
- **The pending decision is a file beside `.env`, not a row in the index.** The
  original recorded it inside the very database about to be moved.

The UI no longer writes `.env` when you choose a location — it validates the
plan, records it, and says *"nothing has moved yet, and this index keeps working
until then"*, which is now true. `app/main.py` performs it at startup after
logging is up and before any store opens: the one moment nothing holds the
files. Copying SQLite from under a live connection yields a database that opens,
reports no error, and is missing whatever was in the write-ahead log.

Also available headless, which is how a hundred-gigabyte move should be run:

```
venv\Scripts\python.exe -m app.cli move-index D:\Leasha\Data --dry-run
venv\Scripts\python.exe -m app.cli move-index D:\Leasha\Data
```

`install.ps1` no longer writes the five subpath keys, which is the root cause.
27 new tests.

### Fixed — six things in the window, four reported and two found on the way

The Qt suite has never run in this project's headless environment — `libEGL` is
absent, so every test that touches a widget was skipped. Installing it changed
the character of this work completely: each of these was **reproduced before it
was fixed**, and two of them were not on the list.

**Sorting a Mail column.** `ResultTable.ROLE_ROW` was `UserRole + 1`, and so is
`sortable_item.SORT_ROLE` — two role numbers that must differ, chosen in two
files with nothing connecting them. Mail's "From" column was sorting by
comparing whole `MailRow` objects. `ROLE_ROW` moved, a test asserts they differ,
and `SortableItem` now ignores a sort value it cannot order, so the next
collision degrades to sorting by text instead of misbehaving.

**The `/` menu after its first use.** `set_values` lowers the popup to fit a
value list; nothing put it back. After using `/type` once, the *command* menu
returned one row tall for the rest of the session — which from the outside is
exactly what a dropdown that has stopped working looks like.

**What each switch expects.** The rows read `/type <type>`, which says a value
goes there and nothing about which. They now read
`/type <pdf, docx, xlsx, …>  Only this kind of file`, and a switch that takes no
value says `(no value)` rather than showing a blank.

**Column widths.** Every table fits its columns to their contents on the first
fill, any column can be dragged, a dragged width is remembered per table, and
the View menu has "Fit columns to contents" to undo one pulled too narrow.

Two found while fixing those:

**`/repo` was being lost on a git route.** Git's catalogue had no `/repo`,
because git is *run inside* a checkout — so `/repo leasha CustomerId /history`
either could not find its repository or searched for the literal string
"/repo leasha CustomerId". It is parsed and consumed now, and never reaches the
command.

**A crash in the width code, ninety seconds after writing it.**
`resizeColumnsToContents` emits `sectionResized`; the handler that records a
dragged width saved the preference; saving redrew the table; the redraw resized
the columns. Unbounded recursion into C++, which does not raise — it exhausts
the stack and the process dies. The first guard was a mouse-button check, and
that is not a guard: a header click holds the button down, which is exactly when
a sort resizes columns. Fitting now happens once per table, and a flag tells our
own resizes from a person's.

### Added — every column a code row can fill

Asked for: *"on code list all all columns which can be viewed for code"*. Name,
Repository, Kind, Size, When, Status and both paths — the shortened one for the
column and the full one for copying. Which are *offered* still depends on the
rows on screen: a column no row can fill takes width from the ones that matter
and reads as a broken index.

### Fixed — the Qt suite could abort on any run, depending on collection order

Several test modules wrote `QApplication.instance() or QApplication([])` and
discarded the result. Where that line was the one creating the application,
nothing held a reference, Python collected it, and the next widget built
anywhere in the process aborted inside Qt. Which module hit it depended on
collection order, so the same suite passed and crashed on alternate runs and the
failure never pointed at the line responsible. One session-scoped fixture in
`tests/conftest.py` now owns it.

### Added — a Visual Studio solution, and both editors now say Leasha

`Leasha.sln` and `Leasha.pyproj` open the project in full Visual Studio, with
the venv wired up relatively (`MSBuild|venv|$(MSBuildProjectFullPath)`), F5 on
`app\main.py`, and pytest registered so Test Explorer finds the suite.

`SearchProject.code-workspace` is now `Leasha.code-workspace`. Its display name
read **"Local Knowledge Graph"** — the name the project had before the graph was
removed and the scope narrowed to search. Two stale names in one line is how a
project ends up called something nobody recognises.

**The folder on disk stays `D:\SearchProject`.** Renaming it would mean
rebuilding the venv — `pip.exe`, `activate.bat` and every console script have
the absolute path compiled into them — for no functional gain. The index at
`D:\KnowledgeGraphData` was never affected either way.

**`Leasha.pyproj` is generated, not hand-maintained.** Visual Studio shows only
the files its project lists, unlike VS Code which shows the folder. With 228
Python files that manifest goes stale the first time somebody adds a module —
and a stale one is worse than none, because the new file is missing from
Solution Explorer while every test that imports it passes, so it looks as though
it does not exist. `scripts/regen_vs_project.py` builds it from `git ls-files`,
and a test asserts the committed file matches what the script produces, so drift
fails the suite rather than surprising whoever next opens the solution.

`.vs/`, `*.suo` and `*.user` are gitignored — per-user state, and `.vs/` alone
reaches hundreds of megabytes here. New: `docs/VISUALSTUDIO.md`.

### Changed — libraries before converters, and LibreOffice for far less

New standing rule from the owner: *"use libraries where you can and only
libreoffice where it cant"*, now non-negotiable 12.

**`.xls`, `.rtf`, `.epub` and `.fb2` are read in-process.** They went through
`soffice` and `pandoc` until now, which meant a machine without LibreOffice read
none of them — and `.xls` and `.rtf` are not exotic. A long archive is full of
both, and `.rtf` turns up wherever anything was ever pasted between two
applications that disagreed about formatting.

- `.xls` → `xlrd==2.0.2`. **It reads `.xls` only** — it dropped `.xlsx`
  deliberately in 2.0, which is what makes the split with `openpyxl` clean
  rather than overlapping. Dates are converted (Excel stores 2019-04-01 as the
  float `43556.0`, and indexed raw no date search ever matches) and whole
  numbers lose their `.0`, so a search for `12400` finds the invoice.
- `.rtf` → `striprtf==0.0.33`, with a `{\rtf` magic-number check first: without
  it a mislabelled binary's stray ASCII gets indexed as though it were the
  document.
- `.epub` and `.fb2` → the standard library, no dependency. **That removed
  pandoc from the project**, and it is off the converter allow-list.

Two things in the e-book reader are worth knowing. EPUB chapters are read in
**spine order, not manifest order** — manifest order is arbitrary, and getting
it wrong produces snippets that read like two sentences from different chapters
glued together, a bug that survives a long time because all the text is present.
And FictionBook's `<binary>` elements are skipped, which is not an optimisation:
a book stores its cover as base64 *inside the document*, so indexing it would
make every chunk random letters and the embedding meaningless.

**LibreOffice is now needed only for `.doc`, `.ppt`, `.pub`, `.wpd` and Apple
iWork** — seven formats rather than nine, and none of them common. `doctor.py`
says which formats actually depend on it instead of the old blanket ".doc,
.xls, .ppt and friends", because an over-broad warning is one people silence by
installing software they do not need.

**The rule is enforced, not remembered.** `CONVERTER_JUSTIFIED` in
`app/extract/converter.py` maps each remaining converter to why no library will
do, mirroring the `NOT_SETTINGS` pattern from the settings registry. Tests
assert that every shipped converter has an entry, that every entry says
something, that no format has both a converter and an extractor — two routes to
one format is how a file reads differently on two machines — and that the map
holds no stale entries for formats that have since moved to a library.

Office automation through `pywin32` was considered and rejected: free in
dependency terms, but it swaps LibreOffice for Microsoft Office, Microsoft does
not support it unattended (KB257757 — Office prompts with a dialog on error,
which on an invisible instance is a hang), and its advantage is formatting
fidelity that a plain-text index discards anyway.

### Changed — the Code page is one box and one list

Corrected by the owner: *"the code search page is all wrong it should be a
combined one search box with the git code files in the list"*. It was a tree of
repositories with a separate git search box underneath, and that made somebody
choose an engine before they had a question. The question is nearly always
*"where is that file"* — and that is answered from the index in milliseconds, so
it has to be what typing does.

**The grammar picks the engine now, not the person.** A line with no git switch
searches the indexed files of every repository at once, as you type, over a new
`code_files` query that joins `files.repo_id` to `repos` — every clause
index-backed, the limit in SQL. A line carrying `/history`, `/branch`,
`/introduced` and the rest runs git, on Enter. Both fill the same table, and the
line above it says which engine answered and what it looked at.

**Routing is not symmetric, and that shaped it.** Sending a git query to the
index costs an empty list. Sending an index query to git costs a subprocess that
diffs every commit — two seconds and a spinning window — for a question that had
a 3ms answer. So it only reaches for git on a switch *only git has*, and
`/repo`, `/type`, `/path` and `/file`, which both catalogues share and which are
the commonest things typed here, stay on the fast path. A test asserts that
list, because nothing else in the suite would notice it changing.

The two catalogues merge into one menu with the index winning every shared
spelling — same reason. The repository is a column rather than a tree, `/repo`
narrows it, and `/branch`, `/tag` and `/author` still offer what the selected
repository actually has.

`app/ui/widgets/repo_tree.py` and `app/ui/widgets/git_search.py` are **deleted**
rather than left unreferenced: code that still imports is code somebody
maintains for nothing. The decisions moved to `presenter.code_route` and its
neighbours, which import no Qt and are covered by 38 tests that run without a
display — this project has shipped UI logic verified by reading that crashed on
the first run, so what stays in the view is wiring.

### Fixed — `doctor` created a folder literally named `D:\KnowledgeGraphData`

Reported: *"i just noticed a folder DKnowledgeGraphData in our search project
folder it was like the bug before"*. It was — a directory whose **name** is a
drive letter, a colon and a path, with `cache`, `fts`, `models`, `state` and
`vectors` inside it, sitting in the checkout.

A backslash and a colon are ordinary filename characters on Linux and macOS, so
`Path("D:\\Data\\vectors").mkdir(parents=True)` there does not fail and does not
warn. `app/core/config.py` has refused foreign paths for a while;
**`doctor.py` never went through it**, because it is deliberately
dependency-free so it can run on a half-built venv. Dependency-free is right;
skipping the rule was not. It now refuses and says why, in the same words.

Worth naming the shape: this is the fourth time in this project that a Windows
path treated as a POSIX one has caused a silent wrong answer.

`test_no_stray_paths.py` guards the **outcome** rather than any one code path —
it does not care what created the folder, only that nothing in the project is
named like a path. That is the right shape for a failure whose only reliable
symptom is the folder itself, and it caught this one within a minute of being
written.

### Fixed — the Ollama switch could not be found

Reported: *"i dont seem to find the button to turn olama on and off"* — and it
was on screen, in a group headed "AI query interpretation" with a tick reading
"Let a local model turn sentences into queries". Every word accurate; none of
them the word somebody scanning the page has in mind. The group is now
"Ollama — AI query interpretation (optional)" and the tick "Use Ollama to turn
sentences into search queries". Naming the thing beats describing it.

### Added — restoring a setting's default, which was a one-way door

`.env` beats the default in `config.py`, so the moment a setting is written down
it is pinned for ever and no improved default can reach that machine again. The
worked example is not hypothetical: the installer wrote
`RERANK_MODEL=BAAI/bge-reranker-base` into every install, the code default was
later changed to a model measured **9.2x faster**, and not one machine got it.

The operation has existed since `75c48ae` — `apply_values(path, {"KEY": None})`
removes a key — and nothing called it. Settings now has a **Restore defaults**
button naming how many settings are pinned, and a **right-click on any control**
restores just that one. The per-control menu is attached generically, by the
registry key each control already carries as its object name, so a setting added
next month gets one without anybody remembering.

`None` removes; writing the default *value* back re-pins it. The two calls
differ by one character, look identical on screen, and only one is the fix — so
a test asserts both halves.

### Fixed — `app.cli index` ignored the folders saved in Settings

One setting, two sources of truth: the window saves "Folders to index" under
`ui:roots`, and the command read only its own arguments. So a command-line run —
including the one somebody uses to verify a migration, which is the whole reason
the command exists — indexed whatever folder was typed rather than what the
application is configured to index, and nothing afterwards could tell the two
apart. Verifying the wrong thing and believing it was the right thing is the
expensive kind of wrong.

No folders now falls back to the saved setting **and prints which folders it is
using**; named folders still win, because naming one is an instruction; neither
names the Settings page rather than only the syntax.

### Added — a staged install tree, and a way to run from it

Asked for: *"organize a structure under Leasha which are the master files as if
they are installed in final environment.. i.e. production and run like that so
that can be tested too"*.

`D:\SearchProject` is a development checkout — source, tests, fixtures, work
orders, scratch files and a two-gigabyte venv in one tree — and every command is
run as `venv\Scripts\python.exe -m app.cli`. Nobody had ever run this the way it
will be installed, so the install layout was unverified.

    venv\Scripts\python.exe scripts\stage.py --check

builds `Leasha\` holding only what ships, writes `Leasha.cmd` and
`leasha-cli.cmd`, and then **runs the staged copy** — `--version`, `commands`,
`formats`, `doctor --quick`. Staging a tree nobody starts proves that files were
copied, which was never in doubt.

**Generated, never hand-maintained**, and deleted before each rebuild: a second
copy of the source that quietly goes out of date is worse than none, because it
is trusted. The manifest is an allowlist rather than "everything except", so it
fails towards a tree that is missing something — an import error on first run —
rather than towards shipping the owner's `.env` and 100GB of scratch.

**The venv is not copied.** The launchers name an interpreter and `cd /d "%~dp0"`
first, which is what makes the application resolve *its own* files: staged, it
reads `Leasha\.env`, writes `Leasha\logs\`, and reports its root as `Leasha` —
verified by running it, not by reading it.

`test_staging.py` reads the application's *imports* rather than the manifest, so
it fails on the day a new package ships nowhere rather than the day a user finds
out. Writing it turned up one thing worth knowing: `app/cli.py` imports the test
corpus for `evaluate --builtin`. That is deliberate, guarded, and answers with a
clear error when the tests are not installed — so the check became "never at
module scope", which is the version that would not have forced a working feature
out of the source build.

### Fixed — the first Interpret after a break timed out on a cold model

Ollama drops a model from memory after five minutes of quiet, and reloading it
costs 8.2s here against a translate budget of five seconds. So the first press
after any pause failed with `ERR_OLLAMA_TIMEOUT` while the service was working
perfectly — which reads as a broken model rather than a cold one, and sends the
diagnosis in exactly the wrong direction. The same shape of confusion the
connect/read timeout split fixed earlier.

Two halves. Every request now carries `keep_alive: 30m`, so the model stays put
across a working session — on the request rather than configured once, because a
server restarted underneath us would otherwise silently go back to five minutes.
And the model is **warmed on the transition**: when somebody switches Interpret
on, and at startup only if it was already on.

**Not warmed at boot**, which is where the advice this came from would have put
it. Interpretation is optional, off by default, and most machines have no Ollama
at all — loading a model into VRAM for somebody who never presses the button is
a cost they did not ask for, in an application whose promise is that it does
nothing until asked.

### Added — repository search: `GitSearch.txt`'s switches, in the `/` grammar

The owner attached an 853-line functional specification and asked for *"all the
functionality in this … as switches … the / style we have in the app"*.
Thirty-four switches now cover its search modes, methods, file/branch/commit/
author/date filters and history intelligence:

```
CustomerId /history /extension cs /exclude-path node_modules
/class OrderService /branch develop
ApiKey /history /removed-only
notice_line /introduced
/file-history src/Order.cs
```

Available headless (`app.cli gitsearch --repo … "query"`, per non-negotiable 8),
listed by `app.cli commands --git`, and in the window as a panel under the
repository tree on the Code tab — searching whichever repository is selected.

**Two facts shaped it, and both are on record.**

*It is a second search domain, not a filter on the first.* The `/` menu until
now narrowed the index: rows describing files read once, at index time. History
is not in the index and cannot be — it is thousands of versions of files that no
longer exist, and reaching it means running git. So the grammar is shared and
the engine is not, and a test asserts that nothing running on a keystroke can
reach git.

*It is slow.* `git log -S` over 200 commits of this repository takes 2.26s;
`git grep` over one revision takes 0.33s. That is why the panel searches on
Enter and never as you type, why every history plan carries `--max-count`, and
why `GitPlan.slow` exists for the interface to read.

**What it builds is a command, not a result.** `gitquery` produces the exact
`git` argument list and runs nothing, so all thirty-four switches are checked by
asserting on that command — without git, without a repository, without waiting.
Three faults surfaced that way before anything ran:

* **`/api/orders` parsed as a switch.** `api` is an alias of `endpoint`, so a
  route — the single most likely thing to type on a tab about source code —
  silently became a different search. Switch names now cannot contain a slash.
* **Asking who or when was silently dropped.** `/author` and `/since` are
  properties of a commit and `git grep` has nowhere to put them, so a query
  carrying them planned as a grep answered a wider question with no symptom but
  too many results. They now make it a history search.
* **`--max-count=1 --reverse` returns nothing at all.** Measured, not assumed:
  `git log -Snotice_line --reverse --max-count=1` on this repository prints
  nothing where the same command without the count prints the commit that
  introduced it. `/introduced` keeps one row itself; the obvious spelling would
  have shipped a switch that silently found nothing.

**Only switches that run are offered.** `--compare`, `--branch-timeline`,
`--references`, `--team`, `--fuzzy` and the security-artifact scanners are real
work rather than oversights, and each is absent with its reason and its cost in
`docs/WORKORDER-git-search-ui.md`. A menu full of commands that quietly fail is
worse than a shorter menu: it is discovered one disappointment at a time.

**Stop abandons the answer; it does not kill git.** Said on the button rather
than implied. Killing it properly needs `Popen` and a process group — recorded
as the next thing worth doing here.

### Added — the `/` menu offers values, not just filter names

The menu answered "which filters exist" and stopped at the colon. The harder
question is the next one — *what do I put here* — and it was left to guesswork.
A guessed value returns nothing, and **a filter that returns nothing is
indistinguishable from a filter that does not work**, so the menu was quietly
teaching people the feature was broken.

Choosing `/type` now keeps the list open and offers the extensions actually in
the index, commonest first; `/from` offers the people who have actually sent
mail; `/repo` the repositories actually found; `/has`, `/after` and `/size` the
values the grammar fixes. Every box that has a `/` menu gets it — search, Files,
Mail and Code — each still restricted to the filters its own tab honours.

**Nothing unbounded behind a keystroke.** `distinct_values` is one query per
kind, every one index-backed (`ext`, `parent_dir`, `sender`, `repos.name` all
have an index), `LIMIT` pushed into SQL rather than applied afterwards, and the
`kind` looked up in a table rather than interpolated. It runs on a worker and is
cached for two minutes, so arrowing through a menu costs one query rather than
one per keystroke. The grammar's own values appear instantly; the index's arrive
when they arrive.

### Added — icons in the `/` menu

Asked for: *"i want the / command to show icons too similar to claude / command"*.
Each filter carries a glyph in `app/search/commands.py` — a character painted
into a pixmap in the palette's own colour, not a shipped image. Nothing to
package, nothing to redraw for dark mode, and a command that gains an icon gains
one character in the catalogue rather than a file.

### Fixed — `install_package` would have raised `NameError`

`presenter.py` imports neither `sys` nor `subprocess` at the top; the function
imported `subprocess` locally and then referenced `sys.executable`. "Never
raises" was written directly above it and was not true. The path is the button
that installs a missing reader — the one nobody runs in a test.

### Added — the preview pane, on every list

Asked for directly: *"preview pane should be in every search type"*. The pane
itself was written weeks ago and worked; it was attached to the search tab and
nowhere else, because `attach_preview` needs a results widget with a `selected`
signal and a `current_row()`, and only `ResultsView` had them. Files and Mail
used a bare `QTableWidget`, which knows about cells and strings and threw the
row object away as soon as the text had been read out of it.

`widgets/result_table.py` is that widget, and it absorbs the table setup the two
views had been copying — which had already drifted, one sorting and one not, for
reasons that were real and written down nowhere. Both reasons now sit next to the
switch that selects between them. Filling stays in the views: what a Mail row
looks like is not something a table should know.

**The row object lives on the item, not in a list beside the table.** Mail sorts
on a header click, and after one, visual row 3 is not `rows[3]`. Qt moves item
data when it sorts; it does not move a list — a positional lookup would preview a
different message from the one highlighted, which nobody reports because they
assume they misclicked.

Two silent failures came out of writing the rule down. Reading a row is now
`preview_loader.load_preview_for`, which imports no Qt and is therefore tested,
where before the pane reached into rows with three `getattr` calls:

* A **Mail** row has no `preview_text`, so every message would have previewed as
  `ERR_FILE_MISSING` against a synthetic path nobody could have opened. The text
  comes from `chunks` instead — the message was extracted at index time and that
  is where it went. A PST is a hundred thousand messages in one file; there is
  nothing on disk that is *this* message.
* A **Code** row's `path` is shortened to fit its column, so every repository
  file would have done the same. "File missing" is plausible enough about a file
  sitting right there that nobody would have questioned it.

### Fixed — the View menu advertised `Ctrl+P` for a pane it could not open

The window binds `Ctrl+P` to "go to Files", the shortcut every editor uses for
"go to file", and a window-level action wins over one on a menu that exists only
while it is open. So the menu named a key that did nothing — worse than naming
none, because somebody presses it, lands on another tab, and concludes the
preview is broken. It is `Ctrl+Shift+P` now, bound in the window, and it toggles
the pane on whichever list is in front. Per list, deliberately: somebody who
wants the pane on Code has said nothing about wanting it on Mail.

### Added — one log file per run

Asked for directly: *"for testing create detailed log files which for every run
you can check on the project folder"*. The value is the round trip. A report of
the form *"this seems stuck"* has found real faults here, but answering it costs
a message asking what the console said, another asking which command was run,
and a third asking what the settings were — by which time the run is over and
the console has scrolled.

**No new logging.** `logs\app\` already held everything at DEBUG. What it did
not hold was a *boundary*: one run's lines sat in the same daily file as the
twenty around it, interleaved with a window session. `logs\runs\` now holds one
file per command and per window session, named `run-YYYYMMDD-HHMMSS-<command>`,
carrying four things the daily log cannot say:

* **The settings in force** — written from the object the run is using, not from
  `.env` and not from the defaults. This is the section that would have caught
  `doctor` reporting its own hardcoded defaults, which cost a week of attributing
  a measurement to the wrong model.
* **The threads still alive at the end**, with the non-daemon ones marked. The
  window closing without the process exiting was diagnosed by reasoning about
  `concurrent.futures`; the footer names them outright.
* **Errors grouped by code.** Four hundred `ERR_CONVERTER_MISSING` and one
  `ERR_DB_LOCKED` reads, flat, as 401 problems. The tally says it is two.
* **Where the time went**, when a command records stages.

Opened *before* the command runs, so a run that dies at configuration — the most
common way this application has failed on the owner's machine — still leaves the
evidence. The path is printed to stderr only when the run failed or logged an
error; a line after every successful `search` is furniture within a day.

Two traps, both found by running it rather than reading it. The sink builds each
line from the record instead of a format string, because a format naming
`{extra[component]}` raises `KeyError` on any line logged before
`logger.configure` has supplied the defaults — and this sink attaches
deliberately early, so that is not a corner case, it is the start of every run.
And `setup_logging` calls `logger.remove()`, which took the run's own sink with
it; without `reattach` the file would have held a header, a footer and none of
the run, which looks exactly like a working feature until you open one.

### Fixed — `extract` was reading the application's own output

`walker.own_paths` exists because *"the indexer was reading its own log file
while writing to it"* — its docstring says so — but it guarded the indexer only.
`app.cli extract` walks folders the same way and had never been given the same
guard, so pointing it at the project folder swept up the SQLite index, the vector
store and the log file. Adding a run log per command made that visible within
minutes: a test that had passed for months started reporting one extra document.

A file **named explicitly** is still extracted wherever it lives. The guard
belongs on the sweep, not on the intent.

### Fixed — `logs\README.txt` described a structure that no longer existed

It was written once, on first run, and never again — so adding a log folder left
a generated document confidently listing the old set. It is now rewritten
whenever it no longer matches `LOG_SUBDIRS`. Nothing in it is hand-edited, and a
stale generated document is worse than none because somebody will act on it.

### Fixed — the `/` menu was hiding a third of itself

`QCompleter` shows seven rows by default and there are eleven commands. The four
below the fold were reachable by scrolling and invisible in every other sense —
on the keystroke whose entire purpose is discovery. Sized to the catalogue now,
so adding a command never quietly hides another one.

### Added — syntax highlighting in the preview, without a library

Code in the preview pane is coloured and set in a monospace font; everything
else is left exactly as it was. `.py`, `.ts`, `.sql`, `.json`, markup, CSS and
the C family are covered — grouped by *grammar*, because `.ts` and `.java`
differ in ways a preview pane cannot see. An extension with no grammar gets no
colouring at all: guessing puts arbitrary words in keyword blue, which reads as
a rendering fault rather than a guess.

**Pygments was the obvious answer and is the wrong one here.** Six hundred
lexers to make strings and comments a different colour in a read-only pane. This
is a table and a dozen regexes, has no install step, and never becomes a reason
the application will not start on a machine with no network. It is not a parser
and does not pretend to be — the failure mode is one word in the wrong colour in
a pane nobody edits in.

**Colours are mixed from the palette, never asserted.** A hard-coded comment
green is unreadable on a dark background, which is the exact failure
`test_forced_theme_stays_readable` exists to catch.

The grammars live in `app/ui/grammars.py`, which imports no Qt, because a regex
that over-matches has to be catchable by a test and a test that needs a display
does not run. Writing them there caught a real bug immediately: painting rule by
rule and letting the last one win **cannot** express what is wanted, because a
keyword inside a string and a `//` inside a string both have to lose to the
string, and under last-wins one of them always won — `url = "http://x"` came out
as a comment. It is one left-to-right scan now, first alternative wins, which is
what a highlighter has to do.

### Added — Office, OpenDocument and drawing files preview as their text

Word, Excel, PowerPoint, OpenDocument and anything else the index can read now
appear in the preview pane instead of falling through to the "no preview" card.
Spreadsheets keep their sheet names and decks their slide labels, because which
sheet a number came from is most of what somebody previewing a spreadsheet wants
to know.

**It reuses the index's own extractor rather than owning a list of extensions.**
Rendering a `.docx` faithfully means a word processor; showing what the *index*
holds means reusing thirty lines of `app/extract`. In a search tool the second
is the more useful thing, because what appears in the pane is exactly what was
searched — "I found it but I cannot see why" is the complaint this answers. The
notice line says so, so nobody concludes their formatting has been lost.

It also means preview coverage cannot drift from index coverage: a file type
added through the file-types UI becomes previewable at the moment it becomes
searchable, with no second list to keep in step.

Types needing a Tier 2 converter are deliberately excluded. Shelling out to
another program is reasonable while indexing a folder overnight; it is not
reasonable because somebody pressed the down arrow.

### Added — the Code tab shows the files, not just the folder

A list of repository names told you a repository existed and nothing about what
was in it, so the obvious next click — open it and look — had no answer. Each
repository now expands into the files indexed under it, in the same five
columns: a repository's `Files` column holds a count, a file's holds a size, and
column three is a kind either way. Two tables stacked would have needed two
headers to say the same thing.

**Children load when a row is opened and not before.** Counting files is cheap —
`repos_list` does it in the same query — but *listing* them is not, and most
repositories in the list are not the one being looked for. Building 48,000 items
for every repository on the chance one gets expanded is the difference between a
tab that opens instantly and one that appears to hang. Past 500 files the tree
stops and says so, pointing at the search box, which is one tab away, takes
`repo:` and was built for the question "which file says this". A tree is for
looking at what is there.

The files come from `files.repo_id` where the store offers an accessor for it,
and from a path-prefix walk where it does not — correct either way, only the
speed differs. **The dedicated accessor is the one thing outstanding for the
backend thread**: `repo_files(repo_id, limit)`, one query on an indexed column.

Enter and double-click now do the obvious thing for the row you are on: search
inside a repository, open a file. `Ctrl`-free, one key, because "the obvious next
thing" differs by level rather than by modifier.

### Changed — the `/` menu offers what each tab can actually honour

**The generic search is the union; the focused tabs are subsets of it.** Search
is where you type before you know which tab you want, so it offers every command
there is. A focused tab now offers only what it can honour: Files takes `type:`
and `name:`, Mail takes precisely the columns the `messages` table has, Code
takes `repo:`, `type:` and `name:`.

Both halves of that were wrong before. Files and Mail offered the whole
catalogue, so `/from` in the Files tab inserted a filter the tab then dropped —
an offer nothing kept, discovered one disappointment at a time. Code offered
nothing at all, because a list of repositories could not answer most of them;
now that it lists files, it can, and it does.

`test_command_subsets.py` asserts the relationship rather than the lists: every
offered command must reach the field its tab's query function consumes, and a
command added to the catalogue and to no tab fails the build. The rule that
makes this checkable is that the subsets live in `presenter.py`, which imports
no Qt — the union rule is about the grammar, not about widgets.

An operator a tab cannot answer is still **named** in the summary rather than
silently dropped, because filtering to nothing without explanation reads as the
tab being broken.

### Added — the Code tab, which is a browser and not a second search

`WORKORDER-git-search-ui.md`. The request was a git search tab; what the audit
found was that **source code was already searchable** — twenty extensions since
Layer 2, with snippets and reranking like everything else. The one thing the
application could not do was say *which repository a result came from*, or let
you ask for one.

So the tab is a repository browser that hands its selection to the search box
you already have. It lists what you have and how much of it is indexed — a
question about the machine, asked before you know what you are looking for — and
it cannot find code by what the code says, because that is the search tab's job.

**Nothing new was invented for it.** The alternative proposal ran to fifty-six
command-line switches; this adds one filter, `repo:`, to the single grammar in
`app/search/commands.py`, so the `/` dropdown, the search box, the Files tab and
the Mail tab all gained it at once. A tab whose filters work nowhere else is how
the fifty-six-switch design arrives through the side door.

Double-click, Enter or the right-click menu puts `repo:"<name>" ` **in the search
box, visible and editable** — not applied as hidden state, for the same reason an
interpreted query is shown. The name is quoted because repository folders contain
spaces, and an unquoted value ends at the first one, matching a different
repository with nothing on screen to explain why. There is a test for exactly
that, and another asserting what happens without the quotes.

Three empty states, because they are three different questions. The one that
matters: an indexed corpus with no repositories says what a repository is here —
a folder containing `.git` — and that adding its parent as an indexed root is
what makes it appear. A generic "no results" would waste it.

Deliberately not built, and recorded: a search box on this tab, branch pickers,
commit ranges, add/remove/configure a repository, and anything needing git
history. The last is minutes of work on a large repository against a contract of
300ms warm, and it never goes behind the Enter key.

### Changed — "Code only" in the scope chips, and Everything means everything

The fourth scope. **"Code only" means "in a repository", not "looks like
code"** — a README inside a repository counts and a `.py` file in Downloads does
not — which is a surprising enough definition that the tooltip says it in words
and points at `type:code` for the other question. Both stay available.

The chip's tooltip now states the principle the whole search box rests on:
**it narrows, it never adds.** Everything is the union of what every focused tab
can find, so a filter learned anywhere works there, and there is never a reason
to visit another tab to ask something the search box cannot.

### Added — the preview pane, and no Chromium anywhere near it

`WORKORDER-ui-shell-and-results.md` §4, the last outstanding UI item. Read a
result beside the list without opening the application that owns it. Off by
default, `Ctrl+P` or the View menu, persisted like every other view preference.

**No `QWebEngineView`, and performance is not the reason.** It is a full
Chromium — multi-process, a GPU process, hundreds of megabytes — which would
reverse the founding decision of V2: one process, no services. Worse, it was
proposed for *HTML email*, which means remote image loading, which means
fifteen-year-old tracking pixels phoning home the moment somebody arrows past a
result. "Nothing leaves this machine" would quietly stop being true, and the
person doing it would be searching their own archive.

So email HTML is sanitised **before it reaches the widget** — not disabled, not
proxied, removed, because a widget that never sees a URL cannot fetch one, and
that is a property a test can assert. `app/ui/sanitise.py` rebuilds the document
from an allow-list rather than stripping a list of dangerous tags: a block-list
has to keep up with a specification that keeps growing, and anything it has not
heard of is permitted by default, which is the wrong default for markup this old
and this untrusted. Every `on*` handler disappears without needing to be named.
The reader is told what was removed, because a message that silently renders
differently from how it was sent is confusing.

Everything is read on a worker — a debounce so holding the down arrow queues one
render rather than fifty, a generation number so a render that lands after the
selection moved on is dropped rather than painted beside the wrong row, and a
size ceiling per kind because a preview of a 400MB log is the first page of it.
PDFs open at the matching page: a 400-page report opened at page one, when the
hit is on page 312, is a preview of the wrong thing. Failure is an `AppError`
rendered as a sentence in the pane — never a dialog, never a traceback for a
file somebody merely scrolled past.

58 tests, all of which run headless, because the sanitiser is the security
boundary and it must be checkable on any machine in milliseconds.

### Fixed — five new controls that saved nothing, and the test that missed them

Introduced in the same commit that fixed U6, which was about controls wired to
nothing. `RERANK_TOP_N`, `RERANK_WINDOW_CHARS`, `RERANK_MODEL`, `MIN_FREE_GB`
and `REQUIRED_FREE_GB` each had an object name, each passed
`test_every_plain_setting_has_a_control`, and not one persisted anything.
`SearchBox` even had a `values()` method that nothing called.

**The test was right and incomplete, and said so.** Its own docstring recorded
that it proves a control is *built*, not that it is wired to the writer — and
that gap was then filled with five controls. A rule enforced at one end only
gets broken at the other.

`test_every_control_writes_its_setting_somewhere` closes it, reading `app/ui`
with `ast` so that `{"RERANK_TOP_N": value}` counts as a write while
`getattr(settings, "rerank_top_n", 30)` does not — a check that cannot tell a
save from a load passes for a control that only ever loads its own default.

It immediately caught a sixth: **`OLLAMA_URL` was a read-only box**, the same
untunable failure `DATA_PATH` had, and named in the work order as "the easy
case". It is now an editable field in the Ollama panel, beside the Refresh and
Test buttons that are the only way to know whether an address is right.

Settings that reach `.env` through a flow rather than a panel are listed with
which flow writes them, so an exemption has to be a decision rather than an
oversight.

### Changed — the model settings are lists with their cost on them, not text boxes

A free-text field for a model name asks somebody to know an exact HuggingFace
identifier, and says nothing when they get it wrong: the next start fails to
download it, by which time they have forgotten what they typed.

**The concrete case was expensive.** `config.py` records a `rerank-bench` run on
this machine — `ms-marco-MiniLM-L-6-v2` at 22ms per result against
`bge-reranker-base` at 203ms, nine times slower for an ordering measured as
equivalent at rank 1 — and the default was changed on those numbers. But the
`.env` written by an earlier install still pinned the slow one, so the
measurement never reached the machine it was taken on. A search spending 10.7 of
its 11.8 seconds in rerank looked normal.

Both model settings are editable combo boxes now, each entry carrying what it
costs: milliseconds per result for the rerankers, dimensions and size for the
embedding models — because `EMBED_DIM` has to match, and a mismatch is refused
by the vector store with an error naming a setting nobody typed. Editable, so an
identifier nobody listed still works.

`test_accessible_names.py` gained the rule: a setting whose value is one of a
known set may not be a plain text box.

### Fixed — switching tabs left the cursor nowhere

Files focused its filter on arrival; Search and Mail did not — so switching to
the tab whose entire purpose is a text box left somebody reaching for the mouse
to click into it.

Now every tab with somewhere to type focuses it, asked for by capability rather
than by name, so a tab added later gets it without anybody remembering. Settings
deliberately does not, having nothing to type into first.

### Fixed — a click on a result was never recorded, for the life of the feature

Not in the review; found by reading beside it. `record_open(engine, search_id,
chunk_id)` takes three arguments and was being handed four, with
`search_options` in front of them. Every click raised `TypeError` inside the
worker.

**Two correct safety nets in a row turned a wrong call into silence.** The
worker catches everything, as it must, or a failed background task takes the
window with it; and `record_open` swallows failures too, because a click is
never worth blocking on. So nothing surfaced anywhere, and the table everything
in Layer 10 is meant to learn from was empty by construction.

Neither net should be removed. What was missing was any check that the call was
ever plausible, which the interpreter cannot do for a callable passed by
reference. `test_worker_calls.py` now matches every `CallableWorker(fn, *args)`
against the signature of `fn`; it fails on the original bug and passes on the
fix.

### Fixed — shutdown stopped no timers at all (U1)

`search_view.shutdown()` asked `stop_timers` for `_typing_timer`, `_idle_timer`
and `_timer`. Its timers are called `_interim_timer` and `_full_timer`. Every
name missed, `getattr(view, name, None)` returned `None` three times, and the
call did nothing whatsoever — while looking correct at both ends.

That is the shutdown race the function exists to prevent: debounce timers
running on into teardown, starting a search against a store being closed.
Timers are found by suffix now, so a rename cannot silently disarm it, and the
window drains its workers with user input excluded (U9) rather than pumping
every event while views are being destroyed.

### Fixed — the View menu reset preferences nobody touched (U5)

`ViewPreferences(prefs.columns, n, prefs.font_pt)` enumerates fields by
position, so everything after the third was silently dropped: **changing the row
height turned grouping back on** and discarded "show why each result matched".

`replace()` was already imported and used correctly two lines away. The test
added is written over `dataclasses.fields`, so a field added later is covered
without anybody remembering to extend it.

### Fixed — mail sorted on what it displayed (U4)

The real values were stored in a custom role and never read: `QTableWidgetItem`
compares `DisplayRole` and nothing else, so "10 KB" sorted before "3 KB" and
dates sorted alphabetically — the exact failures the comment above the code
claimed to prevent. `SortableItem` compares the stored value.

Mail also had **no keyboard route at all** (U6): double-click was the only way
in, in a project whose specification requires keyboard-only operation end to
end. Enter opens the selected message, which means searching inside it — a
message has no file to open, only a synthetic path.

### Fixed — results could be unreadable, and matches were invisible (U3)

The results delegate painted with `option.palette.text()`. Nothing in this
application ever sets a palette — theming is a stylesheet — so that colour is
the operating system's. Forcing dark mode on a light-mode Windows painted
near-black text on `#1e1f22`, and only the results list was affected, because
only the results list paints itself.

It reads the same tokens the stylesheet is built from now, so the two cannot
drift. `QListView` is styled at all (it stopped being a `QListWidget` when rows
became data and the rule was never updated), `#statWarn` has a rule — a figure
the code had decided was worth warning about rendered identically to one that
was fine — and matched words in a snippet are drawn in the highlight colour
rather than bold alone. `#resultSnippet b` was never going to work: no
stylesheet rule reaches a `QPainter`.

### Fixed — four controls that did nothing (U6)

`rerank_toggled` and `cloud_toggled` were emitted into nothing. The rerank
switch looked like it worked and changed no behaviour; the cloud switch was read
live when a run started, so it worked for that run and reset to off at the next
launch — the harder of the two to notice. Both are wired and persisted, and
rerank applies live, because a quality setting that needs a restart is one
people conclude does nothing.

`ui:tray_minimise` and `ui:tray_close` were read at startup and **written by
nothing**, so the entire tray feature was unreachable. Off by default is still
right; "off by default" and "no way to turn it on" are different things.

`set_next_run` existed, said what it was for, and was never called — so the one
line answering "will this run on its own, and when" was permanently blank on the
page whose job is to answer it.

### Fixed — widget churn during indexing, and Interpret searching twice

U8: every skip row — three or four labels and a button — was destroyed and
rebuilt on **every progress tick**, for the hours a 100GB index takes, usually
to show numbers that had not changed. An identical tally now does nothing; the
same reasons with new counts update in place.

U10: writing the interpreted query into the search box fires `textChanged`,
which restarts both debounce timers exactly as typing does — and then the code
dispatched immediately. Every interpretation ran the full pipeline twice.

### Fixed — a new search kept the old scroll position (U11)

`_rebuild` preserves scroll on purpose, so toggling a preference does not throw
away somebody's place. A new query went through the same path, landing them
mid-list with the best hits scrolled off screen — which reads as the search
having returned something worse than it did.

### Added — the index location and meaning model are flows, not fields

Work order §6. Both change what the index *is*, and both were unreachable: the
location was a read-only box, and the model had no control at all.

Typing a path does not move an index; it points at a different, probably empty
one. The flow offers the three things somebody could mean — move it, adopt the
index already there, start empty — enables each only where it means something,
and states the consequence before it is chosen. Changing the meaning model
states its cost in chunks and hours, and refuses to offer the button when the
model has not actually changed.

Neither moves a byte while the application is running: the stores are open, and
copying a database from underneath an open connection is how a half-copied index
becomes the only index.

### Added — accessible names, and a rule that keeps them

The review found none anywhere in `app/ui`. The concrete case is the per-row
checkbox in the file-types table: sixty identical "check box, not checked"
announcements, with the only thing distinguishing them available visually and
nowhere else.

`test_accessible_names.py` enforces the two shapes of control that can never
label themselves — a `QLineEdit` has no text, ever, and a `QCheckBox()` with no
string has nothing to announce. A tooltip is deliberately not accepted as a
label: not every screen reader reads one, and on some platforms no keyboard can
reach it.

### Fixed — the vector index got permanently slower every run (P4)

**The hardest scalability cliff in the review, and it was unrecoverable.**
LanceDB writes a new fragment for every `add` and a new dataset version for
every `delete`, and never compacts itself. **No `optimize` or `compact_files`
call existed anywhere in the codebase.** At twenty million chunks that is tens
of thousands of fragments and millions of versions, all of which a scan has to
open — so the index got slower with every run and never recovered.

`VectorStore.maybe_compact()` now merges fragments and drops versions older than
an hour: every 50,000 rows during a run, and **always at the end of one**,
because a nightly incremental index adds a few thousand chunks and would
otherwise never cross the threshold at all.

Two smaller things fed the same cliff:

- **`delete_by_file_ids` ran once per document, including on a first index**,
  where by definition there is nothing to delete. A hundred thousand documents
  meant a hundred thousand dataset versions created to remove nothing. It now
  returns immediately when the table is empty.
- **`maybe_create_index` called `count_rows()` on every batch** — a scan of a
  growing table, on the write path, to answer a question that only matters when
  it crosses a threshold. The total is tracked and corrected from the table on
  every open.

Twelve tests, against real LanceDB rather than a fake: the entire subject is
what the library does with fragments and versions, and a fake would only assert
what I believe about that.

**Recorded, not done:** a *new* file added to an *existing* index still costs
one no-op version. Knowing it was new would mean changing what `replace_chunks`
returns for every caller, to save a version that periodic compaction now
collects anyway.

### Fixed — two missing indexes, and a filter that scanned (P3, P5, P6)

Schema **v5**. Both indexes are additive: no table is touched and no row is
rewritten, so an existing index gains them in seconds. `ANALYZE` runs after,
because SQLite picks a plan from statistics and will not use an index it knows
nothing about.

- **`files.source_kind` did not exist**, and `keyword.py` carried a comment
  saying it did — *"already indexed, so this costs nothing"*. Clicking the Mail
  chip full-scanned `files` on every keystroke. Now
  `SEARCH f USING COVERING INDEX`. (`documents` is `NOT IN` and can never seek,
  so it still scans, but scans the covering index rather than the table.)
- **`files.mtime_ns` did not exist**, and `after:`/`before:` compare against it.

**`idx_files_mtime` is also what fixes the filter-only browse — which is not
where the diagnosis pointed.** The review read `WHERE c.ordinal = 0 ORDER BY
f.mtime_ns DESC` as needing a `chunks` index, since `idx_chunks_file_ord` is
`(file_id, ordinal)` and `ordinal` is not leading. A partial
`chunks(file_id) WHERE ordinal = 0` was written — and measured **with it and
without it, the plan and the timing were identical**. The planner never chose
it. What the query needed was an ordered way *in*: given `mtime_ns`, SQLite
walks `files` newest-first and finds each file's first chunk through the index
that already existed, so `LIMIT 20` stops after twenty files.

Measured on 20,000 files / 120,000 chunks: **6.30ms → 0.04ms**, and
`USE TEMP B-TREE FOR ORDER BY` leaves the plan. The unused index was dropped
rather than shipped — it cost a write on every document indexed, would never
have been read, and would have stood as evidence for a diagnosis the
measurement did not support.

**P5: `LOWER()` removed from all five `LIKE` predicates** — `path:`, `name:`,
`from:`, `to:`, `subject:`. SQLite's `LIKE` already folds case for ASCII, so
`LOWER(sender) LIKE '%dave%'` and `sender LIKE '%dave%'` return the same rows;
the wrapper only added a function call per row. Measured on 60,000 messages:
**11.54ms → 4.64ms, a 2.49× cut**, identical 1,650 rows both ways.

This does *not* make them use an index and nothing can — a leading `%` gives
nothing to seek to. The scan is at least a covering one, and the subquery is
materialised once rather than re-run per row.

**Still open, now with a number.** Substring matching over mail headers costs a
full scan of `idx_messages_sender` — ~5.5ms at 30,000 messages, and it is
linear. At 20M that is seconds. The fix is an FTS index over the message
headers, which is a schema change and its own piece of work, not part of adding
two indexes.


Twenty tests in `test_query_plans.py`, asserting `EXPLAIN QUERY PLAN` output
against the **real** query builder rather than a hand-written approximation of
it — an approximation is what first suggested the partial index was being used.
They cover the migration applied to a database created *before* v5 (the only
path that happens for real), that the dropped index stays dropped, and that
every filter still matches case-insensitively against a deliberately mixed-case
fixture. One test pins the `LIKE` folding assumption itself, so if
`case_sensitive_like` is ever switched on it fails with an explanation instead
of five filters quietly returning nothing.


### Fixed — a search during indexing could return a document that was never indexed (A1)

**Filed as a scalability finding. It was a correctness one.**

`SqliteStore` held one connection shared between threads, with
`check_same_thread=False` and a comment explaining that writes were serialised
by a lock. Serialising the writes was never the problem. Two threads on one
connection share its *transaction state*, so a search running during indexing
read **inside** the indexing transaction — and when that transaction rolled
back, the search had already returned a result for a document that never
entered the index. A user clicking it opens nothing.

Reproduced before changing anything: a reader on a second thread saw
`/DIRTY.pdf` mid-write and the row was gone afterwards.

Each thread now gets its own connection, opened lazily on first use — Qt hands
work to threads this code never sees created, so requiring every worker to call
`connect()` would mean each one either remembering to or quietly sharing the
main thread's connection again. A registry keeps every connection handed out,
because `threading.local` cannot be enumerated and `close()` must still close
all of them; a connection left open holds a file handle, and on Windows that is
what stops the index directory from being moved afterwards.

WAL is what makes this cheap rather than a trade: separate connections each get
a consistent snapshot of committed data, readers never block the writer, and
the writer never blocks them.

**The first version of the fix reintroduced the blocking it was removing.** One
lock was guarding two unrelated things — serialising writes, and protecting the
connection registry — so a reader opening its *first* connection waited for
whatever write was in flight. The test hung rather than failed, which is how it
was found. There are now two locks, always taken in the order write → conns,
and `close()` takes both in that order so an in-flight write finishes rather
than having its connection closed underneath it.

Thirteen tests. Three of them fail on the old code — the dirty read, the
blocking, and connection identity — which is the only reason to trust the other
ten. The rest guard what per-thread connections newly make possible to get
wrong: pragmas set on the first connection but not the next (`foreign_keys` is
per-connection, and the cascade deletes that keep chunks with their file depend
on it), a worker that never called `connect()`, a closed store quietly
reopening itself, and migrations running once however many threads arrive.

### Added — search says when it has quietly done a worse job

Standing rule from the owner: **nothing fails silently.**

The case that produced it: a search returned sixty keyword hits and zero vector
hits. There *was* a warning — `no vector hits ... meaning-based search may not
be working` — and it went to the log. Visible to somebody running from a
console, and to nobody else. In the window the search looked like it had
worked.

A degraded result that is indistinguishable from a good one is the failure
nobody ever reports. It is worse than a crash, because a crash gets fixed.

`SearchResponse.notices` now carries degradations out to whoever asked, as
`Notice(code, message)` — a code because the contract is that the UI never
parses a message string to decide anything, and a message because the wording
should be free to change without breaking a caller. Three so far:
`NOTICE_NO_VECTORS`, `NOTICE_UNMATCHED_TERMS`, `NOTICE_RERANK_UNAVAILABLE`.

- **The judgement lives in one place.** `keyword_count` and `vector_count` were
  already on the response for exactly this and were not enough: raw numbers
  mean every caller has to know the rule that turns them into a conclusion.
- **`app.cli search` prints them above the results**, and `--json` lists them
  before `results` — a degradation buried under twenty result objects has been
  reported and read by nobody.
- **Each says what still works and names the remedy.** A warning without an
  action is a warning somebody has to research.
- **Not reported when it is not a degradation.** Zero vector hits on a query
  that matched nothing anyway means nothing, and reporting it there would train
  people to ignore the message that matters.

Nine tests, against a real store with a deliberately empty vector store — the
reported state. The first version of them asserted the *condition* rather than
the engine, recomputing `keyword and not vector` in the test and passing
whatever the engine did; that is the same shape as the `--help` test that could
not see the bug it was written for, and it is why these go through
`SearchEngine.search`.

**The window does not draw them yet** — `app/ui/` is the other thread's, and
the task is filed. Backend emits, CLI shows.

### Fixed — `app.cli formats` hid 56 of the 78 file types it indexes

The owner concluded from this command that source code was not being searched.
It was. `formats` listed 61 extensions and omitted `.py`, `.cs`, `.java`,
`.js`, `.ts`, `.sql`, `.go`, `.rs`, `.cpp`, `.php`, `.rb`, `.sh`, `.ps1` and
forty more.

Two causes, both in `cmd_formats`:

- **`rules.describe()` was called without the registry.** Its own docstring
  says *"Pass the registry"*, and explains exactly this: configuration only
  lists extensions that need to differ from an extractor's defaults, so the
  rules alone describe a fraction of what the application reads.
- **The remainder was behind `--all`**, a flag nobody knew to pass. So the
  default output of a command called `formats` answered "which types are
  configured" rather than "which types are indexed", and only one of those is
  a question anybody has.

Now 117 on, nothing hidden. Same family as `doctor` reporting its own defaults
and `stats` printing both halves of the embedding gap in different sections: a
diagnostic that could not see what it claimed to report, believed because it is
a diagnostic.

### Changed — every file type is on by default

On the owner's instruction: *"all formats should be on by default."*

The twelve converter routes — `.doc`, `.xls`, `.ppt`, `.rtf`, `.pages`,
`.numbers`, `.key`, `.epub`, `.fb2`, `.wpd`, `.pub`, `.dwg` — shipped disabled,
on the reasoning that a format silently failing on every file is worse than one
that says it is off.

That was right about the failure mode and wrong about the remedy. A route that
is off is indistinguishable from a format nobody thought of, and somebody with
LibreOffice already installed had to find a setting they did not know existed
before their own `.doc` files were indexed.

**And the failure was never silent.** `ERR_CONVERTER_MISSING` names the binary,
says the file is indexed by name only until it is installed, and carries the
install command — which is strictly more useful than `ERR_UNSUPPORTED_TYPE`,
"this application does not read .doc", a statement that was not true and that
nobody could act on.

Five tests encoded the old decision and now encode the new one, including the
`.dwg` route and the legacy-Office message. One is deliberately tolerant of
both `ERR_CONVERTER_MISSING` and `ERR_CONVERTER_FAILED`, because which appears
depends on whether LibreOffice is on the machine and both are actionable.

### Fixed — closing the window left the process running

Reported: *"when you close the gui it does not exit it is stuck i need to press
ctrl c"*.

`SearchEngine` runs its two retrievers on a `ThreadPoolExecutor`. Those worker
threads are **non-daemon**, and `concurrent.futures` installs an `atexit` hook
that joins every one of them at interpreter shutdown. `shutdown(wait=False)`
returned immediately — and then Python blocked on that join anyway, after Qt
had closed the window, with nothing left on screen to explain the wait.

So the window vanished, the process stayed, and the only way out was Ctrl+C in
a console the person may not have had open.

`cancel_futures=True` drops what is queued, which is exactly the work worth
abandoning: nobody is waiting for a result in a window that has closed. Work
already running still finishes — that cannot be helped without killing a thread
mid-write — which is why `shell._drain_workers` keeps its bounded grace period
on top.

Five tests, one failing on the old `close()`. Ruled out first, by reading the
code rather than guessing: close-to-tray defaults to `False`, the pipeline's
threads are all daemon, `_drain_workers` is deadline-bounded, and nothing sets
`setQuitOnLastWindowClosed`.

### Added — the window now says when a search has quietly done a worse job (UI-1)

The last piece of the owner's standing rule. The engine detected the
degradation, `SearchResponse.notices` carried it, the CLI printed it — and the
window, the only interface the owner actually uses, said nothing at all.

`widgets/notice_bar.py` draws it above the results, in `#statWarn` rather than
`#resultsSummary`. That colour choice is the point: the summary is `text_faint`,
which is what "20 results in 240ms" uses, and `#statWarn` exists in `theme.py`
precisely because a figure the code had decided was worth warning about was
rendering identically to one that was fine. Using the faint one here would have
been making that same mistake inside the fix for it.

**The decision lives in `presenter.notice_line`, which imports no Qt.** That is
what makes it testable: `QtWidgets` needs a display, and the last time UI logic
was verified by reading rather than running it, `QPdfView()` shipped without its
parent argument and crashed the window on startup. Ten tests cover what is
shown; the widget only draws the string it is handed.

`search_view.py` was at 249 of the 250 code lines the presenter guard allows, so
this needed two extractions first — `record_open_async` and
`decorate_results_async` into `workers.py`. Both are plumbing every view wants,
both keep a database write and a filesystem stat off the interface thread, and
the view is at 246 now.

### Fixed — one genuine test failure, and it was the fixture's name

`test_shutdown_stops_every_timer_a_view_owns` has been failing for as long as
anybody has looked, and it was neither `stop_timers` nor the view. The fixture
attribute meant to represent "something that is not a timer" was called
**`not_a_timer`** — which ends in `_timer`, so the suffix rule stopped it,
correctly. A name that reads as "not a timer" to a person and *is* one to
`str.endswith`.

Renamed to `refresh_handle`. The production code was right the whole time.

With that gone, every remaining failure in the suite is environmental: 42 need
model downloads and one needs `libEGL` for `QtWidgets`. **Zero genuine defects.**

### Verified — the forty UI tests that had never executed (UI-3)

The thread-merge handover named this the single highest-value thing to inherit:
the UI had been shipping on inference because no PyQt6 was available where it
was written. It is available here.

**122 of 123 pass.** The one failure cannot load `QtWidgets` — `libEGL.so.1` is
missing and cannot be installed without root — so the widget-level assertions in
`test_command_subsets.py` still need a run on Windows. Everything reachable
through `QtCore` is now verified rather than reasoned about.

### Added — `app.cli repos --scan`, to answer "are there any" in seconds

`app.cli repos` lists what the last index run attributed, which cannot answer
the question somebody actually has: *are there git repositories in my search
folders at all?* That needs a full run first, and on a fresh v6 index the
answer is an empty list — indistinguishable from "none".

`--scan` runs the detection half of the walk on its own. Read-only: nothing is
written, nothing is embedded, and no file is opened except a `.git` pointer.
It reports repositories found beneath each folder **and** any repository the
folder itself sits inside, which is the case a downward walk cannot see.

An empty result says so in words and adds that nothing is wrong — most folders
have none, and silence there reads as a failure.

### Added — `app.cli gitsearch`, and the answer to whether history search can exist

Work order §14 and `HANDOFF-ui-to-backend.md` B4 ask the same question from
opposite sides: can searching git history live behind the Enter key, against a
contract of p95 under 300ms warm?

**A measurement, not a feature.** First run, against this repository:
`git log -S` over **75 commits took 1.59s**; `git grep` over one revision took
0.33s. Seventy-five commits already costs five times the whole search budget,
and `git log -S` diffs every commit, so the cost is proportional to history.

That settles B4 in favour of the UI thread's own position: history search is a
separate, explicitly slow, cancellable action — never a mode of the search box.
The full table belongs in `HANDOFF.md` once it has been run against a large
repository; one small repository on one machine is a direction, not a
conclusion.

Every row records what it was measured under, and a depth deeper than the
repository is flagged `representative: false` rather than reported as fact — a
"50,000 commits" figure taken against 800 commits is precisely the sort of
number that ends up justifying the wrong build. Exit code 1 is treated as "no
matches" rather than failure, because both git commands use it and reporting
every unsuccessful search as a broken tool is how a measurement turns into a
bug report.

Fifteen tests, all against a fake runner so they pass on a machine with no git.
One of them walks the AST of every module in `app/` and fails if anything
outside the CLI imports this: it shells out to a command that takes minutes, and
the first non-negotiable is that no unbounded work sits on a search path.

**On GitPython** — it would not help here. It mostly wraps the same subprocess,
and what a slow cancellable job needs is streaming, a hard timeout and the
ability to kill the process, all of which are more direct without it. It earns
its place only if history is ever traversed as objects rather than grepped, and
this measurement is what decides whether that is worth doing.

### Fixed — the UI thread's handoff: B1, B2 and B3

`HANDOFF-ui-to-backend.md`, all three landed.

**B1 — an error that described itself.** A user saw:

    DETAIL: Could not create the 'chunks' table: AppErrorException:
            [ERR_UNEXPECTED] An unexpected error occurred in storage.vectors.

`VectorStore.db` raises `AppErrorException` when the store was never connected,
and `ensure_table`'s broad handler caught that *already-diagnosed* error and
wrapped it in a second `ERR_UNEXPECTED`. Because `str(AppErrorException)`
renders only the headline, the inner message was destroyed rather than nested —
the real cause, `VectorStore used before connect()`, appeared nowhere. Wrapping
an error that already carries a code and a fix downgrades a diagnosable failure
into an undiagnosable one.

Fixed with `except AppErrorException: raise` ahead of the general handler. An
AST sweep of every module outside `app/ui/` found this was the only site.

**B2 — `vectors_ready`**, the field the UI asked to be named.
`SqliteStore.vector_coverage(vector_rows)` returns `vectors_ready`,
`vector_rows`, `chunks_total`, `coverage` and `missing`. It is **not**
`rows > 0`: a store holding 5% of the corpus is not ready, and calling it ready
is how a half-working search goes on looking healthy. Measured against
`chunks_total` rather than `chunks_embedded`, which is the trap the CLI's own
version documents having fallen into.

**B3 — `repo_files(repo_id, limit)`.** The Code tab worked without it by
scanning the whole `files` table per expansion. `source_kind = 'file'` is in the
WHERE clause so mail can never appear in a list of source files, and `limit` is
deliberately not clamped — the caller asks for `limit + 1` to tell "exactly
500" from "more than 500", and capping it makes a truncated list
indistinguishable from a complete one.

### Fixed — a vector write that produced nothing was recorded as success

**This is how a file ends up `INDEXED` with no vector, permanently.**

`VectorStore.add` returns how many rows it wrote. The pipeline ignored the
return value and marked the chunks embedded and the file `INDEXED` regardless —
so a write that produced nothing was recorded as complete, and because the file
was then `INDEXED` and unchanged, every later run skipped it. Stuck for good.
The only symptom is that meaning-based search quietly covers less of the corpus
than it claims, which is the failure nobody reports.

Such files are now left `PENDING` — the state that *is* retried — and it is
logged as an error rather than passed over. The chunks are already written, so
the retry costs only the embedding.

Two tests, both failing on the old pipeline. The second one matters as much as
the first: `PENDING` is only the right answer if a later run genuinely
recovers it, so that is asserted rather than assumed. (Its first version left a
patched `add` in place across both runs — the fixture hands both pipelines the
same `VectorStore` — and so failed the second run for the first run's reason.)

The fake `VectorStore` in `test_index_freshness.py` returned `None` from `add`
rather than a count, which turned a real check into nineteen false failures. It
honours `-> int` now, and the pipeline treats `None` as "did not report" rather
than as "failed" — silently reading no answer as failure would leave every file
`PENDING` for ever.

### Fixed — `reembed` looked like it had hung, and `stats` said everything twice

**"This seems stuck" — and it was not.** `reembed` printed nothing until its
first batch of 256 passages finished. At the 4.4 passages/second `embed-bench`
measures on a real machine that is nearly a minute of completely silent
terminal, and the correct response to a silent terminal is to assume it died
and kill it — which loses the work.

The standing rule is that nothing fails silently, and a long operation that
says nothing is the same fault in different clothes: there is no way to tell it
from one that has died. It now says how many passages it is about to embed, how
many are already done, and that the first line takes a minute.

**And `stats` printed the embedding gap twice**, because a second reporter was
written without looking for the first. `semantic_search_warnings` had existed
since `ec85b76` and was better: it also handles an empty vector store and
orphaned rows, and its own comment documents having fixed the exact bug the new
copy reproduced — comparing the row count against `chunks_embedded` rather than
`chunks_total`, which agree perfectly on a corpus where almost nothing was ever
embedded, so the check prints nothing on the one machine it was written for.

The duplicate is gone. It had tests and the original had none, so the tests now
point at the original — including that bug, and the branch ordering that makes
under-coverage reported ahead of orphans (a missing vector cannot be found at
all; a stale one merely fails to open).

### Fixed — `app.cli stats` could not answer the warning that sends people to it

    no vector hits for a query with 60 keyword hits - meaning-based search
    may not be working. Check: app.cli stats

`stats` printed SQLite's chunk count and LanceDB's row count in **different
sections** and left the reader to notice they disagreed. That difference is the
whole failure: SQLite marks a chunk `embedded = 1` when its vector is written,
and if the write did not survive - an interrupted run, a rebuilt vector store -
the mark stays and nothing anywhere compares the two.

Same shape as `doctor` reporting its own defaults: a diagnostic that cannot see
the problem it exists for. It now reconciles the stores, says which way they
disagree, notes that keyword search is unaffected, and names `app.cli reembed`
as the remedy - a number without an action is a number somebody has to research.

It also confirms when they *agree*, because silence on success is
indistinguishable from the check not running.

Verified that the batching in the previous entry did not cause this: a full
pipeline run writes 25 chunks, marks 25 embedded and leaves 25 rows in LanceDB.

### Fixed — indexing paused and resumed for ten minutes without getting anywhere

Reported from a real run against the project folder:

    pause - Indexing has added 1,501MB (now 1,601MB), above the 1,500MB...
    run   - clear
    pause - Indexing has added 1,539MB (now 1,638MB), above the 1,500MB...

The memory ceiling is growth above a baseline taken on the **first probe**,
seconds into the run. The embedding model, the reranker and the OCR engine all
load *after* that — the same run's log said `OCR engine loaded in 3.0s`, seven
seconds in — and together they are roughly a gigabyte that is never released.
So growth sat permanently just over the ceiling and the governor oscillated for
as long as it was left alone. It was making progress, and paying a pause cycle
for it.

**The fix is not a bigger ceiling.** A pause only helps if the memory can
actually be released, and a pause that does not move RSS has proved that it
cannot. The floor is then raised to accept that level as resident. Indexing may
still only add `memory_mb` above the new floor, so this is the ceiling being
measured from the right place rather than being ignored — and it is logged at
WARNING, because a limit that quietly moves itself is exactly the sort of thing
that must never happen silently.

The first version of that rule counted *rising* memory as "the pause achieved
nothing" and would have handed a genuine leak another ceiling's worth of
headroom every few seconds. Rising memory is a leak, not residency; only memory
that sits still settles. Caught by the leak test, which is why it is in the file.

`Verdict` gained a `cause` field so the rule can tell a memory pause from a CPU
one. Matching on the reason *text* would break the moment the wording changed,
and the wording is meant to be free to change — it is what the user reads.

### Fixed — the indexer read its own log file while writing to it

`LOG_PATH` defaults to `<project>\logs`, so indexing the project folder swept
up the application's own logs — and its SQLite index, vector store and model
cache, had `DATA_PATH` sat under a root too.

`WalkConfig.exclude_paths` prunes by **absolute path**, not by name. Excluding
`logs` by name would have been wrong twice over: it would miss a log directory
anywhere else, and it would hide a `logs` folder that genuinely belongs to the
person. `app.cli index` derives the set from Settings via `own_paths()`, and the
pipeline additionally protects the two stores it holds first-hand — so a run
started from the window is safe too, without reaching into `app/ui/`.

A root that *is* an excluded path is now skipped outright: pruning only filters
subdirectories, so without that the exclusion worked everywhere except the one
place somebody aimed it.

**The Windows-path trap, for the third time this session.**
`Path(r"D:\Data\fts\knowledge.db").parent` is `.` off Windows, because
`PurePosixPath` treats the whole thing as one filename — so the database
directory was excluded from nothing. Split on both separators, as
`sqlite_store._basename` already does for the same reason.

### Added — repository awareness (schema v6)

`WORKORDER-git-search-backend.md` phase 1. The request behind it was a 56-flag
specification for a git search platform; the finding that set the scope is that
**source code was already indexed**. `TEXT_EXTENSIONS` covers `.py .js .ts .cs
.java .sql` and twenty more, so a file inside a repository on an indexed root
has been searchable by keyword and by meaning all along. The gap was not
extraction, embedding or search — it was that nothing recorded which repository
a file belonged to.

Three things, and no more:

- **Detection during the existing walk.** `.git` is already in
  `DEFAULT_EXCLUDE_DIRS`, so the walker stood next to the evidence on every
  pass and discarded it. It now costs a membership test against a list
  `os.walk` has already built. `.git` as a *file* is handled — a submodule or a
  linked worktree — which anything reading only the subdirectory list walks
  past. An unreadable, empty or binary one means *not a repository*, never an
  error: detection is a convenience laid over a walk that has real work to do.
- **`repo:` filter and a `code` scope.** `code` means **in a repository**, not
  *has a code extension* — `type:code` still answers the second and is
  untouched. A `.md` in a repository is in scope; a `.py` in Downloads is not.
  `documents` deliberately still includes repository files: the scope is a
  narrowing offered to the person searching, not a partition of the corpus.
- **`app.cli repos`**, so detection is checkable headless before any UI exists.

Schema v6 is additive — one table, one nullable column — so an existing 100GB
index gains it in seconds with no re-index, and `repo_id` stays NULL until the
next run attributes it. `ON DELETE SET NULL`, deliberately not `CASCADE`: a
repository that is moved or unmounted must not take the indexed content of its
files with it.

No settings, no new error codes, no git subprocess, no new extractor.

**Two bugs, both found by running it rather than reading it.** `repo:a,b`
returned nothing — each name became its own AND clause, and a file belongs to
exactly one repository, so repeated names could never match; they now OR, which
is the only sensible reading. And `repos.name` stored the full path instead of
the basename, because `Path(r"D:\SearchProject").name` does not split
backslashes off Windows — `_basename` exists in `sqlite_store.py` for precisely
that trap and is now used, so `repo:leasha` matches at all.

Twenty-one acceptance tests, T1-T11 from §11 quoted in the module docstring.
Two of them carry the weight: **T3**, a file sitting directly in a repository
root, which is the ordering trap — `os.walk(topdown=True)` yields those files
in the same iteration that finds the root, so detection placed after the
filename loop leaves exactly them unattributed while everything nested is
correct, and that reads as flakiness. And **T11**, that a tree with no `.git`
anywhere costs nothing: this work is only justified while it is free, so it is
asserted twice — against the same walk with detection off, and by proving no
file is opened at all.

**Hidden files are indexed, and that is what makes this work at all.** `.git`
is created with `FILE_ATTRIBUTE_HIDDEN` by git on Windows, so a walker that
skipped hidden entries would find no repositories on the only platform this
ships to - passing every test here and doing nothing on a real machine. Checked
rather than assumed: nothing in the walk path reads the hidden bit, there is no
blanket dotfile exclusion, and a dot-prefixed file indexes normally. Two
Windows-only tests set the attribute for real and are skipped elsewhere.

**Phase 2, history search, is not built and not authorised.** It is gated on a
measurement: searching a full history is O(commits x changed files), which on a
50,000-commit repository is minutes against a contract of p95 under 300ms warm.
See `HANDOFF.md` for the numbers that have to exist first.

### Fixed — CLI `timings_ms` measured process startup, not search

The window loads the models on a background worker before anyone searches. The
CLI did not, so both loaded **lazily inside the timed block** — `retrieve`
included loading the embedder and `rerank` included loading the cross-encoder.

A one-shot `app.cli search` reported `rerank: 3047ms` for work `rerank-bench`
measures at 660ms. The gap reads as the reranker being slow, and every
model-to-model comparison drawn from those numbers was meaningless — which is
the only thing they were being used for. `search` now warms up before it
starts the clock, matching what the window does.

The numbers already reported from CLI runs were inflated by one model load
each, so they were never comparable to the benchmark or to the application.

### Fixed — `doctor` reported its own defaults, not the application's

`doctor.py` carried its own copy of six defaults —
`env_path("RERANK_MODEL", "BAAI/bge-reranker-base")` and five more. So after
`RERANK_MODEL` was removed from `.env` so that the faster default could apply,
doctor went on reporting **and loading** the model it had been handed as a
fallback. It said `BAAI/bge-reranker-base` while search used
`Xenova/ms-marco-MiniLM-L-6-v2`.

The config change had worked. The diagnostic had not, and it was the
diagnostic that was believed — it sent the owner hunting a bug that was not
there. **A tool that reports its own defaults instead of the application's is
worse than no tool, because it is trusted.**

The same shape as the installer pinning and the un-removable `.env` key: a
second copy of a default, somewhere the first one cannot reach.

`doctor` now reads `settings_registry`, which is stdlib-only on purpose so this
does not compromise its ability to run before the dependencies are proven. The
import is guarded anyway — a doctor that cannot start cannot tell you why
nothing starts — and `.env` still wins over the declared default, so a
deliberate pin is still honoured.

Ten tests, eight of which fail on the old `doctor.py`, including one per key so
a newly hardcoded default fails by name.

### Fixed — changing the reranker invalidated nothing

`_cache_key` included a bare `rerank` on/off flag and **not which model did the
reranking**. So every result reranked by one model was served afterwards as
though a different model had produced it, and changing `RERANK_MODEL`
invalidated nothing: every query already asked kept its old ordering until the
index generation happened to change.

Found while writing the instructions to measure the model swap — the obvious
"run it twice and compare" would have returned the *old* model's cached
ordering the second time, in about a millisecond, and proved nothing. Swapping
to a reranker measured 9.2x faster and finding search unchanged is exactly what
this looks like from the outside, and the model would have taken the blame.

The function's own docstring already said it, about the index generation: *a
cache that cannot tell it is stale is a lie*.

The model name is now in the key, and only when reranking is on — with it off
no model touched the result, and keying on one would split the cache between
two states that produce identical answers. The key version went `v2` to `v3`,
so nothing cached under the old format is read back under the new one.

Fourteen tests, three of which fail on the old key. They encode the general
rule rather than this one field: anything that changes the answer belongs in
the key, anything that does not must stay out of it.

### Added — `.env` keys can be removed, so a default can reach an existing install

`env_writer.render` could only ever *set* a key. There was no operation, in the
UI or anywhere else, that removed one — and since `.env` always beats the
default in `config.py`, **a key written once was pinned for ever**.

That is the general form of the reranker bug below rather than a separate
problem. The installer pinning `RERANK_MODEL` was one way in; the moment
anybody changes a setting in the UI, that value is frozen the same way and no
improved default can ever reach them again.

A value of `None` now removes the key. Removing one that is absent does
nothing, rather than appending `RERANK_MODEL=None` and pinning the key to a
model that does not exist.

Five tests, including one end-to-end through `load_settings`: a pinned value,
removed, and the declared default in effect again. It asserts against
`Settings.model_fields["rerank_model"].default` rather than a model name, so it
keeps passing the next time that default changes on a measurement — which is
the thing it exists to protect.

**The owner's `.env` was fixed with this**, not by hand. `env_writer.py` opens
with *"Non-negotiable 11: `.env` is written by the application, never by the
user"*, and the previous note here asking them to delete line 13 was in direct
breach of it. One line removed, the other nineteen byte-identical.

### Fixed — the installer pinned the slow reranker into every `.env`

`install.ps1` wrote `RERANK_MODEL=BAAI/bge-reranker-base` into every generated
`.env`. That is the slowest of the four models measured — **9.2× slower than
the default, for identical scores** (80/88/75 on both) on the evaluation
corpus.

The default in `config.py` was changed to `Xenova/ms-marco-MiniLM-L-6-v2` on
that measurement, and it **did nothing for anybody**, because a value in `.env`
always wins. The measurement said 9.2× faster and no machine ever got it. It
was still the slow model on the owner's install today, confirmed by `doctor`.

The installer no longer writes the key at all, so the code default applies and
a model chosen on a later measurement reaches existing installs too. An
override is documented in the generated file rather than set.

**This is not about one key.** Any tuning value an installer writes can never
be improved afterwards for the people who already ran it — which is everybody.
`test_launcher.py` now fails if `install.ps1` pins `RERANK_MODEL`,
`RERANK_TOP_N`, `AND_TERM_LIMIT` or `EMBED_BATCH`, with a second test asserting
it still writes the paths and identifiers that are not choices, so the guard
cannot be satisfied by writing nothing.

**Existing installs are not fixed by this** — `.env` is generated once. Delete
the `RERANK_MODEL` line to pick up the default.

### Fixed — `leasha --help` crashed

`TypeError: %o format: an integer is required, not dict`, for every user, on
the most basic command there is.

argparse runs every help string through `%` formatting so that `%(default)s`
expands. The `rerank-bench` one-liner read *"it was 93% of one 9-second
search"*, and `% o` is a valid conversion — space-flagged octal — which wants
an integer and gets argparse's parameter dict. Escaped to `%%`.

**`rerank-bench --help` worked the whole time**, which is why this survived: a
subparser only formats its own strings, so the broken one only surfaced in the
top-level listing that renders every subcommand's description. `test_cli_wiring`
already asked each subparser for its help and caught nothing.

Three tests: `format_help()` on the top-level parser, on every subparser, and a
walk over every help string that names the offending text rather than leaving
argparse's message to be decoded. The walk missed the bug on its first
attempt — the subcommand one-liners live on `_choices_actions`, not on
`action.help` — which is worth recording, because a test that cannot see the
bug it was written for still reads as coverage.

### Changed — indexing one message cost six commits, now about one (P7)

Counted by tracing `COMMIT` statements rather than by reading call sites — the
estimate from reading was four. The six were `upsert_file`, `replace_chunks`,
`set_message`, **two** `set_state` calls for the checkpoint, and `mark_indexed`.

A commit costs about **fourteen times** the same statement inside an open
transaction — 35.2µs against 2.6µs, WAL with `synchronous = NORMAL`. Six per
document across twenty million messages is roughly **1.2 hours** of a run spent
committing; at one it is 0.2.

- **`SqliteStore.batch()`** groups writes into one transaction. `write()` joins
  an open batch rather than nesting inside it — SQLite has no nested
  transactions, so a `write()` that started its own would commit the batch
  early and turn the grouping into a lie that never failed. The depth counter
  is per-thread, so indexing can never quietly strip the transaction from a
  write on another thread.
- **`_write_one`** wraps its three SQLite writes in one batch: 3 → 1. The
  LanceDB delete moved *outside* the block deliberately — a batch holds the
  write lock, and putting a vector-store call inside would trade commit
  overhead for making every other thread wait on LanceDB. It does not change
  what a crash leaves behind: the file is `PENDING` either way, so it is redone.
- **`mark_indexed_many()`** marks a whole embed batch in one transaction rather
  than one per file.
- **The checkpoint** now uses `set_states`, which already existed for exactly
  this and which the checkpoint simply was not calling: 2 → 1.
- `_write_marker` and `_record_skip` batch their two writes each.

Fourteen tests. Most are not about speed but about atomicity not changing shape
underneath code that relies on it: a failure rolls the whole group back, a
nested batch commits once at the outermost, a failed batch does not leave the
store believing one is still open, and the commit counts themselves are
asserted so a regression shows up as a number rather than as a slow run.

**Writes are still serialised, and a batch holds the write lock for its
duration.** That is the existing model rather than something new, but it is now
load-bearing: slow work inside a batch would block every other thread's writes
for as long as it took. One test pins this by holding a batch open and checking
that another thread's write waits, then completes once the batch closes. (The
first version of that test asserted the other thread finished promptly, and
hung for ten seconds — the test was wrong, not the code.)

### Measured — the reranker swap costs nothing on the built-in corpus

Both models, full pipeline, recall at 1 over 20 sentences:

| | bge-reranker-base | MiniLM-L-6 |
|---|---|---|
| overall | 80% | **80%** |
| topic only | 88% | **88%** |
| with a constraint | 75% | **75%** |
| misses | 4 | **the same 4** |
| wall clock, 20 queries | ~17s | **~4s** |

Identical ordering at rank 1, 9.2× faster per search. The wall-clock difference
is the evidence that both reranked rather than both being no-ops — a trap worth
naming, because identical numbers meant "nothing was measured" twice already in
this work.

**What this does not prove.** Recall at 1 over 20 questions on a 21-document
corpus cannot see reordering below the top result, and the note printed under
every run is right that these numbers are optimistic. `--k 3` is the sharper
instrument if the question comes back.

**The four misses are identical between models, so they are not ranking
failures** — a reranker can only reorder what retrieval already found. Three of
the four want a document about a *licence*, and `attachment` sits at 0% and
`sender` at 50%. That is a retrieval question, and the next one worth asking.


### Changed — the reranker, on a measurement

Reranking was **8,338ms of a 8,999ms search** — 93% of it, against a spec budget
of 300ms warm. `leasha rerank-bench` on the owner's machine, 30 candidates
windowed to 600 characters, median of three passes:

| model | size | per search | per passage |
|---|---|---|---|
| **Xenova/ms-marco-MiniLM-L-6-v2** | 0.08 GB | **0.66s** | 22ms |
| jinaai/jina-reranker-v1-tiny-en | 0.13 GB | 0.81s | 27ms |
| Xenova/ms-marco-MiniLM-L-12-v2 | 0.12 GB | 1.98s | 66ms |
| BAAI/bge-reranker-base *(was the default)* | 1.04 GB | 6.08s | 203ms |

**MiniLM-L-6 is 9.2× faster than what was shipping**, and the default is now it.
Two separate things got us there: passage windowing took bge from 8.34s in the
real search to 6.08s here — about 27% — and the model change took the rest.

`bge-reranker-base` is the better *ranker*; that is what the extra 960MB buys.
But an ordering nobody waits for is worth nothing, and six seconds is nobody.
`RERANK_MODEL` in `.env` puts it back, and `leasha evaluate` measures what the
ordering actually costs on one particular corpus — speed was only half the
question and the other half belongs to whoever owns the documents.

The bench flagged `bge-reranker-base` **UNSTABLE**: its three passes varied by
more than 25%. That warning exists because a conclusion was drawn from a noisy
single pass earlier in this project and was wrong.


### Fixed — two faults found by using the Settings page

Both reported in one sentence each, and both invisible to every test in the
suite until they were written afterwards.

*"Scrolling moves from the main page into the list without clicking on it."* A
scrollable table inside a scrollable page takes the wheel from whatever is under
the pointer, so scrolling down Settings stopped dead the moment the pointer
crossed the file-types table — and once it was there, the page could not be
scrolled past it at all. The same rule the spin boxes already use now applies to
the table: **the wheel works when the control has focus, and goes to the page
when it does not.** Click the table and it scrolls; scroll past it and the page
keeps moving.

*"When you double click the file it does not open the edit box."* It did nothing
whatsoever, which on a table of settings is worse than either alternative — it is
the first thing anybody tries. Double-click now opens the type's reader, size cap
and on/off state, and the dialog says what a large cap costs at the moment the
number is raised rather than leaving it to be discovered mid-run.

### Added — the Add file type wizard generates the reader for you

Adding a format meant knowing which of three mechanisms applied, then editing
TOML or writing a module and remembering four separate places to touch. The
knowledge was written down, which is the kind of thing somebody reads once and
then re-derives from memory, badly. The step people forget is the import in
`app/extract/__init__.py`, and its failure mode is the worst kind: the module is
perfect, the extension reports unsupported, and nothing connects the two.

The wizard asks the one question that decides the tier — can something we
already have read this? — and then does as much of the rest as is safe. Tier 1
writes the route. Tier 2 writes the converter block and **refuses a binary that
is not installed**, rather than saving a route that fails on every file until
somebody notices. Tier 3 generates the module, inserts the import in
alphabetical order, pins the package, and installs it.

**It cannot finish Tier 3 and says so** instead of producing something that
looks complete and raises `NotImplementedError` on the first file. What it
generates is the contract — lazy import, declared `requires`, errors as values,
a name-only fallback so an unreadable file is still findable — with one marked
`TODO` where the parsing goes.

`app/core/scaffold.py` holds the generation, and the tests worth having are the
ones proving what it refuses: a name already taken, an extension another reader
claims, a package named with no import name (`python-docx` imports as `docx`),
and applying a plan whose file appeared while the plan was on screen. Nothing is
ever overwritten, and a frozen build refuses outright rather than writing into a
temporary directory nobody will look at.

The pip install runs on a worker. It was written inline first and the
`test_ui_never_blocks` guard caught it in the same run — non-negotiable #5 held
by a test rather than by memory.

### Added — Select all / Select none in the file-types list

**They act on the rows currently shown, not on the whole table.** Filter to
`ocr` and "select none" switches off the eight image types, not the eighty
formats behind the filter. A button that silently acts on rows somebody cannot
see turns one decision into eighty, and they find out at the next index run.

The status line names the filter whenever the count is smaller than the table,
because a number that does not match what is on screen otherwise reads as the
button having half worked.

### Fixed — the File types page omitted most file types

`describe()` listed only extensions that appear in configuration, and
configuration only records what differs from an extractor's own defaults. So
`.pdf`, `.docx` and `.txt` — claimed in code, in no TOML file anywhere — had no
row. **A settings page that quietly omits the settings people came for**: there
was no way to stop indexing PDFs from the UI at all.

The table is built from the registry as well as the rules now. `with_override`
raised `KeyError` for exactly those extensions, and creates a rule instead.

`extractor` also became optional in an `[extensions]` entry. Turning off `.pdf`
should not require restating that `pdf` reads it — that fact lives in the
registry, and copying it into every user's config file means a rename in code
strands them all. An entry with no reader named adjusts whatever already claims
the extension, and is inert if nothing does.

### Added — AutoCAD drawings

`.dxf` is read natively by `app/extract/cad.py`: notes and callouts, **block
attributes**, dimension overrides, and layer and block names, with each layout a
segment of its own so a hit can say which sheet it came from.

Block attributes are the reason this is worth doing. Drawing number, title,
revision and drawn-by are almost never loose text — they are attributes on the
title block's `INSERT`, and an extractor that read only `TEXT` and `MTEXT` would
miss the single field people search drawings by.

**`.dwg` is deliberately not claimed by the extractor.** It has no open
specification and no Python reader — `ezdxf` explicitly does not read it — so it
goes through a Tier 2 converter (`dwg2dxf`) to DXF and back to this reader.
Registering `.dwg` here would have looked more complete and silently disabled the
only thing that can read it, because `extract()` reaches a converter only when
the registry has no answer. The converter ships disabled like every other, so
DWG files are found by name until LibreDWG is installed.

A drawing that cannot be parsed is still indexed by its file name, with a warning
saying why the contents are missing. An empty document would mean
`ERR_NO_TEXT_LAYER` and the drawing would vanish from the index entirely, which
for a file somebody searches for by number is the worst available outcome.

### Added — every file type now says whether it actually works

`app/core/format_health.py` answers the question the routing table never did:
not "what is this extension routed to" but **"will it read my files?"**

The gap was real and silent. `.doc` could be switched on, correctly routed to a
converter, and fail on every single file because LibreOffice was not installed —
once per file, three hours into a run, with nothing on screen distinguishing it
from a format that works. The person sees an empty result set and concludes the
search is bad.

Extractors declare their optional dependencies with a `requires` attribute, and
one function turns that plus the converter binaries into a state per extension:
ready, limited, cannot read, or off. **`doctor` and Settings read the same
function**, so they cannot disagree — a format reported healthy in one and broken
in the other is a support call nobody can answer.

This replaced three hand-written probes in `doctor.py` (OCR, Visio/Project,
converters), each of which had to be remembered separately. A format added later
now appears in both places without either being edited.

Requirements are per-extension where it matters. `visio` covers `.vsdx` (helped
by `vsdx`) and `.vsd` (helped by `olefile`), and neither package does anything
for the other extension — so before this, a `.vsd` row recommended installing
`vsdx`, a fix that changes nothing. A column full of fixes that change nothing is
a column nobody reads.

### Changed — the file-types editor manages formats rather than listing them

A **Status** column carrying the exact fix command on the row, not buried in a
tooltip: a tooltip is not discoverable, and this is the one thing the person has
to act on. Right-click copies it.

Also a filter box and an "only show what needs attention" toggle — sixty-odd
formats is a scroll, and the one being looked for is never the one on screen —
plus **Add file type…** for Tier 1 routes, right-click removal of types added
here, and **Reset to defaults**.

Reset **deletes the override file** rather than writing the current state back as
"everything on". Writing a snapshot would pin today's defaults forever: a format
added in a later release would arrive switched off and a limit improved upstream
would never reach the machine. Deleting is the only action that keeps meaning
"as shipped" after an upgrade.

### Fixed — a broad filter could return no semantic results at all

When more than `MAX_PREFILTER_IDS` files matched a filter, the prefilter was
skipped and the search asked for the global top 100, then discarded the rows that
failed the filter. Whatever survived was not the top 100 PDFs; it was the PDFs
that happened to be in the global top 100 — on a large mixed index, frequently
none of them. `type:pdf` could return nothing while thousands of PDFs matched.

The comment called this "correct but weaker". It was not weaker, it was **wrong**:
a filtered top-k query that silently returns fewer than k eligible rows when more
exist has changed its meaning, not its quality.

It now over-fetches (4×, then 16×), post-filters, and escalates to the full
pushdown if the ladder runs out. Fast in the common case — a filter big enough to
skip the prefilter usually matches most of the corpus, so the first rung fills the
page in one query — and never wrong in the rare one.

### Fixed — diagnostic bundles shipped the raw `.env`

`build_bundle` wrote the whole environment file into `env.txt`, and the CLI tells
people to send the zip for troubleshooting. Today it holds paths and local
endpoints, so nothing has leaked. That is not the point: the workflow says "send
this file to somebody", and the day a token is added to `.env` it would go with
it, silently, having never been a decision anybody made.

Values are now allowlisted — key names always survive, because knowing which
settings exist is most of the diagnostic value — and anything else is redacted.
Comments are dropped rather than shipped: a comment is exactly where somebody
parks an old credential "just for now". Keys containing TOKEN, SECRET, PASSWORD,
API_KEY and friends are redacted even if allowlisted by mistake, so the failure
mode of a future edit is a missing value rather than a leaked one.

### Changed — the JVM test no longer decides whether the suite reports at all

`startJVM()` can take the **host process** down with a Windows access violation
rather than raising, depending on the installed Java and JPype. When it did, the
run ended with no trustworthy pass/fail summary — every other test's result lost
to one optional integration.

Both JVM-starting tests are marked `jvm` and excluded from the default run
(`-m "not jvm"`). Run them deliberately with `pytest -m jvm`.

### Added — regression tests for two guards that were correct only by convention

An external code review reported three release blockers. **Two did not survive
reading the code.** The low-disk stop was said to leave `_interrupted` unset and
let the prune delete rows for files the walk never reached; it sets it, via
`request_stop()`, and the prune is guarded on exactly that. The Qt menu callbacks
were said to raise `TypeError` because `triggered` emits a boolean; PyQt truncates
signal arguments to the callable's arity, so a zero-argument lambda is fine.

Both findings were wrong and both suggested tests were worth writing anyway. The
prune guard is a three-line chain across three methods that a refactor could break
in silence, and the menu now has tests that trigger every action through real
signal dispatch rather than merely constructing it.

The lesson is the process one: **no finding enters the fix queue until it is
reproduced** by a failing test or a manual trigger. A review that reads fluently
and cites plausible line numbers can still be describing code that does not exist.

### Fixed — the model invented a file type, and I had removed the list

`a petrorabigh schedule project file` became `type:project schedule from:petro
project`. `project` is not a file extension; the parser accepted it as one,
`ext=('project',)` matched nothing, and the query looked entirely deliberate
while returning nonsense.

**My regression.** Shortening the prompt two commits ago, I dropped every value
hint — including the list of valid `type:` values. The model had no way to know
what a file type is, so it guessed. `type:` and `has:` take one of a fixed set
and are now spelled out in full; the open-ended operators still are not, because
listing examples for `from:` costs tokens and teaches nothing.

**And the validator checked half of what it should.** `_rejects` verified that
every operator *name* exists and never looked at the values. It now rejects a
`type:` that resolves to an extension nothing could have, built from the parser's
own groups and the extractor registry so a new format is accepted without a
second list to keep in step.

### Fixed — opening a result was slow because it ran on the UI thread

`explorer /select,` takes a few hundred milliseconds just to start, and the
`exists()` check before it is a stat that can block for seconds on a network
share or a sleeping drive. Both ran inline, so the window froze through a launch
that is nearly free once it is off the critical path. The click returns
immediately now.

**The guard test from the last commit should have caught this and did not** — it
banned `subprocess.run` and never named `Popen` or `os.startfile`. A guard that
lists only the obvious blocking call has a gap the shape of the next bug. All
four are named now.

### Fixed — indexing had its own worker but not its own thread

It used `QThreadPool.globalInstance()`, the pool every search, filename lookup,
mail filter and environment check also uses — roughly one thread per core. An
index run holds a slot for *hours*, so on a four-core machine a quarter of the
interactive capacity was gone for the duration and a burst of typing could queue
behind it.

The work was always off the UI thread. It was competing with the work that has
somebody waiting on it, which is the difference between running in the
background and running in the background *and you can tell*. Indexing now has a
dedicated single-thread pool and can never starve a keystroke.


### Fixed — Tab now picks a `/` command

`QCompleter`'s popup handles Enter and Return and nothing else, so Tab fell
through to the search box and moved focus to the next control: the list vanished
and you were somewhere else entirely. Reported as *"you use a keyboard and press
tab, it does not select — it needs to be clicked by mouse"*, which in a project
whose spec requires keyboard-only operation end to end is a plain failure rather
than a rough edge. Tab is the completion key in every shell, editor and IDE; it
now picks the highlighted row, or the first one if nothing is highlighted yet.

### Changed — result rows are painted, not built

`QListWidget` with `setItemWidget` gave every row three live `QLabel`s: five
hundred results was fifteen hundred widgets, each with a layout, a palette and
event handling, all constructed before the first was visible. It is now a
`QListView` over a plain model with a `QStyledItemDelegate` painting only the
dozen rows on screen, so the cost stops scaling with the result count. Expanded
chunks are additional model rows rather than nested widgets, and scrolling is
per-pixel because rows have different heights.

The delegate measures in `sizeHint` and draws in `paint`, and those two
disagreeing clips text at the bottom of every row — so the geometry has exactly
one source, `Metrics`, which lives in `view_options.py` because it is pure
numbers and belongs where it can be tested without a display. The text decisions
(`why`, `kind_tag`, `group_subtitle`, `result_tooltip`) moved to `presenter.py`
for the same reason.

### Added — the application icon, and an optional system tray

`leasha.ico` on the window and taskbar, `leasha-tray.ico` in the tray: below
48px the navy ellipse becomes an indistinct dark mass that swamps the three
shapes, so the small variant is the blobs alone. The icon is looked for beside a
frozen executable as well as beside `app/`, because getting that wrong means it
works from source and vanishes when packaged — exactly when nobody is testing.

**Minimise-to-tray and close-to-tray are both off until asked for.** An
application that vanishes from the taskbar unbidden is alarming. **Quit from the
tray is a real quit**, through the window's normal close path: a tray icon that
leaves a process holding the index lock produces `ERR_DB_LOCKED` on the *next*
launch with nothing on screen to blame. Where no tray exists the preferences are
turned off with a warning rather than silently doing nothing.

### Added — a standing rule that background work never freezes the window

`docs/TROUBLESHOOTING.md` told the owner to **wait ten seconds** when the window
went white, and named building the graph and the environment check as legitimate
causes. That was documenting a defect as expected behaviour, which is how a bug
becomes a feature nobody fixes. The entry now says a frozen window is a bug and
asks for a report.

A guard test enforces it: no `processEvents` outside the shutdown wait (where
there is no event loop to return to), no `subprocess.run` or `time.sleep` on the
interface thread, no `waitForDone` outside shutdown, `presenter.py` still free of
Qt — and for each long operation, an assertion that it starts a worker rather
than running inline.


### Changed — results are grouped by document, and the row leads with the name

The work order's steps 1-6. **One row per document, not one per matching
chunk.** A long PDF matching in five places took five of the top ten rows, so
the person saw three documents where they should have seen ten. Grouping lives
in `presenter.py` and never in the engine — the §3b measurement was taken
against chunk-level ranking, and grouping inside the engine would change what
"rank 1" means and make every future measurement incomparable with that one,
silently. `SearchEngine` returns exactly what it returned before.

A group is ranked by its **best** chunk, never the mean: averaging punishes a
long document that matches strongly in one place, which is the common case in an
archive and precisely the document being looked for. The fetch is four times the
display count, because grouping shrinks the list.

The row now reads like a browser result: **name first**, a `Archive > 2019 >
Leeds` breadcrumb small underneath, date right-aligned, a short kind tag. The old
layout led with a path elided in the *middle* — which is exactly where the
distinguishing part of a long archive path lives. A multi-match row says
"3 matches" and expands to the individual passages, each openable at its page.

`explain` and the score **moved, they were not deleted**: tooltip, a "Why this
result?" menu item, and inline for anyone who ticks the box. Being able to ask is
where trust comes from; it just does not need to be the second thing the eye
lands on, forever.

`ext` and `mtime_ns` now reach the UI. Both retrievers had been selecting them
since Layer 4 and `_to_result` dropped them on the floor — so the date, which in
a fifteen-year archive is frequently the *only* thing telling two results apart,
cost one line and no new query. `format_when(0)` returned "01 Jan 1970"; that is
not a fallback but a false claim, and it now returns nothing.

Mail metadata for a page of results is **one query**, not one per row — at the
fetch depth grouping needs, a per-row lookup is fifty queries per keystroke.

### Fixed — Search and Files no longer blank while you type

Reported as *"the mail tab searches as you type, this is good; the other two are
not the same"*. Mail feels better for one reason: **it never blanks.** Files
wiped its table on the way *to* a query — typing `in` en route to `invoice`
emptied the screen and refilled it — and Search cleared the pane whenever an
interim pass momentarily found nothing. Both now keep what is on screen until
something replaces it. Only an empty box clears.

The scroll position also survives a redraw, so expanding a row or changing a
preference no longer throws you back to the top.


### Fixed — a prompt with no examples, and a correction to yesterday's diagnosis

`qwen2.5:1.5b` answered in 5.1 seconds and handed the sentence back completely
unchanged. `emails from chris about buying a licence` should become
`from:chris licence`; it returned the sentence verbatim.

The prompt was rules with no examples. A 1.5B model pattern-matches far better
than it reasons, and rules-only prompts are precisely where small models echo.
**The prompt now carries five worked examples** — a sender, a date range, a file
type, an exclusion, and one sentence with no constraints at all, so the model
learns that bare words are a correct answer rather than a failure to find an
operator.

They were paid for, not added on top: the prompt went from 1,901 to 1,486
characters. The operator *value hints* are for the dropdown, where a person
needs to remember what a value looks like; the model never did, and they were
1,400 of those characters charged on every translation.

A test asserts every example parses cleanly, that every operator is still
named, and that no example is the sentence the Test button uses — a model handed
the answer in its own prompt copies it, and the test would then pass for a model
that cannot do the job.

**And a correction.** Capping generation at 64 tokens was described here as the
root cause of mistral's thirty-second answer. It was not: with the cap in place
mistral still timed out at thirty seconds, so its cost is prompt *evaluation*,
not generation. The cap is still right — it bounds the worst case and stops the
app paying for text `clean_output` discards — but it was not the fix it was
claimed to be. The shorter prompt is the change that addresses mistral.

**"Nothing to interpret" was a false claim.** It is a statement about the
*sentence*, and it was made about one that plainly says "from chris". When the
model returns the sentence unchanged the message now names the model and says a
different one may do better — because the fix is a different model, not
different wording.


### Added — choose the Interpret model from what Ollama actually has

Typing a model name into `.env` and hoping is not a choice, it is a guess, and
the guess was wrong on the owner's machine in three separate ways. Settings now
has a **Model** dropdown listing what `ollama list` reports, each row saying what
that model will cost — `qwen2.5:1.5b — small and fast, ample for rewriting a
query` against `gpt-oss:20b — large, expect to wait`. A **Test** button runs one
real interpretation and reports the seconds, because the choice is a speed
decision and a speed decision needs a measurement.

Choosing a model sets a budget that fits it, and both apply immediately rather
than on restart. Saved in `index_state`, not written back into `.env`. Embedding
models are listed but greyed out with the reason — hiding `nomic-embed-text`
invites hunting for a model `ollama list` plainly shows.

The probe runs off the UI thread, and **a failed probe never changes the
configured model** — an empty list means Ollama was not answering at that
moment, not that the choice was wrong.

`app.cli ollama` gained `--model NAME` to try one before committing to it.

### Fixed — nothing capped how much the model generated

`num_predict` is unlimited for Ollama's `/api/generate`, so a model asked for a
one-line query was free to write paragraphs explaining itself — and
`clean_output` discarded everything after the first line. **The wait was for
text that was thrown away.** That is why mistral took over thirty seconds to
produce roughly ten tokens of useful output.

Generation is now capped at 64 tokens and stops at the first newline. The prompt
always asked for one line; now something enforces it.


### Fixed — four UI bugs that were doing real damage

**Scrolling the Settings page changed the settings.** Qt lets a `QComboBox` or
`QSpinBox` take a wheel event whether or not it has focus, so scrolling down the
page altered every control the pointer crossed. On a trackpad this is close to
guaranteed. The controls in question set the memory ceiling, the CPU limit, the
worker count and the index schedule — so scrolling past could hand a run four
workers and a 500MB ceiling with nothing announcing it. That is data loss, not
clunkiness. `app/ui/widgets/no_scroll.py` now guards every scroll-sensitive
control in the window in one call at the window level, so a new page gets it for
free. The wheel still works on a control you have clicked into.

**The up/down buttons were unresponsive because each notch wrote to disk.**
Every `valueChanged` persisted five settings keys, each in its own SQLite
transaction, on the UI thread. Spin-box auto-repeat is about ten a second, so
holding an arrow asked the window for fifty committed transactions a second and
it stopped repainting between them. The buttons were working perfectly. Two
fixes: `app/ui/widgets/debounce.py` coalesces the burst into one write once the
changes stop, and `SqliteStore.set_states` does the five keys in one
transaction. `setKeyboardTracking(False)` stops typing `1500` emitting four
times. `flush_pending()` on close means a change made in the last third of a
second is still saved.

**Right-click did nothing, or acted on the wrong row.**
`customContextMenuRequested` delivers a point relative to the *widget*;
`itemAt`, `rowAt` and `indexAt` all want the *viewport*, and the header sits
between them. The lookup was consistently one row low — right-clicking the first
row acted on the second, and right-clicking the last row found no row and
returned silently. One `viewport_point()` helper, called from every list. In the
search results there was a second cause: each row is a real widget over the list
item, and it was entitled to swallow the event. It now declares `NoContextMenu`
so the event reaches the list.

**The progress bar left out the files being indexed.** The numerator was
`unchanged + skipped`. A first index of a fresh corpus has nothing unchanged and
little skipped, so the bar sat near zero for hours while the log showed thousands
of files done — reporting the opposite of the truth on the run where it matters
most. Nothing could have caught it: a bar that moves too slowly still moves.
`presenter.progress_for` now counts everything the walker has finished with, and
is tested.

### Added — a Mail tab, and control over what the lists show

**Mail is now its own tab, as a sortable table.** From, To, Date, Subject,
Attach and Size, newest first, filtered by the same `/` commands as everywhere
else — `/from dave`, `/to priya`, `/subject invoice`, `/has attachment`,
`/after 2024-01-01`. It reads the `messages` table directly and never touches
chunk text: a mailbox is scanned in columns and read newest first, and ranking
one by relevance puts an eight-year-old thread above this morning's. It says so
when free text is typed, rather than returning an empty table. `Ctrl+M`.

**Columns, row density and text size are configurable per list.** A "View"
button on Files and Mail, and on the search results (text size and spacing only —
a result is not a table). Columns are offered *only when the data can fill
them*, because a column of blanks takes width from the ones that matter and
reads as a broken index; a column comes back on its own when the data does.
Compact and Normal row heights are derived from the font's own metrics, so
turning the text up does not clip descenders. Saved per list in `index_state`.

**`/` commands in Files now actually filter.** The dropdown was there and
`/type pdf` was inserted as `type:pdf` — and then handed to a trigram index as a
literal string, matching nothing. It is parsed now, and `/type pdf` on its own
is a complete request rather than being refused for want of two characters of
name.

### Changed

- The Pause button on the Indexing page now says **Stop**, because that is what
  it does: there is no resume, and the next Start begins a new run. A button
  that promises to pause and then stops is one people stop trusting. It keeps
  reporting progress while it winds down instead of looking frozen.
- `app.cli ollama` gained `--translate SENTENCE`, which runs one real
  translation end to end. Everything else the command checks proves Ollama is
  alive; none of it proved the one thing the app asks of it. Its wording also
  said Ollama types knowledge-graph entities — the graph was removed two
  releases ago, and a stale diagnostic sends people to the wrong place with
  confidence.
- A failed index run resets the progress bar rather than leaving it at 40%.

### Fixed — a benchmark that could not fail, and a lever that was not one

Two corrections, both from running the tools on the real machine.

**`evaluate --builtin` reported 100% on every category.** The default was recall
at ten, on a twenty-one document corpus — half of everything. A benchmark that
cannot fail is worse than no benchmark, because it gets believed and then
quoted. The default is now **rank 1**: did the right document come *first*?
Same corpus, same code, honest answer — 70% overall, 88% on topics, 58% on
constraints. A generous `--k` now prints a line saying it is flattering the
result.

**Chunk size is worth about 10-15%, not the 54% claimed here.** Four runs now
exist:

| tokens/sec | 128 | 256 | 512 | 256 vs 512 |
|---|---|---|---|---|
| run 1 (1 pass) | 2,235 | 2,304 | 2,263 | 1.02× |
| run 2 (1 pass) | 2,204 | 2,063 | 1,265 | 1.63× |
| run 3 (median) | 2,190 | 1,802 | 1,172 | 1.54× |
| **run 4 (median)** | **1,948** | **2,017** | **1,772** | **1.14×** |

Run 4 is the trustworthy one — its 512 measurement ranged 3.43–3.50, a 2%
spread. Runs 2 and 3 had a depressed 512 figure, exactly what the spread warning
exists to flag, and the "chunk size IS a lever" conclusion was drawn from run 3
and stated as fact.

**The very first measurement said 1.02× and was closest to the truth.** It was
dismissed because two noisier runs disagreed with it — which is the entire
argument for reporting spread rather than a single number, made against the tool
by its own output.

That leaves **int8 as the only remaining lever worth real money** (~2× on a fp16
model), and chunk size as not worth a re-index.


### Fixed — every document told you to type a command PowerShell refuses to run

```
leasha : The term 'leasha' is not recognized as the name of a cmdlet...
```

PowerShell does not run commands from the current directory — a deliberate
protection against a malicious `ls.exe` left in a folder you happen to be
standing in. So `leasha` fails where `.\leasha` works, and **every example in
every document said `leasha`**. A documentation bug, which is the kind nothing
catches.

- Every copy-pasteable line now reads `.\leasha`.
- `.\add-to-path.ps1` puts the folder on your **user** PATH so the `.\` can be
  dropped — no administrator rights, nothing changed for anybody else on the
  machine, and `-Remove` undoes it. Safe to run twice.
- The installer ends by showing the command that actually works, and offering
  the PATH step.
- `docs/TROUBLESHOOTING.md` answers the error using the words the error itself
  uses, because somebody hitting this will paste it in looking for it.
- A test fails if any document goes back to bare `leasha`. It caught the one
  legitimate exception on its first run — the troubleshooting guide quotes the
  failing message verbatim, and has to.


### Added — the file-types editor, and the work order is complete

Step 6, the last of six. Settings now lists every file type with a checkbox,
what reads it, and its size limit. Anything switched off is never opened at all.

**Only `enabled` is editable, deliberately.** Size caps and extractor routing
are per-format decisions with real consequences — an OCR cap raised to 100MB is
minutes of work per image — and they belong in `config\extractors.toml` beside
the comments explaining each one. A checkbox that could silently make indexing
twenty times slower is not a kindness.

**Changes are stored as differences, never as a snapshot**, and this reversed an
earlier decision. A user entry originally *replaced* the packaged rule outright,
on the reasoning that half a rule from each file matches neither and cannot be
reasoned about. The editor showed that wrong in the case that matters most:
turning `.png` off is one key, and writing the whole rule to say so would pin
`extractor` and `max_bytes` at today's values — so a later release improving
either would have the improvement silently discarded. A user entry now **patches**
the packaged rule; a brand-new extension must still be complete, because there
is nothing to patch.

The panel also says what it does *not* do: a format switched on does not
retrospectively index the files already skipped, and somebody not told that
concludes the setting did not work.


### Added — OCR for images and scanned PDFs, on by default and honest about it

Step 5 of the file-types work order, and the owner's explicit override of "OCR
is out of scope for V2", taken knowing it can dominate an index run.

**That is a decision somebody is entitled to make about their own machine — but
it is only a real decision if the cost is visible**, so it was measured rather
than estimated:

| | |
|---|---|
| A full page of text | **3.6 seconds** |
| Roughly | **8× the cost of embedding one passage** |
| 1,000 scanned pages | 1 hour |
| 10,000 | 10 hours |
| 100,000 | 100 hours |

Every document records `ocr_seconds` and `ocr_confidence`, so slow indexing is
attributable rather than mysterious — "indexing got slow" with no attribution is
a complaint nobody can act on. To switch it off, set `enabled = false` on the
image lines in your own `<DATA_PATH>\extractors.toml`.

**RapidOCR rather than Tesseract**: the models ship inside the wheel, so there
is no separate binary and no `TESSDATA_PREFIX` for an installer to get wrong on
a machine nobody can log into. Tesseract stays reachable as a Tier 2 converter
for anybody who prefers it.

Two things it declines to spend time on. Images below 64×64 are icons, bullets
and spacers — a document-heavy corpus holds thousands, each costing a model call
to yield nothing. And a page that already has a text layer is never OCR'd:
running it over a searchable PDF costs seconds to produce a worse copy of text
already extracted.

The engine is a seam, so every path — missing package, engine failure, low
confidence, an unexpected return shape — is tested with a fake on a machine
where OCR is not installed. One of those tests caught a real crash: RapidOCR's
return shape has changed between versions, and a future one returning a bare
number would have passed the truthiness check and then failed on iteration,
taking an index worker with it.


### Added — Tier 2 converters: a dozen dead formats, one implementation

Step 4 of the file-types work order. LibreOffice alone now covers `.doc`,
`.xls`, `.ppt`, `.rtf`, `.pages`, `.numbers`, `.key`, `.wpd` and `.pub`; pandoc
covers `.epub` and `.fb2`. Adding a format is a line in a text file rather than
a parser.

That leverage also makes this the most dangerous module in the application,
because it runs programs. Every decision in it narrows what that can mean, and
each is asserted by a test rather than trusted:

- **The allow-list is in code, not configuration.** `soffice`, `libreoffice`,
  `pandoc`, `xstexporter`, `tesseract` — nothing else, ever. Config chooses
  among allowed converters; it cannot introduce one. `extractors.toml` is a file
  a person edits, and on a shared or synced machine it is a file *someone else*
  might edit; a configuration format that can name any executable is a way to
  run anything. Anything off the list is `ERR_CONVERTER_BLOCKED`, refused
  **before** it is even resolved.
- **The command never reaches a shell.** `subprocess.run(list, shell=False)`,
  always. Filenames come from the corpus being indexed — precisely the input not
  to trust — and a shell would interpret `;`, `&&`, `|` and backticks in one. A
  test converts a file literally named `report; rm -rf ~.doc` to prove it stays
  a single argument.
- **The absolute path invoked is resolved and logged**, every run. `soffice` on
  `PATH` is whatever `PATH` says today.
- **Every temporary directory is removed in a `finally`.** A 100GB run leaking
  one per converted file fills the disk, and the failure then appears somewhere
  else entirely.
- **The timeout is capped at five minutes** regardless of what config asks for.

A converted document is repointed at the **original** file, because a result
linking to `/tmp/leasha-convert-xyz/report.txt` is worse than no result: it
looks like an answer and cannot be opened.

Converters still ship disabled — the binary may not be installed, and a format
that fails on every file is worse than one that says plainly it is off. An
unconfigured `.doc` therefore stays `ERR_UNSUPPORTED_TYPE` ("this app does not
do that") rather than becoming `ERR_CONVERTER_MISSING` ("something is broken")
on every file in the corpus. `doctor` reports which binaries were found so
Settings can offer exactly those.


### Added — Google Workspace pointers are findable instead of invisible

Step 3 of the file-types work order. A `.gdoc` is not a document: it is a few
hundred bytes of JSON holding a URL, and Drive for Desktop leaves thousands of
them in a synced folder.

**The reason to index them is the failure they otherwise cause.** Unsupported,
they are skipped and invisible — somebody searches for a document they know
exists, finds nothing, and concludes the search is broken. Indexed, the pointer
is findable by name and says plainly *"the text of this document is not stored
on this machine"*, which turns a mystery into an answer.

**It never fetches the URL.** Not once, not optionally, not behind a flag. A
fetch would be an authenticated request telling Google what is being indexed and
when, from an application whose whole proposition is that nothing leaves the
machine — and it would look like a small convenience while doing it. Two tests
assert it: no network module is importable from that file, and no call in it
resembles a fetch.

Both of Drive's historic JSON formats are read, because old files keep the old
shape, and the file id is recovered from the URL when Drive did not write it as
a field — it is stable across renames and is what somebody pastes to find the
file again.


### Added — OpenDocument (.odt, .ods, .odp), with no new dependency

Step 2 of the file-types work order. An ODF file is a ZIP holding
`content.xml`, and both halves are in the standard library.

**`odfpy` was the obvious choice and is the wrong one.** It publishes no wheel,
so pip builds it from source, which needs a compiler present on every machine
that installs this. A dependency that can fail at install time, on Windows, for
a format most corpora hold a handful of, is a poor trade against a hundred lines.

Two bugs found by running it against documents rather than reasoning about it:

- **Inline formatting scrambled sentences.** ODF marks emphasis with nested
  elements, so `<p>Findings from the <span>annual</span> inspection.</p>` holds
  three fragments — the paragraph's text, the span's, and the span's *tail*.
  Ending the paragraph at the first fragment produced "Findings from the /
  inspection. annual". Fixed by walking with enter and exit events rather than
  text alone.
- **Spreadsheets lost their rows.** A `<text:p>` inside a cell closed before the
  cell did, so every cell landed on its own line and "Licence" ended up two
  lines from "12400". That adjacency is the entire reason a spreadsheet is worth
  indexing: a number beside a label is a fact; a number alone is noise.

Namespaces are matched on local name, not URI — ODF's has changed between
versions, and another office suite writing its own would otherwise yield
nothing at all, silently. Comments and tracked changes are left out: an aside is
not the document, and indexing it puts words in a file its author never wrote
there.

`ERR_FILE_TOO_LARGE` joins the registry, checked from the archive header — the
point of a size limit is not to allocate the gigabyte in order to discover it is
a gigabyte.


### Fixed — the Indexing page was blank, and said nothing about why

`refresh_totals` ended in `except: return`. A store read that failed left an
empty label with nothing to explain it — and a blank page is the worst possible
answer to "is my index working", because it is indistinguishable from an empty
index, a broken one, and a bug in the page itself.

It is now a panel that **always produces rows, including for failure**:

| | |
|---|---|
| Documents | 355 indexed |
| Searchable passages | 3,355 |
| **Meaning-based search covers** | **5%** — 154 of 3,355 have a vector |
| Skipped | 12 — ERR_UNSUPPORTED_TYPE (12) |
| Index location | `D:\KnowledgeGraphData` · 1.1 GB |
| Last run | 2 hours ago |

The coverage line is the one that matters: it is the number that was invisible
for weeks while meaning-based search silently did a twentieth of its job. The
location line answers "is the index where I configured it", which previously
needed the command line.

Read in a worker, because it opens the vector store and measures a folder.

### Added — Reset index

Deletes everything indexed and starts over. The confirmation says plainly what
is *not* at risk: no document is touched, the index is derived from them, and
the only real cost is the time to rebuild. Saved folders, schedule and theme all
survive — a reset that forgot which folders to index would be one nobody could
recover from without setting the application up again. Cursors are cleared,
because they point at chunk ids that no longer exist, and the database is
vacuumed so the space actually comes back.

### Fixed — the Files tab could not do anything with a file

No right-click, no Enter, and double-click *revealed* in Explorer rather than
opening — everywhere else in Windows, double-clicking a file opens it. A list of
files you cannot act on is a list of disappointments.

One menu now serves both lists (`widgets/file_menu.py`): Open, Show in folder,
**Search inside this file**, Copy path, Copy file name. Every action is checked
before it is offered — a file that has moved is greyed out, and a "re-index this
folder" action appears in its place, because that is the thing that would
actually help.

"Search inside this file" is the bridge that was missing: found it by name, now
find what is in it.

### Fixed — a menu item that did nothing at all

The results menu offered "Add to document". Layer 7 was cancelled before it was
built and the menu item outlived it, emitting a signal nothing was connected to.
Clicking it did nothing, silently.

### Fixed — a Windows path on a non-Windows platform created a nonsense folder

A backslash is a legal filename character on Linux and macOS, so
`Path("D:\Data").mkdir(parents=True)` cheerfully creates a directory *named*
`D:\Data`. Running the CLI from a Linux sandbox against a Windows `.env`
littered the project root with folders called `D:\KnowledgeGraphData` and
`D:\KnowledgeGraphData\cache`. Nothing raised, nothing warned, and the index
appeared correctly configured while writing somewhere else entirely.

Configuration now refuses a Windows path on a platform that has no idea what it
means, with a sentence rather than a silent mkdir.


### Fixed — plain-English search returned nothing at all

Twenty sentences of the kind somebody actually types, against a corpus with
known answers. **Nineteen returned zero results.** Not badly ranked - not ranked
at all. The application's stated purpose is to let somebody "write in normal
text what I am looking for", and the keyword half was mathematically incapable
of it.

Two causes, both found by running the measurement rather than by any unit test:

- **Every term was ANDed, stopwords included.** "drawings of the pump station"
  became `drawings AND of AND the AND pump AND station`, and the document -
  "Pump station general arrangement drawings" - contains neither "of" nor "the".
  Stopwords are now dropped from the FTS expression only; they stay in the terms
  used for highlighting and in the text sent to the embedder, where "from Dave"
  and "for Dave" genuinely differ.
- **ANDing the remaining content words was still too strict.** People describe
  documents with words that are *about* them rather than *in* them - "email",
  "version", "deck". One such word excluded everything. Terms are now joined
  with OR and BM25 ranks by how much matched.

The OR threshold was chosen by measurement, not taste:

| terms before OR | empty results | recall@1 | recall@3 |
|---|---|---|---|
| 1 | **0** | **70%** | **95%** |
| 2 | 2 | 65% | 85% |
| 3 | 6 | 50% | 65% |
| 4 | 11 | 35% | 45% |

Three was the first guess. Precision is not lost: this is the retrieval stage,
BM25 ranks by how much matched, and fusion and reranking follow. Anybody wanting
a strict match has `"quoted phrases"` and an explicit `AND` - the latter now
honoured rather than overridden, which a test caught.

### Added — a measurement of whether search actually works

`app/search/evaluate.py` and a twenty-sentence corpus with known answers. It
reports recall **split by whether the sentence carried a constraint**, because
one number cannot distinguish "search is bad" from "search is fine at topics
and blind to constraints" - and those have completely different fixes.

The split is the finding, and it is the one the work order predicted:

| | plain sentence | with translation |
|---|---|---|
| overall @1 | 50% | **75%** |
| constrained @1 | 50% | **92%** |
| sender | 50% | 100% |
| type | 67% | 100% |
| recipient | 0% | 100% |
| attachment | 0% | 100% |

**This is what justifies Layer 8a.** A plain sentence cannot honour "from
Chris": the words go into the text search and the sender field is never
consulted. Translated to `from:chris`, it is a filter.

Two of the twenty questions were wrong when first written - one pointed at a
message the named person *sent* rather than received, another described a
five-month-old document as "over a year ago". Both scored zero for reasons that
had nothing to do with search. A test now asserts every question's answer
exists in the corpus.

**What it cannot tell you**, stated because a benchmark believed beyond its
evidence is worse than none: how well search works on the owner's real archive.
Twenty-one documents is a small, clean corpus with no near-duplicates and no
twelve years of drift. Every number here is optimistic, and the twenty real
sentences remain the measurement that matters.


### Added — the search box is now worth typing into

Every filter below existed or was one small change away; almost none of them
were reachable, because nothing in the application ever said they were there.

| Type | Does |
|---|---|
| `/type pdf` | only this kind of file |
| `/from dave` `/to priya` | email from / to this person |
| `/subject licence` | subject contains |
| `/has attachment` | with, or `no-attachment` without |
| `/after` `/before` | date range, including `last month` and `30d` |
| `/path leeds` | inside matching folders |
| `/name invoice` | files **called** this — a different question from `/path` |
| `/size >1mb` | above or below a size |
| `"exact phrase"` `-word` `A OR B` `NOT word` `word*` | operators |

`AND`, `OR` and `NOT` are recognised **only in capitals**. "salt and pepper" and
"one or two" are things people genuinely search for, and a boolean feature that
broke them would cost more than it delivers.

`/type pdf` is rewritten to `type:pdf` before parsing, so the parser never learns
about slashes: one grammar, one set of tests. An unrecognised `/word` is left
exactly as typed — `12/03`, `D:/Projects` and `/var/log` all survive, because
silently rewriting a query is how a search box loses trust.

One catalogue in `app/search/commands.py` feeds the `/` dropdown, `app.cli
commands`, and the grammar Layer 8a hands the model, with a test that fails if
any of the three drift from what the parser accepts.

### Fixed — three filters that looked present and did nothing

- **`_OPERATOR` carried its own hardcoded list of field names**, separate from
  the alias table. Adding `to:`, `subject:` and `has:` to the aliases therefore
  achieved nothing at all: the regex never matched them, the handler was
  unreachable, and the words became ordinary search terms. The pattern is built
  from the alias table now, and a test asserts every documented alias matches.
- **`from:` matched with `LOWER(sender) IN (...)`** — an exact comparison
  against the whole address, so `from:dave` never found `dave.smith@acme.com`.
  Nobody searches that way. Every mail field matches on any part now.
- **`upsert_file` stored whatever extension it was handed.** `files.ext` holds
  no leading dot and `type:pdf` compares against exactly that, so a caller
  passing `".pdf"` wrote a row that was indexed, searchable by text, and
  invisible to every filter, with nothing to explain it. The store normalises
  its own invariant now.

### Added — `app.cli embed-bench`, after an estimate was wrong

Embedding was measured at 1.53 passages/second and called "twenty to sixty times
too slow". That was wrong: the estimate assumed short sentences, and this
application embeds 512-token passages. The arithmetic for a 512-token
transformer on a CPU predicts 1.5/sec, so nothing was misconfigured.

**A throughput number without the sequence length beside it is not a number**,
and a projection built on the wrong one sends somebody optimising the wrong
thing for a week. So `embed-bench` measures rather than predicts: it reads the
ONNX weight dtypes to answer "is the model already quantised", reports which
execution providers onnxruntime can actually see, times real embedding at
several sequence lengths, and projects the corpus from the measured rate.


### Changed — the application is called **Leasha**

The name lived in nine files - window title, QApplication name, CLI banner,
argparse description, diagnostic header, a SQL comment, the package docstring
and two places in `doctor.py`. `app/core/branding.py` now holds it once, beside
`version.py` and for the same reason: **anything a person reads should have
exactly one definition.** A test fails if any module spells it out again.

`leasha.cmd` is the launcher: `leasha` opens the window, `leasha --debug`
records the session, and anything else (`leasha stats`, `leasha formats`) goes to
the command line tool. The Python package stays `app` - that is plumbing, not a
name anybody reads.

### Added — a debug recorder, so a bug report is evidence

`--debug`, or the switch in Settings, writes one JSONL file per session under
`logs\sessions\`: every tab change, button, search, error and timing, with a
millisecond timestamp. Three rules, and they are the design.

- **Off unless asked.** A tool that watches by default is one people stop
  trusting, and this application's whole promise is that nothing leaves the
  machine.
- **It can never cause a failure.** A recorder that raises would turn a small
  bug into a crash, inside the handler for the bug you were chasing.
- **Shape, never content.** A search is its length and its result counts, not
  the query. A file is an extension, not a name. A first version also kept the
  first 60 characters of long strings as a "head" - a content leak wearing a
  debugging hat, caught by its own test, and exactly what would have made
  session files unsafe to send. Sending them is the only thing they are for.

### Added — `app.cli ollama`, because "is it up" was the wrong question

Enrichment sat for 200 seconds and produced nothing. `/api/tags` answered - the
service was running - and the first `generate` then waited on a long read
timeout for a model that was not installed. `health()` and "this will work" are
different questions and were being conflated.

The command asks four separately: is anything listening, which models exist, is
the configured one among them, and does a trial completion actually return (in
at most 30 seconds - this is the command people run *because* something is
hanging). `EntityEnricher` now checks `has_model()` before opening anything, so
the same failure costs milliseconds.

### Added — meaning-based search says when it is not working

`vector.search` returns `[]` for an empty vector store, a failed embedding or a
LanceDB hiccup, and search carries on with keyword results. That is right - half
a search beats none - and it makes the failure **invisible**: results look thin,
and nothing distinguishes "the corpus is thin" from "the semantic half is dead".
The same shape as the sentinel bug that hid every PST.

So `SearchResponse` now carries `keyword_count` and `vector_count`, the status
bar says "keyword results only" when the second is zero, the engine logs it, and
`app.cli stats` compares the passage count against the vector count and says in
words what to run. `app.cli reembed` is that command: it rebuilds LanceDB from
SQLite without re-reading a single document, which is the entire point of one
store being the authority and the other being derived.

### Fixed — the guard against dead-object tracebacks never ran

`_emit` existed to swallow `RuntimeError` when sip has deleted a worker's
`WorkerSignals` at shutdown. It could not work: it took the bound signal as an
argument, so `self.signals.finished` was evaluated at the *call site*, before
`_emit` was entered, and the exception was raised while building the arguments -
outside the try/except written to catch it. The `except` clause then tried
`failed` and the `finally` tried `done`, each failing identically, so one dead
object produced three nested tracebacks: precisely what it was written to
prevent. It now takes the signal's *name* and looks it up inside the try.

### Fixed — closing the window raced its own background threads

A graph run was 200 seconds into waiting on Ollama when the window closed.
Closing tore down the QApplication and both stores while that thread was still
running and holding a cursor. `closeEvent` now asks every job to stop, then
drains the pool for up to four seconds while still pumping events - waiting
without pumping would freeze the window during the one operation nobody will
wait out.


### Added — file types are configuration, not code

`config/extractors.toml` now decides which extensions are indexed, what reads
them, and how large a file is worth opening. It is a **three-tier** model, and
the boundary is the design rather than a limitation:

| Tier | What config may say | Example |
|---|---|---|
| 1 | routing and policy | `.log` → plaintext |
| 2 | run an external converter, then read its output | `.doc` → LibreOffice → txt |
| 3 | nothing. This is code. | PDF, PST, OCR |

Config stops at tier 2 deliberately. A configuration format expressive enough to
describe parsing is a programming language with no debugger, no type checker and
no tests — strictly worse than the Python it set out to replace.

- **`app/core/formats.py`** loads, validates and merges two files: the packaged
  defaults (tracked in git, never written to) and `<DATA_PATH>\extractors.toml`
  (this machine's overrides). An upgrade therefore delivers new defaults without
  discarding anybody's choices, and deleting the user file restores shipped
  behaviour exactly.
- **Everything is checked at load.** An unknown key, an extractor name nothing
  provides, an uppercase extension, a `schema_version` from the future, a
  converter command written as a string — each is an `ERR_CONFIG_INVALID` naming
  the offending item, raised while the app is starting rather than three hours
  into a 100GB run on one file.
- **`app.cli formats`** prints what is indexed, what is off, and where the two
  files live. It is the answer to "why was that file not indexed?", which
  otherwise needs a debugger or a guess.
- A converter command must be a **list**, never a string. A string has to be
  split to be run, and the obvious way to split a command line is a shell.
- Every converter ships **disabled**: the binary may not be installed, and a
  format that fails on every file is worse than one that says plainly it is off.
- A **disabled** route is exempt from the extractor-name check, which is what
  lets config and code ship in separate releases. The check runs the instant the
  line is enabled.

### Fixed — the window froze and had to be killed from Task Manager

Reported as *"the program crashed when i was clicking around, the thread is
stuck, ctrl c does not work in powershell and i had to end task"*. Nothing had
crashed. Four separate pieces of blocking work were running on the UI thread,
and a frozen window is indistinguishable from a dead one.

- **"Run doctor" ran `subprocess.run(timeout=120)` on the UI thread.** Doctor
  probes Outlook over COM and opens LanceDB, so this is seconds at best and the
  timeout says two minutes is possible. For all of it the event loop was
  stopped. Now a `CallableWorker`, with the subprocess call and the text
  rendering moved into `presenter.py` where they are tested.
- **The Graph panel read the store while painting.** `refresh()` was
  `top_entities(500)` plus `edges_among` over 96,712 edges — on every switch to
  the Graph tab, and waiting on the SQLite lock whenever an index run held a
  write. Now read in a worker; the painting method is handed data.
- **Settings counted searches by fetching them.**
  `len(recent_searches(limit=100_000))` built a hundred thousand dictionaries to
  produce one number. `SqliteStore.count_searches()` is a `COUNT(*)`.
- **Clearing search history deleted on the UI thread**, freezing the window at
  the exact moment somebody had asked for something to be erased.

### Fixed — Ctrl+C did nothing, so Task Manager was the only way out

Python does not deliver signals from inside C code. Once `application.exec()` is
running the interpreter never returns from Qt's event loop, so a `SIGINT` sets a
flag nothing ever looks at. `app/main.py` now installs a handler that quits
cleanly — closing the window, releasing the single-instance lock, flushing
SQLite — plus a `QTimer` that does nothing four times a second purely to hand
control back to Python often enough for the handler to run.

### Fixed — the theme hook multiplied every time the theme changed

`_apply_theme` connected `colorSchemeChanged` to a lambda calling `_apply_theme`,
from inside `_apply_theme`. Each theme change added another connection, so one
flick of the system switch re-entered the handler once per change ever made,
each re-entry connecting again. Qt does not warn about duplicate connections.

### Fixed — Settings could not be scrolled, at any window size

Only the Indexing tab had a scroll area. Settings is six group boxes stacked
vertically and had none, so its lower half was unreachable on a short window and
marooned at the top of a maximised one. `app/ui/widgets/scroll.py` holds the
wrapper — including `setWidgetResizable(True)`, the line whose absence leaves a
narrow column of content inside a maximised window — and it is applied where
tabs are added, so it cannot be forgotten for the next view.

A wrapped view is no longer the widget in its tab, which silently breaks both
`tabs.setCurrentWidget(view)` and `tabs.widget(i) is view`. `MainWindow` now
keeps a view→index map and a `_show()` helper; a test fails if
`setCurrentWidget` reappears.

### Fixed — Ollama spent 120 seconds finding out nothing was listening

An enrichment run reported `elapsed_s: 120.09, chunks_processed: 0`. `requests`
applies a single timeout float to **both** the connect and the read, so the
two-minute budget meant for a local model composing a paragraph was also being
spent discovering the socket would not open. Connect now has its own three
seconds: Ollama is a process on this machine, and it either answers immediately
or is not going to.

### Fixed — six index runs started in seven seconds

Ordinary clicking. `IndexingView.start` did refuse the extra runs, but silently,
and only after `MainWindow._start_indexing` had built a `Pipeline` and an
`Embedder` — loading the ONNX model — purely to discard them. The guard now runs
first, and says so in the status bar.

**Layers 2, 3, 4, 5 and 6 code-complete.** All of Layer 2's eight acceptance criteria and all of
Layer 3's eight pass. The one thing no test can
reach - that `Win32ComSession` drives real Outlook - is a single `xfail(run=False)` plus a
manual `app.cli extract --mailbox`. `VERSION` stays at 0.3.2 until that has been run once.

### Fixed — CRITICAL: every PST was silently skipped, always

**`_classify` used `None` to mean two different things.** It was the "this file
has not changed, skip it" answer. It was *also* the perfectly ordinary "changed,
but there is no content hash" answer - which is what every file read through
another application returns, because its bytes are not what gets parsed and it
may be held open. Every `.pst` and every `.ost`, therefore, was classified as
**unchanged on the very first run, before it had ever been indexed**, and never
indexed at all.

It counted as `unchanged`, not `skipped`. No error appeared anywhere, nothing was
written to the skip ledger, and the run reported complete success. The symptom
was `{'seen': 7, 'indexed': 0, 'unchanged': 6, 'skipped': 0}` and a user saying
"I am not sure I have ever seen the PST extract work" - which was exactly right.

The sentinel is now a distinct object with a name. `tests/unit/test_index_freshness.py`
pins it from both directions: an archive is indexed the first time it is seen,
and an untouched archive is never opened again.

### Fixed — five bugs from the first time a human opened the window

Every one of these was invisible to 900 passing tests, because every one needed
a display, a light-mode machine, or a maximised window.

- **Indexing from the GUI was impossible.** The memory ceiling was absolute, and
  the GUI starts above 1.2GB before reading a file - Qt, the ONNX runtime and
  both stores are already resident where the CLI starts at ~200MB. So it paused
  on its first check at 1,597MB and never resumed: `seen: 1, indexed: 0` after 96
  seconds of nothing. **The ceiling now applies to growth above a baseline taken
  when the run starts**, which is also the number that was always meant: not
  "how big is this process" but "is indexing running away". An unknown baseline
  falls back to the absolute figure, which is the safe direction.
- **`RuntimeError: wrapped C/C++ object of type WorkerSignals has been deleted`.**
  `QThreadPool.start()` owns the runnable on the C++ side, but nothing on the
  Python side held the signals object; once the local variable went out of scope
  Python collected it and the still-running worker emitted into a corpse. Every
  worker now goes through `workers.run()`, which retains it until it reports
  itself done, and a test fails if any view calls `pool.start()` directly. It
  only bit when work outlived the function that started it, so it looked
  intermittent and unrelated to anything.
- **The indexing progress bar spun forever.** `start()` was called with no total,
  so the range was set to `(0, 0)` - Qt's indeterminate animation - and the value
  was only ever set `if self._total_estimate`, which was zero. It now grows its
  denominator from what the walker has found so far, because a true total cannot
  be known before the walk finishes.
- **The indexing page showed nothing about the index.** Opening it answered none
  of "is there an index, how big, how old", which is the only reason to open it.
  It now carries a totals line, refreshed on every visit and after every run.
- **Maximising broke the layout.** The skipped-files scroll area took all the
  stretch while its contents were hidden, so a maximised window was mostly empty
  panel with the controls squashed at the top. It now claims space only when it
  has something to show.

### Fixed — the app was dark on a light-mode machine

Hardcoding a dark palette is not a style choice, it is a bug: the application
looked like it belonged to a different operating system, and on a bright screen
it is harder to read rather than easier.

`app/ui/theme.py` holds two palettes with identical token sets - a test fails if
they drift - and the sheet is written against tokens, since Qt stylesheets have
no variables. The OS preference comes from `QStyleHints.colorScheme()` and is
followed live, so flipping the Windows switch changes the window without a
restart. Settings offers Follow Windows / Always light / Always dark.

The `highlight` token needed genuinely different values rather than one shared
colour: search-term yellow on white is nearly invisible, which is the whole
reason these are two palettes and not one with a flag.

### Fixed — noise on top of a real failure

At 12GB the machine was swapping and SQLite came back with nulls, which produced
`int() argument must be ... not 'NoneType'` and three `ERR_UNEXPECTED` reports
**per keystroke** about a store that was closing. `generation` returns 0 for a
missing or NULL value, and the search engine checks `store.is_open` before
reading - a background search outliving the window is expected, not a bug worth
a traceback.

### Fixed — the progress line stopped updating whenever anything was logged

Reported as "it stopped printing or giving indication it is working", with a
warning about an unreadable `.vsd` immediately above.

A `\r` progress line and a logger writing to the same console destroy each
other: the warning lands on top of the line, the next carriage return overwrites
the warning, and what remains is a mangled line that never changes again. From
the outside that is indistinguishable from a hung process, which is exactly the
thing the heartbeat was added to prevent.

`ProgressLine` now owns the console. Anything logged goes through a sink that
wipes the line, lets the message land on its own row, and repaints - the same
discipline `pip` and `apt` use. No curses and no ANSI cursor codes: one carriage
return and some spaces, which behaves the same in a plain console, in Windows
Terminal, and when piped to a file (where it disables itself entirely).

Two traps, both found by shipping it and looking at the output:

- `setup_logging` clears every handler, so the progress sink has to be added
  *after* it. The other way round removes the sink silently and the warnings
  disappear altogether - worse than the mangled line.
- **`setup_logging` is also idempotent**, because both the CLI and the UI call
  it, so reconfiguring for the progress line needs `force=True`. Without it the
  original INFO console sink survives, the progress sink is added on top, and
  every line prints **twice** - once by the handler that respects the progress
  line and once by the handler that walks straight over it. Which is exactly
  what the first attempt did.

- **Java's log4j complaint is quietened.** mpxj ships log4j-api with no binding,
  so the JVM printed `main ERROR Log4j API could not find a logging provider`
  straight to stderr the first time it read a `.mpp`. Harmless, and it appears
  mid-run looking exactly like a failure. Three `-D` properties are passed on JVM
  start; an unrecognising JVM ignores them rather than refusing to start. Not
  verifiable here without a real `.mpp`, so it is best-effort by design.

### Fixed — the run summary was reporting two different units as one number

From a real run: `seen: 8, indexed: 17, unchanged: 335`. Every number is
correct and the line is unreadable, because a `.pst` is one **file** and
hundreds of **documents** and both were being called the same thing.

- `seen` counts files; `indexed` counts documents. Said so now.
- **`unchanged_documents` split out from `unchanged`.** "335 unchanged messages
  inside one changed archive, 17 rewritten" is a completely different story from
  "335 unchanged files", and only one of them was true. The new number is also
  the one that shows per-message indexing paying for itself.
- **`bytes_read` reported 0.0 MB for a 64-second run over 100MB.** Bytes were
  credited when a file's first document was *written*, so an archive whose first
  message happened to be unchanged reported nothing for the entire file. They
  are credited on arrival now, written or skipped.

### Added — Visio and Project, without COM

Three formats, three honestly different answers, because pretending otherwise
would be worse than the gap:

| | what it is | what we get |
|---|---|---|
| `.vsdx` / `.vsdm` | a ZIP of XML, like every modern Office format | **all shape text, per page** |
| `.vsd` | a 2003 OLE compound binary | title, author, subject - and the name |
| `.mpp` / `.mpt` | proprietary, no open specification | title, author, subject - and the name |

- **No COM, and it was not needed.** `.vsdx` is a documented OPC package; the OLE
  summary stream in `.vsd` and `.mpp` is a documented structure `olefile` reads
  in pure Python. COM would additionally have required Visio and Project to be
  *installed*, which on an indexing machine they generally are not.
- **The `vsdx` package is optional.** When it is absent the page XML is read
  straight out of the ZIP - a diagram is mostly labels, and losing them to a
  missing optional dependency would be a poor trade. That fallback is not
  theoretical: it fired on the first test fixture and is what made it pass.
- Labels split across styled runs are rejoined with `itertext()`. Visio splits a
  label the moment any of it is bold, and reading `element.text` alone truncates
  at exactly the tag number somebody would search for.
- **A file we cannot read is still worth indexing.** `.vsd` and `.mpp` produce a
  document of their filename plus summary properties, carrying a warning that
  says *why* nothing inside is searchable and, for `.vsd`, that re-saving as
  `.vsdx` fixes it. A plan indexed by name comes back when you search for the
  project; one the app has never heard of does not exist.
- **`.mpp` contents are reachable at a price.** `mpxj` reads them properly but
  bundles 32 JARs and needs a JVM and JPype - against this application's whole
  premise. The hook is present and guarded; `pip install mpxj jpype1` turns it on.
- A test asserts `diagrams.py` imports no `win32com`, `pythoncom` or `comtypes`.
- **`doctor.py` reports what each format can actually do here** - "full shape
  text", "full task list (mpxj)", "name + document properties" - because the
  difference is invisible until somebody searches for text they know is in a
  diagram and finds nothing. It flags only states naming something installable:
  `.vsd: name + document properties` is the *best achievable* outcome for a
  format with no open specification, and reporting it as a problem would send
  somebody installing a package that changes nothing.
- **The mpxj integration was wrong and is now verified.** The first version
  imported `net.sf.mpxj.reader`; mpxj moved to `org.mpxj` around version 14, so
  it would have failed on every modern install - silently, behind a broad
  `except`, reporting the plan as merely unreadable. Both packages are tried,
  and it was tested against mpxj 16.7.0 with a real JVM rather than assumed.

### Fixed — two things that would not have survived 200,000 emails

Prompted by "my PSTs have possibly 200K+ mails". Both are invisible at test
scale and fatal at real scale.

- **`_prune_missing` materialised every row** - all 200,000 `FileRecord` objects -
  to filter down to the few thousand real files, at the end of every run.
  `iter_files(source_kind=...)` filters in SQL now, and only the ids to delete
  are held. Deleting while iterating a cursor over the same table was also
  quietly unsafe; the ids are collected first.
- **`merge_contained_entities` was O(n²) with a query per pair.** On a corpus
  that size the entity table runs to tens of thousands of rows - billions of
  comparisons - and the symptom is an index run that appears to hang at the very
  end, which is the hardest kind of failure to diagnose. Entities are bucketed
  by first word, which is where containment that matters actually lives
  ("AVEVA Group" inside "AVEVA Group Limited"), with a cap for any word that
  begins thousands of names.

### Added — finding a file by its NAME (schema v4)

`chunks_fts` indexes what documents *say*. **Nothing indexed what they are
called**, so a file named `Invoice 2024.pdf` whose contents never used those
words could not be found at all - which is how most people look for most files.
No existing test could have caught it: every one of them asked about content.

- **`files_fts`, tokenised with `trigram`**, so "voice" finds "Invoice". Filename
  search *is* substring search - people type the middle of a name and expect a
  hit - and a word tokeniser cannot do that at any price. Falls back to
  `unicode61` with prefix indexes if trigram is somehow unavailable.
- **A Files tab** (`Ctrl+P`) and **`app.cli files`**. No embedding, no reranking,
  no snippets: one FTS5 lookup over a table of filenames, fast enough to run on
  every keystroke. Rows show size and age, because "yesterday" answers "is this
  the one I was working on" and a timestamp requires arithmetic.
- **Mail is deliberately absent.** A message's key is synthetic and mail would
  outnumber documents ten to one; mail is searched from the search tab.
- A file indexed **by name only** - a scanned PDF, something locked - says so in
  its row rather than being hidden. "I can see it but cannot search inside it"
  is real and useful, and hiding it invites the same fruitless search twice.
- The migration backfills from the existing index, so no re-index is needed, and
  it is safe to re-run: an FTS5 table rejects a rowid it already holds, so a
  plain INSERT would fail and leave the database stuck between versions.

### Added — scope chips: Everything / Mail only / Documents only

A *filter*, not a mode. You should never have to decide whether a thing was an
email or a document **before** typing, because the usual answer is "I do not
remember, that is why I am searching".

`source_kind` was already a column and `from:` was already an operator, so this
is a WHERE clause rather than a second search path. `ParsedQuery.scoped()`
returns a copy - the class is frozen so it can be a cache key - and **the scope
is part of that key**: without it "All" and "Mail" share an entry for the same
typed text and whichever ran first answers for both. Loose `.eml` files count as
mail, because that is what they are to the person searching.

### Fixed — per-message indexing had made embedding much slower

Reported from a real run: "this method is very slow compared to the other".
Correct, and a regression from the streaming change. Embedding happened once per
document, so an email meant a batch of about three chunks - the size at which
ONNX spends its time on per-call overhead rather than on matrix work.

Embedding now batches **across** documents (`EMBED_BATCH = 256`) in the consumer.
A file is marked INDEXED only after its vectors are written, never before: the
reverse leaves it invisible to semantic search and never retried. A test counts
embedding calls rather than measuring elapsed time - flaky, and it would not say
why.

### Changed — extraction streams, and the unit of work is a document not a file

Found because a 100MB `.pst` took six minutes and showed nothing. `app.cli extract`
returned in seconds, which located the cost precisely: reading the archive was
never the problem.

```python
documents = list(extract(candidate.path))   # the ENTIRE archive, in memory
```

Every message and every attachment's extracted text was materialised before a
single chunk was made - then all the chunks, then **all the embeddings in one
call**. Nothing written, nothing committed and nothing on screen until the whole
archive finished. A 3GB archive would have exhausted memory rather than finishing
slowly.

`_extract_stream` is a generator yielding one `_Extracted` per document, so:

- **memory is flat** - one message at a time rather than a whole archive;
- **work commits as it goes** - an interrupted archive keeps what it read;
- **progress is visible** - `current_item` counts messages, so the screen shows
  `reading 2007.pst [1,284] 94s` instead of nothing;
- **a search result names the email**, not the `.pst` it lives in;
- **re-indexing a changed archive re-reads only what changed** - by a hash of the
  message *text*, because an archive's bytes move whenever Outlook opens it while
  a fifteen-year-old email does not change at all. One new message in 30GB now
  costs one embedding.

Three bugs caught while making it work, each by a test written to fail first:

- **The archive lost its own `files` row**, so the walker had nothing to compare
  against and would have re-read the whole thing on **every run, forever** -
  destroying the exact property incremental indexing exists for. Streams now
  close with a marker row carrying the container's size and mtime.
- **An extractor that forgets `virtual_path` silently overwrote every message
  onto one row**, leaving the archive as a single entry holding only its last
  email, with no error anywhere. Duplicate keys are now made unique and logged
  by name - losing mail to an extractor bug is far worse than an ugly key.
- `stats.bytes_read` counted the archive's size once per message, making the
  throughput figure meaningless.

### Fixed — the resource governor could deadlock the run it was protecting

Found while a real 100MB PST refused to finish. Both bugs were introduced by the
governor added earlier the same day, and both present identically: an index run
that never ends and looks merely slow.

- **The pause happened in the consumer - the only thread that drains the results
  queue.** While it waited, the extraction workers blocked handing over results
  they were still holding in memory. So memory never fell, so the memory pause
  never cleared, and the run hung permanently. **Backpressure belongs at the
  intake, never at the drain**: the waiting moved to `_produce`, which holds
  nothing but a path, so pausing it starves the workers of new work while
  everything already in flight keeps draining - which is what actually brings
  memory down. Three tests drive the governor permanently over its ceilings and
  assert the run still finishes.
- **The indexer counted its own CPU as a reason to stop.** Four workers on a
  four-core laptop saturate the processor by themselves, so it paused, watched
  CPU fall, resumed, spiked and paused again - throttling itself to a crawl on a
  completely idle machine. `Snapshot.other_cpu_percent` subtracts this process's
  own share first (dividing by the core count, because `Process.cpu_percent` is
  per-core while `cpu_percent` is already averaged). Its own load was always
  handled better by below-normal priority anyway.
- **A third bug, caught by the test written for the second:** with the wait moved
  to the producer, a full disk ended the run *silently* - complete success, zero
  files indexed, no error. The same shape as the sentinel bug from earlier the
  same day, which is why it was worth writing the test that could only fail.

### Fixed — a run over few large files showed nothing at all

- **Progress fired every 50 files.** A folder of ten documents plus one 100MB mail
  archive never reaches fifty, so the callback fired exactly once, at the end. The
  screen stayed blank for the whole run, which is indistinguishable from a hang -
  and the correct response to a hang is to kill it. Now checkpoints on **two
  seconds or fifty files, whichever comes first**.
- **A 100MB archive is a single file**, so nothing reaches the consumer until the
  whole thing is parsed and even a time-based tick showed nothing during the
  slowest part. `IndexStats.current` now carries the file being read and how long
  it has been on it, redrawn in place rather than scrolling.

### Added — the indexer is now configurable and stays out of the way

Prompted by "this is designed to run on a working machine". An indexer that makes
Excel stutter gets switched off and never switched back on, and then none of the
rest of this matters.

- **`app/index/resources.py`** - four ceilings, every one configurable, every one
  a ceiling rather than a target. **Memory** pauses and drains rather than
  aborting; **CPU** pauses while the machine is busy, with hysteresis so a
  transient spike does not stall the run; **battery** pauses until mains;
  **disk** is the only one that stops, because it is the only failure that
  damages something outside this application and does not resolve itself while
  the indexer keeps writing. The whole decision is a pure function of a
  `Snapshot`, so every threshold and recovery path is tested with invented
  numbers - no test has to exhaust a real machine's memory.
- **Below-normal CPU and background I/O priority**, applied before the first file
  is read. The cheapest courtesy available and the most effective.
- **Default workers is now half the cores, capped at four** - not `cores - 1`,
  which on an 8-core laptop handed seven cores to a background task and left one
  for the person. Past four the disk is the wall anyway.
- **`app/index/schedule.py`** - manual / on launch / every N hours / daily at a
  time. Pure arithmetic on two timestamps, because scheduling bugs are the ones
  that never reproduce: a laptop opened after a fortnight away indexes **once**,
  not fourteen times, and a clock moved backwards does not trigger a second run.
  A `MIN_GAP_S` backstop means a bug here cannot become a machine that never idles.
- **`app/ui/scheduler.py`** - a one-minute `QTimer` and one guard: a scheduled run
  never starts on top of a run already going. The single-instance lock protects
  the database from a second *process*; nothing protected it from this
  application starting a second run over its own, which is the mistake a timer
  makes at 02:00 with nobody watching.
- **An Indexing panel in Settings** and `--memory-mb`, `--cpu-percent`,
  `--full-speed` on `app.cli index`. `--full-speed` is named for what it costs.
- `IndexStats` now reports `paused_seconds` and `pauses`, because a four-hour run
  that was mostly *waiting* looks identical to one that was slow, and the fix is
  the opposite in each case.
- `psutil==7.1.3` pinned - verified to publish a `cp37-abi3-win_amd64` wheel, so
  no compiler. Every import is guarded: without it the disk guard and worker cap
  still apply, and `doctor.py` reports the ceilings as INACTIVE rather than
  letting somebody believe in a limit that is doing nothing.
- **`app/ui/indexing_settings.py`** extracted, because `settings_view.py` had
  reached 342 lines. The view-length guard now globs every view module instead of
  checking a hand-written list - that drift is exactly what it exists to catch.

### Added — Layer 6, the knowledge graph
- **Schema v3**: `entities`, `entity_mentions`, `entity_edges`. Additive, like v2, and for
  the same reason - the graph is derived entirely from `chunks`, so it is built at leisure
  on an existing index without re-reading a single file, and `--rebuild` is always safe.
- **`app/graph/cooccurrence.py`** - the default, and deliberately not an LLM. Emails,
  filenames, acronyms and capitalised n-grams, with edges weighted by **normalised PMI**.
  Raw co-occurrence produces a hairball centred on the commonest word in the corpus; PMI
  asks whether two things appear together *more than chance predicts*, which is the
  question someone drawing the diagram by hand would ask. Normalised because raw PMI's
  ceiling depends on how rare a pair is, so a threshold chosen today silently stops
  filtering as the index grows.
  Pure functions throughout, so the part that decides what counts as a thing is tested
  with literal strings on any machine.
- **`app/graph/builder.py`** - resumable two-pass build. Two passes because an edge's score
  depends on corpus-wide totals that do not exist until the last chunk has been read.
  **The cursor is committed in the same transaction as the batch it describes**, which is
  not tidiness: edge weights accumulate, so a replayed batch double-counts with no error
  and no way to detect it afterwards.
- **`app/graph/render.py`** - networkx metrics (centrality, sampled betweenness, community
  detection) and a self-contained pyvis page. Capped at 5,000 nodes, and the page **says**
  it is capped - a missing node otherwise reads as evidence that nothing connects there.
- **`app/graph/entities_llm.py`** and **`app/llm/ollama.py`** - optional typed extraction.
  It only ever adds and refines, never deletes, so the deterministic graph stays intact and
  enrichment stays reversible. Ollama stopping mid-run is treated as normal rather than
  exceptional: checkpoint, pause with `ERR_OLLAMA_DOWN`, resume exactly there next time.
- **A Graph tab**: entity table, what each connects to, the passages behind it, and
  "search for this entity". The strength of a link is shown in words - "strongly linked" -
  because `0.62` means nothing to anyone who has not read the PMI definition.
- **`app.cli graph`** with `--rebuild`, `--enrich`, `--entity`, `--html`, `--top`.

### Fixed — found by running Layer 6 on Windows, against a real corpus

Two classes of failure, and neither could have been found in development. The first
needed Windows; the second needed documents nobody wrote for a test.

- **THE CRASH: the graph page could not be written on Windows at all.**
  `pyvis.write_html` calls `open(path, "w+")` with no `encoding`, so the file is written
  in the **process locale encoding** - cp1252 on a UK Windows install. The page carries
  `’ — · …` from entity names, from tooltips and from the inlined vis-network library;
  cp1252 can encode none of them, so the render raised `UnicodeEncodeError` after
  building the entire 264KB document. Every `--html` render and four tests died on it.
  Invisible on Linux and macOS, whose default is already UTF-8.
  Fixed by generating the HTML and writing it here with an explicit encoding, which also
  removed a redundant read-modify-write. A test now asserts on the *bytes*. The rest of
  `app/` was audited for the same pattern; this was the only instance.

- **The graph's top 25 entities on a real corpus were English words.** "Connect",
  "Enterprise", "System", "Optimize", "Access", "Learn", "Use", "How", "Customers",
  "Ability", "Slide" - and, because a slide heading is set in capitals, the "acronyms"
  `DATA`, `CLOUD` and `DESIGN`. 442 entities, and the ones that mattered were buried.
  Four separate causes, each fixed and each pinned by a test named for the wrong output:
  - **ALL-CAPS words broke name runs**, so "AVEVA System Platform" fragmented into an
    acronym and a leftover "System". They now join a run; a *lone* capitalised word is
    still an acronym. This one fix removed most of the noise, because most of it was
    debris from a shattered product name.
  - **A `COMMON_WORDS` blocklist** for single-word entities, and for runs made entirely
    of ordinary words. "PI System" and "Customer FIRST" survive - the rule is about what
    a run is *made of*, not about any word appearing in it.
  - **"and" welded separate names together**: "SCADA and MES" became one entity. A
    connector now joins only when the word beside it is Title Case, which is what
    distinguishes "Work and Pensions" from a conjunction between two acronyms.
  - **Possessives made duplicate nodes.** "AVEVA’s" and "AVEVA" sat side by side with
    the same visible label. Both apostrophes are stripped, because a Word document and a
    PDF disagree about which one they use.
  - An imperative opening a sentence ("Discover AVEVA Insight") no longer joins the name.
  - A run of initials no longer forms an entity - a regression the ALL-CAPS change
    introduced and a test caught: "A B C" briefly became the entity "B C".

- **Second pass over the same corpus.** With the ordinary nouns gone, what surfaced
  underneath was slide-bullet grammar: "Provide", "Accelerate", "Ideal", "Operational",
  "Flexible", plus heading numbering ("II") and ALL-CAPS adjectives ("OPEN", "HYBRID").
  - **A lone Title-Case word that only ever opens a sentence is no longer an entity.**
    Its capital is grammar, and a slide bullet is its own sentence. This is evidential
    rather than another blocklist entry, because no list keeps up with the supply of
    verbs - and it costs nothing for a real name, which is mentioned mid-sentence
    somewhere and still collected there.
  - Roman numerals break a name run instead of joining it, so "OPEN HYBRID II" can no
    longer slip past the all-ordinary-words check on the strength of its "II".
  - **`entities.merge_contained_entities()`**: "AVEVA Group" and "AVEVA Group Limited"
    were two nodes joined to each other and to all the same neighbours. A short name is
    now folded into a longer one **only when every chunk mentioning the short one also
    mentions the long one** - the evidence that it is never used on its own. A textual
    prefix test would have destroyed "AVEVA", which is a prefix of the same string and a
    more important entity in its own right; word boundaries stop "PI" being read as part
    of "PIPELINE". It runs before scoring, so the survivor's PMI reflects the merged
    evidence rather than half of it.

### Fixed — found while building the graph
- **The rendered graph page called out to a CDN.** pyvis emits two jsdelivr tags for
  Bootstrap *even with* `cdn_resources="in_line"`. On a machine with no internet those
  requests hang and fail, on a page whose entire premise is that nothing leaves the
  machine - and it renders perfectly on a connected developer machine, which is how this
  would have shipped. Stripped now, with an acceptance test that greps for `https://`.
- **Filenames swallowed whole clauses.** A space-tolerant filename pattern has no
  left-hand delimiter inside prose, so it walked backwards and produced the entity
  "about the HACCP review and attached Pasteuriser Report.docx".
- **The calendar leaked in one full stop at a time.** "Tuesday." is not in the stopword
  list; "Tuesday" is.
- **Connectors counted against the name-length cap**, so "Department for Work and
  Pensions" was rejected as a heading.
- **`_names` closed over its loop variable.** It worked, but by timing - the generator was
  always drained before the variable was rebound. Rewritten as a plain function taking the
  run as an argument.
- **`--strict-markers` was declared twice in `pyproject.toml`**, one block silently
  overwriting the other.

### Added — reading .pst without Outlook
Reassessed after a direct test rather than from the architecture doc's assumption. `pypff` is
not on PyPI, but **`libpff-python` builds and imports**, and the capability is real. The doc was
right about wheels and was being read as "cannot be done".

- **`app/extract/pst_libpff.py`** - reads archives from the file. No Outlook, no COM, no file
  lock, no changes to the user's mail profile - and, uniquely among the approaches tried here,
  **testable on any machine**, which finally puts a floor under the largest untested surface in
  the project. 26 tests drive a fake `pypff`; one more pins the real library's API so a version
  bump that renames an accessor fails in a second rather than inside a 30GB archive.
- **`PstBackend` with `auto` / `libpff` / `outlook`.** `auto` prefers direct reading and falls
  back to Outlook. `.ost` always goes to Outlook whatever is asked: it *is* the Cached Exchange
  Mode file, libpff reads it poorly, and the live mailbox is Outlook's own business. The
  division is libpff for offline archives, Outlook for live mail - each doing what it is
  actually good at.
- **`app.cli convert` and a Settings button** - export an archive to a folder of `.eml`. The
  permanent escape hatch: afterwards the mail needs neither Outlook nor libpff, any mail client
  can open it, and the folder is added as an index root automatically rather than leaving one
  more step to remember.
- **`libpff-python` is deliberately NOT pinned.** It has no Windows wheel and compiles during
  install, which is exactly what the rule at the top of `requirements.txt` exists to prevent.
  Every import is guarded, so its absence changes nothing; `doctor.py` reports which route is
  available and what the other would cost.
- Filenames from message subjects are sanitised - a subject is attacker-controlled text that
  becomes a path, and a test asserts `../../etc/passwd` cannot escape the destination folder.

### Added — Layer 5, the desktop app
- **`app/ui/presenter.py`** - every UI decision that is not drawing, and **it imports no Qt**.
  Widgets cannot be instantiated without a display, so logic inside them could only ever be
  checked by a person clicking; this module holds the snippet windowing, highlight offsets,
  tier selection, ETA phrasing and skip grouping, and 44 tests cover it. Two further tests
  enforce the split itself: one fails if Qt ever appears in the presenter's imports, one if a
  view module grows past 250 lines, because a long view is where untested logic hides.
- `app/ui/workers.py` - `QThreadPool` wrappers so the UI thread never does I/O. Each converts
  what escapes into an `AppError`: an exception leaving a `QRunnable` vanishes, and the UI
  would wait forever for a signal that never comes.
- `app/ui/search_view.py` - two debounce timers, and **generation-tagged dispatch** so a slow
  search landing after newer typing is dropped rather than overwriting fresher results.
- `app/ui/results_view.py` - path, location, why it matched, and a snippet centred on the hit
  with terms picked out. A result whose file has vanished is **marked, not hidden** - that is a
  genuine finding, and dropping it silently would make the count disagree with the list.
- `app/ui/indexing_view.py` - progress **by file count, never by bytes**: measured on the real
  corpus, a 40MB deck yields fewer chunks than a 30KB Word document, so a byte-based bar sits
  frozen and then races. Plus the skipped-files panel, grouped by cause, biggest first.
- `app/ui/settings_view.py` - roots, toggles, `doctor.py --json` rendered inline, and **clear
  search history**, because the usage log is a record of what someone searched on their own
  machine and must be theirs to erase.
- `app/main.py` - the entry point. Settings first, then the single-instance lock **before**
  either store is opened, then the window; models warm on a background thread.

### Added — Layer 4, search
- **Schema v2**: `searches` and `search_hits`, plus a migration so an existing index gains them
  without a rebuild. Built seven layers before anything reads them because this is the one part
  of adaptive tuning that **cannot be added later** - in six months there is no record of what
  was searched or what turned out to be useful. Local, never transmitted, and clearable.
- `app/search/keyword.py` - BM25 over the sanitised expression, with **filters applied in SQL**.
  Fetching 100 rows and discarding 90 to honour `type:pdf` leaves the 10 best of the wrong set;
  filtering in the query makes the top 100 the top 100 *that match the filter*.
- `app/search/vector.py` - one query embedding per search, ANN with eligible file ids pushed
  down as a prefilter. Above 2,000 ids the prefilter is dropped, because an `IN (...)` list
  that long costs more than the search it was meant to narrow.
- `app/search/rerank.py` - cross-encoder over the top 30, lazily loaded, **never able to fail a
  search**. The model is optional and 1.1GB; missing, half-downloaded and deleted-mid-session
  are all handled identically - log once, keep the fused order, succeed. Returning nothing
  because an optional precision step could not load would be worse than never having it.
- `app/search/engine.py` - parallel dispatch, RRF, generation-keyed cache, and the two-tier
  design: BM25-only while typing, full hybrid on Enter. `SearchResult.explain()` says *why*
  something matched, because trust comes from being able to ask.
- `app.cli search` with typed operators, `--limit` and `--no-rerank`.
- 42 Layer 4 acceptance tests against a real index, including a golden set with recall@10 as a
  regression guard.

### Added — Layer 3, the indexing pipeline
- `app/index/walker.py` - roots, allowlist, exclusions, change detection.
  **Exclusions prune during the walk, never filter after it**: descending into a 40,000-file
  `node_modules` and discarding it costs the whole subtree, so a test counts the directories
  actually visited rather than the output - a filter-afterwards implementation produces
  identical output and takes a hundred times longer.
  **The hash decides; mtime only decides whether to hash.** robocopy, a restore from backup,
  cloud sync and archive extraction all reset mtime while leaving bytes identical. Trusting
  mtime alone would re-index the whole corpus every time any of those happened, and a test
  touches a file and asserts it is *not* re-indexed.
- `app/index/embedder.py` - batched FastEmbed wrapper, lazily loaded, with three guards for
  things that otherwise fail in silence: a dimension mismatch (what changing `EMBED_MODEL`
  looks like), an un-normalised vector (raises nothing, ranks wrong forever), and a batch that
  returns the wrong number of vectors (which would pair chunks with other chunks' vectors).
  The encoder is injectable, so all of it is tested with no model on disk.
- `app/index/pipeline.py` - bounded priority queue, N extraction workers, one embed-and-write
  consumer. Embedding is deliberately not parallelised: ONNX already uses every core inside one
  call. **Both queues are bounded** - without backpressure the process dies of memory around
  hour three having written nothing.
  **Resumability is the `files` table, not a saved position**: a restart re-walks and skips
  what is already `INDEXED` for the cost of a `stat()`, which is more robust than an offset
  that goes wrong the moment the corpus changes underneath it.
  **Chunks and vectors are written before the file is marked `INDEXED`** - a crash between them
  leaves a file that looks unfinished and gets redone, where the reverse would leave it marked
  done with no chunks, invisible to search and never retried.
- `app.cli index` - Layer 3's entry point, with `--first` for folder prioritisation (repeatable
  and ordered), `--fast`, `--no-prune`, `--include-cloud`. Takes the single-instance lock,
  because unlike `extract` it writes.
- 79 Layer 3 tests: 30 walker, 19 embedder, 25 acceptance, 9 CLI.

### Added — Layer 2
- `app/extract/base.py` - the extraction contract. `Document` holds one flat `text`, and every
  `Segment` carries the exact range it occupies within it, so
  `text[s.char_start:s.char_end] == s.text` always. That invariant is what lets Layer 5
  highlight a hit inside the original instead of guessing; `DocumentBuilder` appends text and
  records offsets in one operation so no extractor can let the two drift. The registry refuses
  a duplicate extension claim rather than letting whichever module imported last win.
- `app/extract/chunker.py` - ~512-token chunks, ~64 overlap, paragraph then sentence
  boundaries, never mid-word. Text is atomised once into words-with-spans tagged by the
  boundary preceding them; a chunk is a contiguous range of atoms, which makes exact offsets
  and whole-word boundaries true by construction rather than by care.
- `app/extract/pdf.py` - PyMuPDF, page numbers retained. A PDF with no text on any page is
  skipped as `ERR_NO_TEXT_LAYER` and counted, rather than indexed as an empty success - a scan
  that "indexes cleanly" with no text is unfindable forever while appearing to have worked.
- `app/extract/office.py` - DOCX (document order, tables included), XLSX (`data_only`, sheet
  names in the text, 5,000-row cap), PPTX (slides plus speaker notes).
- `app/extract/plaintext.py` - UTF-8 → cp1252 → latin-1 with the BOM stripped, and NUL-byte
  detection so a binary file with a `.log` extension does not fill the FTS index with garbage.
- `app/extract/email_files.py` - `.eml` via the stdlib, `.msg` via extract-msg. Thread grouping
  from `References[0]`, so every reply in a conversation shares a key.
- `ERR_NO_TEXT_LAYER` and `ERR_UNSUPPORTED_TYPE` in the error registry. The spec referenced the
  first by name and Layer 0 never registered it.
- `tests/fixtures/generate.py` - the fixture corpus, **generated rather than committed**.
  Binary fixtures in git rot: nobody can review a `.docx` diff, nobody remembers which byte was
  corrupted on purpose, and an editor that opens and re-saves one silently destroys the property
  it was testing. Every corruption is now a reviewable line of code.
- 98 Layer 2 tests plus `tests/integration/test_layer2_acceptance.py`, numbered to the spec's
  checklist. The overlap criterion is asserted as a property - every word of every healthy
  fixture must survive into at least one chunk - because a word lost between two chunks is
  unfindable and nothing in the system would ever report it.
- **`app.cli extract`** - Layer 2's headless entry point, which ground rule 7 requires and the
  layer had shipped without. Files or folders, `--chunks` to see every chunk with its page,
  token estimate and offsets, `--text` for the whole document, `--json` for a machine-readable
  dump, `--limit` when pointed at something large. Read-only by construction: it opens no store
  and writes nothing, so it is safe to point at anything. Cloud placeholders are checked
  **before** the file is opened, because opening one is what triggers the download; a test
  asserts it. Prints per-file timing and an overall MB/s - the first real input to the
  throughput question Layer 3 has to answer.
- 23 tests for the command, including one asserting it never opens a store.
- **`app/extract/email_pst.py`** - Outlook archives and the live mailbox, via MAPI.
  **The COM calls are confined to `Win32ComSession`**; the walk, the conversation grouping,
  the attachment dedup and every error path work against small duck types and are tested with
  a fake on any machine. Only the adapter needs Windows, which is the smallest untested
  surface this could have had - the alternative was shipping the whole thing unverified.
  Identity is the `EntryID`, never a folder path: moving a message between folders must not
  make it look like a new one, or an index over a mailbox people reorganise never settles.
  Attachments are **deduplicated by content hash**, and the hash set is the caller's, so
  Layer 3 persists it in `files.content_hash` and dedups across runs rather than within one.
  `Deleted Items`, junk and sync-conflict folders are skipped by default. A `com_error` in one
  folder becomes `ERR_OUTLOOK_BUSY` and the walk continues - by the time Outlook gets closed,
  thousands of messages may already have been read.
- `app.cli extract --mailbox` - walk Outlook rather than a path, reporting counts per store,
  attachment totals and any folders that could not be read. Still writes nothing.
- 27 PST tests driving a fake MAPI session, plus acceptance criteria 5 and 6 implemented
  against it in `test_layer2_acceptance.py`.
- **PPTX now reads grouped shapes, tables and charts.** `slide.shapes` yields top-level shapes
  only, and a group is one opaque shape with no text frame - so reading `has_text_frame` alone
  silently lost every word inside every group, and grouping is how slides get built. On a
  deck-heavy corpus that was not an edge case, it was most of the content. Tables and charts
  had the same problem: a comparison table and a chart's category labels are exactly what
  people search for, and neither has a text frame. A test reconstructs the old logic and
  asserts it finds none of it, so the fix cannot quietly rot.
- **A picture-heavy deck is now flagged.** A PDF with no text is skipped outright, but a deck
  always has a title, so it indexed "successfully" while most of its content stayed
  unsearchable. Below 400 characters per megabyte it now carries an `ERR_NO_TEXT_LAYER`
  warning and is still indexed - the PPTX analogue of a scanned PDF.
- `app.cli extract --out PATH` writes the JSON itself, in UTF-8. Windows PowerShell 5.1's `>`
  redirection emits UTF-16LE with a BOM, which every JSON reader then rejects - the same
  encoding trap that killed `install.ps1` at parse time.
- Skipped files now carry their size, and the summary separates bytes *seen* from bytes *read*.
  A 100MB archive that vanished from the totals because it was skipped made them a lie.
- `extract-msg==0.56.1` in `requirements.txt`.

- `doctor.py` now checks **Git on PATH**. It never did, despite `install.ps1` installing Git and
  the whole release process in `docs/VERSIONING.md` depending on it. Optional, because the app
  runs fine without it - `build_info()` degrades to a version with no commit - but every
  convention silently stops working and the failure surfaces as "git is not recognized" long
  after the installer said it was done. The usual cause is not a missing install but a
  PowerShell window opened *before* Git was installed, which keeps its stale PATH until closed,
  so the fix leads with refreshing PATH in place rather than reinstalling.

### Removed
- `ERR_PST_NOT_BUILT`. It existed for one afternoon to make the gap visible; PST is built, so
  a `.pst` now either indexes or fails for a real reason. A code nothing raises is a lie in the
  registry.

### Fixed — caught while building the libpff backend
- **Every PST message would have had no recipients.** `To` and `Cc` header values were joined
  with a space before parsing. `email.utils.getaddresses` was hardened against malformed input
  (CVE-2023-27043) and now returns *nothing at all* rather than doing its best - so the whole
  recipient list came back empty, silently, for every message in every archive. Joined with a
  comma now, and a test asserts three addresses across two headers.

### Fixed — found by indexing a real folder containing a real .pst
The GUI reported `seen: 7, indexed: 0, unchanged: 6, skipped: 0` and success. The seventh file
- an Outlook archive - was seen, then vanished from the accounting entirely. Three faults, in
the order they compound:

- **One unreadable file ended the entire walk.** A `.pst` that Outlook holds open cannot be
  read, so `content_hash()` raised `PermissionError` - **on the walker thread**, where one
  escaping exception abandons every file not yet reached. It was caught by a handler that
  logged a single line and let `run()` report success. This is a direct violation of
  non-negotiable #4, *one bad file never halts a batch*, in the one place that rule matters
  most. `has_changed()` now returns "changed, unhashed" instead of raising, `_classify()`
  cannot raise at all, and a walk that really does stop early sets `stopped_early` so the run
  says so rather than claiming to have finished.
- **A `.pst` should never have been byte-hashed.** It is read through Outlook, which holds the
  lock, and its bytes are not what gets parsed. Extractors now declare `reads_externally`, and
  those files are change-detected on mtime and size alone.
- **COM was being used from worker threads without `CoInitialize`.** COM is per-thread; without
  it `Dispatch` fails everywhere except the main thread. That is exactly why
  `app.cli extract --mailbox` worked from the command line while indexing the same archive from
  the GUI did nothing - the difference was never Outlook, it was which thread asked.

### Fixed — found by the first run on real Windows
Four failures that Linux hid. Three were real bugs; the platform difference is the point.

- **The search cache never invalidated.** `generation` is a `@property`, and the engine called
  it as `generation()`. The `TypeError` landed in a broad `except` that fell back to `-1`, so
  every search keyed on the same value and **stale results would have been served forever** -
  including hits on text that had just been deleted. The exact failure the test was written to
  catch, hidden by the exception handler meant to make the cache robust. The fallback now logs
  loudly: a cache that cannot tell it is stale is a lie, and it must say so.
- **Locked files re-queued themselves mid-run.** `_candidates()` yielded the walk, then queried
  the store for previously-locked files - lazily, so by the time it ran, files *this run* had
  just marked locked were already in the results. Each was retried immediately, while its lock
  was by definition still held: double the work, double-counted skips. The retry list is now
  snapshotted before the walk begins.
- **As-you-type could not match the word being typed.** Every term was quoted exactly, so
  someone typing "pump st" searched for the literal word "st" and got nothing. The interim tier
  - the one whose entire purpose is to feel instant - stayed empty until the moment a word was
  finished. The last term is now a prefix, but only for that tier: turning every term into a
  prefix would make "cat" match "catastrophe" in a committed search.
- **An edit inside the filesystem's timestamp resolution was invisible.** Two writes in one tick
  produce identical mtimes; if the edit also preserves the size - an overtype, a corrected
  figure - the cheap tier said "unchanged" and the new contents never reached the index.
  Silently, permanently. NTFS and the Windows clock are coarser than ext4's, so the window is
  real on the target platform and absent on the development one. Files modified within two
  seconds are now always hashed, which during an index run is approximately none of them.
- `pytest`'s `--basetemp` sits inside the project, so pytest tried to collect its own scratch
  directory. `norecursedirs` now excludes it.

### Fixed
- Three wiring bugs found by reading the UI back rather than running it: `clicked` and
  `triggered` emit a `bool`, so binding a keyword-only slot to them would have raised
  `TypeError` the first time anyone pressed the button or F5; `finished` was connected inside
  the start handler, so the tenth index run would have refreshed the status bar ten times; and
  index roots were never persisted, so every restart forgot which folders to index. Settings
  that vanish on restart are not settings - they now live in `index_state`, with the index they
  describe.
- **The search cache handed out its stored object and then mutated it.** Stamping `from_cache`
  and `elapsed_ms` on a cached `SearchResponse` changed what every earlier caller was still
  holding, so a response somebody got two searches ago would silently start claiming it came
  from a cache it had not. diskcache pickles and so returns a fresh object; an in-memory cache
  does not, and the engine must not depend on which it was handed. Found by an acceptance test.
- **`request_stop()` did not stop anything.** The flag was set but the consumer loop never
  checked it, so a run continued to completion after being asked to stop - the UI's pause
  button would have done nothing, and the disk guard only worked by accident. Found by an
  acceptance test that stopped a run halfway and got a complete one.
- **The prune step never ran.** It was guarded on the same event that `run()`'s cleanup always
  sets, so "did we stop early?" was permanently true and deleted files were never removed from
  the index. Two meanings had been folded into one flag; they are now separate, and the
  distinction matters: an interrupted walk has not seen the whole corpus, so pruning after one
  would delete perfectly good rows.
- **Chunks ran ~35% over budget.** The chunker costed each word at 1 token while
  `estimate_tokens` valued a chunk at 1.35 tokens/word, so a chunk built to a 512-token budget
  measured 690 and bge-small truncated the tail in silence - the worst kind of bug, because
  nothing fails and search just gets quietly worse. `token_cost` is now the single definition
  and `estimate_tokens` is its sum, so the two cannot disagree. A length term was added for
  words that are not words: a base64 blob costed at 1.35 tokens would have blown any budget.
- `Document.page_for_offset` and `page_lookup` disagreed for an offset landing in the separator
  written between segments. The linear scan returned the document's last page; the binary search
  returned the correct one. Both now resolve by segment *start*.
- `test_docs_versioned.py` hung: `rglob` descends into `venv/Lib/site-packages` before filtering
  it out, which on a mounted drive takes long enough to look like a crash. It now prunes during
  the walk. 0.9s.
- **`app.cli extract PATH --json` failed** with "unrecognized arguments". `--json` and `--env`
  were declared on the parent parser, which in argparse means they are only accepted *before*
  the subcommand - not the order anyone types. Both now work either side, via a shared parent
  with `default=argparse.SUPPRESS`; without SUPPRESS the subparser writes its own default over
  the already-parsed global and `--json extract` silently stops being JSON.
- **The test suite crashed after passing.** Every test went green, then pytest raised
  `PermissionError: [WinError 5]` from `pytest_sessionfinish` while cleaning its temp root: it
  keeps a `pytest-current` junction under `%LOCALAPPDATA%\Temp`, and creating or resolving a
  junction needs a privilege a normal Windows account may not have. The suite passed and the
  process still exited non-zero. `--basetemp=.pytest_tmp` in `pyproject.toml` sidesteps the
  junction entirely and puts scratch I/O on the project's own drive.
- A binary file with a text extension was skipped with `ERR_NO_TEXT_LAYER`'s default advice,
  which talks about scanned documents and OCR - nonsense for a renamed database. It now carries
  advice about the extension instead. An error giving the wrong fix is worse than one giving none.

### Known gaps
- **`.msg` happy path is untested.** A valid `.msg` is an OLE compound document and cannot be
  synthesised in a fixture generator; only the corrupt and missing-library paths are covered.
  A real Outlook-saved sample would close this.
- **`extract-msg` breaks the wheel rule** at the top of `requirements.txt`. The package itself
  is a `py3-none-any` wheel, but its dependency `red-black-tree-mod` publishes no wheel at all.
  It is two pure-Python files with no C sources, so pip builds it locally in seconds and no
  compiler is needed - but `pip download --only-binary :all:` now fails on this tree, and a
  fully offline install needs the sdist cached. `.msg` support is optional and the import is
  guarded, so removing the pin degrades `.msg` to "unsupported" rather than breaking the app.

### Docs
- `BUILD_SPEC_V2.md` → **2.3** (Layer 2 build notes, the CLI entry point, acceptance boxes
  ticked and the two PST criteria left visibly open), `CHANGELOG.md` → **1.3**,
  `HANDOFF.md` → **1.2**, `README.md` → **1.3**, `tests/fixtures/README.md` → **1.1**.
- 0.3.3 release: `HANDOFF.md` → **6.5**, `docs/ORDER_REGISTER.md` → **1.32**, the new
  `docs/WORKORDER-202626191300-indexing-that-works.md` **1.2**, `CHANGELOG.md` → **4.23**.


### Planned
- Attachment recursion for `.eml` files on disk. PST attachments are extracted; loose `.eml`
  files still only record attachment names.
- Layer 3 - the indexing pipeline: walker, resumable queue, embedder.

---

## [0.3.2] - 2026-08-24

Continuity documents, so the project survives being moved, paused or handed on.

### Added
- **`HANDOFF.md`** - the pick-it-up-cold document: where everything lives, what works today,
  how to resume on a new machine, the decisions already made *with their reasoning*, the traps
  that have already caused real failures, and the genuinely open questions.
- **`docs/PROJECT_INSTRUCTIONS.md`** - the standing contract: the ten non-negotiables, how a
  layer gets built, testing and commit conventions, the release checklist, what is deliberately
  out of scope, and how to brief an AI assistant on the project.
- `tests/unit/test_handoff_current.py` - fails the suite if either document's **Applies to**
  version falls behind `VERSION`, or if `HANDOFF.md` loses a section someone resuming needs.
  Enforced rather than trusted to a checklist: a stale handoff is confidently wrong.

### Changed
- Release checklist in `docs/VERSIONING.md` now requires updating `HANDOFF.md`.

### Docs
- `HANDOFF.md` 1.0, `docs/PROJECT_INSTRUCTIONS.md` 1.0, `docs/VERSIONING.md` 1.2,
  `README.md` 1.2. All documents re-pointed at app v0.3.2.

---
## [0.3.1] - 2026-08-24

Troubleshooting infrastructure, and two scope questions answered in the spec.

### Added
- **Structured log folders.** `logs/app` (daily narrative), `logs/errors`
  (warnings and errors as JSON Lines), `logs/install` (installer transcripts),
  `logs/crash`, `logs/diagnostics`. A generated `logs/README.txt` explains each.
  The JSONL sink exists so 3,000 failures in a 100GB run can be *counted and grouped*
  rather than read.
- **`app.cli diagnose`** - one command producing a zip with the environment report, both
  store summaries, config, disk space, package versions, git state and recent logs. Every
  section is collected inside its own guard: a diagnostic that fails when things are broken
  would be worse than useless, so a broken section records its own failure and the bundle is
  still produced. Large logs contribute their tail rather than being dropped or bloating the zip.
- `docs/TROUBLESHOOTING.md` - written for a hobby programmer: what each log means, how to
  read an error line, and the common failures with their fixes.
- `app/core/winfs.py` - cloud placeholder detection for OneDrive and SharePoint sync folders.
- `ERR_CLOUD_ONLY` and `ERR_OUTLOOK_BUSY` error codes, both `SKIP_CONTINUE`.
- VS Code task: **Diagnose (bundle for troubleshooting)**.

### Changed
- **Email scope corrected in the spec.** "PST indexing" was too narrow. MAPI enumerates every
  store Outlook has open, so the same code path indexes the **live Exchange/M365 mailbox** as
  well as `.pst` archives. Documented with its real constraints: Outlook must stay running,
  Cached Exchange Mode governs what is local, and the indexer is strictly read-only.
- **OneDrive and SharePoint documented, with the trap named.** Both sync to ordinary local
  folders, so indexing them needs no API. But Files On-Demand placeholders download in full
  when read, so a naive walk would hydrate an entire cloud library. Placeholders are skipped
  by default; pinned files index normally.
- Installer transcripts moved to `logs/install/`.

---
## [0.3.0] - 2026-08-24

**Layer 1 complete.** All five acceptance criteria pass; 70 tests green.

### Added
- `app/storage/sqlite_store.py` - files, chunks, messages, FTS5, skip ledger, resumability
  cursor and the write generation. One connection with a write lock: WAL gives many readers
  alongside one writer, which is exactly this app's shape, so `database is locked` is avoided
  rather than retried around. `mark_skipped()` records why a file was skipped so a bad file is
  remembered, not raised. `search_bm25()` returns [] on a malformed query instead of throwing.
- `app/storage/vector_store.py` - LanceDB table lifecycle with an explicit Arrow schema, so
  the fixed vector width is enforced by Arrow itself and an empty index is inspectable rather
  than absent. Refuses a dimension mismatch on connect, which is what a changed `EMBED_MODEL`
  looks like, instead of silently poisoning every future search. The ANN index is only built
  past 100k rows, because a flat scan beats a badly trained index below that.
- `app/storage/migrations.py` - schema versioning independent of the app version. Refuses to
  open an index written by a newer build rather than corrupting it.
- `app.cli init` - creates and migrates both stores, safe to re-run. `stats` now reports both
  stores when they exist, and stays read-only when they do not.
- 17 Layer 1 tests, including a hard `os._exit(9)` mid-transaction to prove committed data and
  the cursor survive while uncommitted data does not.

### Fixed
- `set_message()` inserted NULL into `has_attach`, which is `NOT NULL`. Caught by the tests.
- `VectorStore` used `table_names()`, deprecated in lancedb 0.37; now uses `list_tables()`
  with a fallback so a version bump cannot silently break table detection.
- `git_describe()` swallowed its failure reason, hiding why `stats` printed no git line.
  It now reports the cause, and recognises git's "dubious ownership" refusal specifically,
  appending the exact `safe.directory` command that fixes it.

---
## [0.2.0] - 2026-08-24

**Layer 0 complete.** All four acceptance criteria from `BUILD_SPEC_V2.md` pass.

### Added
- `app/core/errors.py` - `AppError`, `ActionType`, and a registry covering all eight codes
  from the spec's recovery table plus `ERR_CONFIG_INVALID`, `ERR_CONFIG_MISSING`,
  `ERR_NOT_IMPLEMENTED` and `ERR_UNEXPECTED`. `guard()` converts anything escaping a worker
  boundary into an `AppError`, and passes precise errors through unflattened. Templates fill
  safely: a missing context key degrades the message rather than raising on an error path.
- `app/core/config.py` - typed `Settings` validated at startup, not at first use. Every path
  is created and proved writable before the app runs, so a disconnected drive fails
  immediately rather than three minutes into a 100GB index. Built on plain pydantic and a
  stdlib .env parser: `pydantic-settings` is a separate distribution and is not installed.
- `app/core/logging.py` - loguru with console (INFO) and rotating file (DEBUG, 10MB, 14 days)
  sinks. `error_code` is a structured field so Layer 5 can group thousands of skips by cause.
  `diagnose=False` deliberately: variable dumps would leak indexed file contents into logs.
- `app/core/single_instance.py` - Windows named mutex via ctypes rather than pywin32, because
  refusing to start must not depend on an optional dependency. POSIX fallback for tests.
- `app/cli.py` - `stats`, `doctor`, `lock` working; `index` and `search` declared and failing
  with `ERR_NOT_IMPLEMENTED` naming the layer that delivers them.
- 53 tests: unit coverage of errors and config, plus the four Layer 0 acceptance tests.
- VS Code project: `.vscode/{settings,launch,tasks,extensions}.json`,
  `SearchProject.code-workspace`, `pyproject.toml`, `requirements-dev.txt`, `docs/VSCODE.md`.
  Settings force `utf8bom` for PowerShell files, making the parse-time encoding failure
  impossible to reintroduce from the editor.

### Fixed
- `make_error()` raised `TypeError: got multiple values for argument 'component'` whenever an
  exception was converted, because `to_app_error` put `component` into `**context` where it
  collided with the positional parameter. Found by the tests, not in production.
- The installer's transcript recorded nothing at all from `doctor.py`. Native stdout was
  being swallowed; it is now routed through `Write-Host` like every other child process.

---
## [0.1.0] — 2026-08-24

First versioned state of the project. Environment and plan only; no application code yet.

### Added
- `install.ps1` — automated Windows installer, self-locating, with per-step
  **[R]etry / [C]ontinue / [A]bort** prompting on failure
- `requirements.txt` — dependency pins re-verified against PyPI, all with Windows wheels
- `doctor.py` — environment verification with a remediation for every failure;
  `--json` and `--quick` modes; required vs optional check distinction
- `BUILD_SPEC_V2.md` — layer-by-layer build plan (L0–L9) with acceptance tests,
  SQLite and LanceDB schemas, module layout, and a per-stage performance budget
- `app/` package skeleton matching the build spec, with layer ownership recorded in
  every module docstring
- `app/core/version.py` — single source of truth for the version, read from `VERSION`
- Versioning: `VERSION`, `CHANGELOG.md`, `docs/VERSIONING.md`, `.gitignore`, `.env.example`
- `tests/` skeleton with fixture folders for healthy and deliberately corrupt files

### Fixed
Faults found by the first real installer run, corrected rather than worked around:

- **`Install Git` failed with exit code `-1978335189`.** winget returns
  `UPDATE_NOT_APPLICABLE` when a package is already installed and current — not a failure.
  Every install step now carries a `-Verify` block; if the command works afterwards, the
  exit code is ignored.
- **`Could not open requirements file`.** The installer created a subfolder, `Set-Location`'d
  into it, and looked for files that were never there — and because it had been launched from
  `C:\Windows\system32`, that is where the project landed. The project folder is now the
  script's own folder (`$PSScriptRoot`), and `requirements.txt` and `doctor.py` are checked
  for in preflight before anything is installed.
- **Both model downloads raised tracebacks.** Pure cascade: pip never ran, so `fastembed`
  was not importable. Package install is a required step and now aborts by default.
- **`ollama pull mistral` stalled on `pulling manifest`.** A freshly installed Ollama has no
  service listening. The step now waits up to 60s for `127.0.0.1:11434`, starting
  `ollama serve` if needed, before pulling.
- **`doctor.py` was run from the wrong directory** and could not be found. Absolute paths
  throughout; `Push-Location`/`Pop-Location` around anything that must change directory.
- **Braille progress spinners rendered as `â ‹`.** Console output encoding is now set to UTF-8.
- **`continue` inside a `switch` inside a `while`** continued the switch, not the loop, so
  "Retry" would not have retried. Replaced with `if`/`elseif`.
- **`.env` was written with a UTF-8 BOM** by PS 5.1's `Set-Content -Encoding UTF8`, which
  breaks some `.env` parsers. Now written BOM-free.

### Changed
- **Dependency pins refreshed.** The draft pins were indicative and badly stale — notably
  `fastembed==0.4.2`, which predates the current `TextCrossEncoder` rerank API:
  PyQt6 6.7.1→6.11.0, lancedb 0.15.0→0.37.1, fastembed 0.4.2→0.8.0,
  pymupdf 1.24.10→1.28.2, python-docx 1.1.2→1.2.0, pywin32 306→312,
  networkx 3.3→3.6.1, pydantic 2.9.2→2.13.4, python-dotenv 1.0.1→1.2.3,
  loguru 0.7.2→0.7.3, tqdm 4.66.5→4.70.0, requests 2.32.3→2.34.2.
  Verified by round-trip, not just version number.
- **Free-space check reconciled.** The installer demanded 150GB while `doctor.py` checked
  for 250GB against the *current directory*. Both now check the drive holding `DATA_PATH`
  against 150GB, overridable via `-RequiredFreeGB`.
- **Model cache location made explicit.** Both `FASTEMBED_CACHE_PATH` and the `cache_dir=`
  constructor argument are set, so models cannot silently land in the user profile instead
  of the index drive.
- V1's layered build plan superseded — it assumed FastAPI, PostgreSQL and Qdrant.

[Unreleased]: https://example.invalid/compare/v0.1.0...HEAD
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
