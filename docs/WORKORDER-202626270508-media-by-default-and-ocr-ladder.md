# Work order (One thread): media files by default, and the OCR ladder that makes it affordable

**Doc version:** 1.3 · **Updated:** 2026-09-07 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract + Index pipeline + formats)
**Status:** SHIPPED — all 17 items ticked, closed 2026-09-07 (lane-a closed
§3a's display/sort wiring; lane-b closed §2e's rung-threshold tunable). Kept
here as record. Originally RELEASED by the owner 2026-08-28, queue position
first of the new batch, after `WORKORDER-202626270326-workspace-features.md`
— this order was the foundation the picture orders (0510, 0511, 0512) build
on. **Scope discipline honoured throughout: pipeline-only, plus the one small
Index Tuning control §2e itself called for.**

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
- [x] **2e** each image pays the ladder once (H1 settle); rung thresholds are
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
  > **2026-09-07, lane-b (tunables).** The remaining half — the tunables/
  > Index Tuning screen half — is built, closing this item. Read
  > `app/extract/ocr_ladder.py` end to end first, per the standing rule,
  > rather than trusting this item's own "rung thresholds" paraphrase: rung
  > 0 (metadata) has no numeric threshold, only filename patterns; rung 2
  > (the detection probe) decides on box count alone (`if not boxes:`), no
  > confidence number anywhere; the module's own comments mention
  > "saturation" and "row-projection periodicity" but neither is
  > implemented — `# For now, simple heuristic: mostly white = document`.
  > The one real hardcoded threshold is rung 1's `if white_fraction > 0.7:`
  > (`_thumbnail_stats`, was line 134).
  > Promoted to `OCR_WHITE_PAGE_PERCENT` (`app/core/settings_registry.py`),
  > a whole-number percentage — `Setting.kind` has no float kind — default
  > `70`, minimum `1`, maximum `100`, group `Tuning`, surface
  > `indexing.tuning`. Label: "How plain white a photo must be to count as
  > a scanned page." Wired into `app/core/config.py` (`Settings.
  > ocr_white_page_percent`, `SETTING_KEYS`, the `_as_int` parse line) the
  > same way as every other Coverage-group setting.
  > `ocr_ladder.py` itself stays settings-free, per its own stated rule of
  > staying importable and testable with nothing but Pillow: `route()` and
  > `_thumbnail_stats()` gained a `white_fraction_threshold` keyword
  > (default `WHITE_FRACTION_THRESHOLD_DEFAULT = 0.7`, the exact old
  > literal), and the caller — `app/extract/ocr.py::ocr_image` — is what
  > reads `OCR_WHITE_PAGE_PERCENT` (via the new, cached
  > `_white_fraction_threshold()`, the same shape as `app/extract/pdf.py`'s
  > `_pages_from_settings`) and divides by 100 before passing it in. A
  > settings read failure falls back to the same literal, so default
  > behaviour is bit-for-bit unchanged unless the setting is touched.
  > Surfaced on the Index Tuning screen's Coverage group
  > (`app/ui/widgets/long_run_box.py::LongRunBox`), the same widget the
  > order 0b delivery notes already name for `PDF_OCR_PAGES` and the rest of
  > "what gets read" — a `QSpinBox` named `OCR_WHITE_PAGE_PERCENT`, wired
  > through `LongRunBox.load()`/`.values()` exactly like its neighbours.
  > `docs/WORKORDER-202626270114-index-tuning.md` was read in full for a
  > cross-reference: its own §4c-3 is already ticked and names four specific
  > settings, none of which is this one, and the order's status there is
  > delivered — left untouched rather than reworded, per the standing rule.
  > Tests: `tests/unit/test_ocr_strategy.py::
  > test_the_white_page_threshold_has_a_control_and_a_sensible_default`
  > (registry entry, default, bounds, surface); `tests/unit/test_ocr_ladder.py::
  > TestWhiteFractionThreshold` (three cases: default reproduces the old
  > fixed behaviour on a synthetic thumbnail; a stricter threshold sends the
  > same image to the detection rung instead; a looser one fast-accepts an
  > image the default would not — the actual behaviour-change proof);
  > `tests/unit/test_ocr.py::test_ocr_image_passes_the_settings_threshold_to_the_ladder`
  > plus three more there covering `_white_fraction_threshold()` itself
  > (reads the percent as a fraction, falls back on a broken settings read,
  > and is cached rather than re-read per image). `pytest -q` on
  > `test_settings_registry.py`, `test_settings_reachable.py`, `test_ocr.py`,
  > `test_ocr_ladder.py`, `test_ocr_strategy.py`, `test_ocr_passes.py`,
  > `test_config.py`, `test_setting_defaults.py` and `test_settings_are_used.py`
  > together: 470 passed, 0 failed. The Qt-dependent acceptance test for the
  > Coverage group (`tests/unit/test_index_tuning_acceptance.py`, the
  > plain-words deny-list guard) could not be run in this sandbox — no
  > `libEGL` here — but the new label and tooltip were checked by hand
  > against its deny-list (onnx, directml, intra-op, quantised model, batch
  > size, ivf, fts5, lance, embedding vector) and contain none of them; a
  > Windows-venv pass should still run it for real.

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
  > **2026-09-05 note (OCR-ladder-items session) — confirms un-ticked is
  > right, and why the wiring does not actually reach storage:** built the
  > missing end-to-end test §4 calls for
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
  > **2026-09-07, lane-b (the follow-up the note above asked for).** Built, on
  > the diagnosis above rather than re-deriving it. `files.taken_at_ns`
  > (migration `_v18_photo_taken_at`, schema v17 -> v18): additive, nullable,
  > nanoseconds since the epoch so it is directly comparable with `mtime_ns`,
  > with a partial index `WHERE taken_at_ns IS NOT NULL` in the shape
  > `idx_files_phash` already uses. `mtime_ns` is left exactly as it was and
  > still holds the file's real mtime, which is what the note above identified
  > as the reason a new column was needed at all - H1's change detection
  > compares against it, so a photo carrying its shot date there would look
  > changed on every rescan forever. `test_rescanning_an_unchanged_photo_does_
  > not_look_changed` asserts the property that rejected fix would have
  > destroyed.
  >
  > **Written from the pipeline, not from the `Document`, and that is the one
  > design decision here worth stating.** The note above lists the write path
  > as "every `pipeline.py` call site that currently passes
  > `mtime_ns=candidate.mtime_ns` (at least four)". Checked each of the four:
  > only two ever hold an image. `_write_one` takes a photo whose OCR found
  > text; **`_record_skip` takes the majority of any real photo library** - an
  > ordinary photograph contains no text, so `extract()` raises
  > `ERR_NO_TEXT_LAYER` and no `Document` is ever produced for it. A shot date
  > threaded through `Document.date` alone would therefore have worked on a
  > captioned fixture and on almost none of the owner's twenty years of
  > photos. So `Pipeline._photo_taken_at_ns` reads the EXIF date at both write
  > sites, gated on `reads_by_ocr` exactly as `_maybe_compute_phash` and
  > `_maybe_embed_image` already are. `_write_name_only` and `_write_marker`
  > (the other two of the four) are `.mp4`-class files and archive markers -
  > neither is ever an image, so neither was touched.
  >
  > `Document.date` and `DocumentBuilder.date` are declared all the same, so
  > that `builder.date = exif_date` in `ocr.py`/`raw.py` - the dead write this
  > item was reopened over - now means something. It is no longer the
  > mechanism the index depends on, which is the point.
  >
  > **`after:`/`before:` (`app/storage/filters.py::_date_clause`), and the
  > shape is measured rather than chosen.** The obvious spelling,
  > `COALESCE(f.taken_at_ns, f.mtime_ns) <= ?`, wraps the columns in a
  > function and so cannot use any index: on a 200,000-row table with a
  > selective cutoff it scanned in **5.79ms** where the plain single-column
  > comparison took **0.37ms**. The two-branch OR now emitted keeps both
  > indexes in play and runs in **0.86ms** (`MULTI-INDEX OR`). `files` is
  > aimed at twenty million rows and this fragment is built on every keystroke
  > of a filtered search, so the COALESCE form was rejected on the number, not
  > on taste. `test_the_date_filter_still_uses_an_index` pins the plan.
  >
  > **A second, real bug found while doing this, inside §3a's own words.**
  > `read_datetime()` promised "DateTimeOriginal (camera time)" in its
  > docstring and returned something else: it iterated `PIL.ExifTags.TAGS` (a
  > dict keyed by tag *number*) and took the first date tag it met, so tag 306
  > `DateTime` - "last modified", which any photo software rewrites when it
  > saves - was always reached before 36867 `DateTimeOriginal`. Measured on a
  > fixture carrying both: it returned **2019-03-01**, the last-saved date,
  > not the 2006 shot date. That is the copy-date bug this item exists to fix,
  > reappearing one layer down and surviving the fix. The preference is now
  > stated as data (`_DATE_TAGS`, original -> digitized -> modified) and
  > iterated directly.
  >
  > Errors: nothing here is fatal and nothing is a skip. A photo with corrupt
  > or absent EXIF falls back to `mtime_ns` quietly and the run carries on -
  > `test_a_corrupt_photo_never_halts_the_run` proves the good photograph
  > beside a corrupt one still indexes, still carries its 2006 date and is
  > still found by `before:2010`.
  >
  > Tests: `tests/unit/test_exif_date_wiring.py` (new, 20 tests) - the tag
  > preference, the migration on a fresh database and on one carried forward
  > from v17, `upsert_file` storing it and *not* blanking it for the callers
  > that know nothing about it, the filter in both directions plus the
  > mtime fallback for every non-photograph, the query plan, and five
  > end-to-end cases through a **real `Pipeline` run** covering both photo
  > write paths. Watched fail first against the unfixed code (19 failed), then
  > pass (28 passed with `test_exif.py`).
  >
  > **Left unticked, and this is the reason - the same discipline the
  > 2026-09-05 note above applied to this item.** §3a is three clauses:
  > storage, `after:`/`before:`, "and any date display use it". The first two
  > are done and proved. The third is not, and it is not a line of polish that
  > can be tacked on: `keyword.py` (5 sites), `vector.py` (1) and
  > `sqlite_store.py::_filter_only` all `SELECT f.mtime_ns` into every hit, so
  > `SearchResult.mtime_ns` - whose own docstring says it exists "for the
  > result row to show" - is what a date column renders.
  >
  > Projecting the shot date there is six lines. **Doing only that would be
  > worse than doing nothing**, because the ORDER BYs beside those SELECTs
  > (`keyword.py:240`, `_filter_only`'s `/newest`//`oldest`) still sort on
  > `mtime_ns`: a list labelled newest-first would render 2006 above 2019 and
  > look broken. Display and sort are one coherent change, and sorting is
  > ranking - it also pulls in `recency.py::freshness` and `folding.py` - so
  > it changes result order everywhere at once and wants its own measured
  > pass, not a fold-in behind a filter fix. It is also the half that shows on
  > screen, and this order's own header sets "scope discipline: this order is
  > pipeline-only. No new UI".
  >
  > So: storage and `after:`/`before:` shipped and tested here; the display
  > and sort clause named precisely, with its file list and its trap, for
  > whoever picks it up. The §4 acceptance test below is ticked, because the
  > sentence it encodes is now true.
  > **2026-09-07, lane-a (second pass).** Built the third clause the note
  > above named: display and sort now use `taken_at_ns`, falling back to
  > `mtime_ns` when it is null, everywhere a result's date reaches a person
  > or decides an order. Read every named site before changing it, and two
  > of the three file names in the note above had drifted from what the code
  > actually does - stated precisely below rather than silently corrected.
  >
  > **`SearchResult` gains `taken_at_ns: int = 0`, beside `mtime_ns` rather
  > than instead of it.** `mtime_ns` is read for real change-detection-shaped
  > reasons downstream of the row too (nothing in this pass touched
  > `pipeline.py`'s own use of the column), so overloading its meaning at the
  > SELECT sites would have made "which date is this" depend on which caller
  > was asking. Every site that used to project `f.mtime_ns` now projects
  > `f.taken_at_ns` alongside it; `_to_result` copies both across unchanged.
  >
  > **The 5 `keyword.py` sites, the ORDER BY at line 240, and `vector.py`'s
  > SELECT** were exactly as the note named them, `_filter_only`'s
  > `ORDER BY f.mtime_ns DESC` included - confirmed by reading the file
  > rather than trusting the line numbers. Two corrections to the note's own
  > file list, found by grepping rather than assumed from its count:
  > `vector.py` has **two** SELECT sites that carry `mtime_ns`, not one -
  > `hydrate_images` (the CLIP image lane - reverse-image search and "find a
  > photo like this", so its hits are photographs more often than any other
  > lane's) was missed alongside `hydrate`, and is now the more important of
  > the two. And `sqlite_store.py::_filter_only` does not exist - there is
  > only one function of that name, in `keyword.py`. The function actually
  > matching the note's description (a filter/text browse honouring
  > `/newest`/`/oldest`, SELECT and ORDER BY both) is `sqlite_store.py::
  > browse_files`; treated as the intended site.
  >
  > **The SQL shape, and it is measured rather than styled, the same
  > discipline `_date_clause` set.** `ORDER BY COALESCE(f.taken_at_ns,
  > f.mtime_ns) DESC` is not sargable, for the same reason the WHERE form
  > was rejected - but for an ORDER BY the cost is worse: it does not just
  > lose an index, it stops SQLite walking `idx_files_mtime` newest-first and
  > stopping at `LIMIT`, the mechanism `idx_files_mtime`'s own comment in
  > `schema.sql` describes and `test_filter_only_browse_neither_scans_nor_
  > sorts` pins the plan for. Measured on a 200,000-file table (600,000
  > chunks, this project's own ratio): the plain column scan-and-stop is
  > **0.011ms**; the COALESCE form is **39.5ms** - a full materialise-and-sort
  > of the whole table; a `CASE` expression is no better, **42.2ms**. The fix
  > splits into two queries - `taken_at_ns IS NULL` ordered by `mtime_ns`,
  > `taken_at_ns IS NOT NULL` ordered by `taken_at_ns`, each still walking its
  > own index and stopping at `LIMIT` - merged in Python
  > (`app.storage.filters.merge_by_date`, new). Measured: **0.10ms**. Used in
  > `keyword.py::_filter_only` and `sqlite_store.py::browse_files`'s
  > no-search-text browse, the two places this can be the whole `files`
  > table. `engine.py`'s post-fusion `/newest`/`/oldest` sort and the
  > `browse_files` exception-fallback sort stay on a bare
  > `taken_at_ns or mtime_ns` Python key without the split - both already
  > operate on a small, already-fetched list (at most `FUSED_LIMIT` or
  > `capped` rows), so there is no index to lose. `browse_files`'s *scored*
  > branch (search text plus `/newest`/`/oldest`) keeps `COALESCE` - checked
  > with `EXPLAIN QUERY PLAN` rather than assumed safe by analogy: that
  > query's `ORDER BY score` already cannot use an index (`bm25()` is a
  > computed value), so it already pays for a full sort of its
  > `MATCH`-bounded row set, and COALESCE adds no new class of cost to a sort
  > that was happening regardless.
  >
  > **`recency.py::blend`** now reads `taken_at_ns` before `mtime_ns` when
  > scoring a hit's freshness. This is not the internal-bookkeeping half of
  > the codebase - it is a display-facing nudge whose result is narrated back
  > to the person verbatim in `presenter.why_result`'s "Recent, so it came
  > slightly ahead of equally good older ones" line, so a photo copied
  > yesterday but shot years ago must not read as fresh. `freshness()` itself
  > is untouched - it already took a bare timestamp, and the caller now
  > decides which one.
  >
  > **`folding.py::_newest_first`** now does the same substitution: it
  > decides which member of a folded group becomes the `head` - "the newest
  > of the group ... which is the whole point" per `fold`'s own docstring -
  > so a burst of the same photograph, re-exported at different times, shows
  > the shot itself as the head rather than whichever export was saved most
  > recently.
  >
  > **Step 6, the UI read site - found, and it is `app/ui/presenter.py`, not
  > `shell.py`.** `presenter.to_row` is the one place a `SearchResult`
  > becomes what every results list actually draws, and it was reading
  > `result.mtime_ns` straight onto `ResultRow.mtime_ns`. Changed to prefer
  > `taken_at_ns`, keeping the field's name - `_build_group`'s `when`/
  > `when_exact` and `result_tooltip`'s date both read `ResultRow.mtime_ns`
  > already, so the one-line substitution there is what makes them correct
  > too, without touching either. `why_result`'s own "Recent, so it came
  > slightly ahead..." line read `result.mtime_ns` directly (not through
  > `ResultRow`) and needed the same one-line fix separately. Both are
  > one-line, which-attribute-this-reads changes - no structural UI work, and
  > `app/ui/shell.py` was not touched at all.
  >
  > Left alone, and said so rather than guessed at: `search_files_by_name`'s
  > own default "empty box, newest first" listing (a different, narrower
  > browse than `browse_files`, not named in the note) now also projects
  > `taken_at_ns` - `browse_files`'s exception-fallback path needs the column
  > - but its own `ORDER BY mtime_ns DESC` is untouched. `presenter.file_rows`
  > (the Files tab's own listing, built from a different store query
  > entirely) and `sqlite_store.py`'s `repo_files`/`code_files` (repository
  > browsing - source code, not photographs) were checked and left alone for
  > the same reason: not named, and outside this pass's scope.
  >
  > Tests: `tests/unit/test_exif_date_wiring.py` (+6: `merge_by_date`
  > directly, `_filter_only`'s sort proven with a fixture whose shot date and
  > copy date sit on opposite sides of a third file's date - the only pairing
  > that can tell "sorts by `mtime_ns`" apart from "sorts by `taken_at_ns`
  > first" - and the display substitution through `presenter.to_row`, both
  > directions), `tests/unit/test_recency.py` (+1), `tests/unit/
  > test_folding.py` (+1), `tests/unit/test_query_plans.py` (updated:
  > `_filter_only`'s plan test now checks both of its two queries, and both
  > still seek their own index). 100 passed in the touched files; the
  > pre-existing Qt-import failures and the `test_layer3`/`test_layer4`
  > disk-space-governor failures this sandbox always shows were confirmed
  > against the unmodified code first, not assumed.
  >
  > §3a is fully ticked now: all three clauses - storage, `after:`/`before:`,
  > and display/sort - are built and tested.
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
  >
  > **2026-09-05 note (OCR-ladder-items session), independently confirming
  > the above and adding one open fact:** `pillow-heif` itself is not
  > installed in this venv (`pip show pillow-heif` and `import pillow_heif`
  > both fail) - so the "installed by owner" half of this item's own text
  > is still unverifiable from here, checked rather than assumed. Noted so
  > a later pass does not have to re-discover it.
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
- [x] EXIF date: fixture photo with 2006 DateTimeOriginal + 2019 mtime indexes
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
  > **2026-09-07, lane-b.** True now, and ticked. The `xfail(strict=True)`
  > marker is gone from
  > `tests/unit/test_exif.py::test_exif_date_beats_mtime_in_a_before_filter_end_to_end`
  > - which is what the note above said the signal to remove it would be -
  > and the test passes on its own terms rather than by being relaxed: the
  > `taken_at_ns` it writes is derived from what `read_datetime()` actually
  > returned, not from a literal, so it still proves the chain end to end.
  > Two corrections to it while removing the marker, both mechanical: the row
  > now carries the shot date in its own column (see §3a above), and the
  > `WHERE` was `WHERE {where}` where `file_filter_sql` returns a fragment
  > with a leading ` AND ` - so the assertion had been failing on a SQL syntax
  > error, one layer before the behaviour it was written to test. It also now
  > asserts the row keeps its real 2019 mtime, so "indexes as 2006" cannot be
  > satisfied by having overwritten the file's own date.
  >
  > The wider proof this item asked for lives beside it in
  > `tests/unit/test_exif_date_wiring.py` (new, 20 tests), including the same
  > sentence driven through a **real `Pipeline` run** for both photo write
  > paths - the captioned one and the far more common one with no text in it
  > at all. Watched fail first: 19 failed against the unfixed code, 28 passed
  > after.
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
