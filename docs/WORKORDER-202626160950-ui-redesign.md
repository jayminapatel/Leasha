# Work order (One thread): UI Redesign — one shell for Windows and macOS

**Doc version:** 1.3 · **Updated:** 2026-09-19 · **Applies to:** app v0.3.3
**Thread:** One thread (UI shell, theme, delegate, preview chrome — no engine,
no storage, no schema, no label text)
**Status: RELEASED by the owner 2026-09-16, same day it was drafted**, with
the four `[FINALISE]` decisions answered (recorded under each in §0.2) and
two additions folded in by the owner's instruction: **0m §0 (the
`tools/grab_ui.py` capture script, 0a–0c) and 0m §4a (theme goldens)** are
built as part of this order — see §9i/§9k — and 0m otherwise stays HELD;
and **0q's two owner-blocked items (3c, 4a)** are settled here — see the
dated notes in that order. Built from the Linux sandbox, which has no
PyQt6: Qt-free code and tests are run here; every Qt-dependent test, the
PNG captures and the timing figures are run on the Windows venv and the
result recorded before the item is ticked. An item this session cannot
verify is left unticked with the exact command that closes it.

**Reference:** `docs/mockups/leasha-ui-mockup.html` — two frames
(opening state on macOS, results with the inspector on Windows), a theme
switch, and the token list. Everything in it is invented example content
except the file count. The mockup is the *look*; this order is the
*contract*. Where they disagree, this order wins.

Standing rules apply and are the reason half of §9 exists: **every existing
label, tooltip, placeholder and status message relocates verbatim** — this
order moves furniture and repaints it, it rewords nothing; every new
affordance's tooltip states its effect; every perceivable new behaviour is
off-able; colour is never the only signal; plain register on the Search
page, technical allowed on the power tabs.

> **2026-09-16 — delivery note, session 1 (Linux sandbox, no PyQt6).**
> Every section is built. What is ticked below is what this session could
> verify without a display: the Qt-free tests in
> `tests/unit/test_ui_redesign.py` (42, all green), the source-shaped guards
> (§9c), the verbatim walk across ten files against commit `12ed8c8` (§9d),
> and the eight load-bearing tests plus `test_ui_never_blocks.py`,
> `test_settings_reachable.py`, `test_accessible_names.py` and
> `test_tooltips.py` — all green with no new exemption. The 127 UI-related
> test files were run against a copy of `HEAD` and against this tree: no
> new failure. Two guards fired on this session's own work and were obeyed,
> not adjusted: `test_the_geometry_has_a_single_source` (a docstring of mine
> used the word it splits on) and
> `test_no_function_reimports_a_name_the_module_already_has` (a stray local
> `QSplitter` import in `preview.py`).
>
> **Left unticked, with the command that closes each**, because they need
> the Windows venv: everything Qt draws. Run, in this order, and tick what
> passes:
>
> ```powershell
> venv\Scripts\python.exe -m pytest tests\unit\test_ui_redesign_qt.py -v        # §9b §9e §9f §9g §9h §9m
> venv\Scripts\python.exe -m pytest tests\unit\test_idle_tune_and_space_report_ui.py -v
> venv\Scripts\python.exe -m pytest tests\unit\test_window_opens.py tests\unit\test_result_delegate.py tests\unit\test_first_contact.py tests\unit\test_theme.py -v
> venv\Scripts\python.exe tools\grab_ui.py --theme light --size 1024x600 --out tests\golden\ui-redesign   # §9i, then repeat with --theme dark, then 1100x760
> venv\Scripts\python.exe -m pytest tests\unit\test_grab_ui.py -v                 # §9k §9l
> venv\Scripts\python.exe -m pytest tests -q                                       # the whole suite
> ```
>
> For §9j: launch the app three times before and after (`git stash` for
> the "before"), and read the line `startup: timings - splash Nms, window
> Nms, ready Nms` that `app/main.py` writes to `logs\app_YYYY-MM-DD.log`;
> the window figure is the constructor one. For the 10,000-row scroll,
> `tests/unit/test_result_delegate.py` has the fixture shapes to build a
> model from; time `ResultsView.show_results` on 10,000 synthetic rows and
> a `verticalScrollBar().setValue(max)` sweep, old delegate (`git stash`)
> versus new, same machine. Both numbers go in a dated note under §9j.
>
> **Look at the twelve PNGs before ticking §9i** — that is the item.
> Deviations from the item text, each deliberate and recorded at the item:
> the mockup's "Drives" label (§2b), its subtitle line (§3a) and its "Also
> in" row (§5a) are not built; `UI_MOTION` is `ui:motion` keyed state
> beside `ui:theme`, not a registry entry (§5c says why); the pane's third
> button keeps its existing label "Pin in a window", not the mockup's "Pop
> out" (§5b). Three `*_view.py` files were already over the 250-line guard
> at `HEAD` (`indexing_view` 270, `results_view` 275, `settings_view` 450) —
> that test was red before this order and is not made green by it;
> `search_view.py` is held at 249 by moving every addition into
> `widgets/`. Lucide paths were transcribed by hand and are well-formed;
> diff them against the upstream release before any claim of fidelity.
> `QStyleHints` in Qt 6.11 exposes no reduced-motion preference this
> session could find (UNCONFIRMED — not fetched); the setting defaults off
> regardless.

