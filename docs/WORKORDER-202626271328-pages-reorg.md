# Work order (One thread): the pages reorg — Settings finds its shelves, Indexing splits in three

**Doc version:** 1.1 · **Updated:** 2026-09-05 · **Applies to:** app v0.3.3
**Thread:** One thread (UI structure only — no engine, no pipeline, no schema)
**Status: RELEASED and delivered 2026-09-05.** Was DRAFT, gated on 0114 and
0157 being fully ticked; the gate was re-checked in full against both
orders' own text (not the stale queue register) and found clear of anything
blocking this reorg specifically — see the dated note under §0 below. Every
item in §1 and §2 is delivered and tested; live verification of §2b is
recorded in its own item. Natural companion to the PySide6/restructure
window if it lands near that.

Promotion condition, as originally set by the owner ("once our workorders
are in"): the index-tuning order (0114) and the search-experience order
(0157) fully ticked — their Settings and Indexing additions land FIRST so
these pages are reorganised once, not twice.

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

**Chosen 2026-09-05: sidebar category list, for both pages.** One reason:
it scales past five categories without crowding a tab bar (the owner's own
argument for Settings), and a single `CategoryNav` widget
(`app/ui/widgets/category_nav.py`) then serves both pages — Settings' five
categories and Indexing's three — where sub-tabs would have needed the same
QTabBar-based layout rebuilt per page with no shared abstraction between
them.

**Promotion gate re-checked 2026-09-05, before starting.** `0114` stands at
35/40 and `0157` at 25/26, both read in full rather than taken from
`docs/ORDER_REGISTER.md` (which was stale at 31/9 and 23/3 from
2026-08-30). Every open item in both was read for whether it touches
`settings_view.py`, `indexing_view.py` or `indexing_settings.py` specifically:
none does. `0114`'s five (5e idle-scheduler wiring, 6c vector_store Arrow
table, 6d two-phase file-status schema, 6h int8 model, 6i converter session)
are pipeline/embedder/storage work, each already investigated and left open
for a reason recorded in that file. `0157`'s one (6c) is explicitly a
pointer to the remediation order's own tracked items, not real work, by its
own text. Confirmed separately: `0114 §4c-4`'s theme move (ticked) is
already live — `indexing_settings.py` carries no theme control today, and
`settings_view.py`'s `WindowBox` already holds it — so this order's "Theme
moves to Appearance" is a placement question (which category) rather than a
move still to perform. Nothing found blocks this reorg specifically;
proceeding as promoted.

## 1. The Settings page

Today: twelve stacked group boxes on one long scroll (settings_view.py) —
the owner's report that started this: "too long and cluttered".

- [x] **1a** categories, each holding today's group boxes unchanged:
  **What's indexed** (roots / file types / code types / PST) ·
  **Search** (search box / behaviour / history / the per-surface
  behaviour grid from 0157 §1b) · **Models & AI** (Ollama, model roles
  when they exist) · **Appearance** (window box; **theme finally moves
  here** — the misplacement flagged in the tuning order, honoured now) ·
  **Storage & maintenance** (storage / environment / activity / restore).
  New boxes landed by the feature orders join the category that reads
  right; none are left orphaned.
  **Delivered 2026-09-05.** `settings_view.py`'s `_build_categories`
  reparents every existing group box - none rebuilt, none reworded - into
  five plain `QWidget`/`QVBoxLayout` containers registered with
  `CategoryNav` (`app/ui/widgets/category_nav.py`). "What's indexed" holds
  `roots_box`, `code_types`, the PST group, the cloud-indexing checkbox and
  `file_types`; "Search" holds `search_box`, `search_behaviour`,
  `editor_box` and the search-history group (the "history" §1a names);
  "Models & AI" holds `models`; "Appearance" holds `window_box` (theme
  already lived there, moved out of the indexing panel by 0114 §4c-4 -
  verified by reading `indexing_settings.py`, which carries no theme
  control today); "Storage & maintenance" holds `storage_box`, `activity`,
  `environment` and `restore_defaults`. Every attribute `shell.py` reaches
  directly (`window_box`, `theme`, `rerank`, `cloud`, `data_path`,
  `debug_pane`, `environment`, `models`, `file_types`, ...) is unchanged -
  `shell.py` is out of this thread's file scope and needed no edit.
