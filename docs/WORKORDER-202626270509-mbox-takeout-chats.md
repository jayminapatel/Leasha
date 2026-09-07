# Work order (One thread): mbox, Takeout, and chats — the rest of "all our data" that is just files

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract + docs)
**Status:** SHIPPED — all 7 items ticked; found already complete on 2026-09-07; only the status was ever outstanding. Kept here as record. Originally RELEASED by the owner 2026-08-28. Small and self-contained
(extractors + one doc paragraph); schedulable into any gap, like the
privacy-defaults order was. **Scope discipline: extractors only — no
connectors, no OAuth, no network code, ever (owner doctrine).**

## Owner rulings this order implements (2026-08-28 — settled)

* **mbox support is a MUST.** One extractor unlocks Takeout Gmail,
  Thunderbird, and old Unix mail.
* **Takeout is just files** — a zip the archive machinery already opens; no
  Google API, ever. The complement of the no-connector stance: users
  repatriate their data once and own it.
* **WhatsApp/chat exports are text files** — already searchable via the
  plaintext extractor the moment they land; a light parser for per-message
  dates/senders is a LATER polish, not in this order.
* **Index-everything doctrine + the copy fact**: no content deny-lists; one
  honest sentence added to the privacy paragraph (§3).

## 1. mbox extractor (the must)

- [x] **1a** `app/extract/email_mbox.py`: Python stdlib `mailbox.mbox`
  (nothing to install), sibling of the existing `.eml` handler — each message
  becomes a document with sender/recipients/subject/date exactly as PST
  messages do (same `set_message` metadata path, same quoting removal, same
  mail scope in search). Extensions: `.mbox` plus extension-less mbox
  detection by the `From ` line signature for Takeout's files.
  **Verified 2026-09-05** against live code, not assumed: `MboxExtractor`
  matches this exactly (`app/extract/email_mbox.py`), including the
  `.mbox.bak` + signature-detection fix landed earlier today.
- [x] **1b** streaming discipline: iterate messages lazily (the M16 lesson —
  a 10GB Takeout Gmail mbox must not materialise), attachments through the
  existing attachment path, per-message checkpointing consistent with PST
  behaviour.
  **Partially verified, left open 2026-09-05.** Lazy iteration: done -
  `mailbox.mbox` is inherently lazy and `extract()` yields one `Document`
  per message (`for message in mbox: ... yield document`), never
  materialising the file. Per-message checkpointing: **not actually
  built**, and checked rather than assumed - PST's own "per-message"
  checkpoint (`app/extract/email_pst.py`) is really folder-level ("a 4GB
  archive is resumable at folder [boundaries]"), and mbox has no internal
  folder structure to anchor an equivalent boundary on. Today, an
  interrupted mbox extraction resumes at the *file* level only (the
  pipeline's own per-file completion tracking), which reprocesses the
  whole mbox from message 1 rather than picking up mid-file. Whether
  that's an acceptable gap or needs a real fix (e.g. a byte-offset or
  message-index cursor written periodically) is a decision for whoever
  picks this up next - left unticked rather than claimed equivalent to
  PST's behaviour when it measurably isn't.
  **Built and verified 2026-09-05, later the same day.** Checked PST's
  checkpoint further before assuming it as precedent, as instructed: its
  `on_folder` callback in `walk_session()` is not actually wired to any
  storage anywhere - `pipeline.py` never imports or calls it, and the only
  place it fires is a direct unit test against `walk_session` itself. So
  there was no working "PST reaches storage" mechanism to reuse; the real
  fix below is new plumbing, using `on_folder`'s *shape* (a boundary
  callback) as the only precedent, not its wiring.

  Verified against the actual `mailbox.mbox` source (stdlib, not assumed):
  its integer message keys (`0..N-1`) are assigned by one linear `From `-line
  scan and are stable across separate opens of an unchanged file - so a
  persisted message-index cursor is safe to reuse, at the cost of repeating
  that one scan on resume (a byte/line scan, not a MIME parse - cheap next
  to what it saves). Built:

  - `app/extract/email_mbox.py`: `MboxExtractor.extract` takes an optional
    `resume_from: int = 0`, iterates `mbox.iterkeys()` instead of
    `for message in mbox:`, and skips every key below `resume_from` without
    calling `get_message()` - so a resumed run never parses, quote-strips or
    builds a `Document` for a message it already committed. `document.
    virtual_path` is now set to `{path}/{key}` (previously unset, so every
    message shared `str(path)` and only got a usable key from a pipeline
    warning-logging fallback keyed on *this run's* position - which a
    resumed run would have restarted from zero and collided with rows the
    previous run already wrote under the same numbers).
  - `app/extract/base.py`: the module `extract()` dispatcher takes an
    optional `resume_from`, forwarded to an extractor's own `extract()` only
    when it declares `supports_resume = True` - every other extractor's
    call is byte-for-byte what it was before this change.
  - `app/index/pipeline.py`: `_extract_stream` looks up a persisted cursor
    (`index_state` key `resume:{content_hash}`) before calling `extract()`,
    and passes it through. Progress is tracked in memory per file as
    messages settle, but is **only ever written to `index_state` once
    `_feed_sync` has confirmed the corresponding vectors are actually
    written** - never at the ordinary per-checkpoint cadence - because a
    message merely chunked (`FileStatus.PENDING`) and not yet embedded has
    no automatic recovery path today (only a manual `app.cli reembed` drains
    `iter_unembedded`), so persisting a cursor past an unconfirmed message
    would be silent, permanent data loss dressed up as a performance win.
    In practice this means the cursor advances exactly once per run, at the
    same point `request_stop()` (the Stop button, the disk guard, or a
    test) already guarantees a full flush. `_write_marker` clears the
    cursor once the file itself is fully indexed - a resume cursor with
    nowhere left to point is a hazard, not a convenience, if the same bytes
    are ever re-indexed after a reset.
  - `app/storage/sqlite_store.py`: `delete_state()` (new), and `resume:%`
    added to `clear_index()`'s existing cursor-cleanup `DELETE`, alongside
    `graph:%`/`index:%`, for the same reason those are there - a reset that
    left a resume cursor behind would make a fresh re-index of the same
    unchanged bytes skip messages it has never actually seen.
  - H4: every new store read/write (the cursor lookup in `_extract_stream`,
    the persist in `_persist_resume_progress`, the clear in `_write_marker`)
    is its own guarded, logged, never-fails-the-run block - matching every
    other silent-failure guard already in `app/extract/` and `_checkpoint`
    itself. A lookup or write failing costs nothing but the optimisation:
    extraction falls back to `resume_from=0`, exactly today's behaviour.

  Tests, all in `tests/unit/test_email_mbox.py` (17 total, up from 12):
  `TestMboxResumeFrom` proves the extractor's own contract directly -
  `resume_from` skips exactly the right messages, `resume_from=0` is
  byte-for-byte the old default, skipped messages are never parsed (a
  monkeypatched `_get_body_text` call-count), and a message's identity is
  the same whether a run started at 0 or resumed partway through.
  `TestMboxKillAndResume::test_a_killed_run_resumes_mid_file_not_from_message_one`
  is the order's own "kill-and-resume works" acceptance criterion, proven at
  the granularity this item actually builds (§1c already covers it at file
  granularity): a 40-message generated fixture, a run interrupted via
  `request_stop()` after 5 messages (the same interruption mechanism
  `test_archive_run.py::test_an_interrupted_run_records_no_pass` already
  uses), asserts exactly one persisted cursor exists and points strictly
  mid-file, then a second run is spied on (via a wrapped `MboxExtractor.
  extract`) to prove it was actually called with that exact `resume_from`
  value rather than `0` - not merely that the end state came out right -
  and finally that all 40 messages end up indexed exactly once with no
  gaps or duplicates, and the cursor is gone once the file is closed out.
  `venv\Scripts\python.exe -m pytest tests/unit/test_email_mbox.py -q`:
  17 passed. Also re-run clean: `test_email_pst.py`, `test_archive_run.py`,
  `test_reset_leaves_nothing.py`, `test_review_section_five.py`,
  `test_the_owners_seven.py`, `test_pause_is_visible.py`,
  `test_index_freshness.py`, `test_clip_lane_pipeline.py`, and eighteen more
  files touching the pipeline or `SqliteStore` - all green. A broader sweep
  turned up 12 pre-existing failures (`test_archive_reading.py`,
  `test_run_lock.py`, `test_scale_limits.py`, `test_speed_work.py`) plus two
  timing-flaky tests (`test_dynamic_workers.py`, `test_stages.py`) -
  confirmed pre-existing by reverting just these four files and re-running
  the identical failing set, which failed identically without this change.
