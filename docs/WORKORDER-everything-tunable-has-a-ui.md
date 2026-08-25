# Work order: every tunable has a UI, or stops being tunable

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3

A new standing rule from the owner, applying **everywhere**, not to one screen:

> Anything which can be modified or tuned has to have a UI. No editing files directly.

Commit the working tree before starting.

---

## 1. The rule has two halves, and the second is the useful one

Read literally, "everything tunable needs a UI" means a Settings dialog with sixty controls,
which is worse for the user than none. The rule only works stated both ways:

> **Every knob is either exposed in the user interface, or it is not a knob.**
> If it has no UI, it becomes a fixed constant with a comment saying why it is fixed and what
> evidence would change it.

That forces a decision on every value rather than letting magic numbers accumulate unexamined.
**Most values should be demoted, not promoted.** The rule should shrink the tuning surface, not
grow it.

A third clause makes it liveable:

> **Every setting must justify its existence.** A control that nobody will ever change is
> clutter, and clutter is how a settings screen becomes unreadable. If the honest answer to
> "who changes this, and when?" is "nobody", it is a constant.

## 2. What is currently tunable without a UI

An audit of the codebase found roughly **thirty module-level constants** and **twenty-nine
`.env` keys**, against about **eight controls** in Settings.

### 2a. Should become settings

| Value | Where | Why a user changes it |
|---|---|---|
| `RERANK_TOP_N`, `RERANK_WINDOW_CHARS` | `.env` | Speed against precision - the main quality dial |
| `INDEX_WORKERS`, `INDEX_MEMORY_MB`, `INDEX_CPU_PERCENT` | `.env` | "Use less of my machine while I work" |
| `INDEX_LOW_PRIORITY`, `INDEX_PAUSE_ON_BATTERY` | `.env` | Laptop behaviour |
| `INDEX_SCHEDULE`, `INDEX_INTERVAL_HOURS`, `INDEX_DAILY_AT` | `.env` | When indexing runs |
| `MIN_FREE_GB` | `.env` | How much disk to leave free |
| `OLLAMA_URL`, `OLLAMA_MODEL` | `.env` | Which local model, or none |
| `MIN_CONFIDENCE`, `MAX_PAGES` | `ocr.py:55,60` | OCR quality against time - the slowest thing here |
| `MAX_ATTACHMENT_BYTES` | `email_pst.py:81` | Whether 64MB attachments are worth indexing |
| `MAX_SHEET_ROWS`, `MAX_SHEET_COLUMNS` | `office.py:46,50` | Spreadsheet-heavy corpora legitimately differ |
| `MAX_TIMEOUT_S` | `converter.py:87` | Slow machines need longer |

Most already have a home: the existing **Indexing settings** panel, plus a **Search** and a
**Reading** group in Settings.

### 2b. Should stop being tunable

Chosen by evidence, not preference. Fix them, and record the evidence in the comment.

| Value | Where | Why it is not a preference |
|---|---|---|
| `RRF_K` | `fusion.py` | 60, from the original paper. Nobody can tune this meaningfully |
| `TARGET_TOKENS`, `OVERLAP_TOKENS` | `chunker.py:53,54` | Changing these **invalidates the whole index**. Not a setting; a rebuild decision - see §4 |
| `EMBED_BATCH` | `pipeline.py:97` **and** `embedder.py:44` | Two constants, one name, different values (256 and 64). Resolve to one, fix it, comment it |
| `VECTOR_BATCH`, `CHECKPOINT_EVERY`, `CHECKPOINT_SECONDS`, `DISK_CHECK_EVERY` | `pipeline.py` | Internal pacing. No user meaning |
| `HASH_CHUNK_BYTES`, `RECENT_EDIT_WINDOW_S` | `walker.py:79,95` | Implementation detail |
| `TOKENS_PER_WORD`, `CHARS_PER_TOKEN`, `MIN_CHUNK_CHARS` | `chunker.py` | Estimator coefficients, not choices |
| `HEALTH_CACHE_S`, `CONNECT_TIMEOUT_S` | `ollama.py:40,54` | Sensible defaults; nobody tunes a health check |
| Debounce intervals | `search_view.py` | Tuned by feel once, then left alone |
| `MAX_LOG_BYTES`, `RECENT_LOGS_PER_FOLDER` | `diagnostics.py:31,35` | Diagnostic plumbing |

### 2c. Already correct

File types are the model to copy: shipped defaults, a user override file, **and a Settings
table that edits it**. Whoever built that got the rule right before it was written down. View
preferences likewise.

## 3. `.env` stops being the editing surface

`.env` remains the machine-generated store of settings. It is written by the installer and by
the UI; **the user is never asked to edit it**. Same pattern the file-type override already
uses.