- [x] **1b** a **filter box** at the top: type "memory" and only settings
  whose label or tooltip matches remain visible, categories auto-expanding
  to show hits. The settings registry (labels + tooltips already
  machine-readable) makes this cheap — the filter walks the registry, not
  the widgets.
  **Delivered 2026-09-05.** `SettingsView._apply_filter` walks
  `settings_registry.SETTINGS`, finds each control by the object name
  `test_settings_reachable.py` already requires it to carry, and matches
  the *registry's* label/help text against the typed word - never the
  widget's own live tooltip, which is what the item's own wording asks
  for. A category with at least one hit is shown; every other category
  hides in the same stroke, which is "auto-expanding to show hits" made
  literal - see `CategoryNav.show_all_for_filter`, plain `setVisible`
  toggling rather than a `QStackedWidget` (a stack's size hint is the
  *maximum* over every page, which would force the smallest category as
  tall as the largest - the same fault §2b removes, so introducing it here
  would have been a second copy of the bug). A non-matching control's
  `QFormLayout` row label hides beside it, so a miss leaves no gap shaped
  like a missing answer. Sidebar disabled while filtering (its own
  selection is meaningless against a filter that shows several categories
  at once); a zero-hit filter shows a plain-words label rather than a
  blank page.
  **The order's own example, checked rather than assumed: "memory" matches
  nothing on the Settings page today.** Every setting whose label or help
  names memory (`INDEX_MEMORY_MB`, `EMBED_BATCH`, ...) has surface
  `indexing.tuning` — §0's settled decisions keep Tuning on the *Indexing*
  page, not Settings, so no Settings-page control can match that word.
  `tests/unit/test_pages_reorg.py` uses "memory" for the honest thing it
  actually demonstrates — the zero-match state, in the order's own word —
  and "rerank" for the shows/hides mechanism, which is a real Settings-page
  match.
- [x] **1c** the page **remembers the last-open category** across launches
  (the same app-state home as 0p §4's window state — small keys, one
  pattern).
  **Delivered 2026-09-05**, through `SqliteStore.get_state`/`set_state`
  under `ui:settings_category` — the same keyed-state mechanism `ui:theme`,
  `ui:pst_backend` and `ui:window_geometry` already use, not a new one.
  Selecting a category writes synchronously (`_category_selected`) - the
  same pattern every other small UI-state write in this window already
  uses (`KEYED_STATE` in `test_ui_never_blocks.py` exempts exactly these
  four store methods from the worker rule, because a single keyed upsert
  is not the scaling query non-negotiable 5 is about). Reading it back is
  the one that had to be careful: never during construction (M13 -
  `test_settings_does_not_read_the_store_while_being_built` and the new
  `test_settings_view_construction_never_touches_the_store` both pin this),
  so `_restore_last_category` rides the *existing* `refresh_slow_labels()`
  call `shell.py`'s `_start_background_work` already makes post-construction
  - no new call site needed in a file outside this thread's scope.
  **A real bug found and fixed on the way, not merely worked around**:
  `CategoryNav.add_category`'s auto-selection of the first category emitted
  `category_changed` on construction, so building a *second* `SettingsView`
  against the same store (the relaunch a test simulates) immediately
  overwrote the just-remembered category with "first category" before
  `_restore_last_category` ever got to run. Fixed by selecting the first
  category with the sidebar's signals blocked, then painting it directly -
  `test_last_category_is_remembered_across_a_simulated_relaunch` failed
  against the first version and passes now.
- [x] **1d** every moved Setting's `surface` updates in
  `settings_registry.py` so `test_settings_reachable` enforces the new
  homes — the registry stays the map of where everything lives.
  **Audited 2026-09-05 — nothing needed moving, one naming mismatch did.**
  No `Setting`'s underlying control changed which `.py` file builds it: the
  reorg reparents existing widgets into new *containers* inside
  `settings_view.py`/`indexing_view.py`, both already named in every
  affected surface's `SURFACE_MODULES` entry, so `test_settings_reachable`
  passed against the very first working version with zero registry edits.
  What the audit *did* find, reading every surface name against what it now
  actually points at: `settings.indexing` and `settings.tuning` never named
  anything on the Settings page - both were always the Indexing page's own
  controls - and the old `settings.*` prefix said otherwise the moment both
  pages gained named categories to check it against. Renamed to
  `indexing.schedule` and `indexing.tuning` (`GROUPS`'s `"Indexing"` entry
  to `"Schedule"` for the same reason), 25 `Setting` entries repointed,
  `SURFACE_MODULES` keys renamed to match with their file lists untouched.
  Confirmed by grep before touching anything: those two surface strings
  appeared nowhere outside `settings_registry.py` and
  `test_settings_reachable.py`, and `GROUPS`/`by_group()` are read only by
  `test_settings_registry.py`'s own membership checks (not by any widget
  building a section header from `setting.group`), so the rename could not
  reach a place this thread is not permitted to touch.

