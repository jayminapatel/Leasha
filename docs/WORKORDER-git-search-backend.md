# Work order (Backend): repository awareness

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3

**Thread:** Backend

**Paired with:** `WORKORDER-git-search-ui.md`, which is **blocked on this one**. It needs the
`repos` table, the `repo:` filter and the `code` scope value before it can draw anything.

A self-contained brief. Everything needed to build phase 1 is below. **Commit the working
tree before starting**, and check `git status --short` for UI work in flight - this order
touches nothing under `app/ui/`, but `app/core/errors.py` and `CHANGELOG.md` are shared.

---

## 1. Why

The request was `GitSearch.txt`: a 56-flag specification for a git search, discovery and
"code archaeology" platform. Two facts about that document decide what happens to it.

**It is truncated.** It ends at line 853, three lines into §15, on a dangling `---------`.
Sections 16 onward do not exist, and they are the ones that would carry output format, data
model, performance budget, error behaviour and acceptance criteria. Everything present
describes *how to ask*; nothing describes what comes back.

**Most of it is `git` with a prefix.** UC-001 to UC-012 and the thirteen "modes"
(`GitSearch.txt:158-297`) are one parameter - a revision spec - that `git log`, `git grep` and
`git log --follow` already take. §13 lists what is rejected and why.

### The finding that sets the scope

**Source code is already indexed.** `TEXT_EXTENSIONS` (`app/extract/plaintext.py:34`) covers
`.py .js .ts .sql .ps1 .bat .cmd .sh .c .h .cpp .cs .java .go .rs .rb .php .html .htm .css`
along with `.json .yaml .yml .xml .ini .cfg .toml .md`. A `.cs` file inside a repository on an
indexed root is searchable today, by keyword and by meaning, with snippets and reranking.
`_EXT_GROUPS` (`app/search/query.py:78`, the `code` entry at :90) already maps `type:code` onto a subset of them.

So the gap is not extraction, not embedding and not search. It is three things, and only
three:

1. **Nothing records that a file belongs to a repository**, so results cannot be grouped,
   filtered or listed by one.
2. **`.git` is pruned and discarded.** It is in `DEFAULT_EXCLUDE_DIRS`
   (`app/index/walker.py:61`), so the walker stands next to the evidence on every pass and
   throws it away. Noticing it costs nothing that is not already being paid.
3. **There is no scope for code**, so a code search and a document search compete in one
   ranking.

None of the three requires git to be installed, a subprocess, a new extractor, or a byte of
new extraction. That is why this phase is small enough to be worth doing now.

## 2. Scope

| In (phase 1) | Out |
|---|---|
| Detect repositories during the existing walk | Anything reading git history |
| `repos` table, `files.repo_id` | Any git subprocess at all |
| `repo:` filter, `code` scope value | Symbol, function, class, interface search |
| `app.cli repos` | Dependency and impact analysis |
| Acceptance tests for all of it | Secret scanning |

**Phase 2 - history search - is sketched in §14 and is not authorised by this order.** It is
gated on a measurement, not on an opinion. See §14.

## 3. Schema v6

Additive only. No existing table is touched, no re-index is required - the same rule
`_v2_usage_logging` set and for the same reason: a migration that costs hours on a 100GB
corpus is a migration people skip.

`CURRENT_VERSION` (`app/storage/migrations.py:30`) goes 5 -> 6. Add `_v6_repositories` to
`MIGRATIONS` at line 211. Mirror the tables into `app/storage/schema.sql` so a fresh index and
a migrated one are identical - `schema.sql` is what a new database is built from, and the two
drifting is a class of bug that only appears on someone else's machine.

```sql
CREATE TABLE IF NOT EXISTS repos (
    id         INTEGER PRIMARY KEY,
    root_path  TEXT    NOT NULL UNIQUE,   -- absolute, as walked
    name       TEXT    NOT NULL,          -- basename of root_path
    kind       TEXT    NOT NULL,          -- 'work' | 'submodule' | 'worktree'
    last_seen  INTEGER NOT NULL           -- unix seconds, from the last walk
);

CREATE INDEX IF NOT EXISTS idx_repos_name ON repos(name);
```

And on `files`:

```sql
ALTER TABLE files ADD COLUMN repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);
```

`ON DELETE SET NULL`, not `CASCADE`. A repository that disappears - moved, deleted, unmounted -
must not take the indexed content of its files with it. The files are still on disk in every
case that matters, and re-indexing 40,000 files because a folder was renamed is not a
behaviour anybody would ask for.

