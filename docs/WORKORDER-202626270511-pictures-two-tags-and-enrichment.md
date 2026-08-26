# Work order (One thread): Pictures II — tags, the enrichment backlog, and places

**Doc version:** 1.0 · **Updated:** 2026-08-28 · **Applies to:** app v0.3.3
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

- [ ] **1a** one Florence-2 pass per photo-class image (the ladder routes;
  document-class images keep the specialist OCR): tags + brief caption in a
  single call. Via `transformers`+torch CPU (owner has installed); the thread
  MAY swap to an ONNX port later for speed — behaviour identical either way.
  Budget measured on fixture (~100–300ms/img target CPU) before acceptance.
- [ ] **1b** storage: results become the image's document text as **labelled
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
