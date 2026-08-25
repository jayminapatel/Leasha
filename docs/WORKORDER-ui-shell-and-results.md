# Work order: results layout, preview pane, tray, and a responsive UI

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.2

**Supersedes `WORKORDER-results-layout.md`**, which covered only the result row. That work is
included here unchanged; this adds virtualised scrolling, an optional preview pane, the
application icon, minimise-to-tray, and one non-negotiable rule about threading.

Origin: the owner disliked how results were presented, and separately reviewed an
AI-generated UI specification. Most of that specification was rejected - the reasoning is in
§8, because "why didn't we do the thing in the spec" is a question that will be asked again.

Commit the working tree before starting.

---

## 1. The rule that outranks everything else here

**No background work may ever disable, freeze, or grey out the user interface.**

This is now a standing constraint, not a preference for this work order. Every long operation -
indexing, embedding, OCR, LibreOffice conversion, query translation, graph-free preview
rendering, `doctor`, diagnostics - runs off the UI thread and reports progress through Qt
signals. The window stays interactive throughout: you can search while indexing, change
settings while OCR runs, and close the window at any moment.

Specifically forbidden:

- `setEnabled(False)` on a panel to prevent interaction during work. Show progress; keep the
  control live. If an action genuinely cannot run yet, let it be pressed and explain why.
- A modal dialog that blocks the window while something is computed.
- `QApplication.processEvents()` used to "keep the UI alive" inside a loop. That is a symptom
  of work on the wrong thread, and it reenters the event loop in ways that produce bugs nobody
  can reproduce.
- `time.sleep`, blocking I/O, `subprocess.run`, or any store call on the UI thread.

`docs/TROUBLESHOOTING.md` currently tells the owner to *wait ten seconds* when the window goes
white, and that building the graph or running the environment check can do it. **That entry
should become untrue.** Delete it once this is done, and treat any remaining white-window as a
bug with a reproduction.

**Tests.** A guard test asserting that no module under `app/ui/` calls `processEvents`, and
that `presenter.py` still does not import Qt. For each long operation, a test that the worker
is started rather than executed inline - assert on the signal, not the result.

## 2. Results: the row

Six faults, in the order they cost the owner something.

### 2.1 One row per chunk, so one document fills the page

`results_view.py` renders chunk-level results with nothing grouping them, so a long PDF
matching in five places takes five of the top ten rows. Ten documents become three.

### 2.2 The path is the headline, and it is truncated

`shorten_path(..., limit=70)` elides the middle - exactly where a long archive path carries its
distinguishing part. People recognise `report_final_v3.pdf`, not `D:\Archive\2019\Projects\...`.
Browsers have this the right way round: title first, location small and grey beneath.

### 2.3 No date

In a fifteen-year archive with eight versions of everything, the date is often the only
distinguishing feature. `format_when()` already exists in `presenter.py`; it is used by
`FileRow` and never wired into `ResultRow`.

### 2.4 No type

Email, spreadsheet or scanned PDF changes how you read a result, and is currently inferable
only from an extension buried in a truncated path.

### 2.5 Rank numbers

`1. 2. 3.` restates the ordering and makes the list read like a printed report.

### 2.6 `explain` and score on every row, forever

`keyword and meaning both matched · score 0.83` is valuable - being able to ask "why is this
here" is where trust comes from. It is also the second thing the eye lands on, on every row,
for the life of the application. Move it to the tooltip and a **Why this result?** item in the
existing right-click menu. Do not delete it.

### The data is already fetched and thrown away

`keyword.py` selects `f.ext` and `f.mtime_ns` in both queries, and the LanceDB table carries
`ext` and `mtime_ns` columns. `SearchResult` has no fields for them.

**Add `ext: str = ""` and `mtime_ns: int = 0` to `SearchResult`**, populated from both
retrievers. No new queries, no schema change.

Email metadata needs a `messages` lookup. **Batch it** - one query for the `file_id`s on the
visible page. A per-row lookup is fifty queries per keystroke at debounce. `mail_rows()`
already formats sender, recipients and date; reuse it.

