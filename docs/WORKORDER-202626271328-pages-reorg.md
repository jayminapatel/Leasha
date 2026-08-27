# Work order (One thread): the pages reorg — Settings finds its shelves, Indexing splits in three

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (UI structure only — no engine, no pipeline, no schema)
**Status: DRAFT — NOT FOR EXECUTION until the owner promotes it.**
Promotion condition, set by the owner ("once our workorders are in"): the
index-tuning order (0114) and the search-experience order (0157) are fully
ticked — their Settings and Indexing additions must land FIRST so these
pages are reorganised once, not twice. At promotion the owner registers a
queue position in HANDOFF; a dated note here records the date. Natural
companion to the PySide6/restructure window if it lands near that.

## 0. Settled decisions (owner, 2026-08-28 — do not relitigate)

1. **One small order for both pages.** Settings and Indexing reorganise
   together, after the feature orders have finished adding to them.
2. **Every existing label, description and tooltip relocates VERBATIM**
   (the standing rule). This order moves furniture; it rewords nothing.
3. **The Indexing page layout is currently broken** (owner's report,
   2026-08-28). The fix is folded into this reorg by his decision — the
   split IS the fix; no patching beforehand.
4. Structure choice: a **sidebar category list is preferred** over top
   sub-tabs for Settings (scales past five categories — the VS Code
   pattern); the owner originally suggested sub-tabs and endorses either.
   The thread picks one and records a one-line reason.

## 1. The Settings page

Today: twelve stacked group boxes on one long scroll (settings_view.py) —
the owner's report that started this: "too long and cluttered".

- [ ] **1a** categories, each holding today's group boxes unchanged:
  **What's indexed** (roots / file types / code types / PST) ·
  **Search** (search box / behaviour / history / the per-surface
  behaviour grid from 0157 §1b) · **Models & AI** (Ollama, model roles
  when they exist) · **Appearance** (window box; **theme finally moves
  here** — the misplacement flagged in the tuning order, honoured now) ·
  **Storage & maintenance** (storage / environment / activity / restore).
  New boxes landed by the feature orders join the category that reads
  right; none are left orphaned.
- [ ] **1b** a **filter box** at the top: type "memory" and only settings
  whose label or tooltip matches remain visible, categories auto-expanding
  to show hits. The settings registry (labels + tooltips already
  machine-readable) makes this cheap — the filter walks the registry, not
  the widgets.
- [ ] **1c** the page **remembers the last-open category** across launches
  (the same app-state home as 0p §4's window state — small keys, one
  pattern).
- [ ] **1d** every moved Setting's `surface` updates in
  `settings_registry.py` so `test_settings_reachable` enforces the new
  homes — the registry stays the map of where everything lives.

## 2. The Indexing page

- [ ] **2a** the page splits into three: **Status** (run controls /
  progress / skips / statistics) · **Schedule** · **Tuning** (the 0114
  screen — Defaults / Auto-tune / Manual — slots in whole, untouched).
  Same structure choice as §1 (sidebar or sub-tabs — one answer for both
  pages).
- [ ] **2b THE LAYOUT FIX** — the owner reports the current page's layout
  is screwed up. Diagnose and name the cause in the delivery note (layout
  stretch/spacing/parenting — whatever it proves to be), and let the §2a
  split resolve it structurally rather than patching the old single page.
  Acceptance is visual: the three new views lay out correctly at the
  default window size, maximised, and at the minimum sensible size.
- [ ] **2c** anything on today's Indexing page that is a *setting* rather
  than a control or readout keeps exactly one home (no duplication
  between Settings and Indexing — a value shown in both places reads
  from the one Setting).

## 3. Tests

- [ ] `test_settings_reachable` passes with every relocated surface — the
  load-bearing test of the whole order.
- [ ] labels unchanged: a before/after walk of every label, description
  and tooltip string proves the move was verbatim (snapshot the strings
  pre-reorg in the test fixture).
- [ ] filter box: "memory" shows the memory settings and hides the rest;
  clearing restores; zero matches says so in plain words.
- [ ] last-category remembered across a simulated relaunch; first run
  opens the first category.
- [ ] pytest-qt navigation scenarios (0m convention): reach a setting in
  every category; reach Status/Schedule/Tuning; start-indexing control
  still works from its new home.
- [ ] layout: the three Indexing views instantiate and lay out without
  overlap/clipping at default, maximised and minimum sizes (the 2b
  acceptance, automated where pytest-qt can assert geometry).

## Done means

Change + tests + suite green + committed by name; CHANGELOG one line per
page. Acceptance sentence: Settings reads like a place where things have
homes — five shelves, a filter that finds any setting by typing, nothing
reworded — and the Indexing page is three clean views where the broken
layout used to be, with the Tuning screen sitting in the third exactly as
the tuning order built it.
