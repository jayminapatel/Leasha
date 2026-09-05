# Work order (One thread): the splash, and a life that starts fast and ends fast

**Doc version:** 1.1 · **Updated:** 2026-09-04 · **Applies to:** app v0.3.3
**Thread:** One thread (main.py startup path + shell.py close path + one new
splash module + installer prefetch)
**Status:** RELEASED by the owner 2026-08-28 — a done deal, design settled
(see §0). **Gap-schedulable** (privacy-defaults pattern); §1 is independent
of §2/§3 and may land first.

**The problem, measured on the owner's machine:** launching shows a Windows
busy cursor and *nothing else* while the app waits on the single-instance
handover (up to 12s on a quick relaunch), opens both stores and constructs
the whole MainWindow — `window.show()` is the sixth thing that happens.
Closing runs timed stages up to a 4s worker-drain, then an *untimed* tail
(store close with `PRAGMA optimize` per connection, LanceDB close,
interpreter teardown of onnxruntime/lance/PyQt6) — all while holding the
lock a relaunch then waits on. Both ends feel broken; neither is.

## 0. THE DESIGN — settled by the owner 2026-08-28, do not redesign

The owner chose this on a live mock; implement it faithfully.

1. **Ground**: brand navy `#15084B`, rounded-corner frameless splash,
   ~560×380 logical px, DPI-aware. A 5px **signature stripe** across the
   top in the logo's three colours: `#A1B000` · `#0778D9` · `#FF9933`
   (equal thirds).
2. **Logo**: the Leasha wordmark+mark, centred (`assets/leasha-logo.png`,
   transparent background, is in the repo). On navy the navy wordmark is
   invisible — derive a **white-wordmark variant** by recolouring the
   navy-family pixels (R<70, G<60, B<130 → white, alpha preserved) at
   build time or first run, cached; the coloured mark stays as it is.
3. **Tagline**, verbatim, white, under the logo:
   **"Forgets nothing. Tells no one. Outlives the drives."**
   (This is brand copy; the owner-approved privacy *paragraph* in the
   README/installer is untouched and remains the legal-honest statement.)
4. **The rotating cases — on ALL moments, owner's explicit call**: beneath
   the tagline, a case line with a small vector drawing to its left,
   cross-fading (~450ms) to the next every ~3s, in a fixed shuffle so
   consecutive launches lead with different cases. The five cases,
   verbatim:
   - (drive icon) "Finds photos on drives you unplugged years ago — it
     remembers what's on them"
   - (photo+magnifier) "Describe a picture from memory — 'the kids on the
     beach' — and it appears"
   - (stopwatch) "Twenty years of files, mail and photos — searched in a
     blink"
   - (home) "No cloud, no account, no subscription — yours, on your
     machine, free"
   - (envelope+check) "Old mail archives, chats and scans — one search
     box finds them all"
   Icons are simple line drawings in the three brand colours (the mock's
   shapes are the reference) — **painted vectors, no image assets**.
5. **Status line** (plain words, faint violet `#9b95c4`): the startup
   breadcrumbs already logged, translated — "Starting Leasha…" · "Opening
   your index…" · "Loading the search engine…" · "Ready". Relaunch
   handover shows "Waiting for the previous Leasha to finish closing…".
   First-run model download shows "Downloading the meaning model — one
   time, about 130 MB" with a thin progress bar (blue on `#2b2160`).
6. **Footer**: version left, `leasha.co.uk` right, small and faint.
7. **Timing**: splash visible **<300ms from process start** (before the
   lock wait — only stdlib+Qt imports may precede it); minimum hold
   ~1.2s so a fast start always shows exactly one case; then a quick fade
   as the main window appears. No click-through dismissal needed; Esc
   never kills the app.
