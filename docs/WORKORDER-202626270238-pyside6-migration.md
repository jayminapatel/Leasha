# Work order (One thread): migrate PyQt6 → PySide6 — DRAFT

**Doc version:** 1.1 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5
**Thread:** One thread (UI + tests + packaging)

> **2026-10-05 - RELEASED by the owner, and built the same day.** The owner asked what moving
> to PySide6 would take, had a trial run on branch `trial/pyside6`, saw the trial window ("it
> looks the same") and said: "do the recommended and finish the port". This reverses the drop
> below; the drop's note is kept as written. The order is 1.0 by its own rule ("bumps to 1.0
> with the owner's promotion"). What the trial found beyond this order's list, and how each
> item went, is in the dated notes in §1-§3. Whole suite on Windows after the merge: **13,282 passed, 0 failed**, no process crashed (131 skipped; 3 processes, 19 min).
>
> **2026-09-27 - DROPPED by the owner. Do not start this order, and do not promote it.**
> Leasha stays on PyQt6. The owner was told the licensing consequence and took the
> decision knowing it: PyQt6 is GPL-3.0-only, so a packaged build handed to anyone else
> is a GPL work, while `LICENSE` reads MIT. Which licence a distributed build carries is
> now an open owner decision recorded in `WORKORDER-202626082213-install-and-distribution.md`,
> not something this order will resolve. Everything below is kept unedited for the
> record. Two of its measured facts had already gone stale by this date and should not
> be reused: PyQt6 is referenced by 205 files, not 75, and `sip` is now called in code
> (`app/ui/widgets/skeleton.py`, `tests/unit/test_later.py`,
> `tests/unit/test_worker_signal_owner.py`), not only named in comments.
**Status: DRAFT — NOT FOR EXECUTION.** The owner has taken the *decision*
(PySide6, to keep every licensing future open) but not scheduled the work.
Scheduled slot: **first item of the post-working-version restructure window**
— after the feature orders land, before the cli/presenter/shell splits, when
nothing else touches UI files. Do not start on this document until the owner
promotes it and bumps it to 1.0.

## Why (so the reasoning survives until then)

PyQt6 is GPLv3: distributing Leasha at all — free, to friends — obliges the
whole application to be GPL, permanently. PySide6 is the Qt company's own
binding under LGPL: Leasha keeps whatever licence the owner later chooses
(open, closed, commercial, any of them), with obligations limited to shipping
the standard unmodified wheels plus Qt's notice. Same C++ Qt underneath —
no performance difference, PyInstaller supports both. The cost of switching
grows with every Qt line written, which is why the decision was taken early
and the work sits at the front of the restructure window.

**Standing instruction to the thread until then** (active now, the one part
of this draft that is): new Qt code keeps using the current PyQt6 idioms
(`pyqtSignal` etc.) for consistency — no pre-emptive half-migration, no
compatibility shims. The migration is one mechanical pass either way; a
mixed style before it would be worse than either binding.

## 1. The mechanical pass (~95% of the change, verified against the tree)

> **2026-10-05 - 1a-1c done.** The rename touched 328 files. Three renames were not enough,
> and the order's "no other idiom in use needs translation" did not hold: `sip` became
> `shiboken6` behind `app/ui/qtsip.py`; `QTextDocument.print` is `print_`; menus are opened as
> `type(menu).exec(menu, ...)` so a test can stand in for them; `event.pos()` is
> `event.position().toPoint()`; a menu reached through `QAction.menu()` is destroyed under
> PySide6 6.11 once the action's handle goes, so menus are found as the bar's children; a
> sort fall-back calling `super().__lt__` recursed until the process died (an access
> violation), and now compares the shown values itself. 1b: the `PySide6` package, which
> brings both Essentials and Addons (QtPdf is in Addons). 1c: a dated note under the header
> of the 19 documents and the technical reference that name PyQt6; CHANGELOG and HANDOFF
> entries.

- [x] **1a** 75 files reference PyQt6 (measured 2026-08-27). Three renames,
  applied tree-wide in one commit: `PyQt6` → `PySide6`;
  `pyqtSignal` → `Signal`; `pyqtSlot` → `Slot`. No other idiom in use needs
  translation — scoped enums, `QAction` in QtGui, connect syntax, `exec()`
  are identical in both bindings (verified against the code, not assumed).
- [x] **1b** `requirements.txt`: `PyQt6` → `PySide6-Essentials` +
  `PySide6-Addons` (**QtPdf lives in Addons** — the preview pane breaks
  without it). `install.ps1` / `run-install.cmd` dependency lists follow.
  Venv rebuild; `doctor.py` unaffected (stdlib only).
- [x] **1c** docs sweep: every doc naming the binding gets a dated
  correction *note* (never a rewrite of released item text — the owner's
  standing rule). CHANGELOG under `[Unreleased]`.

## 2. The three places the sed pass cannot be trusted (verified findings)

> **2026-10-05 - 2a, 2b done; 2c done but for the owner's hand checks.** 2a: the presenter
> guards in `test_presenter.py` and `test_ui_never_blocks.py` name every binding, and a new
> `test_one_qt_binding.py` refuses an import of any other binding anywhere in the tree (the
> laptop's venv still has PyQt6 installed, which would hide one). All three were seen to fail
> on a deliberate `import PyQt6` in `app/ui/presenter/formatting.py`. 2b: the importorskips
> were renamed with 1a; GitHub's Mac job skipped 219 under PySide6 against 218 under PyQt6,
> the one more being `test_header_signal_safety`'s child-process test, which needs sip's
> `transferto` and now says so. 2c: dated notes on the three sip comments (`workers.py` twice,
> `shell.py`); `test_worker_signal_owner.py` and the whole suite pass. Closing mid-search
> and mid-index by hand is the owner's.

- [x] **2a — the guard tests would go silently dead.**
  `test_presenter.py:330` and `test_ui_never_blocks.py:365,375,378` assert
  `"PyQt6" not in imports` — after migration they pass **vacuously**,
  enforcing nothing. They must assert against BOTH names
  (`{"PyQt6", "PySide6"}`), **in the same commit as the rename**, and each
  must be watched to fail once against a deliberate violation before the
  commit lands (the load-bearing-tests rule: a guard nobody has seen fail
  is not a guard).
- [x] **2b — the Qt test subset would silently skip.**
  `test_presenter.py:423` (`pytest.importorskip("PyQt6")`) — and any
  sibling importorskips — would turn every Qt-dependent test into a skip
  after the migration: **a green suite that tested nothing**. Rename them
  with 1a, then assert in the run output that the Qt tests *ran* (skip
  count compared before/after).
- [ ] **2c — workers.py object-lifetime semantics.** The forensic guards
  (`workers.py:166`, `shell.py:1552`) document crashes against PyQt6's
  sip layer; PySide6 uses shiboken, whose deleted-object timing differs
  subtly. Good news, verified: `sip` appears **only in comments**, never in
  code — nothing calls `sip.isdeleted`. Required anyway: run the full
  worker/shutdown test set attentively (not just green — read the output),
  exercise the close-during-search and close-during-index paths by hand on
  Windows, and append dated notes to the sip comments saying the guard was
  re-verified under shiboken.

## 3. Acceptance

> **2026-10-05.** Suite: **13,282 passed, 0 failed**, no process crashed (131 skipped; 3 processes, 19 min) (laptop, after the merge). GitHub, PySide6 only installed:
> Windows green; macOS 13,060 passed and 1 failed - a Describe race in the photo window that
> was a fault on `main` too, fixed in `e67afe4`. The hand smoke test is the owner's; the
> trial window was opened on a copy of the demo store and the owner judged the look the same.
> The golden-picture comparison passes under PySide6. Licence: `docs/THIRD_PARTY_NOTICES.md`
> has a Qt for Python section; `LICENSE` stays MIT, as this order said it would.

- [x] Full suite green on Windows with the Qt subset **confirmed running,
  not skipped** (2b's count check).
- [ ] Hand smoke test on Windows: window opens, search, preview pane
  renders an image AND a PDF (QtPdf/Addons proof), tray, clean close
  mid-search, clean close mid-index.
- [x] `pyproject`/licence notice updated: Qt LGPL notice shipped; Leasha's
  own licence field left as the owner's separate decision — this order
  changes the *binding*, not the project's licence.
- [x] Estimated effort when executed: the renames are an hour; the suite
  run, guard-test hardening and Windows smoke are the real day.

## Done means

One commit series: rename + guard-test hardening together, then packaging,
then docs notes. Suite green before and after on Windows, Qt tests counted
as run, smoke test performed. This document bumps to 1.0 with the owner's
promotion before any of it starts.
