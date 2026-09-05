# Work order (One thread): workspace features — pop-outs, viewers, and the tools around search

**Doc version:** 1.3 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (UI + preview loader + extract/converter + install docs)
**Status:** RELEASED by the owner 2026-08-27 (registered in HANDOFF.md §"What
is Next") — sequenced after `WORKORDER-202626270157-search-experience.md`.
This is the full stack, owner's instruction: every item below is in scope.

The theme, worth stating because it decides many small choices: these features
turn Leasha from *a window you visit* into *a workspace you arrange* — a tool
that sits beside real work all day. Every window this order creates is
view-only: **nothing in it may ever write into a user's file.** Rotation
state, cached renders and window geometry live in the app's own state, never
in the document.

## 1. The log, made actionable

- [x] **1a Colours by level**: normal lines unchanged, warnings in the theme's
  warn colour (`#statWarn`), errors in the theme's danger colour — **theme
  tokens only, never hardcoded hex** (a hardcoded red disappears on the dark
  theme), and the level word stays in the line: colour is never the only
  signal (the accessible-names philosophy, applied to colour).
- [x] **1b Clickable errors**: an error line naming a file or skip reason
  jumps to that file's skip entry / row. A log you can act on, not just read.
- [x] **1c Pop-out**: the log opens as its own top-level window — geometry
  remembered, keeps updating while the main window is minimised to tray,
  closing it returns nothing to re-wire (the in-app log never left).
- [x] **1d Stay-on-top**: a toggle on the popped-out log
  (`WindowStaysOnTopHint`), remembered. The use case is watching a long
  index run while working in another app.

## 2. Pop-out preview windows (the owner's design: a COPY, not a move)

The in-app preview pane stays exactly where it is. "Pop out" opens an
independent, view-only window showing the same document. Multiples are
allowed and expected — compare falls out for free.

- [x] **2a** the window: title bar = filename, tooltip = full path, size
  remembered, stay-on-top toggle (same control as 1d). Each pop-out owns its
  own load through the existing preview worker with **its own generation
  stamp** — a new search in the main window must never blank a pinned
  document; that is the opposite of why it was pinned.
- [x] **2b Find**: Ctrl+F inside the window — highlights, next/previous,
  match count. (Also wire the same find into the in-app preview pane; it is
  the last metre of every search.)
- [x] **2c Print**: prints what is shown, at the shown rotation. Windows'
  print-to-PDF makes this an export feature for free — no separate export is
  built.
- [x] **2d Zoom**: Ctrl+wheel and fit-width for images and PDFs; font-size
  bump for the text kinds. Standard behaviour, nothing custom.
- [x] **2e Rotate**: one button cycling 90°, whole document, images AND PDFs.
  Implementation rides the M11 pattern: pages/images render to `QImage` on
  the worker, so rotation and zoom are two parameters of one render call —
  one pipeline for both kinds, no UI-thread decoding. This replaces
  `QPdfView` in the pop-out with a rendered-page view (PyMuPDF, already in
  the stack, or `QPdfDocument.render` — thread's choice; PDF text selection
  may be scoped out initially and noted). Rotation is remembered **per file
  in app state** — the sideways scan rotated once opens right-side-up
  forever — and the file itself is never touched.
- [x] **2f Copy text**: selectable/copyable text in the text kinds. View-only
  still means copy-out works.
- [x] **2g The exits**: "Open the real file" and "Show in folder" buttons
  (via the existing worker-routed open paths), and for text-rendered Office
  kinds one plain sentence in the window: "shown as text — open the file for
  full layout" (see 4e for the better answer).
- [x] **2h Mail**: a message pops out exactly like a file — pin the mail
  being answered while searching for what it mentions. Same window, no
  special casing beyond the synthetic-path load that already exists.

## 3. The workspace around results

- [x] **3a Global-hotkey mini-search**: a system-wide shortcut (default
  chosen by the thread, changeable in Settings, conflicts detected) opens a
  small stay-on-top search box from the tray — type, pick, Enter opens the
  document, box vanishes. It runs the Search-tab policy (the kid-safe
  surface from the search-experience order). This is the feature that makes
  the product demonstrable in ten seconds; treat its polish accordingly.
- [ ] **3b Drag out**: result rows drag as real files (`QDrag` with file
  URLs) into Explorer, email clients, anything. Mail results drag their
  attachment where one exists. Search becomes the source of files, not just
  the finder.
- [ ] **3c Pinned working set**: a small panel results can be pinned to
  *across* searches — gather from five queries, then act together: open
  all, copy all paths, drag all out. Cleared explicitly, never by a new
  search; survives the session via app state.
- [ ] **3d Timeline strip**: a thin band over results showing when hits
  cluster in time; click a cluster to filter to that period (it composes
  with the existing after:/before: machinery — no new query semantics).
  Off-able like every behaviour, per the owner's configurability rule.

## 4. Viewers — closing the preview gaps with free pieces

- [x] **4a Qt one-liners**: SVG via QtSvg; `.tif/.tiff` added to the image
  suffixes (scanner output — verify against what the indexer already
  accepts); Markdown rendered via `QTextDocument.setMarkdown` instead of raw
  text.
- [x] **4b Spreadsheets as tables**: `.xlsx` (and `.xls` via the existing
  converter route) preview becomes a real grid — `QTableView`, sheet tabs,
  read from `openpyxl` (already a dependency) on the worker, row/column cap
  with a "large sheet — showing first N rows" line. The biggest preview
  upgrade in this order: a spreadsheet that looks like a spreadsheet.
- [x] **4c EPUB**: chapter list + the existing HTML renderer; it is a zip of
  HTML and both halves already exist. No new dependency.
- [ ] **4d HEIC/HEIF**: decode via `pillow-heif` (permissive licence) so
  phone photos preview; extend the image pipeline, not a new kind.
- [ ] **4e Office full layout, on demand**: a "Show full layout" button on
  text-rendered Office/ODF previews converts the document to PDF through
  the **existing LibreOffice converter route**, caches the PDF beside the
  index keyed by content hash, and displays it in the PDF path — rotate,
  zoom, print included. On-demand and cached: never at index time, paid
  once per document a user actually opens. Button hidden (with the 2g
  sentence pointing at installing LibreOffice) when no converter is
  detected — the `format_health` pattern.

## 5. DWG drawings (owner decided 2026-08-27: the user-installs-it path)

- [x] **5a Indexing**: wire `.dwg` through the Tier-2 converter route that
  `cad.py`'s own docstring designed — `dwg2dxf` (LibreDWG) **or** the ODA
  File Converter, both in the converter allow-list, detected via
  `resolve_binary`, whichever the user installed feeds the existing `ezdxf`
  extractor. **Subprocess only, never LibreDWG's Python bindings** —
  invoking a GPL tool is mere aggregation; linking it would GPL the
  application, which the owner has expressly kept open. Nothing is bundled:
  the user installs the tool, so Leasha carries zero licence obligations.
  Convert-failed falls back to the existing `dwg_release` header line.
- [x] **5b 'What to install' surfacing**: Settings/doctor say, in plain
  words, "DWG drawings: install LibreDWG or the ODA File Converter to read
  these" with the count of `.dwg` files waiting — the existing
  format-health machinery, one new row.
- [ ] **5c Preview** (lower priority — do last in this order): `dwg2SVG` →
  cached SVG → the QtSvg path from 4a, labelled "simplified view".
  Line-work and text, not plot fidelity; for "is this the right drawing?"
  that is enough.

## 6. Rules that bound all of it

* View-only everywhere: no code path in this order writes to a user file.
* Every new behaviour ships on and individually off-able (the owner's
  configurability rule); every new control's tooltip states its effect (the
  search-experience order's §6a test will enforce it — coordinate, don't
  duplicate).
