# Work order (One thread): Offline Media I — drives in drawers, findable forever

**Doc version:** 1.3 · **Updated:** 2026-09-15 · **Applies to:** app v0.3.3
**Thread:** One thread (Storage core + Index + new tab UI + Search)
**Status:** RELEASED by the owner 2026-08-28. **The deepest storage change in
the batch — do NOT interleave with other pipeline orders.** Requires 0508
landed (the ladder makes picture-heavy drive scans affordable — owner:
slow media scans accepted). Kinds 2–4 (network/cloud/phones) are order 0514
and beyond; **this order is drives only.**

## Owner's model (settled — do not relitigate)

**Fully MANUAL**: nothing happens to a removable drive unless the user
pressed the button. Verbs: **Scan · Rescan · Delete** — nothing else. No
arrival prompts, no auto-anything (trust/kid-proof/borrowed-stick answer).
Always-connected drives use normal roots — two doors, the user picks by
picking. Scans are dated snapshots. **Drive letters are NEVER stored — assume
the letter is different every plug-in** (owner's explicit requirement).

## 1. Identity and storage (the deep part)

**2026-09-15 — 1a-1e built and proven with real, passing tests, on this
Windows machine.** `volumes` table (schema migration v17), shared with
202626270514 so a later kind never forces a second design onto this one.
`app/core/volumes_win.py` (GUID via `GetVolumeNameForVolumeMountPointW`,
label/serial via `GetVolumeInformationW`, advisory hardware serial via
`Get-PhysicalDisk`) verified against this machine's real C:/D: drives —
`tests/unit/test_offline_media.py::
test_the_real_fixed_drive_on_this_machine_round_trips_through_its_guid`.
Letter-free identity (`leasha-volume://<volume_id>/<relative_path>`, never a
letter) proven end to end through a real `Pipeline` run in
`test_the_same_volume_walked_at_two_mount_points_is_one_row` (the order's
own "catalogue as E:, remount as F:" line) and
`test_two_different_volumes_at_the_same_letter_never_collide`. Offline
immunity in `test_a_run_elsewhere_does_not_prune_an_offline_volumes_rows`.
Two real bugs found and fixed along the way: an extractor's `Document.key`
already defaulted to the raw path before the volume-safe substitution ran,
and the incremental "is this already indexed" lookup in
`Pipeline._classify` looked up the real path rather than the synthetic key,
so every file on a catalogued volume was silently re-extracted on every
rescan until fixed — see `test_a_moved_file_is_repaired_without_re_extraction`.
Command: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py -v`
→ **11 passed**. Commits `03c4568`, `f4f6a58`.

- [x] **1a** `volumes` table: volume GUID/serial (primary), hardware serial
  via WMI (reformat recognition — offer "this looks like <name> reformatted",
  assist never assume), fs label, **user's name + description**, first/last
  seen, size, snapshot date.
- [x] **1b** files on catalogued volumes store `(volume_id, relative_path)`;
  resolution to a real path happens at the last moment via the current mount
  point (letter OR folder mount), every time — open, reveal, rescan. Fixed
  internal disks keep absolute paths untouched (containment; owner's install
  grandfathered — the standing regression fixture).
- [x] **1c** every set that dedups/prunes/walks keys on (volume_id,
  relative_path) for these files — an absolute-path comparison anywhere is
  the bug class this design exists to kill (test: same file seen as E: then
  F: is ONE file).
- [x] **1d** offline ≠ deleted, formalised: rows on offline volumes are never
  pruned, never re-walked, carry offline status (the H8 precedent, now a
  first-class state).
- [x] **1e** rescan efficiency: H1 settling for unchanged files, and
  **hash-match-means-move** — same content hash at a new relative path moves
  the rows instead of re-extracting (drives reorganised elsewhere are the
  common case).

## 2. The Offline Media tab

**2026-09-15 — 2a-2d built and proven.** `app/ui/offline_media_view.py`
(the tab: `QTreeWidget` list, Scan/Rescan/Delete, `set_busy` in place of a
progress bar — 2a has none), `app/ui/widgets/offline_media_dialogs.py`
(`ScanNameDialog` for 2b, `DeleteVolumeDialog` for 2c), and the shared
CLI/UI orchestration core added to `app/index/offline_media.py`
(`scan_new_source`, `rescan_source`, `run_scoped_pipeline`, `find_volume`)
so a Scan typed on the console and one clicked in the window run the
identical `Pipeline` — `cli.py`'s own `_run_offline_media_pipeline` now
delegates to `run_scoped_pipeline` rather than duplicating it. `shell.py`
wires the view's `scan_requested`/`rescan_requested`/`delete_requested`
signals to three new handlers, each a `CallableWorker` under the window's
`GUI`-owned run lock, mirroring `_start_indexing`'s own split between
"the view decides nothing about storage" and "the window owns the store".

2d: `app/core/volumes_win.is_bitlocker_locked` (a `Get-BitLockerVolume`
PowerShell probe, the same shape `hardware_serial_for_root` already uses),
wired into `refresh_volume_statuses` only. **A first pass wired it into
`connected_volumes` instead, by mistake** — that function sits under
`resolve_file_path`, which every pipeline walk and every offline search
decoration goes through, and a PowerShell subprocess per call turned an
18-test file into one that would not finish. Found by the project's own
prescribed method: `widget.grab()`-driven testing stalled, `--timeout`
dumped the stack at `wait_while_throttled`, and the fix — BitLocker
checked only in the explicitly-infrequent "on panel refresh" path 2a
already names — is now regression-guarded
(`test_connected_volumes_never_probes_bitlocker`).

Verified with real, passing tests — `venv\Scripts\python.exe -m pytest
tests/unit/test_offline_media.py tests/unit/test_offline_media_view.py -v`
→ **44 passed**, including a `refresh()` round-trip against a real
`SqliteStore` off a worker, every button's enabled state for
ONLINE/OFFLINE/LOCKED, the Scan dialog's empty-name refusal, and
`widget.grab()` used to catch and fix a real layout bug (the LOCKED status
text was long enough to push Size/Files/Scanned off screen — shortened,
the explanation moved to a tooltip). No visible progress bar exists for
2a's plain "status line + disabled buttons" — deliberately, matching the
order's own plainness rather than the fuller Indexing-page treatment.

- [x] **2a** source list: name, status (online as F: / last seen date —
  letter shown as transient fact only), size/counts, snapshot date; verbs
  Scan/Rescan/Delete. Status checked passively on panel refresh — no device
  watcher, no events.
- [x] **2b** first Scan asks the name ("Give this drive a name you'll
  remember") and description; plain words throughout, tooltips state effects
  (standing rules).
- [x] **2c** Delete = the product's one deliberate deletion: full cascade
  (files/chunks/FTS/vectors — the batched H7/H8 machinery), confirmation
  stating counts and the crucial sentence: *"This removes the catalogue from
  Leasha's index. Nothing on the drive itself is touched."*
- [x] **2d** BitLocker-locked volume = offline-with-reason ("locked").

## 3. Search and browse

**2026-09-15 — started, not finished. None of 3a-3c ticked below** because
each is real but partial; what is actually built and proven, so the next
session does not have to re-discover it by reading the diff:

- **`volume_id`/`relative_path` now travel the whole way through a search
  result** — `SearchResult` (`app/search/engine.py`), every SELECT in
  `keyword.py` and `vector.py`, and `ResultRow` (`app/ui/presenter.py`).
  Nothing downstream can tell a catalogued-volume row from an ordinary one
  without this, and before this order nothing did.
- **3a, the tooltip half.** `presenter.decorate_results` now also computes
  `offline_volume_marks` off-thread (`connected_volumes`, never on the
  interface thread) and `results_view.py`'s tooltip reads "on **<name>**
  (offline, scanned <date>) — plug it in to open" verbatim for an offline
  row, straight from `presenter.offline_volume_note`. **Not done**: this is
  the tooltip only — there is no permanently visible badge painted on the
  row itself the way the order's wording could also be read. Extending
  `result_delegate.py`'s paint pass and `group_subtitle` to show it inline
  needs its own session; the tooltip already says the exact sentence and is
  real, tested behaviour, not a placeholder.
- **3a, the open half, done and correct.** `shell._open_result` now checks
  `row.volume_id`; a volume-backed row resolves through `resolve_file_path`
  (1b) on a worker before opening, instead of trying to open the
  letter-free storage key directly, which is what it did before this
  session and would have surfaced as "file missing" for a file sitting
  right there with the drive plugged in. This half needed no UI decision
  and is complete.
- **3b, not started.** Preview-pane text for an offline row — the mail
  synthetic-path pattern this item names — has not been touched.
  `app/ui/preview_loader.py` was read only far enough to confirm the
  pattern exists for mail; wiring it for a volume row is open.
- **3c, half done.** The `/on` operator (`on:`/`volume:`/`drive:`, comma-
  separated, negatable) is built and proven end to end — parser
  (`query.py`), the SQL filter with the same `OR volume_id IS NULL` guard
  `-repo:` needed (`filters.py`), the `/` menu's real value list
  (`sqlite_store.py`'s `on` `_ValueShape`), and the catalogue entry
  (`commands.py`) — and every tab already offers and honours it, the same
  way every tab already offers `/repo`. **Not done**: "Files tab gains the
  volume filter for browsing a drive in a drawer" — no control was added to
  `files_view.py`. The operator works if typed; there is no picker.

Command: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py
tests/unit/test_offline_media_view.py -v` → **44 passed**, including the
`/on` parser/filter/exclusion tests and the tooltip/open-resolution
coverage above. Full regression run alongside (`test_ui_never_blocks.py`,
`test_window_opens.py`, `test_presenter.py`, `test_results_view.py`,
`test_result_delegate.py`, `test_search_view.py`, `test_commands.py`,
`test_command_subsets.py`, `test_query.py`, `test_slash_context.py`,
`test_eight_year_old.py`, `test_review_section_three.py`,
`test_engine_image_lane.py`, `test_search_policy.py`, `test_folding.py`,
`test_rerank.py`, `test_search_images.py`) all green. Two pre-existing
failures found and confirmed unrelated (same failure against an unmodified
checkout, via `git stash`): `test_query_plans.py::test_filter_only_browse_
neither_scans_nor_sorts` (a SQLite query-plan assertion) and
`test_presenter.py::test_every_qt_view_keeps_its_logic_in_the_presenter`
(`indexing_view.py` already over the 250-line guard) — flagged for whoever
owns those areas, not fixed here.

`test_window_opens.py`'s tab-order and tab-count assertions were updated
for the new tab (Search, Files, Mail, Code, **Offline Media**, Indexing,
Settings — verified against the real `insertTab` offsets, not assumed) —
the same kind of update those tests needed when Mail and Code were
themselves added.

- [ ] **3a** results on offline volumes: "on **<name>** (offline, scanned
  <date>) — plug it in to open"; online → normal open via resolution (1b).
  The offline decoration rides the existing missing-path worker route.
