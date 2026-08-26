# Work order (One thread): media files by default, and the OCR ladder that makes it affordable

**Doc version:** 1.0 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract + Index pipeline + formats)
**Status:** RELEASED by the owner 2026-08-28. Queue position: first of the new
batch, after `WORKORDER-202626270326-workspace-features.md`. This order is the
foundation the picture orders (0510, 0511, 0512) build on — do them in file
order. **Scope discipline: this order is pipeline-only. No new UI beyond
settings entries; no CLIP, no tags, no faces — those are later orders.**

## Owner decisions this order implements (settled — do not relitigate)

1. **"For all files we can scan inside and derive value, scanning is ON by
   default"** (owner, 2026-08-28). Images and other media formats join the
   default-enabled set. Slow first scans of media-heavy drives are ACCEPTED —
   "expected, small price to pay." Never trade coverage for speed; the ladder
   manages cost, it does not cut corpus.
2. **The OCR ladder applies to ALL pictures everywhere** — images pass,
   archive/PST members, scanned-PDF page renders. One implementation in the
   OCR layer; every caller inherits it.
3. **EXIF date is THE date for photos** — file mtime lies after 20 years of
   drive-to-drive copies; EXIF DateTimeOriginal survives them.
4. **Index-everything doctrine**: no content deny-lists, no format ethics.
   Encrypted files naturally yield name-only rows. (Recorded here so nobody
   "helpfully" adds a sensitive-format skip later.)

## 1. Defaults

- [ ] **1a** image formats (jpg/jpeg/png/gif/bmp/webp/**tif/tiff**/**heic/heif**
  /**svg**) enabled by default in the format registry; RAW formats
  (cr2/nef/dng/arw) enabled via embedded-preview extraction (1d). Existing
  installs: additive only — newly enabled formats join the next run; nothing
  is re-asked, nothing existing changes (grandfather rule).
- [ ] **1b** `INDEX_OCR_MODE` default becomes the with-run mode (media on by
  default is hollow if OCR stays off); existing installs keep their stored
  choice.

## 2. The ladder (one module, `app/extract/ocr.py` territory)

- [ ] **2a Rung 0 — metadata routing** (free): EXIF camera tags, filename
  patterns (IMG_/DSC_/Screenshot/WhatsApp), PNG-vs-JPEG+EXIF. **Routes only —
  never rejects** (whiteboard/receipt/sign photos must stay OCR-able).
- [ ] **2b Rung 1 — thumbnail stats** (~5ms, Pillow, 256px grey): white
  fraction, saturation, row-projection periodicity → fast-accept obvious
  documents into full OCR.
- [ ] **2c Rung 2 — detector-only probe** (~50–150ms, ~640px): the OCR
  engine's detection stage without recognition. Zero text boxes → record
  **"no text found (checked)"** — a truthful settled state, NOT
  `ERR_NO_TEXT_LAYER`. Boxes found → full OCR (region-only recognition where
  the engine allows).
- [ ] **2d** the ladder is THE router for scanned-PDF pages too: per-page
  probe on the rendered page; a mostly-text PDF stops paying for its
  non-scanned pages. `PDF_OCR_PAGES` semantics unchanged otherwise.
- [ ] **2e** each image pays the ladder once (H1 settle); rung thresholds are
  envelope tunables (auto-defaulted, Index Tuning screen, plain-words labels
  per the standing rules).

## 3. Image hygiene (the corpus punishes skipping these)

- [ ] **3a EXIF date**: images index EXIF `DateTimeOriginal` as their date;
  fallback to file time only when absent. `after:`/`before:` and any date
  display use it. (Decision 3 above — load-bearing for the 20-year corpus.)
- [ ] **3b EXIF orientation** honoured wherever images are decoded (previews,
  future thumbnails) — portrait photos must not render sideways.
- [ ] **3c HEIC/HEIF** via `pillow-heif` (installed by owner); **TIFF** and
  **SVG** join the preview image suffixes (indexer and preview suffix sets
  asserted consistent by test — the drift class found in review).
- [ ] **3d RAW** (cr2/nef/dng/arw): extract the embedded JPEG preview via
  `rawpy` for both indexing (ladder input) and preview. Never decode full RAW.
- [ ] **3e Markdown** preview renders via `QTextDocument.setMarkdown`
  (one-liner from the viewer-gaps list; lives here because it is hygiene).

## 4. Tests

- [ ] ladder: a text scan fast-accepts at rung 1; a wall photo settles as "no
  text found (checked)" via rung 2 with no recognition run; a receipt *photo*
  (camera EXIF + text) reaches full OCR — the routes-not-rejects proof.
- [ ] per-page PDF probe: fixture PDF with 1 scanned page among text pages —
  only that page pays recognition.
- [ ] EXIF date: fixture photo with 2006 DateTimeOriginal + 2019 mtime indexes
  as 2006; `before:2010` finds it.
- [ ] suffix-set consistency test (indexer vs preview) as a standing guard.
- [ ] perf: ladder floor on the fixture corpus (N photos gated under T —
  measured, then pinned, per the perf-regression pattern).

## Done means

Change + tests + suite green + committed by name; ladder timings from the
fixture recorded in this file; CHANGELOG under `[Unreleased]`. Acceptance
sentence: a picture-heavy folder indexes with images on by default, obvious
documents get OCR'd, wall photos cost milliseconds and record a truthful
state, and a 2006 photo copied five times still says 2006.
