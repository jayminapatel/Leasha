# Work order (One thread): indexing that works

**Doc version:** 1.3 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (`app/core/measured.py` + `app/index/index_bench.py` +
`app/core/envelope.py` + `app/index/embedder.py` + `config/extractors.toml` +
the close path in `app/ui/shell.py` and `app/main.py`)
**Status:** RELEASED by the owner 2026-09-19, raised from a live fault: the
Indexing tab said *"55 days"* for a 114,614-file corpus. Most of it is built in
the same session it was written; what is still open is named in §1 and §9.

**The fault, as observed.** A run started 2026-09-18 20:33 reported about 55
days remaining, indexed at about 1.4 files a minute, and could not be closed:
the window vanished (`closing: took 0.0s`) and the process carried on
invisibly until it was killed from Task Manager.

**The first diagnosis was wrong, and this order exists partly because of that.**
The venv had lost DirectML to a CPU wheel (see §7) and that was reported as the
cause. Measured afterwards, DirectML on the Intel Iris Xe is **no faster than
the processor** (about 7 against 8 passages a second), so restoring it changed
nothing. The real causes are §2–§4, and every one of them was found by running
the indexer, not by reading it.

---

## 0. THE EVIDENCE — read this before changing anything

**The ETA was honest.** `format_eta` is remaining ÷ recent rate. 114,614 files
(the saved scan) at about 1.4 files/min is about 55 days. The defect was the
rate.

**Where the last run's time went** (`last_run_stats`, run of 2026-09-17/18,
elapsed 97,444 s):

| Stage | Seconds | Share |
|---|---|---|
| `embed` | 85,162 | 87% |
| `write` | 8,881 | 9% |
| everything else | ~3,400 | 4% |

48,342 vectors in 85,162 s is **0.57 a second**. Extraction was idle: 24,841
worker-seconds across four workers in 97,444 s of wall time is a **6.4% duty**.

**What the settings actually were** (`resolved`): `workers 4, batch 512,
device auto, threads 1`. Neither `512` nor `1` was chosen by anybody.

**Where `batch 512` came from.** `compute:measured` held
`embed_per_second.cpu = 106,666,662`. `index_bench._time_embedding` timed
`embedder.embed_all(sample)` — a **generator** — without iterating it, so it
measured how long a generator takes to create. `embed_batch_from_rates` reads
any rate of 20 or more as "fast enough that per-call overhead is the limit" and
doubles the heuristic 256 to 512. The machine was in Auto mode, so the stored
rate was used.

**Where `threads 1` came from.** `envelope.onnx_threads` charged each of the
four extraction workers a whole core, against a capacity of 5.2
(2 P-cores + 8 E-cores × 0.4). 5.2 − 4 leaves 1.2, so **one thread for the model
that was 87% of the run**, while the workers it was protecting were busy 6% of
the time.

**Measured, same 90 real documents (1,274 chunks), scratch index, one machine:**

| Run | Wall | Chunks/s |
|---|---|---|
| 1 thread, batch 256 (what the real run used), old code | 1,055 s | 1.2 |
| 4 threads, batch 256, old code | 390 s | 3.3 |
| 4 threads, batch 32, old code | 222 s | 5.7 |
| 4 threads, batch 32, **new code, same settings** | 518 s | 2.5 |
| **new code, Auto** (resolved 4 threads, batch 256) | 587 s | 2.2 |

**Read that table for what it does and does not show.** The 4-thread runs span
222-587 s; the row that repeats an earlier configuration on the new code took
2.3 times as long. This is a 15 W laptop chip (i7-1365U, 2 P-cores + 8 E-cores,
Balanced power plan) with the owner's own applications running - `SCANPST.EXE`
was using about 0.7 of a core while the last run finished - so **run-to-run
noise on this machine is about 2.5x and no in-run timing here is better than
that.** What survives it: every 4-thread run beat the one 1-thread run, and
Auto now resolves 4 threads unaided.

**The model-call size was measured separately, because the runs above cannot
isolate it.** Interleaved in one process, 4 threads, 81 real passages (average
2,280 characters), three rounds: a model call of 32 gave 4.25, 4.22 and 3.98
passages a second; the default of 256 gave 3.42, 3.96 and 3.86. **About 10%, not
the 1.8x an earlier reading of the table above suggested** - that reading
credited the batch for what was noise. What the cap does buy plainly is memory:
at one thread, 512 passages took 173.8 s and **4.4GB** at the default and about
the same speed at 32 in **1.25GB**.

