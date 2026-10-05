# Work order (One thread): the splash, and a life that starts fast and ends fast

**Doc version:** 1.6 · **Updated:** 2026-10-05 · **Applies to:** app v0.3.3
**Thread:** One thread (main.py startup path + shell.py close path + one new
splash module + installer prefetch)
**Status:** RELEASED by the owner 2026-08-28 — a done deal, design settled
(see §0). **Gap-schedulable** (privacy-defaults pattern); §1 is independent
of §2/§3 and may land first.

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

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
- [x] **1b** rotation/fade timers live on the splash and run through every
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
  >
  > **Ticked 2026-09-05, a later session (Order 0r, items 1b/1c).** Both
  > gaps closed in `app/ui/splash.py`. `hide_and_close()` now calls
  > `_wait_out_minimum_hold()` (pumps `QCoreApplication.processEvents()`
  > between short sleeps until `_minimum_hold_time`, capped defensively at
  > `_MAX_HOLD_WAIT_S` against a clock anomaly) before `_fade_out()` (ramps
  > `windowOpacity` 1.0→0.0 over `_FADE_DURATION_S`, pumped the same way).
  > Pumping rather than blocking means the case-rotation `QTimer` and any
  > repaint keep running for the whole wait, the same technique
  > `_acquire_gui_lock_responsively` in `app/main.py` already uses for §2c.
  > This delays only the hand-off, not the work: `main.py`'s only call site
  > reaches `hide_and_close()` after warm-up has finished and `window.show()`
  > has already run, so the window is already showing underneath the splash
  > before either wait begins. Both waits are wrapped in their own
  > `try/except` so a failure in either still reaches `self.widget.close()`
  > — H4: splash behaviour must never be the reason startup doesn't finish.
  > `test_minimum_hold_timing` (previously vacuous — it set the field and
  > asserted elapsed time was small, without ever calling `hide_and_close()`
  > at the boundary, per the 2026-09-04 note in §4) now actually calls
  > `hide_and_close()` and asserts real elapsed time against the remaining
  > hold; two new tests (`test_hide_and_close_fades_opacity_to_zero`,
  > `test_hide_and_close_still_closes_if_fade_raises`) cover the fade and
  > the H4 guard respectively. `test_splash_shows` now satisfies the hold up
  > front (`splash._minimum_hold_time = time.time()`) so it stays a fast
  > visibility check rather than racing the real wait. All 16 tests in
  > `tests/unit/test_splash.py` pass (`2.72s` total — the two timing tests
  > that deliberately wait cost `0.65s`/`0.32s` of that).
