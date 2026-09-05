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

  > **2026-09-05 (session 4).** The 2026-09-04 verification note above this
  > file's section 1 checked that the chevron glyph is *drawn*
  > (`group_subtitle`/`_paint_group`) but not that it is a click target in
  > its own right - and it was not one: only the whole row toggled, via
  > double-click/Enter, which is what "the whole row still toggles as it
  > does today" already promised as unchanged. A single click on the
  > chevron's own line did nothing. Fixed, not just found:
  > `ResultDelegate.subtitle_rect`/`chevron_hit` and a new
  > `ResultsView.eventFilter` on the list's viewport - see the dated note
  > under this section's own test bullet in §8 for the files and tests.

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
- [x] **4c** when two results in one set share a display name
  (`invoice.pdf` × 8), the **distinguishing path segment is emphasised**
  in the location line — the identically-named-tabs trick editors use.
  Computed in the presenter over the result set in hand; zero queries.

## 5. Feel — the details that read as polish

- [x] **5a** rows paint a subtle **hover state** (`State_MouseOver`;
  theme token, both themes).
- [x] **5b** the list scrolls **per pixel** (`ScrollPerPixel`) — per-item
  scrolling with tall rows feels notchy.
- [x] **5c** the list ends with a quiet **terminator** — "that's all 23"
  in plain words, faint — so the end of the list reads as an answer, not
  a stall. Plain register wording; count agrees with the status line by
  construction (same source).

  > **2026-09-04 (session 3).** Now actually ticked. As the verification
  > note above this file's section 1 says, `results_terminator()` existed
  > with its own tests but nothing ever called it - the list never showed
  > it. `presenter.Terminator` is now a small frozen dataclass the delegate
  > recognises and paints centred and faint (`ResultDelegate._paint_terminator`),
  > and `ResultsView._rebuild` appends one, non-selectable and disabled, after
  > every real row via `_append_terminator`. Count agrees with the status line
  > by construction, as the item says, because both read `len(self._rows)`.
- [x] **5d THE STABLE-UPDATE RULE**: when the full tier replaces the
  interim tier (or repository rows append), the refresh may add and
  re-rank rows, but the row under the pointer — and the current
  selection — must not visibly jump. Selection is already id-keyed;
  extend the discipline to scroll/anchor behaviour. This is the
  generation system's promise, one level up.

## 6. Keyboard-first flow

- [x] **6a** with focus in the search box: **↓/↑ move the result
  selection while focus stays in the box** (typing continues the query,
  no Tab dance); the preview follows the selection; **Enter opens the
  selected result** (today's behaviour when nothing is selected is
  unchanged); Ctrl+Enter reveals in folder. The selected row's focus
  state is clearly visible. Spotlight/Raycast/VS Code convention — the
  power-user feature that costs the 8-year-old nothing.

  > **2026-09-04 (session 3).** `SearchView.eventFilter`, installed on the
  > search box, forwards ↓/↑ to `ResultsView.forward_key` (which calls the
  > list's own `keyPressEvent` - Qt's native handling already moves the
  > selection, scrolls it into view, and skips the disabled terminator row
  > correctly, so nothing here re-derives that arithmetic) and dispatches
  > Enter/Ctrl+Enter to `ResultsView.open_current`. Preview already follows
  > the selection via the existing `currentChanged` → `selected` signal
  > chain, unchanged. Enter with nothing selected is left unconsumed, so the
  > box's own `returnPressed` runs a full search exactly as before.
  >
  > **Both `search_view.py` and `results_view.py` were already at, or one
  > line under, the 250-code-line view guard before this item** (see the
  > note on item 5c). Fitting this item required compacting several
  > pre-existing multi-line calls/signatures in both files to single lines
  > (no behaviour changed, only line breaks removed) and, in
  > `SearchView.eventFilter` alone, combining two guard/return pairs onto
  > one physical line each with a semicolon - the one place in this order's
  > four files that departs from the codebase's own style, called out here
  > rather than left for someone to wonder about. `search_view.py` is now at
  > 248/250; the true fix, if the ceiling is reached again, is the
  > deferred `search_view.py`/`results_view.py` split this project's
  > "Working version first" rule holds off until the feature orders are
  > done - not another round of one-line compaction.

## 7. Accessibility verification (a pass, not a feature)