**Presentations.** All 79 of 79 `.ppt` files in the index were
`ERR_CONVERTER_FAILED`, none indexed, each costing 4–19 s of LibreOffice
start-up. `.doc` worked (69 of 94). The `.ppt` rule was copied from `.doc`:
`--convert-to txt:Text`, which is a **Writer** export filter; Impress has none,
so `soffice` exited 1 and wrote nothing.

**Closing.** Log of the hung run, all times 2026-09-19:

```
12:23:53.496  closing: stopping timers and background work
12:23:53.510  closing: took 0.0s
12:24:01 ... 12:26:57   soffice conversions still logging, no window
                        (process at 0 CPU afterwards, 184 threads, 8.9GB)
```

`IndexingView` runs its worker in its **own** `QThreadPool`;
`MainWindow._drain_workers` waited only on the global one, saw nothing, and
returned at once. The same shape is in the run of 2026-09-17: `closing` at
11:27:20, the indexer still working at 20:18, and the event loop returning only
at 20:19:29 — nine hours later. `_extract_worker` also looked at the stop flag
**only when its queue was empty**, and the queue is bounded and kept full.

**What was not established.** *Why* the event loop stays alive after
`closeEvent` while a run is in the pool. The process was stopped before a stack
could be taken. §6c exists so the next occurrence names itself.

**`.env` has `EMBED_DEVICE=cpu`.** Nothing in the application sets it: the
Compute box on the Tuning screen writes the value it shows, so it was chosen as
"Processor". With DirectML no faster on this machine that is a reasonable
choice, and this order leaves it alone.

---

## 1. What must be true when this is done

- [x] Auto gives the embedding model 4 threads and a model call of 32 on the
      owner's machine, chosen by the envelope and not by hand.
  > **2026-09-19:** §3 and §4. Verified by running the new code in Auto mode
  > against the scratch corpus: the run log reads *"about 4.2 core(s) are left
  > once 4 worker(s) are running"* and `resolved` records `threads 4`. **The
  > speed-up itself is real but its size is not pinned** — see the table in §0.
- [x] No stored measurement that cannot be true can steer a setting, including
      the one already in the owner's database.
  > **2026-09-19:** §2b refuses it where it is *read*, so the existing
  > `106,666,662` is ignored the next time the record loads; no migration.
- [x] Every `.ppt` is read, not skipped.
  > **2026-09-19:** §5a. Four of the owner's previously failed files converted
  > (exit 0, 1,021–9,651 characters) through `extract_via_converter` itself.
- [x] Closing the window waits for an index run, and a stopped run stops.
  > **2026-09-19:** §6a and §6b.
- [ ] A window that has closed never leaves a process running for hours.
  > §6c is built: stacks are logged at 30 s and the process is ended at 300 s.
  > It is the *backstop*. §6d, the reason the event loop stays alive at all, is
  > open.
- [ ] **On the owner's real corpus, the ETA on the Indexing tab has fallen from
      "about 55 days" to a figure that matches the measured rate.** Needs the
      owner's machine and a run of a few minutes — see §9.

## 2. The rate that was not a rate

- [x] **2a.** `index_bench._time_embedding` iterates what it times.
  > **2026-09-19:** `deque(embedder.embed_all(sample), maxlen=0)` drains the
  > generator without holding a vector. `embed_all` is lazy on purpose, so the
  > fix is at the call site and not in the generator.
- [x] **2b.** A rate outside what a real measurement can be is refused on read.
  > **2026-09-19:** `measured.plausible_embed_rates`, applied in
  > `Measured.from_dict` — the one place every load goes through — with
  > `MAX_PLAUSIBLE_EMBED_PER_SECOND = 5,000`. Drops non-numeric, non-finite,
  > zero, negative and over-ceiling entries, and logs each once at WARNING. The
  > rest of the stored record survives. The ceiling is a ceiling on belief, not a
  > target.

## 3. The model gets its cores

- [x] **3a.** `envelope.onnx_threads` charges an extraction worker what it uses.
  > **2026-09-19:** `EXTRACT_WORKER_DUTY = 0.25`, four times the larger of the
  > two measured duties (6.4% real run, about 1% scratch run). On the owner's
  > machine 5.2 − 4 × 0.25 = 4.2, so 4 threads. A two-core machine still gets 1,
  > and more workers still mean fewer threads. `oversubscription_warning` is
  > unchanged; it still counts each worker as a whole thread, which is the
  > cautious direction to be wrong in.

## 4. The model's call size

