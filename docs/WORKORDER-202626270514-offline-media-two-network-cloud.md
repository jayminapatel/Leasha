# Work order (One thread): Offline Media II — network shares, cloud mounts, and the placeholder rules

**Doc version:** 1.1 · **Updated:** 2026-09-15 · **Applies to:** app v0.3.3
**Thread:** One thread (Storage + Index + Offline Media tab)
**Status:** RELEASED by the owner 2026-08-28. Requires 0513 (the tab and the
identity seam). **Phones (kind 4, MTP) are decided as removable drives but
PARKED — a separate future order; do not start them here.** Standing safety
rules bind hard in this order: credentials never touched; nothing downloads
by accident; the UI never waits on a network.

## 1. Network shares (kind 2)

**2026-09-15 — 1a-1c built and proven with real, passing tests, on this
Windows machine.** Reuses 202626270513's `volumes` table and pipeline
machinery unchanged (`kind="network"`), per this thread's instruction not to
force a second design onto the first. `app/core/volumes_win.py` gained
`resolve_unc` (mapped-letter → UNC, `WNetGetConnectionW`), `normalise_unc`
(UNC → share root), and `probe_unc_reachable` (hard-timeout reachability,
`ThreadPoolExecutor` submit-without-blocking-shutdown so a dead host's own
slow SMB timeout never propagates to the caller). `app/index/offline_media.py`
gained `identify_source`, which tries UNC, then a mapped letter, then an
ordinary drive - the letter is read only to resolve it and is never stored.
`connected_volumes`/`refresh_volume_statuses` now resolve `kind="network"`
rows too, and a network scan defaults `verify_hash=False` (1c).

**A real bug this found and fixed**: the CLI's `--scan` only checked
reachability for a drive before walking; a network path went straight to
the walker, which discovered an unreachable share the slow way - Windows'
own SMB connection timeout, **13+ seconds** against a dead host in testing
here, reported at the end as "0 files found" rather than as offline. Fixed
by probing with `probe_unc_reachable` (hard 3s budget) before cataloguing
or walking anything; the same real dead-host scan now fails in ~6s
(process startup + the 3s probe) with 1b's exact wording rather than a
confusing empty result.

