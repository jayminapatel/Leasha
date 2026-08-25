# Work order (Owner): the PST scale run

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3
**Thread:** Owner - nobody else can run this

Two separate jobs that have been travelling together and should not:

| | What it proves | Cost |
|---|---|---|
| **A. Outlook sign-off** | `Win32ComSession` actually drives real Outlook | Minutes |
| **B. Scale run** | The pipeline survives 200,000 messages, and how fast | Hours |

**A is the outstanding Layer 2 item** and blocks `VERSION` going to 0.4.0. **B is the only
thing in this project that has never been done**, and the fixtures cannot substitute for it.

Do A today. B needs a decision first - see §4.

---

## 1. Before anything: copy the archive

```powershell
Copy-Item "D:\Archives\2019.pst" "D:\Archives\worktest-2019.pst"
```

Open the copy in Outlook once to confirm it is intact, then work only from the copy.

The application is read-only against your data by design and `libpff` does not even open the
file for writing. This is insurance against your own mistake, not the app's, and against
Outlook - which *does* attach and lock a store it opens, and will happily "repair" one it
thinks is damaged.

## 2. Job A: the Outlook sign-off

Outlook open, then:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe -m app.cli extract --mailbox --limit 200
```

`extract` is **read-only: nothing is written and no store is opened**, so this cannot damage
an index or an archive. `--limit 200` keeps it to a couple of minutes.

**What you are checking** - four things, by eye:

- [ ] It finds your stores. Both the `.pst` archives attached in Outlook and the live mailbox
- [ ] Message counts are plausible for those folders
- [ ] Subjects and senders look like your actual mail, not mojibake or blanks
- [ ] Dates are right - not 1970, not today

Then without the limit, to see the whole walk:

```powershell
venv\Scripts\python.exe -m app.cli extract --mailbox --out mailbox-walk.json
```

**If all four hold, Layer 2 is signed off.** Tell the backend thread; it can move `VERSION` to
0.4.0 and strike the caveat from `HANDOFF.md` §3.

**If something is wrong**, run `app.cli diagnose` and send the zip with a note saying which of
the four failed. Do not try to fix it yourself - COM failures are unenlightening and the
backend thread has the fake-session tests to compare against.

## 3. Job B: what it is really measuring

Three unknowns, none of which a test can answer:

1. **Throughput.** Messages per minute on your machine. Every schedule estimate, the indexing
   view's ETA, and whether OCR-on-by-default is survivable all depend on this number, and
   nobody has it.
2. **Robustness.** Whether a 200,000-message run completes, and what it skips.
3. **Memory.** Whether the ceiling holds over hours rather than minutes.

## 4. The decision to make before running B

The review found a defect that this run will make worse: **LanceDB does a delete plus an add
for every document, and nothing ever compacts it** (`REVIEW-2026-08-25.md`, P4). 200,000
messages means 200,000 delete/add cycles into a store that fragments continuously.

**So do not run B into your real index.** Two options:

**Option 1 - run now, into a scratch index.** Recommended. You get the numbers today, and the
damaged index is thrown away afterwards. §5 does this.

**Option 2 - wait for the backend fix**, then run once into the real index.

Option 1 does not waste the work: recovery from a fragmented vector store is
`app.cli reembed`, which **rebuilds the vector store from SQLite without re-reading any
documents**. The expensive half - parsing PSTs, OCR, conversion - is never repeated.

## 5. Job B: the run

**Close the application first.** The single-instance lock means the window and a CLI index
cannot both hold the store; the second one refuses with `ERR_DB_LOCKED`, correctly.

```powershell
cd D:\SearchProject
Copy-Item .env .env.psttest
```

Edit `.env.psttest` and change **one line**:

```
DATA_PATH=D:\PstTestIndex
```

Leave `VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE` and `STATE_PATH` alone if they are
absent - they derive from `DATA_PATH`. If the installer wrote them explicitly, change them to
match, or the scratch run will write into your real index, which is the one mistake here that
actually costs something.

Then:

```powershell
venv\Scripts\python.exe -m app.cli --env .env.psttest init
venv\Scripts\python.exe -m app.cli --env .env.psttest stats     # confirm it says D:\PstTestIndex
```

**Check that second command's output before continuing.** It is the whole safety net.

```powershell
$start = Get-Date
venv\Scripts\python.exe -m app.cli --env .env.psttest index "D:\Archives\worktest-2019.pst" --full-speed
"Elapsed: $((Get-Date) - $start)"
```

`--full-speed` removes the CPU, battery and low-priority limits. Right for a machine nobody is
using, and it makes the throughput figure mean something. **It will make the machine feel
slow** - that is the flag working.

It is resumable: stop it with Ctrl+C and run the same command again to continue.

## 6. Record these

| Measurement | Value |
|---|---|
| PST size on disk | |
| Messages in the archive (from job A) | |
| Wall-clock time | |
| **Messages per minute** | |
| Peak memory (Task Manager, `python.exe`) | |
| `D:\PstTestIndex` size afterwards | |
| Ratio of index size to PST size | |
| Completed, or stopped early? | |

Then:

```powershell
venv\Scripts\python.exe -m app.cli --env .env.psttest stats
Get-Content D:\SearchProject\logs\errors\errors_*.jsonl |
  ForEach-Object { ($_ | ConvertFrom-Json).record.extra.error_code } |
  Group-Object | Sort-Object Count -Descending
```

**Read `seen` and `indexed` as different things.** A `.pst` is *one file* and thousands of
documents. `seen` counts files, `indexed` counts documents. Confusing the two once made a
healthy run look broken.

## 7. Normal, versus a bug worth reporting

**Normal, do not be alarmed:**

- Thousands of `SKIP_CONTINUE` errors. Corrupt items, locked files, cloud stubs. Judge the
  **distribution**, not the count - a long tail is healthy, one code dominating is a finding
- `Deleted Items`, junk and sync-conflict folders skipped. Deliberate: on a fifteen-year
  archive Deleted Items is often a third of the messages, all of them things you threw away
- Attachment counts lower than expected - identical attachments are deduplicated by content
  hash, so the deck mailed round the team eight times is stored once

**Report these:**

- The run stops without a summary
- Memory climbing past the ceiling rather than pausing
- One error code accounting for most skips
- Any bare Python traceback - that breaks the error contract regardless of what caused it
- Resuming does not resume, and starts over

## 8. Afterwards

```powershell
Remove-Item -Recurse -Force D:\PstTestIndex
Remove-Item D:\SearchProject\.env.psttest
```

Your real index was never touched.

**Send the numbers to the backend thread.** They decide whether P4 and the other pre-index
findings are urgent or theoretical, and they are the input to the chunk-size and model-precision
question in `HANDOFF.md` §7 - which is far cheaper to settle now than after 100GB.

## 9. Then, separately, the measurement that matters most

Once there is a real index of your own mail, `app.cli evaluate` measures whether plain sentences
find the right documents. The table in `HANDOFF.md` §3b - 75% at rank 1 after translation - is
**21 clean fixture documents with no near-duplicates and no drift**.

Twenty of your own sentences against your own archive is the number that says whether this
thing works. Nobody else can produce it.

## 10. Recorded

- **A and B are different jobs.** A is a correctness sign-off in minutes; B is a measurement in
  hours. Coupling them delayed both
- **Scratch index for B**, because of P4. `reembed` means the expensive extraction work is
  never repeated
- **Copy the archive first** - against your own mistake, and against Outlook's willingness to
  repair a store it opens
- **`--full-speed` makes the machine feel slow.** That is the flag doing its job
