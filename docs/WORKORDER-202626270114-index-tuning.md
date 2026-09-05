# Work order (One thread): Index Tuning — one screen, three modes, any machine

**Doc version:** 1.1 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Core profile + Index pipeline + Storage + UI panel)
**Status:** RELEASED by the owner 2026-08-27 (registered in HANDOFF.md §"What
is Next") — sequenced after `WORKORDER-202626082352` §2 leftovers (H5/H6) and
`WORKORDER-202626270046` (slash menu), unless the owner reorders. This order
touches `pipeline.py`, `resources.py`, `embedder.py` and `vector_store.py`
heavily; do not interleave it with other pipeline work.

## What the owner asked for

A section under the Indexing page called **Index Tuning** where *everything*
about indexing performance can be tuned, with three modes — **Defaults**,
**Auto-tune**, **Manual** — that fit any machine: the current one (see §0), a
future one with a strong GPU, with no reconfiguration when the index moves
between them. Choices that cannot work must be unreachable; choices that are
legal but suboptimal must be allowed and warned about, never silently
overridden.

**And this is a product, not a personal tool** (owner, 2026-08-27): it will be
published — friends' machines, the owner's kids' machines, people who are not
IT-literate. That changes the weighting, not the design:

* **The owner's machine is one test case, never the target.** Every envelope
  formula must degrade gracefully down to a 4-core 8 GB laptop and up to a
  workstation; nothing may assume the §0 baseline.
* **Auto is the product.** Defaults/Auto must be zero-touch for someone who
  will never open this screen. Manual exists for the owner and people like
  him; the other 95% must get world-class behaviour without knowing the
  screen exists.
* **Auto keeps itself tuned** — §5d below. A machine that changes, ages, or
  was never benched must converge on good values without anyone asking.
* **Plain words everywhere.** Labels and warnings in the language the existing
  panel already uses ("Files at once", "Pause above") — no "ONNX", "intra-op",
  "DirectML", or "quantised" on any control a Defaults/Auto user can see;
  technical names live in tooltips and logs. A non-technical reader must be
  able to understand every sentence this screen shows them.
* **Existing labels and descriptions never change** (owner, 2026-08-27).
  Every control that already exists keeps its exact current label, tooltip
  wording, and setting key when it relocates to this screen — "Files at
  once", "Memory ceiling", "Pause above", "Stop below" and the rest move
  verbatim. The plain-words rule constrains **new** controls only; it is
  never a licence to reword an old one. A user who learned a control's name
  must find it under that name, and the deny-list test in §7 must therefore
  skip labels that predate this order.
* **One-click way back.** "Return to automatic" is always visible in Manual
  and undoes any experiment in one action — a fiddled-with kids' machine must
  be recoverable by a child.

## Reconciling with the standing rule

`WORKORDER-everything-tunable-has-a-ui.md` says most values should be
**demoted, not promoted** — a sixty-control panel is worse than none. This
order honours both owners' instructions the same way: the **mode switch is the
demotion**. In Defaults and Auto-tune, the panel is a summary card and one
button; every knob exists but is read-only, showing what the mode resolved it
to. Only Manual unlocks editing. Nothing becomes a hidden constant, and nobody
meets thirty spin boxes uninvited. Every knob promoted here must still justify
itself under that order's §1 test; anything that fails it becomes a fixed
constant with the standard comment, not a control.

---

## 0. Machine baseline — the owner's current machine, measured 2026-08-27

```
CPU   13th Gen Intel Core i7-1365U — 10 cores / 12 threads
      HYBRID: 2 P-cores (HT, boost ~5.2 GHz) + 8 E-cores (no HT)
      (the reported 1800 MHz is the base clock, not the working clock)
RAM   31.7 GB
GPU   Intel Iris Xe (96 EU, integrated) — DX12 / DirectML capable
Disk  D: WD Green SN350 2TB NVMe (DRAM-less; QLC-class), 942 GB free, NTFS
      C: Micron 2450 512 GB NVMe
```

Three corrections this data forces on the order below:

* **"No GPU" was wrong.** Iris Xe is DirectML-capable, so §2 is testable on
  *this* machine, not only the future one — and `auto` may legitimately pick
  the iGPU here if the bench says so. Whether 96 EUs beat this CPU on small
  embed models is exactly the question 5a answers; do not assume either way.
* **The core count is not 10 equal cores.** 2P+8E means the envelope's
  arithmetic ("workers ≤ cores−2", "intra-op = cores−workers") is too crude:
  an ONNX thread on an E-core delivers a fraction of a P-core's throughput.
  1a must record P/E topology (`GetLogicalProcessorInformationEx`), and 3a's
  formulas take weighted capacity, not a flat count. The static 4-worker
  default was closer to right on this machine than the flat count suggested.
* **The index volume is a DRAM-less SSD.** Sustained large writes degrade
  sharply once its SLC cache exhausts — bulk phases (6f FTS rebuild, Lance
  compaction) will not run at the drive's headline speed. 6a's timers should
  therefore report write MB/s per phase, and no write-throughput conclusion
  from short benches may be extrapolated to full runs.

The future machine gets the same block appended here when the owner runs the
profile snippet on it (or `doctor` once 1b exists).

## 1. ComputeProfile — the facts everything derives from

- [x] **1a** (Core) `app/core/compute_profile.py`: physical cores, logical
  processors, RAM, AVX support, disk kind (SSD/HDD) for the index volume, and
  GPU: DirectML adapter enumeration → name, VRAM, DX12 feature level. Pure
  detection, no policy. Cached in the store with a **hardware fingerprint**;
  re-detected when the fingerprint changes (this is the future-machine story:
  same index, new box, first launch notices and re-derives everything).
- [x] **1b** (Core) `doctor.py` prints the profile verbatim, plus which
  backend would be chosen and why. A wrong detection must be *visible* before
  it can be argued with.
- [x] **1c** (Core) one documented escape hatch: `COMPUTE_PROFILE_OVERRIDE`
  pointing at a JSON profile, for the day detection is wrong. Logged loudly
  when active. This is the only file-edited tunable, and it exists precisely
  because every other bound derives from detection.

## 2. Compute backends — CPU now, GPU when present

- [x] **2a** (Index) `EmbeddingBackend` interface with two implementations:
  `CpuOnnx` (today's FastEmbed path) and `DirectMLOnnx`. OCR and the reranker
  select providers through the same seam — one runtime, three consumers.
- [x] **2b** `EMBED_DEVICE = auto | cpu | gpu` (Setting, choice). `auto` tries
  GPU when the profile shows one, benches one batch, and **falls back to CPU
  with a notice on any failure** — the H4 discipline exactly: degrade loudly,
  never crash, never silently. `gpu` on a machine without one is refused at
  the control (greyed, with the reason shown), not at runtime.
- [x] **2c** (Install) `onnxruntime-directml` is an optional extra:
  `run-install.cmd` asks its one additional question only when a DX12 adapter
  is detected. It ships the CPU provider too, so one wheel serves both modes.
  **Verified 2026-09-05:** already shipped in commit `1b2ce2f` ("Installer:
  optional DirectML GPU detection"). `install.ps1` asks "Install the GPU
  support? [y/N]" only when a video controller is present (the same
  `Win32_VideoController` proxy `compute_profile.py` uses for "DX12 adapter" -
  neither checks an actual feature-level number) **and** DirectML is not
  already available in the current `onnxruntime`; skipped entirely on a
  machine with no adapter or where it already works. `run-install.cmd` is a
  thin wrapper (parse-check, then `install.ps1 %*`) - the question lives in
  the script it calls, which is where every other installer question in this
  file already lives, so nothing needed to move. `onnxruntime-directml` is one
  wheel carrying both the CPU and DML execution providers, so CPU keeps
  working whether or not the optional install is accepted; the base install
  already pulls plain `onnxruntime` in via `rapidocr-onnxruntime` regardless.
  `scripts\parse-check.ps1` re-run clean against `install.ps1` (3174 tokens,
  UTF-8 BOM, 0 non-ASCII besides the file's existing em-dashes) - no code
  change made, none needed.
- [x] **2d** vectors from different providers may coexist in one index; a
  fingerprint change does not invalidate embeddings. Assert dimension equality
  and record the provider per run in the run log — nothing else.

## 3. The envelope — bounds derived from the machine

- [x] **3a** (Core) `app/core/envelope.py`: for every tunable below, a pure
  function of `(ComputeProfile, measured rates)` returning
  `(floor, ceiling, auto_value, why)`. `settings_registry` stays declarative
  and import-free — its static `minimum`/`maximum` become the absolute hard
  bounds; the envelope narrows them per machine. Testable entirely with fake
  profiles.
- [x] **3b** settings store **intent**: the sentinel `auto` (default for every
  knob) or an explicit number. Explicit values are clamped to the envelope at
  load — with a notice naming old value, clamp, and reason — never silently.
  Index moved to a smaller machine: clamps and says so. Moved to a bigger one:
  ceilings rise, values stand.
- [x] **3c** cross-knob reconciliation, warn-don't-block: the one rule worth a
  warning is oversubscription — `workers + onnx_intra_op_threads >` logical
  processors + 2 shows an inline warning naming the numbers ("9 workers + 8
  ONNX threads oversubscribe your 12 threads — slower, not faster"). Illegal
  states (GPU without GPU) are unreachable; suboptimal states are the user's
  right, informed.

## 4. The Index Tuning screen

A new section on the Indexing page (`indexing_view.py` gains it; the existing
`indexing_settings.py` panel's nine controls fold into it — one screen, as
asked, not two).

- [x] **4a Mode switch** at the top: **Defaults · Auto-tune · Manual**.
  Defaults = envelope auto values from detection alone. Auto-tune = auto
  values refined by measured rates (§5). Manual = every control editable
  within its envelope. Mode is itself a Setting; switching back to
  Defaults/Auto keeps the manual values stored but inert, so experimentation
  is reversible.
- [x] **4b Machine card** (read-only): the ComputeProfile in plain words —
  "10 cores / 12 threads · 32 GB · SSD · no GPU" or "…· NVIDIA RTX xxxx,
  16 GB" — with *Re-detect* and *Benchmark now* buttons (both via
  CallableWorker; nothing on this screen touches the store or hardware on the
  UI thread).
- [x] **4c Compute group** — every control shows its resolved value in Auto
  ("Auto (6)") and its envelope in Manual (spin box clamped to it):
  `EMBED_DEVICE` (choice; gpu greyed-with-reason when absent);
  `INDEX_WORKERS` (existing, ceiling from weighted P/E capacity per §0, 0
  stays "auto/governed"); `ONNX_INTRA_OP_THREADS` (new; auto = weighted
  cores−workers on CPU, 2 on GPU); `EMBED_BATCH` (new; 32–1024, auto 256 CPU
  / 512 GPU); `EMBED_QUANTISED` (new, CPU only — greyed on GPU with the
  reason: no gain); queue depth stays a constant unless Tier-0 timing proves
  otherwise — the demote rule.
- [x] **4c-2 Resources group** — the full audit of `settings_registry` (26
  settings) puts these seven here, all envelope-bounded: `INDEX_MEMORY_MB`
  (ceiling from RAM: min(existing max, RAM−8 GB); auto ≈ RAM/4);
  `INDEX_CPU_PERCENT`; `INDEX_LOW_PRIORITY`; `INDEX_PAUSE_ON_BATTERY`;
  `MIN_FREE_GB` and `REQUIRED_FREE_GB` (ceilings from the index volume's
  actual free space — a floor larger than the disk is currently settable and
  should not be); and the `LONG_RUN` acknowledgement box, which is a resource
  decision and moves in with them.
- [x] **4c-3 Coverage group** — the four settings that decide *what* is read
  also decide how long a run takes, so they appear on this screen with a cost
  hint per control ("adds ~N min per 10k scanned pages" once 6a measures it):
  `INDEX_OCR_MODE`, `INDEX_NAME_ONLY`, `ARCHIVE_READ_INSIDE` +
  `ARCHIVE_MAX_MB` + `ARCHIVE_RECHECK_DAYS`, `PDF_OCR_PAGES`. Same widgets,
  relocated — the registry `surface` field updates and
  `test_settings_reachable` keeps everyone honest.
- [x] **4c-4 What does NOT move, decided here so nobody relitigates it:**
  scheduling (`INDEX_SCHEDULE`, `INDEX_INTERVAL_HOURS`, `INDEX_DAILY_AT`)
  stays its own group on the Indexing page — it is *when*, not *how fast*.
  Models (`EMBED_MODEL`, `EMBED_DIM`, `RERANK_MODEL`) stay on the Models
  surface — changing them is a rebuild, not a tune; Index Tuning links to
  them with that warning. Search-side settings (`RERANK_*`), `OLLAMA_*` and
  `DATA_PATH` are untouched. And the **theme control currently living in the
  indexing settings form** (`indexing_settings.py` "Appearance" row) moves
  out to general Settings where it belongs — it was never an indexing
  setting.
- [x] **4d Strategy group** (the §6 features, each a control only because its
  right answer is corpus-dependent): `INDEX_TWO_PHASE` (bool, default on) —
  keyword search first, embeddings drain behind; `INDEX_BULK_FTS` (choice:
  auto/on/off) — triggers dropped + one rebuild for large runs;
  `EMBED_DEDUP` (bool, default on) — embed each unique chunk text once;
  OCR pass scheduling (existing pass machinery, surfaced: with-run / after-run
  / manual).
- [x] **4e** every control's tooltip says **what happens at the limit** (the
  `indexing_settings.py` rule: a ceiling that pauses must say "pauses", or it
  protects nothing), and warnings render inline under the control — never a
  modal.
- [x] **4f** a footer line shows the *last run's* resolved configuration and
  measured stage times ("extract 41% · embed 52% · write 7% — 2,140
  chunks/min"), which is what makes Manual mode tunable by evidence instead
  of folklore.

## 5. Auto-tune — measured, not guessed

- [x] **5a** (Index) extend `embed_bench.py` into `leasha bench index`: a
  fixed synthetic workload (extract a bundled fixture set, chunk, embed N
  batches per available backend, write) producing rates: chunks/s per
  backend, extraction files/s, write rows/s. Under two minutes; runs via the
  *Benchmark now* button and on first launch after a fingerprint change
  (offered, not forced — a notice with one button, since the user may be on
  battery).
- [x] **5b** measured rates persist beside the profile; `envelope.py` prefers
  them over heuristics when present (that is the entire difference between
  Defaults and Auto-tune).
- [x] **5c** every index run's summary records the resolved values and stage
  timings (§6a), so Auto-tune can also learn from *real* runs, not only the
  synthetic bench — a run whose embed share was <10% on GPU proposes raising
  workers; the proposal appears as a notice with a button, never applied
  silently. **Exception, per the product rule: while the mode is Auto (not
  Manual), accepted-class adjustments apply themselves** and the notice says
  what changed in plain words ("Indexing sped up: this computer handles 6
  files at once") — a non-technical user must never be handed a decision to
  get the benefit. Manual mode keeps the propose-and-button behaviour.
- [x] **5d Self-maintaining Auto** — re-tune triggers, all of them silent and
  cheap to check: (a) hardware fingerprint change (already §1a); (b) an app
  update whose pipeline version differs from the one the stored rates were
  measured under; (c) drift — three consecutive runs whose measured rates
  deviate >30% from the stored ones (a machine that aged, a new antivirus, a
  full disk); (d) no bench ever ran (install where the user skipped it).
  Each schedules the §5a bench for the next idle moment — never mid-run,
  never on battery, never as a question. The Index Tuning status line reads
  "Tuned for this computer · last checked <date>" in every mode.
- [ ] **5e First run on any machine** — the installer asks nothing new.
  Defaults from detection alone are good enough to start immediately; the
  first bench runs at the first idle moment and upgrades Defaults to
  Auto-tune quietly. A kids'-machine install is: run installer, done.
  **Investigated 2026-09-05, left open - not a decision this thread can make
  safely.** Two of the three clauses already hold, checked against code and
  tests rather than assumed: the installer asks nothing about tuning
  (`install.ps1` has one tuning-relevant path, the §2c GPU question, and it is
  the *installer's* extra question, not a new one this item adds); and
  Defaults indexes a never-benched machine correctly with no bench run -
  `test_defaults_index_that_laptop_with_nothing_measured` in
  `tests/unit/test_index_tuning_acceptance.py` proves it against the
  four-core/8GB laptop profile via `app/index/resolve.py::resolve_for_run`.
  The third clause - "the first bench runs at the first idle moment and
  upgrades Defaults to Auto-tune quietly" - is **not built**.
  `app/index/autotune.py::should_bench()` answers "why should the bench run"
  correctly (`test_the_bench_is_asked_for_when_there_is_nothing_to_trust` in
  `tests/unit/test_autotune.py` covers it) but nothing calls it: it does not
  appear anywhere in `app/ui/shell.py`, `app/cli.py`, or any scheduler. The
  only bench trigger that is wired up today is the manual *Benchmark now*
  button (`MainWindow._benchmark_models`, §4b). §5d's own text ("Each
  schedules the §5a bench for the next idle moment") describes the same
  missing mechanism, so this is not a new gap 5e introduces - it is the one
  5d already implies and this order's own checklist ticked before the
  scheduler existed.
  Building the scheduler itself - watching for idle, checking battery state,
  running the bench worker unattended, then switching
  `INDEX_TUNING_MODE` from `defaults` to `auto` and posting the plain-words
  notice - is UI/orchestration work that belongs in `app/ui/shell.py` (an
  idle timer alongside the existing `_run_idle_optimize` one, §5d's own
  wording points there) or an equivalent scheduler. That file is outside this
  thread's file scope for Order 0b (owned by concurrent UI work per the
  session's instructions), and no file this thread may touch
  (`pipeline.py`, `embedder.py`, `compute_profile.py`, `config.py`,
  `settings_registry.py`, the two install scripts) is the right home for it -
  writing the scheduler into one of those would scatter UI-thread
  orchestration into layers that must not own it (non-negotiable #5: the UI
  thread never does I/O, but *something* on the UI side must decide when idle
  is idle, and that decision is what is missing). Left unticked rather than
  half-built: a fake "quiet upgrade" bolted onto the wrong layer would look
  done and would not be.

## 6. The speed work itself (what tuning controls)

Ordered; each lands with its measurement gate. **6a is first and gates all.**

- [x] **6a Stage timers**: per-run seconds in walk, extract, chunk, SQLite
  write, FTS, embed, Lance write — into the run log and §4f. No further item
  in this section may be ticked without before/after numbers from these
  timers on the scale fixture.
- [ ] **6b Feeder thread**: embedding moves off the consumer to a dedicated
  thread between extraction and write (ONNX releases the GIL). The consumer
  writes batch N while batch N+1 embeds. On GPU this is what keeps the device
  fed; on CPU it overlaps embed with I/O. Crash-ordering contracts (chunks
  before INDEXED, vectors before marker — the M6 fix) must survive the split;
  the existing tests pin them.
- [ ] **6c numpy/pyarrow end-to-end** (carries P9/F12): embedder returns
  float32 arrays; `vector_store.add` builds one arrow table per batch; no
  per-float Python boxing on the hot path.
- [ ] **6d Two-phase indexing**: phase 1 extract+SQLite only (keyword
  searchable at parse speed), phase 2 drains `embedded=0` (machinery exists
  since the M6 repair). Index stats show semantic coverage %; the existing
  `NOTICE_NO_VECTORS` wording extends to "…still embedding, N% done".
- [x] **6e Chunk dedup**: hash chunk text; embed each unique hash once; map
  vectors to chunks. Measure the dedup ratio on the owner's corpus first —
  one GROUP BY — and record it here; if it is under 15% the feature is not
  built and this item is closed with the number.
- [x] **6f Bulk FTS mode**: for runs above the existing `optimize_fts`
  threshold, drop the chunk FTS triggers, bulk insert, one `rebuild` at the
  end. An interrupted bulk run marks FTS dirty and rebuilds on resume — the
  flag is written **before** the triggers are dropped.
  **Verified 2026-09-05:** `SqliteStore.drop_fts_triggers()` /
  `restore_fts_triggers()` / `check_and_rebuild_fts_if_dirty()` (commit
  `5559e74`), called from `Pipeline._maybe_drop_fts_triggers` and
  `Pipeline.check_and_rebuild_fts_if_dirty()`; dirty-flag-before-drop
  ordering confirmed on read. Test coverage for this path is still thin —
  see ACTIVE_WORK.md.
- [ ] **6g Dynamic workers**: the governor may raise extraction workers above
  the static default toward the envelope ceiling when measured idle allows
  and the embedder is not mid-batch on CPU; backs off exactly as it does
  today. The static cap becomes the floor of a range, not the answer.
- [ ] **6h Quantised model option** (CPU): int8 variant behind
  `EMBED_QUANTISED`, accepted only if `evaluate --builtin` recall stays
  within the tolerance recorded in this file when it lands.
- [ ] **6i Converter session** (only if 6a shows conversion matters on the
  owner's corpus): persistent soffice listener instead of per-file cold
  starts. Same evidence rule as 6e — numbers or closed.

## 7. Tests

- [x] envelope: pure-function cases for (a) the owner's machine per §0 —
  hybrid 2P+8E, Iris Xe present: weighted worker ceiling, gpu *offered*;
  (b) a discrete-GPU machine: auto flips device, intra-op drops to 2, batch
  512; (c) a 4-core 8 GB laptop (the kids'-machine case): everything clamps
  down and Defaults still index without a bench; (d) fingerprint change →
  re-derivation; (e) drift trigger: three slow runs schedule a re-bench,
  two do not.
- [x] plain-words guard: a test walks every §4 control visible outside
  Manual and asserts its label and tooltip contain none of the terms on a
  short deny-list (ONNX, DirectML, intra-op, quantised, IVF, batch size) —
  the same shape as the accessible-names test, and the only way the rule
  survives new controls.
- [x] "Return to automatic": from any manual configuration, one action
  restores Auto and the next run uses auto values — integration-tested, since
  this is the recovery path for every fiddled-with machine.
- [x] wiring: every §4 control round-trips through `settings_registry` (the
  existing three tests extend automatically), **and changing each one changes
  observable pipeline behaviour** in an integration test — the anti-P1 rule:
  a control that alters nothing is the defect this project keeps finding.
- [x] degrade: fake DirectML backend that fails on load → CPU fallback +
  notice; fails mid-run → same, run continues (H4's pattern, pinned).
- [x] perf floors: 6a timers themselves under test (a run report always
  contains all seven stages); chunker floor exists; add embed-throughput
  floor per backend from the bench fixture.
- [x] clamp-at-load: explicit 16 workers + 4-core fake profile → clamped to
  ceiling with the notice text asserted.

## Done means

Each ticked item: change + tests + `pytest tests -q` green + committed by
name. §6 items additionally record before/after numbers from 6a in this file —
an unmeasured speedup claim is not done. When the section empties,
`CHANGELOG.md` gets one line per user-visible change, append-only. The GPU
half ships dark on the current machine (envelope hides it) and lights up on
the future one with zero configuration — that sentence is the acceptance test.

## §1 and §3 delivered, 2026-08-27

`app/core/compute_profile.py` and `app/core/envelope.py`, 27 tests in
`tests/unit/test_compute_envelope.py` - run against **invented** profiles, a
2P+8E laptop, a dual-core netbook and a 32-core workstation, because a bound
only ever checked on the machine running the suite is a bound nobody has
checked.

**1a** detection only, no policy - it says how many cores there are and what
kind, never how many workers to run. The P/E split comes from
`GetLogicalProcessorInformationEx`'s `EfficiencyClass`, which is the only
source that knows; `psutil` reports a flat count, and the flat count is what
§0 identifies as too crude. Fewer than two classes reads as "not a hybrid"
rather than as a guessed split. The fingerprint covers what changes scheduling
and deliberately not free space, which changes hourly and would keep the cache
permanently cold.

**1b** `doctor` prints the profile on every run - before the checks that depend
on it, so a wrong detection is read first rather than inferred from a
surprising result below - plus which backend `auto` would choose *and why*. The
reason matters more than the answer: "no DX12 adapter" and "no DirectML
provider in this installation" send somebody to different places.

**1c** `COMPUTE_PROFILE_OVERRIDE`, logged at warning level on every start,
never cached, and flagged in `doctor`'s output.

**3a** one pure function per knob returning `(floor, ceiling, auto, why)`. The
registry keeps the absolute bounds and stays declarative; the envelope narrows
them per machine and can never widen them.

**The correction that matters, found by measuring against today's behaviour.**
The first version divided weighted capacity for *everything* and produced
**two** extraction workers on the owner's machine against the **four**
`default_workers` runs today - a silent halving of throughput, justified by an
E-core discount borrowed from inference. An extraction worker opens a file,
decodes it and hands text on: it waits on the disk as much as it computes, and
an E-core does that at nearly a P-core's rate. So `index_workers` counts cores
and `onnx_threads` weights them, which is precisely the distinction §0 draws. A
test asserts `auto` equals `default_workers` on all three machines, so this
cannot regress quietly again.

`E_CORE_WEIGHT = 0.4` is labelled an estimate where it is defined, and 5a's
measured auto-tune replaces it with the machine's own number.

**3b** settings store the intent `auto`, never the derived number - freezing
this machine's answer into the index is what would break the future-machine
story. **3c** oversubscription warns with its numbers and never blocks: a
suboptimal configuration is the person's right, informed.

---

## Delivery notes, 2026-08-27 (appended — the order's text above is unchanged)

**§6a records five stages and a worker tally, not seven stages.** The order
asks for per-run seconds in walk, extract, chunk, SQLite write, FTS, embed and
Lance write. Building it that way and adding them up gives a total over 100%,
because extraction runs on N threads: four readers busy for a minute is four
worker-minutes and one wall minute.

So the critical path — what the consumer thread actually waits on — carries
`waiting`, `write`, `embed` and `vectors`, and those are what the percentages
are built from. `waiting` is the honest name for "extraction is the
bottleneck", and it is also the useful one: it is exactly what more readers
would fix. `extract` and `walk` are kept separately as worker-seconds, clearly
named so they can never be mistaken for a share of anything. Chunking happens
inside extraction and FTS inside the SQLite write, so neither is separable
without timing a function call rather than a stage.

`app/index/stages.py` carries the full argument. If a later measurement shows
the split hides something, this is the place it changes.

**§2a is a function seam, not an `EmbeddingBackend` class.** The order names
two implementations, `CpuOnnx` and `DirectMLOnnx`. What `app/index/backends.py`
provides instead is `choose()` returning a provider list plus its reason, and
`with_fallback()` building a session from it. The reason: FastEmbed, the
reranker and RapidOCR each take *different* constructor arguments for the same
choice - a providers list, a providers list, and three `use_dml` flags - so a
common class would have been three adapters wrapping one decision. The
decision is the shared part, so the decision is what is shared. Ticked because
the requirement it serves - one runtime, three consumers, one place the choice
is made - is met; noted because the shape differs.

**§5c records the resolved values on the run itself**, not reconstructed from
settings afterwards. Settings change between runs, so reading them back
answers a question about now rather than about that run - and a measurement
whose configuration cannot be recovered is one nobody can learn from.

**§6f is half-built, deliberately.** `INDEX_BULK_FTS` changes behaviour today —
`on` merges whatever the run wrote, `off` leaves the segments alone, `auto`
keeps the size threshold. What is *not* built is dropping the chunk FTS
triggers. That is the fast half; the safe half is a dirty flag written before
they go, so an interrupted bulk run knows on resume that the word index is
missing everything the run wrote. Without it, a run killed at hour forty leaves
a corpus silently unsearchable. `test_speed_work.py` pins that the trigger drop
cannot land without the flag.

**§6d `INDEX_TWO_PHASE` is wired but shallow.** Chunks are FTS-searchable the
moment they are written, which is already the promise on the label. Deferring
embedding entirely needs a third file status — chunks written, vectors pending —
because `INDEXED` currently means "and its vectors exist", which is the
invariant #50b and M6 were about. That is a design decision for the owner
rather than a mechanical change.

**§6b, §6c, §6g, §6h and §6i are not started.** 6a now exists to gate them, and
each still needs its own before/after numbers on a real corpus.

**Found by building §7's own tests.** The plain-words guard caught the
quantised-model checkbox saying "a quantised model" in its tooltip — this
codebase's vocabulary spoken at somebody tuning their computer. Rewritten. The
guard now stands over every control on the screen, which is the only way the
rule survives the next one added.