- [x] **1c** first-run: when the model cache is missing, warm-up's
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
  >
  > **2026-09-05, a later session (Order 0r, items 1b/1c). Still NOT
  > ticked — first clause done, second clause genuinely missing and out of
  > this session's file scope.** Checked live against this branch (off
  > `main`), not assumed: `git log --oneline -- app/index/embedder.py`
  > shows `f9a5982` ("Order 0r item 1c: wire fastembed's download progress
  > through to the splash") already merged. Reading it confirms the first
  > clause — `Embedder` gained an `on_progress` parameter, threaded through
  > `from_settings` via its existing `**overrides`; `_DownloadProgressWatcher`
  > polls the model cache directory's growth on a daemon thread (fastembed's
  > own `TextEmbedding.__init__` drops any progress kwarg before it would
  > reach `huggingface_hub`'s `tqdm_class`, confirmed by reading fastembed's
  > source, so this measures from the outside instead); `app/main.py`
  > passes `on_progress=lambda pct: status_reporter(get_splash_status_text
  > ("model_download"), pct)` into `Embedder.from_settings(...)` at the
  > warm-up call site. Every `on_progress` call is guarded
  > (`except Exception: pass`), matching H4. This is the splash-bar half of
  > 1c, and it is done.
  >
  > **The second clause is not.** Grepped `app/search/vector.py`'s
  > `clip_text_embedder_from_settings` (the CLIP text-tower embedder every
  > `SearchEngine` image-search call site builds): it constructs a plain
  > `Embedder` with no `on_progress` at all. Per `app/search/engine.py`'s own
  > comments (work order 0h §1c, ~line 465), this embedder is deliberately
  > lazy — it does not load or download until the first image search reaches
  > `vector.search_images`, which runs long after the splash has closed and
  > the window is showing. If the model cache is emptied mid-life (moved
  > index, re-staged data directory) and an image search is the thing that
  > next tries to load it, fastembed redownloads with zero progress feedback
  > anywhere: no splash (correctly — there should be no second splash), but
  > nothing routes to the window's status bar either (`self.statusBar()` in
  > `app/ui/shell.py` — the "notices bar" this item's text means; confirmed
  > by grep, ~20 existing `self.statusBar().showMessage(...)` call sites,
  > none reachable from a progress callback). Closing this needs
  > `app/search/vector.py` (thread `on_progress` through
  > `clip_text_embedder_from_settings` and `search_images`),
  > `app/search/engine.py` (a seam for `SearchEngine` to reach a status
  > callback), and `app/ui/shell.py` (wire it to `statusBar().showMessage`)
  > — three files this session's scope names as out of bounds
  > (`app/ui/shell.py` explicitly; `app/index/embedder.py`'s core logic only
  > for a small, obviously-safe addition, and this is neither small nor in
  > that file). Flagged as `task_f2f225f6` rather than guessed at here.
  >
  > **Ticked 2026-09-07, a later session (Order 0r, item 1c's second
  > clause).** `task_f2f225f6` is done - built exactly the three files the
  > 2026-09-05 note named, no others.
  >
  > `app/search/vector.py`: `clip_text_embedder_from_settings` takes an
  > `on_progress` and passes it straight into `Embedder(...)` - for symmetry
  > and for a caller that already has somewhere to report to at construction
  > time. `search_images` takes its own `on_progress` too, and this is the
  > one that actually closes the gap: `clip_text_embedder` is built once in
  > `app/main.py`, before the window - and therefore before there is
  > anywhere to report to - exists, so its `on_progress` is `None` at
  > construction. `search_images` sets `text_embedder._on_progress` right
  > before the call that might need it, once a caller (`SearchEngine`) has a
  > live target. Guarded like every other H4 hook in this codebase - an
  > embedder that refuses the attribute (confirmed with a `__slots__` object
  > in the test) still searches.
  >
  > `app/search/engine.py`: `SearchEngine` gained `status_callback` (`None`
  > by default - H4, off means exactly what it meant before) and
  > `_clip_download_progress(percent)`, which turns a raw 0-100 into "
  > Downloading the picture-search model - N%" and calls `status_callback`,
  > itself guarded so a broken callback cannot take an image search down.
  > `_retrieve` hands `_clip_download_progress` to `search_images` as
  > `on_progress` only when `status_callback is not None` - the same
  > "only when someone is listening" gate
  > `Embedder._start_progress_watcher_if_downloading` already uses for its
  > own watcher thread, so a search with nobody wired up starts no extra
  > thread for this at all.
  >
  > `app/ui/shell.py`: `MainWindow` connects a new `pyqtSignal`
  > (`_clip_download_progress`) to `self.statusBar().showMessage(message,
  > 8_000)` and hands the signal's `emit` to `engine.status_callback`, right
  > after `self._image_vectors = image_vectors` in `__init__`. **Not a
  > direct `self.statusBar().showMessage(...)` call from
  > `status_callback`** - the download-progress watcher that would call this
  > runs on a plain `threading.Thread`
  > (`app/index/embedder.py::_DownloadProgressWatcher`), and `search_images`
  > itself runs on `SearchEngine`'s own retrieval thread pool, so either
  > path reaches this callback from a thread that is never the GUI one. A
  > `pyqtSignal` is what this codebase already uses to marshal that safely
  > (`app/ui/workers.py`'s `WorkerSignals`, the same mechanism
  > `IndexWorker`'s `progress` signal relies on) - emitting from a
  > background thread onto a receiver living on the GUI thread queues
  > automatically, where a direct cross-thread widget call would not be
  > safe. The wiring itself is wrapped in `try/except` too, so a future
  > engine that cannot take the attribute still opens a window.
  >
  > **Tests, not assumed.** `tests/unit/test_search_images.py`: `on_progress`
  > is set onto the embedder before the call and left untouched when nobody
  > asked (H4 default), a `__slots__` embedder that refuses the attribute
  > still searches, and - the scenario this item exists for -
  > `test_search_images_reports_a_mid_life_cache_empty_download` builds a
  > real `Embedder` the way `clip_text_embedder_from_settings` builds it in
  > production (no `on_progress` at construction) against a fake
  > `fastembed.TextEmbedding` that grows a cache directory over three real
  > pauses, reusing `test_embedder.py`'s existing slow-download technique,
  > and asserts real progress reaches `search_images`'s own `on_progress`.
  > `tests/unit/test_engine_image_lane.py`: a fake CLIP embedder that
  > reports progress mid-`embed()` (mirroring the real watcher reporting
  > mid-constructor-call) proves `status_callback` receives the plain-words,
  > percentage-bearing message, that its absence wires nothing, and that a
  > callback which raises cannot break the search. `tests/unit/
  > test_window_opens.py::
  > test_clip_download_progress_reaches_the_status_bar_not_a_second_splash`
  > builds a real `MainWindow` (no `SplashScreen` anywhere in the test - the
  > whole point of this item), calls `engine.status_callback(message)`
  > directly and asserts `window.statusBar().currentMessage() == message`.
  >
  > All three new/changed files' directly relevant suites, plus
  > `test_embedder.py`, `test_splash.py`, `test_clip_lane_pipeline.py`,
  > `test_reverse_image_acceptance.py` and `test_search_notices.py`, are
  > green (108 tests). Two failures elsewhere -
  > `test_layer4_acceptance.py::test_a_missing_rerank_model_degrades_to_the_
  > fused_order` and `::test_a_filter_that_matches_nothing_returns_nothing_
  > calmly` - were checked against an unmodified worktree (a tagged `git
  > stash`, per this shared tree's own safety rule, not a bare one) and
  > reproduce there identically; they predate this session and touch neither
  > the image lane nor this wiring. `test_presenter.py::test_every_qt_view_
  > keeps_its_logic_in_the_presenter` (an unrelated `indexing_view.py` line
  > count, a file this session never opened) and three `test_docs_
  > versioned.py` header failures (on `ACTIVE_WORK.md`, `docs/WORKORDER-0q-
  > SESSION-2-HANDOFF.md`, `SESSION_CLOSE_2026-09-04.md` - none touched here
  > either) are the same: pre-existing, out of this item's scope, and not
  > this session's to fix.
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
> **2026-09-20, lane-2b-cut - window-visible cut from about 8 s to about 2.3 s in the offscreen
> measurement; 2b stays UNTICKED (target <1.5 s not reached).** *Offscreen (`QT_QPA_PLATFORM=
> offscreen`) from a scratch install (`tests/unit/e2e_support.py::build_scratch_install`), on a
> machine under heavy background load (a long Ollama evaluation was running) - close to, not
> identical with, a real display and the owner's machine.* Same install, same load, before and after
> interleaved, `startup: timings` line, warm runs (the first run after a copy is cold and is left out):
>
> | | splash | window visible | ready |
> |---|---|---|---|
> | before (HEAD), 3 warm runs | 165-182 ms | **8043 / 9668 / 10626 ms** (min 8043) | 18.9-22.1 s |
> | after, 7 warm runs | 130-170 ms | **2296 / 2311 / 2406 / 2450 / 2461 / 2872 / 3193 ms** (min 2296) | 15.9-21.0 s |
>
> **Where the time went (function-level, `cProfile` + `-X importtime`, not guessed).** The single
> largest cost between splash and window was `import lancedb` inside `VectorStore.connect()`, run
> synchronously in `app/main.py`'s `with` block before the window was built: 4.8 s under this load,
> almost all of it `lance_namespace_urllib3_client` building hundreds of pydantic validators and models (366 `validate_call` wrappers in the profile). Nothing the
> first paint shows needs it. **Fixed:** `VectorStore` / `ImageVectorStore` take `deferred=True`
> (used only by `app/main.py`): `__enter__` connects nothing, `warm()` connects on a background
> thread right after `window.show()`, and every read of the store's connection waits for it
> (`_db` / `_table` are now properties), so a search or an index run started the instant the window
> appears sees the same connected store. A store that cannot be opened (for instance the
> dimension-mismatch refusal) used to stop start-up in the "Cannot start" dialog; it now shows once,
> over the open window, in the same words (`_watch_vector_connect`, "Something needs attention"
> box) and again at the first use. Everything else that opens a store is unchanged (eager).
> Also cut, smaller: `numpy` is imported where `Embedder` normalises a batch, not at module level
> (0.2-0.3 s off the pre-window imports; it now loads after the window, with the vector-store connection); `tray.assets_dir()`
> is remembered once found - the window asked for it about 40 times (once per icon) at three
> `resolve()` calls each. Tests, written failing first: `tests/unit/test_vector_store_deferred.py`.
>
> **What is left, measured.** Of the remaining ~2.3 s: about 1.5 s is Python module imports between
> the splash and `MainWindow` (`app.ui.shell` and its views; about 0.4 s of it is the `app.extract`
> package, whose `__init__` imports every extractor so they can register, and which
> `app.ui.widgets.spreadsheet_view`, `app.core.code_types`, `app.core.media_open` and `app.main`
> itself each reach), about 0.45 s is `MainWindow.__init__` (Search, Files, rail, theme), about
> 0.25 s is `show()`. Reaching 1.5 s needs the extractor registry made lazy (a structural change
> to `app/extract/__init__.py`, out of a measured-fix lane) or the views' imports deferred past
> `show()`. **The 1.5 s target has not been measured on the owner's machine either; per the item's
> own words it is recorded, not promised.**
> **2026-09-20, lane-2b-registry - the extractor registry is lazy now; 2b stays UNTICKED, and
> this machine could not measure whether the target is met.** The note above named the next cut:
> "reaching 1.5 s needs the extractor registry made lazy (a structural change to
> `app/extract/__init__.py`)". That change is made. What could not be done today is the
> measurement: four agents were building and running tests on this machine at the same time, and
> the offscreen start-up timing moved by a factor of ten under that load.
>
> **What is lazy, and how nothing else changed.** The twenty-three `from app.extract import x as x`
> lines moved out of `app/extract/__init__.py` into a new `app/extract/_readers.py` - same lines,
> same alphabetical order, same form the generator writes into. `REGISTRY` and `NAME_REGISTRY` in
> `app/extract/base.py` are now a `dict` subclass that imports `_readers` on the *first read* -
> `REGISTRY[".pdf"]`, `in`, iteration, `len`, `.get/.items/.values/.keys/.pop/.clear/.update`, and
> `dict(REGISTRY)` - so `supported_extensions()`, `extractor_for()`, `extract()`, `format_health`,
> `app.cli formats`, `app/ui/widgets/file_types.py` and the walker all see the full set exactly as
> before, and `register()` still refuses a second claim on one extension (the `.svg`-twice failure
> that lost 32 files). `keys` *and* `__iter__` are both overridden deliberately: CPython copies a
> dict subclass's storage directly unless `__iter__` is overridden, so with only one of the two,
> `dict(REGISTRY)` would have returned `{}`. `app/core/scaffold.py` and `tests/unit/test_scaffold.py`
> now point at `_readers.py`; the generator's insertion, its alphabetical order and its
> duplicate-line guard are unchanged.
>
> **The trap, found by measuring rather than by assuming.** The first version of this change saved
> nothing at all. `register()` runs at the top of every extractor module and read the registry to
> refuse a duplicate - so importing *one* reader loaded all twenty-four, and `app/main.py` imports
> `app.extract.ocr` before the window to set the OCR device. `register()` now checks with a raw
> `dict.get`; the duplicate is still caught, by whichever of the two claims arrives second.
>
> **Deterministic evidence, which load does not distort** (`python -X importtime`, importing the
> pre-splash set and then the post-splash set `main.py` imports, in one process):
>
> | | modules imported after the splash | `app.extract.*` modules |
> |---|---|---|
> | before | 239 | 33 |
> | after | 201 | 9 (`base`, `chunker`, `cells`, `source_types`, `ocr`, `ocr_ladder`, `media_tools`, `transcribe`, the package) |
>
> On a quiet machine earlier the same profile put `app.extract` at 49 ms of 396 ms of post-splash
> import self-time; under the load that arrived later, 292 ms of 1867 ms. Both are the same 38
> modules; only the clock moved.
>
> **Why no before/after timing table.** Five warm offscreen runs of HEAD, taken before any of this
> (`QT_QPA_PLATFORM=offscreen`, scratch install, `startup: timings`): window visible 11159 / 3245 /
> **1466** / 1867 / 1921 ms. The minimum of the warm runs was already inside the 1500 ms target and
> the spread was eight-fold - so on this machine, today, the measurement cannot tell a 40 ms saving
> from the noise, and could be made to "prove" the target either way. An interleaved A/B (same
> load, HEAD install and changed install alternating) was built to beat the noise and did not
> produce a number: the HEAD copy of the scratch install would not start (its configuration carries
> absolute paths, and copying the install moved it), and by then splash-visible alone was running
> at 886-1265 ms against its own 300 ms budget.
>
> **So: not ticked.** The structural cut this item's own note asked for is in and is proved by the
> module count; the <1.5 s claim is not proved, and the remaining time is where the previous note
> already put it - `app.ui.shell`'s own view imports (about 180 ms of the post-splash self-time on
> a quiet machine), `MainWindow.__init__`, and `show()`, none of them in this lane's file scope.
> The next person to close 2b should take the numbers on the owner's machine, on a real display,
> with nothing else running.
>
> Tests, written failing first: `tests/unit/test_extract_lazy_registry.py` (the parser set is not
> imported by `app.extract`, `cells`, `source_types`, `timecode`, `app.core.code_types` or
> `app.extract.ocr`; reading the registry does import it; every accessor still answers in full;
> `dict(REGISTRY)` is not empty; a duplicate claim still raises) and a new case in
> `tests/unit/test_startup_import_order.py` (importing `app.ui.shell` in a fresh interpreter
> imports no parser).
- [x] **2b** defer what the first paint does not need: audit
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
  >
  > **2026-09-07, lane-d. Still NOT ticked - a real, measured, partial
  > improvement, not the target.** This session was explicitly told to
  > touch `app/ui/shell.py` (the opposite constraint from 2026-09-04
  > above) and to audit `MainWindow.__init__` line by line, which it did.
  > `SearchEngine`'s construction protocol was left alone exactly as
  > instructed - this is about the constructor's *own* work, not when or
  > how the engine is built.
  >
  > **What moved.** `MailView` and `CodeView` - two of the five
  > non-Search tabs - are now built by a new `_construct_secondary_views`,
  > scheduled with `QTimer.singleShot(0, ...)` from `__init__` exactly the
  > way `_start_background_work` already was (the established idiom in
  > this file - not a new pattern). Files, Indexing and Settings were
  > **not** deferred: reading `_wire_recorder()`, `_start_background_work`
  > and `_apply_theme()` line by line first showed all three already
  > reach directly into `self.indexing_view`, `self.files_view` and
  > `self.settings_view` (`_wire_recorder` connects their signals,
  > `_start_background_work` calls `indexing_view.refresh_totals` /
  > `settings_view.refresh_slow_labels` / `indexing_view.tuning.
  > start_detection`, `_apply_theme` reaches `settings_view.debug_pane`)
  > - all three already run synchronously inside `__init__` today, so
  > deferring any of those three views would also mean re-guarding all
  > three of those methods, not just the view itself. Mail and Code touch
  > none of them (grepped every `self.mail_view` / `self.code_view` site
  > in the file before moving anything), which is what made them the safe
  > subset for a first, conservative pass on the highest-risk lane - see
  > "not attempted" below for what a fuller pass would still need.
  >
  > **Verified safe, not assumed.** Every place in this file that could
  > reach `self.mail_view` / `self.code_view` before the deferred callback
  > fires is now guarded (`_focus_mail`, `_focus_code`, `_tab_changed`,
  > `_save_code_types`, `closeEvent`'s teardown loop) - each returns or
  > skips rather than raising `AttributeError`. The tab bar itself is
  > unchanged: Mail and Code are inserted at the exact position they held
  > in the old single `addTab` loop (right after Files), via
  > `QTabWidget.insertTab` plus a `_tab_index` refresh keyed on the
  > wrapped widget's own `indexOf`, so the order nobody has to relearn
  > does not move even transiently.
  >
  > **A pre-existing, unrelated bug found live, and fixed as trivial.**
  > Writing the immediate-close test for this item surfaced that
  > `closeEvent` already crashed on `self.scheduler.stop` if the window
  > closes before any event-loop turn - `self.scheduler` is only assigned
  > inside `_start_scheduler`, itself only ever called from
  > `_start_background_work`, deferred via the *same* pre-existing
  > `singleShot(0, ...)` this item did not introduce. Not this item's bug
  > to own, but it made the new closeEvent test permanently red for a
  > reason outside item 2b's scope, and the fix is the identical one-line
  > `getattr(self, "scheduler", None)` guard already being added to this
  > exact method for Mail/Code - so it was fixed rather than left broken
  > next to three lines that fix the same shape of problem.
  >
  > **Measured, offscreen, in the build sandbox - not the owner's machine.**
  > This sandbox has no display and no PyQt6 system libraries by default
  > (`libegl1`/`libgl1`/`libglx0`/`libglvnd0`/`libopengl0` installed from
  > `.deb`s for this session only); `QT_QPA_PLATFORM=offscreen` is not
  > comparable to a real Windows desktop's paint cost, and the fixture
  > engine/stores used here are the same synthetic ones
  > `tests/unit/test_window_opens.py` already uses (empty SQLite/LanceDB,
  > a stub `SearchEngine` with no real ONNX models) - not the owner's
  > ~100GB index. This measures **"did the constructor get lighter",
  > not "is the <1.5s window-visible target met"** - that second
  > question is 2a's, and it needs the owner's real machine, a real
  > profile and a real `SearchEngine`.
  >
  > With that caveat stated plainly: ten fresh `MainWindow` constructions
  > in one process (module-scoped, matching this repo's own
  > `test_window_opens.py` fixture pattern - building and tearing down
  > many in a loop is known to be fragile, see that file's docstring, so
  > single-process-per-construction was also tried and produced far
  > noisier numbers dominated by interpreter/Qt cold-start cost rather
  > than the constructor itself) gave, before → after:
  >
  > | | median | mean | min | max |
  > |---|---|---|---|---|
  > | before (unpatched) | 662.3 ms | 760.8 ms | 584.8 ms | 1459.3 ms |
  > | after (Mail+Code deferred) | 179.3-407.9 ms across two separate runs | 225.2-462.7 ms | 151.2 ms | 596.5-885.9 ms |
  >
  > The range on the "after" row is the honest finding, not a typo: this
  > shared sandbox showed enough run-to-run variance for the *identical*
  > patched code (179ms median one run, 408ms median a few minutes later)
  > that no single number here should be read as precise - only the
  > direction (consistently, substantially lower than the 662ms baseline,
  > across every paired comparison run) is trustworthy from this sandbox.
  >
  > **Verify on the owner's Windows machine** - the number 2a's own table
  > already measures (`log.info("startup: timings - splash {}ms, window
  > {}ms, ready {}ms", ...)` in `logs\runs\run-*-window.log`) is the one
  > that actually answers this item's target:
  >
  > ```powershell
  > venv\Scripts\pythonw.exe -m app.main
  > ```
  >
  > then read the run log's "window {}ms" figure and compare against
  > 2a's pre-change table (2,820-6,590ms). A constructor-only re-measure
  > without a real display, matching this session's own script, is also
  > left at `tests/unit/test_window_opens.py`'s fixture pattern for
  > anyone who wants the sandbox-relative number again without the noise
  > of a fresh process per sample.
  >
  > **Not attempted, and why**: deferring Indexing, Settings and Files -
  > the larger remaining share of the constructor's cost per this order's
  > own audit above - needs `_wire_recorder`, `_start_background_work`
  > and `_apply_theme` re-guarded first, which is a materially bigger,
  > more invasive change to a file explicitly flagged as this repo's
  > highest-risk lane. Left for a follow-up rather than attempted in the
  > same pass as a first, conservative deferral - "working version first"
  > and "correctness over cleverness" both argue for landing the smaller,
  > fully-verified subset rather than a larger, partially-verified one.
  >
  > Tests: `tests/unit/test_window_opens.py` gained seven - the deferral
  > itself (`mail_view`/`code_view` absent immediately after construction,
  > present after the event loop turns), tab order preserved, three
  > rapid-interaction guards (shortcut, tab-switch, immediate close, each
  > exercised with **zero** `processEvents()` calls first - the exact gap
  > this item's own risk is about), and a regression guard proving Search
  > and Files were *not* pulled into the deferred set. All 22 tests in the
  > file pass, offscreen, `--basetemp=/tmp/pytest_tmp_laned` (this
  > sandbox's SQLite-under-FUSE trap, per a sibling lane's finding).
  >
  > **2026-09-29, on the owner's laptop, real display - TICKED.** Window
  > visible **839 / 742 / 777 ms** across three warm starts through
  > `leasha.cmd` (the run log's `startup: timings` line), against <1.5 s.
  > Measured with a sampling profile of the main thread, not inferred. What
  > it took, beyond the earlier deferral:
  >
  > * **`git describe` on the window's thread, 5 s.** `debug_recorder.
  >   _environment` took the version from `build_info()`, which also runs
  >   `git describe` (timeout 5 s). With debug recording on - the owner's
  >   setting - that ran inside `MainWindow.__init__`, and from `leasha.cmd`
  >   it hung to its timeout on every start: window 5.9-6.0 s. Now
  >   `version()`, which reads the `VERSION` file. The first start after a
  >   code update is still slower (7.8 s seen, Python recompiling on Google
  >   Drive) - a one-off, not the warm figure this item is about.
  > * **The splash handed over 3-5 s late.** It closed after the deferred
  >   pages and the vector connect, so it sat over a finished window. It now
  >   closes straight after `window.show()`; `MainWindow.hold_deferred_start`
  >   / `release_deferred_start` keep the pages from building inside its
  >   hold and fade, which pump events. Probe of the real windows: splash
  >   gone 0.27-0.8 s after the window appears (was 5.1 s).
  > * **A third window.** The owner: "a splash comes up, then a small window
  >   and then the main window". A separate top-level "Leasha" window lived
  >   1.6 s after the main one appeared: `ChatBox` called `setVisible(True)`
  >   on itself before it had a parent (in `__init__` and again from
  >   `set_manual` via `load`), which Qt shows as a window of its own until a
  >   layout adopts it. `DebugPane`'s pop-out button and the search bar's
  >   Interpret hint did the same. All four are hide-only now; the probe shows
  >   splash then window, nothing between.
  > * **Splash text off the edge** (owner, same day). Lines had fixed boxes
  >   and no wrap: one case line was 431 px for 417 at the normal size, all
  >   five over at 13 pt. `fitted_font` wraps, then shrinks to a 7 pt floor;
  >   the approved wording is unchanged.
  >
  > Tests, each failing before its fix: `test_window_opens.py`
  > (`..._opens_as_a_window_of_its_own`, `..._rebuilds_nothing`),
  > `test_splash_handoff.py`, `test_debug_recorder.py`
  > (`test_starting_a_recording_never_runs_git`), `test_splash.py`
  > (`test_no_splash_line_runs_off_the_splash` at 9/11/13 pt,
  > `test_the_tagline_is_the_approved_text`).
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
- [x] pytest-qt: splash constructs offscreen, cycles all five cases with
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
  >
  > **2026-09-07, lane-c.** Re-checked honestly against 1b's 2026-09-05 fix
  > rather than assumed still-open, per this order's own instruction to
  > revisit a note after the fix it depended on lands (the 0c/0e/0d/0g/0p
  > pattern in `HANDOFF.md`). Read `test_minimum_hold_timing` line by line as
  > it stands today: it now calls `hide_and_close()` at the boundary and
  > asserts real elapsed time (`elapsed >= 0.25` against a 0.3s remaining
  > hold) - the vacuous version this note complained about is gone, and
  > `test_hide_and_close_fades_opacity_to_zero` / `test_hide_and_close_
  > still_closes_if_fade_raises` confirm a real fade exists and is H4-safe.
  > So (e) minimum-hold and (c) fades are each genuinely true in isolation
  > now. But re-reading the item's own sentence as ONE scenario (which its
  > wording is - "splash constructs offscreen, cycles all five cases with
  > fades, shows each moment's status line, respects minimum hold", not five
  > separate bullet points) surfaced two things still short of that, even
  > post-1b: `test_splash_case_rotation` advances the rotation timer by
  > exactly *one* step, not through all five cases with the wrap back to the
  > first; and no test tells the splash every one of §0.5's status strings
  > and checks `_status_message` reflects each - `test_splash_reports_
  > progress` checks two ad hoc messages, and `TestGetSplashStatusText`
  > checks three of the six via the `get_splash_status_text()` helper
  > function rather than the live splash object. Fades, too, were only ever
  > exercised through `hide_and_close()` on a splash that had done nothing
  > else (no rotation, no status reports) in the same test.
  >
  > Added one new composed test rather than patch each piece separately,
  > matching the item's own framing: `tests/unit/test_splash.py::
  > TestSplashScreen::test_offscreen_scenario_rotates_reports_and_holds_
  > then_fades`. One `SplashScreen`, offscreen (the same session-scoped
  > `QT_QPA_PLATFORM=offscreen` fixture every other `@pytest.mark.qt` test in
  > this file already relies on - no new fixture): shown, told all six of
  > §0.5's status strings in turn with `_status_message` asserted after each;
  > rotated through all five `CASE_LINES` with the index asserted to wrap
  > back to its start and all five pre-wrap indices distinct; then one
  > `hide_and_close()` call on that same instance, with elapsed time proving
  > the hold was genuinely waited out and `windowOpacity()` proving the fade
  > ran, ending invisible. (f) the white-wordmark derivation test (1d,
  > `TestWhiteWordmarkDerivation::test_white_wordmark_differs_only_in_navy_
  > pixels`) needed nothing further - already existed, already passed.
  >
  > `tests/unit/test_splash.py` was 16 tests before this (per 1b's own
  > count); now 17, all green (`17 passed in 4.21s` under
  > `QT_QPA_PLATFORM=offscreen`, `--basetemp=/tmp/pytest_tmp_lanec`).
- [x] startup: a stub-slowed stage still shows the splash within its
  budget (the <300ms import discipline as a test on what `main` imports
  before splash-show).
  > **2026-09-04: NOT ticked — not written.** No such test exists anywhere
  > in the suite. Not attempted here: it wasn't one of this session's six
  > assigned implementation items, and the import-discipline it would check
  > (only stdlib+Qt precede splash-show in `main.py`) held up in every live
  > run measured for 2a (splash visible in 152–304ms), so there was no
  > regression to chase — but the test itself is still missing.
  >
  > **2026-09-07, lane-c. Written - and it found a real bug the 2026-09-04
  > note's inference missed.** Chose the static/import-order approach the
  > item's own wording asks for literally ("the <300ms import discipline as
  > a test on what `main` imports before splash-show") over a timing-based
  > one, which would be flaky in exactly the sandbox this is being verified
  > in. New file `tests/unit/test_startup_import_order.py::
  > TestStartupImportOrder::test_no_heavy_app_import_precedes_splash_
  > construction` parses `app/main.py` with `ast` and collects every import
  > that runs, in actual execution order for the normal startup path
  > (module-level statements, then `main()`'s own imports, then
  > `_run_window()`'s imports up to - not including - the line that
  > constructs `SplashScreen()`), asserting each is stdlib, Qt, or one of the
  > small `app.core`/`app.ui.splash`-family modules the splash itself needs.
  >
  > **Run against the code as it stood, this failed - honestly, not
  > vacuously**: `app.index.embedder`, `app.search.vector`, `app.search.
  > engine`, `app.search.rerank`, `app.storage.sqlite_store`, `app.storage.
  > vector_store` and the whole of `app.ui.shell` were all imported in
  > `_run_window` *before* `QApplication` was even constructed - let alone
  > before `SplashScreen().show()` - and `app.extract.ocr` (the OCR
  > device-configuration import) was imported earlier still. This directly
  > contradicts `app/main.py`'s own comment above `splash.show()` and
  > `app/ui/splash.py`'s module docstring ("Only stdlib and Qt are imported
  > before it is shown"), and is exactly the gap the 2026-09-04 note's
  > inference missed: "held up in every live run measured for 2a" was true
  > of the *total* splash-visible time on the owner's machine, but nobody had
  > actually read the import statements themselves to check what was
  > producing that number - the "verify, never guess" mistake this project
  > keeps a standing rule about, this time from a previous session rather
  > than this one.
  >
  > **Fixed minimally, per this work order's own instruction to fix a bug
  > surfaced while verifying.** In `app/main.py::_run_window`: the
  > `PyQt6.QtWidgets.QApplication` import stays where it was (needed to
  > construct `application` before the splash can exist), but the heavier
  > block (`Embedder`, `vector`, `SearchEngine`, `Reranker`, `SqliteStore`,
  > `ImageVectorStore`/`VectorStore`, `MainWindow`) and the OCR device-
  > configuration import+call now run *after* `splash.show()` and
  > `application.processEvents()`, in their own `try/except ImportError`
  > that calls `splash.widget.close()` before falling through to the same
  > `_fatal(...)` dialog on a genuinely missing dependency (H4: no dialog is
  > worth a splash stuck on screen). No behaviour changes for a normal start
  > or for the missing-dependency error path beyond the splash closing first;
  > `app/ui/splash.py` itself was not touched (§0: do not redesign).
  >
  > **Verified, not assumed.** `tests/unit/test_startup_import_order.py` (2
  > tests, including a guard against the line-lookup itself silently finding
  > nothing and passing vacuously) is green after the fix, and was confirmed
  > red before it (the failure listed all eight violating imports by line
  > number). Re-ran `tests/unit/test_splash.py` (17/17), `tests/unit/
  > test_exit_placement.py` (3/3 - the AST-based `_exit_fast` placement check
  > is unaffected by this reordering) and `tests/unit/test_window_opens.py`
  > (16/16, since it constructs a real `MainWindow`) to confirm nothing else
  > broke. No test anywhere asserted the old import order, so nothing needed
  > updating for the move itself.
  >
  > One caveat, stated plainly rather than left implicit: this sandbox's
  > per-module import timings (measured separately, not part of the test)
  > were dominated by a cold-disk-cache effect specific to this FUSE-mounted
  > checkout - `app.ui.shell` measured over 20s on a first-ever import and
  > well under a second once the filesystem cache was warm - so no timing
  > number from this sandbox is offered as evidence of the real cost on the
  > owner's Windows machine. What is offered as evidence is the structural
  > fact, read directly from the source and now held by a test: those
  > imports no longer precede splash-show, which is what §0.7 and this
  > file's own docstring both already claimed was true.
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

> **2026-09-07 status, lane-c.** 17 of 18 checkboxes now ticked - only 2b's
> own item is still open (a different lane's work, in progress; not touched
> here). Since the 2026-09-04 status note above: 1b, 1c's second clause, 3b
> and 3c were each fixed and ticked by later sessions on 2026-09-05/2026-
> 09-07 (see their own dated notes), and this session closed both remaining
> §4 test lines - the composed pytest-qt scenario and the startup import-
> order test, the latter finding and fixing a real pre-existing bug (heavy
> first-party imports running before splash-show). See those two items'
> own 2026-09-07 notes for the detail.

> **2026-09-19 status: 2b's deferral is built; its item stays unticked.** The
> item's own words are a target - *window visible <1.5s warm on the owner's
> machine, recorded, not promised* - and that number has not been measured
> there, so the box stays open rather than being ticked on the strength of
> construction alone. What exists: `MainWindow._construct_deferred_pages` in
> `app/ui/shell.py` builds Indexing and Settings after the first paint, joining
> Mail and Code (deferred on 2026-09-07); the `_wire_recorder()`,
> `_start_background_work()` and `_apply_theme()` reaches into those pages are
> guarded, and the work that needs them is chained to run once they exist. It
> shipped in `9836255` with nine new tests in `test_window_opens.py` (32 pass
> in that file today). **Files and Search are not deferred** - they stay
> synchronous, and `test_files_and_search_are_not_deferred` pins that. Known
> costs, found while building it and not fixed: `_apply_theme` runs twice at
> startup, and an F5 or a file drop that arrives before the deferred build has
> finished is skipped. **To close 2b:** launch three times on the owner's
> machine, record the window-visible figure from the run log's `startup:
> timings` line, and tick if it is under 1.5s.

> **2026-09-20 - the two "known costs" of 2b's deferral are fixed** (2b itself stays open: its
> <1.5 s target has still not been measured on the owner's machine). (1) `_apply_theme` no
> longer runs twice at startup: the first call (first paint) sets the window's stylesheet, and
> the deferred pages call `_push_palette` alone - it only tells pixmaps and the log pane the
> colours - so Qt no longer re-polishes the whole tree, Settings included, to set the same sheet.
> (2) An F5, a drop or "Index this folder" that arrives before the pages exist is **queued and
> replayed once** when the build finishes (several become one run: `None` roots covers any named
> folder; otherwise the folders are merged; a request that names folders brings Indexing
> forward). Tests: `test_startup_sets_the_window_stylesheet_once_not_twice`,
> `test_an_f5_in_the_gap_is_replayed_once_the_page_exists`,
> `test_a_folder_dropped_in_the_gap_is_indexed_and_shown` in `test_window_opens.py`.

> **2026-09-29 status: 18 of 18 ticked - every item done.** 2b closed on the owner's laptop
> (see its note: 839 / 742 / 777 ms warm, real display). Delivered with it, on the owner's word
> the same day: the stray third window between splash and window, and splash text that ran off
> its edge.

## Done means

Change + tests + suite green + committed by name; the measured numbers
written into §2/§3; CHANGELOG. Acceptance sentence: launch Leasha and
within a third of a second the brand is on screen telling you one true
thing it can do; the window follows fast, and warming up never blocks it;
close it and it is gone from the screen at once, gone from the machine in
moments — and launching again straight away just works, with the splash
explaining any wait instead of a busy cursor explaining nothing.
