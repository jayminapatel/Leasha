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

- [ ] **H7** (Index) `pipeline.py:1977` — prune in batches: one chunked
  `delete_by_file_ids(doomed)`, SQLite deletes inside `store.batch()`. Today it is
  one Lance version + one transaction per file.
- [ ] **H8** (Index) `pipeline.py:1970` — prune deleted archives/PSTs: missing archive
  path → cascade-delete marker, `path LIKE archive_path || '#%'` rows, and their
  vectors. Today a deleted 30GB PST stays searchable forever.
- [ ] **H9** (Extract) `chunker.py:141` — replace the per-word paragraph scan with
  sorted starts + `bisect_right` (O(W log P)); add the perf floor test (100k words
  chunk in < 2s). Measured today: 80k words = 73s.
- [ ] **H5** (Search) `keyword.py:158` + `engine.py:373` — stop materialising every
  eligible file id on filtered searches: probe with `LIMIT MAX_PREFILTER_IDS+1`,
  move filterable columns into the Lance table for native pushdown, run the rest in
  the pool.
- [ ] **H6** (Search) `keyword.py:71` — benchmark at representative scale, then use
  FTS5's `ORDER BY rank LIMIT` rowid subquery when unfiltered and the over-fetch
  ladder when filtered. This is the main p95 risk.
- [ ] **H11** (UI) `files_view.py:318,345,353` — open/reveal route through
  `CallableWorker`; `selected_path()` reads the row object, not the store.
- [ ] **M6** (Index) `pipeline.py:1467-1825` — flush `pending_vectors` before
  `_write_marker`; drain `iter_unembedded` at run start. Closes the permanent
  silent vector-coverage holes.
- [ ] **M8** (Storage) `vector_store.py:393` — no synchronous IVF_PQ retrain from
  `add()`: keep the end-of-run call only, or set a "building index" pause reason.
- [ ] **M16** (Extract) `email_pst.py:281` — iterate `folder.items()` lazily; stop
  materialising 100k-message folders.
- [ ] **M17** (Index) `pipeline.py:622` + `walker.py:419` — one shared seen-path set
  instead of three (~1GB each at 5M files).
- [ ] **M18** (Index) `walker.py:484` — count stat failures into stats; probe the
  Windows long-path policy in doctor (paths >260 chars are currently invisible).

## 3. Search correctness and semantics

- [ ] **M2** (Search) `engine.py:619` — cache key must not lowercase the raw query
  (`pump AND valve` ≠ `pump and valve`). Fix *before* wiring any cache.
- [ ] **M3** (Search) `engine.py:253` / `wildcards.py:236` — wildcard expansion cache
  keyed on `store.generation`, or cleared when it changes.
- [ ] **M4** (Search) `query.py:75` — `-type:pdf` must exclude, not include;
  `-"phrase"` must exclude, not require. Honour or report the negation.
- [ ] **M5** (Search) `engine.py:374` — `result(timeout=…)` on retrieval futures;
  keyword-only degrade + notice on timeout.
- [ ] **M20** (Storage) `filters.py:84` — lowercase shadow columns so `from:josé`
  matches `José@…`; LIKE is ASCII-only case-insensitive and the FTS half disagrees.
- [ ] **L** (Search) `query.py:321` — `before:` with a partial date resolves to the
  period's end, not its start.
- [ ] **L** (Search) `rerank.py:159` — retry budget instead of permanent latch on one
  transient scorer failure. Same pattern for **M15** (Extract) `ocr.py:118` — raise
  `ERR_OCR_UNAVAILABLE` instead of recording every image as `ERR_NO_TEXT_LAYER`.
- [ ] **L** (Search) `wildcards.py:74` — make `BUDGET_S` a real budget
  (progress handler / interrupt) or correct its documentation.

## 4. UI polish and hygiene

- [ ] **M10** (UI) `search_view.py:231` / `shell.py:1447` — search-tier failures go to
  the NoticeBar, not a modal per keystroke.
- [ ] **M11** (UI) `widgets/preview.py:371` — decode `QImage` on the worker; no
  `QPixmap(path)` / `QPdfDocument.load` in UI-thread slots.
- [ ] **M12** (UI) `search_bar.py:163` / `shell.py:344,571` — one rerank state: toolbar
  initialised from `ui:rerank_enabled`, both controls share one handler.
- [ ] **M13** (UI) `settings_view.py:288` — `count_searches()` and libpff probes move
  to `CallableWorker`; nothing reads the store during `MainWindow.__init__`.
- [ ] **M19** (UI) `results_view.py:207` — set `AccessibleTextRole` in `_append`; the
  results list is currently invisible to screen readers.
- [ ] **L** (UI) `result_delegate.py:217` — sizeHint and paint agree on snippet lines
  (reserve one or draw two).
- [ ] **L** (UI) `view_options.py:343` — delete the per-click QMenu
  (`WA_DeleteOnClose`) instead of leaking it.
- [ ] **L** (UI) `shell.py:505` — give the Code tab a shortcut; gate Ctrl+Enter on
  the Interpret button being visible.
- [ ] **L** (UI) `scheduler.py:99` / `shell.py:878` — cache last-run in memory;
  persist via worker, not a UI-thread write per minute.

## 5. Core, extract, CLI — smaller items

- [ ] **M14** (Core) `config.py:299` — env vars override even when the key is absent
  from `.env`; iterate a canonical key list.
- [ ] **L** (Core) `config.py:326` — `RERANK_TOP_N` / `RERANK_WINDOW_CHARS` through
  `_as_int`, not bare `int()`.
- [ ] **L** (Extract) `archive.py:307` — stop double-charging nested archives against
  the byte budget.
- [ ] **L** (CLI) `cli.py:559` — `cmd_extract` streams per record; no corpus-sized
  buffer for `--json --chunks`.
- [ ] **L** (Storage) `sqlite_store.py:1469` — `clear_index` via FTS `'delete-all'`
  with triggers dropped for the duration.
- [ ] **L** (Storage) `sqlite_store.py:810` — 1-2 char query + `ext:` filter must not
  ignore the typed text; `:845` clamp the FTS-branch limit.
- [ ] **L** (Storage) `sqlite_store.py:476` — clear-hash sentinel for
  `verify_hash=False` runs instead of keeping a stale hash.
- [ ] **L** (Storage) `schema.sql:336` — seed fresh DBs at `CURRENT_VERSION`, not 4.
- [ ] **L** (Repo) delete `config/settings.json` (dead `"DummyApp"` scaffold).

## 6. Carried over, still open from the 2026-08-25 review

- [ ] **Decide the cache question once** (Search): build the generation-keyed LRU the
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
