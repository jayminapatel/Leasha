# Work order (UI): wire the Space Report and the idle-tune scheduler into the redesigned shell

**Doc version:** 1.5 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5
**Thread:** UI
**Status correction, 2026-09-20:** SHIPPED - 7 of 7 items ticked 2026-09-16 (the register has said
so since). The line below is the original release text, kept as written.
**Status:** RELEASED by the owner 2026-09-16.

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

## Context

A crash-recovery session built and merged two backend pieces to `main` while
this thread's UI redesign (`WORKORDER-202626160950-ui-redesign.md`) was
actively rewriting `app/ui/shell.py`, `theme.py`, `results_view.py` and the
rest of the shell chrome. To avoid two threads writing the same files at
once, the UI wiring for both pieces was deliberately left undone and handed
over here instead.

**Both backends are already merged, tested, and working from the command
line right now** — nothing below is blocked on anything except deciding how
the redesigned shell wants to present them. Everything in this order is
stated as a functional contract, not a layout: the redesign owns how it
looks.

> **2026-09-16 — both wirings built, same day, in the UI Redesign session
> (Linux sandbox, no PyQt6).** §1: `app/ui/reports_view.py` lists "The
> Space Report" beside Digital Inheritance with a plain-words description,
> renders `render_space_document(...)` on the same `CallableWorker` snapshot
> the Inheritance report already uses (never the UI thread), and exports it
> through the existing `_write_pdf` route with no source picker (the report
> is about the whole corpus). §2: `MainWindow._maybe_run_idle_bench` /
> `_idle_bench_finished` in `app/ui/shell.py`, on the same hourly
> `_optimize_timer`; adapted from the `integrate-main` reference with two
> changes — the "what happened" sentence is a toast (`notify`, the status
> bar is gone under 202626160950 §6), and `cached_profile`/`should_bench`
> moved onto the worker with the bench, because both read the store and the
> UI thread never does (non-negotiable 5; rules 1 and 2 still run on the UI
> thread first, so nothing is dispatched mid-run or on battery). Tests, one
> per rule plus the defaults-only guard, in
> `tests/unit/test_idle_tune_and_space_report_ui.py`. **Nothing below is
> ticked yet**: every item needs the Windows venv —
> `venv\Scripts\python.exe -m pytest tests\unit\test_idle_tune_and_space_report_ui.py -v`
> — and the acceptance sentences read against the running app. §3's
> `regen_vs_project.py` also runs there; the register rows for 0n and 0b are
> corrected (1.21).

