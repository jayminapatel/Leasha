# Work order register

**Doc version:** 1.29 · **Updated:** 2026-09-16 · **Applies to:** app v0.3.3

Every work order in one table, with its status and where it sits in the queue.

Before this document existed the queue lived in `HANDOFF.md` §"What is Next" and each
order's own `**Status:**` line pointed back at it — a circular register, and by
2026-08-30 the HANDOFF half had gone stale while shipped work was still listed as
next. **This table is now the register.** `HANDOFF.md` links here and does not
restate it.

**Progress numbers are counted from the checkboxes in each order**, not asserted.
Regenerate them with:

```powershell
venv\Scripts\python.exe -m app.cli orders     # if built; otherwise the shell one-liner in §4
```

---

## 1. Status vocabulary

| Status | Means |
|---|---|
| `DRAFT` | Written, not authorised. Decisions inside it are still open. Nobody may start it |
| `RELEASED` | Authorised by the owner. It will be built, in queue order |
| `ACTIVE` | Being built now. At most one order should hold this |
| `SHIPPED` | Every item ticked and committed. Kept for the record |
| `HELD` | Authorised in principle, parked behind a named condition |
| `PARKED` | Written, not authorised, no condition set. Revisit deliberately |
| `SUPERSEDED` | Replaced by a later order, named in the row |

**Two conventions this table introduces, because their absence is what let the queue
drift.** First, every order carries a `**Status:**` line in its header — 25 of the 42
did not on 2026-08-30 and are marked *(inferred)* below until backfilled. Second,
`RELEASED` must be able to become `SHIPPED`; before this document there was no
completion state at all, so a finished order looked exactly like a waiting one.

---

## 2. The queue — released and active work, in order

The sequence is the owner's, carried from `HANDOFF.md` §"What is Next" as it stood on
2026-08-28 and corrected against the checkbox counts.

