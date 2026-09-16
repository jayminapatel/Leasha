# Work order (One thread): test automation — the GUI clicked for real, the system proven nightly

**Doc version:** 1.2 · **Updated:** 2026-09-16 · **Applies to:** app v0.3.3
**Thread:** One thread (tests + tooling; app code changes only where a test
exposes a bug)
**Status: IN PROGRESS 2026-09-16** - the owner authorized starting this order
directly, alongside the rest of the open backlog ("do all the coding and
engineering... remove all blocks and defers"). (Was: HELD by the owner
2026-08-28, was-LAST after 0l - see the dated note below for what shipped
this pass and what is still genuinely open.) `pytest-qt`, `pywinauto`,
`hypothesis`, `pytest-timeout` were already in the venv and are now pinned
in `requirements-dev.txt` too, so a fresh venv matches.

**Why this order exists**: the presenter split means logic tests without a
display, and the wiring tests assert connections exist — but nothing in the
suite has ever *pressed a key in a real widget* or driven the assembled app.
The rerank-toggle class of bug (emitted, connected to nothing, looks fine)
lives exactly in that gap, and this project has met it repeatedly. This order
closes the gap in layers, cheapest and most valuable first.

## 0. ADDED by owner 2026-08-28 — the grab script: eyes for the AI (FIRST item when this order starts)

- [x] **0a** `tools/grab_ui.py`: constructs the real `MainWindow` offscreen
  (`QT_QPA_PLATFORM=offscreen`) against a temp/fixture store, walks every
  page and named surface (each tab, Settings, Indexing, dialogs where
  constructible), calls `widget.grab().save(...)` per surface into
  `outputs/screenshots/<surface>.png`, and exits. Optional args: one
  surface only; a `--size WxH` to reproduce layout bugs at a given window
  size. No new dependencies — `grab()` renders offscreen with what is
  already installed.
- [x] **0b** why it is first: it gives the coding agent EYES. The workflow
  it unlocks: "the Files tab looks wrong" → run the script → the agent
  reads the PNG (it is multimodal) → hypothesis → pytest-qt regression →
  fix → re-grab → visually confirm. Every later UI order benefits, and
  §5's visual goldens are this script plus a stored baseline and a diff —
  so 0a is the seed of §5, not a separate machine.
- [x] **0c** a smoke test: the script runs headless in CI, produces a
  non-empty PNG per expected surface, and no surface list drift (a new
  tab without a grab entry fails the test — the walker-test shape).

## 1. pytest-qt — real widgets, in-process (the core of the order)

- [x] **1a** harness: a `qtbot` fixture constructing the real `MainWindow`
  against a temp store + fixture index (reuse the integration fixtures);
  offscreen platform (`QT_QPA_PLATFORM=offscreen`) so it runs headless; a
  `gui` pytest marker so the subset is selectable like `jvm` is.
- [ ] **1b** scenario tests for every user journey the orders promised, each
  as keystrokes-and-assertions, not signal introspection: type → interim →
  full results render; Esc clears results AND status (the M9 regression, now
  end-to-end); tab switching; `/` popup opens, offers, inserts, scoped
  values appear; settings toggle → engine state changes (the anti-rerank-bug
  test, done properly); pop-out opens/finds/rotates/closes (workspace
  order); Offline Media Scan/Rescan/Delete against a fixture volume; the
  8-year-old scenarios from the search-experience order re-expressed as real
  keystrokes.
- [x] **1c** the rule that keeps this suite alive: every future work order's
  "acceptance sentence" gets its pytest-qt scenario **in that order** — this
  order seeds the harness and the backlog of existing journeys; the
  convention is recorded in WORKORDER-CONVENTIONS §5 as a dated note.
- [x] **1d** teach the load-bearing-tests table the new guard: a wiring test
  that a control changes observable behaviour is now expressible as a real
  interaction — migrate the weakest wiring assertions to interactions where
  cheap; never delete a passing guard to do it.

## 2. hypothesis — property tests for the parser-shaped code

