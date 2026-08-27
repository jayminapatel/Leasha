# Work order (One thread): the seven adoptions — best ideas from the five-AI review

**Doc version:** 1.2 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
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

- [ ] **3a** save the current query (text + chips + scope) under a name;
  saved searches listed on the empty-focused search box beneath recent
  searches, and in the `/` menu (`/saved <name>` — values with counts via
  the existing machinery). Run = re-execute live (a smart folder, not a
  snapshot). Rename/delete; stored in the app's own state (one small
  table); per-account like everything.
- [ ] **3b** no auto-saving, no suggestions to save — the user saves
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

- [ ] **6a** spreadsheet extraction carries sheet name (exists as segment
  labels — verify) AND cell/row references into segment labels where the
  extractor can know them; results and snippets for spreadsheet hits show
  "Sheet 'Q3' · near D14" and the grid preview (0326 §4b) scrolls to the
  region when it lands. Extractor-level; measure extraction cost before/
  after on the fixture (must be negligible).
  *Prerequisite: none for labels; grid-scroll waits for 0326 §4b.*

## 7. `leasha://` deep links

- [ ] **7a** register the `leasha://` URL scheme (per-user registry key,
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
- [ ] saved searches: save/rename/delete/re-run round-trip; appears in `/`
  menu with count; per-account isolation.
- [ ] selection-to-search: clipboard restored byte-perfect (the load-
  bearing test); no selection → plain open; off-switch honoured.
- [ ] chips: counts equal the result set's; Tab cycles; zero extra engine
  calls (asserted).
- [ ] cell locators: fixture xlsx hit renders sheet+cell; extraction perf
  delta recorded.
- [ ] deep link: `leasha://search?q=x` fronts the single instance with
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