| # | Ref | Order | Status | Done/Open | Blocked by |
|---|---|---|---|---|---|
| 0 | `202626082352` | Remediate the 2026-08-26 review | RELEASED *(inferred)* | 55 / 3 | The remaining three are the structural splits, deferred by "working version first" |
| 0a | `202626270046` | Context-aware `/` menu, GUI and CLI | SHIPPED | 25 / 0 | Was queued behind review §2 (H5, H6, H11) |
| 0b | `202626270114` | Index Tuning — one screen, three modes | RELEASED | **39 / 1** | §5e closed by the idle-tune wiring (see `WORKORDER-space-report-and-idle-tune-ui-wiring` §2); 6h closed 2026-09-16 by the crash-recovery session (local int8 quantisation via `onnx`/`onnxruntime.quantization`, never a different source - see the order's own dated note). Open: 6i, still unmet on its own condition (§6a shows conversion does not cost enough to justify a persistent soffice session) |
| 0c | `202626270157` | The search experience — one box for an 8-year-old | **SHIPPED** | **26 / 0** | Closed 2026-09-07 |
| 0d | `202626270257` | Privacy defaults | **SHIPPED** | **9 / 0** | Was already complete; status corrected 2026-09-07 |
| 0e | `202626270326` | Workspace features — pop-outs, viewers, tools | **SHIPPED** | **30 / 0** | Closed 2026-09-07 by §5c |
| 0f | `202626270508` | Media by default, and the OCR ladder | **SHIPPED** | **17 / 0** | Closed 2026-09-07 by §3a's display/sort wiring and §2e's tunable |
| 0g | `202626270509` | mbox, Takeout, chats | **SHIPPED** | **7 / 0** | Was already complete; status corrected 2026-09-07 |
| 0h | `202626270510` | Pictures I — the CLIP lane | **SHIPPED** | **13 / 0** | Closed 2026-09-15 — see the order's 2026-09-15 note |
| 0i | `202626270511` | Pictures II — tags, enrichment, places | **SHIPPED** | **14 / 0** | The register's old 7/7 was stale — the order's own doc had already reached 14/14 by 2026-09-15, recounted 2026-09-16 |
| 0j | `202626270512` | The Photo Tagger — naming people | **SHIPPED** | **13 / 0** | Closed 2026-09-16 — 2c's suggestion-chip UI was the one real gap; see the order's own dated note |
| 0k | `202626270513` | Offline Media I — drives in drawers | **SHIPPED** | **17 / 0** | Closed 2026-09-15 — §3 (search/browse decoration) finished the same day; see the order's own dated note |
| 0l | `202626270514` | Offline Media II — network, cloud, placeholders | RELEASED | **13 / 4** | 0k (SHIPPED). 1a/1d/3d/3b-3 and now 2b closed (2b: per-folder cloud-content opt-in with a shared session-wide download cap, built 2026-09-16 by the crash-recovery session — see the order's own dated note). Open: 2a (a new cataloguable cloud volume kind — out of scope by owner decision 2026-09-16, not attempted), 2c (DEFERRED INDEFINITELY, do not build), 3b-2's on-tape ordering and one §4 test (both need hardware not present on this machine) |
| 0n | `202626270602` | Reports, and the Life Timeline | RELEASED | 6 / 9 | §1/§2 built; §3 (Space Report) backend built and on the Reports page. 3c's caching/progress and, later the same day, two of 3a's three sub-asks (pHash near-duplicates, per-source duplicate share) all built 2026-09-16 by the crash-recovery session - checkbox count unchanged both times, each item stays open on its one remaining sub-ask (3a: the interactive table; 3c: the scale-fixture measurement). Open: 3a's interactive table, 3c's measurement, and all of §4 (Life Timeline, held by owner decision 2026-09-16, not started) |
| 0p | `202626271317` | Every table sorts, every header sits over its column | **SHIPPED** | **17 / 0** | Was already complete; status corrected 2026-09-07 |
| 0q | `202626271510` | The results, world class | **SHIPPED** | **25 / 0** | Closed 2026-09-16 - the last §8 item (the pytest-qt scenario sweep) was waiting on 0m's harness, which now exists; `test_gui_scenarios_results.py` closes it and found a real bug along the way (see the order's own dated note: an interim-to-full tier swap never actually kept the selected row, only the isolated widget test did - fixed in `search_view.py`) |
| 0r | `202626271601` | The splash, and a fast lifecycle | RELEASED | 17 / 1 | §2b partially closed 2026-09-07 (Mail/Code deferred; Files/Indexing/Settings still up-front) — one item open |
| 0s | `202626271137` | The seven adoptions — five-AI review | **SHIPPED** | **17 / 0** | Closed 2026-09-07 |
| 0t | `202626130120` | One onnxruntime, and it says which one it is | **SHIPPED** | **24 / 24** | Closed 2026-09-15 — see the order's own 2026-09-15 notes and the register's note below |

**2026-09-07 — recounted, and six orders are now finished.** The previous set of
numbers was written on 2026-08-30 and had gone stale within days: 0f, 0g, 0h, 0p and
0q had all moved, and three orders standing at zero open were still listed as
waiting. That is precisely the drift this register was created to end, so these
counts come from §4's command rather than from editing the table by hand.

Finished in that session: **0c** (§6c — a cross-reference, closed by verifying its
four referenced items line by line in the remediation order rather than duplicating
them), **0e** (§5c, the DWG simplified view) and **0s** (the pytest-qt sweep across
all seven adoptions). **0d, 0g and 0p** were already at zero open and needed only
their status corrected — exactly the "finished or lying" case §4 warns about.

**0h is deliberately left at 12 / 1.** Its last item asks that a photo be found by
typing a description of it. The test is written against the real wiring and runs, but
the real CLIP model cannot be fetched in the build sandbox — `huggingface.co` answers
403 through the proxy, and disk was *not* the constraint. Ticking it would assert
something nobody has run, so the order carries the exact command that closes it on a
machine which can reach the model.

**2026-09-07, second pass — 0f closed, 0r nearly.** Four more parallel lanes, run the
same way as the first pass. **0f** (media by default, and the OCR ladder) went from
15/2 to 17/0 and is now `SHIPPED`: lane-a closed §3a's remaining "and any date display
use it" clause — the shot date now reaches both the SELECT projections
(`keyword.py`, `vector.py`'s image-search hydrate site too, which the item's own
wording had missed, `sqlite_store.py`) and the `/newest`/`/oldest` sort order
(`recency.py`, `folding.py`), with the SQL shaped by measurement rather than the
obvious `COALESCE` form, which a 200k-row `EXPLAIN QUERY PLAN` showed cost 39.5ms
against 0.10ms for the form actually shipped. Lane-b closed §2e's remaining tunables
half: `OCR_WHITE_PAGE_PERCENT` is now a real setting with a plain-words label, wired
into `ocr_ladder.py` and surfaced on the Index Tuning screen's Coverage group,
default-identical to the old hardcoded `0.7` cutoff.

**0r** (the splash, and a fast lifecycle) went from 15/3 to 17/1. Lane-c found the
pytest-qt splash checklist item had gone stale the same way 0c/0e/0d/0g/0p did on the
first pass — 1b's fade and minimum-hold fix landed 2026-09-05 but the §4 test item's
own note was never revisited after — and, checking honestly rather than assuming
either way, found three of six clauses genuinely still unproven; wrote one composed
scenario covering all six and ticked it. Lane-c also wrote the missing "stub-slowed
stage" test and found a real bug doing it: `app/main.py` was importing the search and
storage stack — `app.search.vector`, `app.ui.shell`, `app.extract.ocr` among them —
before `splash.show()`, contradicting both `main.py`'s own comment and `splash.py`'s
module docstring. Fixed. Lane-d closed §2b partially: Mail and Code tab construction
now defers to after `window.show()` via `QTimer.singleShot(0, ...)`; Files, Indexing
and Settings still build up front, flagged as a follow-up rather than attempted in
one pass given the file's size and risk. Measured in the build sandbox only —
662ms→180-410ms constructor time — with the owner's own <1.5s wall-clock target still
needing verification on the real machine; the order carries the exact reproduction
command. §2b stays unticked, honestly, and is 0r's one remaining open item.

Both orders' full diffs were verified against the pre-lane commit (`d042f09`) before
merging — additive only, no reversions — and the two pipeline.py/doctor.py test
failures surfaced during the post-merge sweep were independently reproduced against
that same unmodified commit, confirming they predate this session's work.

**2026-09-13 — 0t raised from a live fault, not from a plan.** An index run the
previous evening reported `0 files/min` and an ETA of `about 1823 days`. The
cause was not the indexer: plain `onnxruntime` 1.30.0 had been installed at
21:10 over the `onnxruntime-directml` 1.24.4 binaries — the two distributions
share one `site-packages\onnxruntime\` directory — so the machine lost
`DmlExecutionProvider` between a 19:03 run that logged *on the graphics card*
and a 23:36 run that logged *on the processor*. Everything after that follows:
CPU models breach the governor's 4,000MB ceiling, the compute fingerprint
changes and discards the measured rates, and the throughput collapse reaches the
user as an absurd ETA. `install.ps1` ships the same trap to every machine — it
installs the CPU wheel from unpinned transitive requirements first and the
DirectML wheel second, so GPU support survives only until the next `pip install`
that re-resolves `fastembed` or `rapidocr-onnxruntime`. 0t pins the pair, makes
the install order deterministic, gives `doctor.py` a check that can fail, and
puts a notice in the window when a machine loses a provider it used to have.

**The ETA arithmetic is deliberately not in 0t.** `format_eta`
(`app/ui/presenter.py:457`) guards only `files_per_minute <= 0`, so 0.067
files/min — printed as `0 files/min` on the same line — divides out to five
years. It is a separate defect in a separate layer and wants its own fix.

**2026-09-15 — 0h closed.** Its one open item was gated on a build sandbox with no
route to `huggingface.co`. This session runs directly on the Windows machine the app
ships for, not that sandbox — `curl https://huggingface.co` returns `HTTP 200` from
here — so the gate no longer holds. Ran the exact command the order's own 2026-09-07
note names: `venv\Scripts\python.exe -m pytest tests/unit/test_clip_lane_wiring.py -v`.
3 passed, 0 skipped, 48.30s; `test_a_photo_is_found_by_typing_a_description_of_it` ran
against the real `Qdrant/clip-ViT-B-32-vision`/`-text` towers (genuinely downloaded,
not stood in for) and passed. 0h moves to `SHIPPED`, 13/0. 0i and 0j's dependency on
0h is now satisfied.

**2026-09-15 — order 0's three remaining feature items closed, 52/6 to 55/3.**
`schema.sql:336` was re-verified rather than rebuilt: the §5 decision to hold
`SCHEMA_BASELINE_VERSION` at 4 still stands, its guard test still passes, and the
schema-versus-migration-replay cost was re-measured against today's
`CURRENT_VERSION` (22, up from 14) - a few tens of milliseconds either way, not a
reason to skip migrations that build `messages_fts`. **P8** closed on its latency
half, the way its own note said it could: `rerank_enabled` now defaults to `False`
across all four places that read it (`config.py`, `settings_registry.py`,
`rerank.py`'s fallback, `build_rerank`'s toolbar checkbox), leaving the toggle where
M12 put it. Its quality half - whether reranking is worth turning back on by default
- still needs `evaluate --builtin` against a real index and stays open as future
work, not as this item. **Relevance** found its recency half already shipped, in
`202626270157` §2d, and built only the missing filename-match half:
`app.search.filename_match`, a measured 0.05 bonus that persists into `rrf_score` so
later steps compose with it instead of discarding it, the way `definitions.boost`'s
own docstring records having happened once already. `test_recency.py`'s own measured
baseline moved from 14/20 to 15/20 as a direct, documented consequence and is
corrected in place. The three structural items in §7 (`cli.py`, `presenter.py`,
`SettingsController`/`IndexController`) were deliberately left untouched - "working
version first" defers them until the feature orders are done. Full account in
`docs/WORKORDER-202626082352-review-remediation.md`'s own three new dated notes.

**2026-09-15, later the same day — 0k closed.** §3 (search/browse
decoration) was the one section left open, and its own 2026-09-15 note had
already said exactly which half of 3a/3b/3c was missing: the inline row
badge (3a), the whole of offline preview (3b), and the Files-tab picker
control (3c). All three built and proven this session, plus a real,
pre-existing bug found and fixed along the way — `browse_files` (what
`/on` queries in the Files tab) never selected `volume_id`/`relative_path`
at all, so a file found by browsing to a catalogued volume opened
"missing" once selected, the identical bug 1b/3a had already fixed for
search results and never carried over to the browser. 0k moves to
SHIPPED, 17/0. See the order's own 2026-09-15 dated note (the later one)
for the full account, including the one line item not built (3b's
cached-thumbnail clause — checked against the code and found nothing to
hook into, not merely assumed absent) and the `files_view.py` 250-line
guard the new picker pushed over and then was brought back under.

**2026-09-16 — 0l moves from 8/9 to 12/5.** 1a's interactive dialog was the
only piece it was missing after 0k's tab shipped (its backend half was
already done 2026-09-15) - `check_renamed_source` (the tab's cheap pre-Scan
worker check), `scan_new_source`'s new `same_as` parameter, and
`RenameSuggestionDialog`, wired into a two-phase `shell.py` scan flow. 1d
and 3b-3 both needed only the tab's own help line, which did not exist
until now (`presenter.offline_media_help_text`, one function for both
sentences). 3d - the online-only results badge - rides 0513's §3a
decoration path exactly as named, now that that path is built: a new
`presenter.placeholder_marks` (a live `winfs.is_cloud_placeholder` check
per row, the same cost class `missing_paths` already pays) reaches the
same tooltip and inline-subtitle machinery 3a's offline-volume badge uses,
proven with a real `FILE_ATTRIBUTE_OFFLINE` round-trip rather than a mock.

