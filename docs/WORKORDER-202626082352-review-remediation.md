# Work order (One thread): remediate the 2026-08-26 review

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Thread:** One thread (items are tagged with their layer)
**Source:** `docs/REVIEW-2026-08-26.md` — every item below cites its finding ID there;
the review carries the evidence, the reasoning, and the suggested fix. This document
is the checklist. Tick here, and only here.

Two items were already fixed in the working tree before this order was written; they
are pre-ticked and must still be committed and tested.

**Section 1 is complete** as of 2026-08-27, with tests, in
`tests/unit/test_review_2026_08_26.py`. Two notes on how it was done, because
both differ from what this order specified and the difference matters:

* **H1 is not an allow-list of retryable codes.** It could not be: the same
  code means opposite things depending on the pass. `ERR_NO_TEXT_LAYER` is a
  settled answer during a text pass and is precisely the work during an OCR
  one. So a skip is settled unless the *pass* says otherwise - a `retry` flag
  the re-queueing passes set, plus a deferral test that knows about `ocr_mode`.
  The first version used the flag alone and switched OCR off completely, which
  `test_ocr_passes` caught immediately: a held image reaches the images pass
  through the ordinary walk, with no re-queue function to set any flag.
* **H3 needed a second fix this order does not mention.** Keyset pagination
  removed the memory fault, and the backfill was still unusable: the store
  opens connections in autocommit, so `executemany` over a batch was five
  thousand separate transactions. Ten thousand rows did not finish inside a
  minute. Each batch now runs in one explicit transaction.

---

## 1. This week — correctness and data safety

- [x] **H4** (Search) `vector.py` / `engine.py` — embedder failure degrades to
  keyword-only with a NOTICE instead of failing the whole search.
  *Fixed in working tree, uncommitted. Add the regression test: broken embedder →
  keyword hits returned, notice fired.*
- [x] **H10** (Extract) `converter.py:333` — `convert()` uses `resolve_binary`, not
  `shutil.which`. *Fixed in working tree, uncommitted. Add the test the review
  names: patch `shutil.which` to None and `_installed_on_windows` to a path, assert
  `convert` runs.*
- [x] **H1** (Index) `pipeline.py:1252` — unchanged SKIPPED/FAILED files are settled,
  not re-extracted every run; allow-list of retryable codes (`ERR_FILE_LOCKED`,
  OCR-mode-changed).
- [x] **H2** (Storage) `migrations.py:516` — v10 recreates `idx_files_mtime` and
  `idx_files_source_kind`; ship v13 `IF NOT EXISTS` repair + `ANALYZE` for databases
  already migrated. Test on a *migrated* DB, not a fresh one — that is why it survived.
- [x] **H3** (Storage) `migrations.py:295` — v7 backfill uses keyset pagination with
  per-batch commits and logged progress, never `fetchall()` of the corpus.
- [x] **M1** (Search) `engine.py:358` — hoist the `closed` guard to the top of
  `search()`; it currently only runs when a cache is configured, i.e. never.
- [x] **M7** (Index) `pipeline.py:1755` — add `"quoted_removed"` to the
  `_store_message_meta` keys tuple. One line; mail preview is dead without it.
- [x] **M9** (UI) `search_view.py:197` — clearing the search box clears results and
  status; the empty branch of `_dispatch` is unreachable from typing today.

## 2. Before the next scale run

**§2 is complete** as of 2026-08-27. H6 did not go the way this order says, and
the difference is the whole finding - see below.

**The indexing half was done first** on 2026-08-27, tests in
`tests/unit/test_review_section_two.py`. H5, H6 and H11 remain: the first two are
*search*-side and the third is the Files tab, so none of them blocks a scale run.

Two corrections to what this section assumed:

* **H8's premise was wrong.** `source_kind="archive"` is not "the archive file";
  it is every row that came *out* of one, marker and members alike. The first
  version treated them all as containers, found that `backup.zip/q3/plan.dwg` is
  not a path on disk, and deleted the members of healthy archives.
  `test_archive_reading` caught it at once. Containers are now decided first and
  members inherit the answer - and an archive whose *parent folder* has also
  vanished is left alone, because that is an unmounted share rather than a
  deletion, and the blast radius is 200,000 rows.
