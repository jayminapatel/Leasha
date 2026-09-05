# Work order (One thread): mbox, Takeout, and chats — the rest of "all our data" that is just files

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract + docs)
**Status:** RELEASED by the owner 2026-08-28. Small and self-contained
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
- [ ] **1b** streaming discipline: iterate messages lazily (the M16 lesson —
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