**2a and 2b are assessed and deliberately left open**, not attempted
partially. Both are real, substantial features - a cataloguable cloud
volume kind with vendor-specific identity and a browser-routed Open action
(2a), and a folder-scoped opt-in with a size cap and its own CLI/UI surface
(2b) - neither of which shares much beyond the *read* guard §3 already
proved. The order's own 2026-09-16 note has the reasoning in full.
3b-2's on-tape ordering and one §4 test remain open for the same reason
they were before: no LTFS tape or mapped network drive exists on this
machine to verify real behaviour against, and this project's own standing
rule is not to guess at vendor behaviour nobody has watched. 2c stays
untouched, per the owner's DEFERRED INDEFINITELY decision.

**2026-09-15 — 0b's remainder: one of five closed, four re-verified still blocked.**
`docs/WORKORDER-202626270114-index-tuning.md` §6c (numpy/pyarrow end-to-end) is
done — `Embedder.embed()` returns a float32 `numpy.ndarray`, `VectorStore.add`
builds one `pyarrow.Table` per batch directly instead of a `list[dict]` LanceDB
converted a second time, measured 38.6% faster (median, every trial individually
faster) on a real LanceDB write path. §5e, §6d, §6h and §6i were
each re-checked against current code rather than assumed unchanged, and each is
still blocked on the same thing the 2026-08-27/2026-09-05 notes found: §5e
needs an idle-detection scheduler in `app/ui/shell.py`, a file several concurrent
worktrees are editing this week; §6d needs a third `FileStatus` and a product
decision for the owner, not this thread; §6h needs a new `onnx` dependency or
an unvetted external model repo, neither of which fastembed's catalogue offers
today; §6i's own condition (§6a shows conversion matters) is not met. 0b
moves from 35/5 to **36/4**. Full detail in the order's own dated notes, one under
each item.

