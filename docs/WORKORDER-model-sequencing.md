# Work order (One thread): the models run where they pay, in the order that pays, and the graphics card is one lock across processes

**Doc version:** 1.4 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/core/gpu_serialize.py`, `app/index/pipeline.py`, `app/index/media_backlog.py`,
`app/extract/ocr.py`, `app/extract/chunker.py`, `app/search/engine.py`, `app/search/vector.py`,
`app/search/translate.py`, `app/ui/workers.py`, `app/ui/shell.py`, `app/chat/engine.py`, `app/chat/context.py`,
`app/ui/controllers/chat_controller.py`, `tests/unit/`)
**Status:** ACTIVE *(RELEASED by the owner 2026-10-10, "do the recomended": the order as written, section 1 first,
D4 as recommended. Written the same day at the owner's request: "critically look at our ai model choices strategy
... efficiencies ... sequencing ... quality, performance and stability is key", then "do all together".)*

**Where this came from.** A review of every model Leasha runs, at index time and at query time, read from the
code and from the numbers already measured on the owner's laptop (i7-1365U, Iris Xe, Windows 11). The
conclusion: **the models are the right ones; the sequencing around them is not.** No model changes in this
order. Changing the meaning model re-embeds the whole index (days), and every other model was chosen by
measurement (MiniLM over bge-reranker-base 9.2x; Qwen 2.5 1.5B q4 over Gemma 3 1B and Llama 3.2 1B on
Interpret; Florence-2 full precision over int8).

One bug the review found was fixed in the same session, not ordered: the run-end photo descriptions and
picture text were parked for a feeder the teardown had already stopped, and the unparked path checked
`_stop`, which is set at every run's end - so they waited for the next run for their vectors
(`Pipeline._drain_unembedded(at_run_end=True)`, `test_photo_tags_at_run_end.py`).

What the numbers say, with their sources:

| Where | Measured | Source |
|---|---|---|
| A 15-hour index run | 54,555 s embedding against 1,568 s writing | HANDOFF 2026-10-08 |
| Meaning model, processor | 13.2 passages/s; int8 1.85x faster, top-5 overlap 76% with full precision | HANDOFF 2026-10-09; HANDOFF ~4163 |
| Per photo, idle laptop | Florence 10.4 s, OCR 2.3 s, faces 0.9 s, CLIP 0.3 s | `pipeline.py` `_drain_photo_tags` docstring |
| Florence full precision, vision on the card | 3.6-5.8 s a photo | `app/ort/catalogue.json` |
| Search, five queries, no rerank | 1,959 ms first, then 177, 238, 664, 976 ms | HANDOFF 2026-10-09 |
| Search, with rerank | 991, 1,129, 1,206, 3,342, 1,856 ms | HANDOFF 2026-10-09 |
| Reranker (MiniLM, top 30, 600 chars) | 576 ms on the card, 1,109 ms on the processor; 818 ms median | HANDOFF 2026-10-08 |
| Chat model reading a prompt | 456 tokens about 14 s (about 33 tokens/s) | `app/ort/llm.py` `PREFIX_SLOTS` comment |
| Chat model decoding | about 5 tokens/s; 285-760 ms a token | HANDOFF ~1660, ~3538 |
| DirectML on the Iris Xe | embedding failed 3 runs of 3 (2026-09-30); OCR native crash 2026-10-09; two `LiveKernelEvent 141` resets 2026-10-09 | HANDOFF |

## 0. Decisions

- **D1 (taken in this order) No model is changed.** Every item below changes *when* or *on what* a model runs.
  A model change remains a catalogue entry with a `verified` record (order 1c).
- **D2 (taken) Measure before and after every item**, on the owner's laptop where the item says so. An item
  is not ticked on a number taken elsewhere; a cloud or sandbox number is marked as such.
- **D3 (taken, order 1g) OCR stays on the card where the device test puts it, in a helper process of its
  own.** 1g measured 259.5 s through the helper against 261.2 s in-process on 131 photographs, zero faults,
  and set `DEVICE_OCR` back to auto. §1 closes what isolation alone leaves open: the helper's card work is
  not serialised against the other two processes.
- **D4 (the owner's) The int8 meaning model on the owner's install.** 1.85x faster, but top-5 results agree
  with full precision only 76% of the time. Keep it on purpose, or return to full precision and pay the
  time. *[OWNER]*
  > **2026-10-10, decided by the owner** ("do the recomended"): kept, on purpose. Section 6's measurements
  > are where its cost to results shows; revisit then.

## 1. Stability first: one graphics-card lock for every process

**Why.** `gpu_serialize.gpu_exclusive` is a `threading.Lock` - one per process. Graphics-card work now runs in
three processes: the indexer (meaning model, CLIP, faces), the OCR helper (`ocr_process.py`) and the vision
host (Florence, the CLIP text tower). Two DirectML sessions can run at once again, which is the crash class
the lock was written for (2026-09-07). The "card is unreliable" latch (`mark_gpu_unreliable`) is per process
too, so a driver fault seen in one does not move the others to the processor.

> **2026-10-10, built.** A lock file (`leasha-gpu.lock`, in the temp folder or `LEASHA_GPU_LOCK_DIR`) through
> the new `app/core/osbridge/filelock.py`, rather than a named mutex: the operating system releases it when its
> holder dies, on Windows and elsewhere. **One departure from the text below:** a wait past
> `MACHINE_WAIT_S` (120 s) goes on without the cross-process lock and warns once, rather than raising - an
> `AppError` there would fail an index batch or a search because another process was slow, which is worse
> than the risk the lock removes. No deadlock across processes: every hand-off to another process (OCR helper,
> vision host, chat host) is made before the lock is taken. Tests: `test_gpu_lock_across_processes.py` (six,
> with real child processes). `tests/conftest.py` gives a test run its own lock folder.
- [x] **1a** `gpu_exclusive` takes a machine-wide lock as well as the thread lock: a named mutex on Windows,
      a lock file elsewhere, through `app/core/osbridge/` (the guard test forbids `ctypes.windll` outside it).
      Held only around session builds and inference, as today. Times out with a plain `AppError` rather than
      waiting for ever on a holder that died.
      *Acceptance:* two child processes each holding the lock in a loop never overlap (a test records
      entry and exit times); a holder killed mid-hold releases it to the next waiter.
> **2026-10-10, built.** The session is `LEASHA_GPU_SESSION`, set by the first process that imports
> `gpu_serialize` and inherited by its children (the index child copies `os.environ`); the latch file is named
> after it, so a fault in an earlier session never keeps a later one off the card. Files older than two days
> are tidied in passing.
- [x] **1b** The unreliable latch is shared: `mark_gpu_unreliable` writes a small state file in the data
      folder for this session; `backends.choose` reads it. A fault in the OCR helper moves the indexer and
      the vision host to the processor too.
      *Acceptance:* a latch set in one child process is seen by a second, started after.
> **2026-10-10, confirmed and fixed.** It was worse than reported: the tail's `_install_ocr_helper` closes
> before it opens, so it cleared the outer run's hook *and* started a second helper process; its close left the
> hook empty, and the outer run's end-of-run OCR ran in the index process. The tail also described photos and
> read picture text itself, which the outer run does straight after - Florence and OCR loaded twice.
> `BacklogPipeline` now leaves the helper and both steps to the outer run (`media_backlog.py`);
> `test_media_backlog_ocr_helper.py`. The base class's behaviour was checked to clear the hook, so the test
> fails without the fix.
- [x] **1c** Verify the review's report that the media sub-pipeline's `_close_ocr_helper` clears the global
      `ocr.set_engine_process`, so OCR after a media backlog runs inside the indexer (UNCONFIRMED). If true,
      fix it: the sub-pipeline must not close a helper it did not open.
      *Acceptance:* a test runs a media backlog and then a run-end picture-text drain and asserts the second
      OCR goes to the helper.
> **2026-10-10, open.** The seams exist (fastembed `ImageEmbedding(threads=)`, RapidOCR's
> `intra_op_num_threads`; insightface builds its sessions inside `model_zoo` and needs a session-options
> route). The counts themselves are a measurement on an idle laptop with a real picture run, before and
> after; a guessed number could slow the run it is meant to help. Left for that measurement.
> **2026-10-10, built; measurement owed.** `envelope.picture_model_threads`: logical processors less the extraction workers less the meaning model's threads, never above the meaning model's own count, one off for each extra caller of a session; no profile, library default. Applied to CLIP (`threads=`), RapidOCR (`intra_op_num_threads`, all three sessions; the helper's four callers taken off) and insightface (`sess_options` through `FaceAnalysis` to `model_zoo`). On the owner's laptop: 4 for CLIP and faces, 1 per OCR session (today 10 each). `test_picture_model_threads.py`, written, not run. The idle-laptop `pipeline_bench` before/after is owed before this is ticked.
- [ ] **1d** Thread counts are set for CLIP (`clip_embedder.py`), RapidOCR (`ocr.py`) and insightface
      (`face_detect.py`) from the same envelope as the meaning model, so the run does not ask for more
      threads than the processor has.
      *Acceptance:* each session's `intra_op_num_threads` is read back in a test; a short real run on the
      laptop is no slower (`pipeline_bench`).

## 2. A one-off check for empty vectors

**Why.** Before the 2026-09-30 fix, DirectML embedding on the Iris Xe returned empty vectors without raising.
An index built on the card before that may hold passages with all-zero vectors that are marked embedded and
can never be found by meaning. Not checked against the owner's index.

> **2026-10-10, built as `reembed --check` and `reembed --bad`**, beside the command that already rebuilds
> vectors from SQLite. `--check` reads only and takes no run lock; `--bad` deletes the affected files' vectors
> and queues **whole files** (`SqliteStore.mark_files_unembedded`) - the pipeline replaces a file's vectors by
> file, so a half-queued file would lose its good ones. LanceDB refuses a NaN at `add`, so only all-zero
> vectors can occur. `test_reembed_bad_vectors.py` (five). **On the owner's laptop:** `reembed --check`
> read `D:\LeashaIndex\Data` (the `.env`'s `DATA_PATH`) and found 0 vectors - checked, not trusted: that
> folder and `D:\Leasha\Data` both hold a new index created that morning, with empty vector folders. The
> 386,665-passage index the review's numbers came from is no longer on this laptop, so there is nothing from
> before the 2026-09-30 fix left to check. Run `reembed --check` on any older index that is restored.
- [x] **2a** `leasha-cli doctor --vectors` (or `app.cli maintenance`) counts rows in the vector store whose
      vector is all zeros or not finite, reads only, and offers to reset their passages to `embedded = 0`
      so the next run embeds them again.
      *Acceptance:* on a fixture store with three zero vectors it reports three and, on yes, resets three;
      run on the owner's index and the count written into this item.

## 3. Chat: read less, and read the right passages

**Why.** Reading the prompt is the cost of a Chat answer. At about 33 tokens/s, up to about 2,250 tokens of
whole source passages (`max_sources=6`, `window*0.55`) are in the order of a minute before the first word
(UNCONFIRMED - estimated from the prefix figure, not timed). Chat searches pass `rerank=False`, and the
answer reads whole passages, where the result list already cuts a 600-character window around the query.

- [ ] **3a** Measure first: time to first word and total time for ten real questions on the owner's laptop,
      with the prompt size in tokens logged for each. Written into this item.
> **2026-10-10, built; 3a's measurement owed.** Premise partly wrong: Chat cut each passage to its share already (~960 characters at six sources). Now `ChatEngine._rerank_sources` makes one rerank call over the candidates already gathered (no second retrieval), and `build_sources(passage_chars=)` gives the model a `RERANK_WINDOW_CHARS` window (600) while the verifier and citations keep the whole passage. `test_chat_sources_rerank.py`, written, not run.
- [ ] **3b** The answer's sources are the reranker's best four to six of the candidates already retrieved
      (one rerank call, no second retrieval), each cut to a window around the query terms
      (`rerank.rerank_window_chars` or a Chat setting of its own), the file name kept.
      *Acceptance:* the same ten questions: time to first word falls; the verifier's pass rate
      (`CHAT_VERIFY_STRICTNESS`) and the cited files are recorded beside it and do not fall.
> **2026-10-10, built.** `chat_controller._title_when_idle` (`TITLE_IDLE_MS` 1500): the title starts only when no question is in progress. Found with it: a conversation deleted while its title waited was saved back by `_titled`; fixed. Tests in `test_chat_tab_qt.py`, written, not run.
- [ ] **3c** The conversation title is taken after the reply has finished and only when the model is idle,
      so it never sits in front of the next question behind the model's lock.
      *Acceptance:* a test with a fake model asks a second question at once and it starts before the title.
> **2026-10-10, built.** Premise partly wrong: the command line did not pass a store either. The window's `QueryTranslator` and both of `app.cli evaluate`'s are given the store now. `test_interpret_window_rules_qt.py`, written, not run.
- [ ] **3d** Interpret in the window gets the store, so the rules-first step (`translate.py`, only the
      leftover words go to the model) runs there as it does on the command line.
      *Acceptance:* a window-built `QueryTranslator` resolves "pdfs from last year" with the dates and type
      from rules and asks the model for nothing, or for less.

## 4. Photos: the cheap models decide what the expensive one sees

**Why.** Florence (4-10 s a photo) describes every photo, screenshots included, and then OCR (2.3 s) reads the
same photo (`_drain_picture_text`). Video keyframes already use the cheaper order - OCR first, Florence only
when there is no text (`media.py`). CLIP has already been computed for every picture by then (0.2-0.3 s), and
the perceptual hash is computed and never used, so every shot of a burst pays for Florence on its own.

- [ ] **4a** An evaluation set first: about 200 of the owner's pictures, hand-labelled photo / screenshot /
      document, with the current captions and OCR text kept. Every later item is measured against it.
- [ ] **4b** Route by the CLIP vector already stored: a zero-shot check against a few text prompts
      ("a screenshot", "a page of text", "a photograph") sends a text-bearing picture to OCR first, and
      Florence only when OCR finds nothing - the keyframe order. A photograph keeps today's order.
      *Acceptance:* on 4a's set, the routing agrees with the hand labels at a rate written here; Florence
      calls fall; no picture loses text it had before.
> **2026-10-10, built; threshold and share owed.** `app/index/photo_reuse.py` `DescriptionReuse`: same folder, EXIF shot times within 60 s (a guessed date never counts), pHash within `REUSE_PHASH_DISTANCE = 8` bits (a judgement: half of search folding's 16) - the copy is marked by `index_state` `description_copied_from:<id>`, and only photos Florence itself described are copied from. Wired where photos are described today (`_drain_picture_text`) as well as `_drain_photo_tags`. `test_photo_description_reuse.py`, written, not run.
- [ ] **4c** A near-duplicate (pHash distance under a threshold, same folder, taken within a minute) reuses
      its sibling's description instead of calling Florence; the reuse is marked so it can be redone.
      *Acceptance:* on a burst in the fixture set, Florence runs once; the threshold and the share of the
      owner's library it skips are written here.
> **2026-10-10, built by the indexing review (P1).** A photo's CLIP, pHash, faces and video frames run on a `pictures` thread while reading lasts; on a fixture of 120 letters and 30 photos at 200 ms each, the last letter was committed at 2.9 s instead of 7.0 s, and every picture is still embedded by the run's end (`test_writer_path_review.py`).
- [x] **4d** CLIP and faces move off the consumer thread during the text pass (to the run's end, or a worker
      of their own), so "text first" is not held up by pictures.
      *Acceptance:* on a mixed fixture run, the time until every text file is searchable by its words falls;
      picture search still finds every picture by the end of the run.

## 5. Search: do the retrieval once

**Why.** Measured 177-976 ms without rerank against a 300 ms budget. With rerank on, the worker runs
`search_once` twice (`workers.py`: results at once, then the better order), and the second call misses the
cache (the rerank flag is in the key), so keyword search, the meaning vector, the picture lane and fusion all
run again only to rerank a list the first call already had. The query's meaning vector is never cached. The
picture lane runs on every search, including on an index with no pictures, and the first search starts the
vision host (about 4 s).

> **2026-10-10, built; the 15-query check owed.** `SearchEngine._retrieve` is split at fusion (`_gather`, `_finish`); the first pass keeps its fused list in a small LRU and the reranked pass runs only `_finish` on a copy. It retrieves again after a write, a degraded first pass, or `use_cache=False`. `test_rerank_reuses_candidates.py`, written, not run.
- [ ] **5a** The rerank pass reranks the first pass's fused list; it does not retrieve again.
      *Acceptance:* a test counts retriever calls: one per search with rerank on; the order of results is
      the same as today's on the 15-query evaluation set.
> **2026-10-10, built; warm times owed.** `vector.QueryVectorCache` (64 entries, locked), keyed by the embedder's identity and the query with its spaces collapsed (case kept); failures never kept. Chat's widening rounds reach it through `SearchEngine.search`. `test_query_vector_cache.py`, written, not run.
- [ ] **5b** A small LRU of query vectors (text and CLIP) keyed by the model and the normalised query, used by
      the relax loop and by Chat's widening rounds.
      *Acceptance:* the relax loop embeds a repeated query once (test); warm search times re-measured.
> **2026-10-10, built.** `SearchEngine._search_pictures` returns nothing before the encoder is touched when the picture table is empty; the count is kept per index generation, and an empty answer is asked again after 30 s (the last photos of a run can land without a generation bump). `test_picture_lane_skipped_when_empty.py`, written, not run.
- [ ] **5c** The picture lane is skipped when the picture table is empty, and the vision host is not started
      for it.
      *Acceptance:* on an index with no pictures the first search does not start the vision host (test).
- [ ] **5d** The rerank cost against its budget: measure `RERANK_TOP_N` 30 / 20 / 15 and a 600 / 400 / 300
      window with `rerank_bench` and the 15-query evaluation set; take the smallest that keeps the top 10.
      *Acceptance:* the table of results written here; the default changed only on that evidence.
- [ ] **5e** Re-run step 3 of the release checklist (the five queries above) after 5a-5d and write the
      numbers into `BUILD_SPEC_V2.md`'s budget table and the architecture's honest numbers.

## 6. The meaning model: embed less, and all of each passage

**Why.** Embedding is most of an index run. Identical files are embedded again (the duplicate check is within
one batch of 256); the 64-token overlap adds about 12.5%; and passages are sized by an estimate of 1.35
tokens a word against a 512 limit, while bge stops reading at 512 real tokens - a passage past that loses its
tail without a word (UNCONFIRMED).

- [ ] **6a** Measure the truncation: run the real tokenizer over a sample of 10,000 of the owner's passages;
      write here the share over 512 tokens and how much is cut. If it matters, size passages by the real
      tokenizer (`chunker.py`) - a change that applies to newly read files only, with no forced re-index.
      > **2026-10-11, measured for 6b on the owner's index** (read-only, while the overnight run was in its meaning
      > phase): 6,328,527 passages are bound for the model (2,629,553 more are keyword-only). **1,610,571 of them (25%)
      > sit in second-and-later copies of a file with the same `content_hash`** - a lower bound for 6b, which matches
      > passages, not whole files. Not small: the item stands. Passage-level share not measured (a full read of the
      > 31 GB store did not finish in 10 minutes).
      > **2026-10-11, built (owner: "build 6b").** Departure from the item's wording: matched by *file*, not by a
      > hash of each passage. A file whose `content_hash` matches a file with vectors takes, for each passage whose text
      > matches, that copy's vector (`SqliteStore.embedded_twin_passages`, `VectorStore.vectors_for`,
      > `Pipeline._embed_reusing`); the rest go to the model. Why: it uses `idx_files_content_hash` and
      > `idx_chunks_file_ord`, which exist, where a passage hash needs a new column and index backfilled over 8.9M rows
      > of a 31 GB store. It covers the 25% measured above; identical passages in *different* files (boilerplate) are
      > still only caught within one batch (6e). Off with `EMBED_DEDUP`. Counted as `IndexStats.vectors_reused` and
      > reported by `app.cli index` ("Copies ..."). Measured on the owner's index, read-only: the twin lookup 1.31 s
      > for a batch of 300 files, `vectors_for` 0.22 s for 256 vectors, against ~35 s of model per batch.
      > `tests/unit/test_vector_reuse.py`: a file copied into three folders is embedded once - the item's acceptance.
- [ ] **6b** A passage whose text is identical to one already embedded (a content hash of the passage) takes
      the existing vector instead of the model.
      *Acceptance:* a fixture with a file copied into three folders embeds its passages once; the share of
      the owner's passages that are duplicates is measured and written here before the item is built, and
      the item is dropped if it is small.

## 7. Done means

- [ ] **7a** The whole suite passes (`scripts/run_suite.py`), and each item's measured numbers are in it.
- [ ] **7b** `HANDOFF.md`, `docs/ORDER_REGISTER.md`, `LOCAL_KNOWLEDGE_GRAPH_V2.md` and `BUILD_SPEC_V2.md` say
      what is true after it.
