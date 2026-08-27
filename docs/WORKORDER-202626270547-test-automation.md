# Work order (One thread): test automation — the GUI clicked for real, the system proven nightly

**Doc version:** 1.0 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
**Thread:** One thread (tests + tooling; app code changes only where a test
exposes a bug)
**Status: HELD by the owner 2026-08-28 — do not execute until he says when.**
(Was: RELEASED 2026-08-28, queued LAST after 0l.) The owner will give the
word to start; nothing here is cancelled, and the was-LAST intent stands —
whenever he releases it, it still runs after whatever has landed, to test
the assembled whole. The per-order scenario convention (each order writes
pytest-qt scenarios for its own acceptance sentences) CONTINUES while this
is held — that convention lives in the other orders, not here. Owner has
installed `pytest-qt`, `pywinauto`, `hypothesis` into the venv.

**Why this order exists**: the presenter split means logic tests without a
display, and the wiring tests assert connections exist — but nothing in the
suite has ever *pressed a key in a real widget* or driven the assembled app.
The rerank-toggle class of bug (emitted, connected to nothing, looks fine)
lives exactly in that gap, and this project has met it repeatedly. This order
closes the gap in layers, cheapest and most valuable first.

## 1. pytest-qt — real widgets, in-process (the core of the order)

- [ ] **1a** harness: a `qtbot` fixture constructing the real `MainWindow`
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
- [ ] **1c** the rule that keeps this suite alive: every future work order's
  "acceptance sentence" gets its pytest-qt scenario **in that order** — this
  order seeds the harness and the backlog of existing journeys; the
  convention is recorded in WORKORDER-CONVENTIONS §5 as a dated note.
- [ ] **1d** teach the load-bearing-tests table the new guard: a wiring test
  that a control changes observable behaviour is now expressible as a real
  interaction — migrate the weakest wiring assertions to interactions where
  cheap; never delete a passing guard to do it.

## 2. hypothesis — property tests for the parser-shaped code

- [ ] **2a** `parse_query`: any generated string parses without raising;
  round-trip properties (rendering a parsed query re-parses to the same
  structure); the negation/phrase/colon corner cases (M4's family) as
  properties, not examples.
- [ ] **2b** `sanitise.py`: output is always well-formed for arbitrary input
  (it is an allow-list rebuild — assert the allow-list holds under fuzz).
- [ ] **2c** chunker: offsets invariant (`text[start:end] == segment`) for
  generated documents of arbitrary paragraph shapes; the perf floor guards
  stay separate.
- [ ] **2d** filters/`file_filter_sql`: generated filter combinations always
  produce parameterised SQL (no injection shape possible under fuzz), and
  LIKE-escaping properties hold.
- [ ] **2e** deadline/health-check settings tuned so hypothesis runs in the
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

- [ ] **4a** `qtbot`-grabbed goldens for the key views (search with results,
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
- [ ] **5c** GitHub Actions `windows-latest`: unit + pytest-qt(offscreen) +
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