**2026-09-15 — 0t closed, 24/24.** Built on the same real Windows venv this
register already runs on. `onnxruntime==1.24.4` is now pinned directly in
`requirements.txt`, matched by a documented `onnxruntime-directml==1.24.4`
pin — confirmed against PyPI as the newest version publishing both a plain
and a DirectML `cp312-win_amd64` wheel, closing the order's one
UNCONFIRMED item. `install.ps1` installs the DirectML wheel unconditionally
and last on any Windows machine with a display adapter
(`--force-reinstall --no-deps`, no more `[y/N]` prompt), verifies
`DmlExecutionProvider` immediately and fails loudly if it is absent, and
checks for a Leasha process holding the venv and stray `~*` stash
directories before touching packages at all. `doctor.py` gained two checks:
`check_onnxruntime_integrity` (required — fails on a version mismatch
between the two distributions or a stash remnant, but treats a
matching-version pair as the healthy, intended state, since section 3's own
fix means a correctly configured DirectML machine always carries both) and
`check_gpu_provider_intent` (optional — warns when an adapter has no
provider, silent when there never was an adapter or the probe failed to
run). `Pipeline.run()` now reports a lost-provider notice through the
existing `IndexStats.notices` mechanism — the same one
`_report_root_problems` already uses — so the Indexing tab shows it with no
new UI code. Verified end-to-end on this session's own real venv, which has
a genuine Intel Iris Xe adapter: reproduced the original fault (a plain
`pip install -r requirements.txt` against the pre-fix file pulled
`onnxruntime` 1.30.0), then fixed it and confirmed
`onnxruntime.get_available_providers()` returns
`['DmlExecutionProvider', 'CPUExecutionProvider']` and `doctor.py` prints
READY. 66 new tests across five files (`test_onnxruntime_pins.py`,
`test_doctor_onnxruntime.py`, `test_gpu_regression_notice.py`,
`test_resolve_gpu_regression.py`, `test_pipeline_gpu_regression.py`).