- [x] **4a.** On a processor, one model call is at most 32 passages.
  > **2026-09-19:** `Embedder._call_options` passes
  > `batch_size=CPU_INFER_BATCH` to `model.embed`. fastembed splits into calls of
  > 256 whatever `EMBED_BATCH` says — the same silent re-split the code's own
  > note on `EMBED_BATCH` describes from the other side — so this is what the
  > model actually receives. The graphics card keeps fastembed's default; nothing
  > measured it wanting less. `EMBED_BATCH` still sets how many the pipeline
  > gathers before writing, and is untouched. Worth about 10% in a controlled
  > interleaved test (§0) and, above all, about a third of the memory — the
  > resource governor pauses the whole run on memory.

## 5. Presentations

- [x] **5a.** `.ppt` is converted to `.pptx` and read by the reader that exists.
  > **2026-09-19:** `config/extractors.toml`:
  > `--convert-to pptx`, `produces = "{stem}.pptx"`, `then = "pptx"`, with a dated
  > comment above the rule. `.doc` is untouched (`txt:Text` is right for Writer).
- [x] **5b.** The 79 `.ppt` rows already recorded as `SKIPPED` are read.
  > **Open, and deliberately not done from here.** A settled skip whose date and
  > size have not changed is never re-attempted
  > (`pipeline._classify`, the `settled` branch), so the fix does not reach them
  > by itself. One run of `venv\Scripts\python.exe -m app.cli index
  > --retry-skipped` does; it retries every settled skip (118 in the owner's
  > index), which is cheap. It writes to the owner's real index, so it is theirs
  > to run.
  > **2026-09-20: proved, by a different route than the one above.** A read-only look at
  > the real index finds 107 files (103 `INDEXED`, 2 `NAME_ONLY`, 2 `SKIPPED` image-only
  > PDFs) and **no `.ppt` rows at all**: the index was reset on 2026-09-19 (§11), so the 79
  > `SKIPPED` rows the item describes no longer exist, and `--retry-skipped` is not needed
  > for them. What the item is really about - that a `.ppt` is read - holds: LibreOffice is
  > at `C:\Program Files\LibreOffice\program\soffice.exe` (not on `PATH`) and was found, and
  > two real `.ppt` copies converted and read (10.3 s / 456 characters, 5.0 s / 405
  > characters). Any `.ppt` indexed from here on is read, not skipped.

## 6. Closing a window

- [x] **6a.** Closing waits for an index run, with its own grace.
  > **2026-09-19:** `IndexingView.pool` exposes the private pool;
  > `MainWindow._drain_workers` waits on both, the global pool for
  > `SHUTDOWN_GRACE_MS` (4 s) and the index pool for
  > `INDEX_SHUTDOWN_GRACE_MS` (30 s). The window is already hidden, so the wait is
  > invisible; the single-instance lock is what it costs.
- [x] **6b.** A stopped extraction worker stops at its next item.
  > **2026-09-19:** `_extract_worker` checks `_stop` after every `get`, not only
  > on `Empty`.
- [x] **6c.** A closed window that leaves a process is diagnosed, then ended.
  > **2026-09-19:** `app/ui/exit_watchdog.py`. Armed by `main()` only, through the
  > `after_close` hook on the window, so the test suite (which calls `closeEvent`
  > constantly) can never end its own session. At 30 s every thread's stack is
  > written to the run log; at 300 s the process exits via `os._exit`. Both timers
  > are daemons. **A design decision worth the owner's eye:** the hard exit skips
  > the store closes that `_exit_fast` normally waits for, so it is the same loss
  > as a Task Manager kill — which it replaces — and only after the index run has
  > had its 30 s to stop.
