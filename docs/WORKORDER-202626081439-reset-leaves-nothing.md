# Work order: a reset must leave nothing behind

**Doc version:** 1.2 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5
**Created:** 2026-08-26 14:39 · **Layer:** L1 - `app/storage/sqlite_store.py`

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

**Thread:** the single merged thread

**Raised by the owner**, as a rule rather than a bug report: *"when an index is reset all data
must be reset, i.e. all db with nothing."*

It does not. Two tables survive `clear_index()`, and one of them is the reason a whole
afternoon was spent on a problem that a reset was supposed to have fixed.

---

## 1. What survives, and why

`clear_index()` (`sqlite_store.py:1348`) deletes from a hand-written list:

```python
for table in ("entity_mentions", "entity_edges", "entities",
              "search_hits", "searches", "files"):
```

Everything else is expected to follow. Most of it does, and correctly - this was checked
rather than assumed:

| Table | Cleared by | Verified |
|---|---|---|
| `chunks`, `messages` | `ON DELETE CASCADE` from `files` | `PRAGMA foreign_keys = ON` is set on every connection (`sqlite_store.py:239`), so the cascades are live |
| `chunks_fts` | `chunks_ai`/`ad`/`au` triggers, external content | `schema.sql:164` |
| `messages_fts` | `messages_ai`/`ad`/`au` triggers, `content='messages'` | `migrations.py:358` |
| `index_state` `graph:%`, `index:%` | Explicit `DELETE` | The cursors go |
| `index_state` `ui:%` | **Deliberately kept** | Correct - folders, schedule, theme. A reset that forgot which folders to index would be unrecoverable without setting the application up again |

Two do not:

### 1.1 `files_fts` - the filename index

A **standalone** FTS5 table: `USING fts5(name, folder, tokenize='trigram')` with no
`content=` and **no triggers**. Nothing cascades into it. `delete_file()` knows this and says
so in its own docstring:

> `files_fts` is a standalone FTS table, not an external-content one, so nothing cascades into
> it and it has to be cleared by hand.

`clear_index()` does not do by hand what `delete_file()` correctly does.

**It is user-visible.** `count_named_files()` (`sqlite_store.py:1082`) is a bare
`SELECT COUNT(*) FROM files_fts` with no join, and it feeds the Files tab summary - so after a
reset that tab reports the old number over an empty index. The result list itself joins `files`
(`:837`), so orphans do not appear as rows; the count is simply wrong, and the rows accumulate
for ever. It is also why `VACUUM` returns less space than a reset implies.

### 1.2 `repos` - the repository table

Not in the list, and nothing else removes a row from it. So every repository ever detected
survives a reset, including one whose `.git` has since been deleted.

**This is the third mechanism**, and it makes `WORKORDER-202626081149-code-tab.md` §2 worse than
that document states. It says the only route back from a wrong attribution is deleting the whole
index. That is wrong: deleting the whole index does not do it either. Correct §2 when this
lands - there is currently **no** route back, by any means the application offers.

## 2. The rule to encode

> A reset leaves no derived data. Every table is empty afterwards except `schema_version`,
> `index_generation`, and the `ui:%` keys in `index_state`.

Stated as an invariant rather than a list, because the list is what failed. `files_fts` was
added in schema v4 and `repos` in v6; both times the table was created, wired into writes, and
not added to this loop. A third table will be added eventually and the loop will be forgotten
again unless the test stops enumerating by hand too.

## 3. What to build

1. **Add `repos` and `files_fts` to the delete list** in `clear_index()`.
2. **Bump the index generation** in the same transaction. The search cache is keyed on it, and a
   cache serving results built from an index that no longer exists is exactly the class of bug
   the generation was introduced to prevent.
3. **The test enumerates `sqlite_master`**, not a list. After a reset, every user table must be
   empty apart from the three named exceptions, discovered from the database rather than
   remembered. This is the part that stops it happening a third time.
4. **`count_named_files()` joins `files`.** A count that can disagree with the list it labels is
   a count that will.

## 4. Acceptance

| | Criterion |
|---|---|
| A1 | After `clear_index()`, every table in `sqlite_master` is empty except `schema_version`, `index_generation`, and `index_state` rows whose key begins `ui:` |
| A2 | A1 is asserted by enumerating `sqlite_master`, so a table added later is covered without editing the test |
| A3 | `count_named_files()` returns 0 after a reset |
| A4 | The `ui:%` settings survive: indexed roots, schedule and theme are all still readable |
| A5 | The index generation is higher after a reset than before |
| A6 | Regression for §1.2: a `repos` row and its attributed files are both gone, so `app.cli repos` prints the "no repositories" line |
| A7 | Resetting an already-empty index is a no-op that does not raise |

## 5. Out of scope

The **vector store** is dropped separately by the caller (`shell.py`, `self._vectors.drop()`),
and `VectorStore.drop()` was checked: it drops the table and resets all four cached counts,
with a docstring recording the bug where two of them survived. That side is correct and is not
touched here.

`app.cli reset` does not exist - reset is a UI flow, deliberately, because destructive actions
here state their cost and confirm. That stays true; this changes what the flow does, not where
it lives.

## 6. Done — 2026-08-26

All four items in §3 built; A1-A7 pass in `tests/unit/test_reset_leaves_nothing.py`.

* `repos` and `files_fts` added to the `clear_index()` loop, with the reason recorded beside it.
* `_bump_generation` called in the same transaction as the delete.
* `count_named_files()` now joins `files`, so the label and the list answer the same question.
* The test enumerates `sqlite_master` and asserts every table is empty apart from
  `schema_version`, `index_generation` and `index_state`'s `ui:%` keys - **A2 is the one that
  matters**, because both misses so far were a table absent from a list, and a test written
  from the same list would have passed while the bug shipped.

Verified: 7/7 pass, **zero regressions** against a reconstructed baseline (58 failures before,
58 after, identical sets - all environmental in a Linux sandbox with no `lancedb` or PyQt6),
and no new lint.

**Not yet verified on Windows.** The suite there, and `doctor.py`, still have to be run - this
changes destructive behaviour, so confirm a reset then an index run really does rebuild before
trusting it.

`WORKORDER-202626081149-code-tab.md` §2 still needs its correction: it says the only route back
from a wrong attribution is deleting the whole index. That was untrue when written, and is now
untrue in the other direction - `forget_repo()` and `prune_repos()` have since landed, and a
reset now clears `repos` as well.
