# Active Work Tracker

**Doc version:** 2.3 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

**This file is no longer the tracker.** `docs/ORDER_REGISTER.md` is - it is counted from the
checkboxes in each order, and this file's own per-agent counts were wrong four different ways
on 2026-09-04 (recorded below). It is kept for one reason: the bugs its sessions found and the
root causes they established are worth having in one place. Everything that was a *plan* or a
*status table* here has been removed because the register and `HANDOFF.md` replaced it; git
holds the 2026-09-04 text if it is ever wanted (`git log -- ACTIVE_WORK.md`).

---

## The seven bugs found on 2026-09-04, and what became of each

Each state below was checked on 2026-09-20 by running the test file named, not by reading the
old note.

> *Note, 1 October 2026:* the call in row 2 is now at `app/extract/ocr.py:500`, not `:342`.

| # | Bug | State | Evidence |
|---|---|---|---|
| 1 | Window geometry was saved as a `QByteArray`, mangled by the `str()`-based store, and silently never restored | **Fixed** 2026-09-04 (base64 round trip) | `tests/unit/test_window_state.py` passes |
| 2 | `ocr_ladder.route()` was never called, and its `METADATA_ROUTE` early-return was inverted | **Fixed** - `ocr.py` calls `ocr_ladder.route` (`app/extract/ocr.py:342`) | `tests/unit/test_ocr_ladder.py` passes |
| 3 | `email_mbox.py`: `.mbox.bak` unrecognised, a missing file raised a raw `mailbox` error, an empty subject stored `None` | **Fixed** | `tests/unit/test_email_mbox.py` passes |
| 4 | The sentence-boundary snippet snap only searched backward | **Fixed** 2026-09-04 | `tests/unit/test_presenter.py` snippet tests pass |
| 5 | The FTS trigger drop/restore code had no tests | **Fixed** - and the tests found a real bug (an interrupted bulk run never put the triggers back) | `tests/unit/test_fts_bulk_recovery.py` |
| 6 | `run_tests.py` hardcoded a Linux sandbox path | **Gone** - the file is not in the tree | `ls run_tests.py` |
| 7 | `requirements.txt` called rawpy "pure Python" | **Fixed** 2026-09-04 | comment now says it wraps LibRaw |

The CRLF/LF churn noted on `GitSearch.txt`, `docs/CODE_REVIEW.md`, `run-install.cmd`,
`Leasha.sln` and `Leasha.pyproj` (item 8 of the old list) was not re-checked on 2026-09-20 and
is left as unverified.

## What went wrong with the old tracker, so it does not happen again

The per-agent Task A/B/C/D status here was cross-checked on 2026-09-04 against each order's own
checkboxes, the register and `git log`, and disagreed with all three:

- **Overstated but real** (0f, 0g): working code that was uncommitted, with nothing ticked.
- **Mislabelled** (0r): the work listed under it was 0p's, already shipped.
- **Miscounted** (0s): a private seven-item scheme against the order's real seventeen.
- **Silently broken while marked done** (0p section 4, bug 1 above): "wired" was mistaken for
  "working".

The rule that came out of it stands: **the checkbox counts in `docs/WORKORDER-*.md` are the
source of truth for what is done, and a session summary is a claim to verify against them.**
Git history was the tiebreaker in every case.

## Two root causes worth keeping

**A window that went blank after minimise and restore, with memory growing to 2.4 GB**
(fixed 2026-09-04, `app/ui/shell.py`). Qt's own `isVisible()` had not caught up with the native
window when the deferred repaint ran, so `update()` and `repaint()` were silent no-ops - the
code meant to repair the blank window was being skipped by the thing it called. The fix calls
`setVisible(True)` on the top-level window first, then walks the children with `update()`. The
"leak" was the broken repaint loop, not a second fault. The Indexing tab's ghosted "Index
tuning" text had the same cause.

**A splash that crashed every start-up, never closed, and painted a squashed logo** (fixed
2026-09-04, `app/ui/splash.py`, `app/main.py`). Raw ints where PyQt6 needs enum members
(`TypeError` at construction, before the window showed); float arguments to `drawEllipse` and
friends; `hide_and_close()` written but never called; a wide 2667x1611 logo forced into a
square with no smooth-scaling hint.

## Test collection, for the record

On 2026-09-04 collection failed with 33 errors and no test ran. There were two causes, not
three: `.svg` claimed by both `ocr` and `plaintext` (the failed import left half-registered
extractors in `REGISTRY` and every later import collided with them - 32 of the 33 errors), and
an unregistered `qt` marker under `--strict-markers`. Both fixed. The `.pytest_tmp`
`PermissionError` flood that followed (Windows controlled-folder access) is documented in
`pyproject.toml`.
