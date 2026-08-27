# Work order (One thread): the seven adoptions — best ideas from the five-AI review

**Doc version:** 1.5 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
**Thread:** One thread (Search/UI polish; one storage touch for saved searches)
**Status:** RELEASED by the owner 2026-08-28. **Gap-schedulable** (the
privacy-defaults pattern): items are independent — do each when its
prerequisite has landed (noted per item); 0m (test automation) remains last
and its scenario convention applies here (each item's acceptance line gets
its pytest-qt scenario in this order).

Source: the owner had five AI assistants describe world-class desktop
search; consolidation (2026-08-28, in the design notes) found the consensus
already built and these seven worth adopting. Everything else was rejected
with doctrine reasons — do not import ideas from those documents beyond
these seven.

## 1. "Why this result?" (the best idea of the five documents)

- [x] **1a** every result row gains an expandable line (context menu or a
  small affordance — thread's choice, tooltip states its effect): the
  plain-words signals that put it here — matched words, meaning-match
  yes/no, recency contribution, "you've opened this N times", definition
  boost (code), folded-versions note. Sourced from signals that already
  exist (RRF lane ranks, recency blend, usage log, fold info) — **no new
  ranking machinery, no invented percentages**: state facts, never scores.
- [x] **1b** Qt-free: a `presenter.why_result(...)` builds the lines from
  the result's recorded signals; the view only displays. Off-able like
  every behaviour; plain register on tab one, technical detail allowed on
  power tabs (notice-register pattern).
  *Prerequisite: search-experience order landed (recency/folding exist).*

## 2. Match-type indication

- [x] **2a** results and snippets subtly distinguish HOW they matched:
  keyword hits keep today's highlight; a meaning-only match shows a small
  plain-words marker ("meaning match") instead of pretending words matched.
  Delegate-level styling; colour never the only signal (accessibility
  rule); off-able.
  *Prerequisite: none — lane provenance is already in the fused result.*

## 3. Saved searches

- [x] **3a** save the current query (text + chips + scope) under a name;
  saved searches listed on the empty-focused search box beneath recent
  searches, and in the `/` menu (`/saved <name>` — values with counts via
  the existing machinery). Run = re-execute live (a smart folder, not a
  snapshot). Rename/delete; stored in the app's own state (one small
  table); per-account like everything.
- [x] **3b** no auto-saving, no suggestions to save — the user saves
  (manual-model instinct). Export/import rides the settings story later.
  *Prerequisite: none.*

## 4. Selection-to-search (mini-search upgrade)

- [ ] **4a** when the global-hotkey mini-search opens, if the foreground
  app has a text selection, it pre-fills the box — selected, so one
  keystroke replaces it (never auto-searches; pre-fill only). Read via
  UI-automation/clipboard-preserving technique — clipboard contents must
  be restored byte-perfect if used as the transport, and the whole feature
  is off-able.
  *Prerequisite: workspace-features order (mini-search) landed.*

## 5. Live category-count chips in the mini-search

- [ ] **5a** beneath the mini-search results: live counts per kind
  ("14 files · 5 emails · 3 photos"); Tab cycles the chips and re-filters
  instantly (scope filter, not a re-search where the engine allows).
  Plain-words labels; counts from the result set already in hand — zero
  extra queries.
  *Prerequisite: workspace-features order (mini-search) landed.*

## 6. Cell-level spreadsheet locators

- [x] **6a** spreadsheet extraction carries sheet name (exists as segment
  labels — verify) AND cell/row references into segment labels where the
  extractor can know them; results and snippets for spreadsheet hits show
  "Sheet 'Q3' · near D14" and the grid preview (0326 §4b) scrolls to the
  region when it lands. Extractor-level; measure extraction cost before/
  after on the fixture (must be negligible).
  *Prerequisite: none for labels; grid-scroll waits for 0326 §4b.*

## 7. `leasha://` deep links

- [x] **7a** register the `leasha://` URL scheme (per-user registry key,
  installer-written, uninstaller-removed): `leasha://search?q=...` opens
  the app (or fronts it) with the query run. Single-instance machinery
  routes to the running window. Enables shortcuts/other tools to open
  saved searches. Low priority — do last in this order.
  *Prerequisite: none.*

## Tests

- [x] why-result: fixture result's explanation lists exactly the recorded
  signals, no invented numbers; off-switch removes the affordance.
- [x] match-type: meaning-only fixture hit shows the marker; keyword hit
  does not; not colour-only (asserted).
- [x] saved searches: save/rename/delete/re-run round-trip; appears in `/`
  menu with count; per-account isolation.
- [ ] selection-to-search: clipboard restored byte-perfect (the load-
  bearing test); no selection → plain open; off-switch honoured.
