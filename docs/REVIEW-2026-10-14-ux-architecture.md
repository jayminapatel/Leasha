# Review 2026-10-14 - UX/UI and architecture survey

**Doc version:** 1.0 · **Updated:** 2026-10-14 · **Applies to:** app v1.0.3

An end-to-end survey of the window against a Raycast / Linear / Craft / Apple Photos bar,
read from the code at `d8e9471`. Four read-only audits ran side by side (navigation, design
system, search and media, contact sync); every High finding below was re-read by hand.

**What this is not.** Nothing here was watched in the real window: the owner's Leasha window
was not available to this session, so look, spacing, DPI and real latency are **UNVERIFIED on
Windows** throughout. Numbers are constants read from the code or figures quoted from
`BUILD_SPEC_V2.md`, not timings taken here. Leasha is PySide6, not a web app: "routing" is a
`QStackedWidget` behind the rail, "skeleton loaders" are `widgets/skeleton.py`, and "masonry"
does not exist - Photos is a uniform square grid by the owner's choice (`photo_thumbs.py:190`).
This review supersedes nothing; the 2026-10-12 Design canvas and UI phase B are the baseline it
was read against.

## 1. Executive summary

The foundation is stronger than most desktop apps at this stage. One theme with token-only
colours (the AST guard finds 0 offenders), one button table, one right-click menu builder, one
list-key contract, 17 of 19 dialogs on `dialog_frame`, a two-tier progressive search with strict
stale-answer discipline, a Raycast-grade quick-search box, a Photos grid with disk cache, blur-up
and neighbour preloading, and a per-field merge window for contacts. What separates it from the
benchmark is not visual polish but **the keyboard model, status that is honest everywhere, and
the paint layer**.

**Top 3 UX friction points**

1. **No command palette, and keys mean different things in different places.** Ctrl+K only
   focuses Search (`shell.py:2336`); `/` inserts filters, never actions (`presenter/commands.py:58`).
   Esc on any page empties the *Search* page's box (`shell.py:2380-2389`). Ctrl+P is Files in the
   window and Preview in the quick search (`shell.py:1494`, `mini_search.py:767`). Arrow keys
   from the box into the list work only on Search and the quick search.
2. **Sync status is scattered, and in places wrong.** Live progress lives in the Sync window
   (opened from Contacts) and on Settings > Accounts while it is visible; Mail has no sync entry at
   all. On Settings > Accounts the **Allow** and **Fix these** buttons do nothing
   (`accounts/panel.py:515-529` handles neither key that `account_activity.actions_for` offers). A
   partial run reads "Up to date" with a tick. Allow deletes without naming what it deletes.
3. **Search does not say when it is working, or when it found nothing.** Once rows are on screen a
   refinement shows no busy sign for 0.4-3.3 s (`skeleton.py:108` only fills an empty list); a
   zero-result answer updates the status line but keeps the previous query's rows
   (`search_view.py:339` then `:351`). With the photo grid on, Down/Up move the hidden list
   (`search_view.py:157`).

**Top 3 technical-debt areas**

1. **The paint layer is duplicated and has no focus state.** Seven `paint()` overrides, none
   checking `State_HasFocus`; selection drawn three ways; the kind badge, font derivation and token
   lookup each implemented two or three times (`result_delegate.py`, `mini_search_rows.py`,
   `contacts/delegates.py`).
2. **The guards have blind spots, so the rules erode quietly.** The spacing guard ignores any call
   with one off-scale number (98 such calls, not "about ninety"); 9 `QMessageBox.question` calls
   bypass `confirm.py` with Yes/No buttons; 15 hand-built `QMenu`s escape the menu guard;
   `QTableView` is missing from the hover/selection selectors.
3. **The sync error contract has drifted from the engine.** Retry-After is parsed and then
   dropped before the schedule (`engine.py:998`; `http.retry_after_of` has no caller); only 503 is
   treated as temporary; `ERR_SYNC_CONFLICT_HELD` is never raised; two suggestion texts send the
   user to an Undo that `can_undo` refuses for a held run; the editor and the Changed-in-two-places
   dialog settle a clash by two different paths.

