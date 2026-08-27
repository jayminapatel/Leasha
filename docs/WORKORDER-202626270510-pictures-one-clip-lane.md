# Work order (One thread): Pictures I — the CLIP lane: find photos by describing them

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Index + Storage/vectors + Search + results UI)
**Status:** RELEASED by the owner 2026-08-28. Requires 0508 (media defaults +
ladder + EXIF dates) landed first. **Scope discipline: vectors and
presentation only — NO tag/caption text generation (that is 0511), NO faces
(0512).** The owner's killer case sits behind this order: describe a picture
from memory and find it — extended to offline drives when the Offline Media
orders land.

## 1. The image-vector lane

- [ ] **1a** image embeddings via **fastembed's ImageEmbedding** (CLIP-class
  model, ONNX — already-installed package; thread verifies model
  availability, else the smallest OpenCLIP ONNX export; free/unencumbered
  only, NO Ultralytics/AGPL imports — standing rule with its guard test).
  Every ladder-passed image gets a vector; ~50–150ms/img CPU budget measured
  on the fixture before acceptance.
- [ ] **1b** second LanceDB table (dimensions differ from text vectors),
  keyed by file_id; same delete/compaction/crash-ordering contracts as the
  text table (the M6/M8/H7 patterns apply — cite them in comments).
- [ ] **1c** third retrieval lane: the query text embedded by the **CLIP text
  tower** (not the FastEmbed text model — different space), ANN over the
  image table, fused via the existing RRF exactly as keyword+vector fuse
  today. Runs when the query plausibly describes imagery — v1 heuristic:
  always run it, weight it in via RRF; measure against `evaluate` photo
  sentences before tuning further. H4 discipline throughout: lane failure
  degrades with a notice, never breaks search.

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
- [ ] pHash: duplicate fixture across two roots folds; reverse-image finds
  the original from a recompressed copy.
- [ ] vector-table contracts: kill-mid-run leaves no orphan image vectors
  (the M6-shape test for the new table).
- [ ] perf: embeddings/min on fixture recorded in this file; grid scroll
  stays worker-fed (test_ui_never_blocks taught the new modules).

## Done means

Change + tests + suite green + committed by name; measured rates recorded;
CHANGELOG. Acceptance sentence: type "kids blowing out birthday candles" and
the photo appears; drop a WhatsApp-compressed copy in and Leasha names where
the original lives.
