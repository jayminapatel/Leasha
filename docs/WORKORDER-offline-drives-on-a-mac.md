# Work order (One thread): Offline drives on a Mac — a scanned drive is found again when it is plugged in

**Doc version:** 1.3 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5
**Thread:** One thread (Core + Index; no new UI)
**Status:** RELEASED by the owner 2026-10-05 ("release it so it just needs testing later,
make sure it is of good quality"), and built the same day. Was DRAFT, written earlier that day
at the owner's request ("do the recommended and draft the work order").
**Register:** queue letter **1d**. Follows **0k**
(`WORKORDER-202626270513-offline-media-one-drives.md`, SHIPPED) and unparks one line of
**0x** §P (`WORKORDER-overhaul-and-mac-ready.md`): "Offline Media drives on a Mac (volume
identity and removable-drive detection)". `docs/PARKED-IDEAS.md` §6 still lists that line as
parked; it stays so until the owner releases this order.

## Where this came from

2026-10-05: the whole suite passed on GitHub's Mac for the first time (13,026 passed, run
37307485258). One test had to be marked `windows` to get there:
`test_gui_scenarios_orders.py::test_offline_media_scan_rescan_and_delete_pressed_for_real`.
On the Mac the scan worked and **Rescan never became available**. The owner asked how to
close the gap.

## What is true today (read from the code, 2026-10-05, commit `d501ea7`)

- A scanned drive is remembered by Windows' volume GUID, never by its letter (0k §1).
  "Rescan" is offered when a plugged-in drive with that identity is found.
- That lookup is three functions in `app/core/volumes_win.py`: `identify_root`,
  `mounted_drive_roots`, `find_drive_by_guid`. Each returns nothing when
  `sys.platform != "win32"`.
- `app/index/offline_media.py::connected_volumes` returns `{}` off Windows before it asks
  anything, so every source reads as unplugged on a Mac, always.
- `volumes_win.py` is on the allow-list of the load-bearing
  `test_osbridge_guard.py::test_no_windows_only_call_outside_osbridge`. It is imported
  directly from `app/index/offline_media.py` and `app/cli/offline_media.py` (ten places).
- **Not established:** what identity, if any, a scan on a Mac stores today. `identify_root`
  returns `None` there, and `identify_source` then returns `None`, which reads as "that is a
  folder, not a drive" - yet the Mac test run got as far as waiting for Rescan. §1a settles
  this before anything is built on it.

> **2026-10-05, on release.** The three decisions below were released as written.
> The owner's instruction was to build it now so that only testing is left, which changes one
> thing in this order: §1 said "establish before building". 1a is answered from the code, 1b
> and 1c were measured on GitHub's Mac against real disk images, and what only a real Mac
> and a real stick can show is left open in 1b and 4a. Each item's own dated note says which.

## Owner's decisions (the recommended ones, 2026-10-05 — confirm on release)

1. **A drive scanned on Windows is a new source on a Mac, and the other way round.** The two
   systems give one drive two identities. Matching across systems by the format serial is
   left out of this order.
2. **Drives only.** Network shares on a Mac (`smb://` mounts under `/Volumes`) are a second
   step and not in this order; a share stays "not connected" on a Mac, as today.
3. **The owner's model from 0k stands unchanged:** fully manual; Scan, Rescan, Delete and
   nothing else; no arrival prompts; a mount point is never stored.

## 1. Establish before building (measure, do not assume)

> **2026-10-05 - 1a answered from the code, not from a Mac.** The scenario test replaced
> `identify_source` and `find_drive_by_guid` with stand-ins, which is why it reached Rescan.
> Without them `identify_root` returned `None` off Windows, so a real Scan on a Mac was
> **refused** ("that is a folder, not a drive") and stored nothing. There were no Mac-made
> rows to migrate.

- [x] **1a** On a Mac, record what Scan does today with a real drive root chosen: what row,
  if any, lands in `volumes`, and with what `identity_key`. Written into this order as a
  dated note before §2 starts.
