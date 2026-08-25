# Work order (UI): the Code tab

**Doc version:** 1.1 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3

**Thread:** UI

**Blocked on `WORKORDER-git-search-backend.md`**, which adds the `repos` table,
`files.repo_id`, `SqliteStore.repos_list()`, the `repo:` filter and the `code` value for
`ParsedQuery.scope`. None of this can be built or tested before those land. Confirm with
`venv\Scripts\python.exe -m app.cli repos` - if that command exists and prints, the backend
half is in.

**Commit the working tree before starting**, and check `git status --short`. This order
touches `app/ui/` and `app/main.py` only, plus appends to `CHANGELOG.md`.

---

## 1. Why, and the shape of the answer

The request was for a git search tab. The backend order (§1) records why most of
`GitSearch.txt` is refused and establishes the finding that shapes this one:

**Source code is already indexed and already searchable**, by keyword and by meaning, with
snippets and reranking. `TEXT_EXTENSIONS` (`app/extract/plaintext.py:34`) covers twenty source
extensions today, alongside the config and markup formats. What the application could not do
was tell you *which repository a result came from*, or let you ask for one.

That makes the tab a different thing from what the specification imagined. It is **not a
second search implementation**. It is a repository browser that hands its selection to the
search box you already have.

### The rule this tab is built on

`mail_view.py:19` states it, and it is the reason that tab worked out:

> **The same `/` commands as everywhere else.** `/from`, `/to`, `/subject`, `/has`, `/after`,
> `/before` all parse through `app/search/query.py`, so a filter learned in the search box
> works here with the same spelling. Nothing new was invented for this tab, which is the point.

`GitSearch.txt` proposed fifty-six command-line switches. Not one of them appears in this
tab. The backend order adds exactly one filter - `repo:` - to the single grammar in
`app/search/commands.py`, and the `/` dropdown, the search box, the Files tab, the Mail tab
and the Ollama prompt all gain it at once because they all read that one list.

**If you find yourself building a control that has no equivalent in the search grammar, stop.**
That is the sixty-switch design arriving through the side door, and it ends with a tab whose
filters do not work anywhere else in the application.

## 2. What to build

Three things, in this order. The first is one line and delivers most of the value.

| # | Thing | Size |
|---|---|---|
| 1 | "Code only" in the scope chips on the existing Search tab | One tuple entry |
| 2 | `app/ui/code_view.py` - the repository browser | ~200 lines, `FilesView` shaped |
| 3 | The bridge: pick a repository, search inside it | Reuses `_search_inside`'s pattern |

`test_every_qt_view_keeps_its_logic_in_the_presenter` globs `app/ui/*_view.py` and caps a view
at 250 code lines, so `code_view.py` is in its scope from the moment it is created. The ~200
estimate clears it, but not by much - if the file starts growing past that, the overflow is
formatting logic that belongs in `presenter.py`, which is exactly what the cap is for.

## 3. The scope chip

`SCOPES` (`app/ui/widgets/search_bar.py:55`) gains a fourth entry:

```python
SCOPES: tuple[tuple[str, str], ...] = (
    ("Everything", "all"),
    ("Mail only", "mail"),
    ("Documents only", "documents"),
    ("Code only", "code"),
)
```

Update the tooltip at line 67, which currently says "Narrow the search to mail or to files on
disk" and would now be wrong.

**"Code only" means "in a repository", not "has a code extension".** The backend defines it
as `files.repo_id IS NOT NULL`. Say so in the tooltip in plain words - a `.md` file inside a
repository is code-scoped and a `.py` file in Downloads is not, and nobody will guess that.
`type:code` still filters by extension and is unchanged; the two questions stay separate and
both stay available.

The comment above `SCOPES` - *"A filter, not a mode: nobody should have to decide whether a
thing was an email or a document before typing"* - applies unchanged. Do not make the Code
tab set the scope permanently or hide the chip.

`SearchView` gains one method so the bridge in §5 can drive it:

```python
def set_scope(self, value: str) -> None:
    """Select a scope by its value. Unknown values are ignored."""
```

Find the index with `findData(value)` and ignore `-1`. Ignoring rather than raising because
this is called across a tab boundary with a string, and a stale caller must not be able to
crash the window.

## 4. `app/ui/code_view.py`

Thin, like every view here: formatting in `presenter.py`, queries in `sqlite_store.py`, the
menu in `widgets/file_menu.py`. Model it on `files_view.py`, which is the closest match - a
table, a summary label, a view-options button, and a bridge signal out.

### Module docstring

