# Work order (One thread): media files by default, and the OCR ladder that makes it affordable

**Doc version:** 1.0 · **Updated:** 2026-09-05 · **Applies to:** app v0.3.3
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
- [x] **2c Rung 2 — detector-only probe** (~50–150ms, ~640px): the OCR
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
  > **2026-09-05 note (OCR-ladder-items session):** built. `route()` now takes
  > an optional `detect` callable (`ocr_ladder.py`); `ocr_image()` supplies one
  > (`_detect_only` in `app/extract/ocr.py`) closed over whatever engine it
  > already loaded, calling it with `use_det=True, use_cls=False,
  > use_rec=False` — RapidOCR's own detection-only split, confirmed by reading
  > `RapidOCR.__call__`'s source in the installed
  > `rapidocr_onnxruntime==1.4.4` package rather than assumed. Zero boxes →
  > `RouteDecision.NO_TEXT` (the existing `checked_no_text` path already
  > handled this once produced — see the note above). Boxes found →
  > `RouteDecision.FULL_OCR`, which re-runs detection inside the normal
  > recognition pass rather than reusing the rung-2 crop — RapidOCR does not
  > expose "resume with these boxes" as a call, so this pays detection twice
  > for the *minority* case (photos that do have text) to reliably skip
  > recognition once for the *majority* case (photos that do not) — the
  > module docstring's rung-2 section now says this plainly instead of
  > implying a resumable call that was never built. **"Region-only recognition
  > where the engine allows" from this item's own wording is not what got
  > built** — RapidOCR's crop-and-recognise is internal to one `__call__`, not
  > a second callable that can be fed rung 2's boxes — so the item is ticked
  > for the truthful, tested behaviour (detect-then-decide, not detect-then-
  > resume) rather than the letter of the original wording. A broken or
  > missing detector falls back to `DETECTION_ROUTE` (never invents `NO_TEXT`)
  > — tested by `test_a_broken_detector_never_claims_no_text`. Covered by
  > `tests/unit/test_ocr_ladder.py::TestDetectionProbe` (`route()` directly)
  > and `tests/unit/test_ocr.py`'s two new rung-2 tests (through `ocr_image()`
  > end to end, with a fake engine honouring `use_det`/`use_rec` — RapidOCR
  > itself untouched since these run without it installed).
- [x] **2d** the ladder is THE router for scanned-PDF pages too: per-page
  probe on the rendered page; a mostly-text PDF stops paying for its
  non-scanned pages. `PDF_OCR_PAGES` semantics unchanged otherwise.
  > **2026-09-05:** partially true structurally — `app/extract/pdf.py`'s
  > `_ocr_pages()` already called the shared `ocr_image()` before this session
  > touched anything, so rendered page bytes now pass through `route()` the
  > same as any other image (`tests/unit/test_ocr.py::test_bytes_source_is_also_routed`
  > proves the bytes path). Left unchecked because the actual cost-saving half
  > — a page with no text boxes stops paying for recognition — is blocked on
  > 2c, not on anything PDF-specific; `PDF_OCR_PAGES` semantics are untouched.
  > **2026-09-05 note (OCR-ladder-items session):** built, and a second gap
  > closed along the way. 2c now makes "a page with no text boxes stops paying
  > for recognition" literally true — `_ocr_pages()` was untouched (its own
  > whitebox tests in `test_pdf_ocr.py` assert on calls in its own source
  > specifically, so refactoring it risked breaking those for no behavioural
  > gain) and every page it renders already goes through `ocr_image()`, so it
  > inherits 2c automatically. **But `_ocr_pages()` only ever ran for a PDF
  > that is scanned throughout** (`result.is_empty` — every page came back
  > with no text layer). A born-digital report with one scanned appendix never
  > reached it at all: `result.is_empty` was False, so the pages with no text
  > layer were only ever warned about, never offered the OCR budget — read
  > `extract()`'s control flow to confirm this, not assumed. That is the "per-
  > page probe... a mostly-text PDF stops paying for its non-scanned pages"
  > half of this item's own wording, and it was not built before now. Added
  > `_ocr_specific_pages()` (a separate function from `_ocr_pages()` for the
  > whitebox-test reason above, not a refactor of it) — same budget
  > (`PDF_OCR_PAGES`/`_pdf_ocr_pages()`), same `ocr_image()` call, applied to
  > exactly the page numbers that had no text layer rather than the first N of
  > the document. `PDF_OCR_PAGES` semantics genuinely unchanged: with the
  > budget at 0 (the default), a partly-scanned PDF behaves exactly as before
  > — text pages indexed, the rest warned about — proved by
  > `test_a_scanned_page_the_budget_does_not_cover_still_warns`. With a
  > budget, `test_only_the_scanned_page_among_text_pages_pays_for_recognition`
  > builds a real 3-page PDF via `pymupdf` (page 1 and 3 real text, page 2 an
  > inserted image with none) and proves exactly one page pays for
  > recognition and all three end up searchable.