**Not done, and 1a left unticked as a whole item because it is compound**:
the identity half - normalised UNC, letter resolved and discarded - is
built and proven; the "renamed server = new source, softened by structure-
match offer" half is an interactive UI prompt, not built, deferred with the
rest of §2's tab. 1d's tab help-line sentence is likewise UI text.
Command: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py -v`
→ **17 passed** (6 of them network-specific). No real network share exists
on this machine to confirm the *reachable* success path of `resolve_unc`/
`probe_unc_reachable` against - **(UNCONFIRMED: the positive case)**, noted
rather than guessed past; the negative/unreachable case (the one 1b/1c are
actually about) is verified for real. Commit `003a90a`.

- [ ] **1a** identity = normalised UNC path; a mapped `Z:` is resolved to UNC
  at add-time and **never stored** (the drive-letter rule's network twin).
  Renamed server = new source, softened by structure-match offer ("is this
  *Old NAS* at a new address?" — assist, never assume).
- [x] **1b** credentials NEVER: user connects via Windows as they always do;
  unreachable/denied → "offline — not signed in or not reachable", plain
  words, no prompt from Leasha ever (safety invariant).
- [x] **1c** network scan defaults: hash verification OFF (mtime/size
  settling — SMB hashing is prohibitive); availability probes hard-timeout
  on workers; nothing network-touching ever runs on the UI thread (extend
  the scanner test to the new modules).
- [ ] **1d** the decommission case documented in the tab's help line: scan a
  share before a server is switched off or access is lost — searchable
  forever. One honest sentence: the catalogue holds what *this account*
  could read on scan day.

## 2. Cloud mounts (kind 3 — Google Drive first, via Drive for Desktop)

- [ ] **2a** V1 is ONLY through the vendor's own streaming mount (G:):
  **names-only catalogue by default** — placeholder metadata reads with zero
  downloads (this is NAME_ONLY's shape exactly). Identity = the account as
  the mount exposes it. Open action may go to the browser (the source whose
  files open from anywhere).
- [ ] **2b THE TRAP, enforced**: reading placeholder content HYDRATES
  (downloads). A scan must never silently pull 500GB onto a 512GB laptop —
  `cloudstub`'s placeholder detection is the guard; content indexing is an
  explicit per-folder opt-in with a plain-words download warning and a size
  cap. Test: names-only scan of a placeholder fixture performs zero content
  reads (asserted at the file-open level).
- [ ] **2c** API connectors (no Desktop client): **DEFERRED INDEFINITELY by
  owner decision** — OAuth + network code + connector treadmill + bends the
  load-bearing privacy paragraph. Recorded here so it is a decision, not a
  gap. The fair ask is "install Google's own Drive for Desktop."

## 3. The per-file placeholder model (applies to NORMAL roots too —
OneDrive Files On-Demand etc.)

- [ ] **3a** the walker reads the cloud-files attributes it already stats
  (RECALL_ON_DATA_ACCESS/OFFLINE/pinned): hydrated file = plain local file,
  full index; placeholder = name-only row, "online-only, content pending" —
  NEVER read placeholder bytes.
- [ ] **3b** transitions: placeholder→hydrated (user opened it) → next
  incremental run content-indexes it (the H1 held-pass shape — the index
  deepens along what the user actually touches); deliberate "index contents
  of this folder" opt-in hydrates on purpose, capped + warned.
- [ ] **3c** hydrated→DEHYDRATED (OS frees space): **KEEP all extracted
  content/chunks/vectors**; the row flips to online-only. Prune/change logic
  is explicitly taught: dehydration is neither deletion nor modification
  (size-on-disk changes; the logical file does not). Test: dehydrate a
  fixture → content still searchable, zero rows pruned.
- [ ] **3d** results badge "online-only — opening will download", riding the
  0513 §3a decoration path.

## 3b. ADDED by owner 2026-08-28 — tape and the archived-source kind

- [ ] **3b-1 Kind 5: "manual/archived source" — the scan-before-archive
  workflow.** "Mark as archived" detaches any catalogued source or folder
  into a source whose identity is just a **name + free-text location**
  ("LTO-7 tape B-0042, fire safe, IT room"): Rescan disabled (nothing to
  reconnect), Browse and Delete remain, snapshot date preserved. Results
  say "on tape B-0042 (archived Mar 2024)". Generalises to DVDs, destroyed
  drives, media handed to third parties — the catalogue-of-record.
- [ ] **3b-2 LTFS tapes** mount as filesystems and work as kind-1 volumes
  already, with ONE rule: content scans of a volume whose filesystem
  reports LTFS (or a "sequential medium" flag the user can set) process
  files in **on-tape order** with a plain-words warning ("this reads the
  tape end-to-end") — random-access walker order on tape is minutes per
  seek. Names-only cataloguing stays instant (LTFS index).
- [ ] **3b-3** proprietary backup formats (Veeam/NetBackup/tar-on-tape)
  are OUT by doctrine — the backup product is the generating system; the
  supported path is cataloguing the staging folder before the tape write,
  which 3b-1 completes. One sentence in the tab's help says exactly this.

## 4. Tests (beyond those inline)

- [ ] UNC identity: mapped-letter add stores UNC; letter remapped → same
  source.
- [ ] share-offline immunity mirrors 0513's (never prune, never wait).
- [ ] mixed folder fixture (hydrated + placeholder): per-file treatment
  exactly as 3a; the transition tests both directions.

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentences: a share scanned before decommission stays searchable years later;
a streamed Google Drive is findable by name with zero bytes downloaded; and
Windows freeing disk space never costs Leasha a word it had already read.