- [ ] chips: counts equal the result set's; Tab cycles; zero extra engine
  calls (asserted).
- [x] cell locators: fixture xlsx hit renders sheet+cell; extraction perf
  delta recorded.
- [x] deep link: `leasha://search?q=x` fronts the single instance with
  results; uninstall removes the key.
- [ ] pytest-qt scenario per item (0m convention); all new strings pass the
  plain-words/tooltip rules.

## Done means

Each item: change + tests + suite green + committed by name; CHANGELOG per
user-visible feature. Acceptance sentence: a user can ask any result "why
are you here?" and get the honest answer, keep their favourite questions
one click away, and summon Leasha around the text they just highlighted —
with nothing imported from the five documents beyond what this order names.

## Note on §1, added 2026-08-28

**Two signals were already being written and thrown away.** `recency.blend`
records why it moved a hit, `definitions.boost` records that a passage
declares the symbol — both annotate the fused hit, and `_to_result` dropped
them on the floor. The same shape as `ext` and `mtime_ns` before the rows
learned to show them. They are carried now, which is most of "no new ranking
machinery".

**"Facts, never scores" is enforced, not just intended.** A test walks every
line of a maximally-decorated explanation, in both registers, and fails on
`%`, `score`, `0.` or `rank`. Percentages are what all five review documents
reached for and what this order forbids: *"87% relevant"* is a sentence nobody
can check and everybody would believe.

**Recency says the date, not the freshness**, and only above one half-life —
below that the blend contributed almost nothing and saying so would be noise
dressed as an explanation.

**Open counts are one query for the whole page.** `search_hits` grows with
every search ever run, but `idx_hits_opened` is partial (`opened = 1`) and
roughly one hit in fifty is opened, so the batched count visits almost
nothing. A per-row count would have been fifty statements against that table —
the keyword path this project already had to fix once.

**It is the seventh behaviour, and it earned the seventh switch.** The
guard-on-the-guard from the search-experience order failed immediately, which
is what it is for: a behaviour with no proof that it does anything. It is
excluded from `policy.describe()` on purpose — that function answers *why did
my search behave like that*, and this one never changes the search; listing it
would make the Code tab, which acts on nobody's behalf, appear to be acting.

**One correction to my own test**: the first version branched on the policy in
its own helper and asserted the branch — a tautology that would have passed
with the switch wired to nothing. It goes through `presenter.explain_for`
now, where the policy actually decides.

Not done: the expandable affordance in the result row itself, which is view
work like the rest of this session's UI items.

## Note on §2, added 2026-08-28 — and a live bug it uncovered

**Only the row that reads as a mistake gets a marker.** A result with none of
the typed words in it looks like a bug to anybody who does not know the search
understands meaning; a keyword hit keeps today's highlight and says nothing
extra, because a badge on every row makes the one that matters invisible.

The marker is a **word**, per the accessibility rule this codebase already
applies to the focus ring: colour is never the only signal. It rides
`explain_results` rather than taking an eighth switch — it is the shortest
possible answer to *why is this here*, and a separate preference for one word
is one nobody could tell apart from the other.

**Read as data, not parsed out of `explain`.** `ResultRow` now carries
`sources`; the rule this codebase set for notices — the UI never reads a
message string to decide anything — applies to a row choosing a marker.

**The bug: every document result has always shown a blank date.** `to_row`
never copied `ext` or `mtime_ns` off the `SearchResult`, though the field
comment on `ResultRow` said both went "straight through". `ResultGroup.when`
is built from `rows[0].mtime_ns`, so it was `format_when(0)` — the empty
string — for the life of the feature. **Mail hid it**: a message takes its
date from `sent_at` in the details map, so the Mail tab looked right while the
other three quietly did not. `kind` survived only by falling back to parsing
the filename, which would itself have failed on a file with no extension.

**This corrects a tick I made in the search-experience order.** §2e's "result
rows show filename, folder, when" was marked already-true on the strength of
`when` existing as a field. It existed and was always empty — the second time
this session I checked a field rather than a value. The §2e note now says so.

## Note on §3, added 2026-08-28

**A query, not a result set — and the test says so literally.** *Run =
re-execute live (a smart folder, not a snapshot)* is the sentence the whole
design turns on, so `test_running_a_saved_search_finds_documents_indexed_
after_it_was_saved` indexes a second document *after* the save and asserts it
comes back. A stored list of ids would have been less code and would have
passed every other test in the file.