- [ ] **2e** each image pays the ladder once (H1 settle); rung thresholds are
  envelope tunables (auto-defaulted, Index Tuning screen, plain-words labels
  per the standing rules).
  > **2026-09-05:** the "pays once" half is true (one `route()` call per
  > `ocr_image()` call). The tunables/Index Tuning screen half is UI work,
  > out of this session's scope (`app/ui/*` excluded), so left unchecked.
  > **2026-09-05 note (OCR-ladder-items session):** re-verified both halves
  > rather than only re-reading the note above. "Pays once" per call: still
  > true, still `tests/unit/test_ocr.py::test_the_ladder_is_consulted_for_a_real_path`
  > (`assert calls == [image]`, i.e. exactly one `route()` call for one
  > `ocr_image()` call). "H1 settle" specifically (not just "one call per
  > call" — the *rescan* guarantee that an unchanged image is not re-extracted
  > at all): read `app/index/pipeline.py`'s classify/settle logic
  > (`_classify`, the `UNCHANGED` return around line 1766, and the `settled`
  > skip check around lines 1797–1803) end to end. Both branches key on
  > `record.status`, `candidate.mtime_ns` and `candidate.size_bytes` alone —
  > no extension or extractor check anywhere in either path — so an unchanged
  > photo settles exactly like an unchanged PDF or spreadsheet; H1 was never
  > type-specific and needed no ladder-side change to cover images. Left
  > unchecked, same as before: the tunables/Index Tuning screen half is
  > `app/ui/*`, out of this session's scope too. Did not touch `pipeline.py`
  > for this item — verification only.

## 3. Image hygiene (the corpus punishes skipping these)

- [ ] **3a EXIF date**: images index EXIF `DateTimeOriginal` as their date;
  fallback to file time only when absent. `after:`/`before:` and any date
  display use it. (Decision 3 above — load-bearing for the 20-year corpus.)
  **Verified 2026-09-05:** `read_datetime()` from `app/extract/exif.py` is
  called and its result assigned to `builder.date` in both
  `app/extract/ocr.py` and `app/extract/raw.py`. No dedicated end-to-end
  test exists yet for the "2006 DateTimeOriginal beats 2019 mtime,
  `before:2010` finds it" scenario specifically - only
  `test_read_datetime_never_raises` covers this function today. Ticked on
  the wiring, not the missing end-to-end proof; see §4.
  > **2026-09-05 note (OCR-ladder-items session) — un-ticked, the wiring does
  > not reach storage:** built the missing end-to-end test §4 calls for
  > (`tests/unit/test_exif.py::test_exif_date_beats_mtime_in_a_before_filter_end_to_end`)
  > and it fails, for a reason worth stating precisely rather than re-ticking
  > past it. `builder.date = exif_date` **is** called (the "Verified" note
  > above is correct as far as it looked) — but `DocumentBuilder` in
  > `app/extract/base.py` never declares a `date` attribute in `__init__`, and
  > `DocumentBuilder.build()` constructs `Document(...)` from `path, text,
  > segments, source_kind, meta, warnings, anchors` only — no `date`. Read
  > both classes directly to confirm: the assignment is a plain Python
  > attribute set on the builder that nothing downstream ever reads, and
  > `Document` itself has no `date` field to read it into regardless. The only
  > date-bearing column in `files` is `mtime_ns` — confirmed in
  > `app/storage/filters.py::file_filter_sql`, which builds every `after:`/
  > `before:` comparison against `f.mtime_ns` with a comment that says so
  > directly ("mtime_ns, because that is what the files table stores"). So
  > today, for every image in the corpus, `after:`/`before:` filter and sort
  > by copy date, not shot date - decision 3 is unimplemented past the
  > extractor call. **Not fixed in this session**: this order's scope was
  > 2c/2d/2e (the ladder), and a real fix needs a schema change rather than a
  > wiring fix — `mtime_ns` is also H1's change-detection key
  > (`app/index/pipeline.py`'s classify/settle logic compares
  > `candidate.mtime_ns` against the stored value to decide "unchanged"), so
  > overwriting it with EXIF would make every photo look changed on every
  > rescan and defeat H1 for the entire image corpus. This needs a new column
  > (something like `taken_at_ns`, separate from `mtime_ns`), threaded through
  > `Document`/`DocumentBuilder`, `SqliteStore.upsert_file`, every
  > `pipeline.py` call site that currently passes `mtime_ns=candidate.mtime_ns`
  > (at least four - `_write_one` and its neighbours), and
  > `file_filter_sql`'s `after:`/`before:` clauses (falling back to `mtime_ns`
  > when the new column is null, for every non-photo file). That is real
  > scope, touching files two other concurrent sessions were also editing
  > around the same run this session found this in — flagged here rather than
  > guessed at, for its own work order.
