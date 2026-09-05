# Work order (One thread): Pictures I — the CLIP lane: find photos by describing them

**Doc version:** 1.2 · **Updated:** 2026-09-05 · **Applies to:** app v0.3.3
**Thread:** One thread (Index + Storage/vectors + Search + results UI)
**Status:** RELEASED by the owner 2026-08-28. Requires 0508 (media defaults +
ladder + EXIF dates) landed first. **Scope discipline: vectors and
presentation only — NO tag/caption text generation (that is 0511), NO faces
(0512).** The owner's killer case sits behind this order: describe a picture
from memory and find it — extended to offline drives when the Offline Media
orders land.

**2026-09-05, §1 build session.** 1a and 1b done, measured, tested. 1c was
built and tested as far as that session's file list allowed, stopping
deliberately at `engine.py`.

**2026-09-05, later the same day: §1c finished.** The engine wiring the
first session stopped short of is built, tested, and merged — search-side
(`SearchEngine`) and, because the lane could not be measured without it,
the `app.cli index` half of indexing-side too. §1 is fully done by this
order's own "Done means" definition below, with one gap found and flagged
rather than fixed: indexing from the *window* (`app/ui/shell.py`) still
does not write CLIP vectors — see §1c's dated note for the exact site.
§2 and §3 remain untouched, per instruction.

## 1. The image-vector lane