* **H6's suggested fix does almost nothing, and the measurement says why.** The
  order proposed FTS5's `ORDER BY rank LIMIT` rowid subquery. Built and timed on
  a 40,000-chunk corpus: 24.5ms to 22.6ms, about 8%. Breaking the statement into
  parts showed where the time actually goes - the raw match of 24,048 rows is
  7.8ms, the *joins* are ~2ms, and **scoring is the rest**. Cost is proportional
  to rows matched, not rows returned.
* **So the lever is matching fewer rows, and `AND_TERM_LIMIT` is not the way.**
  Raising it is the obvious move and it is wrong: its docstring holds the
  measurement, and three left six of twenty real sentences returning nothing,
  four left eleven. The value stands. Instead the AND form is tried **first**
  and the OR form runs only when AND does not fill the page - identical recall,
  because the wide query still runs whenever the narrow one is thin, and
  21.2ms to 5.4ms when the terms genuinely co-occur.
* **M6 needed a repair as well as a fix.** Flushing before the marker closes the
  window; it does nothing for the holes earlier runs already left, and
  `iter_unembedded` was reachable only by a command nobody knows to run. Every
  run now drains them at the start and says how many it filled.

- [x] **H7** (Index) `pipeline.py:1977` — prune in batches: one chunked
  `delete_by_file_ids(doomed)`, SQLite deletes inside `store.batch()`. Today it is
  one Lance version + one transaction per file.
- [x] **H8** (Index) `pipeline.py:1970` — prune deleted archives/PSTs: missing archive
  path → cascade-delete marker, `path LIKE archive_path || '#%'` rows, and their
  vectors. Today a deleted 30GB PST stays searchable forever.
- [x] **H9** (Extract) `chunker.py:141` — replace the per-word paragraph scan with
  sorted starts + `bisect_right` (O(W log P)); add the perf floor test (100k words
  chunk in < 2s). Measured today: 80k words = 73s.
- [x] **H5** (Search) `keyword.py:158` + `engine.py:373` — stop materialising every
  eligible file id on filtered searches: probe with `LIMIT MAX_PREFILTER_IDS+1`,
  move filterable columns into the Lance table for native pushdown, run the rest in
  the pool.
- [x] **H6** (Search) `keyword.py:71` — benchmark at representative scale, then use
  FTS5's `ORDER BY rank LIMIT` rowid subquery when unfiltered and the over-fetch
  ladder when filtered. This is the main p95 risk.
- [x] **H11** (UI) `files_view.py:318,345,353` — open/reveal route through
  `CallableWorker`; `selected_path()` reads the row object, not the store.
- [x] **M6** (Index) `pipeline.py:1467-1825` — flush `pending_vectors` before
  `_write_marker`; drain `iter_unembedded` at run start. Closes the permanent
  silent vector-coverage holes.
- [x] **M8** (Storage) `vector_store.py:393` — no synchronous IVF_PQ retrain from
  `add()`: keep the end-of-run call only, or set a "building index" pause reason.
- [x] **M16** (Extract) `email_pst.py:281` — iterate `folder.items()` lazily; stop
  materialising 100k-message folders.
- [x] **M17** (Index) `pipeline.py:622` + `walker.py:419` — one shared seen-path set
  instead of three (~1GB each at 5M files).
- [x] **M18** (Index) `walker.py:484` — count stat failures into stats; probe the
  Windows long-path policy in doctor (paths >260 chars are currently invisible).

## 3. Search correctness and semantics

**Done** as of 2026-08-27, tests in `tests/unit/test_review_section_three.py`.
Only `wildcards.py:74`'s budget remains, which is a documentation correction
rather than a wrong answer.

Three notes:

* **M20 needed a schema change**, not an expression. `LIKE` folds ASCII only and
  so does SQLite's own `LOWER()` - `SELECT lower('JOSÉ@x')` is `'josÉ@x'` - so
  no query over the stored column could ever match. Schema **v14** adds folded
  copies written by Python's `str.lower()`, and the filters read
  `COALESCE(sender_lc, sender)` so an interrupted backfill degrades to today's
  behaviour rather than losing rows.
* **M2 went further than lowercasing the key.** The raw text is out of the key
  entirely: the parse identifies a search, so `Quarterly Report` and
  `  quarterly report ` share an entry while `pump AND valve` and
  `pump and valve` no longer do.
* **The cache question is settled: built, not deleted.** The machinery was
  complete and correct - generation in the key, copies on read, guarded writes -
  and simply unreachable, because `cache=` was passed nowhere. A bounded
  in-memory LRU is now the default; `cache=False` switches it off.