- [ ] **3b EXIF orientation** honoured wherever images are decoded (previews,
  future thumbnails) — portrait photos must not render sideways.
  **Checked 2026-09-05, genuinely not done:** `read_orientation()` exists in
  `app/extract/exif.py` but nothing calls it anywhere in the tree - searched
  `app/ui/preview_loader.py` and both image extractors specifically.
- [ ] **3c HEIC/HEIF** via `pillow-heif` (installed by owner); **TIFF** and
  **SVG** join the preview image suffixes (indexer and preview suffix sets
  asserted consistent by test — the drift class found in review).
  **Checked 2026-09-05:** the formats themselves are enabled (see 1a), but
  the specific "indexer and preview suffix sets asserted consistent by
  test" this item calls for does not exist - searched for a suffix-
  consistency test and found only an unrelated one (table-alignment, not
  images).
  > **2026-09-05 note (OCR-ladder-items session):** the suffix-consistency
  > half is done now, but not by this session - `tests/unit/test_viewer_suffixes.py`
  > exists (commit `1caa388`, "Workspace §4a", same day, a different
  > concurrent session) and does exactly what this item and this order's own
  > §4 ask for: `_IMAGE_SUFFIXES` in `app/ui/preview_loader.py` pinned equal
  > to `OcrExtractor.extensions`, not merely overlapping. Ran it -
  > `test_image_suffixes_match_the_indexers_ocr_extensions` passes. `pillow-heif`
  > itself is not installed in this venv (`pip show`/import both fail), so the
  > "installed by owner" half is still unverifiable from here - checked, not
  > assumed. Left unchecked because that half is outside what this session
  > verified either way; noted so the next pass does not re-search for a test
  > that already exists.
- [x] **3d RAW** (cr2/nef/dng/arw): extract the embedded JPEG preview via
  `rawpy` for both indexing (ladder input) and preview. Never decode full RAW.
  **Verified 2026-09-05:** `app/extract/raw.py`'s `extract_preview()` does
  exactly this via `rawpy.imread(...).extract_thumb()`.
- [ ] **3e Markdown** preview renders via `QTextDocument.setMarkdown`
  (one-liner from the viewer-gaps list; lives here because it is hygiene).
  **Checked 2026-09-05, not done:** no `setMarkdown` call anywhere in
  `app/ui/`.

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

- [x] ladder: a text scan fast-accepts at rung 1; a wall photo settles as "no
  text found (checked)" via rung 2 with no recognition run; a receipt *photo*
  (camera EXIF + text) reaches full OCR — the routes-not-rejects proof.
  > **2026-09-05 note (OCR-ladder-items session):** all three now exist and
  > pass. Rung-1 fast-accept:
  > `tests/unit/test_ocr_ladder.py::TestRungOneFastAccept::test_a_text_scan_fast_accepts_at_rung_1`
  > (a detector that must-not-run is passed in, so a false pass via rung 2
  > cannot happen). Wall photo → `NO_TEXT` via rung 2, no recognition:
  > `TestDetectionProbe::test_a_wall_photo_settles_as_no_text_found_checked`
  > at the `route()` level and
  > `test_ocr.py::test_a_wall_photo_settles_no_text_without_recognition_ever_running`
  > end to end through `ocr_image()`, with an `engine` that raises if
  > recognition is ever reached. Receipt photo reaches full OCR:
  > `TestDetectionProbe::test_a_receipt_photo_reaches_full_ocr_via_rung_2` and
  > `test_ocr.py::test_a_receipt_photo_reaches_full_ocr_end_to_end`, both using
  > a generic filename and a non-white thumbnail deliberately, so the proof is
  > about rung 2 specifically and not rung 0/1 already having settled it.