> **2026-10-05 - 1b measured on disk images, and left open for real sticks.** On GitHub's Mac
> (run 37312880250, commit `817ce6e`, `macos-14`) `hdiutil` made one image each of APFS, Mac OS Extended, exFAT and FAT32; each was
> mounted, unmounted and mounted again three times. **All four kept one identity throughout**,
> the system call and `diskutil` both answered for all four and agreed, and two exFAT images
> with the same name were told apart when mounted in either order
> (`tests/unit/test_mac_volumes.py`). So FAT and exFAT are no longer UNCONFIRMED for images.
> **Not done:** a real stick, NTFS, and a second Mac. The box stays open for those
> (`docs/MAC_VERIFICATION.md` 5.3a-5.3g).

- [ ] **1b** On a real Mac with real media, record the volume UUID macOS reports for one
  stick of each kind - **FAT32, exFAT, APFS, Mac OS Extended, NTFS (read-only on a Mac)** -
  unplugged and replugged three times each, and once on a second Mac. **Whether FAT and
  exFAT sticks have a stable UUID is UNCONFIRMED**, and most USB sticks are one of those.
  If one kind has none, this order says so in a dated note and that kind is refused with a
  plain sentence rather than identified by something that changes.
> **2026-10-05 - 1c measured** (run 37312880250, commit `817ce6e`, `macos-14`, the start-up disk, mean of five): the system call
> **0.100 ms**, `diskutil info -plist` **71.0 ms**. The key is `VolumeUUID`, confirmed by the
> two routes agreeing. The system call is used; `diskutil` is asked only when it has nothing.
> One runner, one disk: representative of the ratio, not of a slow USB stick.

- [x] **1c** Time the two ways of asking - `diskutil info -plist <mount point>` (a
  subprocess; the key is believed to be `VolumeUUID`, **UNCONFIRMED**) and the system call
  behind it (`getattrlist` with `ATTR_VOL_UUID`, through `ctypes`). `connected_volumes`
  sits under `resolve_file_path`, the path every Open and preview takes; 0k kept
  subprocesses out of it for that reason. The number decides which is used where.

## 2. Identity and finding it again

> **2026-10-05 - 2a-2f built.** `app/core/osbridge/volumes.py`. **2b was built differently
> from its wording:** the one door is the three functions that already existed in
> `volumes_win.py`, which hand the question to `osbridge` when asked on a Mac. The ten places
> that import them, and every test that replaces them, are untouched; routing the callers
> through a new name would have changed all of those for no gain. 2d is a prefix,
> `macos-volume:<UUID>`, on the stored identity: no column, no migration. 2e's sentence is in
> `docs/TROUBLESHOOTING.md`. On a Mac a share is not asked about at all (decision 2).

- [x] **2a** A Mac counterpart to the three functions, inside `app/core/osbridge/` so the
  guard test covers it: identify a mounted root (volume UUID, label, file system), list the
  mounted removable roots (the entries of `/Volumes`, the start-up disk left out), and find
  the mount point for a stored UUID. Never raises; an unmounted root is `None`, as on
  Windows.
- [x] **2b** One door for both systems. `offline_media.py` (index and CLI) asks the
  `osbridge` layer, which picks Windows or Mac. `volumes_win.py` keeps its name and its
  Windows calls; nothing in it is reworded.
- [x] **2c** `connected_volumes` and `refresh_volume_statuses` lose the "not Windows, so
  nothing" early return and answer for `kind="drive"` on a Mac. `network`, `cloud`,
  `phone` and `archived` answer exactly as today.
