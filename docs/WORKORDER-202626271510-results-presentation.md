# Work order (One thread): the results, world class — every row earns its trust

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (ResultsView/ResultDelegate/presenter — the painted
list and the Qt-free text decisions behind it)
**Status:** RELEASED by the owner 2026-08-28. **Gap-schedulable**
(privacy-defaults pattern) with per-item prerequisites noted. Scope
boundary: `results_view.py` / `result_delegate.py` / `presenter.py` snippet
and row-text functions. **Not here:** thumbnails and generous first-contact
rows (0157 §2e owns them — coordinate, never duplicate); table views
(0p owns `result_table.py`); why-result and match-type markers (0o,
landed — this order must not disturb them).

Standing rules apply: existing labels/strings never change; every new
affordance's tooltip states its effect; perceivable new behaviours are
off-able; colour is never the only signal; plain register on tab one,
technical allowed on power tabs.

> **2026-09-04 verification note (session 3).** Before starting the
> remaining items, every checkbox in this file was checked against the
> live code rather than trusted. Six were already correct and are now
> ticked: **1b/1c** (`_snap_back`/`_snap_forward` plus
> `_find_sentence_start`/`_find_sentence_end`, `presenter.py`, landed in
> `da9f940`; tests in `test_presenter.py`); **2a** (the chevron is drawn in
> `group_subtitle`, `presenter.py:1026`, and read by `result_delegate.py`'s
> `_paint_group`); **5b** (`ScrollPerPixel`, `results_view.py:88`); **7a
> partial** (`accessible_text`, `presenter.py:1097`, set on both
> `AccessibleTextRole` and `DisplayRole` in `results_view.py:225-227`).
> **5c was NOT ticked**, despite `results_terminator()` existing in
> `presenter.py:1254` with its own passing tests: the function is never
> called from `results_view.py` or anywhere else, so the list never
> actually shows it — a real gap between "the helper exists" and "the
> acceptance sentence is true". It is wired in below, in this session's
> work. **4a was NOT ticked** for the same reason: `elide_path_left()`
> (`presenter.py:405`) is defined and tested but not called from anywhere
> that draws a location line — the visible location line already comes
> from the pre-existing `breadcrumb()` (tail-first, `… > 2019 > Leeds`),
> which independently satisfies the *spirit* of 4a, but the new
> left-eliding helper it was meant to feed sits unused. Left for the owner
> to decide whether `elide_path_left` is dead code to remove or is meant
> to replace `breadcrumb`'s formatting somewhere — that is a design choice
> the item's wording does not settle, and out of caution nothing here was
> changed to force one over the other.

## 1. Snippet quality (the substance of the order)

The snippet is why the user believes the result. Today it is one
right-elided line, so a late match can render with zero highlighted words
— the row looks like a false positive precisely when the engine did its
job.

- [x] **1a** the snippet wraps to **two lines in comfortable density**
  (compact keeps one); `sizeHint` and `paint` stay one source of geometry
  (the file's own rule — the gap-under-every-row bug is the regression to
  fear, and its test shape exists).
- [x] **1b** the snippet **window centres on the match**: the visible text
  always contains at least one highlighted term (the first, or the densest
  cluster when matches bunch); a window starting mid-passage shows a
  leading "…". Window selection is presenter logic (`build_snippet`),
  Qt-free, tested without a display.
- [x] **1c** windows **snap to word and sentence boundaries** — never cut
  mid-word; prefer starting at a sentence when one begins within a few
  words of the ideal window. "…he agreed the deposit would be returned by
  March." reads like an answer; "posit would be returned by Mar…" reads
  like a database.

## 2. The expansion affordance

- [x] **2a** a group that holds more than one matching chunk paints a
  chevron (▸ collapsed / ▾ expanded) and its subtitle says so in plain
  words ("matched in 5 places"). The chevron is a real click target;
  the whole row still toggles as it does today. Without this, people who
  don't know to click see one match where there are five — silent
  information loss on the page that exists to show findings.

## 3. Kind-aware rows — the list understands what it found

- [x] **3a** the `[PDF]`-style text tag gives way to a **real file icon**
  (`QFileIconProvider`, pixmaps cached per extension, painted in the slot
  the tag used; correct in both themes). Recognised faster than read —
  the 8-year-old knows the red icon before she can read "PDF". The
  kind *word* stays available in the tooltip and accessible text.
- [x] **3b** mail rows lead with the **sender** — "Mum — Re: holiday
  photos" — because that is how people remember mail; subject follows,
  date stays right. Display order only: grouping, payloads and actions
  unchanged.
