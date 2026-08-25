# Work order: indexing 600GB, heading for 1.5TB

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3
**Layer:** Backend - `app/index/`, `app/cli.py`, `app/core/settings_registry.py`

The corpus was sized at **600GB today, expected to reach 1.5TB**. Everything in
this project was designed, reviewed and measured against 100GB. That is a six to
fifteen-fold change, and it moves the binding constraint from correctness to
wall-clock time.

Commit the working tree before starting.

---

## 1. What already scales, and what does not

Checked rather than assumed, because the answer decides how much work this is.

**The storage layer is in better shape than the number suggests.**

- LanceDB builds an **IVF_PQ** index past `INDEX_MIN_ROWS = 100_000` and
  retrains when the row count doubles. Compaction runs every 50,000 rows.
- SQLite has the indexes the August review flagged as missing:
  `idx_files_source_kind` and `idx_files_mtime` are both in `migrations.py`.
- Change detection is a `files` table keyed on path, mtime, size and hash, so a
  killed run resumes for the cost of a `stat()` per file.

None of that needs rebuilding. **The problem is time.**

| Corpus | At the measured 84 MB/min |
|---|---|
| 600 GB | ~122 hours - **5 days** |
| 1.5 TB | ~312 hours - **13 days** |

That rate is one 97-second sample from a run that also loaded the OCR engine, so
treat it as an order of magnitude, not a forecast. It is still enough to settle
the design question: **this is a background job measured in days**, and every
decision below follows from that.

### And it is paid once

The owner's correction, which changes the shape of this work more than the size
does: **almost all of the corpus is historic data that does not change.**

So the five to thirteen days is a **one-off**, not a recurring cost - which
makes it far more tolerable than the number first suggests. Nobody minds a job
that runs for a week if it runs for a week *once*.

But it moves the design problem somewhere else. On a static corpus the expensive
thing stops being extraction and becomes **the re-walk**: `stat()`ing several
million files to prove that nothing changed. That is hours of work to learn
nothing, and it happens on **every** run. See section 2a - it is now the most
valuable thing in this document.

## 2. Measure the corpus before indexing it

**Do this first, and do not skip it.** Every estimate here depends on the mix of
file types, and guessing the mix is how a five-day estimate becomes fifteen.

Add `app.cli scan ROOT...` - a walk with no extraction, no embedding and no
writes. It answers:

- [ ] How many files, and how many bytes, in total.
- [ ] The same **broken down by extension**, biggest first.
- [ ] How many files route to each tier: a registered extractor, a converter,
      OCR, or nothing at all.
- [ ] How many are **scanned images or image-only PDFs**, because that is the
      number that decides the whole schedule (section 3). A PDF's text layer can
      be checked cheaply - `page.get_text()` on the first page or two - without
      running OCR on anything.
- [ ] How much is inside `.git` directories, which may be a large fraction of a
      repository corpus and is mostly not worth indexing twice.

Print it as a table and support `--json`. It should run in minutes on 600GB
because it never opens a file, only `stat`s it.

**This is also the honest input to the progress bar.** `progress_for` currently
returns an indeterminate range until the walk completes, precisely because
nothing knows the total in advance. A `scan` result passed as `total_estimate`
turns the bar into a real percentage from the first tick of a multi-day run,
which is the difference between a run somebody trusts and one they kill.

## 2a. Archival roots: the answer to a corpus that does not change

**This is the highest-value item here.** Most of the corpus is historic. Almost
every re-walk therefore does millions of `stat()` calls and finds nothing, and
then does it again on the next run.

Two settings make that worse than it looks:

- `INDEX_SCHEDULE` is `manual` today, but `INDEX_INTERVAL_HOURS` defaults to
  **6**. Switching scheduling on would re-walk 1.5TB four times a day to
  discover, four times a day, that a fifteen-year archive is still fifteen years
  old.
- `prune_missing` walks to find deletions. On an archive there are none.

**Mark a root as archival.** A root the owner declares static is walked once,
then checked on a much longer cycle - weekly, or only when asked.

- [ ] Per-root setting in the Folders-to-index UI: **Live** or **Archive**.
      Not a global switch: a corpus is nearly always both, and the mail folder
      that changes hourly sits beside twelve years of project files that do not.
- [ ] An archival root that has completed a full pass records
      `archived_at` and its file count. Later runs skip it entirely unless
      `--recheck-archives` is passed, the interval has elapsed, or **the
      top-level directory mtime has moved** - which is a single `stat()` per
      root and catches the common "somebody dropped a folder in" case.
- [ ] `--recheck-archives` forces the full walk when something has plainly
      changed, and the UI offers the same as "Rescan archived folders now".
- [ ] The Indexing panel must **say** a root was skipped and why, with its
      file count and the date of its last full pass. A skipped root that looks
      identical to an empty one is how somebody concludes their archive was
      never indexed.

**What this is worth:** an incremental run over a mostly-static 1.5TB goes from
hours of fruitless `stat()`ing to seconds - it walks only the live roots. That
turns background indexing from something that must be scheduled carefully into
something that can just run.

**What to be careful about.** An archive that is skipped is an archive nobody is
checking, so the failure mode is a file that changed and never got re-read.
Hence the directory-mtime check, the visible status, and the explicit rescan.
The rule is *skip cheaply, but never silently*.

## 3. OCR is the schedule, not a feature

