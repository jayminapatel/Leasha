# Work order (One thread): every table sorts, every header sits over its column

**Doc version:** 1.1 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
**Thread:** One thread (UI widgets — `result_table.py` centred, views follow)
**Status:** RELEASED by the owner 2026-08-28. Small and **gap-schedulable**
(privacy-defaults pattern). Owner's report, verbatim intent: *Mail sorts on
header click; this should be global on all lists no matter where — and
headers are centre-aligned while their columns are not; headers must align
the same way as their column.*

## 1. Sorting everywhere

- [x] **1a** `ResultTable` (`widgets/result_table.py`) defaults
  `sortable=True`; every table view adopts it — Files, Mail (already),
  Code results, git tree, file-types table, roots, and the settings grids
  where ordering is meaningful. Sorting uses the existing
  `SortableItem`/`SORT_ROLE` machinery on EVERY column — dates sort as
  dates, sizes as bytes, counts as numbers, never as display text (the U4
  lesson, now applied globally, with the payload set at population time in
  each view).
- [x] **1b THE RANKED-VIEW RECONCILIATION** — `files_view.py:111` refused
  sorting deliberately: results are ranked by match quality and a header
  click discarded the ranking irrecoverably. That reasoning stays honoured,
  superseded by making rank restorable: ranked tables store the original
  result position in a sort role, and **"Relevance" is a first-class sort
  order the user can return to** (the thread picks the affordance — a
  leading sortable column, a header context-menu entry, or re-click
  cycling asc/desc/relevance — whichever reads cleanest; tooltip states
  the effect). The old comment gets a dated note pointing here, appended
  never rewritten.
- [x] **1c** the id-keyed selection rule extends with sorting: any table
  that becomes sortable must select/act **by id, not by row position**
  (mail_view:290's pattern — after a header click, visual row 3 is not
  payload row 3; this is the bug class sorting introduces, closed at the
  door). Audit each newly sortable view for positional row access.
- [x] **1d** new-result arrival vs an active sort: repopulation re-applies
  the user's current sort (or Relevance default); a mid-typing interim
  refresh must not visibly reshuffle under the pointer — the mail view's
  disable-during-populate pattern (`setSortingEnabled(False)` … `(True)`)
  becomes part of `ResultTable`'s own populate path so no view can forget
  it.

## 2. Header alignment follows the column

- [x] **2a** `ResultTable` gains a per-column alignment spec (left for
  text, right for sizes/counts/dates as each view already aligns its
  cells) and applies it to BOTH the cells and the header sections
  (`setDefaultAlignment` per section) — the header sits over its column
  the way the column reads. One implementation in the shared widget;
  views declare alignment once instead of per-cell flags scattered
  through population loops (existing per-cell flags migrate to the spec;
  visual result identical for cells, corrected for headers).
- [x] **2b** the non-ResultTable tables (any raw QTableWidget/QTreeWidget
  in widgets/) get the same treatment or migrate to ResultTable where
  trivial — grep-audit listed in the delivery note, none left
  centre-headed over an aligned column.

## 3. Tests

- [x] sorting: pytest-qt — header click on a date column orders by real
  date across every sortable view (fixture with dates whose display and
  chronological orders differ — the classic trap); sizes numeric; a second
  interaction path returns a ranked view to Relevance and the order equals
  the engine's original ranking.
- [x] id-keying: sort, then act on visual row N — the action hits the row's
  id, not position N's original payload (per newly sortable view).
- [x] repopulate-under-sort: refresh during an active sort preserves the
  sort; interim refresh does not reorder mid-click (the 1d guard as a
  test).
- [x] alignment: for every table, each header section's alignment equals
  its column's cell alignment (a walker test over instantiated views — the
  suffix-consistency test shape, applied to alignment, so future columns
  can't regress it).
- [x] labels unchanged (standing rule); new affordances' tooltips state
  their effect.

## Done means

Change + tests + suite green + committed by name; CHANGELOG one line.
Acceptance sentence: click any column header anywhere in Leasha and it
sorts correctly for its data type, every header sits flush over its
column, and a ranked list can always find its way back to Relevance.

## 4. ADDED by owner 2026-08-28 — remember the window state

- [ ] **4a** the main window remembers its last state across launches:
  maximised reopens maximised; normal reopens normal at its last size and
  position (`saveGeometry`/`restoreGeometry` — Qt's own blob carries
  state + geometry + screen). Stored in the app's own local state,
  per-account like everything; nothing new leaves the machine.
- [ ] **4b** the honest edge cases, handled not ignored: a remembered
  position on a monitor that is no longer attached clamps back onto a
  visible screen (never opens off-screen); a window closed while
  *minimised* reopens normal, never minimised (an app that starts
  invisible looks broken); first run with no saved state keeps today's
  default.
- [ ] **4c** the save/restore lives in one small helper so the pop-out
  windows (workspace-features order 0326) can adopt the same behaviour
  per-window when they land — noted there as an appended dated line, not
  built here.

### 4. Tests

- [ ] pytest-qt: close maximised → reopen maximised; close normal at a
  size/position → reopen identical; saved geometry pointing off-screen →
  reopened window is fully on a visible screen; close minimised → reopen
  normal; no saved state → default behaviour unchanged.