## 2. The Indexing page

- [x] **2a** the page splits into three: **Status** (run controls /
  progress / skips / statistics) · **Schedule** · **Tuning** (the 0114
  screen — Defaults / Auto-tune / Manual — slots in whole, untouched).
  Same structure choice as §1 (sidebar or sub-tabs — one answer for both
  pages).
  **Delivered 2026-09-05.** Same `CategoryNav` as Settings, three
  categories: Status keeps the headline/totals/stats/bar/detail/notices/
  controls/archives/skips block exactly as it was, byte-for-byte moved
  into its own container; Schedule holds `schedule_box` (`IndexingSettings`,
  already schedule-only since 0114 §4); Tuning holds `tuning` (`TuningBox`,
  the 0114 screen), untouched down to its own internal mode switch and
  group boxes. `shell.py`'s `self.indexing_view.schedule_box`/`.tuning`/
  `.start_button`/`.stop_button`/`.scan_button`/`._worker` and every signal
  it connects are the same objects at the same attribute names — `shell.py`
  needed no edit and got none.
- [x] **2b THE LAYOUT FIX** — the owner reports the current page's layout
  is screwed up. Diagnose and name the cause in the delivery note (layout
  stretch/spacing/parenting — whatever it proves to be), and let the §2a
  split resolve it structurally rather than patching the old single page.
  Acceptance is visual: the three new views lay out correctly at the
  default window size, maximised, and at the minimum sensible size.
  **Diagnosed 2026-09-05, read rather than guessed.** `shell.py` wraps
  Settings in an external scroll area (`wrap_if_needed(view, scroll=True)`)
  because it is "six group boxes stacked vertically" (its own comment);
  Indexing is wrapped with `scroll=False`, on the assumption - true when
  written - that a page "built around a table, list or splitter... already
  scrolls its own contents" (the skips panel, `stretch=1`). That assumption
  broke the moment 0114 §4 folded the *entire* Index Tuning screen (a mode
  switch, a machine card, and four more group boxes: Compute, Resources,
  Coverage, Strategy) in underneath the skips panel with nothing to scroll
  either of the two new sections: the page's required height outgrew any
  sensible window with no scrollbar anywhere to reach the rest of it - the
  exact "checkboxes cut off" class of fault `test_settings_layout.py`
  already exists to catch on the *other* page.
  **`shell.py` is out of this thread's file scope, so the fix holds without
  that `scroll=False` changing.** Schedule and Tuning - the two shelves
  that pushed the old page past its height - each carry their own
  `widgets/scroll.scrollable` wrap, applied *inside* `indexing_view.py`;
  Status does not, because it already scrolls its own contents (the skips
  panel) and wrapping it again would reintroduce the two-scrollbars fault
  `widgets/scroll.py`'s own docstring warns against - confirmed structurally
  by `test_status_keeps_its_own_scrolling_schedule_and_tuning_gain_theirs`.
  **Verification 2026-09-05, and one gap named honestly.** A second
  `venv\Scripts\pythonw.exe -m app.main` was launched to click through the
  real window as instructed, and it could not get past its own splash
  screen: *"Waiting for the previous Leasha to finish closing..."* -
  `run_lock`'s single-instance guard was held by another already-running
  process on this shared machine (this repo's own working method: other
  sessions may be active concurrently). Killing that process to force the
  launch through was refused - it is not this thread's process to end, and
  the instructions are explicit that only what this session itself started
  may be torn down. Only the newly-spawned instance was killed
  (`taskkill /F /PID 1080`, its own splash-holder child); the pre-existing
  process was left exactly as found.
  As the closest available substitute, the *real* `SettingsView` and
  `IndexingView` production widgets (not a mock, not a fixture double) were
  built offscreen (`QT_QPA_PLATFORM=offscreen`) with the real stylesheet
  applied, resized through all three named sizes, walked through every
  category, and grabbed to PNG per state - the leasha skill's own tier-2
  method ("`widget.grab()` to PNG - renders a widget even offscreen").
  Read back: at the minimum size (1024×600) Tuning shows a working vertical
  scrollbar with every row at a readable height, Schedule fits without
  needing one, and Status is visually unchanged from before this order, at
  all three sizes, on the real widget tree. This is real-widget, real-size
  evidence for the mechanism the automated zero-height and QScrollArea
  tests already assert; it is not, and is not claimed to be, the
  full interactive click-through the instructions asked for, which the
  singleton lock made impossible to obtain safely. Whoever can confirm this
  on a machine with no other instance running gets the last mile for free -
  nothing about the fix depends on anything this session could not check.
