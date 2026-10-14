# Work order (Parallel threads): the indexing cockpit, nothing silent, and one conductor for every thread

**Doc version:** 1.1 · **Updated:** 2026-10-14 · **Applies to:** app v1.0.3
**Thread:** Four parallel threads - lanes (`app/index/live_progress.py`, `pipeline.py`), the cockpit
(`app/ui/indexing_view.py`, `app/ui/widgets/`, `app/ui/presenter/`), the found faults (Files and Photos side
lists, file kinds, icons), and the conductor (`app/index/conductor.py`, `pipeline.py`), which starts on top of
the lanes branch. One master thread merges and tests them together.
**Status:** RELEASED by the owner 2026-10-14 ("go with a, build the cockpit, and write the order ... c do it
... coordinate this and do it all, the rest you get the intent do the recomended"), after asking why only one
of four threads worked while many photos had no faces.

## Why

The owner saw one thread busy, three idle, and thousands of photos with no faces. Read, not guessed, on
2026-10-14:

- **The photos never reached the face step.** `INDEX_OCR_PASS` was `after-run`, so every scheduled run is the
  text pass, which files a picture as `ERR_OCR_HELD` and runs no picture step on it. The images pass starts
  only after a text pass finishes whole, and `index:images_pass_due` was not set. The owner's index held
  **36,505 photos as `ERR_OCR_HELD`, none face-scanned** (read-only query of `files` and `face_scans`).
- **The "workers" setting is readers only.** Four extraction threads read files; faces, picture search and the
  duplicate fingerprint run on one `pictures` thread; meaning on one `feeder`; writing on the consumer; and the
  end of a run is about ten passes one after another on the run's own thread. Readers are busy 6.4% of a long
  run (`envelope.py`), the meaning model 54,555 s of a 15-hour run against 1,568 s of writing.
- **Most of that work is silent.** Only readers have a slot on the page. The pictures thread, the feeder, the
  consumer, the walker and every end-of-run pass show at most a phase sentence.
- **Dynamic reader growth never runs**: `PipelineConfig.worker_ceiling` defaults to 0 and nothing sets it.
- **Faces on the processor scale with photos in parallel, measured.** 40 of the owner's photos, below-normal
  priority, the owner's own run going at the same time, face pack on the processor (4 intra-op threads):
  1 thread 0.16 photos/s, 2 threads 0.37, 4 threads 0.66 - about 4x (the 1-thread figure carries some warm-up;
  read it as 3-4x). Per-photo model time stays 1.6-1.9 s however many run. On the graphics card the one
  process-wide lock (`gpu_serialize`) would take most of that back (UNCONFIRMED - not measured, the card was
  the owner's run's).

## Rules (every thread)

- Existing labels, tooltips and descriptions are never reworded; new words only for new things.
- Every module keeps its `Layer:` docstring; comments say why.
- A fault found is fixed with a test that fails without it.
- All coding first; one combined test pass at the end (`CLAUDE.md`, "Parallel, then test together"). Measured
  claims carry what they were measured under; inferred ones are marked UNCONFIRMED.

## 1. The held photos, and the pass schedule

- [x] **1a** The owner's `INDEX_OCR_PASS` set to `with-run` through the app's own settings writer (never by
  hand-editing `.env`), done at go-live with the owner's run stopped.
- [ ] **1b** A test proves a `with-run` run reads a photo the earlier text pass left as `ERR_OCR_HELD`: its
  faces, picture vector and fingerprint are made and the row leaves `ERR_OCR_HELD`.
- [x] **1c** The cockpit says how many files are held and why ("Held for later"), so a queue can never again
  look like nothing.

## 2. Nothing silent - a lane for every thread

- [x] **2a** `IndexStats.lanes`: one plain-value record per lane - `walker`, `pictures`, `meaning`, `writer`,
  `beside`, `pass` - with task, item, path, start time, done, total and device. Readers stay in `workers`.
- [x] **2b** The pictures thread names each step on each photo ("Looking for faces" and so on), and goes idle
  when its queue is empty.
- [x] **2c** Every end-of-run pass (face backfill, face grouping, describing, text in pictures, cards, meaning
  catch-up, tidying) shows on the `pass` lane with its file and, where cheap, a total.
- [x] **2d** Lanes cross the process boundary (`run_events`) and move the heartbeat, so a run doing only faces
  never reads as "No progress".

## 3. The cockpit (Status shelf)

- [x] **3a** Fixed geometry: header with state, headline and controls; a phase stepper; six metric tiles; the
  bar and one detail line; the lanes table; a resource strip; a fixed-height tab panel. Nothing moves or
  resizes because of content - long text elides, empty states show in place.
- [x] **3b** The lanes table has one row per thread, readers dynamic (rows follow the reader count mid-run and
  scroll inside the table), each reader row with its skip-current-file button (today's "Force skip reader N").
- [x] **3c** The tab panel: Run log, System log, Skipped, Held for later, This index.
- [x] **3d** Tests: geometry identical across very different snapshots; dynamic reader rows; skip maps to the
  right reader; old behavioural assertions kept, old position assertions updated and listed.

## 4. The system log on the cockpit

- [x] **4a** A System log tab using the same widget as Settings' "Recent activity", fed also from today's log
  file on a worker thread, so a run in its own process is visible - read only while the tab is shown.

## 5. One conductor for every thread

- [ ] **5a** `app/index/conductor.py`: one budget of processor slots (from `envelope`, leaving the window its
  share) and the one graphics-card slot; lanes ask it for a slot before model work and give it back after;
  priority order meaning > reading > faces and pictures > describing > text in pictures > cards. Work-conserving:
  a free slot goes to the most important ready task. The resource governor stays the brake above it.
- [ ] **5b** The end-of-run passes run side by side under the conductor instead of one after another, keeping
  each photo's own order (faces, then description, then text - the owner's 2026-10-04 order) and the cards last.
- [ ] **5c** Model intra-op threads are sized from the conductor's budget, so the processor is never asked for
  more threads than it has.
- [ ] **5d** Faces get a pool when they run on the processor (sized by the conductor; measured above); on the
  graphics card they stay one at a time behind the existing lock. The shared decode cache and the CLIP batch
  are made safe for more than one picture thread.
- [ ] **5e** Measured before and after on the same corpus and machine: per-lane busy time and wall time of the
  run's end. A change that does not save is reverted and said so.

## 6. Faults found while working

- [x] **6a** "More... (N)" on the Files and Photos side lists did nothing when clicked.
- [x] **6b** Microsoft Project and Visio (`.mpp`, `.vsdx`, `.vsd`) did not appear under Files > Kinds although
  `FILE_KINDS_ALWAYS` names them.
- [x] **6c** Controls without an icon (the Browse page's Thumbnails / Table switch and others): every one given
  its Lucide icon through the tables, and the guard test widened so the next one fails.
- [x] **6d** People's "Seen once" pile raised `ValueError` on right-click - its menu line had no icon.
  *Done 2026-10-14, `d8e9471`, with a guard over every written menu line.*

## 7. Close-out

- [ ] **7a** The full unit suite once, combined, on the merged branch; CI green on `main`.
- [ ] **7b** `HANDOFF.md`, `docs/ORDER_REGISTER.md`, `CHANGELOG.md` and the user guide's Indexing section say
  what is true afterwards.
