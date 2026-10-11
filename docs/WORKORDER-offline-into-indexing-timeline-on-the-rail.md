# Work order (One thread): Offline moves into Indexing, and the timeline takes its place on the rail

**Doc version:** 1.1 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/ui/shell.py`, `app/ui/indexing_view.py`,
`app/ui/widgets/indexing_layout.py`, `app/ui/offline_media_view.py`, `app/ui/reports_view.py`,
`app/ui/timeline_view.py`, `app/ui/controllers/timeline_controller.py`, `tests/unit/`, the user
guide, `docs/GLOSSARY.md`)
**Status:** BUILT 2026-10-11, 11 of 13 (2d and 3d need the real window); RELEASED by the owner 2026-10-11 ("yes write the order for both and give it to the
running thread to execute"), after asking whether "the pill for offline belong[s] in the indexing
tab and the timeline ... in [its own] tab".

**Where this came from.** Two placements were set by earlier orders and never revisited:

1. **Offline** is its own rail entry, above Reports (`shell.py`, the `(self.offline_media_view,
   "Offline", ...)` tuple). Order `202626270513` §2 made it a tab; the owner's 2026-09-20 ruling
   was about its label ("Offline", not "Offline Media"), not its place.
2. **Browse your timeline** is the third row in the Reports list (`reports_view.REPORTS`), beside
   Digital Inheritance and the Space Report. Order `202626270602` §4 put it there; that order's
   own words call it "a place to wander rather than a document".

The reasoning, agreed in conversation on 2026-10-11:

- **Offline is upkeep, not finding.** Its page is a list of catalogued drives with Scan, Rescan
  and Delete - the same kind of job as the Indexing page (what Leasha knows about, and how it is
  kept current). The eight-year-old benchmark never needs it; it should not hold a rail slot.
  Results from an unplugged drive still appear in Search exactly as now.
- **The timeline is a destination, not a report.** Reports are read and exported; the timeline
  is browsed. As row three of a list it is undersold. It gets the rail slot Offline gives up.

**What must not change.** Offline stays **fully manual** (order 0513's own header): nothing on
the Indexing page starts, schedules or watches a drive scan, and the Indexing pill does not report
offline scans as index runs unless it already does today. No store, worker or layer change - this
is a move of existing widgets. Existing UI labels are not reworded (CLAUDE.md).

---

## Decisions (defaults the building thread may take; say which were taken at close-out)

- **D1 Where Offline sits inside Indexing.** Default: a fifth shelf in the Indexing sidebar
  (`assemble_pages`, after `CATEGORY_TUNING`), titled **"Offline"** - the existing label,
  verbatim. The shelf holds the whole `OfflineMediaView` unchanged.
*Corrected 2026-10-11 (owner, same day as release: "the pill text should only be Browse"): the
timeline's rail entry is labelled **"Browse"**, not "Timeline". This overrides D2's default and
the word "Timeline" wherever 2a and 3a use it as the rail label.*

- **D2 The timeline's rail label.** It needs a short rail word; "Browse your timeline" is the
  Reports row's title and too long for the rail. Default: **"Timeline"** - a new label, not a
  reword of an existing one. Its tooltip / accessible name may use "Browse your timeline".
- **D3 The Reports row.** Default: **removed** from `REPORTS`, so the timeline has one home.
  The other doors (a result's "See everything from this month", the timeline strip's right-click,
  Mail's `period_requested`) now open the rail entry instead of the Reports pane.
- **D4 Position on the rail.** Default: where Offline was - immediately above Reports.

---

## 1. Offline into the Indexing page

- [x] **1a** `shell.py` no longer adds `offline_media_view` as a rail entry; the Go menu's
  "Offline" item (`add(go, "Offline", ...)`) opens the Indexing page on the Offline shelf.
- [x] **1b** The Indexing page gains the Offline shelf (D1). `indexing_view.py` is at the
  250-line view guard - the shelf is assembled in `widgets/indexing_layout.py`, not added inline.
- [x] **1c** The refresh rule holds: the Offline list is read when its **shelf** is shown, not
  when the Indexing page is shown and not on a timer (the `_tab_index` check at `shell.py`
  ~line 2031 moves to a shelf-shown signal).
- [x] **1d** Scan / Rescan / Delete are wired exactly as now (`scan_requested`,
  `rescan_requested`, `delete_requested` -> `index_ctl`). A scan still takes the run lock and
  still refuses while an index run holds it, with the same words.
> *2026-10-11, built:* the Go menu's Offline (`MainWindow._show_offline`) and every refresh land on the shelf. No notice or opener message named the tab; the `/on` command's hint still reads "as shown in the Offline Media tab" (`app/search/commands.py`) - existing UI wording, left for the owner.
- [x] **1e** Anything that sent the person to the Offline tab (a "drive not plugged in" notice,
  the opener's message, the `/` menu, `test_command_subsets`' page names) now lands on the
  Indexing page's Offline shelf.

## 2. The timeline on the rail

> *2026-10-11, built:* `MainWindow.timeline_view`, rail title "Browse" (owner's correction), icon `calendar`, tooltip and accessible name "Browse your timeline" (`Rail.describe`). The same `TimelineView`, built where Reports built it before.
- [x] **2a** `TimelineView` becomes a rail page of its own (D2, D4), built from the same widget
  Reports hosts today - no second timeline.
- [x] **2b** The Reports row is removed (D3); `ReportsView` no longer hosts the timeline pane.
- [x] **2c** `TimelineController`'s doors (`browse_period`, the result menu, the strip) open the
  rail page at the asked period. Its module docstring is rewritten to say so.
> *2026-10-11, not done:* the view is built at the same point as before (it was made inside `ReportsView.__init__`, now in `MainWindow.__init__` beside it), so start-up does the same work; the window-visible time before and after was not measured - it needs the real window.
- [ ] **2d** The page is built lazily if it is costly at start-up, as Mail and Code are - measure
  the window-visible time before and after (order 0r's budget) and record both.

## 3. Tests and documents

- [x] **3a** The rail-label tests (`test_ui_redesign*.py`, `test_window_opens.py`,
  `test_ui_review_0x9.py`, `test_e2e_pywinauto.py`) say the new rail: no "Offline" entry, a
  "Timeline" entry above Reports.
- [x] **3b** New tests: the Offline shelf exists on the Indexing page and refreshes only when
  shown; each timeline door lands on the rail page at the right period
  (`test_timeline_entry_points.py`); Reports lists two reports.
- [x] **3c** Documentation rewritten in place to describe the app as it now is (owner rule
  2026-10-06): the user guide, troubleshooting, `docs/GLOSSARY.md` ("the Offline page" -> the
  Offline shelf of the Indexing page), and any screenshot list.
> *2026-10-11:* `run_suite.py --affected` run on the laptop: the 1i failures were all tests pinning the old rail and were updated; the real window at 125% not yet looked at (the open app runs the code from before this commit).
- [ ] **3d** `--affected` then the full suite (`scripts/run_suite.py`) green on Windows; the
  real window looked at once at 125% (HANDOFF trap "Look at the real window").

## Close-out

Tick each box only when built and tested. Update this order's Status line, the register row
(`docs/ORDER_REGISTER.md`, 1i) and `HANDOFF.md`; name which of D1-D4 were taken as written.