---

## 3. Held, draft, parked and shipped

| Ref | Order | Status | Done/Open | Note |
|---|---|---|---|---|
| `202626270547` | Test automation — the GUI clicked for real | RELEASED | **13 / 5** | Queue letter **0m**. Owner authorized starting directly 2026-09-16 (was HELD). §0/§1(mostly)/§2/§4 built and verified by the crash-recovery session - a real `gui_mainwindow` pytest-qt harness plus nine passing scenarios, hypothesis properties across four modules, and two real bugs found and fixed along the way (a stale surface name in `grab_ui.py`, a never-wired Escape key). `tools/nightly.py` (§5a) runs and was verified live but is left open - no pinned perf floors yet, only recall. §3 (pywinauto) is written but needs the owner's own interactive desktop to verify. See the order's own 2026-09-16 dated note for the full breakdown. |
| `202626270611` | The Chat tab — answers with receipts | HELD | 0 / 26 | Deliberately after the finder is finished |
| `202626082213` | Install and distribution | DRAFT | 0 / 0 | **Five `[FINALISE]` decisions open.** See §5 |
| `202626270238` | Migrate PyQt6 → PySide6 | DRAFT | 0 / 10 | Structural — deferred behind the working version |
| `202626270515` | Video and audio | DRAFT | 0 / 4 | The epoch after pictures |
| `202626160950` | UI Redesign — one shell for Windows and macOS | **ACTIVE** | **45 / 5** | Windows-venv verification run 2026-09-16 by the crash-recovery session: 29 of 34 open items ticked against passing tests and direct code reads (see the order's own dated note). Five stay open, honestly: 9b (shortcut-to-page and first-run-opens-Search scenarios untested), 9g (the full density/text-size sizeHint matrix), 9h (a single start-to-finish keyboard-only walkthrough), 9i (twelve golden PNGs — only eight of the described set exist and were looked at), 9j (performance timing — never reached, needs the owner's own machine) |
| `space-report-and-idle-tune-ui-wiring` | Wire the Space Report and the idle-tune scheduler into the redesigned shell | **SHIPPED** | **7 / 0** | Raised 2026-09-16 by the crash-recovery session; both wirings built the same day in the redesign session (`reports_view.py`, `shell.py`); ticked 2026-09-16 by the same session's Windows run (`pytest tests/unit/test_idle_tune_and_space_report_ui.py -v`, 10 passed) — see the order's own dated note |
| `202626271328` | The pages reorg | **SHIPPED** | **13 / 0** | Row corrected 2026-09-16: the order's own file has said RELEASED and delivered 2026-09-05, every item ticked, since that date; this row still read DRAFT 0/13 — the "finished or lying" case §4 names, found while registering the UI Redesign draft |
| `202626081052` | OCR where it pays | PARKED *(inferred)* | 10 / 7 | Largely superseded by `202626270508`'s ladder |
| `202626081149` | The Code tab | PARKED *(inferred)* | 0 / 0 | — |
| `202626081801` | Git sharpness and mail preview | PARKED *(inferred)* | 0 / 0 | — |
| `terabyte-scale` | Indexing 600GB, heading for 1.5TB | HELD *(inferred)* | 16 / 6 | — |
| `owner-pst-scale-run` | The PST scale run | HELD *(inferred)* | 0 / 4 | Owner-run, not code |
| `libraries-before-converters` | Libraries first, converters only where none exists | SHIPPED *(doctrine)* | 0 / 8 | Now non-negotiable #12 |
| `everything-tunable-has-a-ui` | Every tunable has a UI | SHIPPED *(doctrine)* | 0 / 6 | Now non-negotiable #11 |
| `zip-archives` | Every file findable, reading inside `.zip` | SHIPPED | **39 / 0** | — |
| `202626081106` | Wildcards without growing the index | SHIPPED | **29 / 0** | — |
| `file-types-and-ocr` | Config-driven file types, format editor | SHIPPED | 21 / 1 | — |
| `scope-change-search-and-chat` | Scope change — plain-English local search | SHIPPED | 8 / 1 | The decision that defines the product |
| `202626081439` | A reset must leave nothing behind | SHIPPED *(inferred)* | 0 / 0 | — |
| `202626081059` | Search does not do what a person expects | SHIPPED *(inferred)* | 0 / 0 | — |
| `inbound-ui-fixes` | Three finished UI fixes | SHIPPED *(inferred)* | 0 / 0 | — |
| `git-search-backend` | Repository awareness | SHIPPED *(inferred)* | 0 / 0 | Phase 1 only; phase 2 (history) **not authorised** |
| `git-search-ui` | The Code tab | SHIPPED *(inferred)* | 0 / 0 | — |
| `ui-shell-and-results` | Results layout, preview, tray | SUPERSEDED | 0 / 0 | By `202626271510` |
| `CONVENTIONS` | How work orders work now | — | — | Not an order; the rules for orders |

