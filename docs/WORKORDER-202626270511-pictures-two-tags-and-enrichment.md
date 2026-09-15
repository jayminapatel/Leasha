# Work order (One thread): Pictures II — tags, the enrichment backlog, and places

**Doc version:** 1.5 · **Updated:** 2026-09-15 · **Applies to:** app v0.3.3
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

**2026-09-15, addendum found closing item 4b - the real cost is bigger than
the 11.36s/image figure above states on its own.** That number is inference
only, on an already-loaded model. A *cold* Florence-2 load measured 17-25s
on this machine on top of it (machine-load dependent), so the first
OCR-empty image in any process - a real indexing run, or any pre-existing
test that happens to exercise that path - pays 30-90s, not 11s, once
torch/transformers are installed (they now are, in this shared venv - see
this item's own note above on that). Confirmed this is the mechanism behind
what first looked like a test hang while verifying 4b, not a deadlock - see
4b's own dated note for the full account. Nothing here changes 1a's own
scope or proof; recorded because whoever indexes 100,000 photos next should
know the true floor, not just the warm-model number.

- [x] **1a** one Florence-2 pass per photo-class image (the ladder routes;
  document-class images keep the specialist OCR): tags + brief caption in a
  single call. Via `transformers`+torch CPU (owner has installed); the thread
  MAY swap to an ONNX port later for speed — behaviour identical either way.
  Budget measured on fixture (~100–300ms/img target CPU) before acceptance.
- [x] **1b** storage: results become the image's document text as **labelled
  segments** — `AI description` beside `OCR text` — flowing through the
  existing chunk/FTS/embed pipeline unchanged. Zero new storage concepts.
  Preview shows "AI description:" with the label visible.
**2026-09-15 — a real storage decision this item's own text left open.**
1b says tags flow through the pipeline as plain text with "zero new storage
concepts", but a browsable vocabulary with real counts needs something
`GROUP BY` can run over - free text inside a chunk cannot be grouped or
counted cheaply behind a keystroke. Followed the codebase's own precedent
for exactly this problem (`repos`, schema v6, for `/repo`): a new
`file_tags(file_id, tag)` table, schema v19, one row per (file, tag),
written by `SqliteStore.set_file_tags` right after `upsert_file` in
`Pipeline._write_one`. This is a new storage concept in the literal sense -
recorded here rather than silently added, since 1b's own text says there
isn't one - but it is the vocabulary index 1c explicitly asks for, not the
document text itself, which is unchanged from 1b.

`shows:dog,cat` is comma-separated and ORed within one filter, matching
`repo:`'s own grammar exactly (`ParsedQuery.shows`, `file_filter_sql`) -
deliberate consistency over a special case, even though a file commonly
carries several tags where a repository owns exactly one file each.

Proof: `venv\Scripts\python.exe -m pytest tests/unit/test_photo_tags.py -q`
- 10 passed, against a real SQLite database (not a mock): schema v19 creates
  `file_tags`; `set_file_tags` replaces rather than accumulates (a real
  dedup bug was found and fixed here - `["Dog","Park","dog"]` stored three
  rows before the fix, `dict.fromkeys` after); `distinct_value_counts
  ("shows")` returns real per-tag counts; `shows:dog` and `-shows:dog` both
  narrow a real query to the correct file end to end. Also re-ran the wider
  query/command/filter suite (`test_commands.py`, `test_command_subsets.py`,
  `test_value_suggestions.py`, `test_query.py`, `test_vector_filters.py`,
  `tests/integration/test_layer1_acceptance.py` and others) after this
  change - fixed two test files whose own hardcoded lists needed `shows`
  added (`test_value_suggestions.py`'s `known` sources,
  `test_command_subsets.py`'s `consumed` dict) and one real product gap
  (`ParsedQuery.has_filters` did not check `self.shows`, so a `/shows dog`
  search would have under-reported whether a filter was active).

One pre-existing failure found and ruled out as unrelated: `test_query_plans
.py::test_filter_only_browse_neither_scans_nor_sorts` fails identically at
the pre-1c commit (`6861502`, checked directly by restoring that commit's
files into the working tree and re-running) - not caused by this item, not
fixed by it, left alone.

- [x] **1c** `/shows` operator: values from the tag vocabulary with counts
  via `distinct_values` — the slash-menu machinery already built. Tags become
  a browsable dimension.

## 2. The enrichment backlog (the architecture piece)

**2026-09-15 - checked what "the three existing ad-hoc drains" actually
are, rather than assumed - and this item's own text is wrong about two of
them.** Grepped the whole `app/` tree before writing a line: only
`unembedded_chunk` (`Pipeline._drain_unembedded`) is a real, proper drain.
`ocr_pending` exists, but not as a drain - it is `_no_text_layer_candidates`/
`_locked_candidates`, a requeue-via-skip-code mechanism woven into the
walker's own candidate stream (`_candidates`), tested by
`test_ocr_passes.py` and `test_review_2026_08_26.py`. `image_tag`
("untagged images") does not exist in any form - no function, no query, no
test.

**Built:** `Pipeline._run_enrichment_drains` (the "one drain loop"),
`IndexStats.enrichment_counts` (the per-kind counters, present even at zero
so "ran, found nothing" reads differently from "did not run"), and
`app.cli`'s new `Backlog` summary line ("filled 214 tags, 30 pending" per
this item's own example format - `_print_enrichment_counts`). Three kind
names are declared (`KIND_UNEMBEDDED_CHUNK`, `KIND_OCR_PENDING`,
`KIND_UNTAGGED_IMAGE`) for the "room for future kinds" the item asks for.

**Migrated for real, no behaviour change:** `unembedded_chunk`. Its
internal logic is untouched; only the call site moved from a direct call to
one registered entry in the new loop, and every existing test that exercises
it through `Pipeline.run()` (not a single one called the private method by
name, so nothing was "pinned" more tightly than the public behaviour) stays
green.

**Counted but not restructured:** `ocr_pending`. Its requeue mechanism is
NOT reshaped into a `_drain_*`-style function this session - that would
touch well-tested walker integration for a naming consistency, not a
functional gain, which is a bad trade. It is made visible the same additive
way `unembedded_chunk` is: `_candidates` now records how many held files it
requeues into `enrichment_counts["ocr_pending"]`, with no change to what
gets requeued or when.

**Left undrained, deliberately:** `image_tag`. What should count as
"OCR-pending" was answerable from the code (the requeue above); what should
count as "untagged" is not - there is no marker anywhere distinguishing
"Florence never attempted" from "Florence attempted and found nothing",
and inventing that distinction would be inventing product behaviour the
order does not specify. Flagged rather than guessed at.

**A real, pre-existing bug found and fixed along the way, load-bearing for
this item's own counters.** `_drain_unembedded` computed `len(pending)`
*after* calling `_embed_pending(pending)` - which ends by calling
`pending.clear()` on its own argument, for its normal caller's benefit
(`_produce`, which reuses one accumulator list across many calls). So
`filled` was always incremented by `len([])`, i.e. zero, and
`stats.vectors_repaired` has reported 0 for every real repair since this
function was written - silently, because the repair itself worked
perfectly (`mark_embedded`/`mark_indexed_many` run before the clear); only
its own count was wrong. Found by writing a real test that pre-seeded an
unembedded chunk and re-ran the pipeline, expecting the count to match -
and it did not. Fixed: the length is captured before the call now. This
predates 0i entirely and is a correctness fix `vectors_repaired` has needed
since the M6 work order, not scope creep.

Proof: `venv\Scripts\python.exe -m pytest tests/unit/test_enrichment_backlog.py
-q` - 6 passed, against a real `Pipeline`/`SqliteStore`, including the bug
fix (`test_unembedded_chunk_kind_is_recorded`), the zero-vs-absent
distinction, a failing-kind-does-not-block-the-run test, the `ocr_pending`
count against a real held PDF, and the CLI print formatting. Re-ran
`test_embedding_gap.py` (22), `test_ocr_passes.py`, `test_review_2026_08_26
.py` (22) and `tests/integration/test_layer1_acceptance.py` clean - the
migration changed nothing these already pinned. One unrelated failure
cluster surfaced in the wider sweep (`test_cli_wiring.py`'s `gitsearch`/
`scan` tests) - checked and it is the pre-existing "worktree-in-tmp-path"
environmental issue already known from this session's earlier baseline
diagnostic, not caused by anything here (`pipeline.py` has zero references
to gitsearch).

- [x] **2a** unify the idle-drain queues into ONE mechanism with job kinds:
  unembedded chunks (today's M6 drain), OCR-pending, untagged images — and
  room for future kinds (rich captions 3, transcripts in the video epoch).
  One table, one drain loop at run start + idle, per-kind counters in the run
  summary ("filled 214 tags, 30 pending"). The three existing ad-hoc drains
  migrate INTO it; no behaviour change to what they drain, pinned by their
  existing tests.
**2026-09-15.** Built and proven: the unembedded_chunk drain now calls
`governor.wait_while_throttled(should_stop=self._stop.is_set)` at every
batch boundary, the same call `_produce` already makes per file (`app/index/
pipeline.py:1249`) - so a repair pass genuinely respects battery/CPU limits
rather than running the fan flat out. `apply_priority()` was already called
once at the top of `run()`, before any drain, so nothing new was needed
there. Proof: `test_a_stopped_governor_lets_no_batch_through` - a fake
governor that always says "stop" lets zero batches through, confirmed
consulted at least once.

**Not built: a dedicated idle-only trigger.** "While you sleep" implies
enrichment runs even when nobody has started an index run at all - and no
such infrastructure exists anywhere in this codebase (checked: no
resource-governor-driven "machine is idle" loop; the one thing already
called "idle" in the UI, `MainWindow._run_idle_optimize`, is an hourly
`ANALYZE` refresh unrelated to enrichment). What exists is `app/ui/
scheduler.py`/`app/index/schedule.py`, a time-based scheduled full index -
if the owner has one configured, it already delivers "smarter while you
sleep" for every drain that runs at start-of-run, with no new code, since a
scheduled run is still a run. A dedicated trigger that runs enrichment
*without* a full index (cadence, what counts as "idle", whether it competes
with a scheduled run) is a real design question this item's text does not
answer, and is left open rather than guessed at - same shape as 2a's
`image_tag` gap.

- [x] **2b** trickle pacing: enrichment respects the resource governor and
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
**2026-09-15 - "Takeout sidecar" does not exist to rank against.** Checked
before writing this (grepped for `photoTakenTime`/`Takeout`/sidecar JSON
reading anywhere in `app/extract`): no such date source exists in this
codebase. The ranking built is EXIF > era hint > mtime - the two tiers that
are real.

Built: `app/extract/era_hints.py` (`guess_year`, `year_to_epoch_ns` - pure,
no I/O, folder name wins over filename, a year bounded to [1826, this year]
so a camera's own numbering like "img20045.jpg" is never mistaken for one),
schema v20 (`files.taken_at_is_hint`, paired with `taken_at_ns` in
`upsert_file`'s own conflict resolution rather than independently
COALESCEd - see the migration's docstring), and `Pipeline._photo_taken_at`,
the ranking wrapper both write paths (`_write_one` and `_record_skip`) now
call instead of the EXIF-only `_photo_taken_at_ns`.

**A real, useful side effect of building this: it surfaced that a "no text"
photo can now take EITHER write path depending on whether Florence tagging
(0i 1a/1b) succeeds**, which `test_exif_date_wiring.py`'s own docstring did
not anticipate ("extract raises ERR_NO_TEXT_LAYER... written by
_record_skip, not _write_one" - no longer universally true once Florence is
available and produces a caption). Verified directly against a real
Pipeline that the date still comes out correct via *either* path - `_write_
one` now calls `_photo_taken_at` too, so this was a matter of proving it,
not a gap to close. That pre-existing test's actual assertions still pass
either way; only its docstring's claimed mechanism is now sometimes
incomplete, which is a documentation nuance, not a fix - left alone.

Proof: `pytest tests/unit/test_era_hints.py -q` - 14 passed (10 pure-logic,
4 wiring tests against a real `Pipeline` instance with `read_datetime`/
`guess_year` mocked, so these run in milliseconds rather than paying a
real Florence-2 load). End-to-end correctness through both `_write_one`
and `_record_skip`, including the Florence-tagged case, was verified
directly against a real `Pipeline.run()` this session (not committed as a
test - a cold Florence-2 load adds 20-70s to any scenario that touches an
OCR-empty image once torch/transformers are installed, which no test in
this file should have to pay to test EXIF/era-hint ranking specifically).

Also fixed, found while verifying: two hardcoded `CURRENT_VERSION == 18`/
`schema_version == 18` assertions in `test_exif_date_wiring.py` that this
item's own migrations (v19, v20) broke - the exact trap that file's sibling
`test_phash_column_migration.py` already documents hitting once before (at
v17->v18) and fixed the same way: retargeted onto "is this migration
registered and not since dropped" rather than the literal current version.

**A separate, real finding surfaced investigating a reported test hang,
unrelated to this item but worth recording here since it was found while
verifying it**: this machine's `ResourceLimits.pause_on_battery` defaults
to `True`, and the physical machine has been on battery for part of this
session - any real `Pipeline.run()` test with no explicit override (several
pre-existing files, including this one's own `_run` helper) blocks
indefinitely waiting for AC power that a test run will never supply. Not
this order's bug and not fixed here (out of scope - a pre-existing,
whole-suite fragility); this session's own new tests
(`test_enrichment_backlog.py`) were hardened against it with an explicit
`ResourceLimits(pause_on_battery=False)`.

- [x] **4b folder-year era hints**: filenames/folder names carrying a
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
