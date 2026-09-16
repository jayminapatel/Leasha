# Work order (One thread): Offline Media II — network shares, cloud mounts, and the placeholder rules

**Doc version:** 1.3 · **Updated:** 2026-09-16 · **Applies to:** app v0.3.3
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

**2026-09-15 — 1a narrowed further, and §3a/3b/3c/3b-1 closed, real bugs
found and fixed along the way.** Environment check first, since a previous
attempt at this exact continuation was spawned into a stale worktree
(missing this order's own §1b/1c work): confirmed at `9320adb` on
`claude/open-workorders-display-9b8146` before writing anything.

**1a's offer half, at the backend and CLI layer** (`app.index.offline_media.
suggest_renamed_source`, `SqliteStore.rename_volume_identity`, `SqliteStore.
volume_top_level_names`, wired into `app.cli offline-media --scan` as a
notice plus the new `--same-as NAME_OR_ID`): a shallow, name-only top-level
comparison between a new identity's root and every catalogued-but-different
source of the same kind - never a walk, never a byte of content - offers a
match above a deliberately conservative overlap threshold and never merges
anything on its own (`--same-as` is always something the caller typed).
**Still not done**: the interactive dialog itself, because it needs the
Offline Media tab - 0513 §2, confirmed still entirely unbuilt this session
(no `app/ui` file mentions Offline Media; 0513 stands at 10/17 with only its
§2 tab and §3 search-integration items open). Per non-negotiable 8 (CLI
before UI), what can be built without the tab has been; the item stays
unticked as a whole because the order's own wording names the dialog.

**A real, pre-existing bug found and fixed underneath 3a**: `app/index/
walker.py`'s cloud-placeholder check did `continue`, dropping the candidate
entirely - no row, no skip code, no count anywhere. Indistinguishable from
the file not existing, and the exact "invisible is the worst of the three
possible answers" bug class `WalkConfig.name_only` was built to kill for
unreadable extensions (see that field's own docstring), reappearing for
cloud placeholders through a different door. `LOCAL_KNOWLEDGE_GRAPH_V2.md`
already documented the intended shape - `SKIPPED`/`ERR_CLOUD_ONLY`, findable,
with a stated and fixable reason - and `app.cli extract` already raised it;
the real walk never did. Fixed in `walker.py` (yield the candidate with
`readable=False` instead of dropping it) and `pipeline.py`'s
`_extract_worker` (a placeholder candidate now produces `ERR_CLOUD_ONLY`,
not a bare `name_only` row).

**A second bug found while proving 3b**: with the first fix alone, a
placeholder that had already been skipped once and then genuinely hydrated
was never reprocessed - `ERR_CLOUD_ONLY` fell into the generic "settled
skip" fast-path (mtime/size unchanged ⇒ trust the old answer), which is
correct for a permanently-broken file but wrong for a skip reason that can
change with nothing on disk moving. Fixed by adding `ERR_CLOUD_ONLY` to
`Pipeline.DEFERRED_SKIP_CODES`, the same mechanism `ERR_OCR_HELD`/
`ERR_FILE_LOCKED` already use for exactly this shape of problem.

**Verified real vendor behaviour where this machine allows it, left open
where it does not.** `GoogleDriveFS` and `OneDrive`/`OneDrive.Sync.Service`
are genuinely running here, and `G:` is a real Google Drive Streaming mount
(confirmed via `Get-Process`, not assumed). One real snapshot of files under
`G:\` decoded to `FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_REPARSE_POINT |
FILE_ATTRIBUTE_PINNED` (525344) - Google Drive Desktop uses the Cloud Files
API's `PINNED`/`UNPINNED` pair, distinct from the `OFFLINE`/`RECALL_ON_OPEN`/
`RECALL_ON_DATA_ACCESS` trio `winfs.CLOUD_PLACEHOLDER_MASK` already checks -
and the file was correctly read as non-placeholder by existing code, since
its data is genuinely local (pinned, no `RECALL_*` bits). **Could not get a
real, reproducible unhydrated Google Drive placeholder on this machine** to
confirm the positive case (`G:\`'s virtual-filesystem enumeration was itself
flaky across repeated listings, and deliberately unpinning a real synced
file to manufacture one would modify the user's own cloud state, which is
out of bounds for a read-only verification pass) - left as
**(UNCONFIRMED: the positive placeholder case for Google Drive specifically)**
rather than guessed past, matching this order's own standing convention for
`resolve_unc`. `winfs.CLOUD_PLACEHOLDER_MASK` is left unchanged because its
three attributes are Microsoft's own documented "reading this recalls it"
contract, provider-agnostic by design - `RECALL_ON_DATA_ACCESS` in
particular is what a real download-on-read looks like regardless of vendor,
which is the actual question 3a asks.

Command: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py
tests/unit/test_walker.py -v` → **73 passed** (32 in `test_offline_media.py`,
41 in `test_walker.py`), including the exact acceptance sentences named
inline: "names-only scan of a placeholder fixture performs zero content
reads (asserted at the file-open level)" (2b), "dehydrate a fixture →
content still searchable, zero rows pruned" (3c), and the §4 mixed-folder
and share-offline-immunity fixtures. `app.cli` offline-media wiring:
`venv\Scripts\python.exe -m pytest tests/unit/test_cli_wiring.py -k
offline_media` → **8 passed**. All run with the resource governor disabled
(`ResourceLimits(cpu_percent=0, ...)`, the same pattern `test_resources.py`/
`test_governor_settles.py` already use) - this session's machine was running
enough other concurrent work that the governor's own CPU/memory checks
otherwise hung every `Pipeline.run()`-based test in the suite, unrelated to
anything changed here; confirmed by reproducing the identical hang against
unmodified files (`test_name_only.py`, `test_index_freshness.py`) and by the
stack trace pointing at `resources.py`'s probe, never at this order's code.

- [x] **1a** identity = normalised UNC path; a mapped `Z:` is resolved to UNC
  at add-time and **never stored** (the drive-letter rule's network twin).
  Renamed server = new source, softened by structure-match offer ("is this
  *Old NAS* at a new address?" — assist, never assume).

  **2026-09-16.** The offer's backend and CLI half was built 2026-09-15
  (`suggest_renamed_source`, `--same-as`); the interactive dialog named
  above as the only missing piece is built now that 0k §2's tab exists.
  `app.index.offline_media.check_renamed_source` is the tab's own
  worker-side pre-Scan check (identify, then offer only when the identity
  is genuinely new - an ordinary rescan of a known source is never
  offered). `scan_new_source` gained `same_as`, mirroring the CLI's own
  inline `--same-as` logic exactly rather than duplicating it a third
  time. `app/ui/widgets/offline_media_dialogs.RenameSuggestionDialog`
  ("Is this 'Old NAS' at a new address?", Yes/No, wording in
  `presenter.rename_suggestion_text`) is wired into
  `shell.MainWindow._offline_media_scan`, now a two-phase flow: a cheap
  worker check before the Scan dialog's answer is acted on, the dialog
  only when a structure match is found, then the real Scan. Nothing here
  changes the identity resolution itself, which was already complete.
  Verified: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py
  tests/unit/test_offline_media_view.py -v` → all green (see the item's
  own test names: `test_check_renamed_source_*`,
  `test_scan_new_source_with_same_as_*`,
  `test_rename_suggestion_dialog_*`). The two-phase flow inside
  `shell.py` itself is verified by import/construction only - no test in
  this tree drives a full `MainWindow` through this exact click path (none
  did before this either; every other Offline Media shell handler is in
  the same position) - flagged honestly rather than claimed proven.
- [x] **1b** credentials NEVER: user connects via Windows as they always do;
  unreachable/denied → "offline — not signed in or not reachable", plain
  words, no prompt from Leasha ever (safety invariant).
- [x] **1c** network scan defaults: hash verification OFF (mtime/size
  settling — SMB hashing is prohibitive); availability probes hard-timeout
  on workers; nothing network-touching ever runs on the UI thread (extend
  the scanner test to the new modules).
- [x] **1d** the decommission case documented in the tab's help line: scan a
  share before a server is switched off or access is lost — searchable
  forever. One honest sentence: the catalogue holds what *this account*
  could read on scan day.

  **2026-09-16.** `presenter.offline_media_help_text` (two sentences - 1d
  and 3b-3 below share one function, since both are "the tab's help
  line" and a person reads them together), wired into
  `OfflineMediaView.help_line`, a word-wrapped label under the tree.
  Verified: `test_the_tab_shows_its_own_help_line`,
  `test_help_text_names_the_decommission_case`.

## 2. Cloud mounts (kind 3 — Google Drive first, via Drive for Desktop)

**2026-09-16 — assessed, not built this session.** Real and substantial:
2a needs a new volume kind (`cloud`) actually cataloguable from the tab and
CLI, identity resolution for a vendor mount that carries no drive GUID and
no UNC path, and an Open action that goes to a browser URL instead of
Explorer for exactly that kind. 2b needs a folder-scoped opt-in store, a
size cap enforced before a read, and a CLI/UI surface for both - the
existing `WalkConfig.include_cloud` is a whole-run boolean and covers
neither. Both are real features, not small extensions of what §3 already
proved (§3's placeholder *read* guard is the mechanism 2b's trap needs, but
2b is the opt-in and the cap around it, which do not exist yet in any
form). Deliberately left open rather than attempted partially and left in
a state nobody could trust - see `HANDOFF.md`'s own rule about a claim
that sounds finished being worse than an honest gap.

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

  **2026-09-15 — the guard half is built and proven for real; the opt-in
  half is not, so the item stays unticked.** The guard: a placeholder is
  never opened, whatever kind of root it is under - `test_2b_names_only_
  scan_of_a_placeholder_performs_zero_content_reads` asserts it at exactly
  the level the order names, by monkeypatching `builtins.open` and proving
  it is never called with the placeholder's path, not merely by checking the
  result. **Not built**: a genuine *per-folder* opt-in with a size cap.
  `WalkConfig.include_cloud` (a whole-run boolean, pre-existing) is the only
  lever that exists; there is no folder-scoped opt-in, no size cap, and no
  CLI or UI surface for it at all yet. Narrower than the order asks for, so
  left open rather than ticked on the strength of the guard alone.
- [ ] **2c** API connectors (no Desktop client): **DEFERRED INDEFINITELY by
  owner decision** — OAuth + network code + connector treadmill + bends the
  load-bearing privacy paragraph. Recorded here so it is a decision, not a
  gap. The fair ask is "install Google's own Drive for Desktop."

  **2026-09-15**: still nothing to build here - re-read this session,
  decision unchanged, no action taken.

## 3. The per-file placeholder model (applies to NORMAL roots too —
OneDrive Files On-Demand etc.)

- [x] **3a** the walker reads the cloud-files attributes it already stats
  (RECALL_ON_DATA_ACCESS/OFFLINE/pinned): hydrated file = plain local file,
  full index; placeholder = name-only row, "online-only, content pending" —
  NEVER read placeholder bytes.

  **2026-09-15 — real bug found and fixed, proven with a real Windows
  attribute round-trip.** `walker.py` used to `continue` past a placeholder
  candidate, dropping it entirely - no row anywhere, the exact "invisible is
  the worst of the three possible answers" class `WalkConfig.name_only`
  exists to prevent for unreadable extensions. Fixed so the candidate is
  always yielded, with `readable=False`; `pipeline.py`'s `_extract_worker`
  now recognises a placeholder specifically and writes `SKIPPED`/
  `ERR_CLOUD_ONLY` rather than a bare `name_only` row - findable, with a
  stated and fixable reason, per non-negotiable 2.
  `test_3a_a_placeholder_is_findable_and_skipped_with_a_reason` and
  `test_3a_a_settled_placeholder_is_not_reprocessed_every_run` set a real
  `FILE_ATTRIBUTE_OFFLINE` bit via `SetFileAttributesW` (not a mock) and run
  it through a real `Pipeline`. "hydrated file = plain local file, full
  index" is 3b's own line, ticked there.
- [x] **3b** transitions: placeholder→hydrated (user opened it) → next
  incremental run content-indexes it (the H1 held-pass shape — the index
  deepens along what the user actually touches); deliberate "index contents
  of this folder" opt-in hydrates on purpose, capped + warned.

  **2026-09-15 — the placeholder→hydrated half is proven; "opt-in, capped +
  warned" is 2b's per-folder mechanism and is not built (see 2b's own
  note).** `test_3b_a_placeholder_that_becomes_hydrated_is_indexed_next_run`
  clears a real `FILE_ATTRIBUTE_OFFLINE` bit between two pipeline runs and
  proves the second one content-indexes the file. Needed a second real bug
  fix beyond 3a's: `ERR_CLOUD_ONLY` was falling into the generic
  settled-skip fast path (mtime/size unchanged ⇒ trust the old answer, wrong
  here because the *answer* can change with nothing on disk moving) - fixed
  by adding it to `Pipeline.DEFERRED_SKIP_CODES`, the same mechanism
  `ERR_OCR_HELD`/`ERR_FILE_LOCKED` already use. Ticked for the transition
  half only; the deliberate per-folder opt-in stays open exactly as 2b
  describes.
- [x] **3c** hydrated→DEHYDRATED (OS frees space): **KEEP all extracted
  content/chunks/vectors**; the row flips to online-only. Prune/change logic
  is explicitly taught: dehydration is neither deletion nor modification
  (size-on-disk changes; the logical file does not). Test: dehydrate a
  fixture → content still searchable, zero rows pruned.

  **2026-09-15 — the order's own acceptance line, proven word for word.**
  `test_3c_dehydration_keeps_content_and_prunes_nothing`: index a real file,
  set `FILE_ATTRIBUTE_OFFLINE` on it (a real dehydration stand-in), run
  again with `prune_missing=True`. `status` stays `INDEXED` - the pipeline's
  pre-existing "never demote a row that was read" rule, written for a
  narrower case (an extension set narrowing), already covered this exactly
  once 3a's fix stopped the file from vanishing from the walk's candidates
  altogether - and zero rows are pruned. `"the row flips to online-only"` -
  the *display* half - is `volume_location_label`'s job for a volume-level
  source and 3d's for a search result; `status` staying `INDEXED` is what
  keeps the content itself alive, which is what this item is actually
  about.
- [x] **3d** results badge "online-only — opening will download", riding the
  0513 §3a decoration path.

  **2026-09-16.** 0513 §3a's decoration path closed 2026-09-15 (the row
  badge and the tooltip both now exist) and this rides it exactly as named:
  `presenter.placeholder_marks` is 3d's own worker-computed set - which of
  a results page's rows are a live cloud placeholder right now
  (`winfs.is_cloud_placeholder`, one stat per row, the same cost class
  `missing_paths` already pays) - and `online_only_note` is the exact
  sentence. Wired into the same places §3a's badge already reaches:
  `decorate_results` (a fourth key, `"placeholders"`), `results_view.py`
  (`_placeholders`, kept in step with `ResultDelegate.placeholders`), the
  tooltip (`result_tooltip`'s new `placeholder` flag), and the inline
  subtitle (`group_subtitle`'s new `online_only` flag, painted in the same
  slot the offline-volume note uses - the two can never both apply to one
  row in practice, and the volume note wins if they somehow did, since it
  is the more specific fact).

  **Not `volume_location_label`.** That function's own docstring offered
  itself as "the wording ... whenever 0513 §3a's decoration path is
  built" - read again before writing this, and it does not quite fit:
  it formats a `VolumeRecord` (a catalogued *source*), and a cloud
  placeholder here is an ordinary indexed *file* under a normal root,
  with no volume row at all. Reusing it would have meant inventing a fake
  record just to satisfy its signature. `online_only_note` is a one-line
  sibling instead; 3b-1's own still-unwired "Results say 'on tape ...'"
  half is the one `volume_location_label` was actually written for, and
  remains open exactly as its own note already said.

  Verified: `venv\Scripts\python.exe -m pytest tests/unit/test_offline_media.py
  tests/unit/test_result_delegate.py -v` → all green, including a real
  `FILE_ATTRIBUTE_OFFLINE` round-trip for `placeholder_marks` itself
  (`test_placeholder_marks_finds_a_real_cloud_placeholder`) and a real
  paint pass proving the delegate wiring
  (`test_the_delegate_paints_the_online_only_badge_from_the_placeholders_set`).

## 3b. ADDED by owner 2026-08-28 — tape and the archived-source kind

- [x] **3b-1 Kind 5: "manual/archived source" — the scan-before-archive
  workflow.** "Mark as archived" detaches any catalogued source or folder
  into a source whose identity is just a **name + free-text location**
  ("LTO-7 tape B-0042, fire safe, IT room"): Rescan disabled (nothing to
  reconnect), Browse and Delete remain, snapshot date preserved. Results
  say "on tape B-0042 (archived Mar 2024)". Generalises to DVDs, destroyed
  drives, media handed to third parties — the catalogue-of-record.

  **2026-09-15.** `SqliteStore.archive_volume` flips `kind`→`archived`,
  `status`→`ARCHIVED`, writes `location_note`; every `files` row is
  untouched (`ON DELETE SET NULL`/`ON CONFLICT` machinery never runs, since
  nothing about `volume_id` or `identity_key` changes). `connected_volumes`
  already treated every kind outside `(drive, network)` as never
  resolvable, so Rescan stops meaning anything with no extra code; Browse
  (`resolve_file_path`) and Delete (`delete_volume`) both keep working,
  since neither depends on `kind`. CLI: `app.cli offline-media --archive
  NAME_OR_ID --location "..."`.
  `test_archive_volume_flips_kind_and_status_and_keeps_files` proves the
  flip, the untouched file count, and that `connected_volumes` never offers
  it; `test_offline_media_archive_without_a_location_is_a_clean_error` and
  `test_offline_media_archive_of_an_unknown_source_is_a_clean_error` prove
  the CLI's error paths. "Results say ..." is the display half - covered by
  `volume_location_label` (see 3d's note) but not wired into a results
  surface yet, same reason as 3d.
- [ ] **3b-2 LTFS tapes** mount as filesystems and work as kind-1 volumes
  already, with ONE rule: content scans of a volume whose filesystem
  reports LTFS (or a "sequential medium" flag the user can set) process
  files in **on-tape order** with a plain-words warning ("this reads the
  tape end-to-end") — random-access walker order on tape is minutes per
  seek. Names-only cataloguing stays instant (LTFS index).

  **2026-09-15 — the flag and its warning are built; on-tape ordering is
  not, and left open rather than guessed at.** `volumes.sequential_medium`
  (schema already carried it, unused until now) is settable at Scan time via
  `app.cli offline-media --scan ... --sequential-medium`, and the CLI prints
  the plain-words warning before a scan of such a volume starts. **Not
  built and not verified**: this machine has no LTFS tape or sequential
  medium to test real on-tape physical ordering against, and inventing a
  reordering scheme without that would be exactly the kind of vendor
  behaviour this order says not to guess at. Names-only cataloguing is
  unaffected (unchanged code path) and stays instant.
  `test_scan_records_sequential_medium_for_a_tape_volume` proves the flag's
  storage; the ordering half is undone and unverifiable from here.
- [x] **3b-3** proprietary backup formats (Veeam/NetBackup/tar-on-tape)
  are OUT by doctrine — the backup product is the generating system; the
  supported path is cataloguing the staging folder before the tape write,
  which 3b-1 completes. One sentence in the tab's help says exactly this.

  **2026-09-15**: the doctrine itself needs no code - 3b-1 already completes
  the supported path. What remains is purely the tab's help-line sentence,
  which does not exist because the tab does not (same as 1d, 3b-3's
  sibling). Left open.

  **2026-09-16.** The tab exists (0k §2, shipped) and now has a help
  line - see 1d's own note, same function (`presenter.
  offline_media_help_text`), same verification.

## 4. Tests (beyond those inline)

- [ ] UNC identity: mapped-letter add stores UNC; letter remapped → same
  source.

  **2026-09-15**: still **(UNCONFIRMED: the positive case)** - no real
  mapped network drive on this machine, same gap `resolve_unc`'s own note
  already records. What is provable without one - that `identity_key` is
  the UNC itself, never letter-dependent, so any letter that resolves to it
  is the same source by construction - is exactly what `upsert_volume`'s
  `ON CONFLICT(identity_key)` already guarantees and `test_rename_volume_
  identity_keeps_every_file_row` exercises for the renamed-identity case.
  Left open rather than claimed from the mechanism alone.
- [x] share-offline immunity mirrors 0513's (never prune, never wait).

  **2026-09-15.** `test_a_run_elsewhere_does_not_prune_an_offline_shares_
  rows` mirrors `test_a_run_elsewhere_does_not_prune_an_offline_volumes_
  rows` exactly, for `kind="network"`: a run over an unrelated root with
  `prune_missing=True` leaves a decommissioned share's rows byte-for-byte
  unchanged. "Never wait" was already 1c's own proof
  (`test_probe_unc_reachable_times_out_rather_than_hanging`).
- [x] mixed folder fixture (hydrated + placeholder): per-file treatment
  exactly as 3a; the transition tests both directions.

  **2026-09-15.**
  `test_3a_mixed_folder_hydrated_and_placeholder_get_different_treatment`:
  one real local file and one real placeholder (`FILE_ATTRIBUTE_OFFLINE`),
  same folder, same run - the local file is `INDEXED`, the placeholder is
  `SKIPPED`/`ERR_CLOUD_ONLY`, proving the decision is per-file, from each
  candidate's own attributes, not per-folder or per-run. "Both directions"
  of the transition are 3b's (placeholder→hydrated) and 3c's
  (hydrated→dehydrated) own tests.

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentences: a share scanned before decommission stays searchable years later;
a streamed Google Drive is findable by name with zero bytes downloaded; and
Windows freeing disk space never costs Leasha a word it had already read.