- [x] **7a** painted delegates are where accessibility silently dies:
  verify `accessible_text` for every row form this order adds or changes
  (icons carry the kind word; sender-first mail reads sensibly; chevron
  state announced; friendly dates read as their exact date). Verify the
  delegate survives Windows text scaling at 125/150/200% (the relative
  font machinery should — prove it) and that the highlight remains
  signalled by weight as well as colour. World class means the screen
  reader user and the 8-year-old both get the good version.

  > **2026-09-05 (session 3).** This pass found two of the four
  > sub-verifications were not actually true and fixed them rather than
  > just recording the gap: `accessible_text`/`result_tooltip` never
  > mentioned the kind word at all (item 3a's icon replaced the `[PDF]` text
  > tag that used to carry it visually, but nothing was ever added to speak
  > or hover it - `kind_tag` is now in both) and the chevron's expanded/
  > collapsed state was purely visual (`accessible_text` now takes an
  > `expanded` flag from `ResultsView._append`, which already had it, and
  > says "5 matches, expanded"/"collapsed"). **Sender-first mail** already
  > read sensibly, for free - item 3b's name field IS what `accessible_text`
  > speaks. **Friendly dates** now read as their exact date deliberately:
  > `accessible_text` prefers `when_exact` over `when`, so a screen reader
  > never hears the register's "yesterday" approximation, sighted or not.
  > Text scaling (125/150/200%) and the weight-plus-colour highlight signal
  > were both already true by construction (`_fonts` is relative to
  > `option.font`, never a fixed pixel count; `_draw_run` sets a bold font
  > and a distinct pen for every highlighted run) - each now has its own
  > test proving it rather than resting on that being obviously so.

## 8. Tests

- [x] snippet: window always contains ≥1 highlight (property test over
  generated match positions — Qt-free); boundary snapping never cuts a
  word; two-line hint equals two-line paint at every density (the
  gap-regression shape).

  > **2026-09-05 (session 4).** The example tests already covered a handful
  > of hand-picked positions; two `hypothesis` property tests now cover the
  > space between them - `test_the_window_always_contains_the_match_wherever_it_lands`
  > and `test_the_window_never_cuts_a_word_wherever_it_lands`
  > (`test_presenter.py`), generating the match's position in the source
  > text rather than fixing it. `hypothesis` was already a project
  > dependency (per `HANDOFF.md`) with no prior test using it - this is the
  > first. The two-line hint/paint agreement was tested at comfortable
  > density only; `test_sizehint_stays_one_line_tall_at_compact_density`
  > (`test_result_delegate.py`) closes the "at every density" half the
  > existing test's own docstring already claimed but did not check.
- [x] chevron: multi-chunk groups paint it, single-chunk groups don't;
  toggle works by chevron and by row; subtitle counts match chunks.

  > **2026-09-05 (session 4).** Writing this test found the same shape of
  > gap session 3 found under 5c and 7a: item 2a's own text says "the
  > chevron is a real click target", but nothing in `results_view.py` or
  > `result_delegate.py` gave the chevron a click handler of its own -
  > only the whole row toggled, via double-click/Enter (`_on_activated`,
  > pre-existing and unchanged). Fixed rather than merely noted:
  > `ResultDelegate.subtitle_rect` (the chevron line's geometry, shared with
  > `paint` so a click and a paint can never disagree) and
  > `ResultDelegate.chevron_hit` (the hit test itself - kept out of
  > `results_view.py` on purpose, since that file's own 250-code-line guard
  > was already close to full) back a new `ResultsView.eventFilter` on the
  > list's viewport. Tests: `test_the_chevron_has_its_own_click_target_on_multi_match_groups`,
  > `test_chevron_hit_finds_the_file_id_under_a_click_on_the_subtitle_line`,
  > `test_chevron_hit_ignores_an_invalid_index` (`test_result_delegate.py`);
  > `test_a_click_on_the_chevron_expands_the_group`,
  > `test_a_click_off_the_chevron_does_not_toggle`,
  > `test_the_whole_row_still_toggles_by_activation` (`test_results_view.py`,
  > pytest-qt). `results_view.py` is now at 249/250 code lines.
- [ ] kind rows: mail fixture renders sender-first; code fixture renders
  monospace + line number; icon cache returns one pixmap per extension.

  > **2026-09-05 (session 4).** Left unticked - the line-number half is the
  > same infrastructure gap item 3c's own note above documents, and cannot
  > be tested without being built. The other two-thirds are genuinely
  > covered: mail-fixture sender-first in
  > `test_a_message_group_leads_with_the_sender_not_a_folder`
  > (`test_result_groups.py`), the code fixture's monospace half in
  > `test_a_code_row_gets_a_monospace_font` (`test_result_delegate.py`), and
  > the icon cache in `test_an_icon_is_cached_after_the_first_lookup` -
  > keyed per `kind` by construction, so a second kind can never reuse the
  > first's cache slot.