`kind` is recorded because the three are found differently (§4) and because a submodule's
files are inside its parent's tree, which the UI will need to know before it draws a list.

## 4. Detection, in the walk

### 4.1 What to look for

**`.git` is not always a directory.** In a submodule or a linked worktree it is a *file*
containing `gitdir: ../.git/modules/foo`. Detection must test both, from the two lists
`os.walk` already hands over:

| Evidence | Found in | `kind` |
|---|---|---|
| `.git/` directory | `subdirectories` | `work` |
| `.git` file, contents start `gitdir:` and the target contains `/modules/` | `filenames` | `submodule` |
| `.git` file, contents start `gitdir:` otherwise | `filenames` | `worktree` |

A `.git` file is **already safe from indexing** and will stay that way: `Path(".git").suffix`
is `""`, so it never matches `resolved_extensions()` and is dropped by the existing check at
`app/index/walker.py:~215`. Verified. It is also therefore invisible to anything that only
looks at `subdirectories`, which is the trap this section exists to close.

Read at most 4KB of a `.git` file, and treat an unreadable or unparseable one as
**not a repository** rather than an error. Detection is a convenience; it may never fail a
walk. This is the posture the `walk()` docstring already states for a directory that cannot be
read (`app/index/walker.py:187`): *"A permission error on one folder is not a reason to
abandon a walk."*

### 4.2 Where the code goes

`walk()` is a generator, so it cannot return a second value. Add a sink to `WalkConfig`:

```python
#: Repository roots found during the walk, written here as they are seen.
#:
#: A mutable output parameter, which is not the shape this module prefers -
#: but `walk()` is a generator and a second return value is not available.
#: The alternative, a second pass over the tree purely to find `.git`, costs a
#: full walk of a 100GB corpus to learn something the first walk already had in
#: its hands.
repo_sink: Optional[dict[str, str]] = None    # root_path -> kind
```

Populate it in the prune block at `app/index/walker.py:~200`, **before** the names are
removed and **before** the `for filename in filenames` loop below it.

That ordering is load-bearing and is the thing to get wrong. `os.walk(topdown=True)` visits
the repository root in the same iteration that yields the files sitting directly in it, so
detection that happens after the filename loop leaves those files unattributed while
everything in subdirectories is attributed correctly - a bug that looks like flakiness and
will be blamed on the pipeline. Write the test for exactly this case (§11, T3).

### 4.3 Roots inside a repository

An indexed root may be *below* a repository root - `D:\SearchProject\app` has no `.git`
beneath it, and every file under it is still in a repository. One pure function, no walking:

```python
def enclosing_repo(start: Path, *, ceiling: Optional[Path] = None) -> Optional[Path]:
    """The nearest ancestor of `start` holding a `.git`, or None."""
```

Call it once per configured root, before the walk, and seed `repo_sink` with what it finds.
Stop at the drive root. `ceiling` exists for the tests, not for production.

### 4.4 Attribution, in the pipeline

`app/index/pipeline.py:453` already iterates `walk(self.config.walk)`. Around that loop:

1. Before: seed `repo_sink` from `enclosing_repo()` for each root.
2. During: for each candidate, find its repository by **longest matching prefix** over the
   roots discovered so far. Keep them in a list sorted by length descending and take the
   first match - an in-memory string comparison per file, no I/O, and correct for nested
   submodules by construction, which a first-match search is not.
3. After: upsert each discovered root into `repos` with `last_seen = now`.

`upsert_file` (`app/storage/sqlite_store.py:380`) gains `repo_id: Optional[int] = None`,
written into the new column in both the INSERT and the `ON CONFLICT` update. Additive; every
existing caller keeps working and writes NULL.

**Do not add a setting for this.** Detection is free, non-destructive and changes no
enumeration - so per non-negotiable 11, it is a constant and not a control. A repository
found under an indexed root is indexed exactly as it was before this work order; the only
difference is that the app now knows what it is looking at.

## 5. The `repo:` filter

Two lines, one table, per the comment at `app/search/query.py:58` - the field list is built
from `_FIELD_ALIASES`, so an alias added without a `COMMANDS` entry is invisible and a
`COMMANDS` entry without an alias silently matches nothing.

`_FIELD_ALIASES` (`app/search/query.py:43`) gains:

```python
"repo": "repo", "repository": "repo", "project": "repo",
```

`ParsedQuery` gains `repos: tuple[str, ...] = ()`, and `has_filters` gains `or self.repos`.

`COMMANDS` (`app/search/commands.py:79`) gains one entry, placed after `path`:

```python
Command(
    name="repo",
    aliases=("repository", "project"),
    summary="Only files in this code repository",
    example="/repo leasha",
    value_hint="a repository name, as shown in the Code tab - or several: leasha,tools",
),
```

`tests/unit/test_commands.py` already asserts `COMMANDS` and `_FIELD_ALIASES` agree. That test
passing is the definition of done for this section, and it also means the `/` dropdown,
`app.cli commands` and the Ollama prompt each gain the filter with no further work.

**Match on name, case-insensitively, and on path prefix.** `repo:leasha` and
`repo:D:\SearchProject` both work; people refer to a project by its name and to a checkout by
its path, and which one they reach for is not predictable.

In `keyword.py`, `repos` becomes a join to `repos` on `files.repo_id`. Follow the shape of the
existing `paths` clause rather than inventing one.

## 6. The `code` scope

`ParsedQuery.scope` (`app/search/query.py:158`) currently takes `"all" | "mail" | "documents"`.
Add `"code"`, defined as **`files.repo_id IS NOT NULL`** - in a repository - and handled in
`keyword.py` beside the existing branches at lines 104 and 108.

**"In a repository" and "has a code extension" are different questions and both stay
available.** `type:code` already answers the second (`_EXT_GROUPS`, `query.py:90`) and is
untouched. The scope answers the first, which is the one nothing could ask before, and is the
one that actually separates a work project from the same words in a document. A `.md` file in
a repository is in scope; a `.py` file in Downloads is not. Say so in the docstring, because
the opposite reading is the natural guess.

`scoped()` (`query.py:170`) and the cache key at `engine.py:410` already carry `scope`
through, so nothing else needs to change. Confirm `tests/unit/test_cache_key.py` still covers
the new value.

Note while you are in there, but **do not fix it under this order**: `docs/REVIEW-2026-08-25.md:27`
records that `cache=` is passed at none of the three `SearchEngine` construction sites, so
`self.cache` is always `None` and the whole `_cache_key` apparatus is currently dead. That is
a real finding with its own owner. It does not change anything here - the key must still be
correct for when it is wired up - but do not read "the cache already carries scope" as
evidence that a warm cache exists.

**`"documents"` must keep meaning what it means.** Do not quietly exclude repository files
from it. Somebody searching Documents for a connection string they know they wrote should
find it; the scope is a narrowing offered to the user, not a partition of the corpus.

## 7. Storage reads

**One** method on `SqliteStore`:

```python
def repos_list(self) -> list[Mapping]:
    """Every known repository with its indexed file count, most files first.

    Returns: id, name, kind, root_path, last_seen, files.
    """
```

One `LEFT JOIN` with a `GROUP BY`. The Code tab opens on it, so it must be fast on a cold
index - if it is not, the count becomes a cached column on `repos` rather than a join, and
that is your decision to make with a measurement in hand.

**A `repo_for_path()` lookup was considered and cut.** Nothing calls it. The UI order does not
use it, and a method written now for a caller that may never arrive is precisely the shape the
2026-08-25 review found five of: documented, believed, and wired to nothing. Add it in the
same commit as its first caller or not at all.

## 8. Settings

**None.** See §4.4. If this section ever grows an entry, the entry needs a reason that
survives non-negotiable 11 - "somebody might want to" is not one.

## 9. Errors

**No new codes in phase 1.** Detection is a filename comparison and a 4KB read whose failure
mode is defined as "not a repository". There is nothing to report. Phase 2 needs
`ERR_GIT_NOT_FOUND`; do not add it speculatively now.

## 10. CLI

Per non-negotiable 8, the CLI ships before the UI, so this can be tested headless and so the
UI thread has something to compare its output against:

```
app.cli repos [--json]
```

Prints each detected repository: name, kind, indexed file count, root path, last seen. Follow
`cmd_files` (`app/cli.py:890`) for structure and `cmd_formats` (`app/cli.py:1654`) for the
`--json` shape. Add the subparser beside `p_files` at `app/cli.py:1935`.

An index with no repositories prints one line saying so and exits 0. It is not an error; most
machines have none.