- [ ] **3b** preview from the index works offline (the mail synthetic-path
  pattern: stored text + segments; images show cached thumbnail when 0510's
  thumbnails exist).
- [ ] **3c** `/on` operator: volume names with counts (slash-menu machinery);
  Files tab gains the volume filter for browsing a drive in a drawer.

## 4. Tests

**2026-09-15 — all five proven** in `tests/unit/test_offline_media.py`
(18 passed; `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py -v`).
"The manual guarantee" is proven the way a static claim can be: a
grep-shaped guard (`test_nothing_outside_the_cli_command_and_tests_calls_a_scan`)
asserts nothing outside `app.cli`'s own offline-media handlers and their own
definitions can call `upsert_volume`/`identify_source` at all - no device
watcher exists to plug a real drive into and watch for a reaction, so this
is the checkable half: no code path *could* fire on its own, now that a
CLI command exists for it to be tested against.

- [x] letter roulette: catalogue as E:, remount fixture as F: → open/rescan/
  dedup all correct; nothing anywhere stored the letter (grep-shaped guard).
- [x] offline immunity: full run with the volume absent → zero prunes, zero
  changes to its rows.
- [x] delete cascade: counts stated, everything gone, drive bytes untouched
  (asserted).
- [x] reorganised-drive rescan: moved files move (1e), extraction count ≈ 0.
- [x] the manual guarantee: plugging in any volume triggers NO index
  activity and NO prompt (asserted — the borrowed-stick test).

## Done means

Change + tests + suite green + committed by name; CHANGELOG; rescan/scan
rates on the fixture recorded here. Acceptance sentence — the killer case:
describe a file from memory, be told it's on **Projects 2019** in a drawer,
scanned 12 Nov, and see its text without touching the drawer.