---

## 4. Regenerating the numbers

The Done/Open columns are checkbox counts. From the repo root:

```powershell
Get-ChildItem docs\WORKORDER-*.md | ForEach-Object {
  $t = Get-Content $_ -Raw
  $d = ([regex]::Matches($t, '(?m)^\s*[-*]\s*\[[xX]\]')).Count
  $o = ([regex]::Matches($t, '(?m)^\s*[-*]\s*\[ \]')).Count
  '{0,-58} {1,4} {2,4}' -f $_.Name, $d, $o
}
```

A row whose Open count is zero and whose status is still `RELEASED` or `ACTIVE` is
either finished or lying, and either way somebody should look.

---

## 5. The five open packaging decisions

`202626082213` is the only DRAFT holding decisions nobody can answer from the code.
Listed here because they gate Layer 9 and therefore any release.

1. PyInstaller one-folder versus uv with an embedded Python.
2. Per-user versus per-machine install.
3. The index default location — `%LOCALAPPDATA%\Leasha` is correct for privacy but
   sits on C:, and a 100GB corpus wants 150GB free.
4. The update mechanism.
5. The minimum supported Windows version.

**A sixth decision has moved underneath this order and was never revisited.** It was
written while the repository had no `LICENSE`, and concluded that free code signing
through SignPath — which requires a public codebase under a recognised OSS licence —
was therefore unavailable. `LICENSE` now exists and is MIT. The signing question is
open again.