Measured at **3.6 seconds per page** - about eight times what embedding a
passage costs. 100,000 scanned pages is 100 hours **on its own**, before any
other file is touched.

It is on by default, by the owner's explicit instruction, and that was the right
default at 100GB. At a terabyte it needs to stop being a single decision.

**Two passes, and the order matters:**

- [ ] **Pass one: everything except OCR.** `app.cli index --skip-ocr` (or
      `--no-ocr`) walks the whole corpus and indexes every file whose text can be
      read without OCR. Image-only files are recorded with a skip code that says
      *"held for OCR"* - not a failure, a queue.
- [ ] **Pass two: OCR only.** `app.cli index --only-ocr` picks up exactly those
      rows and fills them in.

The point is that **search becomes useful after pass one**, in a day or two
rather than a fortnight, and pass two runs behind it without anyone waiting. A
single pass that does both means nothing works until everything works.

- [ ] Both flags need a UI equivalent (non-negotiable 11). The natural shape is
      a three-way choice in Settings - *text only*, *text then images*, *images
      now* - not two checkboxes.
- [ ] The skip ledger must show "held for OCR" as its own row with a count, so
      the queue is visible rather than looking like 40,000 failures.

## 4. Settings that are wrong at this size

Each of these is defensible at 100GB and actively harmful at 1.5TB.

| Setting | Now | Why it hurts | Suggested |
|---|---|---|---|
| `INDEX_MEMORY_MB` | 1500 | Already trips on a 14-file run. The models are ~1GB resident before any work, so the ceiling is nearly all baseline; a long run pauses constantly and each pause is dead time. | 4000-6000, and see below |
| `REQUIRED_FREE_GB` | 150 | Checked once at start. At 1.5TB the index itself may approach this. | Derive from corpus size, or raise to 300 |
| `INDEX_WORKERS` | 0 (auto) | Auto is 4. Extraction is I/O bound and parallelises well. | Measure 4 vs 8 on a real subset |
| `EMBED_BATCH` | 256 | Fine, but confirm it is actually reaching the embedder - the review found it silently re-split to 64 once. | Verify, do not assume |

- [ ] **The memory ceiling needs to measure growth, not total.** The governor
      already raises its floor when a pause frees nothing - `_accept_resident` -
      which is the right instinct. Confirm that path is reached before a long run
      rather than after ten minutes of oscillation, because oscillation at this
      scale costs hours.

## 5. Reporting that survives a week

A progress line designed for a run of minutes is unreadable over days.

- [ ] **Throughput over a window, not since the start.** `files_per_minute`
      averaged over four days tells you nothing about whether it is still moving.
      Report the last fifteen minutes as well.
- [ ] **An ETA that is allowed to say "unknown".** With `scan` (section 2) it can
      be real; without it, no number is better than a wrong one.
- [ ] **A daily line in the run log** - files done, bytes, skips by code, hours
      elapsed - so a week-long run can be reviewed without reading a million
      lines.
- [ ] **Pauses are already visible** as of this session; check the wording still
      makes sense when the pause is the twentieth of the day.
- [ ] **Checkpoint the cursor at least every 2 seconds**, which it does. Confirm
      a kill at hour 60 resumes at hour 60 and not hour 0 - by actually killing
      one, not by reading the code.

## 6. Search at ten million vectors

- [ ] `num_partitions` is `sqrt(rows)` **capped at 4096**. Past ~16 million
      vectors that cap binds and partitions grow, so search either slows or
      loses recall. Measure query latency and recall at 5M, 10M and 20M rows and
      decide whether the cap moves.
- [ ] `INDEX_MIN_ROWS = 100_000` and "retrain when rows double" means the index
      is rebuilt at 100k, 200k, 400k... 12.8M. **Each rebuild is expensive and
      they land during indexing.** Check what a rebuild costs at 10M rows and
      whether the doubling rule should slow down at the top end.
- [ ] Confirm the FTS5 side: an external-content table over tens of millions of
      rows is fine, but the `optimize` command has never been run here.

## 7. What to do first

In order. Each step's result changes the next one.

1. **`scan` the corpus.** Nothing else is worth planning until the mix is known.
2. **Index one representative 20-30GB subtree**, timed, with OCR off. That gives
   a real MB/min for this hardware and this data, replacing the 84 MB/min guess.
3. **Repeat with OCR on** over a subtree known to contain scans. The ratio
   between the two is the whole argument for section 3.
4. **Extrapolate, then decide** whether 600GB is one run or a phased one.
5. Only then start the full index.

## 8. Recorded

- **The design scales; the schedule does not.** IVF_PQ, compaction, the file
  indexes and resumability are all present and correct. Time is the constraint.
- **The corpus is mostly static, so the big number is paid once.** That makes a
  week-long first pass acceptable and makes the *re-walk* the thing worth
  engineering - archival roots (2a) are worth more than any throughput tuning
  in this document.
- **Skip cheaply, never silently.** A root skipped because it is archival must
  say so, with its file count and the date of its last full pass.
- **OCR is the dominant cost** at 3.6s/page and must become a separate pass, or
  nothing is searchable until everything is.
- **Measure before planning.** Every number here rests on a 97-second sample and
  a file-type mix nobody has counted.
- **The memory ceiling is mostly baseline.** ~1GB of models under a 1.5GB
  ceiling leaves 500MB of headroom, which is why it oscillates.
- **A five-day run needs different reporting from a five-minute one** - windowed
  throughput, a daily summary, and an ETA honest enough to say it does not know.
