# Work order (One thread): Reports — the index tells you about your hoard — and the Life Timeline

**Doc version:** 1.3 · **Updated:** 2026-09-16 · **Applies to:** app v0.3.3
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

- [ ] **3a** across ALL sources, from content hashes + pHash (photos):
  total duplicate bytes reclaimable, largest duplicate groups (what, where,
  each copy's source), per-source duplicate share. Table + a few plain
  numbers; sortable; row → reveals the copies with their sources.

  **Built: exact-content duplicates by `content_hash`, across local roots
  and catalogued volumes alike** — total reclaimable bytes, the
  `DUPLICATE_GROUPS_SHOWN` largest groups biggest-reclaim-first, every
  copy named with its source. **Not built**: pHash near-duplicate
  matching for photos (two visually-identical but not byte-identical
  images), per-source duplicate share as its own number, and the
  interactive sortable/row-expands-to-reveal-copies table the item asks
  for — this ships as a rendered document instead, the same shape the
  Digital Inheritance report already uses, with every copy already
  named in the group's own paragraph rather than behind a click. Left
  unticked because three real sub-asks are missing, not because the
  built half is wrong.
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
- [ ] **3c** performance: report queries are prepared/indexed (hash and
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

- [ ] **4a** a timeline view: pick a period (year → month drill-down, or
  free after:/before: range) → everything from that period across ALL media
  and sources — photos as thumbnails, documents/mail/videos as rows,
  interleaved chronologically, offline items included with their source
  badge. Dates use the truthful-date rules (EXIF > sidecar > era-hint >
  mtime, per 0508/0511).
- [ ] **4b** entry points: from the Reports section ("Browse your timeline")
  AND from any result's date ("see everything from this month" in the
  context menu) AND composing with the existing timeline strip when the
  search-experience order lands it (strip click → this view, pre-filtered).
- [ ] **4c** density handling: a month with 4,000 photos paginates/clusters
  (burst folding applies here too); scrolling stays worker-fed
  (test_ui_never_blocks taught the module).
- [ ] **4d** it is a browsing surface, not a search tab: no query box of its
  own — the search box already speaks dates; this is for wandering.

## 5. Rules and tests

**2026-09-16 note:** the read-only-guarantee half of the first bullet is now asserted
for §1/§2 (`test_the_inheritance_module_never_writes_to_disk`, source-scan style). The
plain-words deny-list and tooltip-effect halves, and the fixture/pytest-qt items below,
are not yet written for this order's surfaces and stay open — §3/§4 don't exist yet to
test either, so this bullet cannot be ticked whole.

- [ ] read-only guarantee asserted (no write syscalls to user paths from any
  report path — the view-only invariant extended); plain-words deny-list
  and tooltip-effect tests cover the new surfaces; every control off-able
  where behavioural (per the configurability doctrine).
- [ ] fixture tests: inheritance PDF contains every fixture source's name
  and location text, and NO file contents; space report finds the planted
  cross-source duplicates and the planted unique-to-one-drive file; timeline
  June-2015 fixture shows the photo (EXIF), the letter (mtime), and the
  offline drive's item with badge; thin-data degradation states itself.
- [ ] pytest-qt scenarios for each acceptance sentence below (0m convention,
  written here).

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentences: a family member who has never seen Leasha can read the printed
map and know which drawer holds what; the owner learns in one glance which
drive holds the only copy of anything; and "June 2015" is a place you can
go — every photo, letter and file from that month, wherever it lives now.
