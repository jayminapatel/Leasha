# Work order (One thread): migrate PyQt6 → PySide6 — DRAFT

**Doc version:** 0.2 · **Updated:** 2026-09-27 · **Applies to:** app v0.3.3
**Thread:** One thread (UI + tests + packaging)

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

- [ ] **1a** 75 files reference PyQt6 (measured 2026-08-27). Three renames,
  applied tree-wide in one commit: `PyQt6` → `PySide6`;
  `pyqtSignal` → `Signal`; `pyqtSlot` → `Slot`. No other idiom in use needs
  translation — scoped enums, `QAction` in QtGui, connect syntax, `exec()`
  are identical in both bindings (verified against the code, not assumed).
- [ ] **1b** `requirements.txt`: `PyQt6` → `PySide6-Essentials` +
  `PySide6-Addons` (**QtPdf lives in Addons** — the preview pane breaks
  without it). `install.ps1` / `run-install.cmd` dependency lists follow.
  Venv rebuild; `doctor.py` unaffected (stdlib only).
- [ ] **1c** docs sweep: every doc naming the binding gets a dated
  correction *note* (never a rewrite of released item text — the owner's
  standing rule). CHANGELOG under `[Unreleased]`.

## 2. The three places the sed pass cannot be trusted (verified findings)

- [ ] **2a — the guard tests would go silently dead.**
  `test_presenter.py:330` and `test_ui_never_blocks.py:365,375,378` assert
  `"PyQt6" not in imports` — after migration they pass **vacuously**,
  enforcing nothing. They must assert against BOTH names
  (`{"PyQt6", "PySide6"}`), **in the same commit as the rename**, and each
  must be watched to fail once against a deliberate violation before the
  commit lands (the load-bearing-tests rule: a guard nobody has seen fail
  is not a guard).
- [ ] **2b — the Qt test subset would silently skip.**
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

- [ ] Full suite green on Windows with the Qt subset **confirmed running,
  not skipped** (2b's count check).
- [ ] Hand smoke test on Windows: window opens, search, preview pane
  renders an image AND a PDF (QtPdf/Addons proof), tray, clean close
  mid-search, clean close mid-index.
- [ ] `pyproject`/licence notice updated: Qt LGPL notice shipped; Leasha's
  own licence field left as the owner's separate decision — this order
  changes the *binding*, not the project's licence.
- [ ] Estimated effort when executed: the renames are an hour; the suite
  run, guard-test hardening and Windows smoke are the real day.

## Done means

One commit series: rename + guard-test hardening together, then packaging,
then docs notes. Suite green before and after on Windows, Qt tests counted
as run, smoke test performed. This document bumps to 1.0 with the owner's
promotion before any of it starts.
