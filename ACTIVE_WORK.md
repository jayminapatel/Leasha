# Active Work Tracker

**Doc version:** 1.0 · **Updated:** 2026-09-04 · **Applies to:** app v0.3.3

**Last updated:** 2026-09-04 · **Current session:** Cowork + Claude Code coordination

---

## ✅ RESOLVED (autonomous tick): 3 of the 6 concrete bugs from the reconciliation pass

Fixed and verified while the owner was away, following the reconciliation below - each is a
small, independently-testable local edit, left uncommitted (matching this checkout's
existing convention) pending the owner's review.

**1. Window-geometry restore was a silent no-op, every launch (Order 0p §4).**
`save_window_state()` returned `QByteArray`, not real `bytes` as its own signature claimed;
`shell.py` stored it via a shared, `str()`-based generic key-value store, which mangled it
into a repr string; the read-back `isinstance(bytes)` check then always failed, silently.
**Fix:** `save_window_state()` now returns real `bytes` (`bytes(window.saveGeometry())`);
`shell.py` base64-encodes on write and decodes on read, round-tripping the actual bytes
through the string-only storage layer instead of `str()`-mangling them. Verified: the
specific tests that exercise this exact path (`test_save_returns_bytes`,
`test_window_saves_geometry_on_close`, `test_maximised_window_state_restored`) now pass.

**Separate issue surfaced, not fixed:** 5 other `test_window_state.py`/`test_window_opens.py`
tests still fail on exact width/position values (e.g. expecting 900px restored, getting
798px) - traced to Qt's `restoreGeometry()` fitting windows to the `offscreen` QPA test
platform's 800×800 virtual screen, reproducible with plain Qt widgets with no app code
involved. This looks like a test-environment constraint, not a code bug, but needs an
owner or Qt-specialist decision (adjust the tests' expected sizes to fit within 800×800,
or find a way to run this test file against a larger virtual screen) rather than more
guessing.

**2. `email_mbox.py`: 2 of 3 edge-case bugs fixed (Order 0g).**
- `.mbox.bak` files were never recognized: `Path.suffix` only returns the *last* extension
  component (`"archive.mbox.bak"` → `.bak`), so the membership check against the compound
  `.mbox.bak` string could never match. **Fixed:** check the filename's tail directly.
- A missing file raised raw `mailbox.NoSuchMailboxError` (not a subclass of the
  `FileNotFoundError` the code was catching) straight past this extractor's own error
  contract. **Fixed:** catch both.
- **Not fixed, deliberately:** the "empty subject stores `None` instead of `\"\"`" report is
  real, but traced to `build_email_document()` in `email_files.py` - shared code used by
  every email extractor (EML/MSG too), which does `subject or None` *and* `sender or None`
  as a matched, symmetric pair. That reads as a deliberate convention (e.g. NULL vs empty
  string for some downstream filter/query), not an isolated mistake - changing it based on
  one new mbox test's assumption, without knowing what depends on the `None`, is a bigger
  and more speculative call than an autonomous pass should make alone. Left for the owner
  to decide; `test_empty_subject_handled` still fails, correctly, until that decision is
  made.
- Verified: `test_email_mbox.py` 12/13 pass (up from 9/13); `test_takeout_acceptance.py`
  and `test_email_pst.py` (32 tests, unrelated extractors) still fully pass - no regression
  from touching shared code paths.

**3. `presenter.py`: sentence-boundary snippet snap fixed (Order 0q, item 1b-1c).**
`_snap_back()` only ever searched *backward* from the ideal window start for a nearby
sentence boundary - but the failing case (and the whole reason this function exists, per
its own docstring) is exactly the opposite: the density-derived ideal start lands mid-word,
partway through an *earlier* sentence, and the next sentence begins only a few words
*later*. A backward-only search can never find a boundary that is only ahead of the start.
**Fix:** try backward first (loses the least content when both exist), then the same check
forward within the same small distance, before falling back to a bare word boundary.
Verified: `test_snippets_snap_to_sentence_boundaries` passes; full `test_presenter.py`
suite (68 tests) passes.