Open it the way `files_view.py` and `mail_view.py` open theirs: with why this is a tab rather
than a filter. The honest answer, and worth writing down because it is the thing that stops
this tab growing:

> The search box answers "which file says this". This answers "what have I got, and how much
> of it is indexed" - a question about the machine, not about a query, asked before you know
> what you are looking for. It is a browser and it says so: it cannot find code by what the
> code says, and it hands that job back to the search tab rather than reimplementing it.

Name the layer: `Layer: L5, driving L1`.

### Contents

**One table**, columns in the order somebody scans them:

| key | Heading | From `RepoRow` | Right-aligned |
|---|---|---|---|
| `name` | Repository | `name` | no |
| `files` | Indexed files | `files` | yes |
| `kind` | Kind | `kind` | no |
| `seen` | Last seen | `seen` | yes |
| `path` | Location | `path` | no |

`ALWAYS_OFFERED = ("name",)`, `PREFS_KEY = "ui:code"`. Sorted by indexed file count descending
from the store, so the repository somebody actually works in is at the top rather than
whichever happened to be walked first.

**Sorting on:** unlike Files and Search, these rows are not ranked by match quality - the
order is arbitrary and the columns are all comparable, so `setSortingEnabled(True)` and use
`SortableItem` with `SORT_ROLE` as `mail_view.py` does. File counts must sort numerically;
that is exactly what `SortableItem` is for.

**A summary label** above the table: `"12 repositories, 48,301 files indexed"`. Use
`format_count` from the presenter, which already exists.

**No search input on this tab.** The temptation is a filter box; resist it until somebody with
forty repositories asks for one. Twelve rows do not need filtering, and an empty search box on
a tab called Code will be read as "search my code here" - which is the search tab's job and
the bridge's whole purpose.

**No repository management.** No add, no remove, no browse-for-folder, no per-repository
settings. Repositories are found by the walk; they are not registered. If a repository is
missing from this list the answer is that its folder is not an indexed root, and the tab
should say that in an empty state rather than offering a dialog.

### Loading

`repos_list()` on a worker via `CallableWorker`, never on the UI thread - non-negotiable 5.
Copy `FilesView.refresh_summary` (`app/ui/files_view.py:~180`) exactly, including
`worker.signals.failed.connect(lambda _e: None)` if the failure only costs a label. For the
table itself, route failures to the `error` signal.

Refresh when the tab comes forward, not on a timer. `_tab_changed` in `shell.py:775` already
does this for Indexing and Files; add a branch for this view.

### Empty states

Three, and they are different questions with different answers:

| Situation | Message |
|---|---|
| Nothing indexed at all | "Nothing is indexed yet." + the existing route to the Indexing tab |
| Indexed, no repositories found | "No code repositories found under your indexed folders." Then say that a repository is any folder containing `.git`, and that adding its parent folder as an indexed root is what makes it appear |
| Repositories found, none selected | Normal table, nothing special |

The second is the one that matters and the one a generic "No results" would waste.

## 5. The bridge

`FilesView` already solved this and the pattern is proven - copy it rather than inventing a
second mechanism.

`CodeView` declares:

```python
#: A repository to search the contents of. The bridge between the browser and
#: the search box: found the repository, now find what is in it.
search_repo_requested = pyqtSignal(str)
```

Emitted on double-click, on Enter with a row selected, and from a "Search this repository"
item in the right-click menu. All three, because `files_view.py` binds all three and a
keyboard user should never have to reach for the mouse to act on a selected row.

In `shell.py`, beside `_search_inside` (`app/ui/shell.py:960`):

```python
def _search_repo(self, name: str) -> None:
    """Found the repository; now find what is in it."""
    if not name:
        return
    self._show(self.search_view)
    self.search_view.set_scope("code")
    self.search_view.input.setText(f'repo:"{name}" ')
    self.search_view.input.setFocus()
    self.statusBar().showMessage(
        f"Searching {name} - type what you are looking for.", 8_000)
```

Quote the name. Repository folder names contain spaces more often than you would like, and an
unquoted value ends at the first one - the filter would silently match a different repository,
or none, with nothing on screen to explain it.

Use `self._show(view)`, never `tabs.setCurrentWidget(view)`. The docstring at `shell.py:~763`
records why: for a view inside a scroll area the latter silently does nothing.

**The search box keeps the filter visible and editable.** Do not apply the repository as
hidden state. Non-negotiable: a translated or applied query is shown and editable, because
invisible narrowing makes search unpredictable. The person can see `repo:"leasha"`, delete it,
or type another - and that is the feature, not a leak.

## 6. Wiring in `shell.py`

Construct beside the others (`shell.py:232-241`):