8. **Copy rule (house doctrine, tested)**: no performance numbers in any
   splash string — "in a blink", never milliseconds (owner's call). The
   130MB download figure is a fact and stays. Splash strings join the
   deny-list test the adoptions order built for invented numbers.

## 1. The splash

> **2026-09-04 verification pass (Order 0r, remaining items).** Went through
> every item below against the live code and, for 1a/2c, against a real
> running instance (`venv\Scripts\pythonw.exe -m app.main`), not just the
> source. Findings and fixes are noted per item; nothing here was ticked
> without personally confirming it.

- [x] **1a** `app/ui/splash.py`: frameless top-level widget (or
  `QSplashScreen` subclass) painting §0 entirely in code from theme-free
  constants (the splash is brand, not theme — identical in light and
  dark). Shown from `main.py` immediately after `QApplication` exists and
  the icon is installed; every later startup stage reports to it via a
  simple callable (the same strings the log breadcrumbs use, translated
  to plain words in one table).
  > **2026-09-04:** confirmed the frameless/theme-free painting in
  > `app/ui/splash.py` (`WA_TranslucentBackground` + `FramelessWindowHint`,
  > colours are hex constants, no theme lookup) and the show order in
  > `app/main.py` (`QApplication` → `install_window_icon` →
  > `_make_ctrl_c_work` → `SplashScreen().show()`). **Found and fixed a real
  > gap**: `STATUS_MESSAGES["stores_opening"]` ("Opening your index…") was
  > defined but never reported — `main.py` jumped straight from the handover
  > line to "Loading the search engine…", so that stage's own status text
  > never appeared. Added the missing `status_reporter("Opening your
  > index…")` call in `app/main.py`. Ticked after re-running
  > `tests/unit/test_splash.py` and `tests/unit/test_exit_placement.py`
  > (17/17 green) and confirming the new call appears in a live run's status
  > sequence.
- [ ] **1b** rotation/fade timers live on the splash and run through every
  moment (normal, first-run download, handover wait) per §0.4; the
  minimum-hold logic delays the handover to the main window, never the
  work itself (startup continues underneath; only the fade waits).
  > **2026-09-04: NOT ticked — two real gaps found, left as documentation
  > rather than a redesign.** (1) `_minimum_hold_time` is set in
  > `SplashScreen.__init__`/`show()` but never read anywhere else in the
  > class — there is no logic that actually delays anything on it.
  > `hide_and_close()` closes immediately and unconditionally; its own
  > docstring explains this was a deliberate choice (closing synchronously
  > so `test_splash_shows` doesn't have to race a timer) reasoned through by
  > the session that wrote it, on the argument that real startup always
  > takes far longer than 1.2s — true in every run measured for 2a below,
  > but not enforced by code, so "the minimum-hold logic delays the
  > handover" is not a true statement of what's there. (2) There is no fade
  > at all — `hide_and_close()` is a bare `self.widget.close()`, and its own
  > comment says "A fade animation can be added later." Building the fade or
  > wiring real minimum-hold enforcement is a UI feature this session's
  > scope didn't include (not one of the six items assigned to implement),
  > so it's left here rather than guessed at. §0's "do not redesign" also
  > argues for leaving the previous session's considered synchronous-close
  > decision alone rather than overriding it unilaterally.
- [ ] **1c** first-run: when the model cache is missing, warm-up's
  download progress (fastembed reports bytes) streams to the splash bar;
  if the window is already up when a download starts (cache emptied
  mid-life), the existing notices bar carries the message instead — H4
  register, no second splash.
  > **2026-09-04: NOT ticked — not built.** `app/index/embedder.py` has no
  > progress-reporting hook at all (grepped for `on_progress`/`progress`/
  > `callback` in that file: no matches), and nothing in `app/main.py` ever
  > calls `splash.report_progress(message, progress=...)` with a real
  > numeric value — only plain status strings. `app/index/embedder.py` was
  > outside this session's file-touch scope, so it's flagged for follow-up
  > (`task_80d1a1e8`) rather than fixed here.
- [x] **1d** the white-wordmark derivation of §0.2, cached beside the
  asset; a test asserts the derived image differs from the source only in
  the navy-family pixels.
  > **2026-09-04:** verified against the real asset, not just the unit
  > test's synthetic image. `assets/leasha-logo-white.png` already exists
  > (produced by an earlier live run) — loaded both PNGs with `QImage` and
  > diffed all 4,296,537 pixels pixel-by-pixel: 132,463 differ, and every one
  > of them satisfies the navy-family test (R<70, G<60, B<130); zero
  > non-navy pixels changed. `tests/unit/test_splash.py::
  > TestWhiteWordmarkDerivation` passes.

## 2. Startup, faster underneath the splash

- [x] **2a** measure first (the §0 problem statement is diagnosis, not
  numbers): log process-start → splash-visible → window-visible →
  warm-up-complete on the owner's machine; record the numbers in this
  file. The splash hides the wait; this section shrinks it.
  > **2026-09-04: measured live**, three real launches via
  > `venv\Scripts\pythonw.exe -m app.main`, cache warm (models already
  > downloaded), reading `startup_timer.summary()` — now actually logged
  > (see below) — from each run's `logs\runs\run-*-window.log`:
  >
  > | run | splash visible | window visible | ready (warm-up complete) |
  > |---|---|---|---|
  > | 1 | 304ms | 6,590ms | 11,879ms |
  > | 2 | 198ms | 4,956ms | 8,129ms |
  > | 3 | 152ms | 2,820ms | 6,456ms |
  >
  > Splash-visible is comfortably inside the <300ms budget in 2 of 3 runs and
  > right at it in the third (304ms) — consistent with the "only stdlib+Qt
  > imports precede it" discipline already in place. **Window-visible is the
  > real cost** (2.8–6.6s), and reading the same runs' stage breadcrumbs
  > shows essentially none of that gap is model loading: "stores open" to
  > "constructing the window" (embedder + reranker + `SearchEngine`
  > construction) took under 15ms in every run. The entire multi-second gap
  > is inside `MainWindow.__init__` — see 2b.
  >
  > **Instrumentation added to make these numbers loggable at all**:
  > `StartupTimer.summary()` was already computed but never logged anywhere
  > (`startup_timer` object existed, its methods were called, nothing ever
  > read `.summary()`). Added a `log.info("startup: timings - splash {}ms,
  > window {}ms, ready {}ms", ...)` call in `app/main.py`. Also discovered
  > along the way: `log.info(msg, **kwargs)` calls elsewhere in `main.py`
  > (e.g. the pre-existing `model=..., cache=...` on the "stores open" line)
  > have *always* rendered invisibly — the file/console log format strings
  > only interpolate `{message}` plus `component`/`error_code` from `extra`,
  > so arbitrary kwargs are silently swallowed. The new timing/close logging
  > interpolates values directly into the message string instead, which is
  > the only way that actually reaches the log file.
- [ ] **2b** defer what the first paint does not need: audit
  `MainWindow.__init__` for work movable to after `show()` (the M11
  pattern — construct light, populate async). Target: window visible
  <1.5s warm on the owner's machine, recorded, not promised.
  > **2026-09-04: audited, NOT ticked — target not met, and the fix is out
  > of this session's file scope.** Per the 2a table, window-visible is
  > 2.8–6.6s against a <1.5s target — not met. Reading `app/ui/shell.py`'s
  > `MainWindow.__init__` (read-only; this session must not edit
  > `app/ui/shell.py` — concurrent Orders 0q/0s own it) shows the cost is
  > real work, not padding: constructing `SearchView`/`IndexingView`/
  > `SettingsView`/files/mail/code views, several `self._read_state(...)`
  > round-trips against the SQLite store, `OllamaClient` setup, and signal
  > wiring, all synchronously in the constructor before `window.show()` is
  > ever reached in `main.py`. `MainWindow`'s constructor also requires a
  > fully-built `SearchEngine` (embedder+reranker already loaded) as a
  > parameter, so deferring model loads past `show()` would need a
  > different construction/attachment protocol in `shell.py`, not just
  > reordering `main.py`. Flagged for follow-up as `task_c87cefe4` with the
  > measured numbers above; not attempted here because it requires editing
  > a file this session was told not to touch.
- [x] **2c** the handover wait paints honestly: during
  `acquire(wait_s=HANDOVER_WAIT_S)` the splash shows the §0.5 handover
  line — the 12s worst case becomes an explained wait instead of a dead
  cursor.
  > **2026-09-04: a real bug found and fixed, verified live.**
  > `SingleInstance.acquire(wait_s=...)` blocks the calling thread in its own
  > `time.sleep`-based retry loop, and at that point in startup
  > `application.exec()` has not been called yet — there is no Qt event loop
  > running to pump. The splash's `_status_message` was being set to the
  > correct handover text before the call, but with nothing pumping events
  > during a call that can block for up to 12s, the widget never actually
  > repainted to show it and its case-rotation timer never fired — the
  > splash would sit frozen on whatever was painted last for the whole wait,
  > which is the exact "busy cursor and nothing else" symptom from this
  > work order's own problem statement, still present underneath a splash
  > built to explain it. Fixed by adding `_acquire_gui_lock_responsively()`
  > in `app/main.py`: polls `gui_lock.acquire(wait_s=0.0)` (a single
  > non-blocking attempt) with `application.processEvents()` between
  > attempts, preserving `SingleInstance`'s own error contract (same
  > `AppErrorException` on timeout, same deadline) without touching
  > `app/core/single_instance.py`. `application.processEvents()` calls were
  > also added after every other `status_reporter(...)` call in the startup
  > path (handover, "Opening your index…", "Loading the search engine…") for
  > the same reason — previously only two `processEvents()` calls existed in
  > the whole startup (`after splash.show()` and `after window.show()`), so
  > every intermediate status update and the case-rotation timer were dead
  > for the entire time in between. Verified by running the real app and
  > confirming the status sequence reaches the log in order.
- [x] **2d** installer prefetch: `install.ps1` optionally downloads both
  models (embedder + reranker) into `MODEL_CACHE` at install time with
  visible progress, so first launch never downloads. On by default,
  skippable (offline installs must still work — the app's own first-run
  path remains the fallback, which is why 1c exists).
  > **2026-09-04: found already implemented, pre-dating this order** —
  > contrary to this session's brief describing it as "NOT yet built at
  > all". `install.ps1` lines ~551–582 already download both models at
  > install time: the embedding model (`bge-small-en-v1.5`, ~130MB) as a
  > required-but-recoverable step (a failure — e.g. offline — falls through
  > to the existing Retry/Continue/Abort handling rather than hard-blocking
  > the install), and the rerank model (`bge-reranker-base`, ~1.1GB) marked
  > `-Optional`, skippable via `-SkipOptional`. Both write into
  > `$DataPath\models`, matching `MODEL_CACHE`. Progress is visible: fastembed's
  > tqdm/huggingface progress goes to stderr, and `Invoke-PythonSnippet`
  > streams the child process's stdout+stderr line by line to the console.
  > No code change made — re-implementing a second mechanism for something
  > already working would only risk regressions. Verified by reading the
  > script; not re-run end to end here (a real install would re-download
  > ~1.2GB of models this session doesn't need).

## 3. Close, faster and honest to the end

- [x] **3a** time the tail: the stage log currently ends at engine close;
  add breadcrumbs+timings for store close, vector close, and final exit
  so the next slow stage names itself. Record before/after numbers here.
  > **2026-09-04: implemented in `app/main.py`, measured live.** After
  > `application.exec()` returns, `store.close()`, `vectors.close()` and
  > `gui_lock.release()` are now each called explicitly and timed (all three
  > are documented safe to call more than once, so the `with` statement's
  > own automatic exit immediately afterwards is a harmless no-op — nothing
  > about what closes or when has changed, only that each stage now names
  > itself). `CloseTimer` (already written in `startup_timing.py`, never
  > instantiated anywhere until now) records `stores_closed` and
  > `lock_released`. Measured on a real close (`run-20260904-232409-window.log`):
  > event loop returned → sqlite store closed **2ms** → vector store closed
  > **2ms** → lock released **0ms**, "stores and lock released, exiting -
  > stores 5ms, lock 6ms since event loop returned". A second run measured
  > store 1ms / vector 1ms / lock 0ms, "stores 5ms, lock 5ms". No pre-3a
  > "before" number exists for the store/vector/lock portion specifically —
  > there was nothing timing it at all before this. What can be said with
  > the git history: this tail used to include a `PRAGMA optimize` call per
  > connection (removed in commit 5559e74, see 3c below), which SQLite's own
  > documentation says can be non-trivial on a large index — this session's
  > single-digit-millisecond measurement is only representative of a store
  > that never ran it.
- [x] **3b** perceived-instant close: on a real quit the window **hides
  first**, then the existing staged teardown runs invisibly. The
  disable-input discipline of `_drain_workers` stays; the tray icon (when
  installed) is the only visible remnant and disappears last.
  > **2026-09-04: NOT ticked — a real crash found live, breaks this on
  > every close.** Ran the real app and closed it (via `pywinauto`, both
  > `Window.close()` and Alt+F4) three times. Every time, `MainWindow.
  > closeEvent` (in `app/ui/shell.py`, out of this session's scope to edit)
  > raised `AttributeError: 'MainWindow' object has no attribute
  > 'set_states'` at the window-geometry save (`self.set_states(...)` where
  > every other call site in the same file correctly calls
  > `self._store.set_states(...)`) — logged in the run log as "unhandled
  > exception - the process may stop here", immediately followed by the
  > event loop returning. That ordering means `self.hide()` (the §3b
  > behaviour this item is about) and the entire staged teardown after it
  > (`view.shutdown` x4, schedule/tuning flush, indexing stop, scheduler
  > stop, `_drain_workers`, `recorder.close`, `engine.close`) **never run**
  > on a real close — the process still exits (main.py's own explicit
  > store/vector/lock close in 3a covers for it), but not via the graceful
  > path this item describes, and §4a's window-geometry persistence is
  > silently broken every time. Flagged as `task_6c99824d` (one-line fix:
  > `self.set_states` → `self._store.set_states`, plus a regression test) —
  > not fixed here because `app/ui/shell.py` is outside this session's
  > file-touch scope. **This also means the "close: hide-first verified"
  > test in §4 cannot be written and pass honestly until that bug is fixed.**
  >
  > **Ticked 2026-09-05, a later session.** The `self.set_states` bug
  > (`task_6c99824d`) was fixed in an earlier session today - verified live,
  > not assumed: `grep -n "self\.set_states\|self\._store\.set_states"
  > app/ui/shell.py` shows every call site correctly uses
  > `self._store.set_states(...)` now, and `closeEvent`'s own docstring
  > (line ~2214) documents the hide-first ordering the code carries out at
  > line ~2244, before the `stage()` loop begins. The §4 test this note
  > said could not honestly be written until the bug was fixed is now
  > written: `tests/unit/test_window_opens.py::
  > test_close_hides_before_the_staged_teardown_begins` hooks the *first*
  > teardown stage and asserts the window is already invisible at that
  > exact moment - proving the ordering, not just that it ends up hidden
  > eventually (which `test_close_event_persists_geometry_without_raising`,
  > already passing, would not have caught on its own).
- [x] **3c** `PRAGMA optimize` moves off the exit path: run it on idle
  (the enrichment-backlog/idle pattern, or a coarse every-N-hours timer)
  instead of once per connection at close. SQLite's own guidance is
  periodic, not at-exit; close then pays nothing for it.
  > **2026-09-04: NOT ticked — half done.** Confirmed via `git show 5559e74
  > -- app/storage/sqlite_store.py` that the `conn.execute("PRAGMA
  > optimize")` call was removed from `close()` and replaced with a comment
  > claiming it "moved to idle" — but grepping the whole `app/` tree for
  > "PRAGMA optimize" (case-insensitive) finds only that comment and one
  > unrelated docstring in `optimize_fts()` (a different thing — FTS5
  > segment merging, not the query-planner statistics `PRAGMA optimize`
  > touches). **Nothing anywhere actually calls `PRAGMA optimize` any
  > more** — it was deleted, not relocated. Close is genuinely fast now (see
  > 3a), but the database never gets this maintenance at all, which is the
  > opposite failure mode from the one this item names. `app/storage/
  > sqlite_store.py` is outside this session's file-touch scope, so this is
  > flagged as `task_c21f9a55` rather than fixed here.
  >
  > **Ticked 2026-09-05, a later session.** `task_c21f9a55` is done -
  > verified live: `SqliteStore.optimize_query_planner()`
  > (`app/storage/sqlite_store.py`) is a public wrapper round `PRAGMA
  > optimize`, called from `MainWindow._run_idle_optimize`
  > (`app/ui/shell.py`) off the UI thread via `CallableWorker`, on an
  > hourly `QTimer` (`self._optimize_timer`, interval 3,600,000ms) started
  > in `__init__`. `grep -n "PRAGMA optimize\|optimize_query_planner"
  > app/storage/sqlite_store.py app/ui/shell.py` shows both ends of the
  > wiring; `close()` no longer calls it, matching this item's own
  > requirement that close pay nothing for it.
- [x] **3d** after the stores and lock are cleanly released, skip
  interpreter teardown of the heavyweight native modules with
  `os._exit(code)` — placed so it is provably after `SqliteStore.__exit__`
  and `VectorStore.__exit__` and the log flush, never before. This is the
  standard remedy for slow onnx/arrow unload; the placement constraint is
  the whole safety argument, so it gets a test and a loud comment.
  > **2026-09-04:** confirmed in `app/main.py`: `_exit_fast(code)` is called
  > strictly after the `with gui_lock, SqliteStore(...) as store,
  > VectorStore(...) as vectors:` block exits (which runs both `__exit__`s),
  > and now also after this session's own explicit store/vector/lock close
  > calls (3a) — the log line "shutdown: stores and lock released, exiting"
  > is the last thing logged before `_exit_fast`. `tests/unit/
  > test_exit_placement.py` (3 tests) passes, including the AST-based check
  > that `_run_window` calls `_exit_fast`. Verified live: every real close in
  > this session's testing exited the process cleanly (confirmed via
  > `tasklist` showing no `pythonw.exe` left) within milliseconds of the
  > "stores and lock released" log line.
- [x] **3e** the handover benefits measured: relaunch-after-close wait
  time on the owner's machine before/after, recorded here — this is the
  number the 12s `HANDOVER_WAIT_S` exists to absorb, and it should
  shrink.
  > **2026-09-04: measured live**, with a caveat. Closed a running instance
  > and immediately (`(venv\Scripts\pythonw.exe -m app.main &)` fired right
  > after the close call returned) launched a second one, three times.
  > Because the close tail is now ~5–13ms (3a) — faster than the ~150–300ms
  > it costs an external script just to spawn `pythonw.exe` and reach the
  > `SingleInstance.acquire` call — **every relaunch attempt found the lock
  > already free and acquired on its first try; none hit real contention.**
  > That absence of contention *is* the measured result: with the close tail
  > this short, `HANDOVER_WAIT_S`'s 12s ceiling is essentially never spent
  > in practice on this machine any more. **No "before" number exists on
  > this exact codebase** — the `PRAGMA optimize`-at-close code (3c) was
  > already removed before this session started, so there was no way to
  > check out that state and measure it without disturbing other agents'
  > concurrent work in this shared tree; the git history (5559e74) is the
  > only record that it used to run per-connection at close, and SQLite's
  > own documentation is the basis for expecting that to have been
  > non-trivial on a large index, not a number this session measured
  > itself. One test attempt did hit the full 12s wait and then a genuine
  > `ERR_DB_LOCKED` failure — but that was a limitation of the test method,
  > not a product bug: an Alt+F4 sent via `pywinauto.type_keys` to a
  > background window in this sandboxed environment did not reliably
  > deliver a real `WM_CLOSE` (the window stayed fully visible afterward,
  > with no `closeEvent` log line at all), so the "closing" instance was
  > never actually asked to close and correctly never released the lock.
  > Switching to `pywinauto`'s `Window.close()` (the UIA Close action rather
  > than a keystroke) delivered the close reliably every time afterward.
  > That confirms `HANDOVER_WAIT_S`'s wait-then-fail behaviour is itself
  > working honestly for a genuinely stuck instance; it says nothing about
  > 3c or 3d.

## 4. Tests

- [x] splash strings pass the plain-words rules and the invented-number
  deny-list (§0.8); tagline and case lines byte-exact against §0 (brand
  copy is load-bearing — a test holds it, the keep-descriptions rule
  applies to it from release).
  > **2026-09-04:** `tests/unit/test_splash.py::test_status_messages_plain_words`,
  > `::test_tagline_byte_exact` and `::test_case_lines_byte_exact` all pass.
- [ ] pytest-qt: splash constructs offscreen, cycles all five cases with
  fades, shows each moment's status line, respects minimum hold; the
  white-wordmark derivation test (1d).
  > **2026-09-04: partially true, not ticked.** Construction-offscreen,
  > case-rotation-through-all-five, status-line and white-wordmark-derivation
  > tests all exist and pass. "With fades" and "respects minimum hold" do
  > not, honestly: there is no fade to test (see 1b), and
  > `test_minimum_hold_timing` doesn't actually exercise enforcement — it
  > sets `_minimum_hold_time` and asserts elapsed time is small, without ever
  > calling `hide_and_close()` at the boundary, so it would pass even if the
  > field did nothing at all (which, per 1b, it currently does). Left
  > un-ticked rather than let a passing-but-vacuous test claim more than 1b
  > actually verified.
- [ ] startup: a stub-slowed stage still shows the splash within its
  budget (the <300ms import discipline as a test on what `main` imports
  before splash-show).
  > **2026-09-04: NOT ticked — not written.** No such test exists anywhere
  > in the suite. Not attempted here: it wasn't one of this session's six
  > assigned implementation items, and the import-discipline it would check
  > (only stdlib+Qt precede splash-show in `main.py`) held up in every live
  > run measured for 2a (splash visible in 152–304ms), so there was no
  > regression to chase — but the test itself is still missing.
- [x] close: hide-first verified (window invisible before drain begins);
  `os._exit` placement — a test proves the exit call is unreachable while
  a store is open; idle-optimize runs and close no longer calls it.
  > **2026-09-04: partially true, not ticked.** `os._exit` placement:
  > `tests/unit/test_exit_placement.py` (3 tests) passes. "Hide-first
  > verified" and "idle-optimize runs and close no longer calls it" are
  > both unwritten and, per 3b and 3c above, would currently fail honestly
  > if written — hide-first is defeated by the `set_states` crash, and
  > idle-optimize doesn't exist anywhere to test. Both files that would need
  > editing to fix the underlying behaviour (`app/ui/shell.py`,
  > `app/storage/sqlite_store.py`) are outside this session's scope; see
  > `task_6c99824d` and `task_c21f9a55`.
  >
  > **Ticked 2026-09-05, a later session.** All three now written and
  > green: `test_close_hides_before_the_staged_teardown_begins` (hide-first,
  > as an ordering - the window is already invisible when the *first*
  > teardown stage runs, not merely by the time `close()` returns),
  > `test_idle_optimize_timer_actually_calls_the_store` (the hourly timer's
  > handler really invokes `store.optimize_query_planner`, not just that
  > the method works in isolation), both in `tests/unit/test_window_opens.py`;
  > `test_close_no_longer_runs_pragma_optimize_itself`
  > (`tests/unit/test_idle_optimize.py`) already covered the "close no
  > longer calls it" half. `os._exit` placement was already green.
- [x] measurements recorded in this file: 2a, 2b target, 3a, 3e.
  > **2026-09-04:** real numbers recorded for all four under their own
  > items above — 2a's three-run table, 2b's target-vs-measured gap (2.8–6.6s
  > against <1.5s, not met, root cause identified), 3a's close-tail timings,
  > and 3e's handover-wait finding (effectively zero real-world contention
  > left to measure, plus the honest-failure case for a genuinely stuck
  > instance).

