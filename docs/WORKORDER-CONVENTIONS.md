# How work orders work now

**Doc version:** 2.1 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3

## One thread, from 2026-08-25

The owner has collapsed Planning, Backend and UI into **a single thread**. The
reason given: handovers between threads cost more than the collisions they
prevented.

That is a real trade and it went the right way. Two threads meant that a change
crossing the boundary - a store accessor the window needed, a notice the window
had to draw - stopped at the boundary and waited for a document. Three items sat
in `HANDOFF-ui-to-backend.md` for a day; two of them were an hour's work each.

**What is gone:** asking permission. One thread owns every path in the table
below and may edit any of it.

**What is not gone, and must not be:** everything the ownership table was
protecting. The boundaries were never really about who was allowed to type -
they were about *layering*, and the layering is what makes this application
testable. Keep reading; §0 is now the important part of this document.

---

## 0. What the thread split was actually enforcing

With two threads, a UI change that reached into `app/storage/` was stopped by a
convention. With one thread there is no convention left to stop it, and the only
thing standing between this codebase and a window that queries SQLite inline is
a handful of tests. **They are load-bearing now in a way they were not before.**

**Named exactly, and with the file each lives in.** Two of the seven are in
files whose names do not suggest them, and one row here named a test that does
not exist - `test_the_presenter_still_imports_no_qt`. A table of load-bearing
tests that names them approximately is a table that will not notice the day one
is deleted, which is the only day it matters.

| Test | Where | Stops | What it stops happening |
|---|---|---|---|
| `test_every_qt_view_keeps_its_logic_in_the_presenter` | `test_presenter.py` | `*_view.py` over 250 code lines | Logic drifting into views, where it cannot be tested without a display |
| `test_the_presenter_never_imports_qt` | `test_presenter.py` | Qt in `presenter.py` | The one Qt-free UI module becoming untestable like the rest |
| `test_the_presenter_still_does_not_import_qt` | `test_ui_never_blocks.py` | the same, from the other side | Belt and braces; both were written after it happened |
| `test_a_long_operation_starts_a_worker` | `test_ui_never_blocks.py` | I/O on the interface thread | The window freezing on a network share |
| `test_a_worker_is_called_with_arguments_it_accepts` | `test_worker_calls.py` | `CallableWorker(fn, *args)` disagreeing with `fn` | Silent failures inside workers - this caught one that had shipped |
| `test_every_control_that_cannot_label_itself_is_labelled` | `test_accessible_names.py` | Unnamed controls | A window a screen reader cannot describe |
| `test_every_plain_setting_has_a_control` | `test_settings_reachable.py` | A tunable with no control | Non-negotiable #11, which nothing else enforces |
| `test_no_tab_offers_a_command_it_cannot_honour` | `test_command_subsets.py` | An offered `/` command a tab cannot honour | Menus that promise what they do not deliver |

`test_the_load_bearing_tests_all_exist` (`test_docs_versioned.py`) reads this
table and asserts every name in it is a real test in the file named beside it.
Three of the eight rows were wrong when it was written.

**If one of these fails, it has found something.** The temptation with one thread
and no reviewer is to adjust the test. Do not. Every one of them was written
after the thing it prevents had already happened once.

The two rules worth stating in prose, because no test can express them:

1. **`app/ui/` may call the layers below it. Nothing below may import from
   `app/ui/`.** That direction is the whole architecture. A single thread can
   break it in one line without noticing.
2. **When the window needs something from the store, add it to the store.** The
   handover era produced one workaround already - `read_repo_files` walks the
   whole `files` table because a `repo_files` accessor did not exist yet - and
   that is exactly the compromise one thread no longer has to make. Fix the
   layer that should have had it.

---

## 1a. Ownership, as it now reads

One thread owns all of it. The table below is kept because it still records
**which layer a file belongs to**, which is the part that mattered. Read the
"Owner" column as "layer", not as "permission".

---

## 1. Ownership

| Path | Owner |
|---|---|
| `app/storage/`, `app/index/`, `app/extract/`, `app/search/`, `app/llm/` | **Backend** |
| `app/ui/` including `widgets/`, `presenter.py`, `theme.py` | **UI** |
| `app/main.py` | **UI** (it builds the window) |
| `app/core/` | **Backend**, except as below |
| `app/cli.py` | **Backend** |
| `assets/`, `config/extractors.toml` | **Backend** |
| `install.ps1`, `run-install.cmd`, `scripts/`, `doctor.py` | **Backend** |
| `tests/unit/test_<module>.py` | Whoever owns `<module>` |
| `tests/integration/` | **Backend**, unless the file is named `*_ui_*` |