* Existing labels never change; plain words on anything a Defaults/Auto user
  sees.
* No work on a keystroke or UI-thread I/O: all rendering through workers
  with generation stamps, per the M11 pattern and the `test_ui_never_blocks`
  scanner, which must be taught any new module names.
* External tools: detected, never bundled; allow-listed in code; timeout and
  temp hygiene per the existing converter contract.

## 7. Tests

- [x] log: level→token mapping (both themes), clickable error routing,
  pop-out update-while-minimised.
- [x] pop-outs: independent generation stamps (main-window search does not
  touch a pinned window — the regression test for 2a's whole point);
  rotation remembered per file and absent from the file itself (bytes
  unchanged, asserted); print honours rotation.
- [ ] viewers: each new kind routed correctly; spreadsheet cap line; full-
  layout cache hit on second open; suffix sets consistent between indexer
  and preview (the `.tiff` class of drift, as a test).
- [x] mini-search: hotkey conflict fallback; opens/dispatches/closes without
  the main window shown; runs the Search-tab policy.
- [ ] drag-out: a dropped file lands as a real path; pinned set survives a
  restart.
- [x] DWG: allow-list entries; either converter feeds `ezdxf`; bindings
  import is *forbidden by test* (a guard asserting no `libredwg` import
  anywhere — the licence rule as code).

## Done means

Each ticked item: change + tests + `pytest tests -q` green + committed by
name; CHANGELOG under `[Unreleased]` per user-visible feature. Windows smoke
test for the window-management items (pop-outs, stay-on-top, hotkey) — Qt
window flags are exactly where Linux CI lies about Windows. The order is done
when a user can: watch a colour-coded log in a pinned window during a run,
pin three documents and a mail side by side, rotate a sideways scan once and
never again, see a spreadsheet as a grid, summon search from anywhere with a
hotkey, and drag the found file straight into an email.

## Note on §1, added 2026-08-28

**A `danger` token had to exist first.** The palette had `warning` and nothing
below it, so an error and a warning were the same colour — and a log whose two
not-fine states look identical cannot answer the question it is open to
answer. Both palettes gained one, with different values, which is the reason
the palette is two dictionaries rather than one with a flag. Muted rather than
pillar-box: this sits in a wall of 12px text, where a saturated red vibrates
against a dark ground.

**Three buckets, not seven.** loguru has seven levels; a log rendering five of
them in five colours is *harder* to scan than one rendered in none, because
the eye is hunting the lines that are not fine. `LEVELS` maps everything to
normal / warn / error and a test pins that it stays three.

**The level word stays in the line**, per §1a's own instruction and this
codebase's standing rule. Asserted, because "colour is never the only signal"
is the kind of thing that is true when written and quietly stops being true.

**A defect found while writing §1b, and it decided the regular expression.**
The first path matcher stopped at the first space — so
`C:\Users\jay\My Docs\a.docx`, the shape of most real paths on Windows,
matched nothing at all. Now non-greedy up to the first extension followed by a
boundary, which also handles `//server/share/site plan.dwg`. Four shapes are
in the test table.

**Only warnings and errors that name a file are clickable**, and that is a
decision rather than an oversight: an underlined `INFO` that goes nowhere
teaches people that none of the underlined things go anywhere. An extension is
required too — a folder is not a file, and a click that opened the wrong thing
is worse than a line that does not respond. **Double-click**, not single,
because single click is how somebody selects a line to copy it, which is the
first thing anyone does with a line that looks wrong.

**The pop-out is a copy, exactly as §2 requires of previews.** `LogWindow`
builds its own `DebugPane`; nothing re-parents the one in Settings, and a test
greps for `setParent`/`takeWidget` to keep it that way. It is **not parented**
to the main window either — a parented `QWidget` with a window flag is still
owned by its parent, so minimising the main window would take the log with it,
which is the one thing §1c exists to prevent. It keeps polling because the
pane starts its own timer on `showEvent`, and nothing hides it when the main
window hides.

**`show()` after `setWindowFlag` is not optional**: changing a window flag
re-creates the native window and Qt leaves the new one hidden, so a
stay-on-top toggle without it makes the log vanish — which reads as the
checkbox having closed it.

**Geometry is four integers, not `saveGeometry()`'s blob.** Somebody working
out why a window opened off-screen has to be able to read `index_state` and
see where it thinks it is. A remembered size below the floor, or from a screen
no longer attached, is discarded rather than used.

**The pop-out button lives on the pane, not in Settings** — partly because it
belongs beside the thing it pops out, and partly because `settings_view.py`
was at 249 of the 250 lines its guard allows. The guard doing its job again.

**And the suite caught one of mine.** `_remember_log_window` locally imported
`CallableWorker`, which the module already imports;
`test_no_function_reimports_a_name_the_module_already_has` refuses that,
because a local import of a module-level name makes it local to the *whole*
function and turns any earlier use into `UnboundLocalError`.

Not verified on Windows: window flags are exactly where Linux lies, as the
order's own "Done means" says. Worth one look at stay-on-top and at the log
still updating while the main window is in the tray.

## Note on §2, added 2026-08-28

Delivered in two commits: the pieces first, then the window.

**One render call, and it is what makes rotate and zoom parameters rather
than features.** `render_page.render()` turns an image *or* a PDF page into a
`QImage`; everything after it — rotating, scaling, printing, drawing — treats
the two identically. `QImage` and never `QPixmap`, because Qt refuses to build
a pixmap off the interface thread and crashes on some platforms rather than
refusing. PyMuPDF, imported as `pymupdf` rather than the legacy `fitz` alias.
**The cost is PDF text selection in the pop-out**, which the item explicitly
allows scoping out; the in-app pane keeps `QPdfView` and keeps selection with
it, so nothing anybody had is lost.

**Two bugs of my own, both found by measuring rather than reading.**
`_shape` scaled only when fitting, so an image at 2× rendered at 100% — a
zoom control that visibly does nothing. And the find counter keyed off the
match index, which is −1 for a fresh search, so typing a word that is *not* in
the document said nothing at all; a find box that goes silent is one somebody
presses harder. Both are covered by name.

**Rotation is remembered per file, keyed by a hash of the path.** Not
tidiness: `index_state` is a table somebody may open, and a list of every
document they have ever rotated — with its full path — is not a thing to leave
on a shared machine. The privacy order's reasoning one level down. **Zoom is
deliberately not remembered**: it is something you do to look closer at one
passage, and a file reopening at 400% because of something done in March reads
as broken.

**The generation stamp is per window, and that is 2a's whole point.** A search
in the main window must never blank a pinned document. Two windows, two
counters, and a test asserts one moving does not move the other — because a
shared counter is exactly how this breaks the first time somebody tidies it
into "one place".

**View-only asserted on the bytes.** The rotation test reads the file before
and after and compares them, and a grep guard refuses `write_text`,
`write_bytes`, `shutil` and `"w"` anywhere in the window.

**§2g's sentence is a flag, not a parsed notice.** `_extracted` now sets
`meta["extracted"]`, because the rule this codebase set for notices — nothing
reads a message string to decide anything — applies here too. A `.txt` shown
as text has no layout to be missing; a `.docx` does.

**Print has no export beside it**, per §2c: Windows' print-to-PDF is the
export, and a second code path that wrote a file is the one thing §6 forbids.

**Mail needed no special casing**, as §2h predicted — but the signal carries
two things rather than one. A mail row has no file on disk, so the pane's
`body_provider` travels with the row; that is the whole of it.

**A wiring bug the sequence caught.** The loop connecting all four preview
panes was first placed beside the log wiring — three views before those views
exist. It would have been an `AttributeError` on the first window open.

**A weak assertion caught while testing**: `not isVisible()` passes for any
widget whose ancestors are not shown, so it would have passed over a find bar
that never hides. `isHidden()` is the question actually being asked.

Not verified on Windows, and the order says this is where Linux lies: window
flags (stay-on-top), the print dialog, and Ctrl+wheel over a real trackpad.

## Note on §3a, added 2026-08-28

**Qt has no global hotkey, and that is not an oversight** — a shortcut that
fires while another application has focus is an operating-system service, not
a widget one. Windows offers `RegisterHotKey`, reached through **ctypes rather
than pywin32**, for the reason `single_instance.py` records: pywin32 is
optional, needed only for PST ingestion, and the feature the whole product is
demonstrated with must not depend on an optional package.

**A conflict is reported, never swallowed.** `RegisterHotKey` failing because
something else owns the combination is the single most likely thing to happen
on a real machine, and a shortcut that silently does not work is
indistinguishable from a broken application. Settings carries a plain sentence
under the box saying whether the operating system granted it.

**`MOD_NOREPEAT`, and it is not a detail.** Without it, holding the
combination fires dozens of times a second and each one opens the box and
takes focus — a keyboard held a moment too long becomes a machine that cannot
be typed on.

**`Ctrl+Alt+L`, and the letter is the point.** The obvious candidates are all
taken: `Ctrl+Space` is the IME switch on any machine with a second keyboard
layout, `Win+S` is Windows' own search, `Ctrl+Shift+F` is find-in-files in
every editor a developer has open. Being wrong here costs one trip to Settings
rather than a shortcut that fights something else all day.

**One canonical spelling.** `Alt+Ctrl+L` and `Ctrl+Alt+L` are one shortcut,
and two spellings in Settings would be two rows nobody can tell apart — the
same reasoning saved searches needed for names.

**The box runs the Search tab's policy by name**, per §3a: somebody who
summoned it from inside Excel is the least likely person to be in the mood to
debug a query. Seven rows, a deliberate ceiling rather than a screenful — this
is *"the thing I was thinking of, now"*, and a list long enough to scroll is
one somebody reads instead of recognises. A row says what it is and where it
lives and nothing else; a snippet would make it three lines tall and turn a
recogniser into a reader.

Escape closes it, clicking away closes it, choosing closes it, and it holds
nothing afterwards — a box that reopened showing the last search would be
showing somebody else's question on a machine anyone might walk past. Enter
with nothing found hands the query to the main window instead of doing
nothing: somebody who pressed Enter meant something to happen.

**A third instance of one recurring mistake, recorded because it keeps
happening.** A guard grepping for `"win32"` matched the module's own
explanation of why *not* to use pywin32 — exactly as the `HKEY_LOCAL_MACHINE`
guard did in the deep-link order, and the `chunk_by_id` one before that. The
rule that comes out of it: **grep for what the code would do (`import win32`),
never for the name its own justification contains.**

Not verified on Windows, and this is the item where that matters most: the
registration, the conflict path and the `WM_HOTKEY` filter are all Windows,
and Linux cannot say anything about them. Worth pressing the combination once
with Leasha minimised to the tray.

## Note on §5, added 2026-09-05

**5a was already built.** `cad.py`, `converter.py`'s allow-list and
`config/extractors.toml`'s `[converters.".dwg"]` route were all written on
2026-08-25 by `9623c58 feat(extract): read AutoCAD drawings` — two days
before this order even existed. `test_cad.py` already asserted the route
exists, targets `cad`, and stays off the extractor registry so it cannot
shadow the converter. What was missing was the one thing §7 asks for by
name and no earlier commit had reason to write: a guard that the licence
rule is enforced as code, not just as a comment. Added —
`test_no_module_imports_libredwgs_python_bindings` in `test_cad.py`, an
AST walk over every module under `app/` refusing any `import` whose module
name contains `libredwg`. It passes today because nothing does; its job is
to keep that true.

**5b: the message already existed, generically, and undersold the drawing's
own case.** `.dwg`'s row was already produced by `format_health.py`'s
ordinary converter path — but that path is written for a converter with
exactly one route, and says so: *"'dwg2dxf' was not found..."*. `.dwg` has
two allow-listed routes, and a message naming only the one that happens to
be configured in `extractors.toml` reads as though the other were not an
option, when the owner's own instruction is "install LibreDWG **or** the
ODA File Converter". `_converter_status` in `format_health.py` now
special-cases `.dwg` to say exactly that, in those words, before falling
back to the generic single-binary message for everything else.

**The count needed a source, and the one honest source is the index
itself.** `format_health()` gained an optional `counts` parameter —
`{".dwg": 40}` — folded into the row's detail when a caller has it, absent
without complaint when a caller does not. `doctor.py` is the caller that
does: `_pending_counts_by_ext()` is one `GROUP BY ext` over the `files`
table already the authority per non-negotiable 6, guarded to read
`.env`/`load_settings` failing, no index built yet, or a corrupt db file as
"nothing to report" rather than a crashed check. **Settings does not get
the count** — `app/ui/widgets/file_types.py` is the file that would wire it
through, and it is outside this order's touchable-files list. Settings
does still get the corrected plain-words message, because both consumers
already share the same `format_health()` call; only the number is
doctor-only until a session with UI in scope wires `file_types.py` to pass
its own counts.

**5c is not attempted, and re-checked after 4a landed mid-session.**
`app/ui/preview_loader.py` was read again once `1caa388 Workspace §4a: SVG,
.heic/.heif and .tif/.tiff as images, Markdown rendered` appeared on this
branch, in case it had closed the gap. It has not: 4a's own commit message
says plainly *"SVG needed no new code: this build's Qt already has the qsvg
imageformat plugin, so `QImage(path)` decodes an `.svg` exactly like any
raster image"* — `.svg` was simply added to `_IMAGE_SUFFIXES` and decoded
through the existing `QImage`/`QPixmap` pipeline as `KIND_IMAGE`. There is no
`KIND_SVG`, no `QtSvg`/`QSvgWidget` import, and nothing that treats an SVG as
a cacheable render target rather than a file Qt happens to open — confirmed
by `grep`, not inferred from the commit message alone. §5c's own text names
"the QtSvg path from 4a"; what 4a actually built is a QImage path, which is
a different (and narrower) thing to hang a cached render on. Regardless of
which shape 4a took, the wiring — recognising a `.dwg`, finding or building
its cached SVG, and handing it to whichever preview path exists — lives in
`kind_for()`/`load_preview()` in `app/ui/preview_loader.py`, which stays on
this order's do-not-touch list for this thread. Building a parallel preview
mechanism here rather than touching that file would be exactly the
"duplicate mechanism" this order's own rules forbid. Left undone, checkbox
unticked; a session with `preview_loader.py` in scope should re-read this
note rather than assume 4a's QImage route is a drop-in fit for a *cached*
render the way a directly-openable image is.