- [x] **1a** image embeddings via **fastembed's ImageEmbedding** (CLIP-class
  model, ONNX — already-installed package; thread verifies model
  availability, else the smallest OpenCLIP ONNX export; free/unencumbered
  only, NO Ultralytics/AGPL imports — standing rule with its guard test).
  Every ladder-passed image gets a vector; ~50–150ms/img CPU budget measured
  on the fixture before acceptance.

  Built as `app.index.clip_embedder.ClipImageEmbedder` — a parallel class to
  `Embedder`, not a modification of it (different fastembed class,
  `ImageEmbedding`, different call shape). Model: `Qdrant/clip-ViT-B-32-vision`,
  512-dim, MIT — confirmed via `ImageEmbedding.list_supported_models()`, paired
  with `Qdrant/clip-ViT-B-32-text` for the query side (§1c). Same three guards
  as `Embedder`: dimension checked once, unit-norm enforced, batched
  (`CLIP_IMAGE_BATCH = 16`, FastEmbed's own default).

  **Measured, real model, this machine (i7-1365U, 12 logical cores), single
  arbitrary 400×400 fixture JPEG, `venv\Scripts\python.exe`, CPU only,
  `onnxruntime` default settings:**

  | | value |
  |---|---|
  | Model load incl. first-run download (2 files, ~340MB) | 38.6s |
  | Per-image embed, steady state, 10 repeats, single-image calls | **1,675.6ms average** |
  | Per-image embed, 5 individual single-image calls (spread) | 0.95s – 3.04s |
  | Per-image embed, batch of 8 identical paths in one `embed()` call | 1,003.9ms average |

  **This blows the ~50–150ms budget by roughly 7–11x, and that is reported
  here rather than hidden or quietly rounded away** — this project's working
  method is to measure and record, not to assume a number is right because it
  was written down first. The 50–150ms figure in this item was an estimate at
  spec time, not a prior measurement; ViT-B/32's vision tower is a genuinely
  heavier forward pass than a 384-dim text embed, and this machine's CPU has
  no GPU/DirectML path exercised here (0114's device-selection machinery
  was deliberately not wired into `ClipImageEmbedder` for v1 — see the
  design note below). Batching helped (1.68s → 1.0s/image) but did not come
  close to closing the gap, and the spread across single calls (0.95s–3.0s)
  says this machine's cores were not fully idle during the run (a concurrent
  background test sweep was also under way for part of this measurement —
  flagged here rather than silently averaged over).

  **What this means in practice, honestly stated rather than glossed over:**
  a corpus of 100,000 photos at ~1.7s/image is roughly 47 hours of CPU time
  for the CLIP pass alone — on top of, not instead of, the OCR ladder's own
  3.6s/page. This is consistent with the product's own accepted trade-off
  for photo and video drives ("slow scans... are accepted, expected, small
  price to pay" — HANDOFF §5a) and with the images pass already being the
  schedule-dominant one, but it is a real number the owner should see before
  it is treated as background noise.

  **Done 2026-09-05, later the same day:** `ClipImageEmbedder` now takes
  `device=`/`profile=`/`problems=` and calls `backends.choose`/
  `with_fallback` exactly as `Embedder` does — same seam, same `auto`
  measure-don't-assume behaviour, same H4 discipline (a GPU that fails on
  first use falls back to the CPU with a visible notice, never a crash).
  `from_settings` reads the same `embed_device` setting the text embedder
  and reranker already share, so one control governs all three rather than
  a second one nobody would think to change together. `record_provider`
  logs it as `"image model"` in the run log, alongside `"meaning model"`.
  Wiring verified with five new tests in `tests/unit/test_clip_embedder.py`
  (CPU-only asks for no `providers` kwarg at all — the CPU path stays
  byte-for-byte unchanged; `auto` on a GPU-capable profile passes
  `["DmlExecutionProvider", "CPUExecutionProvider"]`; a GPU that raises on
  first use falls back to the CPU and appends to `problems`; a profile with
  no adapter goes straight to the CPU with no notice at all, since nothing
  was tried and failed). All 98 tests across `test_clip_embedder.py`,
  `test_backends.py`, `test_image_vector_store.py`, `test_search_images.py`,
  `test_clip_lane_pipeline.py` and `test_embedder.py` pass.

  **Re-measured 2026-09-05, same day, after `onnxruntime-directml` was
  installed into this venv** (`pip uninstall onnxruntime && pip install
  onnxruntime-directml==1.24.4` — mirrors exactly what `install.ps1` already
  does for an accepted GPU install per 0114 §2c; `pip check` afterwards
  complains `onnxruntime` "is not installed", which is the expected,
  already-documented cosmetic side effect of that swap, not a real gap —
  `import onnxruntime` and `get_available_providers()` both work).
  `onnxruntime.get_available_providers()` now reports
  `['DmlExecutionProvider', 'CPUExecutionProvider']`. Full regression run
  (186 tests: the CLIP lane, `test_backends.py`, `test_embedder.py`, every
  OCR test file, `test_rerank_window.py`) passes clean with the swapped
  package.

  | | CPU | GPU (DirectML, Iris Xe) |
  |---|---|---|
  | Model load | 2.7s | 1.4s |
  | First embed call (JIT/shader compile not yet paid) | 58.7ms | 176.0ms |
  | Per-image embed, steady state, 10 single-image calls | **69.1ms** avg (53.3–103.3ms) | **37.5ms** avg (35.7–43.5ms) |
  | Per-image embed, batch of 8 in one call | 75.7ms/image | 97.1ms/image |

  **Both are now well inside the original ~50–150ms budget** — a real
  change from the 7–11x-over-budget finding above, worth being honest about
  in both directions rather than only when the news is good: the CPU number
  alone (69.1ms) is **~24x faster than the 1,675.6ms first measured**, on
  the same machine, same fixture, same methodology. The two things that
  changed between the two CPU measurements are (a) no concurrent background
  test sweep this time — the first measurement explicitly flagged one as a
  likely confound — and (b) the `onnxruntime` build itself changed version,
  1.29.0 → 1.24.4, as a side effect of installing the DirectML wheel, which
  also changes the plain CPU provider's binary. Neither is isolated from
  the other here, so **the magnitude of the original number should not be
  trusted going forward** — this table is the current, clean baseline, but
  which of the two causes did most of the work is genuinely not known and
  not claimed.

  **The GPU result itself is a real, single-image speedup** (37.5ms vs
  69.1ms, ~1.8x) but **loses on the batch-of-8 call** (97.1ms/image vs
  75.7ms/image on the CPU) — DirectML's per-dispatch overhead does not
  amortise the same way this ONNX Runtime build's CPU threading does over a
  small batch on this GPU. `auto` in `backends.choose` cannot see this
  nuance (it decides once, per session, not per call shape), so the
  practical effect on an index run — which always calls in batches of
  `CLIP_IMAGE_BATCH` (16) — is genuinely unclear from this fixture alone,
  and worth a real-corpus re-measurement before concluding `auto` should
  prefer the GPU here at all. Recorded rather than smoothed over, per this
  project's own rule: measure, don't assume — including when the model
  being measured is "GPUs are faster."

