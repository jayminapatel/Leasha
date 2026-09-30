# Archive — files the project no longer uses

**Doc version:** 1.0 · **Updated:** 2026-09-30 · **Applies to:** app v0.3.3

Moved here on 2026-09-30 at the owner's request ("archive any old files we don't use
now"), with `git mv`, so each file's history follows it. Nothing here is imported, run
or tested: `pytest` only collects `tests/`, and `ruff` skips this folder. To bring one
back, `git mv` it to where it came from — its old path is the path under `archive/`.

Chosen by evidence, not by age: every Python module was traced from `app/main.py`,
`python -m app.cli` and the tests, and every doc was checked against the register and
for references from code. Superseded work orders that code comments still cite were
**left in `docs/`**, because moving them would break those pointers. `app/chat/sessions.py`
looked unused (only its own test imports it) and was moved, then moved back the same
hour: `test_chat_layering` holds it as a module the architecture documents promise.

| Moved | Why it is no longer used |
|---|---|
| `app/ui/widgets/table_filter.py` | Nothing imports it; its only caller went with the one-box Code page (`d7d4240`, 2026-08-25). |
| `tools/bench_results_paint.py` | One-off benchmark for UI redesign item 9j, closed 2026-09-20. |
| `tools/measure_onnx_photo.py` | One-off Florence-2 full-versus-int8 comparison for order 1b item 8, closed 2026-09-30. |
| `docs/WORKORDER-ui-shell-and-results.md` | Superseded by order `202626271510` (register). |
| `docs/SESSION-2026-09-04-0q-results-handoff.md` | Its own header: historical, retired 2026-09-20. |
| `SESSION_CLOSE_2026-09-04.md` | One session's snapshot; the register and HANDOFF replaced it. |
| `docs/HANDOFF-github.md`, `scripts/setup-github.ps1` | Done: the repository has been on GitHub since, as `origin`. |
| `docs/HANDOFF-thread-merge.md` | Its own header: the UI thread closed 2026-08-25. |
| `finish-cleanup.ps1` | One-off removal of old `.claude\worktrees` folders; the working copy moved on 2026-09-30 and those folders went to `D:\Local\Archive\SearchProject-2026-09-30` with the rest of the old copy's leftovers. |
