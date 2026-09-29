# Work order (local Windows session): bring the laptop up to date and prove the 29 September merge

**Doc version:** 1.0 · **Updated:** 2026-09-29 · **Applies to:** app v0.3.3
**Created:** 2026-09-29 · **Layer:** none new - verification of L2/L3/L5 already merged
**Thread:** One Claude Code session running **on the owner's laptop** (Claude Desktop or `claude remote-control`), able to run PowerShell there
**Status:** RELEASED *(owner, 2026-09-29: "can you write a work order i will start a new session locally")*

## Why

On 2026-09-29 sixteen pull requests (#15-#29, #31) and the screenshot fix (#33) were merged into
`main` on GitHub (`a2fa6f5`). Every one was green on Windows CI. None has run on the owner's
laptop yet, and the cloud session that built them cannot reach it. The owner's working copy is
still on the old code:

- `D:\Local\GDrive\SearchProject` is at `864ea0a`. `git pull` downloaded `a2fa6f5` but was
  interrupted before applying it: git's automatic `gc` could not delete `.git/objects/01` because
  **Google Drive held it locked**, and it kept asking "Deletion of directory ... failed. Should I
  try again?".
- Two old Leasha windows were running: pid 33400 (`venv\Scripts\pythonw.exe -m app.main`) and pid
  27956 (`C:\Program Files\Python\3.12\pythonw.exe -m app.main` - the **system** Python, not the
  venv; something on the laptop starts Leasha that way).
- The owner saw the old UI (number fields with up/down arrows, a model box that can be typed into)
  and rightly concluded the update had not landed.

This order is: make the laptop run `a2fa6f5`, prove each merged change on the real machine and
real data, and write down what is true afterwards. **Verification, not new features.** A bug found
here is fixed in its own small PR; anything larger is written up and brought to the owner.

## Rules for this session

- Everything in `CLAUDE.md` applies - above all **verify, never guess**: run it, read it, paste it.
- **Never end, kill or restart a process that is not Leasha.** The laptop runs Claude's own tools
  (Odoo MCP servers under `uv\cache`) in Python too; only processes whose command line contains
  `app.main` or `app.cli` are Leasha's.
- **Ask before** deleting anything, before `git reset`, and before anything that touches the
  owner's index (`DATA_PATH`) other than a normal index run.
- **Google Drive:** the working copy lives inside Drive. Pause Drive syncing while running git, or
  expect locked-file errors. Keep `gc.auto 0` set in this copy (step 0.2).

## 0. Bring the working copy up to date

- [ ] **0.1** Record the starting state and paste it into the session log:
      `cd D:\Local\GDrive\SearchProject; git branch --show-current; git log --oneline -1; git status --short`.
      Expected: `main`, `864ea0a`, one untracked `_Knowledge/prompt_log/<date>.jsonl` (leave it alone).
- [ ] **0.2** `git config gc.auto 0` in this copy, so git never again tries to tidy files Drive has
      locked. (Tidying can be done by hand later with Drive paused: `git gc`.)
- [ ] **0.3** Close every running Leasha, and only Leasha:
      `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'app\.main|app\.cli' }`
      - list them first, then `Stop-Process -Id <each>`.
- [ ] **0.4** `git fetch origin`, then `git merge --ff-only origin/main`. If it says it cannot
      fast-forward, **stop and show the owner** - do not merge, rebase or reset.
- [ ] **0.5** Prove it: `git log --oneline -1` shows `a2fa6f5` (or later), and
      `Select-String app\ui\widgets\search_box.py -Pattern setEditable` shows `setEditable(False)`.
- [ ] **0.6** Find what started pid 27956 with the system Python (a shortcut, a Startup entry, a
      scheduled task - check `shell:startup`, `Get-ScheduledTask | ? { $_.Actions.Execute -match 'python' }`,
      and desktop/Start-menu shortcuts). Report it to the owner; change it only with their say-so. A
      launcher on the wrong interpreter runs without the venv's packages and may be what "does
      nothing" on some starts.

## 1. The environment

- [ ] **1.1** `venv\Scripts\python.exe -c "import pypff; print('libpff ok')"`. If missing:
      `venv\Scripts\pip install libpff-python` (fast PST path, order 0z lane C). No other package
      was added by the merge.
- [ ] **1.2** `venv\Scripts\python.exe doctor.py` prints **READY**. Anything else: paste it and fix
      the cause before continuing.
- [ ] **1.3** Note `DATA_PATH` and `LOG_PATH` from `.env` (`Select-String .env -Pattern 'DATA_PATH|LOG_PATH'`).
      The index database migrates itself to schema **v29** on first start - expect it once.

## 2. See each merged change in the window

Start with `.\leasha` from `D:\Local\GDrive\SearchProject`. Tick each only after seeing it.

- [ ] **2.1 Number fields (#26).** Settings › Indexing › Tuning, and Search: no up/down arrows on any
      number field; a small ↺ at the right edge; its tooltip reads "Back to the default (N)"; it is
      greyed when the field already holds its default; clicking it restores and saves the default.
- [ ] **2.2 Model choices (#26).** Rerank model, "Change the meaning model", photo model, speech
      model, Interpret and Chat models are drop-downs that cannot be typed into; a saved value not in
      the list shows as "(current, not in the list)"; the Download row shows installed/not installed.
      Do not press Download unless the owner wants that model.
- [ ] **2.3 Start in its own process (#19, #21).** Tuning › Strategy › "Index in a separate process"
      on; Start. Within a minute the Indexing page moves ("Finding files, to read the newest first…",
      files-seen climbing). This is the owner's original bug ("did nothing"); the cause was the
      child's stdin pipe freezing it on Windows (`app/cli/index.py` `_private_stdin`). If it does
      not move within 2 minutes, capture the log (§3) before touching anything.
- [ ] **2.4 Start while running (#21).** Press Start again during a run: a question offers "Stop the
      current run"/"Keep it running"; Keep shows "An index run is already in progress.".
- [ ] **2.5 Reading order (#25).** Newest files are read first; Settings › What's indexed › right-click
      a folder › "Index this folder first" puts it at the front ("Read first" column shows 1, 2…).
- [ ] **2.6 Status everywhere (#23).** Results have a one-word Status column; the Indexing page shows
      the status funnel; Mail search says "Showing 500 of N messages"; Files says "Showing 200 of N".
- [ ] **2.7 Time limits (#22).** Tuning › Coverage shows "Time limit per file" (120 s) and the stall
      limit (600 s); a reader row has "Force skip reader N"; a large real `.pst` is **not** cut off
      while its message count moves.
- [ ] **2.8 PST (#24).** A real `.pst` shows folder and message progress with per-message counts; a
      damaged message is skipped, the archive carries on.
- [ ] **2.9 Junk pictures (#31).** Coverage › "Leave out signature logos and icons in email" is on;
      the Indexing page shows "Pictures in mail not read" rising on mail with signatures.
- [ ] **2.10 Code tab (#17, #18).** No black console flash on a history search; Esc/Stop ends it;
      typing a name shows matching lines with line numbers; "Ignore this repository" then Undo.
- [ ] **2.11 Chat box (#27).** A three-line question makes the box grow line by line (was stuck at
      one line on Windows fonts).

## 3. One real index run, logs read while it runs

- [ ] **3.1** With the laptop on mains, run a real index (separate process on, read processes off).
- [ ] **3.2** Follow `LOG_PATH\runs\run-<date>-<time>-index.log` and `LOG_PATH\app_<date>.log` while
      it runs. Look for, and report with the lines: ERROR/Traceback; "sent nothing for Ns; ending it"
      (child silence); "An index run is already in progress (another process)" (lock); "did not let
      go ... left behind" (a hung reader); Timed out / Force skip; OCR time per picture.
- [ ] **3.3** At the end: the run summary line, the status counts, files/min, and any file that
      Failed or Timed out, with its reason. Compare with the owner's last known run if there is one.
- [ ] **3.4** Then once more with "Read files in separate processes" on (0x §5b, still off by
      default): same corpus, compare files/min and memory in Task Manager. The default changes only
      on the owner's word and in its own PR.

## 4. PST field test (the owner's own archives)

- [ ] **4.1** Close Outlook and any index run. `venv\Scripts\python.exe scripts\pst_field_test.py
      "<folder with the PSTs>" --outlook`. Read-only; one process per archive; a reader that stands
      still 300 s is ended and reported with the folder/message it stopped at.
- [ ] **4.2** Put `pst-field-test.txt`'s table in the session log. Every archive that is not
      `finished`, or has Failed items, is a finding: folder, message number, stderr tail.

## 5. Write down what is now true

- [ ] **5.1** `HANDOFF.md`: tick, with a dated note, each owner-check line this session proved
      (0x §5b read processes, 0z lanes A-E, number fields/models, the separate-process indexer), and
      add the Drive/`gc.auto` trap and the system-Python launcher finding.
- [ ] **5.2** `docs/ORDER_REGISTER.md`: the 0z row still reads **0 / 24** and 0y **7 / 8** although
      the lanes are merged - recount from the orders' ticked boxes and correct the rows with a dated
      note (never by rewording the old text).
- [ ] **5.3** CHANGELOG `[Unreleased]`: one entry for this verification and any fix it produced.
- [ ] **5.4** One PR for the documents, one PR per fix. The owner merges.

## Not in this order

- Florence-2 to Ollama - parked by the owner 2026-09-29 (Intel graphics; see
  `docs/REFERENCE-model-backends.md`).
- 0y §3 (streaming history) and §4 (mail preview) - still queued in order 0y.
- The flaky `test_chat_tab_qt::test_without_the_helper…` (2 in 16 runs on main; `ChatController.ask()`
  never checks `_available`) - a separate small fix when convenient.
- Moving the working copy out of Google Drive - worth raising with the owner, not done here.