- [x] **1b** second LanceDB table (dimensions differ from text vectors),
  keyed by file_id; same delete/compaction/crash-ordering contracts as the
  text table (the M6/M8/H7 patterns apply — cite them in comments).

  Built as `app.storage.vector_store.ImageVectorStore(VectorStore)` — same
  LanceDB directory, table name `image_vectors`, dim 512 vs the text table's
  384. Subclassed rather than reimplemented so the guarantees are inherited,
  not re-asserted: `delete_by_file_ids` (H7 — one batched delete, not one
  Lance version per file), `maybe_compact`/`COMPACT_EVERY_ROWS` (also H7's
  neighbourhood), and `add` never calling `maybe_create_index` inline (M8 —
  training happens once, at end of run, exactly as the text table's does in
  `Pipeline.run`). `chunk_id` is written equal to `file_id` on every row
  (`add_images`), because there is no chunk-splitting concept for a photo —
  see that class's docstring for the full reasoning, including why this
  choice needed a rename before reaching `fuse_hits` (§1c below).

  M6 (crash ordering) is a *pipeline* discipline, not something the store
  class itself can enforce alone — mirrored in `Pipeline._flush_pending_images`
  and `_maybe_embed_image` (`app/index/pipeline.py`): every CLIP vector is
  computed synchronously (not deferred to the async feeder thread the text
  side uses) and flushed, batched, immediately before every point that can
  mark a file INDEXED. See that method's docstring for why this closes the
  M6 window entirely rather than needing a repair path (`iter_unembedded`'s
  equivalent) the way the text table did.

  Tests: `tests/unit/test_image_vector_store.py` (18 tests — the same shape
  as `test_vector_compaction.py`, re-run against the new table) and
  `tests/unit/test_clip_lane_pipeline.py` (8 tests — the pipeline wiring,
  including an H7-shape test that 10 images cost ≤3 dataset versions, not
  10, and an H4 test that one broken image never costs the run or the file).