> **2026-09-04 status: not done.** 9 of 18 checkboxes ticked (1a, 1d, 2a, 2c,
> 2d, 3a, 3d, 3e, and the plain-words/measurements test line). Genuinely not
> built and outside this session's file-touch scope: 1c (fastembed progress
> → splash, `task_80d1a1e8`), 2b's actual deferral (blocked on
> `app/ui/shell.py`, `task_c87cefe4`), 3c (idle `PRAGMA optimize` never
> actually relocated, `task_c21f9a55`). Found broken by live testing and
> flagged rather than fixed (also out of scope): 3b — a crash in
> `MainWindow.closeEvent` (`self.set_states` should be
> `self._store.set_states`) defeats hide-first and the whole staged
> teardown on every real close (`task_6c99824d`). 1b is a smaller,
> in-scope-but-not-attempted gap (no fade exists; minimum-hold is set but
> never enforced) — left alone deliberately rather than redesigned
> unilaterally, per §0's "do not redesign". The four §4 test lines that
> depend on 1b/1c/3b/3c are correspondingly unwritten rather than written
> to pass vacuously.

## Done means

Change + tests + suite green + committed by name; the measured numbers
written into §2/§3; CHANGELOG. Acceptance sentence: launch Leasha and
within a third of a second the brand is on screen telling you one true
thing it can do; the window follows fast, and warming up never blocks it;
close it and it is gone from the screen at once, gone from the machine in
moments — and launching again straight away just works, with the splash
explaining any wait instead of a busy cursor explaining nothing.
