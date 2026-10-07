# Work order: three finished UI fixes, and four things found while making them

**Doc version:** 1.2 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

**Thread:** the single merged thread (`WORKORDER-CONVENTIONS.md` v2.0, §0)

Three changes were asked for, built and tested. They are **not in the repository** - they
were lost to a `git reset` before they could be committed, and they now exist only as a patch
outside it. §2 is how to take delivery. §3 to §6 are findings from doing the work, each of
which outlives the patch.

**Do not start this while anything else is writing to the tree.** §7 is why.

---

## 1. Why this document exists rather than a commit

At 23:44 on 2026-08-25 a `git reset` discarded every uncommitted change in the working tree.
The reflog records it:

```
1920218 HEAD@{0}: reset: moving to HEAD
```

It was the second reset that day - `59afcf1 HEAD@{4}` is the first. The three changes below
were complete and passing at the time; so was a substantial amount of other uncommitted work
in `presenter.py`, `shell.py`, `pipeline.py` and `indexing_settings.py`, which went with them.

The changes were reconstructed from a working copy and are saved as a patch. Everything in
§2 has been run; nothing in §2 has been run **on Windows**, against Qt, or through
`doctor.py`, and that is the gap this hands over.

## 2. Take delivery of the patch

```
outputs\git-search-changes\my-changes.patch
outputs\git-search-changes\*.py          # the ten complete files, if the patch rejects
```

```powershell
git checkout -b fix/columns-tabs-typemenu
git apply --check outputs\git-search-changes\my-changes.patch    # dry run FIRST
```

**Expect `commands.py` and `presenter.py` to reject.** Both have moved since the patch was
built - `3b29b7f` and `aeeafcb` landed during it. The three changes are independent and can be
applied one at a time in any order; the full files are beside the patch for hand-merging.

### 2.1 Every table column is capped - `app/ui/view_options.py`

The complaint: *"the Files tab, the name column is too big and hard to manage"*, and *"one of
them is stuck"*.

`MAX_COLUMN_SHARE = 0.40` with a `MIN_COLUMN_CAP_PX = 140` floor. In `view_options.py` rather
than in a view, so every table and every tree inherits it - the request was explicitly
application-wide.

Three details worth keeping when merging:

* **Capped in two places.** `_apply_widths` caps after restoring saved widths, which is what
  unsticks a column already saved too wide; `remember_widths.record` caps on the way in, so a
  new drag cannot create one. Capping in only the first place leaves the stored preference and
  the visible table permanently disagreeing, and a "Reset widths" that appears to do nothing.
* **`_cap_columns` never sets `APPLYING` itself.** It must be called with the flag already
  set. `setColumnWidth` emits `sectionResized`, which `remember_widths` listens to - the
  recursion note already in `_apply_widths` describes the process death that follows.
* **The last visible column is exempt**, because `setStretchLastSection` owns it, and a table
  with one visible column is exempt, because 60% of it would be permanently blank.

`column_cap()` is pure and has four tests. It returns 0 for a widget that is not laid out yet,
which means "do not cap" - a widget reports width 0 before it is shown, and capping against
that pins every column to the floor before the window appears.

### 2.2 The tab bar reads as tabs - `app/ui/theme.py`

The request: *"make all of those options on the top a tabbed look"*, clarified as *"the
titles, search, files, code.. etc"*.

Was an underline - transparent tabs, accent bar beneath the selected one. Now bordered tabs
with rounded tops, unselected sitting 3px lower, and the pane pulled up by `top: -1px` so the
selected tab's bottom edge and the page's top edge are the same line, painted in the page
colour.

**The accent moved to the top edge rather than being dropped.** Shape, background and accent
all change on selection, so the state is not carried by hue alone - the same reasoning as
`#statWarn`. There is a `QTabBar::tab:focus` rule as well, because Ctrl+Tab moves between
these and selection colour alone does not say where the focus went.