### Group in the presenter, never in the engine

`SearchEngine` keeps returning chunk-level results ranked exactly as today. Grouping is
display, and belongs in `presenter.py`.

Not a stylistic preference: the measured baseline in `HANDOFF.md` §3b was taken against
chunk-level ranking. Grouping inside the engine changes what "rank 1" means and makes every
future measurement incomparable with the one now on record.

```python
@dataclass(frozen=True, slots=True)
class ResultGroup:
    file_id: int
    name: str          # filename, or subject for a message
    folder: str        # breadcrumb, not a raw path
    kind: str          # "pdf" | "email" | "sheet" | ...
    when: str          # format_when()
    rows: list[ResultRow]
    best: ResultRow
```

Rank a group by its **best** chunk, never the mean - averaging punishes a long document that
matches strongly in one place, which is the common case in an archive of long reports.

Fetch deeper than you display: group the existing fused set, then show the top N *groups*.
Make the depth a named constant with a comment saying why it exceeds the display count.

### The row

```
[icon]  report_final_v3.pdf                          12 Mar 2019
        Archive > 2019 > Projects > Leeds          3 matches v
        ...identified three hazards at the pump station requiring...
```

Name 15px medium; date right-aligned muted 12px; folder as a breadcrumb with the full path in
the tooltip and in Copy path; match count only when above one, expanding to per-chunk rows each
openable at its own page; snippet from the best chunk with the existing highlighting.

A message substitutes `from Chris Bell · 2 attachments` for the folder line.

**Keep** the missing-file marking. A result whose file has vanished is a real finding.

## 3. Virtualised scrolling

`results_view.py` uses `QListWidget` with `setItemWidget` - a live widget per row, three
`QLabel`s each. Five hundred results is fifteen hundred widgets.

Move to **`QListView` with a `QStyledItemDelegate`**: rows are painted, not constructed. This
is the one genuinely good idea in the reviewed specification, and it must happen in the same
pass as the grouping work, because both rewrite the same file. Doing them separately means
rewriting it twice.

Expanded groups become additional rows in the model rather than nested widgets.

**Test:** ten thousand synthetic results render and scroll without the widget count growing
with the result count.

## 4. Preview pane - optional, and off by default

A right-hand pane showing the selected result without opening the owning application.

**No `QWebEngineView`.** It is a full Chromium: multi-process, GPU process, hundreds of
megabytes, and it would reverse the founding decision of V2 - one process, no services. Worse,
the reviewed specification proposed it for **HTML email**, which means remote image loading:
tracking pixels in fifteen-year-old marketing mail phoning home the moment a result is
previewed, silently breaking "nothing leaves the machine".

Use instead:

| Kind | Renderer |
|---|---|
| Text, markdown, code | `QTextBrowser`, read-only, `setOpenExternalLinks(False)` |
| HTML email | `QTextBrowser` in **`QTextDocument` rich-text mode**, with remote content stripped before display |
| PDF | Qt's native `QPdfView` + `QPdfDocument` |
| Images | `QLabel` with a scaled pixmap |
| Anything else | Name, size, date, and an Open button |

**Email HTML must be sanitised before it reaches the widget:** strip `<script>`, `<iframe>`,
`<object>`, `<link>`, `@import`, and every remote `src`/`href` for images. Show a discreet
"remote images blocked" line. `QTextBrowser` will not execute script, but it *will* fetch
remote images if asked, and that is the leak that matters.

**Performance, as requested:**

- **Off by default.** A toggle in the view menu and `Ctrl+P`, persisted through the existing
  `ViewPreferences`.
- Rendering happens **on a worker**, never on selection-change directly. Arrow-keying down a
  list must not stutter.
- **Debounce** selection by ~200ms, so holding the down arrow does not queue fifty renders.
- **Cap** what is rendered: a size ceiling per kind, and for PDFs render the matching page
  first rather than the document.
- **Cancel** superseded renders - if the selection moved on, drop the result rather than
  painting it.
- Failure is an `AppError` in the pane, never a crash and never a modal.

## 5. Application icon

Built from the supplied logo and already in `assets/`:

| File | Contents |
|---|---|
| `assets/leasha.ico` | 256, 128, 64, 48 with the full mark; 32, 24, 16 with the blobs alone |
| `assets/leasha-tray.ico` | 32, 24, 16, blobs only |
| `assets/leasha-256.png` | About dialog, docs |
| `assets/leasha-mark-64.png` | Small mark |
| `assets/leasha-source.png` | Cleaned full-resolution master, transparent |

**Why two variants.** Below 48px the navy ellipse becomes an indistinct dark mass that swamps
the three shapes; the blobs alone stay legible. Verified by rendering every size and looking at
them. The white background is transparent throughout, so the mark works on light and dark
taskbars.

Palette, for the theme: blue `#0777D9`, olive `#A1AF00`, orange `#FF9934`, navy `#14084A`.

Wire up: `QApplication.setWindowIcon(QIcon("assets/leasha.ico"))`, the tray icon from
`leasha-tray.ico`, and `--icon assets/leasha.ico` when packaging at L9.

## 6. Minimise to system tray

- `QSystemTrayIcon` using `leasha-tray.ico`.
- **A preference, not a default**: "Minimise to system tray" and "Close to tray", both off
  initially. An application that vanishes from the taskbar when you did not ask it to is
  alarming.
- Left-click or double-click restores; the menu offers Show, Search (restores and focuses the
  box), Indexing status, and Quit.
- **Quit from the tray must be a real quit** - release the single-instance lock, flush the
  database, stop workers. A tray icon that leaves a process holding the index lock produces
  `ERR_DB_LOCKED` on next launch with no visible cause.
- The tooltip shows indexing state, so the tray is informative while minimised.
- `QSystemTrayIcon.isSystemTrayAvailable()` may be false. Fall back to normal minimising and
  disable the preference with an explanation - never fail silently.

**Tests:** closing to tray keeps the single instance held; quitting from the tray releases it;
tray unavailable degrades to normal minimise.

## 7. Order

1. `ext` and `mtime_ns` through `SearchResult`. Nothing visible changes.
2. `ResultGroup` and grouping in `presenter.py`, with tests. Still nothing visible.
3. `QListView` + delegate, drawing groups. **The visible change.**
4. Expand and collapse; mail rows; view preferences.
5. Icon and tray.
6. Preview pane, off by default.
7. The threading audit in §1, and delete the "window has gone white" entry from
   `docs/TROUBLESHOOTING.md`.
8. **Open the window and use it.** Eleven UI faults so far, every one found by clicking.

## 8. Rejected from the reviewed UI specification, and why

Recorded so it is not re-proposed.

| Rejected | Why |
|---|---|
| `QWebEngineView` | Full Chromium; reverses the one-process decision; remote images in email leak |
| Frameless window, custom title bar | Reimplements Windows 11 snap layouts, Aero Snap, edge resize, per-monitor DPI. Gains ~30px and a look |
| `Alt+Space` global hotkey | That is the Windows system menu. Hijacking it is hostile |
| Workspaces tree ("AI contexts") | A new concept, data model, drag-drop and persistence that nobody asked for |
| "Top AI synthesis" result cards | That is L8b, deferred deliberately |
| RAM and CPU telemetry bars | `resources.py` already throttles for real. Bars invite anxiety and cost a permanent polling timer |
| Model selector in the status bar | `OLLAMA_MODEL` in `.env` handles a once-a-month action |

The specification also said PySide6 throughout; this project is PyQt6. It was written from a
generic template, not against this codebase, which is the reason to read the rest of it
sceptically.

## 9. Recorded

- **Background work never disables the UI.** Standing rule, tested.
- **Grouping is display-only**, so §3b stays comparable.
- **Group score is the best chunk**, not the mean.
- **`explain` is moved, not deleted** - trust comes from being able to ask why.
- **Preview is off by default**, rendered on a worker, debounced, cancellable.
- **No browser engine, ever.**
- **Email HTML is sanitised and remote content blocked** before display.
- **Tray is opt-in**, and quitting from it releases the lock properly.