- [x] **M2** (Search) `engine.py:619` — cache key must not lowercase the raw query
  (`pump AND valve` ≠ `pump and valve`). Fix *before* wiring any cache.
- [x] **M3** (Search) `engine.py:253` / `wildcards.py:236` — wildcard expansion cache
  keyed on `store.generation`, or cleared when it changes.
- [x] **M4** (Search) `query.py:75` — `-type:pdf` must exclude, not include;
  `-"phrase"` must exclude, not require. Honour or report the negation.
- [x] **M5** (Search) `engine.py:374` — `result(timeout=…)` on retrieval futures;
  keyword-only degrade + notice on timeout.
- [x] **M20** (Storage) `filters.py:84` — lowercase shadow columns so `from:josé`
  matches `José@…`; LIKE is ASCII-only case-insensitive and the FTS half disagrees.
- [x] **L** (Search) `query.py:321` — `before:` with a partial date resolves to the
  period's end, not its start.
- [x] **L** (Search) `rerank.py:159` — retry budget instead of permanent latch on one
  transient scorer failure. Same pattern for **M15** (Extract) `ocr.py:118` — raise
  `ERR_OCR_UNAVAILABLE` instead of recording every image as `ERR_NO_TEXT_LAYER`.
- [x] **L** (Search) `wildcards.py:74` — make `BUDGET_S` a real budget
  (progress handler / interrupt) or correct its documentation.

## 4. UI polish and hygiene

- [x] **M10** (UI) `search_view.py:231` / `shell.py:1447` — search-tier failures go to
  the NoticeBar, not a modal per keystroke.
- [x] **M11** (UI) `widgets/preview.py:371` — decode `QImage` on the worker; no
  `QPixmap(path)` / `QPdfDocument.load` in UI-thread slots.
- [x] **M12** (UI) `search_bar.py:163` / `shell.py:344,571` — one rerank state: toolbar
  initialised from `ui:rerank_enabled`, both controls share one handler.
- [x] **M13** (UI) `settings_view.py:288` — `count_searches()` and libpff probes move
  to `CallableWorker`; nothing reads the store during `MainWindow.__init__`.
- [x] **M19** (UI) `results_view.py:207` — set `AccessibleTextRole` in `_append`; the
  results list is currently invisible to screen readers.
- [x] **L** (UI) `result_delegate.py:217` — sizeHint and paint agree on snippet lines
  (reserve one or draw two).
- [x] **L** (UI) `view_options.py:343` — delete the per-click QMenu
  (`WA_DeleteOnClose`) instead of leaking it.
- [x] **L** (UI) `shell.py:505` — give the Code tab a shortcut; gate Ctrl+Enter on
  the Interpret button being visible.
- [x] **L** (UI) `scheduler.py:99` / `shell.py:878` — cache last-run in memory;
  persist via worker, not a UI-thread write per minute.

## 5. Core, extract, CLI — smaller items

- [x] **M14** (Core) `config.py:299` — env vars override even when the key is absent
  from `.env`; iterate a canonical key list.
- [x] **L** (Core) `config.py:326` — `RERANK_TOP_N` / `RERANK_WINDOW_CHARS` through
  `_as_int`, not bare `int()`.
- [x] **L** (Extract) `archive.py:307` — stop double-charging nested archives against
  the byte budget.
- [x] **L** (CLI) `cli.py:559` — `cmd_extract` streams per record; no corpus-sized
  buffer for `--json --chunks`.
- [x] **L** (Storage) `sqlite_store.py:1469` — `clear_index` via FTS `'delete-all'`
  with triggers dropped for the duration.
- [x] **L** (Storage) `sqlite_store.py:810` — 1-2 char query + `ext:` filter must not
  ignore the typed text; `:845` clamp the FTS-branch limit.
- [x] **L** (Storage) `sqlite_store.py:476` — clear-hash sentinel for
  `verify_hash=False` runs instead of keeping a stale hash.
- [ ] **L** (Storage) `schema.sql:336` — seed fresh DBs at `CURRENT_VERSION`, not 4.
- [x] **L** (Repo) delete `config/settings.json` (dead `"DummyApp"` scaffold).

## 6. Carried over, still open from the 2026-08-25 review

- [x] **Decide the cache question once** (Search): build the generation-keyed LRU the
  engine anticipates — after M2 — or delete the dead `_cache_key` apparatus. Also
  fixes **M** `engine.py:625` (cached responses share mutable `SearchResult`s —
  `replace()` each, or freeze the dataclass).
