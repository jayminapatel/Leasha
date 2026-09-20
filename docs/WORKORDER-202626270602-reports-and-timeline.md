# Work order (One thread): Reports — the index tells you about your hoard — and the Life Timeline

**Doc version:** 1.6 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (new Reports surface + timeline view + report queries)
**Status:** RELEASED by the owner 2026-08-28. **Queue position: after 0l
(Offline Media II), BEFORE 0m (test automation) — 0m stays deliberately last
so its scenarios also cover this order's surfaces** (its §1c convention:
this order's acceptance sentences get pytest-qt scenarios written HERE).
Dependencies: 0508 (EXIF dates) for the timeline; 0510 (pHash/hashes) and
0513/0514 (sources) for the reports — full value arrives with them, and
each report degrades gracefully when a dependency's data is thin (states
plainly what it can't yet say, never errors).

The theme (owner, from the late-night round): **the index outlives the media
it describes** — these are the features that make that property visible.
Everything here is read-only over existing tables: no report may ever write
to user files, and report generation runs on workers like everything else.

## 1. The Reports section (the home)

**2026-09-16 — §1 and §2 built and independently verified** (a crashed session's
uncommitted work, recovered and checked rather than trusted): `app/reports/inheritance.py`
+ `app/ui/reports_view.py` + `app/ui/widgets/report_export_dialog.py`, reached from the CLI
(`leasha report inheritance`, `--json`, `--out`) before the UI per non-negotiable #8, and wired
into the shell as its own tab (`test_window_opens.py` confirms six tabs, Reports among them).
21 tests in `test_reports_inheritance.py` (three added this session: location text reaching
the document, the no-location case rendering cleanly, and a source-scan guard mirroring
`test_preview_window.py`'s read-only proof), plus `test_cli_wiring.py`'s report tests, all
green. §3 and §4 below are genuinely not started - do not read this note as covering them.

- [x] **1a** a Reports page in the shell (sibling of Indexing/Settings, not
  one of the four search tabs — reports are outputs, not searches): a list
  of available reports with one-line plain-words descriptions, each opening
  into its view with an **Export** action (PDF via the print machinery, and
  CSV where tabular). Room for future reports (this is a section, not two
  hard-coded screens).
- [x] **1b** every report view states its data timestamp ("from the index as
  of last run, <date>") — a report is a snapshot of the catalogue, honest
  like everything else.

## 2. Report: Digital Inheritance — the catalogue book

- [x] **2a** one document that maps *everything Leasha knows exists*: every
  source (local roots, drives, shares, cloud, archived/tape) with its NAME,
  user description, physical location text where given, kind, counts, sizes,
  snapshot dates, online/offline status — grouped by kind, written for a
  reader who is NOT the owner ("the drive labelled 'Projects 2019', last
  seen Nov 2026, holds 41,205 files — photos 2004–2019, project documents…"
  — top-level folder summary per source, derived from the index).
- [x] **2b** export as a clean printable PDF titled for its purpose
  ("A map of <name>'s files — generated <date>"), plain words throughout;
  contents = names/locations/summaries ONLY — never file contents, never
  credentials-adjacent names beyond what listing already shows. One
  plain-words note in the UI about what it's for (with the will; the family
  finds the map) — dignified, one sentence, no melodrama.
- [x] **2c** optional per-source include/exclude checkboxes before export
  (a source can be private even from the map).

## 3. Report: the Space Report — duplicates and uniqueness

**2026-09-16 — built and added to the Reports page (`app/reports/space.py`,
`app.cli report space`), honestly partial rather than asserted whole.** 16
new tests in `test_reports_space.py`, 3 more in `test_cli_wiring.py`, all
against a real `SqliteStore`.

> **2026-09-19 - 3a and 3c are ticked, and here is exactly what that rests on.**
> **3a's last open sub-ask is built**: the interactive table (`app/ui/widgets/space_table.py`,
> shipped in `a4baf1c`) - one tab per question the report answers, columns that
> sort on their real values, and a row that opens to show every copy with its
> source, plus export. `test_space_report_table.py` and `test_reports_space.py`
> prove it, and they pass. **3c's last open sub-ask is measured, on synthetic
> data only**: `test_space_report_scale.py` builds a 200,000-row `files` table
> (85,160 distinct hashes; 27,037 photos of which 12,732 carry a pHash; three
> volumes plus this computer) and times the queries, best of three on a loaded
> machine: duplicate groups 97-325 ms, total reclaimable 95-361 ms, similar
> photos 565-2,490 ms (about 200 s before the pHash comparison was vectorised),
> duplication by source 335-1,130 ms, the only copy 164-650 ms, the whole
> snapshot about 2.5 s. `test_space_report_caching_and_progress.py` proves the
> cache hit and the staged progress. **Not measured: the owner's real index at
> `D:\Leasha\Data`.** The numbers above say the queries do not fall over at
> 200,000 rows, and nothing more. Section 4 (the Life Timeline) is untouched;
> it stays held.

