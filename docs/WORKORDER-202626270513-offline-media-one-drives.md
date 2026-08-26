# Work order (One thread): Offline Media I — drives in drawers, findable forever

**Doc version:** 1.0 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
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

- [ ] **1a** `volumes` table: volume GUID/serial (primary), hardware serial
  via WMI (reformat recognition — offer "this looks like <name> reformatted",
  assist never assume), fs label, **user's name + description**, first/last
  seen, size, snapshot date.
- [ ] **1b** files on catalogued volumes store `(volume_id, relative_path)`;
  resolution to a real path happens at the last moment via the current mount
  point (letter OR folder mount), every time — open, reveal, rescan. Fixed
  internal disks keep absolute paths untouched (containment; owner's install
  grandfathered — the standing regression fixture).
- [ ] **1c** every set that dedups/prunes/walks keys on (volume_id,
  relative_path) for these files — an absolute-path comparison anywhere is
  the bug class this design exists to kill (test: same file seen as E: then
  F: is ONE file).
- [ ] **1d** offline ≠ deleted, formalised: rows on offline volumes are never
  pruned, never re-walked, carry offline status (the H8 precedent, now a
  first-class state).
- [ ] **1e** rescan efficiency: H1 settling for unchanged files, and
  **hash-match-means-move** — same content hash at a new relative path moves
  the rows instead of re-extracting (drives reorganised elsewhere are the
  common case).

## 2. The Offline Media tab

- [ ] **2a** source list: name, status (online as F: / last seen date —
  letter shown as transient fact only), size/counts, snapshot date; verbs
  Scan/Rescan/Delete. Status checked passively on panel refresh — no device
  watcher, no events.
- [ ] **2b** first Scan asks the name ("Give this drive a name you'll
  remember") and description; plain words throughout, tooltips state effects
  (standing rules).
- [ ] **2c** Delete = the product's one deliberate deletion: full cascade
  (files/chunks/FTS/vectors — the batched H7/H8 machinery), confirmation
  stating counts and the crucial sentence: *"This removes the catalogue from
  Leasha's index. Nothing on the drive itself is touched."*
- [ ] **2d** BitLocker-locked volume = offline-with-reason ("locked").

## 3. Search and browse

- [ ] **3a** results on offline volumes: "on **<name>** (offline, scanned
  <date>) — plug it in to open"; online → normal open via resolution (1b).
  The offline decoration rides the existing missing-path worker route.
- [ ] **3b** preview from the index works offline (the mail synthetic-path
  pattern: stored text + segments; images show cached thumbnail when 0510's
  thumbnails exist).
- [ ] **3c** `/on` operator: volume names with counts (slash-menu machinery);
  Files tab gains the volume filter for browsing a drive in a drawer.

## 4. Tests

- [ ] letter roulette: catalogue as E:, remount fixture as F: → open/rescan/
  dedup all correct; nothing anywhere stored the letter (grep-shaped guard).
- [ ] offline immunity: full run with the volume absent → zero prunes, zero
  changes to its rows.
- [ ] delete cascade: counts stated, everything gone, drive bytes untouched
  (asserted).
- [ ] reorganised-drive rescan: moved files move (1e), extraction count ≈ 0.
- [ ] the manual guarantee: plugging in any volume triggers NO index
  activity and NO prompt (asserted — the borrowed-stick test).

## Done means

Change + tests + suite green + committed by name; CHANGELOG; rescan/scan
rates on the fixture recorded here. Acceptance sentence — the killer case:
describe a file from memory, be told it's on **Projects 2019** in a drawer,
scanned 12 Nov, and see its text without touching the drawer.
