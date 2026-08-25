# Code Review

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.2

**Date:** 2026-08-25  
**Scope:** Current `layer/2-extraction` worktree, including the uncommitted UI and storage changes.  
**Review focus:** correctness, data safety, performance, reliability, security/privacy, and test coverage.

## Executive Summary

The extraction and indexing design is thoughtfully documented and has strong unit coverage, but the current worktree has three release-blocking defects:

1. A low-disk stop can still run the missing-file prune and delete valid index rows for files that were never visited.
2. Semantic filters become incomplete when more than 2,000 files match: the code searches only the global top 100 and filters afterward.
3. The shared Qt context menu has callback-signature errors, so common actions fail when triggered.

The full suite could not be accepted as green. Running with the repository's temp configuration fails during pytest cleanup with `WinError 5`; running with an external temp directory gets substantially farther but hits a Windows access violation while JPype starts MPXJ in the diagram test. Focused tests for the new view-preference/debounce/settings code passed (`49 passed`).

## Findings

### P1: Low-disk stop can prune unvisited files

**Location:** [app/index/pipeline.py](app/index/pipeline.py#L520-L545) and [app/index/pipeline.py](app/index/pipeline.py#L356-L365)

`_consume()` breaks when `_disk_ok(stats)` is false, but it does not set `_interrupted` or call `request_stop()`. `run()` then reaches the prune phase, whose guard checks only `_interrupted`. With `prune_missing=True`, every indexed file not yet yielded by the walker is absent from `seen_paths` and can be deleted from SQLite. The next run will no longer know those files were indexed.

**Impact:** Potential loss of searchable index data during exactly the resource-exhaustion condition intended to protect it. User documents are not deleted, but valid metadata, chunks, and messages can be removed.

**Recommendation:** Use one helper for all deliberate early stops that sets `_interrupted` and the stop event, or make the disk branch explicitly call `request_stop()` before breaking. Add a regression test with a populated store, a walker that has not yielded all existing paths, a failing disk check, and `prune_missing=True`; assert that no unseen row is pruned.

### P1: Large semantic filters return incomplete results

**Location:** [app/search/vector.py](app/search/vector.py#L68-L88)

When `allowed_file_ids` exceeds `MAX_PREFILTER_IDS`, `_prefilter()` returns `None`. The code then asks the vector store for only `limit` global hits (normally 100) and applies the file-id filter afterward. A matching file ranked 101st globally is discarded without being replaced by the next eligible file. A broad filter such as `type:pdf` can therefore produce few or zero semantic hits even when many eligible PDFs exist.

The comment describes this as “correct but weaker,” but it is not correct for a filtered top-k query; it is an approximation that changes search semantics.

**Impact:** False negatives in the primary search workflow on larger indexes, especially for common type/folder filters.

**Recommendation:** Keep filtering inside the ANN query, use a temporary filter table/partition supported by the vector backend, or retrieve enough candidates to guarantee the requested eligible top-k with an explicit bounded approximation and visible warning. Add a test where the only eligible vector is ranked beyond the first 100 global rows.

### P1: Qt context-menu actions have incompatible signal signatures

**Location:** [app/ui/widgets/file_menu.py](app/ui/widgets/file_menu.py#L68-L112)

`QAction.triggered` emits a boolean, but the callbacks for Open, Show in folder, Search inside this file, Copy path, Copy file name, and Re-index are lambdas with no parameter. Triggering one of these actions will call the lambda with an argument and raise `TypeError`. The newer copy actions correctly accept `_checked=False`, which makes the inconsistency especially easy to miss.

**Impact:** Core file actions are unusable from the context menu. The error occurs only when a user clicks the action, so ordinary construction tests will not catch it.

**Recommendation:** Make every `triggered` callback accept `_checked=False`, or use a small adapter that discards Qt's checked argument. Add Qt tests that trigger each enabled action and assert its callback runs once.

### P2: Full test execution is not reliable on Windows

**Locations:** [pyproject.toml](pyproject.toml) and [app/extract/diagrams.py](app/extract/diagrams.py#L299)

The default full run attempted to clean a project temp/junction path and reported `PermissionError [WinError 5]`. With `--basetemp D:\pytest-review-all`, the run progressed further but terminated with a Windows access violation while JPype called `startJVM()` for MPXJ; Loguru queue-writer and asyncio threads were still active in the crash output. The test process did not produce a trustworthy final pass/fail summary.

**Impact:** CI/release confidence is reduced, and a developer can mistake a partially executed suite for a green suite.

**Recommendation:** Isolate the MPXJ test in a subprocess or mark it as an explicit environment-dependent integration test; ensure JVM startup/shutdown is serialized and compatible with the installed JPype/Java combination. Keep the external temp workaround in the documented Windows test command if the junction cleanup issue remains. Record the expected outcome for optional native integrations.

### P2: Diagnostic bundles expose the raw `.env` file

**Location:** [app/core/diagnostics.py](app/core/diagnostics.py#L205-L225)

`build_bundle()` writes the complete environment file into `env.txt`, while the CLI tells users to send the resulting zip for troubleshooting. The current `.env` appears to contain paths and local endpoints rather than credentials, but this creates a future secret-disclosure boundary and may expose sensitive local directory names today.

**Impact:** A diagnostic zip shared outside the machine can disclose configuration values and filesystem structure; adding a token later would silently make the existing “send this file” workflow unsafe.

**Recommendation:** Redact values by allowlist, or include only key names and non-sensitive settings. If raw values are required for diagnosis, make the bundle opt-in and print a prominent review warning before sharing.

## Performance Observations

- The pipeline's bounded queues and batched embedding are good choices for the stated 100GB target.
- The large-filter vector fallback trades correctness for query speed. It should not be treated as a performance optimization until its result semantics are made explicit.
- `MailView` populates a `QTableWidget` row-by-row for up to 500 rows and six columns on the UI thread. This is manageable at the current page size, but repeated refreshes after indexing can visibly block repainting. A model/view table or incremental population would scale better if the page size grows.
- `search_files_by_name()` accepts a caller-provided `limit` without enforcing a positive upper bound. A negative SQLite `LIMIT` has special unlimited behavior, so defensive clamping would prevent accidental large result materialization.

## Test Gaps

- No regression test covers the low-disk early-stop plus prune interaction.
- No test proves filtered ANN search remains complete when the eligible file-id set exceeds `MAX_PREFILTER_IDS`.
- No Qt test triggers each `FileActions` menu action, so the `triggered(bool)` defects escaped.
- Real Outlook COM behavior and cloud non-hydration remain manual/environment-dependent as documented; keep those limitations visible in release sign-off.
- The MPXJ/JPype integration needs an isolated stability test rather than relying on the full suite process.

## Suggested Fix Order

1. Fix the low-disk stop state before enabling pruning in production.
2. Restore correctness for large semantic filters, then benchmark the chosen implementation.
3. Repair and test all context-menu signal adapters.
4. Isolate the native MPXJ test and make the Windows test command deterministic.
5. Redact diagnostic configuration before sharing bundles.