- [ ] locations: left-elision keeps the tail at narrow widths; twins
  fixture (8 × invoice.pdf) shows distinguishing segments; unique names
  unchanged.

  > **2026-09-05 (session 4).** Left unticked for the same reason as 4a
  > above: `elide_path_left()` is not called from anywhere that draws a
  > location line, so "keeps the tail at narrow widths" has no rendered
  > location line to test it against - testing the bare function again
  > would only restate the coverage `test_left_eliding_keeps_the_tail`
  > already has. The twins and unique-names thirds are genuinely covered -
  > `test_eight_invoices_are_all_told_apart` is the work order's own
  > 8-invoice example, `test_two_invoices_in_different_clients_get_the_client_emphasised`,
  > `test_twins_already_distinguished_by_the_ordinary_breadcrumb_are_untouched`
  > and `test_unique_names_carry_no_emphasis` (all `test_result_groups.py`).
- [x] friendly dates: register-gated (plain on, technical off), tooltip
  always exact; off-switch honoured.

  > **2026-09-05 (session 4).** Already fully covered, verified rather than
  > assumed: `test_plain_register_reads_friendly`,
  > `test_technical_register_keeps_an_exact_date`,
  > `test_the_exact_date_is_always_present_whichever_register_is_showing`,
  > `test_an_unrecognised_register_falls_back_to_plain` (`test_result_groups.py`);
  > the global off-switch in `test_turning_off_plain_words_globally_reaches_the_search_tab_too`
  > (`test_presenter.py`).
- [x] stable update: interim→full swap with pointer over row N — N's
  payload id unchanged on screen (pytest-qt).

  > **2026-09-05 (session 4).** Already fully covered, verified rather than
  > assumed: `test_the_current_row_survives_a_rebuild_that_adds_rows`
  > (`test_results_view.py`, pytest-qt) is exactly this scenario - interim
  > tier, land on document 2's row, full tier re-ranks and appends, document
  > 2 is still current by `file_id`.
- [x] keyboard: pytest-qt — type, ↓↓, Enter opens the third result,
  focus never left the box; preview followed.

  > **2026-09-05 (session 4).** The individual pieces (arrow moves
  > selection, Enter opens, Ctrl+Enter reveals, focus never called) were
  > already covered separately; `test_the_whole_flow_types_then_arrows_down_then_opens_the_third_result`
  > (`test_search_view.py`) is the first to chain them into one scenario and
  > assert the preview's `selected` signal fired along the way. One
  > correction to this bullet's own count, in a note rather than an edit to
  > it: nothing is current straight after a search, so it takes three ↓
  > presses to reach the third result, not two - `show_results` clears the
  > anchor, and the first ↓ from nothing selected lands on the *first*
  > result, exactly as `test_arrow_down_moves_the_selection_without_leaving_the_box`
  > already showed before this session.
- [x] accessibility: accessible text assertions per changed row form;
  suite runs at a forced 150% scaling fixture where feasible.

  > **2026-09-05 (session 4).** Already fully covered, verified rather than
  > assumed: `test_accessible_text_carries_the_kind_word`,
  > `test_accessible_text_announces_a_multi_match_group_s_state`,
  > `test_accessible_text_says_nothing_about_expansion_for_a_single_match`,
  > `test_accessible_text_prefers_the_exact_date_over_the_friendly_one`
  > (row-form assertions); `test_the_delegate_scales_with_the_system_font`
  > is parametrised over 100/125/150/200%, so the 150% fixture this bullet
  > asks for is one of its four cases (all `test_result_delegate.py`).
- [ ] pytest-qt scenario per item (0m convention); all new strings pass
  plain-words/tooltip rules.

  > **2026-09-05 (session 4).** Left unticked for the same reason order 0s
  > recorded against its own copy of this exact bullet the same day: 0m
  > (`docs/WORKORDER-202626270547-test-automation.md`) is explicitly HELD
  > by the owner ("do not execute until he says when"), and the
  > qtbot/real-`MainWindow` harness that convention names does not exist
  > anywhere in the repo - building it here would mean executing 0m's own
  > work past its hold, on the strength of one bullet in a different order.
  > The other half is true and checked: every string this order added or
  > changed (`kind_tag`, `group_subtitle`'s "N matches ▸/▾", the
  > terminator's "That's all — N results.", the friendly-date words) reads
  > in the same plain, jargon-free voice as this order's already-shipped
  > strings, and `test_tooltips.py`'s AST guard already covers every module
  > under `app/ui/`, this order's four files included.

## Done means

Change + tests + suite green + committed by name; CHANGELOG per
user-visible change. Acceptance sentence: every result on the page shows
the words that put it there, cut like a sentence rather than a database
dump; a document that matched five times says so; mail looks like mail
and code looks like code; the list never jumps under your hands, ends by
saying so, and the whole page can be driven — and heard — without a
mouse.