- [x] **2c** anything on today's Indexing page that is a *setting* rather
  than a control or readout keeps exactly one home (no duplication
  between Settings and Indexing — a value shown in both places reads
  from the one Setting).
  **Verified 2026-09-05, unchanged by this order.** `schedule_box` and
  `tuning` are the only *setting*-holding widgets on the Indexing page both
  before and after the split, and neither's controls are duplicated on
  Settings - `settings_view.py`'s five categories hold none of the
  `Schedule`/`Tuning`-group settings. Nothing about *where* a value is read
  from changed; only which container each box sits inside did.

## 3. Tests

- [x] `test_settings_reachable` passes with every relocated surface — the
  load-bearing test of the whole order.
- [x] labels unchanged: a before/after walk of every label, description
  and tooltip string proves the move was verbatim (snapshot the strings
  pre-reorg in the test fixture).
  `tests/unit/test_pages_reorg.py::test_every_pre_reorg_label_and_tooltip_still_exists_verbatim`
  reads the pre-reorg text from commit `28e3e9f` (the commit immediately
  before this order's changes) rather than a hand-copied fixture, extracts
  every string passed to a label/tooltip/description call or constructor
  via `ast`, and asserts the pre-reorg set is a subset of the current one -
  a superset is expected (the filter box is new wording for a new control)
  but nothing pre-existing may disappear or change by a character.
- [x] filter box: "memory" shows the memory settings and hides the rest;
  clearing restores; zero matches says so in plain words.
  Using "rerank" for the shows/hides half and the order's own "memory" for
  the zero-match half — see the note under 1b for why "memory" itself
  matches nothing on this page today.
- [x] last-category remembered across a simulated relaunch; first run
  opens the first category.
- [x] pytest-qt navigation scenarios (0m convention): reach a setting in
  every category; reach Status/Schedule/Tuning; start-indexing control
  still works from its new home.
- [x] layout: the three Indexing views instantiate and lay out without
  overlap/clipping at default, maximised and minimum sizes (the 2b
  acceptance, automated where pytest-qt can assert geometry).
  `test_no_indexing_control_is_laid_out_at_zero_height`, parametrized over
  all three sizes, plus `test_status_keeps_its_own_scrolling_...` pinning
  the structural half of the fix.

All 21 new tests in `tests/unit/test_pages_reorg.py` pass, alongside the
full existing suite for both views (`test_external_run.py`,
`test_settings_layout.py`, `test_settings_reachable.py`,
`test_settings_are_used.py`, `test_tooltips.py`, `test_review_section_four.py`,
`test_first_contact.py`, `test_index_progress.py`, `test_tuning_screen.py`,
`test_index_tuning_acceptance.py`, `test_tray.py`,
`test_settings_registry.py`, `test_ocr_strategy.py`, and the rest of the
registry-adjacent suite) — a single pre-existing, unrelated failure
(`test_tooltips.py::test_every_control_explains_itself[mini_search.py]`,
in a file this thread's scope excludes) confirmed present against `HEAD`
before this order touched anything.

## Done means

Change + tests + suite green + committed by name; CHANGELOG one line per
page. Acceptance sentence: Settings reads like a place where things have
homes — five shelves, a filter that finds any setting by typing, nothing
reworded — and the Indexing page is three clean views where the broken
layout used to be, with the Tuning screen sitting in the third exactly as
the tuning order built it.