**Bonus, found while verifying the above:** `presenter.py`'s `elide_path_left` docstring had
literal Windows paths (`\Projects\Foo\Final`) in a non-raw docstring, triggering a
`SyntaxWarning: invalid escape sequence '\P'` on every import - this was the exact cause of
a previously-failing test (`test_no_stray_paths.py::test_no_module_compiles_with_a_syntax_warning`).
**Fixed:** made the docstring raw (`r"""..."""`). Verified: that test file's 6 tests all
pass now.

**4. Bonus, `requirements.txt`'s rawpy comment corrected (bug #7).** It claimed rawpy is
"pure Python with no C extension on Windows" - rawpy wraps the LibRaw C library via Cython;
it is not pure Python. The comment now says that plainly, while preserving the actually
useful claim underneath it: rawpy ships a prebuilt Windows wheel, so installing it needs no
local compiler, matching this file's own stated "wheel rule."

**Deliberately not touched this tick:** bug #2 (`ocr_ladder.py` dead code + internal
routing bug - touching the live extraction pipeline is more invasive than a scoped fix),
bug #5 (zero test coverage on the FTS-trigger code - writing new tests is its own task, not
a bug fix), bug #6 (`run_tests.py` - recommended for discarding, not fixing, and deleting a
file is a decision left to the owner). See the reconciliation section below for full detail
on all 6.

---

## ✅ RESOLVED: Window went blank (then black) after minimize→restore, with runaway memory growth

**Found and fixed by:** Claude Code, 2026-09-04, driving the real window end-to-end (not a
test — `app/main.py` launched, window visually inspected via screenshots and Win32 API
state checks). Reported by the owner as "when you maximise and minimise the screen layout
gets screwed up... look on the indexing page and must be check elsewhere too."

**Root cause, confirmed empirically (not guessed):** a diagnostic build logged
`isVisible()` from inside the deferred restore handler - it printed `False`, even though
Win32 already reported the window as shown, on-screen, and correctly sized. Qt's own
internal visibility bookkeeping had not caught up with the native window by the time the
deferred callback ran, so `update()`/`repaint()` were silently no-ops (Qt skips scheduling
paint for anything it believes is not visible) - the very code meant to fix the blank
window was being skipped by the thing it called.

**Fix, in `app/ui/shell.py`:**
- `changeEvent` now also handles the *un-minimise* transition (previously it only handled
  going minimised, for the tray), deferring to a new `_repaint_after_state_change`.
- That method calls `self.setVisible(True)` on the top-level window **before** `update()` -
  this reconciles Qt's bookkeeping with reality first - then walks every child widget with
  `update()`. (Not `child.setVisible(True)` on every child - that would wrongly reveal
  anything legitimately hidden, like an inactive tab's page or a closed popup. Fixing the
  ancestor is enough for `update()` to reach everything actually meant to be shown.)

**Verified, not assumed:**
- Full repro sequence (minimize → restore → maximize → restore) now renders correctly at
  every step, run repeatedly, on both a fresh launch and mid-session.
- The separate Indexing-tab "Index tuning" garbled/ghosted-text bug (reported in the same
  message, "look on the indexing page") turned out to share this exact root cause - it
  disappeared with the same fix, confirmed on a completely fresh launch going straight to
  that tab as the first action.
- Memory: previously grew from ~105MB to ~2.4GB while sitting untouched in the broken
  state; after the fix, sat flat (~512MB → ~513MB) over a full idle minute. The "leak" was
  a symptom of the broken repaint loop, not a separate problem.
- `tests/unit/test_shell.py` (20 tests) and `tests/unit/test_splash.py`: all pass, no
  regressions.

---

## ✅ RESOLVED: Splash screen never closed, crashed some startups, and painted a distorted/blurry logo

**Found and fixed by:** Claude Code, 2026-09-04, in the same live-testing session, per the
owner's follow-up: "the splash screen stays up and the text which is changing should be
next to the icon also the leasha icon looks blurry address this in the splash and shutdown
screen too."

Four separate bugs in `app/ui/splash.py` (a new, uncommitted file from Task C's Order 0r
work) plus one wiring gap in `app/main.py`:

1. **`setAttribute(4, True)`** and **`setWindowFlags(0x00000080 | 0x00000010)`** used raw
   ints instead of `Qt.WidgetAttribute`/`Qt.WindowType` enum members. PyQt6 is strictly
   typed here and raised `TypeError: unexpected type 'int'` at construction - **this
   crashed every single startup**, before the window ever showed, every time. Fixed by
   using `Qt.WidgetAttribute.WA_TranslucentBackground` and `Qt.WindowType.FramelessWindowHint
   | Qt.WindowType.WindowStaysOnTopHint`.