```python
self.code_view = CodeView(store)
self.code_view.error.connect(self._show_error)
self.code_view.search_repo_requested.connect(self._search_repo)
```

Add to the tab tuple at `shell.py:255`, **after Mail and before Indexing**:

```python
(self.code_view, "Code", False),
```

`scroll=False` - it is a table that fills the window and scrolls its own contents, and the
comment above that tuple explains what nesting a second scroll area does.

Placement is deliberate: Search, Files, Mail, Code, Indexing, Settings keeps the four
"find something" tabs together and leaves the two "manage the app" tabs at the end.

Add a branch to `_tab_changed` (`shell.py:775`) to refresh on the way in. Register the view in
`_tab_index` through the loop, which happens automatically - do not add a separate entry, and
do not compare `tabs.widget(i) is self.code_view`, for the reason the map exists.

## 7. Presenter

All formatting goes in `app/ui/presenter.py`, which is UI-owned despite containing no Qt, and
must keep importing no Qt - `test_the_presenter_never_imports_qt`
(`tests/unit/test_presenter.py:308`) enforces it, and
`test_every_qt_view_keeps_its_logic_in_the_presenter` (line 333) will read `code_view.py` the
moment it exists.

```python
@dataclass(frozen=True, slots=True)
class RepoRow:
    name: str
    files: str        # formatted, e.g. "48,301"
    kind: str         # "Repository" | "Submodule" | "Worktree"
    seen: str         # relative, e.g. "2 hours ago"
    path: str         # shortened for display
    root: str         # full path, for the menu and the tooltip

def repo_rows(rows, *, now=None) -> list[RepoRow]: ...

def repo_summary(repos: int, files: int) -> str: ...
```

Reuse what exists rather than writing new formatters: `format_count` (line 292),
`format_when` (line 634) and `shorten_path` (line 265) already do three of the five columns.
`kind_tag` (line 758) is the model for turning the backend's `work`/`submodule`/`worktree`
into words a person reads.

`now` is a parameter for the same reason `format_when` takes one: a relative time is untestable
otherwise.

## 8. Settings

**None.** The backend adds no setting for this and neither does the UI. Detection is free and
changes nothing about what gets indexed, so per non-negotiable 11 there is nothing to tune. A
control here would fail `test_every_control_writes_its_setting_somewhere` anyway, having
nothing to write.

## 9. Acceptance tests

`tests/unit/test_code_view.py` and `tests/integration/test_layer5_ui_code.py` - note the
`_ui_` in the integration name, which is what puts it on this side of the ownership line.

| # | Criterion |
|---|---|
| U1 | The Code tab is present, titled "Code", and sits between Mail and Indexing |
| U2 | `repos_list` is called on a worker; the UI thread performs no database read. Assert as the existing view tests do |
| U3 | Double-click, Enter, and the context-menu item each emit `search_repo_requested` with the repository name |
| U4 | `_search_repo` brings the Search tab forward, sets the scope combo to `code`, and puts `repo:"<name>" ` in the input |
| U5 | A repository name containing a space arrives quoted, and `parse_query` returns it as one value |
| U6 | With no repositories, the empty state names `.git` and indexed roots. With nothing indexed at all, it routes to Indexing |
| U7 | Sorting by "Indexed files" sorts numerically, not lexically - 9 below 10 |
| U8 | Column, density and text-size preferences persist under `ui:code` and survive a restart |
| U9 | `set_scope("code")` selects it; `set_scope("nonsense")` is a no-op and does not raise |
| U10 | "Code only" appears in `SCOPES` and reaches `search_options` as `scope="code"` |
| U11 | `shutdown()` stops the view's timers; closing the window mid-load raises nothing |
| U12 | `presenter.py` still imports no Qt |

## 10. Definition of done

1. U1-U12 pass, and the whole suite passes.
2. **Open the window and use it.** Every UI fault in this project was found by somebody
   clicking, never by a test - eleven so far, and that count is in `HANDOFF.md` for a reason.
   Click every row, sort every column, resize the window to its 720x480 minimum, and switch
   the system theme while the tab is open.
3. Check it against a machine with at least one real repository under an indexed root.
4. `CHANGELOG.md` appended under `[Unreleased]`. **Append only, never reorder.**
5. Your `HANDOFF.md` paragraph written and **handed to the backend thread**, which owns that
   file. Do not edit it yourself.
6. `git add` by name. Never `git add -A`.

## 11. What is deliberately not built

