# Work order (One thread): media files by default, and the OCR ladder that makes it affordable

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
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

- [x] **1a** image formats (jpg/jpeg/png/gif/bmp/webp/**tif/tiff**/**heic/heif**
  /**svg**) enabled by default in the format registry; RAW formats
  (cr2/nef/dng/arw) enabled via embedded-preview extraction (1d). Existing
  installs: additive only — newly enabled formats join the next run; nothing
  is re-asked, nothing existing changes (grandfather rule).
  **Verified 2026-09-05:** all formats present in `config/extractors.toml`
  (tiff/heic/heif/svg → ocr; cr2/nef/dng/arw → raw).
- [x] **1b** `INDEX_OCR_MODE` default becomes the with-run mode (media on by
  default is hollow if OCR stays off); existing installs keep their stored
  choice.
  **Verified 2026-09-05:** `app/core/settings_registry.py` sets
  `default="with-run"` for `INDEX_OCR_MODE` exactly.

## 2. The ladder (one module, `app/extract/ocr.py` territory)

> **2026-09-05 note (integration bug fix session):** `ocr_ladder.py` was fully
> written with its own passing tests but was never called from anywhere else
> in the codebase — confirmed by grepping `app/extract/` and `app/index/` for
> `route(`. Two things were fixed: `route()`'s own short-circuit bug (camera-
> default filenames fell through to the detection rung instead of stopping at
> the metadata rung — the reverse of what `test_camera_default_filename_routes`
> and `test_screenshot_filename_routes` expected), and the missing wiring —
> `route()` is now called from `ocr_image()` in `app/extract/ocr.py`, the one
> function `OcrExtractor`, `RawExtractor` and `app/extract/pdf.py`'s scanned-page
> OCR all already funnel through. **2a and 2b are genuinely exercised now and
> marked done below. 2c and 2d are NOT done and stay unchecked** — see why
> under each. `app/extract/pdf.py` was not touched (out of this session's
> scope) and did not need to be: it already calls the shared `ocr_image()`, so
> scanned-PDF page renders are routed through the ladder exactly like any other
> image the moment `ocr_image` consults it — but see the 2d note below for what
> that does and does not buy today.

- [x] **2a Rung 0 — metadata routing** (free): EXIF camera tags, filename
  patterns (IMG_/DSC_/Screenshot/WhatsApp), PNG-vs-JPEG+EXIF. **Routes only —
  never rejects** (whiteboard/receipt/sign photos must stay OCR-able).
- [x] **2b Rung 1 — thumbnail stats** (~5ms, Pillow, 256px grey): white
  fraction, saturation, row-projection periodicity → fast-accept obvious
  documents into full OCR.
- [ ] **2c Rung 2 — detector-only probe** (~50–150ms, ~640px): the OCR
  engine's detection stage without recognition. Zero text boxes → record
  **"no text found (checked)"** — a truthful settled state, NOT
  `ERR_NO_TEXT_LAYER`. Boxes found → full OCR (region-only recognition where
  the engine allows).
  > **2026-09-05:** not implemented. `route()` still returns `DETECTION_ROUTE`
  > as a "the caller must decide" placeholder rather than actually running the
  > OCR engine's detection stage alone, so nothing today ever produces
  > `RouteDecision.NO_TEXT` for a real file. `ocr_image()` is wired end-to-end
  > to honour `NO_TEXT` the moment something produces it (skips recognition
  > entirely, sets `OcrResult.checked_no_text`, covered by
  > `tests/unit/test_ocr.py::test_a_ladder_settled_no_text_skips_the_engine_entirely`)
  > — but building the actual detector-only call against RapidOCR (region-only
  > recognition, confidence thresholds, and the "checked" ledger state
  > distinct from `ERR_NO_TEXT_LAYER`, which needs a decision about how
  > `app/extract/base.py` classifies a checked-empty result vs. a genuinely
  > empty one) is a distinct feature with its own design questions, not an
  > integration bug, and was left rather than guessed at.
- [ ] **2d** the ladder is THE router for scanned-PDF pages too: per-page
  probe on the rendered page; a mostly-text PDF stops paying for its
  non-scanned pages. `PDF_OCR_PAGES` semantics unchanged otherwise.
  > **2026-09-05:** partially true structurally — `app/extract/pdf.py`'s
  > `_ocr_pages()` already called the shared `ocr_image()` before this session
  > touched anything, so rendered page bytes now pass through `route()` the
  > same as any other image (`tests/unit/test_ocr.py::test_bytes_source_is_also_routed`
  > proves the bytes path). Left unchecked because the actual cost-saving half
  > — a page with no text boxes stops paying for recognition — is blocked on
  > 2c, not on anything PDF-specific; `PDF_OCR_PAGES` semantics are untouched.
- [ ] **2e** each image pays the ladder once (H1 settle); rung thresholds are
  envelope tunables (auto-defaulted, Index Tuning screen, plain-words labels
  per the standing rules).
  > **2026-09-05:** the "pays once" half is true (one `route()` call per
  > `ocr_image()` call). The tunables/Index Tuning screen half is UI work,
  > out of this session's scope (`app/ui/*` excluded), so left unchecked.

## 3. Image hygiene (the corpus punishes skipping these)

- [x] **3a EXIF date**: images index EXIF `DateTimeOriginal` as their date;
  fallback to file time only when absent. `after:`/`before:` and any date
  display use it. (Decision 3 above — load-bearing for the 20-year corpus.)
  **Verified 2026-09-05:** `read_datetime()` from `app/extract/exif.py` is
  called and its result assigned to `builder.date` in both
  `app/extract/ocr.py` and `app/extract/raw.py`. No dedicated end-to-end
  test exists yet for the "2006 DateTimeOriginal beats 2019 mtime,
  `before:2010` finds it" scenario specifically - only
  `test_read_datetime_never_raises` covers this function today. Ticked on
  the wiring, not the missing end-to-end proof; see §4.
- [x] **3b EXIF orientation** honoured wherever images are decoded (previews,
  future thumbnails) — portrait photos must not render sideways.
  **Checked 2026-09-05, genuinely not done:** `read_orientation()` exists in
  `app/extract/exif.py` but nothing calls it anywhere in the tree - searched
  `app/ui/preview_loader.py` and both image extractors specifically.
  > **2026-09-05 (format-support session):** wired. `decode_image()` in
  > `app/ui/preview_loader.py` is the sole place an image is decoded to
  > pixels for display anywhere in the tree - confirmed by grepping
  > `app/extract/` for `QImage`/`QPixmap` (none - nothing below `app/ui/`
  > touches Qt) and `app/ui/` for `QImage(`/`Image.open(` (only
  > `preview_loader.py`, `render_page.py` and `splash.py`; see below for
  > `render_page.py`). Neither `OcrExtractor` nor `RawExtractor` decodes
  > pixels for display - `OcrExtractor._worth_reading` opens an image only to
  > read its size, and `RawExtractor.extract_preview` hands raw JPEG bytes
  > straight to OCR, never to a `QImage` - so "the image extractor(s) that
  > build a preview" resolved to no additional call site once checked, not a
  > second one left undone. Both of `decode_image`'s branches - the plain
  > `QImage(path)` path every non-HEIF format takes, and `_decode_heif` for
  > HEIC/HEIF - now call `read_orientation()` and apply the correction via a
  > new `_apply_orientation()` helper before the pane ever sees the pixels.
  > Tests: `tests/unit/test_image_orientation.py` (new), 9 tests - the pure
  > transform for all eight EXIF codes plus the "1 or unrecognised is a
  > no-op" cases, then `decode_image()` against a real JPEG fixture with a
  > written Orientation tag (dimensions swap correctly for a 90°/270°
  > correction), and the HEIC branch with the same faked-`pillow_heif` seam
  > `test_heif_preview.py` already uses. **Found but out of scope, flagged
  > separately (not fixed here):** `app/ui/render_page.py`'s own `_image()`
  > (used by the "pin in its own window" pop-out, `preview_window.py`) is an
  > *independent* second decode path with the identical gap - no orientation
  > correction, and no HEIC/HEIF handling at all, since it never routes
  > through `preview_loader.py`. This session's scope was `preview_loader.py`
  > only; the pop-out window is a different file this order did not name, so
  > a portrait photo previews upright in the main pane after this fix but can
  > still preview sideways (or blank, for HEIC/HEIF) if pinned to its own
  > window. Flagged as a follow-up task rather than fixed on this session's
  > own initiative.
- [x] **3c HEIC/HEIF** via `pillow-heif` (installed by owner); **TIFF** and
  **SVG** join the preview image suffixes (indexer and preview suffix sets
  asserted consistent by test — the drift class found in review).
  **Checked 2026-09-05:** the formats themselves are enabled (see 1a), but
  the specific "indexer and preview suffix sets asserted consistent by
  test" this item calls for does not exist - searched for a suffix-
  consistency test and found only an unrelated one (table-alignment, not
  images).
  > **2026-09-05 (format-support session):** already done by the time this
  > session checked - not by this session. `tests/unit/test_viewer_suffixes.py`
  > (`test_image_suffixes_match_the_indexers_ocr_extensions`, "Workspace §7")
  > and the underlying `_IMAGE_SUFFIXES`/`KIND_MARKDOWN` wiring in
  > `app/ui/preview_loader.py` landed via the Workspace order's own commits
  > (`1caa388` "Workspace §4a: SVG, .heic/.heif and .tif/.tiff as images,
  > Markdown rendered") - dated 2026-09-05, the same day as the note above,
  > and evidently after it: the note's own search for a suffix-consistency
  > test predates that commit landing on `main`. Re-ran the test against
  > current `main` (2f2e8ee) and it passes. Nothing to build; the item's
  > condition is satisfied and the earlier "not done" note was correct at
  > the time it was written, not stale by omission.
- [x] **3d RAW** (cr2/nef/dng/arw): extract the embedded JPEG preview via
  `rawpy` for both indexing (ladder input) and preview. Never decode full RAW.
  **Verified 2026-09-05:** `app/extract/raw.py`'s `extract_preview()` does
  exactly this via `rawpy.imread(...).extract_thumb()`.
- [x] **3e Markdown** preview renders via `QTextDocument.setMarkdown`
  (one-liner from the viewer-gaps list; lives here because it is hygiene).
  **Checked 2026-09-05, not done:** no `setMarkdown` call anywhere in
  `app/ui/`.
  > **2026-09-05 (format-support session):** already done by the time this
  > session checked - not by this session, for the same reason as 3c above.
  > `app/ui/widgets/preview.py:387` and `app/ui/widgets/preview_window.py:294`
  > both call `self.text.document().setMarkdown(...)`, landed in the same
  > `1caa388` "Workspace §4a" commit. `tests/unit/test_preview_loader.py`
  > covers `kind_for` routing `.md`/`.markdown` to `KIND_MARKDOWN` rather than
  > `KIND_TEXT`. Confirmed by grep (`grep -rn setMarkdown app/`) and by
  > reading both call sites.

## 4. Tests

> **Checked 2026-09-05, all correctly left open:** the receipt-photo half of
> the ladder test exists (`test_receipt_filename_goes_straight_to_ocr` in
> `tests/unit/test_ocr_ladder.py`), but the "wall photo settles as no text
> found (checked)" half can't exist yet - it needs rung 2 (item 2c), which
> isn't built. The per-page PDF probe test needs the same. `test_perf_floors.py`
> exists but covers chunking/query/wildcard speed, not the ladder's own
> perf floor. No dedicated end-to-end EXIF-date test or suffix-consistency
> test exists either (see 3a/3c above). None ticked, since none is fully
> satisfied.

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