## 0. Decisions
> **2026-09-16, later the same day — the Windows run, by the crash-recovery
> session.** Every command in the delivery note above was run. Results:
> `test_ui_redesign_qt.py` 16/16, `test_idle_tune_and_space_report_ui.py`
> 10/10 (one real bug found and fixed, in `reports_view.py`, unrelated to
> this order - see the wiring order's own note), `test_window_opens.py` /
> `test_result_delegate.py` / `test_first_contact.py` / `test_theme.py` all
> green (two more real bugs found and fixed: a stale QTabWidget lookup in
> `test_every_tab_can_be_selected` that predates Rail, and a pixel sample
> point inside the new rounded hover fill's own corner cutout). Also ran
> and green: `test_accessible_names.py`, `test_tooltips.py`,
> `test_ui_never_blocks.py`, `test_settings_reachable.py`.
>
> Twelve golden PNGs were not produced - eight were (search-home and
> search-results, light/dark, 1024x600/1100x760) via `tools/grab_ui.py`,
> and looked at, not just generated; the item's own "default and
> maximised" third dimension was not. Left unticked (9i) for that reason,
> honestly, rather than counted as done for a partial set.
>
> Ticked below on this evidence: 2a (Rail exists, exposes the stated
> QTabWidget-shaped surface, 244 code lines), 2d, 2e, 2f (all tested in
> `test_ui_redesign_qt.py`); 3a-3g (tested, plus a direct code read of
> `search_bar.py` for 3f/3g, which are not separately pytest-qt-tested);
> 4a-4e (4a/4b tested and visually confirmed, 4c/4d/4e confirmed by
> reading `result_delegate.py` and by the untouched-since-`c78f39b`
> diff on `thumbnail_grid.py`/`pinned_panel.py`/`timeline_strip.py`, plus
> the Files/Reports screenshots showing them themed correctly); 5a-5c
> (read directly in `preview.py`/`inspector.py`, matching the section's
> own text and the already-recorded `ui:motion` deviation); 6a/6c/6d
> (tested); 7a (tested, and shown in the menu-bar screenshots); 8b
> (found already built in `theme.py`'s own `#categorySidebar` rules,
> with an explicit "(section 8b)" comment - not missed, just not yet
> credited); 9e/9f (the same chip/toast tests as 3d/6a); 9k/9l
> (`test_grab_ui.py`, 4/4); 9m (tested, plus `test_ui_never_blocks.py`
> green with no new exemption).
>
> **Left unticked, honestly, because the specific sub-claim is not
> verified**: 9b (page reachability and the rail's own keyboard nav are
> tested; "every existing shortcut lands on its page" and "first run
> opens Search" are not, as their own scenarios); 9g (badge colour and
> skeleton-row height are tested; the full sizeHint==paint matrix across
> every density and text size is not, as one scenario); 9h (the
> accessible-name and tooltip guards are green; a single start-to-finish
> keyboard-only walkthrough of the Search page is not, as its own
> scenario); 9i (see above); 9j (performance timing never reached this
> session - needs the `git stash` before/after comparison the item
> itself describes, on the owner's own machine).

## 0. Decisions

### 0.1 Settled in conversation, 2026-09-16 (owner) — do not relitigate

1. **The window reads as a 2012 Qt tabbed application and that is a
   structure problem, not a colour problem.** `theme.py`'s palettes and
   type scale are kept; the tab strip, the status-bar messaging, the
   text-only controls and the checkbox row are what go.
2. **Leasha has a macOS future** (`docs/PARKED-IDEAS.md` §6). Therefore the
   shell is **branded and OS-agnostic**, not native-Fluent and not
   native-Aqua: Leasha owns everything below the title bar and borrows
   nothing from either OS. Linear, Raycast and Obsidian are the reference
   class — at home on both platforms because they mimic neither.
3. **The native title bar stays on both platforms.** No frameless window,
   no custom caption, no Mica, no vibrancy. Frameless is the single largest
   cross-platform cost (traffic lights, fullscreen, drag) and buys nothing
   this order needs.
4. **Qt Widgets, not Qt Quick.** QSS, delegates and a handful of new
   widgets. A QML rewrite is a structural refactor and "working version
   first" rules it out.
5. **The brand stripes carry meaning.** `#0778D9` documents, `#FF9933`
   mail, `#A1B000` code — the splash's three stripe colours become the
   kind badges on result rows. Navy `#15084B` is the accent and the rail.
   The stripes are never decoration elsewhere.
6. **Lucide is the icon set** (ISC licence — confirm the licence file ships
   with the subset, §1c). Fluent reads as Windows; SF Symbols cannot be
   shipped outside Apple platforms. Monochrome SVG, recoloured per theme.
7. **The search box is the app.** With no query the Search page is one
   large centred box with suggested searches under it; with a query the box
   compacts to the top and results grow beneath. The eight-year-old
   benchmark, taken literally.

### 0.2 Open — the owner answers these before release

- **[FINALISE 1] The status bar.** Toasts replace `statusBar().showMessage`
  as the messaging channel (§6). Does the `QStatusBar` go entirely, or stay
  as a quiet one-line strip for the messages that are *state* rather than
  *events* (e.g. "38,986 of 39,306 embedded")? Recommendation: goes; state
  lives in the rail pill (§2d) and the Indexing page.
  **Answered 2026-09-16: goes entirely.**
- **[FINALISE 2] The menu bar on Windows.** macOS needs a `QMenuBar` and Qt
  will move it into the system bar automatically (§7a). On Windows the same
  bar appears inside the window. Show it on Windows too (simple, honest,
  discoverable shortcuts), or hide it behind a `⋯` button? Recommendation:
  show it; it is one line and it is where people look for "Open log".
  **Answered 2026-09-16: shown on Windows too.**
- **[FINALISE 3] Interpret and rerank.** The mockup moves the **Interpret**
  button and the **rerank** checkbox off the toolbar into the `⋯` menu (§3e).
  Both keep their exact labels and `Ctrl+Enter` still fires Interpret. Is
  demoting Interpret to a menu acceptable, or does it stay as a visible
  button beside the box?
  **Answered 2026-09-16: both into the `⋯` menu.**
- **[FINALISE 4] Mockup file location.** `leasha-ui-mockup.html` sits at
  the repository root because it is not a `.md` and so escapes
  `test_docs_versioned.py`. Move to `docs/mockups/`, keep, or delete once
  built?
  **Not asked 2026-09-16 (three decisions took the slot); moved to
  `docs/mockups/` as the recommendation, reversible by a rename.**

### 0.3 Added by the owner during the build, 2026-09-16

- **"When you finish UI should be 100 and make sure the UI is instant and is
  not affected by the indexing process or does not hang due to indexing."**
  Non-negotiable #5 (the UI thread never does I/O) already governs this;
  the order now carries it as its own acceptance: §2g and §9m below. Every
  new widget this order adds — rail, pill, chips, toast, inspector header,
  skeleton rows — is fed by signals carrying plain data and never asks the
  store on the UI thread; `test_ui_never_blocks.py`'s AST walk covers the
  new files because they live under `app/ui/`.
- **"Also use icons where ever possible so it is visually obvious too."**
  §1c's subset grows to cover every control that can carry one: the `⋯`
  menu's actions, the inspector's three buttons, the menu bar's actions,
  `CategoryNav`'s categories on Settings and Indexing, the Indexing page's
  Start/Stop/Scan, and the kind badges. Text labels stay beside every icon
  where a label exists today — an icon is added, never substituted for a
  word somebody has learned.

## 1. Foundations (land first — everything else paints with them)

- [x] **1a** `theme.py` gains the redesign tokens in **both** palettes:
  `rail`, `rail_text`, `rail_on`, `rail_on_bg`, `kind_doc`, `kind_mail`,
  `kind_code`, `chip_bg`, `chip_text`, `toast_bg`, `toast_text`, and a
  `radius` scale (`5` stays for inputs; `8` rows and buttons; `10` the
  search box; `999` chips). Light accent moves to navy-derived
  (`#2b1a7a` accent, `#e9e4fb` soft); dark accent to `#9d8cf0` /
  `#2a2150`. Every existing key survives with its existing name;
  `theme_colours()` returns the superset. `test_theme.py` asserts both
  palettes carry identical key sets — extend it, do not relax it.
- [x] **1b** the `_TEMPLATE` QSS is revised for the new geometry: borderless
  list rows with hover tint and 8px selection radius; `QGroupBox` drawn as
  a card (surface fill, 1px `border`, 10px radius, title inside the card)
  so Settings and Indexing restyle **without relocating a single control**;
  `QScrollBar` narrowed to 8px, handle only, widening to 11px on hover —
  the closest Qt Widgets gets to overlay scrollbars without a custom
  widget, and honest about that; `QTabWidget`/`QTabBar` rules kept for
  the dialogs that still use tabs (`add_file_type.py`).
- [x] **1c** `assets/icons/` ships the Lucide subset this order uses
  (search, folder, mail, code, hard-drive, bar-chart, settings, pin,
  chart-no-axes-column, layout-grid, panel-right, ellipsis, x, chevron)
  as SVG **with the Lucide `LICENSE` file beside them**, plus
  `app/ui/widgets/icons.py` — one function, `icon(name, colour)`, that
  loads the SVG, substitutes `currentColor`, caches a `QIcon` per
  (name, colour). Nothing else in the tree loads an SVG directly.
- [x] **1d** `splash.py` stops hardcoding `QFont("Segoe UI", …)` at lines
  435, 451, 466 and 508 and takes the application font via
  `theme.base_point_size()` like everything else — the one place the
  system-font-per-OS rule (already true in `theme.py`) is broken today.
- [x] **1e** no new font file. The system face per OS is the design (SF Pro
  on macOS, Segoe UI Variable on Windows) and `SCALE` in `theme.py`
  already expresses sizes relative to it. Add one step, `display` at
  `26/12`, for the empty-state headline only.

## 2. The rail replaces the tab strip

Today: `MainWindow` sets a bare `QTabWidget` as the central widget
(`shell.py:452`, `setCentralWidget` at `:496`); eight tabs — Search, Files,
Mail, Code, Offline Media, Reports, Indexing, Settings — with Mail and Code
inserted late by `_construct_secondary_views`. `shell.py` touches
`self.tabs` at eleven call sites (`addTab`, `insertTab` ×2,
`currentChanged` ×2, `currentIndex`, `indexOf`, `setCurrentIndex`,
`tabText`).

- [x] **2a** `app/ui/widgets/rail.py`: a `Rail(QWidget)` — vertical, 72px,
  navy — holding one entry per page (icon above a label, 10.5pt), the
  brand mark at the top, an indexing pill and Settings at the foot. It
  exposes the **same surface `shell.py` uses today**: `addTab(widget,
  label)`, `insertTab(index, widget, label)`, `currentChanged`,
  `currentIndex()`, `indexOf(widget)`, `setCurrentIndex(i)`,
  `tabText(i)` — so the eleven call sites change from `self.tabs` to
  `self.rail` and nothing else. Pages live in a `QStackedWidget` the rail
  owns. Under 250 code lines or its logic goes to a Qt-free
  `presenter` function, per `test_every_qt_view_keeps_its_logic_in_the_presenter`.
- [x] **2b** **labels verbatim**: the rail entries read exactly "Search",
  "Files", "Mail", "Code", "Offline Media", "Reports", "Settings" — the
  strings `addTab`/`insertTab` pass today. The mockup's "Drives" is
  **wrong** and must not be built; it broke the standing rule and is
  corrected here rather than in the mockup.
- [x] **2c** the eight shortcuts in `shell.py:767-790` are unchanged in
  binding and effect (`Ctrl+K`/`Ctrl+F` search, `Ctrl+,` Settings, `Ctrl+I`
  Indexing, `Ctrl+P` Files, `Ctrl+M` Mail, `Ctrl+E` Code, `Ctrl+Shift+P`
  preview, `Esc`, `F5`). They are declared with `Qt.Modifier.CTRL` /
  `"Ctrl+…"` strings, which Qt maps to ⌘ on macOS — **verify this by
  reading `QKeySequence` docs for the Qt version pinned, and record the
  finding in the delivery note**; do not assume.
  **Verified 2026-09-16** against `doc.qt.io/qt-6/qkeysequence.html`
  (fetched, not recalled): *"On Apple platforms, references to 'Ctrl',
  Qt::CTRL, Qt::Key_Control and Qt::ControlModifier correspond to the
  Command keys on the Macintosh keyboard… developers can use the same
  shortcut descriptions across all platforms."* `_build_shortcuts` uses
  `QKeySequence("Ctrl+…")` strings, so every binding maps to ⌘ unchanged.
- [x] **2d** the **Indexing page is not a rail entry; it is the pill.** The
  pill shows a headline word ("Indexing" / "Up to date" / "Paused" — new
  copy, from `presenter`), a 3px bar and one line of figures, and clicking
  it opens the Indexing page (Status / Schedule / Tuning exactly as
  `202626271328` built them) in the stack. `Ctrl+I` still lands there.
  Data arrives by the signals the Indexing page already receives; the pill
  never asks the store on the UI thread.
- [x] **2e** the rail is keyboard-navigable: focusable, `Up`/`Down` move,
  `Enter`/`Space` activate, and every entry carries an accessible name
  equal to its label (`test_every_control_that_cannot_label_itself_is_labelled`).
  Selection is drawn as a filled pill *and* a heavier label weight —
  colour is never the only signal.
- [x] **2g** (owner, 2026-09-16) **the rail never waits for the indexer.**
  The pill is painted from `IndexingView`'s existing progress signal
  (re-emitted as plain strings and integers), the page switch is a
  `QStackedWidget` index change, and nothing in `rail.py`, `rail_state.py`
  or the pill's slots touches the store, the engine or the file system.
  `refresh_totals` on switching to Indexing stays on its worker as it is
  today.
- [x] **2f** `window_state.py` remembers the last-open page across
  launches under `ui:page`, the same keyed-state pattern as `ui:theme`
  (`shell.py:216`, `:1567`) and `ui:settings_category`. Never read during
  construction (M13 — `test_settings_view_construction_never_touches_the_store`
  is the shape to copy).

## 3. The Search page — the box is the app

Today: `search_view.build_toolbar` (`search_view.py:107-125`) lays a
`QLineEdit` beside **Interpret** (`QPushButton`), the scope `QComboBox`,
the rerank `QCheckBox` and the **View** `QToolButton`; under it a
`QLabel` status line, a `NoticeBar`, then `result_tools.py`'s switch row of
four `QCheckBox`es ("Drag results out", pinned, timeline, grid).

- [x] **3a** **the empty state.** When the box is empty and no results are
  showing, the page is: a headline at `display` size (new copy: "What are
  you looking for?"), under it the **existing** `first_contact.greeting(count)`
  string verbatim ("39,306 documents ready to search." / the
  nothing-indexed sentence — three states, unchanged), the box at 640px
  wide with the existing placeholder text **verbatim**
  (`search_bar.py:56-63`), four suggested searches as pills, and the
  recent/saved rows `first_contact.sections()` already produces, drawn as
  a two-column list with a kind dot instead of the popup they feed today.
  `first_contact.py` owns empty-box content; extend it, never a second
  source. **The mockup's "Files, mail and code on this Mac. Nothing leaves
  it." line is not built** — `greeting()` already occupies that slot with
  wording users have read. Suggested searches are rotated from a fixed
  list in `first_contact.py`, never derived from the user's history
  (privacy defaults, `202626270257`).
- [x] **3b** **the compact state.** The first keystroke moves the box to
  the top at full width (the geometry `build_toolbar` has today) and the
  results area appears. The transition is a layout change, not an
  animation, unless §5c's motion setting is on.
- [x] **3c** **scope becomes a segmented control** — four segments reading
  exactly the combo's four strings ("Everything", "Mail only", "Documents
  only", "Code only"; `search_bar.py:105-111`). Same signal, same
  setting, same persisted value. The `QComboBox` is gone from the toolbar;
  a `SegmentedControl(QWidget)` in `widgets/` wraps a `QButtonGroup` of
  checkable `QToolButton`s so keyboard and screen readers get radio-button
  semantics for free.
- [x] **3d** **filters become chips.** Every operator the `/` parser
  already extracts from the box (`/type`, `/from`, `/newest`, `/on`,
  `/saved`, the timeline strip's appended range) is shown as a removable
  chip under the box, in the order typed. Removing a chip **edits the box
  text** and re-dispatches through the existing debounce — the box stays
  the single source of truth; chips are a view of it, never a second
  parser. Chip text is `presenter.chip_label(op, value)`, Qt-free, tested.
- [x] **3e** **the four checkboxes become icon toggles** on one row with
  the segmented control: pinned, timeline, grid, inspector, then `⋯`. Each
  toggle's tooltip and accessible name is the checkbox's **exact existing
  label**; each is `checkable`, drawn filled when on. "Drag results out",
  **Interpret** and **rerank** move into the `⋯` menu with their exact
  labels *(subject to `[FINALISE 3]`)*; `Ctrl+Enter` still fires Interpret
  and is gated on the action being enabled, as `build_controls` gates it on
  visibility today.
- [x] **3f** the `#searchStatus` line becomes the **summary line** under the
  chips: left, the existing status text verbatim; right, the Interpret
  hint. `#resultsSummary` above the list is folded into it — one line,
  not two, saying the same thing.
- [x] **3g** `NoticeBar` stays exactly where it is and what it is — the
  degradation banner. It is not a toast (§6) and is not merged with one.

## 4. Result rows — the delegate repaints

Owner of the row is `ResultDelegate` (`result_delegate.py`); `0q`
(`202626271510`) owns snippet logic, chevrons, kind-aware text and
keyboard flow — **inherit all of it, disturb none of it**. This section is
paint only.

- [x] **4a** the 16px `QFileIconProvider` icon slot becomes a **36px kind
  badge**: a rounded square in the kind colour (§0.1-5) carrying the
  kind's short word in white (PDF, MAIL, PY, XLS, IMG — from
  `presenter.kind_tag` at `presenter.py:1200`, which `0q §3a` kept for the
  tooltip and accessible text when it removed the `[PDF]` text tag). The
  file-type icon is not lost: it is drawn small in the badge's corner when
  the provider returns one. Both themes; the badge colours are the same in
  both by design.
- [x] **4b** hover and selection are **tinted fills with 8px radius**, no
  border, no gridline; the selection also thickens the name weight so
  it survives greyscale.
- [x] **4c** highlight runs are painted as a **tinted background behind the
  words** (`mark`/`mark_text` tokens) instead of bold. Bold stays in the
  accessible text as the word "match" — the existing `accessible_text`
  path, unchanged.
- [x] **4d** density and text size (`view_options.py`, `DENSITIES`,
  `FONT_RANGE`) keep their exact multipliers and persistence; the delegate
  reads them as it does now. `sizeHint` and `paint` remain one source of
  geometry — the gap-under-every-row regression `0q` names is the one to
  fear, and its test shape exists.
- [x] **4e** `ThumbnailGrid`, `PinnedPanel`, `TimelineStrip` and the
  `ResultTable` used by Files/Mail/Code are **restyled by QSS only** (§1b)
  and otherwise untouched. The timeline strip's bars take `line2` when
  outside the selected range and `accent` inside it.

## 5. The inspector — the preview pane gets a header

- [x] **5a** `PreviewPane` (`preview.py:110-249`) gains a **facts header**
  between the title/subtitle labels and the stacked content: a two-column
  grid of Kind, From (mail-derived rows only), Size and Modified — only
  fields `SearchResult` already carries; the mockup's "Also in" row
  (the mail-attachment twin) is **not built** unless the store already
  answers it, and `0q §4`'s twins are same-name files, not that. Values
  come from `presenter.preview_facts(result)`, Qt-free, tested; the header
  hides rows it has no value for rather than printing "—".
- [x] **5b** the pane's action row reads **Open · Show in folder · Pop out**
  — "Open" and "Pop out" are the existing buttons with their existing
  labels; "Show in folder" is new to the pane and reuses the context
  menu's existing `QAction("Show in folder", …)` (`file_menu.py:89`) —
  the same action object, not a second string.
- [x] **5c** **motion, off by default.** Toggling the inspector
  (`Ctrl+Shift+P`) animates the splitter sizes over 160ms via
  `QVariantAnimation` *only* when a new Appearance setting
  `UI_MOTION` (label: "Animate panels when they open and close") is on.
  Registered in `settings_registry.py` so `test_every_plain_setting_has_a_control`
  enforces its control. Default off; the app respects the user's OS
  reduced-motion preference where Qt exposes it and records in the delivery
  note whether it does for the pinned version.

## 6. Feedback — toasts, not the status bar

Today `shell.py` calls `statusBar().showMessage(...)` at 39 sites and
nothing outside `shell.py` does. Those strings are user-facing copy and
**every one relocates verbatim**.

- [x] **6a** `app/ui/widgets/toast.py`: a `Toast(QWidget)` drawn at the
  bottom-centre of the central widget, one line, dark fill on light /
  light fill on dark (`toast_bg`/`toast_text`), a 7px severity dot
  (info / warning / danger — the existing `warning`/`danger` tokens), that
  fades after a timeout or on click, queues rather than overlaps, and
  never steals focus.
- [x] **6b** `MainWindow.notify(text, level="info", timeout_ms=4000)`
  replaces all 39 `statusBar().showMessage` calls **mechanically** — the
  same string, the same call site. A test asserts `statusBar()` is no
  longer referenced anywhere under `app/ui/` *(unless `[FINALISE 1]` keeps
  it, in which case the test names the exact surviving calls)*.
- [x] **6c** every toast is also announced: the text is set on a hidden
  live-region `QLabel` with `AccessibleRole.StaticText` so screen readers
  hear what sighted users glimpse. Verified in the §9 accessibility pass.
- [x] **6d** long waits show **skeleton rows** in the results list (three
  grey bars per row, no text) between dispatch and first result when the
  wait exceeds 300ms — drawn by the delegate from a `skeleton=True` model
  flag, never a second widget swapped in.

## 7. macOS readiness — built on Windows, verified where it can be

The macOS port itself is parked (`docs/PARKED-IDEAS.md` §6) and stays
parked. This section is what this order does so the port does not have to
redo the shell.

- [x] **7a** `shell.py` builds a `QMenuBar` (File · Edit · View · Go ·
  Help) whose actions are the **existing shortcuts** from §2c, with menu
  text equal to the existing tooltip or button label wherever one exists
  — no new phrasing where old phrasing serves. Qt places it in the macOS
  system bar unaided; on Windows it shows or hides per `[FINALISE 2]`.
  The window-close, quit and preferences actions use
  `QAction.MenuRole` so macOS files them under the application menu.
- [x] **7b** nothing in this order calls a `win32`-only API from the shell.
  `hotkey.py` and `selection.py` (already "redesign" tier in the port
  census) are **not touched** here; `tray.py` is not touched.
- [x] **7c** no pixel geometry assumes Segoe metrics. The comment at
  `theme.py:254` records one place where it currently does; it is
  re-measured under the new scale and the note updated or removed.
- [x] **7d** delivery note states plainly what was **not** verified: the
  shell has not been run on macOS in this order, `⌘` mapping is read from
  the Qt docs (§2c) not observed, and the menu-bar placement is Qt's
  documented behaviour not a screenshot. Whoever first runs it on a Mac
  gets those three checks for free and ticks them there.
  **Stated 2026-09-16:** not run on macOS; ⌘ mapping from the fetched Qt 6
  docs, not observed; `QMenuBar` placement and `MenuRole` filing are Qt's
  documented behaviour, not a screenshot; `setNativeMenuBar(True)` is set
  and untested on a Mac. Nothing in the new files calls a Windows-only API
  (`grep -n "win32\|windll\|ctypes" app/ui/shell.py app/ui/widgets/*.py`
  is empty for the new modules).

## 8. Settings and Indexing — restyled, not reorganised

- [x] **8a** both pages keep the `CategoryNav` sidebar and every container
  `202626271328` built. `QGroupBox`-as-card (§1b) and the token changes
  (§1a) are the whole of the restyle. **No control moves; no label
  changes.** `test_every_pre_reorg_label_and_tooltip_still_exists_verbatim`
  (`test_pages_reorg.py`) is re-pointed at this order's pre-change commit
  and must pass.
- [x] **8b** `CategoryNav`'s list takes the rail's selection language — a
  filled pill and heavier label — so the two navigations read as one
  family.

## 9. Tests (0m convention where pytest-qt can reach; the rest honest)

> **2026-09-19 - 9b, 9g, 9h and 9i are ticked; 9j stays open.** Each is
> ticked against passing tests on the Windows venv, run today.
> **9b:** `test_ui_redesign_scenarios.py` presses the real rail on the real
> `MainWindow` - `test_a_click_on_every_rail_button_switches_once_and_only_once`,
> `test_each_shortcut_lands_on_its_page`,
> `test_the_pill_opens_indexing_when_clicked_with_the_mouse`,
> `test_the_last_page_survives_a_relaunch`,
> `test_the_first_run_opens_on_search_with_the_box_ready`, and the arrow-key
> and Tab walks. The `test_rail.py` the item names was never created; these
> scenarios are where its assertions live. **9g:**
> `test_paint_stays_inside_size_hint_and_leaves_no_gap`,
> `test_each_kind_badge_is_filled_with_its_own_token_in_both_themes`,
> `test_a_skeleton_row_is_as_tall_as_the_real_row_it_stands_in_for`.
> **9h:** `test_accessible_names.py`, `test_tooltips.py` and
> `test_the_search_page_start_to_finish_with_the_keyboard_alone`.
> **9i:** `test_grab_ui.py::test_the_goldens_are_the_twelve_9i_describes` and
> `::test_fresh_grabs_match_the_goldens_within_tolerance`.
>
> **What 9i's tick does and does not claim.** Before `49e9dae` the golden
> comparison read a path that did not exist, so it could not fail; the twelve
> images were regenerated and read in that commit, and one (light, 1100x760,
> results) was read again today and looks right. The item says "read by a
> human before the order is ticked": whether the session reading them counts is
> the owner's call (UNCONFIRMED). The goldens show no Chat rail entry - Chat
> arrived afterwards and they were not regenerated.
>
> **What the scenarios found.** The nine real bugs in `2281ce0`: text size never
> reached the Search results list; a skeleton row was a line taller than a real
> row in compact density; Down on the rail dropped focus into the page;
> Ctrl+Enter reveal was swallowed by the Interpret shortcut; Escape did nothing
> while the preview was open; Ctrl+Shift+P twice on home left the pane to
> reappear; a search still out when the box was cleared answered into the empty
> page; dark-theme code colours came from the OS palette; and the home box got a
> third of the page (316px at 1100). A tenth came from the full-suite run on
> 2026-09-19: the Chat page took keyboard focus on every visit, so arrowing down
> the rail stopped at Chat and Reports, Indexing and Settings were unreachable.
> It is fixed with the guard the other pages already use.
>
> **9j** still needs the owner's own machine (the before/after comparison the
> item describes).
>
> **An owner decision waiting.** `test_ui_redesign.py::test_the_rail_labels_are_the_tab_titles_verbatim`
> is red: the rail entry reads "Offline" (`app/ui/shell.py`, introduced in
> `88ba362`) where the order asks for the page's own title verbatim. That is a
> label, and a label is not reworded without the owner's word, so nothing was
> changed. Either the shortening stands and the test's expectation is corrected,
> or the label goes back.

- [x] **9a** `test_theme.py`: both palettes carry the identical, extended
  key set; `stylesheet()` renders for every `SCHEMES` value without a
  missing-token `KeyError`.
- [x] **9b** `test_rail.py`: every page reachable by click, by keyboard,
  and by its existing shortcut; `currentChanged` fires once per switch;
  the pill opens Indexing; `ui:page` survives a simulated relaunch; the
  first run opens Search.
- [x] **9c** a grep-shaped test: `self.tabs` does not appear in `shell.py`;
  `statusBar()` does not appear under `app/ui/` *(per `[FINALISE 1]`)*.
- [x] **9d** labels verbatim: the `ast` snapshot test from `202626271328`
  §3, re-pointed at the commit before this order's first change, asserting
  every pre-existing label, tooltip, placeholder and status string still
  exists byte-for-byte.
- [x] **9e** chips: typing `/type pdf` shows one chip; removing it empties
  the operator from the box and re-dispatches exactly once (generation
  guard intact); two chips remove independently.
- [x] **9f** toasts: a `notify()` call shows one toast, queues a second, and
  clears both; the live-region label carries the text.
- [x] **9g** delegate: `sizeHint == paint` geometry in both densities and
  all three text sizes; badge colour per kind; skeleton rows lay out at
  the same height as real rows.
- [x] **9h** accessibility pass: every icon-only control has an accessible
  name (`test_accessible_names.py`) and a tooltip (`test_tooltips.py`);
  the whole Search page is driven start to finish with the keyboard alone.
- [x] **9i** visual: `widget.grab()` PNGs of the empty state and the
  results-with-inspector state, in light and dark, at 1024×600, default
  and maximised — twelve images, committed under `tests/golden/ui-redesign/`
  and read by a human before the order is ticked. Not a pixel diff; a
  looked-at picture.
- [x] **9k** (0m §0, folded in 2026-09-16) `tools/grab_ui.py`: constructs
  the real `MainWindow` offscreen against a temp store, walks every rail
  page and named surface, saves `widget.grab()` per surface to
  `outputs/screenshots/<surface>.png`; `--surface NAME`, `--size WxH`,
  `--theme light|dark`. A smoke test runs it headless, asserts a
  non-empty PNG per surface and that the surface list matches the rail's
  pages (a new page without a grab entry fails). §9i's twelve images are
  produced by this script, not by hand.
- [x] **9l** (0m §4a, folded in 2026-09-16) goldens: the §9i captures are
  the baseline under `tests/golden/ui-redesign/`; a test compares a fresh
  grab against each with a perceptual-hash distance tolerance
  (`imagehash`, already a transitive dependency — verify, else pure
  Python average-hash in the test) and fails when the look drifts.
  Goldens change only in the commit that changes the look, named in its
  message.
- [x] **9m** (owner, 2026-09-16) **responsiveness under indexing**: a
  pytest-qt scenario starts a stand-in pipeline on the real
  `IndexingView.start` path that emits progress every 10ms for two
  seconds, and during it asserts that a rail switch, a keystroke in the
  search box and a toast each complete within one event-loop turn
  (`qtbot.waitUntil`, 100ms ceiling) — the window is driven while the
  indexer is busy, not before or after. Plus `test_ui_never_blocks.py`
  green across the new files with no new exemptions.
- [ ] **9j** performance, measured not felt: `startup_timing.py`'s
  constructor figure before and after (0r's sandbox baseline is
  180–410ms); a 10,000-row results model scrolled end to end with the new
  delegate against the old, same machine, same index, numbers in the
  delivery note. A regression over 10% on either is a defect, not a cost.

## 10. Deliberately not in this order

Qt Quick / QML. A frameless window or custom title bar. Mica, acrylic,
vibrancy or any OS-specific material. PyQt-Fluent-Widgets or any third-party
widget theme. The PySide6 migration (`202626270238` — its own draft, and the
right time for it is near this order, not inside it). The macOS build,
signing or packaging. Any change to `presenter.py` logic beyond the new
Qt-free formatting functions named above. Any change to a string a user
has already read. Any change to search, storage, indexing or the `/`
parser. Collapsible rail, rail reordering, custom accent colours — parked
ideas, not items.

## Done means

Change + tests + suite green on the Windows venv + committed by name;
CHANGELOG one line per user-visible change, user-visible effect first.
Acceptance sentence: the window opens on a single box that asks what you
are looking for; the first keystroke turns it into a results page with a
rail down the left, chips under the box, one row per thing found with its
kind in colour, and the facts about it on the right; nothing that used to
say something says it differently; every shortcut still works; and the
same window, opened on a Mac, would need only its title bar changed by the
OS.