**`saved:name` is an expansion, not a filter, and that decided where it
lives.** `COMMANDS` in `commands.py` has a test asserting it matches
`_FIELD_ALIASES` in the parser exactly — offered and parsed are one set — and
`saved:` is neither. It is replaced by the query it stands for *before*
`parse_query` is called, exactly as `/type pdf` becomes `type:pdf` first. So
it went into a new `ACTIONS` tuple beside `COMMANDS`, and the search box's
catalogue is `SEARCH_CATALOGUE = ALL_CATALOGUE + ACTIONS`. **The Code box does
not get it**: it has no scope and does not run the main engine, so the row
would open, complete, and quietly search for two words — the "menu row that
does nothing" failure `command_popup.py` opens by warning against.

**Expanded in place.** `saved:weekly leeds` is the saved query *plus* leeds.
Replacing the whole box would throw away something the person just typed, and
no sentence on screen makes that feel right. A name nobody saved is **left
exactly as typed**, the rule `expand_slashes` follows for an unknown `/word`.

**The count in the menu is runs, not files.** "Values with counts via the
existing machinery" is `distinct_value_counts`, and the honest number there is
how often the search has been run: counting matching files would mean running
every saved search behind a keystroke, which is the unbounded work that method
exists to keep out. `VALUE_NOUNS["saved"] = "runs"`, so the row reads
`invoices   2 runs` — and it is also the more useful number, because it puts
the search somebody runs every Monday at the top.

**Schema v15**, one small table. `name_lc` is folded in **Python**, for the
reason v14 needed a whole column: `SELECT lower('JOSÉ')` returns `josÉ` in
SQLite, so no SQL-side fold can carry a `UNIQUE` constraint over a name. The
test measures that rather than asserting it from memory.

**Where it went, and why the view barely changed.** `search_view.py` was at
247 of the 250 code lines the presenter guard allows, so everything that is
not a widget lives in `app/ui/saved_box.py`: which list is current, when it is
re-read, what a token expands to, what the empty box offers. The view gained
two lines and one replaced line, and is now at **249** — the next item
touching it has to extract something first.

**§3b is honoured by omission and asserted anyway.** Nothing writes to the
table except `save_search`, and `test_nothing_is_ever_saved_without_being_
asked` logs a search and checks the saved list is still empty — because
`searches` has held every query since Layer 4, and a saved list filled from it
would be a hundred half-typed queries and worth nothing. `suggest_name` fills
a box in a dialog the person opened; that is the whole of the automation.

Not done here: the Qt dialog for save/rename/delete, and drawing the two
sections under the empty box. Both wait on §2e's recent-searches attachment in
the search-experience order, which is the widget they share — the rules and
the store side are complete and tested. Export/import rides the settings
story, as §3b says.

## Note on §6a, added 2026-08-28

**Verified first, as the item asks.** The sheet name *does* already reach the
segment label and the indexed text - `office.py` has written `Sheet: Q3` since
Layer 2, which is what makes searching for a sheet name work. What did not
exist was any row or cell reference, and `chunks.page` holds the sheet
*index*, so a hit in a forty-thousand-row workbook rendered as **"page 3"** -
true, useless, and naming a thing no spreadsheet calls a page.

**A live defect found on the way in, and it decided the design.** The old row
flattener dropped empty cells while joining, so the first *written* cell of a
row could be column F and any locator built by counting tab-separated fields
would have said C. A cell reference that is confidently wrong is worse than
none, so the column number now travels beside each value. **The indexed text
is byte-for-byte what it always was** - asserted, because if it moved, every
chunk boundary, every stored offset and the comparability of the measured
retrieval baseline would move with it, for a label.

**Anchors, not segments.** The obvious move - one segment per row - is wrong
twice: `DocumentBuilder.SEPARATOR` is a paragraph break, so the chunker would
split on every row, and a 5,000-row sheet would become 5,000 chunks. So a
segment stays the unit of *structure* and an anchor is a landmark inside one:
`Document.anchors`, `Document.anchor_lookup()`, resolved by the same binary
search and the same *last one starting at or before* rule as `page_lookup`,
so the two cannot disagree.

**A second bug the first version had.** The label line `Sheet: Q3` sits above
every anchor, so the first chunk of every sheet - which starts at offset zero -
resolved to nothing and showed no locator at all. The first anchor now claims
the top of its segment: a heading belongs to the rows under it.

**Schema v16** adds one nullable `chunks.label`, not three columns. The value
is only ever read whole, `!` is the spreadsheet's own separator, and what is
stored - `Q3!D14` - is also what somebody could paste into the Name Box.
`extract/cells.py` builds it and parses it back; `presenter.cell_location`
turns it into *Sheet 'Q3' · near D14*. The store keeps a code and the words
live in the presenter, exactly as the notices do.