- [ ] **6d.** Find out why the event loop stays alive after `closeEvent` while a
      run is in the pool.
  > **Open.** Not reproducible from here. The next occurrence will log the stacks
  > (§6c); read them first and do not reason ahead of them.
  > **2026-09-20: still open, but one mechanism is found and closed, and one is ruled
  > out as the cause.** Measured with plain PyQt6 offscreen (no application code): closing
  > the main window with a **second visible top-level widget** left `exec()` running until
  > something else called `quit()` (3.0 s, the experiment's own timer), against 0.2 s
  > without one. `PreviewWindow` (pop-outs), `LogWindow` and `MiniSearch` are all
  > parentless top-level widgets, so any one left visible keeps the loop alive with the main
  > window gone. `MainWindow.closeEvent` now closes every other visible top-level window
  > (`app/ui/close_windows.py`, one line in `shell.py`); `test_close_ends_the_app.py` builds
  > the real `MainWindow`, starts a real index run in the view's pool, shows a `LogWindow`
  > and a bare window, closes, runs a real event loop and requires it to end by itself - it
  > fails (the backstop timer ends it) with the fix removed. **A second mechanism was also
  > reproduced and is not the cause:** deleting an object that owns a `QThreadPool` blocks
  > the main thread in `~QThreadPool` for the length of the run (6.0 s for a 6 s job), but
  > `MainWindow` is not `WA_DeleteOnClose` and nothing deletes the view inside `exec()`.
  > **What this does not explain:** the 2026-09-17 incident had no visible pop-out. The
  > watchdog (§6c) stays as the backstop and the box stays unticked - do not tick it until an
  > occurrence has been read.
- [x] **6e.** A stop that arrives mid-batch finishes the batch it is in.
  > **Open, and smaller than it was.** Each model call is now at most 32
  > passages (about 6 s at 5.7/s), but the pipeline still embeds everything it
  > has gathered — up to `EMBED_BATCH`, 256 — before the run can report stopped,
  > because vectors are written before an archive's completion marker (M6) and an
  > abandoned batch would leave a marker with no vectors. Interrupting inside a
  > batch needs that ordering redesigned, not a flag.
  > **2026-09-20: done - and the ordering did not need redesigning, only guarding.**
  > `Pipeline._embed_pending` now embeds in slices of one model call (`CPU_INFER_BATCH`, 32;
  > the graphics card keeps whole batches) and looks at the stop flag before each. When a
  > stop arrives it writes vectors for **whole files only**, marks only those `INDEXED`
  > (a file cut in half is left `PENDING`, the state a crash has always left, which the next
  > run redoes), and sets `_embed_abandoned`. That flag holds back the two things that would
  > otherwise claim the abandoned work: an archive's completion marker (`_consume`, after its
  > blocking `_feed_sync`) and the resume positions (`_persist_resume_progress`). A stopped
  > run therefore costs at most one model call (about 6 s here), not the up-to-256-passage
  > batch. `tests/unit/test_stop_mid_batch.py`: one call not the batch; only whole files
  > `INDEXED` and every one has a vector; a resume ends with every passage embedded once (the
  > fake store asserts no chunk is added twice); no marker and no saved position after a
  > cut; and the same in `test_close_ends_the_app.py` through the real window. Each guard was
  > removed in turn and its test went red. **Not covered:** the image (CLIP) flush is a
  > separate path and is not sliced; `stats.indexed` still counts files whose vectors were
  > abandoned (they are `PENDING` in the database and redone).

## 7. The venv

- [x] **7a.** The documented optional install no longer un-pins onnxruntime.
  > **2026-09-19:** `requirements.txt` — the face-detection command ended in a bare
  > `onnxruntime`. With only `onnxruntime-directml` installed pip does not count
  > that as satisfying `onnxruntime`, so it fetched 1.30.0 (CPU) over the DirectML
  > files on 2026-09-15, four days after 0t pinned it. Now
  > `onnxruntime==1.24.4`, with a dated note.
- [x] **7b.** The owner's venv is repaired.
  > **2026-09-19:** plain 1.24.4 then DirectML 1.24.4 last, both
  > `--force-reinstall --no-deps`; `get_available_providers()` lists
  > `DmlExecutionProvider`; `pip check` reports nothing about onnx.

## 8. Tests

- [x] Each cause above has a test that fails on the code as it was.
  > **2026-09-19:** `tests/unit/test_indexing_that_works.py` (25 tests: the
  > implausible rate refused and healed on read, the bench consuming what it
  > times, four threads on the owner's machine, the CPU call cap and the GPU
  > default, the `.ppt` rule and the reader it names, the watchdog dumping then
  > exiting, and that only `main()` arms it) and
  > `tests/unit/test_close_waits_for_index_run.py` (3 tests: the drain waits on
  > the index pool, gives up at its grace, and a stopped worker stops with a full
  > queue). Confirmed red with the source reverted.
- [x] Run the tests that touch what changed, and compare every failure with the
      untouched baseline.
  > **2026-09-19:** the **whole suite was not run** - it is far too slow on this
  > machine to finish in a session (about 5% in 15 minutes). Instead: the 93
  > files that reference a changed module, then 30 of the closest serially.
  > Running four in parallel produced 723 failing test ids against 616 on the
  > baseline and every difference disappeared when re-run alone, so **parallel
  > numbers here are noise**. Serially, 27 of 30 pass. `test_embedder` failed and
  > *was* this work's: three fakes of fastembed's `embed(texts)` rejected the new
  > `batch_size` argument - fixed by giving them fastembed's real signature.
  > `test_speed_work::test_the_triggers_are_not_dropped_until_the_dirty_flag_exists`
  > and two in `test_converter_discovery` fail identically on the untouched
  > commit `75690a8`, as do three archive-path tests
  > (`\backup.zip` against `/backup.zip`) - none are this work, and none are
  > fixed by it.