### Done means (§4)

Same bar as above; acceptance sentence: close Leasha however you left it,
and it opens the next time exactly the way you left it — on a screen you
can see.

## Note added 2026-08-28 — the Indexing page layout

The owner reports the Indexing page layout is currently broken. **Not in
scope here** — by the owner's decision it is folded into the future
pages reorganisation (Settings sidebar categories + Indexing page split,
triggered "once our workorders are in"). Recorded so the report is not
lost; do not attempt layout fixes in this order.

## Delivery note, 2026-08-28

**Sorting is on by default now, and the old refusal is honoured rather than
dropped.** `files_view:111` said it plainly: results are ranked by match
quality and a header click discarded that irrecoverably. That was right. What
was missing was a way back — so `ResultTable(ranked=True)` keeps the engine's
order in one hidden column, and **a third click on the same header returns to
it**. The affordance is click-cycling (ascending, reverse, best match again)
rather than a menu entry or a leading column: it is zero new furniture, it is
reversible in place, and it is where a person's hand already is. The header
tooltip states it, because a cycle nobody knows about is a cycle nobody uses.
The old comment keeps its text with a dated note appended beneath it.

**Two hooks, so no view can forget.** `setRowCount` turns sorting off (Qt
re-sorts after *every* `setItem` otherwise — `O(n log n)` per cell, a visible
freeze at five hundred rows) and `set_row_objects` turns it back on and
**re-applies the sort somebody chose**. `mail_view` was the only place in the
application that had worked the first half out, by hand; three tables were
about to become sortable without inheriting it. `setItem` is the third hook,
for alignment.

**Typed sort roles, and the fixture is the point.** Files gained `size_bytes`
and `mtime_ns` on `FileRow` — both were formatted away at display time, so
that list could not have sorted correctly even if it had been allowed to. The
test fixture is built so display order and real order **disagree**: "10 KB"
sorts before "3 KB" and "3 weeks ago" before "yesterday", and a fixture that
agreed with itself would pass with the sort roles deleted.

**A defect found on the way in.** `code_results` destructured its
right-aligned flag into a throwaway (`_r`), so `Size` was declared
right-aligned in `COLUMNS` and rendered left for the life of that table. That
is what §2a's "declare once" fixes at the root: the alignment is applied by
`ResultTable.setItem`, so a population loop cannot drop it. A cell that set
its own alignment is left alone, so a view migrates one column at a time.

**Verified by a real click, not only by the helper.** `_click` in the tests
imitates what Qt does on a header press, and an imitation that drifts would
let every test in that section pass over a feature nobody can use — this
project has shipped a working mechanism beside a broken outcome twice. So one
test puts the pointer on the header and presses it three times through
`QTest`, and asserts the list comes back to the engine's order.

### Three tables deliberately not made sortable, with reasons

**File types** (`widgets/file_types.py`). Column 0 is a `QCheckBox` inside a
`setCellWidget` holder. **Qt moves item data when it sorts and does not move
cell widgets**, so one header click would leave every checkbox against the
wrong row — a settings screen silently lying about what is switched on, which
is worse than a table that does not sort. Converting to a checkable item would
lose the per-row accessible names `_fill` argues for at length (a screen
reader otherwise announces sixty identical "check box, not checked"), and
rewriting that as a side effect of a sorting order is not this order's work.
Header alignment is fixed.

**Folders to index** (`widgets/roots_box.py`). `current_roots()` serialises
what is on screen, so a header click would silently **rewrite the saved root
list into the sorted order** — and column 1 is a `QComboBox` per row through
`setItemWidget`, the same widget-does-not-move problem. The order there is the
person's, not the data's. Header alignment is fixed.

**The search-behaviour grid** (`widgets/search_behaviour_box.py`). A fixed
matrix of behaviour by tab, read-only and non-selectable; there is no ordering
question to answer. Header alignment is fixed.

**The git tree** (`widgets/git_tree.py`) has `setHeaderHidden(True)` — there
is no header to click, and its order (repositories, then working tree,
branches, recent commits) is deliberate grouping a sort would scramble.
`app/ui/results_view.py` is a `QListView` with a painting delegate, not a
table: no headers, so header-click sorting does not apply.

### The grep audit §2b asks for

Every item view in `app/ui/`, and where each stands:

| widget | kind | sorts | headings aligned |
|---|---|---|---|
| `files_view` results | ResultTable | **now, ranked** | yes |
| `mail_view` results | ResultTable | already | yes (spec) |
| `widgets/code_results` | ResultTable | **now, ranked** | yes (and Size finally right) |
| `widgets/file_types` | QTableWidget | no — cell widgets | **now** |
| `widgets/roots_box` | QTreeWidget | no — order is the user's | **now** |
| `widgets/search_behaviour_box` | QTableWidget | no — fixed matrix | **now** |
| `widgets/git_tree` | QTreeWidget | no header at all | n/a |
| `widgets/code_types_box` | QListWidget | n/a — one column, no header | n/a |
| `results_view` | QListView + delegate | n/a — painted rows | n/a |

None left centre-headed over an aligned column.
