# UI thread: closing handover

**Doc version:** 2.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3
**Status:** the UI thread is closed. One thread owns everything from here.

The owner has merged Planning, Backend and UI into a single thread. This is the
UI thread's last document: what it built, what is uncommitted, what is still
open, and what it was doing that the merged thread now has to keep doing on its
own.

`docs/WORKORDER-CONVENTIONS.md` v2.0 carries the new model. **Read §0 of it
first.** The short version: the thread boundary was enforcing layering, and with
the boundary gone, seven tests are the only thing left enforcing it.

---

## 1. The four items that were outstanding — three are already done

`HANDOFF-ui-to-backend.md` (now this file, renamed) raised four. The backend thread answered them
before this merge, which is worth recording because it is the argument *for* the
merge: three of the four were an hour's work each and cost a day of document.

| | Item | State |
|---|---|---|
| **B1** | `ensure_table` wrapped its own `AppErrorException` and destroyed the message | **Fixed.** `except AppErrorException: raise` is in `vector_store.py` |
| **B2** | Vector search silently returning nothing | **Half done.** `SearchResponse.notices` exists and the CLI prints them. **The window does not draw them** — see UI-1 below |
| **B3** | `repo_files(repo_id, limit)` accessor | **Shipped.** `sqlite_store.py:1408`. The Code tab picks it up automatically — see §2 |
| **B4** | Branches, history, specific commits | **Answered, with a measurement.** `HANDOFF.md` §3 — `git log -S` costs 1.59s over 75 commits against a 300ms budget, so history is a separate cancellable job, never a mode of the search box |

**B2 is the one that matters and it is not finished.** The owner's standing rule
is in `HANDOFF.md`: *"For all things it should not fail silently it should notify
in some way."* Today the backend detects the degradation, the CLI reports it, and
the window — the only interface the owner actually uses — says nothing. The user
who reported "the git does not search through the code" was, on the evidence of
their own log, running against an index whose vector half was dead. They could
not have known.

---

## 2. What the UI thread built, and what it assumed

### The Code tab is a tree now

Repositories at the top, their indexed files as children, loaded when a row is
expanded. `app/ui/code_view.py` + `app/ui/widgets/repo_tree.py`.

**One thing to check now that B3 has landed.** `presenter.read_repo_files` was
written before `repo_files` existed:

```python
dedicated = getattr(store, "repo_files", None)
if callable(dedicated) and row.repo_id:
    return list(dedicated(row.repo_id, limit=limit + 1))
# otherwise: walk iter_files(source_kind="file") and match the root as a prefix
```

The signatures match, so the fast path should now be live. **It has not been
verified against the real store** — no PyQt6 in the environment this was written
in. Confirm the walk is no longer running, then consider whether the fallback
still earns its place. It was insurance against a boundary that no longer
exists; a single thread would simply have added the accessor.

The `limit + 1` is deliberate: it distinguishes "exactly 500" from "more than
500", which is what the truncation note depends on. Do not clamp it.

### Per-tab `/` menus

`presenter.py` holds `FILES_COMMANDS`, `MAIL_COMMANDS`, `CODE_COMMANDS`; the
search box passes no restriction and gets everything. The rule the owner stated:
**generic search is the union, focused tabs are subsets.**

`test_command_subsets.py` asserts the relationship rather than the lists — every
offered command must reach the field its tab's query function consumes. **A
command added to `app/search/commands.py` and to no tab will fail that test.**
That is intentional. It forces "which tabs can honour this?" to be answered
rather than forgotten, and with one thread there is no longer anyone else to
answer it.

### Syntax highlighting, and Office preview

`app/ui/grammars.py` (no Qt, so it is testable) plus `widgets/highlight.py`.
No Pygments — a table and a dozen regexes, no install step, nothing that can
fail at import.

Office/OpenDocument preview calls `app.extract.base.extract` — **the index's own
extractor, deliberately**. It means preview coverage cannot drift from index
coverage: a type added through the file-types UI becomes previewable at the
moment it becomes searchable. It also means **anything that changes an
extractor's output changes what the preview pane shows.** That coupling is
intended, but it is now a coupling one person owns both ends of, so it is worth
knowing it is there.

Tier 2 converters are excluded on purpose: shelling out to LibreOffice is fine
overnight, not on a down-arrow keypress.