*near*, not *at*: the locator is the row the passage **starts** on, and a
passage is several rows long. "at" would be a precision the value does not
have, on the one screen where somebody is deciding whether to open a huge
workbook.

**Extraction cost, measured as the item requires.** 5,000 rows x 3 written
columns, best-of-15 in a warm process:

| | median | floor (min) |
|---|---|---|
| before | 206 ms | 175 ms |
| after | 200 ms | 185 ms |

Medians are indistinguishable - one *after* run came in faster than *before* -
and the floor moves ~10ms, about **1.8 microseconds per row**, against a stage
dominated by openpyxl's XML parsing. Negligible, which is what the item
required. (An earlier reading of +54% was machine noise: seven runs, and its
own minimum was above every later measurement. Fifteen runs and the minimum
is the estimator that survives a loaded sandbox.)

Two things paid for it rather than costing: the old comprehension evaluated
`str(value).strip()` **twice per cell**, once to test and once to keep, and
the plain loop that replaced it does it once; and the column letter is
memoised, since a sheet has a few dozen written columns and tens of thousands
of rows.

`.xls` got the same treatment - a 1998 workbook and a 2024 one should tell you
the same things, the rule `xls.py` already states about its row caps - and is
tested through `xlwt`.

**Not done: `.ods`.** `odf.py` is a flat event walk shared by `.odt`, `.ods`
and `.odp` that tracks neither the sheet name nor the row index, and ODS rows
carry `number-rows-repeated`, so a row counter there is a correctness problem
rather than a counter. It needs its own pass and would have been guesswork
bolted onto this one.

**The grid-scroll half stays gated**, as the item says, on 0326 §4b - which is
why `ResultRow` keeps the raw `Q3!D14` beside the sentence: getting it back by
re-parsing the words is precisely the mistake `cell_location` exists to
prevent.

## Note on §7a, added 2026-08-28 — and one promise the installer could not keep

**The scheme was the easy half. The second copy was the item.**
`SingleInstance` deliberately *refuses* a second window rather than talking to
the first — two copies cannot share one index, and that is the right answer
for a double-clicked shortcut. It is the wrong answer for a link: somebody
clicking one is not asking for a second application, they are asking the one
they have to look something up, and "another copy is already running" is a
useless thing to say to a link.

So the link process never becomes a window. `leasha open leasha://search?q=…`
writes one `index_state` row and exits. **That is not a new mechanism** —
`run_lock.request_stop` already passes an instruction between two processes
through the same table, for the same stated reason: *"a flag rather than a
signal because the two processes share nothing else."*

**One action, and that is the security decision.** A URL scheme is an input
from *outside* the application: anything on this machine can invoke it, and a
link in a document is not a trusted instruction. `search` is safe — the worst
a hostile link achieves is a search the person can see and did not want.
Anything that indexed a folder, opened a file or changed a setting would be a
stranger giving orders, so `ACTIONS` holds exactly one entry and a test
asserts it. The query is capped at 500 characters so a link cannot paste a
megabyte into the box.

**It rides the existing watcher rather than bringing a timer.** The window
already polls every four seconds for an index run started elsewhere; the
pending link is read in the same worker, on the same tick. Polling the
database every second for the life of every session, so that a link somebody
clicks once a week arrives instantly, is not a trade this codebase makes
anywhere else. **The cost is stated rather than hidden: a link takes up to
four seconds to land when a window is already open.** If that turns out to
grate in use, the fix is a shorter interval on that one read, not a second
timer.

**Per-user registry.** `HKCU\Software\Classes\leasha` — no administrator to
install, nothing left behind for the next person to use the machine, the same
reasoning that put the index in `%LOCALAPPDATA%`. `leasha open register`
writes it, `unregister` removes it, `show` prints what would be written, and
all three are honest about doing nothing off Windows. The installer asks
before registering, the way it asks about tab completion, and for the same
reason: changing how the whole machine treats a kind of link is not something
to do to somebody quietly.

**The one promise this could not keep, and it was in the order.** The
acceptance line says *"uninstall removes the key"* — **there is no uninstaller
in this repository.** Packaging is Layer 9 and has not been built. Rather than
write an installer line claiming a script nobody has written removes the key,
the installer says `.\leasha open unregister` does, which is true today, and
`unregister()` sits waiting for the L9 hook to call it. The test box is ticked
for the half that exists — registration, removal, and the values themselves,
all checkable on a machine with no registry — and this paragraph is the record
of the half that does not.

**Not verified on Windows.** `register`/`unregister` are the only code here
that cannot run in this environment; everything they would write is returned
as data by `registry_values` and asserted. Worth one run of
`leasha open register` and a click on a `leasha://` link on the owner's
machine before this is trusted.
