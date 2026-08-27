# Work order (One thread): Reports — the index tells you about your hoard — and the Life Timeline

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
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

- [ ] **1a** a Reports page in the shell (sibling of Indexing/Settings, not
  one of the four search tabs — reports are outputs, not searches): a list
  of available reports with one-line plain-words descriptions, each opening
  into its view with an **Export** action (PDF via the print machinery, and
  CSV where tabular). Room for future reports (this is a section, not two
  hard-coded screens).
- [ ] **1b** every report view states its data timestamp ("from the index as
  of last run, <date>") — a report is a snapshot of the catalogue, honest
  like everything else.

## 2. Report: Digital Inheritance — the catalogue book

- [ ] **2a** one document that maps *everything Leasha knows exists*: every
  source (local roots, drives, shares, cloud, archived/tape) with its NAME,
  user description, physical location text where given, kind, counts, sizes,
  snapshot dates, online/offline status — grouped by kind, written for a
  reader who is NOT the owner ("the drive labelled 'Projects 2019', last
  seen Nov 2026, holds 41,205 files — photos 2004–2019, project documents…"
  — top-level folder summary per source, derived from the index).
- [ ] **2b** export as a clean printable PDF titled for its purpose
  ("A map of <name>'s files — generated <date>"), plain words throughout;
  contents = names/locations/summaries ONLY — never file contents, never
  credentials-adjacent names beyond what listing already shows. One
  plain-words note in the UI about what it's for (with the will; the family
  finds the map) — dignified, one sentence, no melodrama.
- [ ] **2c** optional per-source include/exclude checkboxes before export
  (a source can be private even from the map).

## 3. Report: the Space Report — duplicates and uniqueness

- [ ] **3a** across ALL sources, from content hashes + pHash (photos):
  total duplicate bytes reclaimable, largest duplicate groups (what, where,
  each copy's source), per-source duplicate share. Table + a few plain
  numbers; sortable; row → reveals the copies with their sources.
- [ ] **3b THE UNIQUENESS WARNING (the backup conscience)**: files that
  exist on exactly ONE source — counted and listed per source, headline
  first: "372 files exist nowhere else but 'Old WD' (last seen 14 Aug)."
  Offline-only uniqueness ranks above all (the drawer holding the only
  copy). No nagging machinery — the report states facts; the user acts.
- [ ] **3c** performance: report queries are prepared/indexed (hash and
  pHash columns get the indexes these GROUP BYs need — measured on the
  scale fixture, the H2-lesson applied in advance), generated on a worker
  with progress, cached until the next index run.

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