- [x] per-page PDF probe: fixture PDF with 1 scanned page among text pages —
  only that page pays recognition.
  > **2026-09-05 note (OCR-ladder-items session):**
  > `tests/unit/test_pdf_ocr.py::test_only_the_scanned_page_among_text_pages_pays_for_recognition`
  > - a real 3-page PDF built with `pymupdf` (pages 1 and 3 real text, page 2
  > an inserted image with none), `ocr_image` faked to count calls. Asserts
  > exactly one call, all three pages' text present in the result, and no
  > warnings left over. Paired with
  > `test_a_scanned_page_the_budget_does_not_cover_still_warns` proving
  > `PDF_OCR_PAGES=0` (the default) behaves exactly as before - the "unchanged
  > otherwise" half of 2d's own wording.
- [ ] EXIF date: fixture photo with 2006 DateTimeOriginal + 2019 mtime indexes
  as 2006; `before:2010` finds it.
  > **2026-09-05 note (OCR-ladder-items session):** written -
  > `tests/unit/test_exif.py::test_exif_date_beats_mtime_in_a_before_filter_end_to_end`
  > - and it fails, honestly, because the behaviour it describes does not
  > exist: see the corrected 3a above for exactly where the wiring stops
  > (`DocumentBuilder.build()` never reads the `date` it was given). Marked
  > `xfail(strict=True)` with the full diagnosis in the reason string, rather
  > than left red or deleted, so the suite stays green and the day this gets
  > fixed the test flips to an unexpected pass (`XPASS`) - the signal to
  > remove the marker rather than to re-derive whether it works. Left
  > unchecked: the acceptance sentence this proves is not yet true.
- [x] suffix-set consistency test (indexer vs preview) as a standing guard.
  > **2026-09-05 note (OCR-ladder-items session):** exists already -
  > `tests/unit/test_viewer_suffixes.py`, added the same day by a different
  > concurrent session (commit `1caa388`, "Workspace §4a") - not written by
  > this one. Ran it directly rather than trusting the commit message:
  > `test_image_suffixes_match_the_indexers_ocr_extensions` passes, and it
  > pins `_IMAGE_SUFFIXES` equal to `OcrExtractor.extensions` rather than
  > merely overlapping, which is what this item and 3c both ask for. Ticked
  > because the guard exists and holds, not because this session built it.
- [x] perf: ladder floor on the fixture corpus (N photos gated under T —
  measured, then pinned, per the perf-regression pattern).
  > **2026-09-05 note (OCR-ladder-items session):**
  > `tests/unit/test_perf_floors.py::test_two_hundred_photos_clear_rungs_zero_and_one_fast`,
  > following the file's own established pattern (`elapsed_ms`, best-of-three;
  > `SLACK = 10`; `@pytest.mark.slow`). 200 fixture photos (256×256, half blue
  > half white, generic filenames so every one reaches rung 1 - the worst
  > case), routed through `ocr_ladder.route()` with no detector (rungs 0-1
  > only, since rung 2/full OCR depend on an engine this floor should not
  > need). **Measured 2026-09-05: 444ms best-of-three for all 200
  > (~2.2ms/photo)** on the machine this session ran on. Floor pinned at
  > `450 * SLACK` = 4,500ms. Passes.

## Done means

Change + tests + suite green + committed by name; ladder timings from the
fixture recorded in this file; CHANGELOG under `[Unreleased]`. Acceptance
sentence: a picture-heavy folder indexes with images on by default, obvious
documents get OCR'd, wall photos cost milliseconds and record a truthful
state, and a 2006 photo copied five times still says 2006.
