# The A+ checklist — what stands between here and world-class, and when each item can be done

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3

The target state, from the 2026-08-27 prospective review: **nothing claimed
that isn't proven, nothing pending that matters, nothing broken that's known.**
Three groups: what can be done NOW while the product is still under
development, what needs the current work orders landed first, and what only
matters at publication — which is deliberately unplanned, so that group is
parked, not forgotten. This is a checklist, not a work order: nothing here
instructs the thread until the owner promotes an item.

## 1. Now — during development, no future plan required

- [ ] **Decide the licence gate** (decision, ~an evening of reading + one
  choice). PyQt6 = GPLv3 = distributing the app ever means the code is GPL
  forever. PySide6 (LGPL, near-identical API) keeps every future open —
  free, commercial, or both. Since the future is unplanned, PySide6 is the
  option-preserving choice, and its cost grows with every Qt line written.
  If chosen, the migration is a good candidate for the post-working-version
  restructure pass — but the *decision* is now.
- [x] **Decide the household-privacy gate — DECIDED 2026-08-27.** Per-account
  `%LOCALAPPDATA%\Leasha` default (choosable at install), roots start empty
  with own-profile suggestions, honest-paragraph stance on shared logins,
  owner's install grandfathered. Released to the thread as
  `WORKORDER-202626270257-privacy-defaults.md`. Gate closed as a decision;
  the checkbox in that order closes the implementation.
- [ ] **Run the evaluate questions on the real corpus** (HANDOFF items 4–6,
  already listed as next): twenty real sentences — does semantic retrieval
  earn its place; chunk size and model precision; AND_TERM_LIMIT against a
  realistic corpus. These need no new code, only runs and honesty.
- [ ] **The first real scale pass** (HANDOFF items 1–3): the 200K-message
  PST, the ~50GB run, Outlook COM once, the full suite on Windows. Worth
  doing once *now* to find the unknowns early, and again after index tuning
  to measure the improvement — two data points instead of one.
  *Note (owner, 2026-08-27): Outlook COM has run in testing — the only code
  path no test could exercise now has. Remaining: the same at scale, plus
  the PST and 50GB passes.*
- [ ] **Decide PySide6 — DECIDED 2026-08-27**: PySide6 chosen to keep every
  licensing future open. Draft order:
  `WORKORDER-202626270238-pyside6-migration.md` (NOT for execution;
  scheduled first in the restructure window). Gate closed as a decision.
- [ ] **Start the claims-dating habit** (rule, zero code): from today, any
  comment stating a measurement carries its date; any comment stating an
  invariant names the test enforcing it. Old comments get dated as they are
  touched — no sweep, just a ratchet. This is the defence against the
  project's one recurring disease: documentation good enough to be believed.
- [ ] **Verify the diagnostics path end to end once**: a friend-shaped user
  can find "something's wrong", produce the redacted bundle, and get it to
  the owner. The machinery exists; the walk-through hasn't happened.

## 2. After the current orders land (the working version exists)

- [ ] **Re-run the comprehensive review** (standing offer): verify every tick
  in all four orders at file:line, review the new code as new code, test
  each order's acceptance sentence, plus the claims audit above.
- [ ] **Prove the new concurrency at scale**: kill-at-50%-and-resume against
  the feeder-thread / two-phase pipeline shape, on a large corpus, with the
  crash-ordering invariants asserted rather than commented.
- [ ] **Pin the scale numbers as regression floors**: the measured chunks/min,
  search p95, and stage shares from the post-tuning scale run become test
  floors, the way the chunker floor already works.
- [ ] **Declare the feature freeze**: from here to publication, fixes and
  measurements only. The owner's own "working version first" rule, applied
  through to the end. (The parked restructures — cli/presenter/shell splits,
  and PySide6 if chosen — happen inside this window, when the tree is calm.)

## 3. At publication — parked until the future is planned

- [ ] **Code signing certificate** — unsigned installers meet SmartScreen
  warnings no friend's parent should click through. Decide signed-vs-not
  when distribution is real; buying early gains nothing.
- [ ] **The install order's five [FINALISE] questions**
  (`WORKORDER-202626082213-install-and-distribution.md` §7): freeze vs uv,
  per-user vs per-machine, default index location, self-update policy,
  minimum Windows version.
- [ ] **Auto-update position** — an updater, or a stated no-update policy.
  Either is fine; silence is not.
- [ ] **Open-source paperwork**: licence headers matching the gate decision;
  fixtures/docs audit for anything personal (paths, machine names, real
  senders); CONTRIBUTING.md explaining the work-order and load-bearing-test
  culture — it is the project's real constitution; issue templates that
  route non-technical reports; README whose first screenshot is the
  8-year-old search succeeding.
- [ ] **Support surface for strangers**: plain-words troubleshooting page for
  the five failures friends will actually hit; the one-click resets
  ("Return to automatic", "Reset search behaviour") verified as the
  recovery path they claim to be.

## What A+ explicitly does NOT require

Rewriting in Rust, more features, more configurability, or solving the
one-maintainer constraint. Those are stated limits, not flaws. The grade is
earned by the three sentences at the top of this file.