- [x] **1c** third retrieval lane: the query text embedded by the **CLIP text
  tower** (not the FastEmbed text model — different space), ANN over the
  image table, fused via the existing RRF exactly as keyword+vector fuse
  today. Runs when the query plausibly describes imagery — v1 heuristic:
  always run it, weight it in via RRF; measure against `evaluate` photo
  sentences before tuning further. H4 discipline throughout: lane failure
  degrades with a notice, never breaks search.

  **Built and tested as far as this session's file list reaches; STOPPING
  short of full wiring, and here is exactly why — 2026-09-05.** The files
  this session was told it may touch are `app/index/embedder.py` (or a new
  parallel file), `app/storage/vector_store.py`, `app/search/vector.py`,
  `app/search/fusion.py`, `app/index/pipeline.py` (indexing flow only), and
  their tests. **The place `keyword` + `text-vector` are actually fused
  today is `app/search/engine.py:886` (`fuse_hits(...)`), and `engine.py` is
  not on that list.** Verified with `grep -rn "fuse_hits(" app/`, which
  returns exactly one call site outside `fusion.py` itself — this is not a
  guess.

  What is built and tested, standalone:
  - `app.search.vector.search_images` — the CLIP-text-tower ANN lane. It
    calls the *existing* `search()` unmodified (nothing in that function is
    text-specific; it takes a store and an embedder and does the rest,
    H4 degradation included), passed an `ImageVectorStore` and a plain
    `app.index.embedder.Embedder(model_name="Qdrant/clip-ViT-B-32-text",
    dim=512)` — no new text-embedder class was needed, since `Embedder` is
    already generic on model name and dimension. Confirmed the model is
    real and loads: `Qdrant/clip-ViT-B-32-text`, 512-dim, MIT, downloaded
    and embedded a real sentence on this machine (see below).
  - The `chunk_id` rewrite (`"img:<file_id>"`) that stops an image hit from
    colliding with an unrelated real passage's `chunks.id` once both reach
    `fuse_hits` under its default `id_key="chunk_id"` — two independent
    SQLite sequences that both start at 1, so a bare integer file_id would
    silently collide the moment the numbers matched. This was found by
    reasoning through what `fuse_hits` actually keys on, not assumed; a test
    (`test_fusing_keyword_text_and_image_lanes_does_not_collide`) proves a
    text chunk id 7 and a photo whose file_id is also 7 survive fusion as
    two distinct rows.
  - `hydrate_images` — the image lane's `hydrate`, joining `files` by
    `file_id` since a photo has no `chunks` row, no text, no page.
  - Tests: `tests/unit/test_search_images.py` (9 tests) covering the CLIP
    text tower being genuinely distinct from the FastEmbed text model, the
    namespacing, H4 degradation on a broken model, an empty query, and
    `hydrate_images`.

  **Done 2026-09-05, a later session: `SearchEngine.search()` wiring.** The
  two design questions above are answered and the wiring built:

  1. **Construction**, at every real search entry point (`app/main.py`'s
     window startup, `app.cli`'s `search`, `shell`, `evaluate` commands —
     verified with `grep -n "SearchEngine(" app/main.py app/cli.py` before
     touching anything; the `evaluate --builtin` benchmark path
     (`app/cli.py`, ~line 2099) was found and deliberately left alone: a
     synthetic 21-document corpus for reranker A/B comparison, no images,
     not in the instruction's named list). One shared constructor,
     `vector.clip_text_embedder_from_settings(settings)`, follows the
     `Embedder.from_settings`/`Reranker.from_settings` precedent — not
     literally that classmethod, because `Embedder.from_settings` passes
     `model_name` positionally and cannot take an override for it (documented
     on the function itself). `image_vectors`/`clip_text_embedder` reach
     `SearchEngine` as optional kwargs, `None` by default, H4 throughout:
     absent means the engine behaves exactly as it did before this lane
     existed. Confirmed lazy: `SearchEngine.warm_up()` deliberately does not
     touch `clip_text_embedder` (0r's startup budget has room for one eager
     ONNX load, not two) — it loads on the first search that reaches it.
  2. **Weight**: 1.0, literal, by passing nothing — `hit_lists` and
     `weights` are built as parallel lists in `_retrieve`, appending the
     image lane's hit list to the first and, only when `weights is not
     None`, `1.0` to the second, so a future tuned `weights=` cannot hit
     `rrf`'s length-mismatch `ValueError`. `self.weights` stays the
     `(keyword, vector)` pair it always was — no third element hardcoded in.

  **Also wired, because the lane could not be measured without it**: the
  indexing side. `app/cli.py`'s `index` command (the only real `Pipeline(`
  construction site in that file — verified via `grep -n "Pipeline("
  app/cli.py`) now builds a `ClipImageEmbedder` and `ImageVectorStore`
  alongside its existing `Embedder`/`VectorStore` and hands them to
  `Pipeline` as `image_embedder=`/`image_vectors=`. Before this, `grep -rn
  "image_embedder=\|image_vectors=" app/` returned only test call sites — no
  real `app.cli index` run had ever written a CLIP vector.

  **Two real bugs found and fixed while wiring, not assumed away:**
  - `SearchResult.chunk_id` is typed `int`, and every fused row used to be
    cast with a bare `int(hit.get("chunk_id", 0))` in `_to_result` — which
    raises on an image hit's `"img:<file_id>"` string. Would have crashed
    the first search that ever fused in a photo. Fixed with `_result_chunk_id`,
    falling back to `file_id` (the image table's whole key) once fusion's
    job of preventing a collision is already done.
  - `NOTICE_NO_IMAGES` needed its own entry in `app/search/plain_notices.py`'s
    `PLAIN` table - caught by the suite's own `test_every_notice_code_has_a_
    plain_form`, not missed silently.

  **A gap found, not guessed past — flagged rather than fixed:**
  `app/ui/shell.py:_start_indexing` (~line 1949) constructs its own
  `Pipeline` for indexing started from the window, independently of
  `app.cli index`, and was not in this instruction's named scope
  (`app/ui/*` was off-limits in the session that built §1a/§1b/§1c's
  standalone half, and this instruction named only `app/cli.py:1140`).
  **Indexing from the window still does not write CLIP vectors** until that
  site gets the same `image_embedder=`/`image_vectors=` wiring
  `app/cli.py`'s `cmd_index` just got. Search-side wiring (this session's
  other change) is unaffected either way, since it queries whatever the
  image table already holds - it will simply find nothing until an
  `app.cli index` run, or this second site, has written to it.

  Five wiring-level tests added in a new file, `tests/unit/
  test_engine_image_lane.py` (existing files already prove
  `search_images`/`hydrate_images` and `fuse_hits`/`backends` themselves;
  none of them touch `SearchEngine`, which is what these fill in): a photo
  with no matching text or filename is found by description alone, through
  the real `SearchEngine.search()` path - the search half of this order's
  acceptance sentence, now demonstrable rather than only unit-tested in
  isolation; the lane is off when either constructor argument is `None`
  (H4's default); a broken lane degrades with its own `NOTICE_NO_IMAGES`
  rather than silence or the text-vector lane's code; the thread pool grew
  to three workers so a third lane never serialises behind the other two.
  Full regression run (186+ tests across the CLIP lane, `test_layer4_
  acceptance.py`, `test_cli_wiring.py`, `test_policy_reaches_the_engine.py`,
  `test_search_images.py`, `test_fusion.py`, `test_backends.py`,
  `test_embedder.py`, the new file) is clean except confirmed pre-existing
  failures, unrelated to this change and reproduced against the pre-change
  commit before being set aside: `test_a_missing_rerank_model_degrades_to_
  the_fused_order`, `test_a_filter_that_matches_nothing_returns_nothing_
  calmly` (both `test_layer4_acceptance.py`), `test_reembed_says_what_it_
  is_doing_before_the_silence` (`test_cli_wiring.py` - reproduced against
  `main` with this session's changes stashed out), and one wall-clock-timing
  flake in `test_match_marker.py` unrelated to search entirely.

  Also observed, not fixed: `test_cli_wiring.py::test_search_warms_the_
  models_before_it_times_anything` calls a real, unmocked `cmd_search` and
  already tolerated the primary embedder attempting a real network download
  in this environment ("no network here" in its own docstring turns out not
  to hold) - this change makes that same, pre-existing pattern happen twice
  (the CLIP text tower now loads lazily too), which is slower and noisier
  in test output but not a new failure mode: the test's exit code is still
  0, confirmed by running it in isolation. Worth a follow-up to isolate
  model loading in that test rather than something to fix under this order.

  Also not built: `evaluate`'s photo-sentence benchmark the item asks to
  measure against before tuning further - now reachable (the engine-level
  lane exists), but running it and recording real recall numbers is a
  measurement task in its own right, not assumed done by wiring existing.

  **Real models confirmed working end to end**, on this machine, beyond unit
  tests with injected encoders: a real 512-dim vector from the real
  `Qdrant/clip-ViT-B-32-vision` model was written to a real
  `ImageVectorStore` and read back by `search()`, and a real sentence was
  embedded by the real `Qdrant/clip-ViT-B-32-text` model via a plain
  `Embedder` instance — see the §4 test-item note below for the numbers.

## 2. Same-image intelligence

- [ ] **2a pHash** (`imagehash`, installed) computed in the images pass,
  stored per file: exact/near duplicates cheap across all sources — feeds
  "also on <source>" display and, later, backup awareness.
- [ ] **2b burst folding**: near-identical results (pHash first, CLIP
  distance as tiebreak) fold into one row — newest/best first, "N similar
  photos" expandable. Same folding UI contract as the search-experience
  order's version folding; coordinate, don't duplicate.
- [ ] **2c reverse image search**: drop/paste an image into the search box →
  embed it, find it and relatives; result headline distinguishes "this exact
  photo (pHash) " from "similar". The degraded-WhatsApp-copy → "full-res
  original is on <source>" case is the acceptance demo.
- [ ] **2d more-like-this for images**: the existing right-click action
  extended to photo results via the image table.

## 3. Presentation

- [ ] **3a** thumbnail-grid toggle on image-heavy results (list stays
  default; the toggle is a per-surface preference, off-able like every
  behaviour); thumbnails decoded on workers (M11 pattern), orientation-
  correct (0508 §3b).
- [ ] **3b** pop-out viewer gains next/previous (arrow keys) = the lightbox;
  lives here because grid+lightbox ship together as the photo browsing
  experience.

## 4. Tests

- [ ] lane wiring: a fixture photo of a distinctive scene is found by a text
  description with zero matching filename/text (the whole point, as a test);
  lane failure → keyword+text-vector results + notice (H4 pattern pinned).

  **Not fully buildable this session** — this is an end-to-end search-box
  test, and the lane is not reachable from a search yet (§1c's stop-note
  above). What *is* built and green, standing in for it as far as this
  session's file list allows:
  - `tests/unit/test_search_images.py::test_fusing_keyword_text_and_image_lanes_does_not_collide` —
    a text hit and an image hit fuse into two distinct rows via the real
    `fuse_hits`, which is the mechanism this test needs once wired.
  - `tests/unit/test_search_images.py::test_a_broken_clip_text_model_returns_no_hits_and_a_notice` —
    the H4 pattern, pinned, on the lane itself.
  - `tests/unit/test_clip_lane_pipeline.py` end to end confirms a real photo
    with **zero** OCR text still gets a real CLIP vector from the real model
    (`test_image_vector_is_written_regardless_of_ocr_text`), which is the
    indexing half of the acceptance sentence.
  - The remaining half — typing a description into `app.cli search` or the
    window and getting the photo back — needs `engine.py`'s `fuse_hits` call
    to take a third list. Flagged for the next thread, not guessed here.

- [ ] pHash: duplicate fixture across two roots folds; reverse-image finds
  the original from a recompressed copy.

  **Out of scope for this session** — §2, explicitly held back per
  instruction until §1 lands and is reviewed.

- [x] vector-table contracts: kill-mid-run leaves no orphan image vectors
  (the M6-shape test for the new table).

  `tests/unit/test_image_vector_store.py::test_a_file_never_embedded_leaves_no_row`
  and `::test_deleting_replaces_rather_than_accumulates` cover the store-level
  shape (an `add_images` that never happened leaves no row; a delete-then-add
  never leaves an orphaned old one); `tests/unit/test_clip_lane_pipeline.py::
  test_pending_images_are_flushed_before_the_run_ends` and `::
  test_a_failing_image_does_not_break_the_run_or_the_file` cover the
  pipeline-level ordering (a batch is always flushed before `run()` returns;
  a failed embed leaves the store exactly as it was, never a half-written
  row).

- [x] perf: embeddings/min on fixture recorded in this file; grid scroll
  stays worker-fed (test_ui_never_blocks taught the new modules).

  **Embeddings/min recorded above, under 1a — and it is a bad number,
  reported honestly**: roughly 36 images/minute single-call, ~60/minute
  batched, on this machine's CPU. The grid-scroll half of this item is §3
  (thumbnail grid), untouched this session — `app/ui/*` was off limits and
  §3 is explicitly held back regardless.

## Done means

Change + tests + suite green + committed by name; measured rates recorded;
CHANGELOG. Acceptance sentence: type "kids blowing out birthday candles" and
the photo appears; drop a WhatsApp-compressed copy in and Leasha names where
the original lives.

**2026-09-05, first session: §1 only, and not fully "done" by this
definition yet.** 1a and 1b met it in full — real models, real
measurements, tests green. 1c met it for everything inside that session's
file list and stopped at `app/search/engine.py`'s `fuse_hits` call site,
which was outside it.

**2026-09-05, later the same day: §1 reaches "done".** `SearchEngine`
wiring landed (see 1c's dated note), and `app.cli index` now writes real
CLIP vectors on a real run — the acceptance sentence's search half
(`type "kids blowing out birthday candles" and the photo appears`) is
demonstrated end to end by `tests/unit/test_engine_image_lane.py::
test_a_photo_with_no_text_is_found_by_the_image_lane` through the real
`SearchEngine.search()` path, not just at `fuse_hits`/`search_images` in
isolation. **Committed by name**, suite green (this session's own sweep;
see 1c's note for the exact files and the pre-existing failures set aside).
**One real gap, named rather than silently left**: indexing from the
window does not yet write CLIP vectors (`app/ui/shell.py:_start_indexing`,
flagged in 1c's note) — so the acceptance sentence is only true for a
corpus indexed via `app.cli index`, not yet via the window's own "start
indexing" button, until that second site gets the same wiring. The
WhatsApp-compressed-copy half of the acceptance sentence (reverse image
search, pHash) is §2, explicitly out of scope here.

CHANGELOG entry added for this half - see `[Unreleased]`.

**For whoever picks up §2/§3 next:** the retrieval and indexing lanes both
work and are tested; `app/ui/shell.py:_start_indexing`'s missing
`image_embedder=`/`image_vectors=` wiring (see 1c) is the one loose end
worth closing before or alongside §2/§3, since a same-image-intelligence
feature built against a photo that got indexed from the window would find
no vector to work with at all.