## 9. What cannot be checked from here

- **The ETA on the real corpus.** The scratch runs used 90 documents; the corpus
  is 114,614 files with PSTs, archives and scans in it. The Indexing tab's own
  figure after a few minutes is the readout. Expect it to fall a great deal and
  not to reach the scratch run's 5.7 chunks/s.
- **§6d.** Needs the next hang and its log.
- **How much faster a real run is.** The scratch runs varied 2.3x between
  near-identical configurations on this machine (§0), so the honest statement is
  "faster, by somewhere between about 1.8x and 4.8x". A number needs the machine
  quiet, and several runs.
- **Whether more than 4 threads or a call below 32 is faster still.** Not
  measured; 4 and 32 are what was tested.
- **Anything on the graphics card.** DirectML was measured slower than the
  processor on this machine's integrated GPU, at one batch size.

## 10. Not in this order

- **The ETA wording.** It says "about N days" for any rate, however small; a
  rate that low deserves a plainer sentence than a number of days. Separate.
- **Which processor to use.** `EMBED_DEVICE` stays as the owner set it.
- **The memory ceiling.** The resource governor paused the last run 47 times
  (344 s). With a model call of 32 the footprint drops; whether the default
  ceiling is then right is a measurement, not an assumption.

## 11. Applied, and the index reset

- [x] Merged to `main` (fast-forward, `63e14ed`) and so live in the checkout the
      application runs from.
- [x] The existing index was cleared, **by moving it, not deleting it.**
  > **2026-09-19:** `D:\Leasha\Datats`, `vectors`, `cache` and
  > `completions.json` moved to `D:\Leasha\Data-index-backup-20260919-1435`
  > (141 MB; 1,425 files, 30,033 chunks, 13 tag rows, 3 messages, no saved
  > searches or face piles). `models` was left in place. A fresh database was
  > created by `SqliteStore` and given back the 39 settings worth keeping: every
  > `ui:*` key (the five folders, tray behaviour, columns, window geometry) and
  > `scan:last`, so the first run has its 114,614-file total and a real ETA. The
  > stored `compute:*` records - including the impossible rate - were not carried
  > over. `PRAGMA quick_check` on the new file: ok. `doctor.py --quick`: READY,
  > no failures, onnxruntime install coherent, DirectML available. Delete the
  > backup folder when satisfied; nothing needs it.
- [x] The tuning parameters were reviewed against measurement.
  > **2026-09-19:** Auto on `main` resolves 4 workers, 4 ONNX threads, batch 256
  > (a call of 32). `EMBED_DEVICE=cpu` stays: DirectML on the Iris Xe measured no
  > faster. `INDEX_CPU_PERCENT=80`, `INDEX_MEMORY_MB=16000` and the OCR settings
  > were read and left; nothing measured argues for a change. **One warning will
  > appear on every run** - `REQUIRED_FREE_GB=300` against 217GB free. It is a
  > warning, not a block (the run stops cleanly at the 5GB floor), and it is the
  > registry default; the index is expected to be a small fraction of the
  > corpus's 249GB, so lowering it is reasonable and was not done unasked.
- [x] **Decide `EMBED_QUANTISED` before the first index run.**
  > **Decided 2026-09-19: ON**, at the owner's instruction ("do embed now"), with
  > the index empty. Set through `env_writer.apply_values` (one line changed in
  > `.env`; a copy is at `.env.before-int8-20260919`). Verified by building the
  > embedder from the real `.env` and reading the ONNX session's model path:
  > `models\BAAI--bge-small-en-v1.5-int8-local\model_optimized.onnx`, CPU, 384
  > dimensions, unit norm. **To go back costs a full rebuild** - vectors from the
  > two models must not be mixed. The measurements behind the choice:
  > measured 2026-09-19, 81 real passages, 4 threads, interleaved: the int8 model
  > ran **1.85x faster** (4.4-5.3 against 2.5-2.8 passages a second). It also
  > **changes results**: mean cosine to the fp16 vector 0.974 (worst 0.950),
  > top-5 neighbours overlap 76%, and the #1 neighbour differs for 16 of 81
  > passages. Without ground-truth relevance that means *different*, not *worse*
  > or *better*. The local int8 copy is already built
  > (`models\BAAI--bge-small-en-v1.5-int8-local`). Switching later invalidates
  > every vector, so **now, with an empty index, is the only free moment.** Left
  > off pending the owner's decision.