- [ ] **3c** code rows paint their snippet in **monospace with the line
  number** — the form coders already read everywhere else. Spreadsheet
  hits already carry Sheet/cell (0o §6a, landed): inherit, don't touch.

  > **2026-09-04 (session 3).** Left unticked - only half of this is done.
  > `presenter.is_code_kind` and `result_delegate._snippet_font` paint a
  > code-extension row's snippet in the system monospace font, at the same
  > size, and that half is tested and working. **The line number is a real
  > infrastructure gap, not an oversight**: verified by reading
  > `app/extract/plaintext.py`, `app/extract/chunker.py` and
  > `app/extract/base.py`'s `Segment` - none of them track a line number,
  > chunk-relative or absolute, so nothing between extraction and a
  > `SearchResult` carries one for this order's Qt-free layer to show.
  > `presenter.Snippet` has no offset into its source chunk either. Adding
  > one would mean changing the extractors, which is outside this order's
  > file scope (`app/ui/presenter.py`, `result_delegate.py`, `results_view.py`,
  > `search_view.py` only) - and a *guessed* line number (say, counting
  > newlines within the snippet window alone, which is all the data in
  > reach) would not correspond to the real file and would be actively
  > misleading rather than merely absent, on the one order whose acceptance
  > sentence is "every row earns its trust". Left for the owner: either scope
  > a follow-up order through `app/extract/` to carry a line number the whole
  > way, or drop the line-number half of this item's wording.

## 4. Locations, dates, and twins

- [ ] **4a** the location line **elides on the left**, keeping the tail —
  `…\Projects\Foo\Final` — because the leaf folder is the distinguishing
  part of a deep path. Same sin the name-elision fix corrected, one line
  down.
- [x] **4b** dates on plain-register surfaces read **friendly** —
  "yesterday", "last week", "Mar 2019" — with the exact date always in
  the tooltip; technical surfaces keep exact dates. Register-gated via
  the existing notice-register/policy seam, off-able.
- [ ] **4c** when two results in one set share a display name
  (`invoice.pdf` × 8), the **distinguishing path segment is emphasised**
  in the location line — the identically-named-tabs trick editors use.
  Computed in the presenter over the result set in hand; zero queries.

## 5. Feel — the details that read as polish

- [ ] **5a** rows paint a subtle **hover state** (`State_MouseOver`;
  theme token, both themes).
- [x] **5b** the list scrolls **per pixel** (`ScrollPerPixel`) — per-item
  scrolling with tall rows feels notchy.
- [ ] **5c** the list ends with a quiet **terminator** — "that's all 23"
  in plain words, faint — so the end of the list reads as an answer, not
  a stall. Plain register wording; count agrees with the status line by
  construction (same source).
- [ ] **5d THE STABLE-UPDATE RULE**: when the full tier replaces the
  interim tier (or repository rows append), the refresh may add and
  re-rank rows, but the row under the pointer — and the current
  selection — must not visibly jump. Selection is already id-keyed;
  extend the discipline to scroll/anchor behaviour. This is the
  generation system's promise, one level up.

## 6. Keyboard-first flow

- [ ] **6a** with focus in the search box: **↓/↑ move the result
  selection while focus stays in the box** (typing continues the query,
  no Tab dance); the preview follows the selection; **Enter opens the
  selected result** (today's behaviour when nothing is selected is
  unchanged); Ctrl+Enter reveals in folder. The selected row's focus
  state is clearly visible. Spotlight/Raycast/VS Code convention — the
  power-user feature that costs the 8-year-old nothing.

## 7. Accessibility verification (a pass, not a feature)

- [ ] **7a** painted delegates are where accessibility silently dies:
  verify `accessible_text` for every row form this order adds or changes
  (icons carry the kind word; sender-first mail reads sensibly; chevron
  state announced; friendly dates read as their exact date). Verify the
  delegate survives Windows text scaling at 125/150/200% (the relative
  font machinery should — prove it) and that the highlight remains
  signalled by weight as well as colour. World class means the screen
  reader user and the 8-year-old both get the good version.

## 8. Tests

- [ ] snippet: window always contains ≥1 highlight (property test over
  generated match positions — Qt-free); boundary snapping never cuts a
  word; two-line hint equals two-line paint at every density (the
  gap-regression shape).
- [ ] chevron: multi-chunk groups paint it, single-chunk groups don't;
  toggle works by chevron and by row; subtitle counts match chunks.
- [ ] kind rows: mail fixture renders sender-first; code fixture renders
  monospace + line number; icon cache returns one pixmap per extension.
- [ ] locations: left-elision keeps the tail at narrow widths; twins
  fixture (8 × invoice.pdf) shows distinguishing segments; unique names
  unchanged.
- [ ] friendly dates: register-gated (plain on, technical off), tooltip
  always exact; off-switch honoured.
- [ ] stable update: interim→full swap with pointer over row N — N's
  payload id unchanged on screen (pytest-qt).
- [ ] keyboard: pytest-qt — type, ↓↓, Enter opens the third result,
  focus never left the box; preview followed.
- [ ] accessibility: accessible text assertions per changed row form;
  suite runs at a forced 150% scaling fixture where feasible.
- [ ] pytest-qt scenario per item (0m convention); all new strings pass
  plain-words/tooltip rules.

## Done means

Change + tests + suite green + committed by name; CHANGELOG per
user-visible change. Acceptance sentence: every result on the page shows
the words that put it there, cut like a sentence rather than a database
dump; a document that matched five times says so; mail looks like mail
and code looks like code; the list never jumps under your hands, ends by
saying so, and the whole page can be driven — and heard — without a
mouse.