2. **`_paint_icon`'s `drawEllipse`/`drawRect`/`drawLine` calls** passed bare Python floats
   (`size / 4` etc. - true division always yields float). PyQt6 has no overload taking four
   bare floats for these; only `QRectF`/`QLineF`/int-tuple forms. This didn't crash startup
   (caught per-paint-event) but spammed `UNHANDLED TypeError` into the log on every
   rotation for 3 of the 5 case icons. Fixed by building `QRectF`/`QLineF` from the floats
   instead of passing them positionally.
3. **Nothing ever called `hide_and_close()`.** It existed, fully implemented, and `main.py`
   showed the splash, reported every startup stage through it, and then just called
   `application.exec()` - so it sat on screen, rotating its 5 cases, for the entire life of
   the process. This is what the owner meant by "stays up." Fixed with one line in
   `main.py`, right after `status_reporter("Ready")`.
4. **The logo was squashed.** `leasha-logo.png` is 2667×1611 (≈1.66:1, a wide wordmark), but
   `_paint` forced it into a literal `logo_size × logo_size` square via
   `drawPixmap(x, y, logo_size, logo_size, pixmap)` - visible vertical distortion. Combined
   with no `SmoothPixmapTransform` render hint (Qt's fast/low-quality scaling path), this is
   what the owner saw as "blurry." Fixed both: derive width from the pixmap's own aspect
   ratio instead of forcing a square, and enable `SmoothPixmapTransform`.
5. **Icon and text read as two unrelated things**, not one line: the icon was anchored at
   `center - w/6` and the text at `center + h_icon/2`, independently - a ~90px gap on this
   widget's size. Fixed by treating them as one group indented from a shared left margin
   with a small fixed gap.

**On "shutdown screen":** no separate shutdown/closing splash exists in the codebase to
fix - `MainWindow.closeEvent` documents "Perceived-instant close (§3b)": the window hides
immediately and the real teardown runs invisibly behind that. If the owner has seen or
wants a visible closing screen, that is a new feature request, not a bug in an existing
one - worth a decision before anyone builds it.

**Verified:** clean startup log with zero exceptions through multiple full splash
rotations (previously crashed before the window ever showed); splash now closes itself
once the window is ready (confirmed via taskbar - only one window entry, not two-to-three
lingering ones); logo renders at correct proportions, confirmed via zoomed screenshot;
icon+text confirmed adjacent, reading as one line. One regression caught and reverted
during verification: an initial fix deferred `hide_and_close()` to respect the documented
1.2s minimum hold, which broke `test_splash_shows`'s synchronous
`assert isVisible() is False` right after the call - reverted to a synchronous close,
since real startup (embedding model + reranker + window construction) already takes
several seconds, well past 1.2s, making the deferral unnecessary in practice.
`tests/unit/test_splash.py`: all pass, no regressions.

---

**This is the default Windows minimize/restore path, not the tray feature.**
`TrayPresence.minimise_to_tray` was confirmed `False` (its default — only set `True` by
an explicit, persisted `"ui:tray_minimise"` setting nobody had turned on this run), so
`changeEvent`'s `_hide_to_tray` branch in `app/ui/shell.py` never ran. The repro is just:
click the native taskbar minimize button, then restore. Every user hits this, not just
tray users.

### Repro, exact steps

1. Launch the window normally. Everything paints correctly (verified: Search tab,
   Indexing tab, live search results all render).
2. Click the taskbar/title-bar **minimize** button. Confirmed via Win32
   (`IsIconic=1`, off-screen sentinel rect `(-25600,-25600,...)`).
3. Restore it — by taskbar click, or programmatically via
   `ShowWindow(hwnd, SW_RESTORE)` + `SetForegroundWindow(hwnd)` (tried both; identical
   result). Win32 now reports `visible=1, iconic=0`, a normal on-screen rect, and the
   window's **title bar and frame paint correctly** — but the **entire client area
   renders solid white**. No tabs, no content, nothing.
4. A forced resize (`MoveWindow` nudge, +10px then back) does **not** recover it —
   still blank. Ruled out as a simple "missing resize event" cause.
5. Clicking inside the blank area still works *logically* — confirmed via
   `logs/sessions/session-*.jsonl`: a click at the old Search-tab coordinate correctly
   logged `{"kind": "tab", "name": "Search", ...}`, and typing + Enter in the (invisible)
   search box correctly executed a live search
   (`{"kind": "search", ..., "results": 50, "reranked": true, ...}`). **The app is not
   crashed or hung — only the paint layer is broken.** The stale, never-cleared text box
   also meant the blind-typed query appended to old content (`"readmereadme"`, 0 keyword
   hits) rather than replacing it — a second, smaller symptom of the same
   never-repainted state.
6. Toggling **maximize** while stuck blank does not recover it either — it went from
   blank **white** to **solid black** instead, and stayed black after restoring back to
   normal size. The corruption compounds with further state-change attempts; it does not
   self-heal.
7. **No error, warning, or any log line was written at any point during this entire
   sequence** — `logs/runs/run-*-window.log` has nothing between the last real search
   and process exit, and `logs/crash/crash.log` is empty. That gap is worth calling out
   on its own: this app's stated design principle is "nothing fails silently," and a
   silently-blank main window is exactly the kind of degraded-but-looks-fine (or in this
   case degraded-and-looks-broken-but-isn't) state that principle exists to catch.
8. **Memory:** the process (`pythonw.exe`) grew from ~105MB (normal, models loaded) to
   **2.4GB** while sitting untouched in the blank/black state with no user action logged
   after the last real search. Something in this code path is very likely leaking on
   repeated (failed) repaint/state-change attempts — this needs profiling, not just a
   visual fix.

### Separate bug found on the Indexing tab, independent of the above

Even in the **normal, never-minimized** window, the "Index tuning" section renders with
**overlapping/ghosted text** — the tuning-mode value (looks like it should read
"Defaults") and the tick/segment bar beneath it both show doubled, smeared text as if two
paint passes landed on the same pixels without clearing between them. This persists
through a maximize (confirmed, still garbled at full screen size) and is visible on a
totally fresh, first-ever window open — it has nothing to do with the minimize/restore
bug above, but the same *class* of problem (stale paint, background not cleared before
redraw) makes both worth investigating together.

### Where to look

- `app/ui/shell.py` — `changeEvent` (line ~2049) and `closeEvent` handle window-state
  transitions but there is **no `showEvent` override and no forced repaint anywhere** in
  this file. Nothing intercepts the native minimize→restore transition at all when
  tray-minimize is off — Qt's default handling is left to do the whole job, and it isn't
  doing it.
- `app/ui/tray.py` `TrayPresence.restore()` (`showNormal()` + `raise_()` +
  `activateWindow()`) is the tray-specific restore path, confirmed **not** exercised in
  this repro (`minimise_to_tray` was off) — don't assume fixing this method fixes the bug
  above, but it's worth adding the same recovery there defensively once a fix is found,
  since it calls `showNormal()` too and may share the same underlying issue.
- The Indexing-tab tuning widget doing the overlapping paint — likely in
  `app/ui/indexing_view.py` or `app/ui/tuning.py` given the "Index tuning" section
  content — worth checking for a custom `paintEvent` that isn't calling the base class's
  clear/fill, or a widget being stacked twice in the same layout cell.

**Next step:** needs an owner with a debugger attached to the live window (or at minimum,
a controlled `QTimer`-driven repro script) — this is exactly the "needs hands-on UI
testing on Windows" category Task D flagged for `docs/WORKORDER` items, and it now has a
concrete, reliable repro instead of a guess.

---

## ✅ RESOLVED: Test Collection Was Broken (33 errors, 0 tests ran)

**Fixed by:** Claude Code, 2026-09-04. Verified with
`.\venv\Scripts\python.exe -m pytest tests -q -m "not jvm" --collect-only`
(33 errors / exit 2 → **0 errors / exit 0**).

**Correction to the original diagnosis: there were only two independent root
causes, not three.** "Bug 2" below was a misdiagnosis — it never existed as a
separate defect.

### Real bug 1: `.svg` claimed by both `ocr` and `plaintext`

```
ValueError: '.svg' is already handled by 'ocr'; 'plaintext' cannot also claim it
```

This was the root cause of **32 of the 33 errors**, not just one. Here's the
mechanism, because it's worth understanding before it happens again: `plaintext.py`'s
module-level `register(PlainTextExtractor())` call raises this `ValueError`
partway through iterating its extension set — but only *after* it has already
written several other extensions into the shared `REGISTRY` dict. Because the
import raised, Python drops `plaintext.py` out of `sys.modules`, so *every
subsequent test file* that imports `app.extract` re-executes the module from
scratch: a **new** `PlainTextExtractor()` instance registers again, collides
with the entries the first (failed) instance already left in `REGISTRY` under
a different object identity, and raises immediately — on whatever extension
happens to be first in iteration order. That surfaced as 31 unrelated-looking
`ValueError: '.t' is already handled by 'plaintext'` errors scattered across
totally unrelated test files (`test_walker.py`, `test_wildcards.py`,
`test_write_batching.py`, etc.) — **not** a `.vcxproj` duplicate.

**Fix:** removed `.svg` from `source_types.py`'s `DOCS_AND_DATA` (plaintext's
list). `config/extractors.toml` and `ocr.py` already declare `ocr` as `.svg`'s
intended owner (from the HEIC/HEIF/SVG media-ladder work), so plaintext's copy
was the stale one.

**There was no `.vcxproj` duplicate-registration bug.** `.vcxproj` is
registered exactly once, only in `source_types.py`. Fixing bug 1 alone
collapsed all 32 of these errors to zero — confirmed by re-running
`--collect-only` after the single-line fix.

### Real bug 2: `test_splash.py` uses unregistered `qt` marker

```
Failed: 'qt' not found in `markers` configuration option
```

`@pytest.mark.qt` in `tests/unit/test_splash.py` wasn't registered under
`[tool.pytest.ini_options] markers` in `pyproject.toml`, and `--strict-markers`
turns that into a hard collection error. **Fix:** registered the marker.

### Full-suite run after both fixes

`.\venv\Scripts\python.exe -m pytest tests -q -m "not jvm" --tb=short`
(collection succeeds; approximate counts from the run, since a **separate,
pre-existing environmental issue** — see below — prevented pytest from
printing its own final summary line):

- **~4,640 passed**
- **35 failed**
- **~1,119 errors**
- **~39 skipped**

**The ~1,119 errors are a different, pre-existing problem — not something these
two fixes touch, and not something to chase as part of this task.** They are
all `PermissionError: [WinError 5] Access is denied: '...\.pytest_tmp'`,
raised during test setup/teardown when pytest tries to manage its temp-file
junction. This is the exact failure mode `pyproject.toml`'s own comment
already documents (Windows Developer Mode / antivirus controlled-folder-access
blocking junction creation/resolution) — it was simply invisible before
because collection never got far enough to reach it. It also swallowed
pytest's final `X passed, Y failed, Z errors in Ns` summary line, hence the
counts above are derived from the progress-dot line rather than quoted
verbatim.

**The 35 genuine failures are unrelated to collection** (docs-versioning
checks on `ACTIVE_WORK.md`'s own header, `test_splash.py` timing/type
assertions, `test_window_state.py`, `test_ocr_ladder.py`, etc.) — pre-existing
issues for whoever owns those areas, not part of this fix.

**Next owner of the environment issue:** whoever picks this up should either
run pytest with Developer Mode enabled, or reconfigure `basetemp` off
`D:\SearchProject\.pytest_tmp` and onto a location the controlled-folder-access
policy already exempts — worth its own task rather than folding into this one.

---

## ⚠️ Tracker reconciliation, 2026-09-04: the Task A/B/C/D status above (now removed) was wrong

The per-agent tracking that used to live below this line (Task A/B/C/D, "Round 2
Assignments") was cross-checked against three independent sources — each order's own
`docs/WORKORDER-*.md` checkbox count, `docs/ORDER_REGISTER.md`, and `git log`/`CHANGELOG.md`
— using two full investigation passes. **The three independent sources agree with each
other almost exactly, everywhere. The Task A/B/C/D claims are the only numbers that
disagree with all three, and they disagree in four different ways:**

- **Overstated-but-real** (0f, 0g): real, working code exists, but it's **uncommitted**, its
  own work-order file has every box still unticked, and nothing is in `CHANGELOG.md`.
  "7/17" and "✅ complete" were never true completions - they described code sitting in the
  working tree.
- **Mislabelled** (0r): Task C's "0r (4/18)" entry actually listed 0p's work
  (sorting/alignment/window-state) - real, and separately already shipped in commit
  `9019956` - under the wrong order number. The actual 0r artifact (the splash screen) sat
  at 0 ticked items, undiscovered until the owner reported it visibly broken today.
- **Miscounted** (0s): Task D's own 7-item scheme undercounts the order's real 17 checkboxes
  and misclassifies item 6a (spreadsheet cell locators) as UI-blocked, when the work-order
  file shows it shipped with measured performance numbers.
- **Silently broken despite being marked done** (0p §4): window-state save/restore is
  claimed "✅" and wired end-to-end, but has a type bug that makes it a silent no-op every
  time (detailed below) - "wired" was mistaken for "working."

**Git history was the tiebreaker in every case**: every genuinely shipped item has a
matching commit title; every item claimed as newly done that the work-order file shows
unticked has zero matching commit, zero CHANGELOG line, and its source sitting in
`git status` as uncommitted. Going forward, **`docs/WORKORDER-*.md` checkbox counts are the
source of truth for what's done** - a per-agent session summary is a claim to verify
against that file, not a replacement for it.

Also worth a decision from the owner: the roadmap you gave says 34 orders; `ORDER_REGISTER.md`'s
own text implies 42; there are 43 `WORKORDER-*.md` files on disk (two of which,
`WORKORDER-CONVENTIONS.md` and `WORKORDER-0q-SESSION-2-HANDOFF.md`, aren't orders at all).
None of these three counts match. Doesn't change any order's individual status above, but
someone should pick one number as canonical.

---

## Reconciled status per order (roadmap fraction | file's own count | verified true state)

Every fraction below is the work-order file's own checkbox count, hand-verified against
`git log` for anything claimed shipped. Where this differs from what a Task A/B/C/D entry
used to claim, that's called out.

| Order | Fraction | True state |
|---|---|---|
| **0** review remediation | 52/6 | Done modulo 6 correctly-open items (structural splits, one deferred schema item, one rerank-quality decision). Unblocks everything else - cleared. |
| **0a** slash menu | 25/0 | **Fully done.** (`ORDER_REGISTER.md`'s own `Status: ACTIVE` field is stale - a labelling bug in the register, not in the feature.) |
| **0b** index tuning | 31/9 | In progress, matches file exactly. Open items are real (GPU install flag, first-run bench, several speed-work items). |
| **0c** search experience | 23/3 | In progress, matches file exactly. Blocked on 0b per the roadmap; 0b is not yet fully ticked. |
| **0d** privacy defaults | 9/0 | **Fully done.** |
| **0e** workspace features | 16/14 | Matches file; §1-§3a git-confirmed (commits `b1fdd7c`/`ec40c40`/`d86b017`/`aa9fb28`). §3b-d and §4-5 correctly open. |
| **0f** media/OCR ladder | 0/17 | **Not "7/17."** Real, uncommitted code: `exif.py`/`raw.py` are genuinely wired into the pipeline and pass their tests. `ocr_ladder.py` is **dead code** - fully written but never called from anywhere outside its own test - and has an internal bug independent of that (see Concrete Bugs below). |
| **0g** mbox/Takeout/chats | 0/7 | **Not "✅ complete."** Wired in, integration test passes (5/5), but 3/13 unit tests fail on real edge cases (see Concrete Bugs below). |
| **0h-0l, 0n** picture stack, offline media, reports | 0/13-17 each | Not started, correctly. |
| **0m** test automation | 0/18 | Correctly untouched (HELD by owner, after 0n). |
| **0p** table sorting | 11/6 | §1-3 (11 items) done, git-confirmed (`9019956`). **§4 (window state, claimed "✅") is unticked in the file and is genuinely broken** - see Concrete Bugs below. §5 (column widths) correctly open. |
| **0q** results presentation | 0/25 | Not "6/25" as a clean win - 6 items are genuinely implemented (chevron, left-elide, terminator, pixel-scroll, a11y, sentence-snap), but **the sentence-boundary snap fails its own new test** (see Concrete Bugs below). File itself still shows 0/25 ticked. |
| **0r** splash & fast lifecycle | 0/18 | The mislabelled order (see above). Real artifact (`app/ui/splash.py`) exists, was crashing every startup, now fixed and verified by Claude Code today (see the RESOLVED section above) - but zero of its 18 checklist items are formally ticked yet, including the "measured numbers recorded" requirement the work order calls load-bearing. |
| **0s** seven adoptions | 12/5 | Matches file exactly, including item 6a (spreadsheet cell locators) genuinely shipped with measured perf numbers - **not** blocked as previously claimed. |
| **pages reorg** (draft) | 0/13 | Promotion condition is "0b + 0c fully ticked" - **neither is** (9 and 3 items still open respectively), so this is not yet eligible despite one investigation pass suggesting otherwise. Owner should re-check before promoting. |
| **install/distribution** (draft) | 0/0 | 5 `[FINALISE]` decisions still open, 2 partially informed by a note in the privacy-defaults order. |
| **PySide6 migration** (draft) | 0/10 | Correctly untouched - after all feature orders. |
| **video/audio** (draft) | 0/4 | Correctly untouched - after picture stack. |
| **chat tab** (held) | 0/26 | Correctly untouched. |
| **terabyte-scale** | 16/6 | Matches file; 6 open items need a real corpus/long run (owner-only). New §6f code (FTS trigger drop/restore for bulk indexing) exists uncommitted with **zero test coverage** - see Concrete Bugs below. |
| **owner-pst-scale-run** | 0/4 | Matches file; unstarted, owner-only. |
| **PARKED** (OCR-where-it-pays, Code tab, git sharpness/mail preview) | — | All match the register; no discrepancies. |

---

## Concrete bugs found during reconciliation, blocking real completion (not yet fixed)

These are real, verified defects in the uncommitted working-tree code - found by running
each area's own tests and reading the failing assertions, not assumed. None of these are
part of the two bug fixes already shipped above (window-repaint, splash). Listed in rough
priority order:

1. **Window-geometry save/restore is a silent no-op, every time (Order 0p §4).**
   `app/ui/window_state.py::save_window_state()` returns a PyQt6 `QByteArray`, not `bytes`
   as its own signature claims. `shell.py` persists it via `SqliteStore.set_states()`,
   which calls `str()` on every value before writing to a SQLite TEXT column - and
   `str(QByteArray(...))` produces a Python repr string, not the underlying bytes. On next
   launch, `shell.py`'s `isinstance(saved_geometry, bytes)` check reads back that mangled
   string, fails silently, and skips restoration - nobody ever sees an error, the window
   just never remembers where it was. 7/10 tests in `test_window_state.py`/
   `test_window_opens.py` fail and directly evidence this. **Fix shape:** store/read actual
   bytes (`bytes(qbytearray)` before writing, wrap on read), or store via a binary-safe
   path instead of `str()`.

2. **`app/extract/ocr_ladder.py` is fully written but never integrated, and has its own
   bug regardless (Order 0f).** Nothing outside its own test file calls `route()` -
   `ocr.py`/`raw.py` do their own OCR without consulting it. Its own docstring in
   `ACTIVE_WORK.md`'s prior (now-corrected) entry already admitted this: "used via
   `route(Path)` in future work." Separately, `_metadata_decision()` returns
   `RouteDecision.METADATA_ROUTE` as an "ambiguous, keep going" sentinel for
   camera-default filenames, but `route()`'s early-return only short-circuits for
   decisions *other than* `METADATA_ROUTE` - so camera-default files fall through to
   the expensive detection rung instead of stopping early, the reverse of what its own
   tests expect. 3/12 tests fail.

3. **`email_mbox.py` has real edge-case gaps (Order 0g)**, despite a session-close note
   elsewhere claiming "7/7 COMPLETE... ready to merge": `.mbox.bak` files aren't recognized
   by `supports()`; a message with no `Subject:` stores `None` instead of `""`; a missing
   file raises a raw `mailbox.NoSuchMailboxError` instead of going through the app's
   `AppErrorException`/`guard()` error contract - a direct violation of this project's own
   "nothing fails silently" doctrine. 3/13 unit tests fail (the integration test, which
   only exercises the happy path, does pass).

4. **`presenter.py`'s sentence-boundary snippet snap fails its own test (Order 0q, item
   1b-1c)** - the one item previously called "production-ready and tested." A snippet
   test expects a snap to a clean sentence start ("Second sentence...") but gets one
   starting mid-word instead. Off-by-something in the boundary search.

5. **`terabyte-scale` §6f's new FTS-trigger drop/restore code has zero test coverage.**
   `drop_fts_triggers`/`restore_fts_triggers`/`check_and_rebuild_fts_if_dirty` in
   `app/index/pipeline.py`/`sqlite_store.py` look carefully ordered on read (dirty-flag
   before drop), but this is data-integrity-critical code with no tests at all -
   the highest-risk untested code in the current working tree, precisely because a bug
   here could leave the FTS index silently stale rather than failing loudly.

6. **`run_tests.py` (untracked, root of the repo) is broken and should not be committed
   as-is** - it hardcodes a Linux Cowork-sandbox path (`/sessions/funny-tender-shannon/...`)
   and crashes immediately (`NotADirectoryError`) on this Windows machine. Looks like a
   stray file from a different agent's session that landed in the wrong checkout.

7. **Minor:** `requirements.txt`'s new comment claims rawpy is "pure Python with no C
   extension on Windows" - almost certainly wrong (rawpy wraps the LibRaw C library via
   Cython) and worth fixing before it misleads an install-time decision.

8. **Not bugs, just noise:** `GitSearch.txt`, `docs/CODE_REVIEW.md`, `run-install.cmd`,
   `Leasha.sln`, `Leasha.pyproj` show large `git diff --stat` counts but zero real change
   under `--ignore-space-at-eol` - pure CRLF/LF churn, safe to normalize separately from
   any of the above.

---

## Execution plan, respecting the owner's dependency graph and the state above

The owner's original sequencing is sound and unchanged; what changes is which orders are
actually clear to build on top of, given the bugs above.

**Before anything else touches this working tree:** decide what to do with the 6 concrete
bugs above. They block *closing out* 0f, 0g, 0p, 0q even though those orders have real
progress - none of them block *starting* new work, since they're isolated to their own
files. Recommended: fix 1-4 (small, scoped, each independently testable) before ticking
any checkbox in their respective work-order files; discard 6 (`run_tests.py`) rather than
fix it; decide on 5 (add tests) and 7 (doc correction) opportunistically.

**Phase 1 (unchanged from the owner's list, now status-corrected):**
1. Order 0 - already clears the path; no action needed.
2. Order 0a - already fully done; no action needed.
3. Order 0b (31/9 open) - continue; nothing above blocks it.
4. Order 0c (23/3 open) - continue after 0b; nothing above blocks it.
5. Order 0d - already fully done; no action needed.
6. Order 0e (16/14, §4-5 open) - continue after 0c.
7. Orders 0p, 0q, 0r, 0s - gap-schedulable, but **0p and 0q specifically should get their
   concrete bugs (1, 4 above) fixed before their checkboxes are ticked**, and **0r's 18
   items are all still open** despite today's crash-fixing - ticking them (and recording
   the "load-bearing" startup/close timing numbers the work order calls for) is real
   remaining work, not cleanup.

**Phase 1, after 0e:**
8. Order 0f - **do not build 0h/0i/0j on top of it yet.** Fix bug #2 above (wire
   `ocr_ladder.route()` in, fix the `METADATA_ROUTE` short-circuit) first, since 0f is
   explicitly the foundation for the picture stack and currently ships a scaffold with a
   logic bug, not a working ladder.
9. Order 0g - fix bug #3 above before treating it as mergeable; the integration test
   passing masked three real edge-case gaps.
10. Orders 0h→0i→0j→0k→0l→0n→0m - unchanged sequencing; 0k/0l's "no interleaving" note
    still applies.

**Phase 2-4 (pages reorg, install/distribution, PySide6, video/audio, chat tab, scale
runs):** unchanged from the owner's list - none of the bugs above touch these, and their
promotion/start conditions are correctly still unmet except pages-reorg's, which needs a
recheck (see reconciliation table above) rather than an assumption either way.

**On the 34-vs-42-vs-43 order-count mismatch:** worth the owner's five-minute decision
before it causes a real scheduling gap, but doesn't change anything above.