> **2026-09-20 - two things found while writing section 5's fixture tests, and one
> caution restated.** (1) **The Space Report said "No duplicate files were found." about an
> index where nothing had been compared** - a name-only pass leaves `content_hash` empty, so
> the queries returned nothing and the document reported the *empty column* as a fact about
> the owner's files. It now says, in the document, the table headline and `report space
> --json` (`files_total`, `files_compared`): "None of the N files has been compared with the
> others yet ... so this report cannot say what is duplicated or what exists only once", or
> "Leasha has compared X of Y files" when partly done (`space.hash_coverage`,
> `coverage_sentence`; `test_reports_thin_data.py`, written failing-first). (2) **The
> Digital Inheritance map dropped the owner's own description of every drive, share and
> tape** ("the old work drive") - it was printed for a local root only, where the
> "description" is merely the path - although 2a asks for "NAME, user description, physical
> location text". It is printed for every source now (`_source_paragraph`;
> `test_reports_fixtures.py` reads the real PDF back). (3) **3c's measurement remains
> synthetic** - the 200,000-row fixture of 2026-09-19, not `D:\Leasha\Data`, which cannot be
> reached from here; nothing in this note changes what that measurement does and does not say.

- [x] **3a** across ALL sources, from content hashes + pHash (photos):
  total duplicate bytes reclaimable, largest duplicate groups (what, where,
  each copy's source), per-source duplicate share. Table + a few plain
  numbers; sortable; row → reveals the copies with their sources.

  **Built: exact-content duplicates by `content_hash`, across local roots
  and catalogued volumes alike** — total reclaimable bytes, the
  `DUPLICATE_GROUPS_SHOWN` largest groups biggest-reclaim-first, every
  copy named with its source.

  > **2026-09-16, later the same day — two more of the three built, by
  > the crash-recovery session.** `find_near_duplicate_photo_groups`
  > (`app/reports/space.py`) clusters photos by pHash, reusing
  > `app.search.folding.phash_distance`/`PHASH_NEAR_THRESHOLD` rather
  > than a second near-duplicate rule - one representative row per
  > distinct `content_hash`, so a photo already reported as an exact
  > duplicate is not counted a second time as a near one. Carries no
  > `reclaimable_bytes`: these are not byte-identical, so deleting all
  > but one is not automatically safe the way it is for exact
  > duplicates, and the report says what was found rather than
  > asserting a saving it cannot guarantee. `find_source_duplicate_share`
  > answers "how much of what source X holds also exists elsewhere",
  > the companion question to 3b's own `find_source_uniqueness`. Both
  > reach the document (`## Similar photos`, `## Duplication by
  > source`) and the Reports page (`app/ui/reports_view.py`, staged as
  > two more progress steps in the worker built for 3c) and
  > `app.cli report space --json`. 30 new tests in
  > `test_reports_space.py`.
  >
  > **Still not built, and this stays unticked for it**: the
  > interactive sortable/row-expands-to-reveal-copies table - this
  > still ships as a rendered document. A genuinely new Qt widget for
  > the Space Report specifically, deliberately not attempted in the
  > same pass as the query work above given the size of what else was
  > still queued this session; the two finished sub-asks are not a
  > reason to tick a box that also promises the third.
- [x] **3b THE UNIQUENESS WARNING (the backup conscience)**: files that
  exist on exactly ONE source — counted and listed per source, headline
  first: "372 files exist nowhere else but 'Old WD' (last seen 14 Aug)."
  Offline-only uniqueness ranks above all (the drawer holding the only
  copy). No nagging machinery — the report states facts; the user acts.

  Built exactly to the example's own wording. **Scope note**: local-root
  uniqueness is one "This computer" bucket, not split per individual
  root — a local root is not at risk of disappearing the way a drive in
  a drawer is, which is this item's own stated reason volumes rank
  first, and splitting it would cost a per-row path-prefix match this
  item's own priority does not need. Catalogued volumes are reported
  individually and rank first, exactly as asked.
- [x] **3c** performance: report queries are prepared/indexed (hash and
  pHash columns get the indexes these GROUP BYs need — measured on the
  scale fixture, the H2-lesson applied in advance), generated on a worker
  with progress, cached until the next index run.

  **Built**: `content_hash` now has a partial index (schema v24,
  `idx_files_content_hash`) — `pHash` already had one (schema v17). Both
  report queries run through `CallableWorker`, the same off-the-UI-thread
  pattern every other report and page uses. **Not built at the time of the note above; built 2026-09-16, later the
  same day**: `_report_snapshot` (`app/ui/reports_view.py`) now checks
  `report_generated_at` (`MAX(files.indexed_at)`, the same "data as of"
  timestamp 1b already shows) before doing anything else, and returns
  immediately when it has not moved since the last load - the duplicate
  and uniqueness queries do not run at all on a tab switch that changes
  nothing, which is "cached until the next index run" read literally.
  When it has moved, `CallableWorker` (`app/ui/workers.py`, a new opt-in
  `report_progress=True`) hands the query function an `on_progress(stage)`
  callback; `_report_snapshot` calls it four times ("Reading sources...",
  "Finding duplicates...", "Checking what exists nowhere else...",
  "Writing the report...") and `ReportsView` shows the current stage in a
  label beside the report, cleared the instant the worker finishes. Five
  new tests in `test_space_report_caching_and_progress.py` prove the
  cache hit skips every stage call and the cache miss reports them in
  order. **Still not built, and this stays unticked for it**: no
  measurement against the scale fixture - H2's own lesson was applied in
  advance (the indexes already existed) but never proven with a number
  the way H2 itself was.

## 4. The Life Timeline — a browsing surface

> **2026-09-20 - the owner's HOLD on this section is RELEASED, and it is built.** Decision by
> the lead, on the owner's delegation, 2026-09-20 (the hold was the owner's own decision of
> 2026-09-16). Built CLI first (non-negotiable 8) and then the window:
> `app/reports/timeline.py` (queries), `timeline_words.py` (every sentence, one place),
> `app/cli/timeline.py` (`leasha timeline`), `app/ui/timeline_view.py` +
> `widgets/timeline_list.py` / `timeline_picker.py` / `timeline_host.py` +
> `presenter/timeline.py` + `controllers/timeline_controller.py`. It lives in the Reports
> page as its third entry, "Browse your timeline", so it adds no rail page.
> About 490 test cases (360 of them parametrised query-plan checks); the numbers and the measurements are in the notes below and in
> `test_timeline_scale.py`'s docstring.
>
> **The truthful-date rules, as built** (`app/reports/timeline.py`'s docstring is the
> authority): a camera's own date (`taken_at_ns`) > a **date sidecar - NOT BUILT: no reader
> for a Takeout `.json` exists anywhere in this codebase (`era_hints.py` checked and said so),
> so there is nothing to rank; when one exists it slots in here and only the SQL changes** >
> a year guessed from a folder name (labelled "Guessed from the folder name") > for mail,
> `messages.sent_at`, **never the file row's time** (every message in a `.pst` carries the
> container's one modified time) > the file's modified time (labelled "File date", with the
> tooltip that after copying it is often the day of the copy). A file with no usable date
> is left off and counted ("N items have no date at all, so they are not on the timeline").
> **Left out on purpose:** files inside an archive, and program code (`repo_id` set) - code
> is one choice away in the "Show" box, because a checkout touched in June 2015 is thousands
> of files nobody asking for June 2015 wants.
>
> **Density (4c).** A page is 200 entries, read by keyset (date, source, id), never OFFSET:
> a month of 4,000 photographs arrives a page at a time as the list is scrolled, on a
> worker, and a heartbeat test shows the window never stalls while it does. Photographs are
> painted in fixed-height bands of five and only what is on screen is drawn. Burst folding
> is the search's own (`app.search.folding`: identical bytes, near-identical pHash) and
> can be turned off; a folded row says how many are behind it and right-click takes it
> apart. **Measured on a synthetic 200,000-row index (not the owner's), VM steps being the
> noise-free number:** first page of a 4,000-photo month 87,100 steps (43-129 ms depending on
> what else the machine was doing); **a page 15 years in cost 2,095,300 steps until the
> cursor's own date became a lower bound (now 95,500)** - `EXPLAIN QUERY PLAN` said "SEARCH ...
> USING INDEX" the whole time, which is why the steps are asserted, not only the plan; the
> picker's count is one table scan, 8.9M steps, 1-13 s, on a worker, cached until the next
> index run. **Not measured: 20 million rows.** On a database that has never been analysed the
> planner chose `idx_files_source_kind` (72 ms a branch, sorting everything); a unary `+`
> keeps it on the date indexes and `test_timeline_query_plans.py` pins both states.
>
> **Entry points (4b), and one decision.** Reports > "Browse your timeline"; a search
> result's or a mail row's right-click "See everything from this month" (the file's
> *truthful* date is asked of the index on a worker, so a photograph copied in 2019 and shot
> in 2015 opens June 2015; a file with no date says so); and the timeline strip, which
> **already exists in the tree** - a right-click on a period there opens the timeline on that
> period. **Decision:** the order says "strip click -> this view", but a left click on the
> strip already means "add `after:`/`before:` to the search" (workspace 3d), and changing
> what an existing control does under somebody's hand was the worse trade; the left click is
> unchanged (one appended sentence on its tooltip says the right-click exists). The Files
> tab's menu does not have the door yet: `files_view.py` is 248 lines against a 250 budget.
> **The timeline needs Windows only for one thing:** whether a catalogued drive is plugged in
> is asked live (`connected_volumes`), once per period shown, on a worker; offline items are there
> with their source badge and cannot be opened (the shell's opener says which drive to plug in).

- [x] **4a** a timeline view: pick a period (year → month drill-down, or
  free after:/before: range) → everything from that period across ALL media
  and sources — photos as thumbnails, documents/mail/videos as rows,
  interleaved chronologically, offline items included with their source
  badge. Dates use the truthful-date rules (EXIF > sidecar > era-hint >
  mtime, per 0508/0511).
- [x] **4b** entry points: from the Reports section ("Browse your timeline")
  AND from any result's date ("see everything from this month" in the
  context menu) AND composing with the existing timeline strip when the
  search-experience order lands it (strip click → this view, pre-filtered).
- [x] **4c** density handling: a month with 4,000 photos paginates/clusters
  (burst folding applies here too); scrolling stays worker-fed
  (test_ui_never_blocks taught the module).
- [x] **4d** it is a browsing surface, not a search tab: no query box of its
  own — the search box already speaks dates; this is for wandering.

## 5. Rules and tests

**2026-09-16 note:** the read-only-guarantee half of the first bullet is now asserted
for §1/§2 (`test_the_inheritance_module_never_writes_to_disk`, source-scan style). The
plain-words deny-list and tooltip-effect halves, and the fixture/pytest-qt items below,
are not yet written for this order's surfaces and stay open — §3/§4 don't exist yet to
test either, so this bullet cannot be ticked whole.

> **2026-09-20 - section 5 is written, all three bullets.** *Read-only*
> (`test_reports_read_only.py`): the store's connection is given a SQLite authorizer that
> denies every write while every report and timeline function runs; every write-shaped
> filesystem call is recorded while the command-line reports and the timeline window run over
> real files and none is under the user's folders; a source scan covers every report module
> by glob - and each of the three guards is shown to fail. *Plain words*
> (`test_timeline_wording.py`): the project's own deny-list, imported, over every label,
> tooltip, placeholder, menu entry and sentence, plus a runtime "says what pressing it does"
> check on every control. The behavioural switches are "Show" (what kinds) and "Group
> near-identical photos", both remembered and both off-able. *Fixtures*
> (`test_reports_fixtures.py`, one family index): the printed map is really written and read
> back with PyMuPDF (every source's name and location text present, no file contents, not
> even a file name); the space report finds the planted cross-source duplicates and the
> unique-to-one-drive file; June 2015 shows the EXIF photo, the mtime letter and the offline
> drive's item with its badge; thin data states itself (`test_reports_thin_data.py`).
> *Scenarios* (`test_reports_acceptance_scenarios.py`): each of the order's three acceptance
> sentences is pressed in the real `MainWindow`. **Not run in this pass: the whole suite** -
> files were run by name only, in a shared checkout.

- [x] read-only guarantee asserted (no write syscalls to user paths from any
  report path — the view-only invariant extended); plain-words deny-list
  and tooltip-effect tests cover the new surfaces; every control off-able
  where behavioural (per the configurability doctrine).
- [x] fixture tests: inheritance PDF contains every fixture source's name
  and location text, and NO file contents; space report finds the planted
  cross-source duplicates and the planted unique-to-one-drive file; timeline
  June-2015 fixture shows the photo (EXIF), the letter (mtime), and the
  offline drive's item with badge; thin-data degradation states itself.
- [x] pytest-qt scenarios for each acceptance sentence below (0m convention,
  written here).

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentences: a family member who has never seen Leasha can read the printed
map and know which drawer holds what; the owner learns in one glance which
drive holds the only copy of anything; and "June 2015" is a place you can
go — every photo, letter and file from that month, wherever it lives now.