## 2. Component and heuristic audit

Severity: **H** high, **M** medium, **L** low. Effort: S hours, M a day or two, L a wave.
**Fault** marks behaviour that is wrong today (fixable under "fix what you find");
**Decision** marks a change to words or behaviour that is the owner's call.

### 2.1 Information architecture and navigation

The map: a 72 px rail of nine reorderable pages (Search, Mail, Photos, Files, Contacts, Code,
Browse, Chat, Tidy; `rail_order.py:156`) plus the Indexing pill and Settings at the foot - eleven
destinations, six Settings categories. Pages swap instantly (`rail.py:388`); order and last page
are remembered. No back/forward history exists.

| ID | Sev | Screen / component | Current implementation | UX violation / gap | Recommended refactoring | Effort | Kind |
|---|---|---|---|---|---|---|---|
| IA-1 | H | Window, Ctrl+K | Focuses Search (`shell.py:1499, 2336`); actions only in menu bar and toolbars | No palette for verbs and places, the core of Raycast/Linear | Qt-free `presenter/actions.py` built from the menu bar's existing actions; palette on the `CommandPopup` frame; `>` for actions, plain text falls through to Search | M | Decision |
| IA-2 | M | Window, Esc | Window-wide action clears `search_view.input` whatever page is in front (`shell.py:2389`) | Silently discards a hidden page's query; Esc does not clear the page you are on | Clear Search only when it is current; otherwise the current page's own clear | S | Fault |
| IA-3 | M | Shortcuts | Ctrl+P = Files vs quick-search Preview; no keys for Contacts, Photos, Browse, Tidy, Chat; Go menu omits Search and Chat | One key, one meaning; positional page keys | Ctrl+1..9 by rail position; quick-search preview to Ctrl+Shift+P; every page in Go | S | Decision |
| IA-4 | M | Scopes | Search: Everything / Mail only / Documents only / Code only (`search_bar.py:121`); quick search: All / Files / Mail / Photos / Code (`quick_search.py:58`) | Two vocabularies, Photos only in one | One `SCOPES` table for both boxes | S-M | Decision (labels) |
| IA-5 | H | Accounts and sync | Setup in Settings > Accounts; live progress in the Sync window from the Contacts toolbar; Mail has no sync entry | "Is my Gmail fetching?" is answered under Contacts | Sync window from Mail, the Indexing pill and the palette; later one "Accounts & sync" place | M | Decision |
| IA-6 | M | Photos people and Contacts | Contact panel can link a face group (`contacts/index_panel.py:250`); Photos naming has no contact link | One-way link | "Open contact" / "Link to contact" on person entries; "See all" from a contact to Photos/Mail with a prefilled switch | M | Decision |
| IA-7 | M | Face naming | `PhotoTaggerPage` inside Photos and again as its own window (`shell.py:2067`) | Two hosts, two reload paths | Go menu to `_show_photos(naming=True)` (`shell.py:2481`) | S | Fault |
| IA-8 | L | Contacts toolbar | Find duplicates in `TOOLS` and `MORE_TOOLS` (`contacts/toolbar.py:34, 46`) | Same tool twice | Drop the More copy | S | Decision |
| IA-9 | L | Ctrl+F | Outside a preview always jumps to Search (`shell.py:2341`) | Benchmark: filter this view | Current page's filter first, Search as fallback | S | Decision |
| IA-10 | L | History | None | Deep links (search to contact, sync to account) have no way back | Page history on Alt+Left/Right and mouse back | M | Decision |
| IA-11 | L | Breadth | 11 destinations; occasional pages (Browse, Tidy, Chat) at Search's level | Benchmarks keep 5-7 | After IA-1, let the owner hide pages from the rail-order store | M | Decision |

### 2.2 Component consistency and design-system adherence

Measured with per-line regexes over `app/ui`, `theme.py` excluded:

| Metric | Rest | Contacts | Total |
|---|---|---|---|
| `setStyleSheet` outside the theme | 24 | 9 | 33 |
| Margin/spacing calls with a number off 4/8/12/16/24 | 60 | 38 | **98** (6 x51, 2 x28, 10 x22) |
| `setIconSize(QSize(n, n))` literals | 0 | 4 | 4 (all exempt files) |
| `setPixelSize` calls | 3 | 7 | 10 |
| `QMessageBox.question` outside `confirm.py` | 9 | 0 | 9 |
| `QMenu(` outside `menus.py` | 10 | 5 | 15 |
| Delegates checking `State_HasFocus` | 0 | 0 | 0 of 7 |
| Hex colours failing the colour guard | 0 | 0 | 0 |
| `QDialog` subclasses framed | | | 17 of 19 (two allowlisted) |

| ID | Sev | Component | Current implementation | Violation / gap | Recommended refactoring | Effort | Kind |
|---|---|---|---|---|---|---|---|
| DS-1 | H | Confirmations | 9 `QMessageBox.question` with Yes/No (`controllers/settings_controller.py:262, 874, 955, 1090`; `outlook_cleanup_controller.py:66`; `photo_tagger_page.py` x4), two destructive, one with Qt's default left in | Guide §8: the button names its verb, Cancel is default | Route through `confirm()`; guard on `QMessageBox.question(` outside `confirm.py` | S | Decision (new verbs) |
| DS-2 | H | Painted lists | No delegate draws keyboard focus; results show selection tint only (`result_delegate.py:228`); three selection styles | Keyboard users lose their place; hue-only marker (the theme's own warning, `theme.py:272`) | One `paint_row_state()` helper: rounded hover/selection fill plus a 2 px `focus_ring` when the view has focus | M | Fault |
| DS-3 | M | Tables | Hover/selection rules name `QTableWidget::item` only (`theme.py:301, 305, 332`); `QTableView` used at `contacts/page.py:148`, `photo_browser.py:469`, `spreadsheet_view.py:83` | Photos Details and spreadsheet previews unthemed (UNCONFIRMED on screen); the same bug once hit QListView (`theme.py:288`) | Add `QTableView::item` / `QTreeView::item` to the selectors | S | Fault |
| DS-4 | M | Focus rings | 2 px on fields, 1 px on buttons, chips, lists; tabs use `accent`; rail uses label ink | No single focus treatment | `focus_width` token (2 px) on every `:focus`; tab-reason property to imitate focus-visible | M | Decision (look) |
| DS-5 | M | Spacing guard | `test_ui_style_rules.py:300-314` flags a call only if every number is on scale | New off-scale calls pass unseen | A ceiling test (<= 98, ratchets down), then migrate file by file with a visual pass | S guard / L migration | Fault (guard) |
| DS-6 | M | Drop-down menus | 15 hand-built `QMenu`s, some with no icons (`conflicts_dialog.py:52`, `duplicates_dialog.py:104`, `contacts/toolbar.py:119`) | Guide §3 icons rule enforced for right-click only | `menus.attach(button, lines)`; widen the guard with a tray/View allowlist | M | Fault |
| DS-7 | M | Cards | `AnswerCard` QFrame with no theme rule, `MailCardView` rule-only, `PersonCard` inline sheet with literal 8px, `MachineCard` a QGroupBox | Four cards, four looks | `#card` rule (surface, border, `radius_box`, `SPACE_M`) and a `Card` base | M | Decision (look) |
| DS-8 | M | Type scale | `page_frame.TITLE_SCALE` 1.25, x13/12, x1.33, x1.35, literal 11pt, 10 `setPixelSize` - none in `SCALE` | Five ad-hoc steps; pixels against the guide | Add a `title` step; `scaled_font(base, step)`; extend the pixel guard | M | Fault (pixels) |
| DS-9 | L | Weights | 600, 700 and 800 all used for emphasis | No weight tokens | `WEIGHT` table and helper | S | Decision (look) |
| DS-10 | L | Radii | 16 literal radii in `theme.py`; 8 px row radius declared three times; `mini_search.py` private sheet | Same value, three sources | `radius_menu`, `radius_window` in `RADIUS`, exported to painters | S | Fault |
| DS-11 | L | Delegate code | Badge, font and token helpers duplicated across three files | Drift (badge radius already differs) | `widgets/paint_kit.py` | M | Refactor |
| DS-12 | L | Micro-states | Primary `:pressed` = rest; danger has no pressed or focus; segmented no hover; rail no pressed | Press gives no feedback | `accent_pressed` token and the missing rules | S | Fault |
| DS-13 | L | Style guide | §7 still lists `smart.py`, `ToolDialog`, `PlanDialog` as unframed (they are framed); `smart.py:118`, `person_photo.py:63` still use a raw Ok/Cancel box | Guide not true now | Correct §2 and §7; move both to `dialog_frame.buttons()` | S | Fault (doc) |

### 2.3 Media and search discovery

Keystroke to answer on Search: 150 ms keyword tier (`presenter/search.py:18`), 400 ms full tier
(`:22`), every search on a worker, skeleton after 300 ms only on an empty list, unreranked rows
first and the reranked order redrawn in place, stale answers dropped by generation. Spec: warm
search under 300 ms **not met** - 177-976 ms rerank off, 991-3,342 ms rerank on
(`BUILD_SPEC_V2.md:799`). Page debounces differ: Files 80, Code 90, Mail 120, quick search 180,
Photos 220 ms.

| ID | Sev | Screen / component | Current implementation | UX gap | Recommended refactoring | Effort | Kind |
|---|---|---|---|---|---|---|---|
| MS-1 | H | Search, in flight | Skeleton only on an empty model (`skeleton.py:108`); no shimmer on Search | Old list looks like the answer for up to 3 s | `ShimmerBar` on the results header from ~120 ms after dispatch to the finished answer | S | Fault |
| MS-2 | H | Search, photo grid | Down/Up always go to `self.results` (`search_view.py:157`) while the grid replaces it; Space toggles a hidden preview | Keyboard path into the grid broken (UNCONFIRMED on screen) | Forward to the current stack view; Space opens the lightbox | S | Fault |
| MS-3 | M | Esc | As IA-2 | | | | |
| MS-4 | M | Files, Mail, Code, Photos | No box-to-list arrow forwarding (only `search_view.py:157`, `mini_search.py:758`) | Type-then-arrow stops at the box | One `forward_arrows(box, view)` beside `list_keys.install`; guide §4 | S | Decision |
| MS-5 | M | Search, layout | Grid is a global tick box that filters to photos only (`thumbnail_grid.py:204`); list delegate draws no images; no empty state in the grid | Mixed queries cannot show photos inline | A Photos strip atop mixed results; suggest the grid when most rows are images; EmptyState | M | Decision |
| MS-6 | M | Search, thumbnails | Own pipeline: queues every photo, full decode each, 200 in memory, 2 threads, generic placeholder (`thumbnail_grid.py:63-260`) | Slower and unlike Photos | Back it with `ThumbLoader` (disk cache, newest first, blur-up), visible cells plus one screen | M | Refactor |
| MS-7 | M | Search, zero results | Status set from the new answer (`search_view.py:339`), rows kept (`:351`) | "0 results" over the previous rows | Dim kept rows and say "No matches for ... - showing previous", or empty once the full tier ends | S | Fault |
| MS-8 | L | Workers | Stale `SearchWorker`s not taken back from the shared pool | Bursts can queue behind stale work (UNCONFIRMED, not measured) | `tryTake` superseded workers; a small search pool | S | Fault |
| MS-9 | L | Space | Docked pane in lists, info panel in Photos, next photo in the viewer | Three meanings; no transient Quick Look | Space opens/closes an overlay; docked pane stays on Ctrl+Shift+P | M | Decision |
| MS-10 | L | Preview window | Handles Left/Right only; Esc closes the find bar (`preview_window.py:817`) | Lightbox does not close on Esc; the photo viewer does | Esc closes after the find bar | S | Fault |
| MS-11 | L | Palette | As IA-1 | | | | |
| MS-12 | L | Photos layout | Uniform square crop, the owner's choice | Panoramas and portraits cropped | Optional justified "Aspect" mode - a parked idea, not a defect | L | Decision |

### 2.4 Multi-account contact sync and conflicts

Already strong: per-field merge with source drop-downs, a live result card and Split again
(`merge_dialog.py`); conservative duplicate rules (exact email or E.164 mobile *and* agreeing
names, `dupes.py:8-60`); per-field clash table; plain-words activity rows with live counts;
refused contacts kept across runs; sign-in expiry warned two days ahead.

**Edge-case matrix**

| Case | Detected | What the user sees | Recovery offered | Gap |
|---|---|---|---|---|
| HTTP 429 / 503 + Retry-After | `http.py:506-549`, up to 300 s, 5 tries | "...asked Leasha to slow down..." / "Nothing to do - Leasha waits as long as the provider asks" | Try again, Test | Retry-After beyond the transport is never given to the schedule (`engine.py:998`), so the promise is untrue |
| Sign-in expired | 401, `invalid_grant` | Row, toast "Sign in again", tray line | Reconnect | Strong |
| Network drop / 10054 | `http.py:100`; reads retried | "...could not be reached..." | Try again, backoff | One dropped write ends that account's run |
| Some contacts refused | `engine.py:1077-1098` | Sync window list; log "Done, with problems" | Open contact, Retry these | Activity row says "Up to date" with a tick |
| Delete brake | `engine.py:482, 797` | Brake text, toast "Sync held" | Allow (Sync window, log) | No confirmation, held contacts never named, "press Undo" not available on a held run; **Allow dead on Settings > Accounts** |
| Churn guard | `engine.py:517-528` | "...changed in the same way..." | Allow | Toast says delete limit for every hold (`accounts/logic.py:380`); Undo suggested but refused |
| Server 500 / 502 / 504 | Not retried; `ERR_ACCOUNT_REJECTED` | "...refused the request..." | Try again, Test | An outage reads as a refusal |
| Changed on both sides | `engine.py:605-635` | Row glyph, sidebar, editor banners | Editor per contact; dialog per field | The two paths settle differently (CS-6) |
| Mirror waiting on duplicates | `engine.py:256-262` | Nothing - log only | None | Status invisible |

| ID | Sev | Screen / component | Current implementation | UX gap | Recommended refactoring | Effort | Kind |
|---|---|---|---|---|---|---|---|
| CS-1 | H | Settings > Accounts activity row | `actions_for` offers `allow` and `fix` (`presenter/account_activity.py:266-269`); `AccountPanel._row_action` handles neither (`accounts/panel.py:515-529`) | Buttons that do nothing | Handle both as `sync_dialog.py:150-162` does, through one shared handler; test every key on every host | S | Fault |
| CS-2 | H | Allow | Runs at once; `plan_text` cannot name `held_deletes` (`accounts/logic.py:320-330`) | A delete with less ceremony than an undo | Allow dialog listing the held names, "Allow and delete N" as danger | M | Decision (new words) |
| CS-3 | H | Churn-guard toast and suggestions | One toast for every hold; suggestions point at an Undo refused for held runs (`errors.py:1018, 1036`) | Wrong reason, impossible remedy | Toast by error code; allow Undo on held runs or correct the suggestion | S | Fault |
| CS-4 | M | Activity after a partial run | No partial state (`account_activity.py:146, 193`) | "Up to date" hides refusals | Partial state with warning icon and Retry these | S | Fault |
| CS-5 | M | Rate limits | `record_failure` without `next_retry_at` (`engine.py:998`, `mail_sync.py:330`) | Backs off 1-2-4 min regardless | Pass `retry_after_of(exc)`; say "trying again at HH:MM" | S | Fault |
| CS-6 | M | Clash resolution | Editor: `engine.resolve_conflict` re-merges and pushes; dialog: `book_actions.accept_conflicts` edits the hub only | Settled clash likely asked again (UNCONFIRMED - test first) | Dialog through `service.resolve_conflict`, or refresh the link snapshot | M | Fault (to confirm) |
| CS-7 | M | Server 5xx | Only 503 temporary (`http.py:506`) | Outage reads as refusal, fills the refused list | Retry 500/502/504 on safe methods; account-wide; "having trouble" wording | S | Fault |
| CS-8 | M | Background progress | Only the Sync window (4 s poll) and visible Accounts page (5 s) | No ambient status | Sync chip on the Contacts toolbar and a tray line from `scheduler.progress` | M | Decision |
| CS-9 | L | Account attribution | Account-colour chips (`delegates.py:98-125`); same glyph for Google and Microsoft by design; reader "Kept in" plain text (`reader.py:220`); link state merged away (`hub.py:216`) | Cannot see waiting, refused or deleted-there per account | A state mark per chip with a tooltip | M | Decision |
| CS-10 | L | Mirror waiting | Log and an unread plan key | Invisible | Activity line with Find duplicates | S | Fault |
| CS-11 | L | `ERR_SYNC_CONFLICT_HELD` | Defined (`errors.py:1010`), never raised | Dead contract | Raise on held clashes, or remove | S | Fault |
| CS-12 | L | Live refusals | No contact id in progress rows (`sync_dialog.py:237`) | Open contact does nothing until reload | Carry `contact_id` in `plan.failures` | S | Fault |

## 3. Prioritised action plan

**Phase 1 - quick polish wins (faults, each S, one parallel wave, tested together).**
IA-2/MS-3 Esc, IA-7 Tagger host, MS-1 search busy line, MS-2 grid keyboard, MS-7 zero-result
honesty, MS-10 lightbox Esc, DS-3 QTableView selectors, DS-12 pressed states, DS-13 guide and two
raw button boxes, CS-1 dead Allow/Fix, CS-3 churn toast, CS-4 partial state, CS-5 Retry-After,
CS-7 5xx, CS-10 mirror line, CS-11 dead code, CS-12 live refusals; plus the DS-5 ceiling guard and
the guards for DS-1 and DS-6 (allowlisting today's offenders, so nothing new slips in).

**Phase 2 - component standardisation (M each, needs a visual pass on the laptop).**
`paint_kit` with `paint_row_state` and a keyboard focus ring (DS-2, DS-11); one focus width
(DS-4); a `Card` base (DS-7); `title` step, weight tokens and radii (DS-8, DS-9, DS-10); confirm
verbs for the nine questions (DS-1); `menus.attach` for drop-downs (DS-6); one search
thumbnail pipeline (MS-6); `forward_arrows` on every page (MS-4); off-scale spacing migrated file by
file (DS-5).

**Phase 3 - architectural and flow overhauls (product decisions, each its own order).**
The command palette over a Qt-free action registry (IA-1/MS-11) and a coherent key map (IA-3,
IA-9, IA-10); one "Accounts & sync" home with ambient progress (IA-5, CS-8) and per-account link
state (CS-9); a confirmed, named Allow (CS-2) and one clash-resolution path (CS-6); Photos and
Contacts linked both ways (IA-6); mixed results with inline photos and one Quick Look (MS-5,
MS-9); one scope vocabulary (IA-4); rail breadth (IA-11). Justified photo rows (MS-12) belongs in
`docs/PARKED-IDEAS.md` if wanted at all.

## 4. What needs the owner

1. Any item marked **Decision** changes words or behaviour; released labels are not reworded
   without the owner's word, and new words (Allow dialog, confirm verbs, scope names) need approval.
2. Phase 3 items are work orders; none was created by this review.
3. Every look-and-feel claim waits on the real window at Windows DPI.