- [x] **2a** `parse_query`: any generated string parses without raising;
  round-trip properties (rendering a parsed query re-parses to the same
  structure); the negation/phrase/colon corner cases (M4's family) as
  properties, not examples.
- [x] **2b** `sanitise.py`: output is always well-formed for arbitrary input
  (it is an allow-list rebuild — assert the allow-list holds under fuzz).
- [x] **2c** chunker: offsets invariant (`text[start:end] == segment`) for
  generated documents of arbitrary paragraph shapes; the perf floor guards
  stay separate.
- [x] **2d** filters/`file_filter_sql`: generated filter combinations always
  produce parameterised SQL (no injection shape possible under fuzz), and
  LIKE-escaping properties hold.
- [x] **2e** deadline/health-check settings tuned so hypothesis runs in the
  default suite without flaking CI (profile: fewer examples default, many
  under a `slow` marker).

## 3. pywinauto — five black-box journeys, no more

- [ ] **3a** UIA-driven smoke against the *packaged/launched* app (leasha.cmd
  path, real window): launch → window appears; search → result row exists
  (found via accessible names — the accessibility discipline pays here);
  open a result; pop-out + stay-on-top actually stays on top; clean close
  mid-search (the shutdown-race classic, black-box). Marked `e2e`, excluded
  from the default run, executed before releases and after the PySide6
  migration.
- [ ] **3b** flake discipline: each journey retries once, artifacts a
  screenshot on failure, and a journey that flakes twice in a month is fixed
  or deleted — a flaky e2e suite is worse than none (recorded as the rule).

## 4. Visual regression, lightly

- [x] **4a** `qtbot`-grabbed goldens for the key views (search with results,
  files, mail, indexing, settings, pop-out) in BOTH themes; tolerance-based
  diff (imagehash distance is fine); goldens updated only deliberately, in
  the commit that changes the look, named in its message. This is the only
  automated catch for the theme-token bug class (the unreadable-QListView
  incident).

## 5. The nightly system loop (on the owner's machine — the target hardware)

- [ ] **5a** one script: build/refresh the scale fixture → full index run via
  CLI → perf floors asserted (chunker, embed rate, ladder, search p95 vs
  pinned numbers) → `evaluate --builtin` recall floors → kill-resume spot
  check → one-line report appended to a log the owner can glance at
  (`logs/nightly.log`), loud only on failure.
- [ ] **5b** registered as a Windows scheduled task by an opt-in installer
  step (never silently — the owner enables it once); `doctor` shows when the
  nightly last passed.
- [x] **5c** GitHub Actions `windows-latest`: unit + pytest-qt(offscreen) +
  hypothesis(default profile) on every push; the `e2e`/`jvm`/`slow` markers
  excluded. This is the contributor gate for the open-source future.

## 6. Done means

Suite green with the new layers on Windows; the `gui` subset counted as RUN
not skipped (the vacuous-skip lesson from the PySide6 draft applies here
from day one); nightly has passed three consecutive nights on the owner's
machine; flake rule and acceptance-scenario convention recorded as dated
notes in WORKORDER-CONVENTIONS. Acceptance sentence: a stranger's pull
request cannot silently break a keystroke, a theme, a floor, or a promise —
because something automated presses the keys every day.

---

> **2026-09-16 — owner authorized starting this order directly** ("do all
> the coding and engineering... remove all blocks and defers"), alongside
> the rest of the open backlog. What shipped this pass, and what is still
> genuinely open:
>
> **Section 0 (0a/0b/0c), ticked.** Already built during the UI Redesign
> order (`202626160950` §9k/§9l) and confirmed here rather than rebuilt.
> Verifying it live caught a real bug: `tools/grab_ui.py`'s `SURFACES` dict
> still said `"page": "Offline Media"` after the rail tab was shortened to
> "Offline" - `test_every_rail_page_has_a_grab_surface` (0c's own smoke
> test) failed on it immediately. Fixed. This is the exact class of drift
> §0's own text says the tool exists to catch.
>
> **Section 1, mostly ticked.** 1a: `tests/unit/conftest.py`'s
> `gui_mainwindow` fixture - a real `SqliteStore`, a real `SearchEngine`
> (only the embedding model stubbed, so nothing downloads), a real
> `MainWindow`, driven by `qtbot`. The `gui` marker is registered in
> `pyproject.toml` and, per the item's own requirement, is **not** excluded
> from the default run - only `jvm` and the new `e2e` marker are.
> `tests/unit/test_gui_scenarios.py` has nine real keystroke-and-click
> scenarios, all passing, stable across repeated runs: type-to-results, a
> no-match query, Escape clearing the box, the rerank checkbox actually
> flipping `engine.reranker.enabled`, both rerank controls staying in step,
> the `/` popup inserting an operator by a real mouse click on its popup
> row, and the pop-out opening/staying-on-top/closing. Driving the Escape
> scenario found a second real bug: nothing had ever wired Escape to empty
> the search box - `_on_text_changed`'s own comment already said "or
> pressing Esc" as if it did, but no `keyPressEvent`/`QShortcut` existed
> anywhere on the box. Fixed in `search_view.py`'s existing `eventFilter`
> (the same one already handling Up/Down/Enter), reusing the M9 empty-box
> path rather than duplicating it. **1b left unticked**: what is built is
> real and verified, not a placeholder, but it does not yet cover Offline
> Media Scan (opens a native drive-browse dialog, not drivable headless -
> Rescan/Delete against a fixture volume are covered instead), the pop-out's
> find/rotate (needs a real photo; this file's fixtures are text/PDF), or an
> exhaustive re-expression of every "eight-year-old" scenario from the
> search-experience order. 1c: the convention is recorded in
> `docs/WORKORDER-CONVENTIONS.md` §5b, dated. 1d: the M12 rerank wiring
> tests in `test_review_section_four.py` are untouched (never delete a
> passing guard) and now have real-interaction siblings alongside them.
>
> **Section 2, ticked in full.** `tests/unit/test_query.py`,
> `test_sanitise.py`, `test_chunker.py` each gained `hypothesis` properties
> (2a/2b/2c); `tests/unit/test_filters_properties.py` is new (2d) - it runs
> `file_filter_sql`'s output against a real, migrated `SqliteStore` schema
> rather than only asserting shape, the same reasoning `test_query.py`
> already uses for `to_fts_match`. 2a's "round-trip" is driven through the
> parser's own grammar (build a query from a known filter, confirm the same
> filter parses back out) rather than through a `ParsedQuery` renderer,
> because no such renderer exists - inventing one only for this test would
> have been scope the item never asked for. 2e: a `"leasha"` hypothesis
> profile (`max_examples=25`, no deadline) is registered and loaded by
> default in `tests/conftest.py`; a `"leasha-thorough"` profile
> (`max_examples=300`) is available via `HYPOTHESIS_PROFILE=leasha-thorough`
> for a deliberate deeper run.
>
> **Section 3, left unticked.** `tests/unit/test_e2e_pywinauto.py` has the
> five journeys, written against an isolated fixture `.env` (never the real
> `D:\Leasha\Data` - non-negotiable #10), UIA lookups by `auto_id`/name, and
> 3b's retry-once-then-screenshot mechanism. **Not live-verified**: driving
> real UIA mouse/keyboard synthesis against a genuinely focused, on-screen
> window is a different thing from the offscreen Qt this session can run,
> and needs the owner's own interactive desktop - the same category of gap
> as 0l's on-tape ordering and the §4 UNC test, both already documented as
> hardware-blocked rather than code-blocked. Run it by hand:
> `pytest tests/unit/test_e2e_pywinauto.py -m e2e -v`.
>
> **Section 4, ticked.** Already built and verified during the UI Redesign
> pass; re-confirmed here (`test_fresh_grabs_match_the_goldens_within_
> tolerance`, imagehash `phash`, both themes, eight goldens on disk).
>
> **Section 5, one of three ticked.** 5a left unticked on purpose: `tools/
> nightly.py` exists and was verified live - twice, once with `--quick` and
> once running the kill-resume stage for real, both against the real CLI,
> producing a real `evaluate --builtin` recall (0.7) and a real
> `logs/nightly.log` line - but it does not yet assert the chunker/embed-
> rate/ladder/search-p95 perf floors the item names, because no real
> measurement of any of them exists yet to pin as a floor. `PERF_FLOORS` in
> the script is explicit about this (`None` until a real number replaces
> it) rather than a guessed placeholder - this project's "measure, do not
> assume" rule applies to a floor as much as to any other number. 5b left
> unticked: `doctor.py`'s half (`check_nightly_status`, "last passed"/"last
> failed" read from `logs/nightly.log`) is built and verified against a
> real log line; `install.ps1`'s opt-in scheduled-task step is written,
> ASCII-checked, BOM-preserved, and PowerShell-parses cleanly, but was
> deliberately **not run** - registering a real recurring Scheduled Task is
> a standing-configuration change on a real machine, not something to do
> without being asked. 5c ticked: `.github/workflows/ci.yml` runs unit +
> `gui` (offscreen) + hypothesis on `windows-latest`, excluding
> `e2e`/`jvm`/`slow` - YAML-validated here; an actual push is what proves it
> against real GitHub Actions.
>
> **§6's own "done means" is not fully met**, and said so above rather than
> being reworded: the suite is green with every new layer that can run
> offscreen, but "nightly passed three consecutive nights" is inherently a
> multi-day, real-hardware claim nobody can make from inside one session.
