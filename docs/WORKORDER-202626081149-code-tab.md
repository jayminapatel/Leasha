# Work order: the Code tab, and repository detection that cannot be undone

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 11:49 · **Layer:** L1/L3/L5 - `app/storage/sqlite_store.py`, `app/index/`, the Code tab

**Thread:** the single merged thread

**Raised by the owner** from the window: *"in git view i dont see the files, that view is just
a view by git and the search should deliver the results in the list, it does not."*

Investigating that produced a data-integrity finding rather than a UI one. §1 is what actually
happened on this machine; §2 is why the application could not recover from it; §3 to §6 are the
faults that made it invisible. The two-view redesign the owner then described is **parked** and
recorded in §8 so the thinking is not lost.

---

## 1. What was wrong on the owner's machine

`app.cli repos`:

```
NAME           KIND    FILES  ROOT
SearchData     work    1,179  D:\SearchData          <- the document archive
DF_IECEvent    work       11  D:\SearchData\GIT_REPOS\DF_IECEvent
QA23_GitRepo   work        0  D:\SearchData\GIT_REPOS\QA23_GitRepo
SearchProject  work        0  D:\SearchProject
```

`D:\SearchData` is the owner's document archive - PDFs, PSTs, Visio, Office. It held a `.git`
directory created 2026-08-25 15:30, and `git log` inside it showed **this project's own
commits** on `layer/2-extraction`, with every tracked file reporting as deleted. A copy of
`D:\SearchProject\.git` had been dragged into the data folder and its working tree emptied.

Consequences, all three of which the owner met:

* **1,179 of 2,677 indexed files - 44% of the corpus - were attributed to a repository.**
* `scope:code` is `repo_id IS NOT NULL`, so "Code only" matched the whole archive.
* Two of the four repositories held **zero** files, so selecting either produced an empty list
  with no explanation. That is the reported symptom.

**Verified before advising deletion:** every commit in the stray `.git` exists in
`D:\SearchProject`, and its HEAD is an ancestor of the real HEAD. Nothing was unique. The owner
is resetting and rebuilding overnight with it removed.

**That fixes this machine and changes nothing about the application.** The same accident on any
other machine produces the same result, with the same lack of recovery.

## 2. The finding: attribution is a one-way door

Once a file is attributed to a repository, **nothing in this application can undo it**. Three
independent mechanisms each prevent recovery, and all three must be fixed:

| # | Mechanism | Effect |
|---|---|---|
| 1 | Nothing prunes `repos` | A repository whose `.git` has gone keeps its row for ever |
| 2 | Nothing ever sets `files.repo_id` back to NULL | Attribution survives every subsequent run |
| 3 | `repo_id = COALESCE(excluded.repo_id, files.repo_id)` (`sqlite_store.py:456`) | A NULL **cannot** overwrite an attribution - so even `index --force` will not clear it |

Mechanism 3 is defensible alone; its comment explains it stops callers that know nothing about
repositories (`_record_skip`, the PST path) from blanking a good attribution. Combined with the
other two it becomes a trap: **the only route back is deleting the whole index.** That is what
the owner is doing, for what is a bookkeeping error.

`app.cli repos` accepts only `--scan`. There is no command, flag or control anywhere that
disowns a repository.

**This is mine.** `WORKORDER-git-search-backend.md` §4.4 said repositories are *"found, not
registered - no add, no remove"*, and §11 of the UI order repeated it. That was right about
finding them and wrong about disowning one, and it should be corrected in both documents rather
than left to contradict this one.

### What to build

1. **`app.cli repos --forget <root>`**, and an "Ignore this repository" action on the row.
   Clears `repo_id` for its files, deletes the row, bumps the index generation so the search
   cache cannot serve results built while those files were code. Persist the root in an ignore
   list so the next walk does not re-adopt it.
2. **Detection prunes.** A repository whose root no longer holds a `.git` is removed on the next
   walk that covers it, exactly as deleted files are pruned.
3. **An explicit way to mean "no repository".** Keep the COALESCE default; add a sentinel the
   indexer can pass that clears the column deliberately. A defensive guard that cannot be
   overridden is not a guard, it is a one-way door.

## 3. Detection should be suspicious of an archive

`D:\SearchData` had 1,179 files, almost none with a code extension, and a working tree where
every tracked file read as deleted. All three are strong signals that this is not a checkout
somebody works in.

Detection adopted it silently, and `app.cli repos` has been printing a warning about the
consequence - *"`scope:code` will match your whole corpus rather than just code"* - since
repositories were added. **The application knew, said so in a place nobody was reading, and
adopted it anyway.**

Flag it at detection, once, where it will be seen: a repository whose indexed files are
overwhelmingly non-code, or whose `git status` reports its whole tree deleted, should be
reported rather than silently adopted. Adopt-and-warn is fine; silent adoption is not.

## 4. The Code tab hides most of a repository, silently

`DEFAULT_PRESET = "build"` (`app/core/code_types.py:110`). Resolved:

| | `build` (default) | `docs` |
|---|---|---|
| `.py` `.cs` `.sql` `.ps1` | yes | yes |
| `.md` `.txt` `.json` `.yml` `.csv` | **no** | yes |

`README.md`, `package.json`, `requirements.txt` and every `.yml` are filtered out of the list on
a fresh install. The grouping is deliberate and defensible - a README is arguably code and a
200MB CSV export plainly is not, and they are the same group.

**The fault is that nothing on screen says a filter is active.** `code_type_filter`'s own
docstring warns that *"a Code tab that has silently hidden a language is far harder to notice
than one showing a stray PDF"*, and then the default does exactly that for documentation and
config. Combined with §1 it is the second reason the owner saw no files.

Fix: the summary line names the preset and the arithmetic whenever anything is hidden -
`Showing 12 of 340 - Source, config and build files` - with the preset a click away.

## 5. An empty list must say which kind of empty it is

Three states currently render identically, as nothing:

| State | What it should say |
|---|---|
| The repository has no indexed files | "No indexed files. Its folder may not be under an indexed root." |
| It has files, all hidden by the type filter | "0 of 340 shown - hidden by your code-type filter." |
| It has matching files, but the query excludes them | "No file matches that query in this repository." |

`repo_empty_state` (`presenter.py:2073`) already does this well for *no repositories at all* -
two questions, two answers, and a docstring explaining why a generic "no results" would waste
the one that matters. The same care has not been applied one level down.

## 6. `scope:code` means "in a repository", not "is code"

Defined as `repo_id IS NOT NULL`. On this machine that was 1,190 files that were mostly not
code, and no amount of ranking would have made "Code only" behave.

The definition was a deliberate choice - `WORKORDER-git-search-backend.md` §6 argues for it, and
`type:code` remains the extension filter, two different questions both available. **That
argument still holds**, and §2 to §4 are the reason it stopped being true in practice rather
than a reason to change it. Fix those first, then re-read the scope chip's tooltip and confirm
it still describes what happens.

## 7. Acceptance

| | Criterion |
|---|---|
| A1 | `repos --forget <root>` clears attribution, prunes the row, bumps the generation, and the root is not re-adopted on the next index run |
| A2 | Deleting a `.git` and re-indexing removes that repository without a reset |
| A3 | A repository whose indexed files are overwhelmingly non-code is reported at detection, once, visibly |
| A4 | With the default preset, a repository containing `README.md` and `main.py` shows one file **and says so**, naming the preset and the count hidden |
| A5 | Each of the three empty states in §5 produces its own sentence |
| A6 | `scope:code` over a corpus with one real repository returns that repository's files and nothing else |
| A7 | Regression for §1: a directory holding a `.git` whose whole tree reads as deleted does not silently attribute 1,000 files |

A7 is the one worth writing first. It is the whole incident, reduced to a fixture.

## 8. Parked: the Code tab as two views on one result set

**Deferred at the owner's request**, pending real scenarios. Recorded so it is not re-derived.

The owner's description: *"think of the git page as just a view on the same data - when a file
is selected on the right it should navigate to the git automatically, and vice versa; if a git
is selected it should only show files from that git which match the criteria. One is a simple
list view, the other a view by git tree."*

The design conclusion reached before parking, which should survive:

> **The search box is the single source of truth; the tree is a view of it, not a second
> input.** Selecting a repository in the tree writes `/repo <name>` into the box and the normal
> re-query happens. No hidden state, no conflict between a typed switch and a selection, and it
> matches the Files tab's existing `search_inside_requested` bridge.

And the distinction everything turns on:

> **Reveal is not scope.** Clicking a *tree node* re-scopes and re-queries. Clicking a *file
> row* only reveals that file's repository in the tree - it must not re-scope, or arrowing down
> a mixed result set would filter away the results being scanned, one keypress at a time.

Three constraints already identified:

* **Guard the feedback loop.** Tree changes list changes selection changes tree. The `APPLYING`
  property in `view_options.py` is the precedent and exists because a similar re-entry killed
  the process.
* **Reveal may never trigger a fetch.** A branch node costs `git ls-tree`, a subprocess.
  Revealing may navigate only to nodes already loaded - repositories, which are free.
* **History rows have no tree position.** A `git log -S` hit can be a file deleted years ago.

Open questions for the owner, unchanged: tree depth; what a file click does to the tree;
whether a tree click writes into the box; whether "all repositories" is a state to sit in;
branch listings instant-but-checked-out versus accurate-but-seconds; what the tree does for a
history hit; the sentence when the filter hides rows; and whether selection survives a tab
switch.

## 9. Definition of done

1. A1-A7 pass; the whole suite passes on Windows.
2. `WORKORDER-git-search-backend.md` §4.4 and `WORKORDER-git-search-ui.md` §11 corrected - they
   currently assert repositories are never removed, which §2 shows was wrong.
3. `app.cli repos` re-run on the owner's machine after the overnight rebuild, and the output
   pasted into `HANDOFF.md` as the record that §1 is closed.
4. `CHANGELOG.md` appended under `[Unreleased]`. Append only.
5. `WORKORDER-CONVENTIONS.md` §7 gains a row.