Consequences:

- Settings **writes** `.env`, atomically - temp file plus replace, so a crash mid-write cannot
  leave an unparseable config.
- `.env.example` is documentation, not an instruction.
- Anywhere the docs currently say "edit `.env`", replace with where the control lives.
  `docs/TROUBLESHOOTING.md` says this in at least two places.
- Values changed in the UI take effect **without a restart** wherever possible. Where a restart
  is genuinely required, say so on the control rather than letting the change appear to work.

## 4. Two settings that are not settings, and must not be text boxes

**`data_path`** is currently a `QLineEdit` with `setReadOnly(True)` (`settings_view.py:98-99`),
as is `ollama_url` (`:100-101`). They are **displays, not controls** - so today they fail the
new rule by being untunable rather than by being dangerous. Whoever wrote them chose the safe
option, and was right to.

The fix is not to remove `setReadOnly`. Typing a new path into a box does not move an index; it
points the application at a different, probably empty, one, and the user's index appears to
vanish.

It needs a **flow**: *Move the index here* (copy, verify, switch, delete the original only on
success), *Use an existing index at this location*, or *Start a new empty index*. Anything else
is a data-loss trap wearing the costume of a setting.

`ollama_url` is the easy case by contrast - editable, validated, with a "Test connection"
button that says what it found.

**Chunk size and embedding model** invalidate every vector in the index. If either is ever
exposed, it belongs in the same shape: a deliberate action that states the cost - "this will
re-embed 4.2 million chunks, about six hours" - with a confirmation, not a spin box that
silently makes search worse until the next full rebuild.

## 5. Making the rule enforceable

A rule nobody can check is a rule that decays. This project already enforces doc versions and
handoff currency with tests; do the same here.

**A single registry** - `app/core/settings_registry.py` - as the one place a setting is
declared:

```python
@dataclass(frozen=True)
class Setting:
    key: str            # the .env key
    label: str          # what the user sees
    kind: str           # bool | int | path | choice | text
    default: object
    group: str          # "Indexing" | "Search" | "Reading" | "Models"
    surface: str        # the UI panel that exposes it
    restart: bool = False
    help: str = ""      # one line, shown next to the control
```

Then the tests that make the rule real:

- [ ] **Every key read from `.env` appears in the registry.** Parse `config.py` for the keys it
      reads and diff against the registry - a new key without a control fails the suite.
- [ ] **Every registry entry is reachable from the UI.** Assert the panel named in `surface`
      builds a control for it. This is the test that catches a setting existing but being wired
      to nothing - which has already happened twice here (`rerank_toggled`, `cloud_toggled`).
- [ ] **Every setting round-trips**: set through the presenter, saved, reloaded, and comes back
      equal. The current View menu drops `group_by_document` and `show_scores` on an unrelated
      change; this test would have caught it.
- [ ] **Settings that need a restart say so**, and settings that do not, apply live.
- [ ] `.env` writes are atomic, and a malformed file produces `ERR_CONFIG_INVALID` naming the
      key rather than a silent default.
- [ ] **No control is orphaned**: every signal declared by a settings panel has a receiver.
      Static assertion over `app/ui/`.

The registry also gives `app.cli settings` for free - list, get, set - which keeps the
"every layer ships a CLI entry point" rule intact without making the CLI the primary surface.

## 6. Order

1. Write `settings_registry.py` and populate it from the existing `.env` keys. **Nothing
   visible changes yet.**
2. Add the diff test. It will fail. That failing list is the work.
3. Demote everything in §2b to fixed constants with comments. This shortens the list before you
   build anything.
4. Build the missing controls, grouped: Indexing, Search, Reading, Models.
5. `.env` writing, atomic, with live application where possible.
6. The `data_path` flow in §4.
7. Fix the orphaned signals found in the review - `rerank_toggled`, `cloud_toggled`,
   `ui:tray_minimise`, `ui:tray_close`.
8. Sweep the docs for "edit `.env`" and replace with where the control is.

## 7. Recorded

- **Every knob is exposed or is not a knob.** The second half is what keeps this from becoming
  a sixty-control dialog.
- **Every setting justifies its existence.** "Nobody changes this" means it is a constant.
- **`.env` is written by the app, never by the user.**
- **A registry plus tests**, because a rule that is not checked will drift - which is precisely
  the finding of `docs/REVIEW-2026-08-25.md`.
- **Destructive settings are flows, not fields.** Index location and chunk size change what the
  index *is*; they must state the cost and confirm.
- **Developer-facing files are out of scope.** `pyproject.toml`, `requirements.txt` and the test
  configuration are not user tuning.