**`app/ui/presenter.py` is UI**, despite containing no Qt. It is the UI's logic, and the
existing test forbidding it from importing Qt is what keeps it testable - not a hint that it
belongs to the backend.

## 2. Shared files, and the rule for them

These are touched by both and are where a collision will actually happen:

| File | Rule |
|---|---|
| `VERSION` | **Neither thread edits it.** The owner bumps it at release |
| `CHANGELOG.md` | Both append under `[Unreleased]`. **Append only, never reorder** - two appends merge cleanly, a reflow does not |
| `HANDOFF.md` | **Backend owns it.** UI sends its paragraph to the backend thread or to the owner |
| `docs/PROJECT_INSTRUCTIONS.md` | **Planning only.** It is the contract; the parties do not edit it |
| `app/core/settings_registry.py` | **Backend declares, UI consumes.** See §3 |
| `app/core/errors.py` | Either may add a code. **Append to `ERROR_REGISTRY`, never reorder** |
| `requirements.txt` | **Backend.** UI raises a request rather than editing |
| `pyproject.toml` | **Backend** |

Anything not listed: it belongs to a layer even so. Work out which before writing.

## 3. Contracts that were between the threads

**These outlive the split, and that is the point of them.** They were written so
two threads could not change one thing silently. With one thread the risk
inverts: nothing stops a field being renamed on both sides in the same commit,
and the tests below are what turn "I changed both sides" into "I changed both
sides *and the contract still holds*". Each is still a **file with tests on both
sides**; keep it that way.

**Settings.** `app/core/settings_registry.py` is the contract. Backend declares a `Setting`;
UI builds a control from it. Neither invents a key the other has not seen, and
`tests/unit/test_settings_registry.py` fails if they drift - in both directions.

**Search results.** `SearchResult` and `SearchResponse` in `app/search/engine.py` are the
contract. Backend owns the fields; UI owns how they are drawn. **Adding a field is a backend
change and never breaks UI**; removing or renaming one is a coordinated change and needs a
work order naming both threads.

**Errors.** `AppError` is the contract. Backend raises, UI renders. UI never parses a message
string to decide anything - that is what `code` and `action_type` are for.

**Progress.** Long operations report through Qt signals carrying plain data. Backend never
imports Qt; UI never calls a store method on the interface thread.

## 4. Work order format

Every work order names its thread in the title and the header:

```
# Work order (Backend): connection per thread
**Doc version:** 1.0 · **Updated:** ... · **Applies to:** app v0.3.3
**Thread:** Backend
```

Work spanning both is **split into two documents**, each stating the contract and which side
lands first. The dependent one says so at the top: *"Blocked on WORKORDER-backend-x.md, which
adds `ext` and `mtime_ns` to `SearchResult`."*

A single document addressed to both threads is how two people end up editing the same file at
the same time.

## 5. Before starting, and before finishing

**Before:**

```powershell
git pull                       # or: confirm the tree is clean
venv\Scripts\python.exe -m pytest tests -q
```

Never start on a red suite. If it is already failing, fixing that is the work.

**Before finishing:** run the whole suite, not just your own tests. Commit **only the files
your work order names** - `git add <path>` by name.

### 5a. `git add -A` is now allowed for a checkpoint, and only for that

This rule said *never* `git add -A`. It was written for two threads, where `-A` swept up the
other's half-finished state into your commit - worse than a merge conflict, because it looks
clean. **With one thread there is no other state to sweep up**, and on 2026-08-25 the risk it
was guarding against turned out to be much smaller than the risk it was creating.

Two `git reset` commands ran that day. The second discarded a day's uncommitted work across
eleven files - three finished, tested UI changes among them - and they survived only because a
working copy happened to exist outside the repository. The rule that would have prevented it
was the one rule everybody had a good reason to break.

**So: checkpoint before anything that touches the whole tree.** `git reset`, `git checkout
-- .`, `git stash`, or opening a second session against the same folder:

```powershell
git add -A && git commit -m "wip: checkpoint"
```