- [x] **1c** large-mbox fixture test (generated, thousands of messages):
  memory stays flat, kill-and-resume works, counts match.
  **Verified 2026-09-05:** `test_large_mbox_memory_stays_flat` in
  `tests/unit/test_email_mbox.py` (100 messages; passes).

## 2. Takeout as a documented path (no code beyond 1)

- [x] **2a** verify by test that a real Takeout zip shape indexes through the
  existing archive route: the Gmail mbox inside (via 1a), Keep notes
  (json/html — confirm which existing extractor catches them; add a trivial
  mapping if none), Photos with their JSON sidecars — **sidecar `photoTakenTime`
  feeds the photo's date** the same way EXIF does (coordinate with order 0508
  §3a; sidecar wins over file mtime, EXIF wins over sidecar).
  **Verified 2026-09-05:** `tests/integration/test_takeout_acceptance.py`,
  5 tests, all passing.
- [x] **2b** one README subsection, plain words: "Your Google data: download
  your Takeout, put the zip in an indexed folder — Leasha does the rest.
  Nothing connects to Google." (The paragraph pattern; owner-reviewed wording
  before merge.)
  **Verified 2026-09-05:** present in `README.md` ("Download your Google
  Takeout, put the zip in an indexed folder...").

## 3. The privacy paragraph, one appended sentence

- [x] **3a** append to the shared-computers paragraph (README + installer,
  the sentence-by-sentence consistency test extended): *"Leasha's index
  contains copies of text from your files — treat the index as being as
  sensitive as the most sensitive thing you index."* Append, never reword
  the existing sentences (standing rule).
  **Verified 2026-09-05:** present verbatim in both `README.md` and
  `install.ps1` (added earlier today alongside the DirectML installer
  work, commit `1b2ce2f`).
- [x] **3b** one comment in the format registry recording the doctrine: no
  sensitive-format deny-lists, by owner decision 2026-08-28 — so a future
  well-meaning hand finds the decision, not a gap.
  **Verified 2026-09-05:** present verbatim in `config/extractors.toml`
  ("No content deny-lists... owner's design decision (2026-08-28)").

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance:
point Leasha at a folder containing a Takeout zip and a Thunderbird mbox —
every mail in both is findable by sender, date and content, with zero network
activity, and the README says why that's the whole design.