**One trap, and it cost a startup crash before it was caught:** the template goes through
`str.format`, so *every* brace in it must be doubled - including braces inside CSS comments. A
single `{` in prose is a `KeyError` when the window is built, not a styling problem. There is
now a test asserting no unsubstituted tokens survive in either palette.

### 2.3 `/type` offers everything, not just what is indexed

The complaint: *"the file type drop down is not dynamic, i.e. not all files configured are in
the list"*. Three separate causes, all fixed.

| | Cause | Fix |
|---|---|---|
| a | `distinct_values` reads `files.ext`, so a format switched on in the file-types editor is invisible until something is indexed | A third source: every enabled format, from `FormatRules.describe(REGISTRY)` - the same answer `app.cli formats` prints |
| b | The kind words `_EXT_GROUPS` expands - `excel`, `mail`, `code`, `word` - have parsed since Layer 4 and were **never once offered**, because `/type` carries a `source` and `files.ext` has no row saying "excel" | `Command.values` on `/type`, with a test asserting it matches `_EXT_GROUPS` in both directions |
| c | One `VALUE_LIMIT = 40` for every source | Per-source ceilings |

Ordering is the feature and is deliberate: **index first** (frequency - the type somebody
wants is nearly always one of the three they have thousands of), then the kind words, then
enabled-but-not-indexed. That reverses the previous "fixed values first" rule for any command
carrying a `source`; `test_fixed_values_come_before_the_index_s` was renamed and its docstring
now records why the old rule did not survive.

Note (b) is the shape of failure this codebase keeps finding: built, tested, shipped, and
delivering nothing because it was invisible. The `value_hint` even *named* two of the kind
words, so the menu was advertising values it would not complete.

**`clear_format_catalogue()` is called when the file-types editor saves.** The catalogue is
cached because it parses two TOML files and is reached from a keystroke. Without the
invalidation, a format switched on stays missing from `/type` until the window restarts -
which is the same "the setting did not work" the editor's own status line exists to prevent.

**The catalogue is gated on there being a store, and that is not about the store.** The menu
is built twice: instantly on the interface thread with no store, then on a worker. Reading
TOML is I/O and the interface thread does not do I/O, so the catalogue rides with the pass
that is already on a worker.

### 2.4 What was verified, and what was not

| Verified | Not verified |
|---|---|
| 250 tests pass across the files touched | Anything requiring PyQt6 |
| Zero regressions - failure sets byte-identical against a reconstructed baseline | `doctor.py` |
| Zero new lint under the project's ruff config | The window actually opening |
| Both palettes format cleanly | That the tabs and the capped columns *look* right |

The last one is the one that matters, and `HANDOFF.md` says why: every UI fault in this
project was found by somebody clicking, never by a test. **Open the window.** Sort every
column, drag one wide, restart, switch the system theme with the tab bar visible, and resize
to the 720x480 minimum.

## 3. The `ext` ceiling is now the wrong number

`VALUE_LIMITS = {"ext": 120}` in the patch was sized against a world of 34 text extensions.

`3b29b7f` took that to **405** (`app/extract/source_types.py`), which lands after the patch was
written and makes 120 arbitrary. Worse, it inverts the ordering §2.3 was built around: with 405
enabled formats and a corpus holding perhaps 40, the "enabled but not indexed" tail dwarfs the
indexed head, and the menu fills with types the machine does not have.

**Do not simply raise it.** The fix is two ceilings rather than one - a generous limit on what
is actually indexed, and a much tighter one on the configured tail, so the tail stays a
reachable answer to "did my setting work" without becoming the menu. A prefix of two or more
characters is the case where showing more of the tail is right.

This is properly this thread's call because this thread made the change that invalidated the
constant, and because 405 was a deliberate answer to a real request.

## 4. `/type` cannot name a file that has no extension

`distinct_values` filters `WHERE ext <> ''` (`sqlite_store.py:1583`).