| Not built | Why |
|---|---|
| A search input on the Code tab | It is the search tab's job, and the bridge is how you get there. Two search boxes is two grammars eventually |
| Branch pickers, commit-range fields, scope dropdowns | The sixty-switch design. Every filter this application has is typed in one grammar and works everywhere |
| Add / remove / configure a repository | Repositories are found, not registered. A missing one means a missing indexed root, and the empty state says so |
| A commit list, a diff view, blame, a file-history timeline | All of it needs phase 2, which is not authorised and is gated on a measurement - backend order §14 |
| Per-repository settings | Nothing to set |

## 12. When phase 2 arrives

Not now, and not to be anticipated in the code. Recorded only so the tab is not built in a
shape that forbids it.

History search - deleted code, `git log -S`, lifecycle - is minutes of work on a large
repository, against an application whose contract is 300ms warm. If it is ever built it gets
its own explicit control with a progress bar and a Cancel button, on this tab, wired to a
worker. **It never goes behind the Enter key in the search box**, and the search box's promise
about latency is the reason.

Leave room for it below the table. Do not add a disabled button, a greyed-out control or a
"coming soon" - `search_bar.py:85` records what this project thinks of those, and it is right:
a greyed-out button is a permanent question with no answer visible on the screen it appears on.

---

## Addendum, 2026-08-25: repository search, and what GitSearch.txt did not get

The owner attached `GitSearch.txt` — a 853-line functional specification for a
repository intelligence platform — with the instruction: *"all the functionality
in this should be available as switches and should be searchable for git … the
style should be the / style we have in the app"*.

### What was built

`app/search/gitquery.py` (the catalogue, the parser and the argv builder — no
git, no Qt, so every switch is asserted on the command it produces),
`app/search/gitsearch.run_query` (running it and reading four different output
formats), `app.cli gitsearch` (headless first, per non-negotiable 8) and
`app/ui/widgets/git_search.py` (the panel under the repository tree).

Thirty-four switches, covering the specification's search modes (§4), methods
(§5), file filters (§6), branch filters (§7), commit filters (§8), author
filters (§9), date filters (§10) and history intelligence (§11).

### What was deliberately left out, and why

**The project's own rule is that a list is not an affordance if half of it does
nothing.** A menu full of switches that quietly fail is worse than a shorter
menu, because it is discovered one disappointment at a time. Each of these is
real work rather than an oversight:

| Switch | Why not | What it needs |
|---|---|---|
| `--compare main develop` (§13) | Two searches and a set difference, not one git command | A second plan kind that runs both and diffs the row sets |
| `--branches` / `--branch-origin` (§13) | `git branch --contains <sha>` per matched commit — one subprocess per result | Batching, or accepting N invocations behind a progress bar |
| `--branch-timeline` (§13) | A visualisation, not a search | A drawing surface; the graph work was cancelled in the V2 scope change |
| `--references` (§15) | Dependency analysis needs a parser per language | Out of scope until symbol indexing exists |
| `--team` (§9) | Needs a team roster the application has never been given | A place to define one, which is a settings feature |
| Security artifacts (§3) | Secret scanning is a rules engine, not a grep | Its own module, its own rule set, its own false-positive problem |
| `--fuzzy` (§5) | `CustomerId` matching `customer_id` needs an identifier-aware matcher | A tokeniser; `/regex` covers the cases people actually type |

`--wildcard` (§5) is absent as a switch because `/regex` and git's pathspecs
already do it and a third spelling of "match loosely" is a menu that has to be
explained.

### The two facts that shaped it

**It is a second search domain, not a filter on the first.** The `/` menu until
now narrowed the index — rows describing files read once, at index time. History
is not in the index and cannot be. So the grammar is shared and the engine is
not, and `test_gitsearch.py` asserts that nothing running on a keystroke can
reach git.

**It is slow, and the number is on record.** `git log -S` over 200 commits of
this repository: 2.26s. `git grep` over one revision: 0.33s. That is why the
panel searches on Enter and never on a keystroke, why every history plan carries
`--max-count`, and why `GitPlan.slow` exists for the UI to read.

### Next, in order

1. **Cancellation that actually kills git.** Stop currently abandons the answer
   and lets the subprocess finish or time out — stated on the button rather than
   implied. Doing it properly needs `Popen` and a process group, which changes
   the `runner` seam every test in `test_gitquery.py` and
   `test_gitsearch_reading.py` depends on. Worth doing; not worth doing quietly.
2. **`--compare`**, which is the most-asked-for of the absent switches and is
   two runs and a set difference.
3. **Streaming results.** A history search returning 2,000 rows currently draws
   them all at once, at the end. `git log` produces them steadily and the table
   could fill as they arrive.