---

## 3. Still open on the UI side

In the order the UI thread would have done them.

### UI-1 — draw `SearchResponse.notices` in the window · **the important one**

Backend emits, CLI prints, window ignores. Everything needed is already there:

```python
@dataclass(frozen=True)
class Notice:
    code: str        # branch on this
    message: str     # already worded for a human
```

Two codes exist today: unmatched terms, and keyword-hits-with-no-vector-hits.

What it needs: a line above the results showing `notice.message`, in the warn
colour rather than the faint one. `theme.py` already has both — `#resultsSummary`
is `text_faint`, and `#resultMissing` and `#statWarn` are `warning`. Use a warn
objectName, not the summary's: `#statWarn` exists precisely because a figure the
code had decided was worth warning about was rendering identically to one that
was fine. That is the same mistake this task is here to avoid making again.

**Never parse `message`** — that is what `code` is for, and `app/ui/` has a test
asserting the UI does not parse error strings.

Where: `search_view.py` is at 249 of 250 code lines, so this needs an extraction
first. `widgets/notice_bar.py` is the obvious shape, and Files and Mail will want
it too — which is the consistency rule, below.

### UI-2 — the preview pane on Files and Mail

The owner's standing rule: *"any feature added which helps the other search
areas has to be applied to others for consistency."* Preview is on Search only.
`FileRow.path` was already added for this. Both views sit at 240 and 244 lines,
so both need an extraction first.

### UI-3 — verify the Qt tests actually pass

**Read this before trusting anything above.** There is no PyQt6 in the
environment the UI thread worked in and it could not be installed. Every Qt test
skipped. Qt code was verified by `py_compile` and by reasoning, which is exactly
how `QPdfView()` — missing its parent argument — crashed the window on startup
after passing review.

Roughly forty tests written today have never executed:

```powershell
venv\Scripts\python.exe -m pytest tests\unit\test_code_view.py tests\unit\test_command_subsets.py tests\unit\test_repo_tree_rows.py tests\unit\test_grammars.py tests\unit\test_preview_extracted.py -q
```

**If the merged thread has a working PyQt6, this is the single highest-value
thing it inherits.** The UI has been shipping on inference for a week.

---

## 4. Uncommitted, right now

The working tree holds **both threads' work**, unseparated. Nothing here has
been committed by either.

Backend, uncommitted: `app/cli.py`, `app/core/errors.py`,
`app/core/settings_registry.py`, `tests/unit/test_ui_responsiveness.py`,
`tests/unit/test_view_options.py`.

UI, uncommitted: `app/ui/preview_loader.py`, `app/ui/widgets/command_popup.py`,
`app/ui/widgets/preview.py`, and new — `app/ui/grammars.py`,
`app/ui/widgets/highlight.py`, `app/ui/widgets/repo_tree.py`,
`tests/unit/test_grammars.py`, `tests/unit/test_preview_extracted.py`,
`tests/unit/test_command_subsets.py`, `tests/unit/test_repo_tree_rows.py`.

Also untracked and older than today — several test files and work orders that a
previous commit instruction never ran: `test_accessible_names.py`,
`test_index_flows.py`, `test_preview_loader.py`, `test_preview_pane.py`,
`test_sanitise.py`, `test_settings_reachable.py`, `test_worker_calls.py`,
`docs/WORKORDER-*.md`. **These are guard tests. Untracked, they protect
nothing** — the next clone runs a suite that is missing exactly the tests written
to stop the bugs that have already happened.

`GitSearch.txt` is untracked and unexplained; check before including it.

Suite as of this document: **2271 passed, 32 skipped, 3 failed.** All three
failures are `test_cli_index.py` on `fastembed is not importable` — a sandbox
missing the package, not a defect. They should pass on the owner's machine, and
**if they do not, that is B2's root cause showing itself again.**

---

## 5. What the UI thread would say if it could say one thing

The window is where every fault in this project was found — eleven of them, all
by somebody clicking, none by a test. That is in `HANDOFF.md` as an observation.
It is really an instruction.

The merged thread now owns the layer that produces the evidence *and* the layer
that hides it. The failure mode to watch for is not a collision any more; it is a
degradation reported in a log, detected correctly, described precisely, and never
shown to the one person using the application. That is B2, and it is still open.