- [x] **2d** The stored identity says which system made it (a prefix on `identity_key`, or
  a column - whichever needs no migration of the owner's existing rows). A Windows GUID is
  never compared with a Mac UUID.
- [x] **2e** Locked or encrypted drives: on a Mac a locked drive is not mounted, so it
  reads as unplugged. The BitLocker probe is never run off Windows. Said in one sentence in
  the troubleshooting document; no new status word.
- [x] **2f** The advisory hardware serial (`hardware_serial_for_root`, 0k's "this looks
  like <name> reformatted") stays Windows-only and returns `None` on a Mac. Recorded as a
  known difference.

## 3. Tests

> **2026-10-05 - 3a-3d pass.** `tests/unit/test_mac_volumes.py`: 15 tests that run anywhere
> and 6 that run only on macOS against real images. 3a answers `diskutil` with a dictionary
> rather than a recorded file. 3b is said the Mac way (`/Volumes/Photos` and
> `/Volumes/Photos 1` changing places), once with stand-ins and once with two real images;
> 0k's own "a run elsewhere does not prune" test already ran on every system and still
> passes. 3c: the `windows` marker is off the scenario and it is skipped only where neither
> system's check exists. 3d: GitHub's Windows job is green on the same commit and the guard
> test needed no new allow-list entry. Whole suite on macOS: **13,051 passed, 0 failed**
> (run 37312880250, commit `817ce6e`, `macos-14`). The laptop ran the nine affected files (502 passed), not the whole suite.

- [x] **3a** Pure-Python tests of the Mac module against recorded `diskutil` output and a
  fake `/Volumes` folder, runnable on Windows: identify, list, find again, unmounted,
  unreadable, a name with spaces and non-ASCII letters.
- [x] **3b** 0k's own acceptance lines, run on the Mac job with the identity stubbed at the
  `osbridge` door: "catalogue at one mount point, remount at another, it is ONE row", "two
  different drives at the same mount point never collide", "a run elsewhere does not prune
  an offline drive's rows".
- [x] **3c** The `windows` marker comes off
  `test_offline_media_scan_rescan_and_delete_pressed_for_real` and it passes on GitHub's
  Mac job: Scan, unplug (stubbed), Rescan unavailable, plug in, **Rescan available and
  pressed**, Delete.
- [x] **3d** The whole suite on Windows is unchanged in count and result, and
  `test_no_windows_only_call_outside_osbridge` passes without a new allow-list entry.

## 4. Proven on a real Mac (GitHub's runner has no USB drive)

> **2026-10-05 - §4 is what is left, and it is the owner's.** 5.3 in the checklist is now
> seven steps, 5.3a to 5.3g. 4b is half done: the changelog and handoff are written; the
> user guide is not touched until 4a has been done, so it does not promise a Mac user
> something nobody has tried.

- [ ] **4a** `docs/MAC_VERIFICATION.md` 5.3 ("Plug in a USB drive: does Offline Media see
  it?") is carried out on a real Mac with a real stick, by the owner or with the owner:
  Scan, eject, the source reads as unplugged, plug into a different port, Rescan is offered
  and works, a search result from the drive opens. Until then every claim in this order is
  marked *UNVERIFIED on a real Mac*.
- [ ] **4b** `CHANGELOG.md`, `HANDOFF.md` and the user guide say what a Mac user can now do,
  and the "Not yet on a Mac: Rescan in Offline" line in the changelog gets a dated note
  above it rather than an edit.

## Not in this order

- Network shares, cloud folders and phones on a Mac.
- Recognising on a Mac a drive that was scanned on Windows (decision 1).
- Any change to what Scan, Rescan or Delete do, or to their wording.
- The other lines of 0x §P (CoreML, the hardware probe, the hotkey, packaging, live
  mailboxes).

## Acceptance

1. On a Mac, a drive scanned once is offered for Rescan every time it is plugged in,
   whichever port, and never when it is not.
2. On a Mac, files on an unplugged scanned drive stay in search results and are never
   pruned.
3. On Windows nothing changes: same rows, same identities, same suite result.
4. Nothing outside `app/core/osbridge/` learns which system it is running on to do this.
