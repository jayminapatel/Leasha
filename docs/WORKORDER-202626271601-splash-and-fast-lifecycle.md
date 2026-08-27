# Work order (One thread): the splash, and a life that starts fast and ends fast

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
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

- [ ] **1a** `app/ui/splash.py`: frameless top-level widget (or
  `QSplashScreen` subclass) painting §0 entirely in code from theme-free
  constants (the splash is brand, not theme — identical in light and
  dark). Shown from `main.py` immediately after `QApplication` exists and
  the icon is installed; every later startup stage reports to it via a
  simple callable (the same strings the log breadcrumbs use, translated
  to plain words in one table).
- [ ] **1b** rotation/fade timers live on the splash and run through every
  moment (normal, first-run download, handover wait) per §0.4; the
  minimum-hold logic delays the handover to the main window, never the
  work itself (startup continues underneath; only the fade waits).
- [ ] **1c** first-run: when the model cache is missing, warm-up's
  download progress (fastembed reports bytes) streams to the splash bar;
  if the window is already up when a download starts (cache emptied
  mid-life), the existing notices bar carries the message instead — H4
  register, no second splash.
- [ ] **1d** the white-wordmark derivation of §0.2, cached beside the
  asset; a test asserts the derived image differs from the source only in
  the navy-family pixels.

## 2. Startup, faster underneath the splash

- [ ] **2a** measure first (the §0 problem statement is diagnosis, not
  numbers): log process-start → splash-visible → window-visible →
  warm-up-complete on the owner's machine; record the numbers in this
  file. The splash hides the wait; this section shrinks it.
- [ ] **2b** defer what the first paint does not need: audit
  `MainWindow.__init__` for work movable to after `show()` (the M11
  pattern — construct light, populate async). Target: window visible
  <1.5s warm on the owner's machine, recorded, not promised.
- [ ] **2c** the handover wait paints honestly: during
  `acquire(wait_s=HANDOVER_WAIT_S)` the splash shows the §0.5 handover
  line — the 12s worst case becomes an explained wait instead of a dead
  cursor.
- [ ] **2d** installer prefetch: `install.ps1` optionally downloads both
  models (embedder + reranker) into `MODEL_CACHE` at install time with
  visible progress, so first launch never downloads. On by default,
  skippable (offline installs must still work — the app's own first-run
  path remains the fallback, which is why 1c exists).

## 3. Close, faster and honest to the end

- [ ] **3a** time the tail: the stage log currently ends at engine close;
  add breadcrumbs+timings for store close, vector close, and final exit
  so the next slow stage names itself. Record before/after numbers here.
- [ ] **3b** perceived-instant close: on a real quit the window **hides
  first**, then the existing staged teardown runs invisibly. The
  disable-input discipline of `_drain_workers` stays; the tray icon (when
  installed) is the only visible remnant and disappears last.
- [ ] **3c** `PRAGMA optimize` moves off the exit path: run it on idle
  (the enrichment-backlog/idle pattern, or a coarse every-N-hours timer)
  instead of once per connection at close. SQLite's own guidance is
  periodic, not at-exit; close then pays nothing for it.
- [ ] **3d** after the stores and lock are cleanly released, skip
  interpreter teardown of the heavyweight native modules with
  `os._exit(code)` — placed so it is provably after `SqliteStore.__exit__`
  and `VectorStore.__exit__` and the log flush, never before. This is the
  standard remedy for slow onnx/arrow unload; the placement constraint is
  the whole safety argument, so it gets a test and a loud comment.
- [ ] **3e** the handover benefits measured: relaunch-after-close wait
  time on the owner's machine before/after, recorded here — this is the
  number the 12s `HANDOVER_WAIT_S` exists to absorb, and it should
  shrink.

## 4. Tests

- [ ] splash strings pass the plain-words rules and the invented-number
  deny-list (§0.8); tagline and case lines byte-exact against §0 (brand
  copy is load-bearing — a test holds it, the keep-descriptions rule
  applies to it from release).
- [ ] pytest-qt: splash constructs offscreen, cycles all five cases with
  fades, shows each moment's status line, respects minimum hold; the
  white-wordmark derivation test (1d).
- [ ] startup: a stub-slowed stage still shows the splash within its
  budget (the <300ms import discipline as a test on what `main` imports
  before splash-show).
- [ ] close: hide-first verified (window invisible before drain begins);
  `os._exit` placement — a test proves the exit call is unreachable while
  a store is open; idle-optimize runs and close no longer calls it.
- [ ] measurements recorded in this file: 2a, 2b target, 3a, 3e.

## Done means

Change + tests + suite green + committed by name; the measured numbers
written into §2/§3; CHANGELOG. Acceptance sentence: launch Leasha and
within a third of a second the brand is on screen telling you one true
thing it can do; the window follows fast, and warming up never blocks it;
close it and it is gone from the screen at once, gone from the machine in
moments — and launching again straight away just works, with the splash
explaining any wait instead of a busy cursor explaining nothing.