Named-file commits remain the rule for delivering work, because a commit is also a
description of what changed and `-A` describes nothing. A checkpoint is not delivering work;
it is refusing to be unrecoverable. **"Not finished" is not a reason to be unrecoverable** -
that was the actual failure, not the reset.

A rule quietly broken every day is worse than one that was changed on purpose.

## 6. When the other thread is mid-flight

Check before writing to a file you do not own outright:

```powershell
git status --short
```

Uncommitted changes in your target area mean the other thread is in it. Wait, or take a
different item from the work order. **This has already gone wrong here** - a work order was
handed over while eleven files were being edited, and the only reason nothing was lost is that
somebody looked first.

## 7. Outstanding work, split

| Work order | Thread | State |
|---|---|---|
| `WORKORDER-file-types-and-ocr.md` | Backend | **Complete** |
| `WORKORDER-scope-change-search-and-chat.md` | Backend | L8a done; L8b deferred |
| `WORKORDER-everything-tunable-has-a-ui.md` §1-3, §5 | Backend | Registry and writer **done**; constants still to demote |
| `WORKORDER-everything-tunable-has-a-ui.md` §4, §6 | **UI** | Controls, `data_path` flow - not started |
| `WORKORDER-ui-shell-and-results.md` | **UI** | Not started. **Blocked** on `SearchResult` gaining `ext` and `mtime_ns` |
| `WORKORDER-results-layout.md` | - | **Superseded** by `WORKORDER-ui-shell-and-results.md`. Deleted 2026-08-25 |
| `WORKORDER-git-search-backend.md` | Backend | Not started. Phase 1 only; phase 2 gated on a measurement, see its §14 |
| `WORKORDER-git-search-ui.md` | **UI** | Not started. **Blocked** on the backend order above - it needs `repos`, `repo:` and the `code` scope |
| `WORKORDER-inbound-ui-fixes.md` | One thread | §2 patch **applied and committed** (`4f2b92c`); §3, §4, §6 and §7 **done**; §5 partly - `mail_view` reclaimed, `search_view` still at 247. Outstanding: the Windows suite, `doctor.py`, and opening the window |
| `WORKORDER-202626081059-search-quality.md` | One thread | **Not started. Raised by the owner** - "I am not happy with what the search does". Seven verified findings; items 1-3 are half a day and fix the parsing outright |
| `WORKORDER-202626081149-code-tab.md` | One thread | §2 and §3 landed (`forget_repo`, `prune_repos`, `NO_REPO`, `repo_health`). §4-§6 outstanding. §8 parks the two-view redesign pending the owner's scenarios. **§2 needs correcting** - see the reset order below |
| `WORKORDER-202626081439-reset-leaves-nothing.md` | One thread | **Done** - `repos` and `files_fts` were surviving a reset. A1-A7 pass, zero regressions. Windows suite and `doctor.py` still to run |
| `WORKORDER-202626081801-git-sharpness-and-mail-preview.md` | One thread | §1 `/message` **built** (7 tests, zero regressions). §2 tree filtering and §3 mail preview outstanding - both need Qt |
| `WORKORDER-202626082213-install-and-distribution.md` | One thread | **DRAFT - do not start.** L9 packaging. Three decisions taken (unsigned, winget, models on install); **five marked [FINALISE] in §7** must be answered first |
| `WORKORDER-terabyte-scale.md` | One thread | **Complete** (`4780c4b`). §7's own list - scan the corpus, time a 20-30GB subtree with and without OCR, then decide whether 600GB is one run or a phased one - is measurement the owner has to run |
| `REVIEW-2026-08-25.md` | Both | See below |

The review's findings split cleanly: **P1-P14 and A1-A5 are backend**, **U1-U11 and the
accessibility findings are UI**. Its five pre-index items are all backend, and all of them are
cheaper before 100GB is indexed than after.

## 8. What the planning thread will and will not do

**Will:** write work orders and reviews, research and verify claims, audit the codebase, split
work along the boundary above, and say when something is unclear.

**Will not:** write application code, commit to `app/`, or run the implementation. Two
existing commits - `88573dd` and `07d1a13` - predate this rule; they are the settings registry
and its guard, and the backend thread owns them from here.

**Will keep doing one thing regardless:** verifying claims before writing them down. Every
version pin in this repository was checked against PyPI rather than recalled, and the review
that started this restructure found five things that were documented, believed, and never
wired up. A work order asserting something false is worse than no work order, because it gets
implemented.