> **2026-09-16, later the same day — the Windows run, by the crash-recovery
> session.** `pytest tests\unit\test_idle_tune_and_space_report_ui.py -v`:
> 10 passed (one real bug found and fixed along the way, not in this order's
> own code — `app/ui/reports_view.py::_show_selected` refused to render the
> Space Report whenever there were zero inheritance sources, gating both
> reports on one report's own emptiness; fixed to check per-report). Both
> acceptance sentences read against the real running window: the Reports
> page lists "The Space Report" beside "Digital Inheritance" with its own
> tooltip, opens to the duplicate/uniqueness content, and the Export button
> is live; the idle-tune scheduler's five rules each have their own passing
> test, `INDEX_TUNING_MODE` only ever moves from `defaults`, and the tuning
> control updates without a restart. Every item below is ticked. §3's
> `regen_vs_project.py` ran; `docs/ORDER_REGISTER.md`'s 0n/0b rows were
> already current (1.21) and needed no further change.

## 1. The Space Report

`app/reports/space.py` (merged) finds duplicate files across every source
and warns about content that exists on only one — see the module's own
docstring for the full reasoning. It is already reachable headless:

```powershell
venv\Scripts\python.exe -m app.cli report space
venv\Scripts\python.exe -m app.cli report space --json
```

Run that command against a real index to see exactly what the report says
before building anything — it is the authoritative content.

**The functional contract, exactly:**

- `find_duplicate_groups(store)` → the biggest duplicate groups, each
  carrying its copies and their sources.
- `total_reclaimable_bytes(store)` → one number, the whole corpus, not only
  the groups shown.
- `find_source_uniqueness(store)` → per-source counts of content that exists
  nowhere else in the index (volumes ranked first — see the module
  docstring for why).
- `render_space_document(groups, uniqueness, total_reclaimable=..., generated_at=...)`
  → one string, the whole report, plain text with `#`/`##` headings — the
  identical shape `app.reports.inheritance.render_inheritance_document`
  already produces for the Digital Inheritance report.

**What must be true when this is done:**

- [x] The Space Report is reachable from wherever the redesigned shell puts
      "Reports" — findable the same way the Digital Inheritance report is,
      whatever that ends up looking like in the new design.
- [x] Opening it shows the same content `app.cli report space` prints.
- [x] It can be exported to a PDF. `render_space_document`'s output is
      plain markdown-ish text — feed it through the same
      `QTextDocument`/`QPrinter` route the Inheritance report's own export
      already uses (`_write_pdf` in the pre-redesign `app/ui/reports_view.py`
      is the working reference, off a worker thread, never the UI thread).
- [x] Generation runs off a `CallableWorker`, not the UI thread — it touches
      the store.

Acceptance sentence: a person opens the redesigned Reports surface, sees The
Space Report listed with a plain-words description, opens it, sees the
duplicate summary and the "only copy" warning, and can save it as a PDF.

## 2. The idle-tune scheduler

Work order `202626270114` (0b) §5e: the first time the computer is idle, if
it has never been benchmarked, time it once and quietly upgrade Index Tuning
from Defaults to Auto — no dialog, no question.

**The functional contract, exactly, in order:**

1. Never fire while an index run is in progress.
2. Never fire while genuinely on battery — an unknown/unreadable battery
   state must **not** block it, only a confirmed "running unplugged"
   answer does (`psutil.sensors_battery()`; `None` means no battery or the
   question does not apply).
3. Ask `app.index.autotune.should_bench(store, profile)` for a reason. Empty
   string means do nothing this tick.
4. If there is a reason: run the bench off the UI thread. Reuse the exact
   call the manual "Benchmark now" control already makes —
   `app.index.index_bench.run_index_bench(settings, devices=...)` via
   `CallableWorker`.
5. On success: remember it —
   `app.core.measured.remember(store, result.as_measured(profile.fingerprint()))`.
6. **Only if `INDEX_TUNING_MODE` is still `"defaults"`** (never overrides a
   real Manual or Auto choice someone already made): persist
   `INDEX_TUNING_MODE=auto` the same way every other setting write in the
   redesign persists, refresh whatever control shows the current mode, and
   say what happened afterwards — past tense, plain words, e.g. *"Timed
   this computer while it was idle - tuned automatically from now on."*

Hook this into whatever the redesign uses for periodic background
maintenance (the pre-redesign shell ran this off the same hourly timer that
already refreshes the query planner — if an equivalent exists in the new
shell, that is the natural home).

**A fully working, tested reference implementation exists** against the
pre-redesign `shell.py` — two methods (`_maybe_run_idle_bench`,
`_idle_bench_finished`, ~70 lines total) plus four tests in
`test_window_opens.py`, on branch `integrate-main` in the
`crash-recovery-ac8615` worktree. The surrounding file has since diverged,
so it is a reference to adapt rather than a patch to apply, but the logic
itself does not need re-deriving — only re-placing.

**What must be true when this is done:**

- [x] The five ordering rules above hold, each provably (a test per rule is
      the cheapest way to keep them that way).
- [x] A successful idle bench changes `INDEX_TUNING_MODE` only from
      `defaults`, never from `manual` or `auto`.
- [x] The mode control in the redesigned UI reflects the change without
      needing a restart.

## 3. Housekeeping, once both land

```powershell
venv\Scripts\python.exe scripts\regen_vs_project.py
```

`Leasha.pyproj` was deliberately left unregenerated — the redesign is still
adding files, and regenerating mid-flight would just need doing again.

Then correct `docs/ORDER_REGISTER.md`'s rows for `0n` (Reports) and `0b`
(Index Tuning) — both work orders' own dated 2026-09-16 notes have the
exact current checkbox counts to copy in.

## Done means

Change + tests + suite green + committed by name. Both acceptance sentences
above, read literally against the running app.