- [ ] **P8** rerank off the critical path or off by default until async.
- [ ] **P9** numpy end-to-end in embedder/vector_store (also review finding on
  `vector_store.py:285` per-row float re-boxing).
- [ ] **P11** streamed reads in `plaintext.py` instead of `read_bytes()` at 2GB.
- [ ] **A3** delete the dead knowledge-graph methods from `SqliteStore`.
- [ ] **Partial A4** `filters.py:85` — escape `%`/`_` with `ESCAPE '\'` like the
  store helpers do, so a literal `%` means the same thing everywhere.
- [ ] **Relevance**: blend a small recency decay + filename-match bonus into the
  fused score; `/newest` should not be the only way to prefer this decade.

## 7. Structural (schedule as its own orders when picked up)

- [ ] Split `app/cli.py` (3,160 lines) into a package by subcommand;
  `build_parser` stays the single registry.
- [ ] Split `presenter.py` (3,738 lines) into `app/ui/presenter/` by domain with
  re-exporting `__init__`; move worker bodies to `app/ui/tasks.py` and teach
  `test_ui_never_blocks` that boundary.
- [ ] Extract `SettingsController` + `IndexController` from `shell.py`.

## 8. The tests that make this stick (the review's closing point)

- [ ] Perf regression category: a chunking floor (100k words < 2s) and a keyword-path
  budget at a representative corpus size. H9 and H6 survived because nothing times
  anything.
- [ ] Windows-marked converter smoke test: convert one real `.doc` when soffice is
  present. H10 lived in exactly that shadow.
- [ ] Extend the `test_ui_never_blocks` scanner to the modules written since it was —
  it passed while UI-01/02, M11 and M13 shipped. A rule worth stating is worth a
  test that fails when new code breaks it.

---

## Done means

Each ticked item: the fix, the test that would have caught it, `pytest tests -q`
green, committed by name per §5 of `WORKORDER-CONVENTIONS.md`. When a section
empties, note it in `CHANGELOG.md` under `[Unreleased]`, append-only.

---

## Notes appended during execution

**§4 complete, 2026-08-27.** Tests in `tests/unit/test_review_section_four.py`.
Three of them failed on their first draft by matching the *comment* explaining
why the forbidden call was absent - the same trap that had already caught
`test_narrowing_the_tree_runs_no_subprocess` and `test_the_close_path_checks_
the_flag`. They parse the AST and drop docstrings now, via a shared `_code`
helper written for exactly that reason.

`settings_view.py` crossed its 250-line guard while M13 was being fixed, so the
worker body and both label sentences moved to `presenter.py` - which is where
the guard intends them to be, and makes the wording testable without a display.

**§5 complete, 2026-08-27.** Tests in `tests/unit/test_review_section_five.py`.

One item in §5 was **not** done, on measurement: *"`schema.sql:336` - seed
fresh DBs at `CURRENT_VERSION`, not 4."* Doing it would skip migrations 11-14,
which are not no-ops - they build `chunks_vocab`, `messages_fts` and its
triggers, and the folded mail columns. A database seeded forward comes up with
no mail search in it and nothing to say so. The replay it was meant to save
costs 2.9ms against `schema.sql`'s own 11.3ms. The seed stays at 4, is now
named `SCHEMA_BASELINE_VERSION`, and
`test_a_fresh_database_is_migrated_not_assumed_complete` fails if anybody moves
it forward without completing `schema.sql` first.

Two knock-ons: the nested-archive fix made a defensive branch unreachable
(`read_archive` returns before `_member` once the budget is spent), which the
test written for it proved, so the branch was removed rather than left as
untested code. And `test_settings_counts_the_usage_log_without_reading_it`
looked for `count_searches` in `settings_view.py`, where §4 had just stopped it
being; it now checks the presenter too, because the rule is about the count and
not about the file.

**§3's last item, 2026-08-27.** `BUDGET_S` was timed after the query returned
and logged if exceeded, which is a report rather than a ceiling: the one pattern
it exists to contain - `*a*` on a corpus with millions of distinct terms - ran
to completion regardless. A SQLite progress handler now aborts the statement at
the deadline, and being cut short is reported through a `problems` list in the
same shape `vector.search` already uses. Measured on a 60,000-term fixture: an
unbounded scan of 50ms, cut to 8ms by an 8ms budget, with the refusal saying to
add another letter.