## 11. Acceptance tests

`tests/integration/test_repos_acceptance.py`, with these criteria quoted in the module
docstring.

| # | Criterion |
|---|---|
| T1 | A tree containing `repo/.git/` and `repo/src/a.py` yields one `repos` row, `kind='work'`, and `a.py` has its `repo_id` |
| T2 | A `.git` **file** containing `gitdir: ../.git/modules/lib` yields `kind='submodule'`; one containing any other `gitdir:` yields `kind='worktree'` |
| T3 | A file **directly in the repository root** is attributed, not only files in subdirectories. This is the ordering trap in §4.2 |
| T4 | Nested repositories: a file under `repo/vendor/inner/` where both have a `.git` is attributed to `inner`, not to `repo` |
| T5 | An indexed root *below* a repository root attributes every file to the enclosing repository |
| T6 | A `.git` file that is empty, binary or unreadable is not a repository, and the walk completes with every other file indexed |
| T7 | `repo:name` and `repo:<root path>` both return that repository's files; an unknown name returns `[]`, never an error |
| T8 | `scope="code"` returns only files with a `repo_id`; `scope="documents"` still returns them too |
| T9 | Migrating a v5 index to v6 preserves every existing `files` row, and their `repo_id` is NULL until the next index run |
| T10 | Deleting a `repos` row leaves its files indexed and searchable with `repo_id` NULL |
| T11 | A walk over a tree with no `.git` anywhere writes nothing to `repos` and costs no measurable extra time - assert the walk still completes within the existing budget |

T11 is the one that protects everybody else. This work is only justified while it is free.

## 12. Definition of done

1. T1-T11 pass, and the whole suite passes - not just these.
2. `doctor.py` prints READY on a clean install and on a migrated v5 index.
3. `app.cli repos` runs on a real machine with at least one repository under an indexed root.
4. A walk over an unchanged corpus is not measurably slower than before (T11, and once by
   hand at real scale).
5. `CHANGELOG.md` appended under `[Unreleased]`. **Append only.**
6. Your paragraph for `HANDOFF.md` §3 written and handed over - backend owns that file, so
   write it yourself, but co-ordinate if the UI thread has a paragraph pending.
7. `git add` by name. Never `git add -A`.

## 13. What is deliberately not built, and why

Each of these is in `GitSearch.txt` and each is refused here. Recorded so the decision is not
relitigated from memory.

| Rejected | Why |
|---|---|
| Symbol / function / class / interface search | A parser per language, and across history a parser per language per commit. tree-sitter and LSP do this and only for the working tree. One line of that spec is more work than everything else in it combined |
| §15 dependency and impact analysis | Call-graph construction, not search |
| Secret and credential scanning | Real value, wrong tool. Needs severity, allowlists and false-positive suppression to be anything but noise. gitleaks exists |
| `--team` | No source of truth for team membership exists anywhere in this application |
| `--endpoint`, `--config` | Saved regexes wearing a flag. If wanted, they are saved searches |
| `--remote-branches` | Implies fetch, credentials and network. Search what is cloned |
| Seven pattern-matching switches | Two axes: literal or regex, and case. Smart-case by default, as `git grep` has for years |
| `--commit`, `--commits`, `--range`, `--tag`, `--between-tags` | One revision expression, if phase 2 ever happens |

## 14. Phase 2, and the measurement that gates it

**Not authorised by this order.** History search - deleted code, `git log -S`, lifecycle,
`--follow` - is the genuinely valuable half of the original request and the half that can
break the application's central promise.

Searching the full history of a repository is O(commits x changed files). On a 50,000-commit
repository that is minutes. This application's contract is a p95 under 300ms warm, and the
non-negotiable is that no service and no unbounded work sits in the search hot path.

So before any of it is built:

```
app.cli gitsearch --repo <path> --rev <expr> "<pattern>"
```

Headless, no UI, shelling out to `git log -S` and `git grep`. Run it against the largest
repository available and **write the numbers into `HANDOFF.md`**: elapsed time at 1k, 10k and
50k commits, and peak memory.

Those numbers decide the design, and there are only two outcomes. Fast enough to be
interactive, and it can be a mode of the search box. Not fast enough - which is the likely
answer - and it is a separate, explicitly slow, cancellable job with a progress bar, wired to
its own button, never to Enter. Building the UI before knowing which is how the 300ms budget
gets lost by accident.
