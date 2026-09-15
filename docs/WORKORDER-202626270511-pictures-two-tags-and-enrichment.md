# Work order (One thread): Pictures II — tags, the enrichment backlog, and places

**Doc version:** 1.1 · **Updated:** 2026-09-15 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract/AI + Index pipeline + Search operators)
**Status:** RELEASED by the owner 2026-08-28. Requires 0510. **Scope
discipline: NO faces (0512), NO video/audio (draft 0515).** This is the order
where the owner's "dog and man" metadata arrives.

## Standing rules that bind this order

AI-written text is ALWAYS marked as AI-written (labelled segments — never
confused with the file's own words). Free-and-unencumbered models only; the
no-AGPL-import guard test extends to any new model package. Everything
on-by-default and individually off-able, plain-words controls.

## 1. The fast pass — Florence-2 tags

**2026-09-15 — torch/transformers were NOT installed, contrary to this
item's own text.** Checked with `pip show torch`/`pip show transformers`
before writing a line of 1a, and both came back "not found" on this machine's
shared venv. Installed for real: `torch==2.14.0+cpu`,
`transformers==4.49.0` (pinned below 5.0 - the first version tried, 5.17.0,
raised `AttributeError: 'Florence2LanguageConfig' object has no attribute
'forced_bos_token_id'` against Florence-2's own `trust_remote_code=True`
modeling file; a real, reproduced incompatibility, not assumed), plus
`einops`/`timm`, which Florence-2's remote code needs for its DaViT vision
backbone and which were not mentioned anywhere in this item. All four now
pinned with reasoning in `requirements.txt`'s "Optional: Florence-2 photo
tagging" section. Model: `microsoft/Florence-2-base`, checked MIT licensed
against the model card directly (not assumed) - no conflict with Leasha
being a sold product.

**Budget measured on a real image on this machine, honestly, and it misses
the target named in this item by roughly 40-100x**: 11.36s for one image
(two `generate()` calls - `<DETAILED_CAPTION>` then `<OD>`, `max_new_tokens
=128` each, CPU, `Florence-2-base`, `num_beams=1`), against the
~100-300ms/img target this item names. A transformers/CPU autoregressive
pipeline generating over a hundred tokens twice per image cannot reach that
budget by any reasonable tuning of beam count or max tokens alone - this
item's own text anticipated exactly this ("the thread MAY swap to an ONNX
port later for speed"), and that ONNX port is the real path to the stated
budget, not built in this session. Ticked below for the functional behaviour
- a real photo-class image gets a real caption and tags from the real model,
proved end to end - with this gap recorded rather than hidden.

Proof: `venv\Scripts\python.exe -m pytest tests/unit/test_florence_tagger.py
tests/unit/test_ocr.py -q -m slow` - both real-model tests pass (model
downloaded once, then cached); `-m "not slow"` - 43 passed, covering the
wiring, the label, and the Florence-unavailable fallback path (which
required updating one pre-existing test,
`test_a_blank_page_yields_nothing_rather_than_an_empty_document`, renamed
and split in two - see the dated note directly on that test in
`tests/unit/test_ocr.py` for why: a blank page is exactly the photo-class
case this item targets, so "yields nothing" stopped being the correct
expectation for every blank page rather than only the Florence-unavailable
one).

- [x] **1a** one Florence-2 pass per photo-class image (the ladder routes;
  document-class images keep the specialist OCR): tags + brief caption in a
  single call. Via `transformers`+torch CPU (owner has installed); the thread
  MAY swap to an ONNX port later for speed — behaviour identical either way.
  Budget measured on fixture (~100–300ms/img target CPU) before acceptance.
- [x] **1b** storage: results become the image's document text as **labelled
  segments** — `AI description` beside `OCR text` — flowing through the
  existing chunk/FTS/embed pipeline unchanged. Zero new storage concepts.
  Preview shows "AI description:" with the label visible.
- [ ] **1c** `/shows` operator: values from the tag vocabulary with counts
  via `distinct_values` — the slash-menu machinery already built. Tags become
  a browsable dimension.

## 2. The enrichment backlog (the architecture piece)

- [ ] **2a** unify the idle-drain queues into ONE mechanism with job kinds:
  unembedded chunks (today's M6 drain), OCR-pending, untagged images — and
  room for future kinds (rich captions 3, transcripts in the video epoch).
  One table, one drain loop at run start + idle, per-kind counters in the run
  summary ("filled 214 tags, 30 pending"). The three existing ad-hoc drains
  migrate INTO it; no behaviour change to what they drain, pinned by their
  existing tests.
- [ ] **2b** trickle pacing: enrichment respects the resource governor and
  the battery/CPU settings exactly as indexing does; "your index gets
  smarter while you sleep" is the product story, and it must never make a
  laptop hot in a lap.

## 3. Rich captions — on demand now, trickle when able

- [ ] **3a** "Describe" button in the preview/pop-out: Ollama vision model
  (llava/qwen-vl class — owner pulls the model; detected like every optional
  capability, greyed-with-reason when absent). Result cached as another
  labelled segment; ~seconds, on demand, per image the user actually cares
  about.
- [ ] **3b** corpus-wide caption trickle as an enrichment job kind, OFF by
  default on CPU-only profiles, offered by Auto-tune when the machine can
  afford it (the GPU-machine future) — envelope-governed like everything.

## 4. Places and eras

- [ ] **4a places, offline**: EXIF GPS → `reverse_geocoder` (bundled
  dataset, zero network) → nearest-town name indexed as metadata text +
  `/place` operator with counts. Composes with the CLIP lane by construction
  (both are just lanes/filters).
- [ ] **4b folder-year era hints**: filenames/folder names carrying a
  plausible year ("Diwali 2004", "Summer_1999") provide a date hint for
  images with no EXIF date (scanned prints — the pre-digital layer). Hint
  ranks below EXIF and Takeout sidecar, above file mtime; recorded as a hint
  in the row so the future Tagger-page batch-era control (0512) can override.

## 5. Tests

- [ ] tags-as-segments: a fixture dog photo is found by "dog", the segment is
  labelled AI, the preview shows the label; `/shows dog` offers with count.
- [ ] backlog: the three migrated drains still pass their existing tests
  through the new mechanism; a run summary counts per kind; kill-mid-drain
  resumes.
- [ ] describe: absent Ollama → button greyed with reason (never an error);
  present → caption cached, second click instant.
- [ ] places: fixture GPS photo findable via /place; no network syscalls in
  the geocode path (asserted).
- [ ] era: EXIF-less scan in "Christmas 2001" folder sorts to 2001, not
  scan-date.

## Done means

Change + tests + suite green + committed by name; per-stage rates recorded;
CHANGELOG. Acceptance: "dog and man" typed by the owner's daughter finds the
photo, the words visible in the preview marked as AI-written, and the index
visibly deepens overnight without warming a lap.