That was harmless when nothing extensionless was indexed. `NAMED_FILES` now exists in
`source_types.py` and `plaintext.py:116` matches on it, so `Dockerfile`, `Makefile` and their
kind **are indexed and cannot be filtered for at all** - not offered in the menu, and no value
that would match them if typed.

The gap was created by fixing something else, which is the usual way. Options, in order of
how much they change: store a synthetic `ext` for named files; or add a `named` source that
reads basenames; or give `/type` a `makefile`-style group in `_EXT_GROUPS` that expands to the
names. The third is cheapest and reuses the mechanism §2.3 just made visible.

## 5. Four views are within eight lines of a load-bearing cap

`test_every_qt_view_keeps_its_logic_in_the_presenter` caps a view at 250 code lines. Measured
now:

| View | Lines | Headroom |
|---|---|---|
| `mail_view.py` | 249 | **1** |
| `search_view.py` | 247 | 3 |
| `code_view.py` | 244 | 6 |
| `files_view.py` | 242 | 8 |
| `settings_view.py` | 235 | 15 |
| `indexing_settings.py` | 228 | 22 |

§0 of the conventions is right that this test is load-bearing and right that the temptation
will be to adjust it. But a rule with one line of headroom is a rule about to be broken by the
next feature, and at that moment the pressure to raise the number will be highest and the
reasoning worst.

Better to spend the headroom deliberately now, while nothing is blocked: move formatting out
of the two tightest views into `presenter.py`. Worth noting honestly that `presenter.py` is
approaching 2,400 lines and is becoming the place everything goes to avoid a cap elsewhere -
splitting it by view is the version of this that does not just relocate the problem.

Not urgent. It is on this list because it is invisible until it stops work, and cheap now.

## 6. Two documentation corrections

* `WORKORDER-CONVENTIONS.md` §0 names `test_the_presenter_still_imports_no_qt`. No test has
  that name. There are two, spelled differently: `test_the_presenter_never_imports_qt`
  (`tests/unit/test_presenter.py:308`) and `test_the_presenter_still_does_not_import_qt`
  (`tests/unit/test_ui_never_blocks.py:248`). A table of load-bearing tests should name them
  exactly, or the day one is deleted the table will not notice.
* Two of those seven guards live in files whose names do not suggest them. Worth a line saying
  where each is, for the same reason.

## 7. The reset, and the one rule that follows

Two resets in one day, the second of which destroyed a day's uncommitted work across eleven
files. The single-thread model removed the collision risk the conventions were written
for - it did not remove this one, and arguably made it worse, because there is no longer a
second party whose dirty files are a reason to stop and look.

**Checkpoint before anything that touches the whole tree.** `git reset`, `git checkout -- .`,
`git stash`, or starting a second session against the same folder:

```powershell
git add -A && git commit -m "wip: checkpoint"
```

`git add -A` is exactly what §5 of the conventions forbids, and that prohibition was written
to stop one thread sweeping up another's half-finished state. With one thread there is no
other state to sweep up, and the risk it was guarding against is now smaller than the risk of
losing everything. **Update §5 to say so**, rather than leaving a rule everybody has a good
reason to ignore - a rule quietly broken every day is worse than one that was changed on
purpose.

Not committing was the actual failure here. The work in §2 existed for two hours without a
commit because it was not finished, and "not finished" is not a reason to be unrecoverable.

## 8. Definition of done

1. The patch is applied, or its three changes are hand-merged, and the whole suite passes on
   Windows - not the non-Qt subset.
2. `doctor.py` prints READY.
3. **The window has been opened and used** - §2.4.
4. §3 is decided with a number and a reason, not left at 120.
5. §4 is either fixed or written into `HANDOFF.md` as a known gap, so it is not rediscovered.
6. `CHANGELOG.md` appended under `[Unreleased]`. Append only.
7. `WORKORDER-CONVENTIONS.md` §7 gains a row for this document, and §5 is updated per §7 above.
