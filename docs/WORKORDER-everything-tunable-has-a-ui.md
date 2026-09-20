# Work order: every tunable has a UI, or stops being tunable

**Doc version:** 1.2 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3

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

> **2026-09-05 - acceptance checklist verified.** All six items below were audited against
> the current code rather than assumed: read `config.py`, `settings_registry.py`,
> `env_writer.py`, and every existing test over them, then checked each property by hand
> before trusting a test's own claim to cover it. Five held and now have a test proving so;
> item 6 found one real, dated violation whose fix falls outside this order's file scope. See
> the note under item 6.

- [x] **Every key read from `.env` appears in the registry.** Parse `config.py` for the keys it
      reads and diff against the registry - a new key without a control fails the suite.
      Held already: `test_every_setting_config_reads_has_a_control` in
      `tests/unit/test_settings_registry.py` does exactly this, by `ast`-parsing `config.py`
      rather than trusting an import. No missing key found.
- [x] **Every registry entry is reachable from the UI.** Assert the panel named in `surface`
      builds a control for it. This is the test that catches a setting existing but being wired
      to nothing - which has already happened twice here (`rerank_toggled`, `cloud_toggled`).
      Held already: `test_every_plain_setting_has_a_control` and `test_a_flow_is_actually_invoked`
      in `tests/unit/test_settings_reachable.py`. Both bugs named here are already fixed (see
      `shell.py`'s wiring and `widgets/window_box.py`'s docstring for the tray pair).
- [x] **Every setting round-trips**: set through the presenter, saved, reloaded, and comes back
      equal. The current View menu drops `group_by_document` and `show_scores` on an unrelated
      change; this test would have caught it.
      Held already: `test_every_default_survives_a_round_trip` sets every registry default
      through `apply_values` and reads it back; `test_config_can_read_what_the_writer_wrote` in
      the same file proves the writer and `load_settings` agree end to end.
- [x] **Settings that need a restart say so**, and settings that do not, apply live.
      Held, and only partly tested before - `test_restart_settings_are_declared` checked two
      hardcoded keys. Added `test_restart_settings_say_so_somewhere_the_user_can_see`
      (parametrised over every `restart=True` entry) to `test_settings_reachable.py`: it
      confirms the word "restart" is visible on the setting's own surface for `EMBED_DEVICE`,
      `EMBED_QUANTISED` and `RERANK_MODEL`, and in `shell.py`'s flow-outcome message for
      `DATA_PATH`, `EMBED_MODEL` and `EMBED_DIM` (`_change_index_location` /
      `_change_meaning_model`). The "applies live" half is spread across many existing tests
      (search behaviours, the mini-search hotkey, live rerank numbers) rather than one place;
      not re-proven here, only confirmed by reading `shell.py::_settings_changed`.
- [x] `.env` writes are atomic, and a malformed file produces `ERR_CONFIG_INVALID` naming the
      key rather than a silent default.
      Held already. Atomicity: `write_env` writes a temp file in the target directory and
      `os.replace`s it, proven by `test_write_leaves_no_temp_files`,
      `test_unwritable_target_raises_an_app_error` and the BOM tests in
      `tests/unit/test_settings_writes.py`. Malformed input: `load_settings` raises
      `ERR_CONFIG_INVALID` naming the key for every bad value, parametrised in
      `tests/unit/test_config.py::test_bad_value_names_the_offending_key` (outside this order's
      file scope, so not duplicated here - it already exists and passes).
> **2026-09-20 - the last box, closed.** `history_cleared` now has a receiver, and the
> exemption and its test are deleted. Decision: **add a receiver rather than delete the
> signal**, because something does show the log: the search box's "recent searches" list is
> a cached read of the `searches` table that "Clear search history" empties, so until now
> someone who cleared their history was still offered every search they had just erased,
> until the next launch. `MainWindow` connects the signal to `SavedSearches.forget_recent`
> (drops the cached list at once and re-reads, so an already-out fetch cannot put it back).
> Saved searches are not the log and are untouched. Proven by
> `test_clearing_the_history_stops_the_search_box_offering_it` in `test_window_opens.py`
> (fails without the connection) and by `test_every_signal_a_settings_panel_declares_has_a_receiver`,
> which now has no exemption to hide behind. The paragraph below is the 2026-09-05 record
> and is left as written.

- [x] **No control is orphaned**: every signal declared by a settings panel has a receiver.
      Static assertion over `app/ui/`.
      **Blocked on scope, one real violation found.** Added
      `test_every_signal_a_settings_panel_declares_has_a_receiver` to
      `test_settings_reachable.py`: for every `pyqtSignal` declared across the Settings and
      Indexing pages and their widgets, something in `app/ui` must call `.connect()` on it.
      Every previously-known case (`rerank_toggled`, `cloud_toggled`, `ui:tray_minimise`,
      `ui:tray_close`) is already fixed. The test found one the review missed:
      **`settings_view.py:95` declares `history_cleared = pyqtSignal(int)`, and
      `settings_view.py:385` emits it (`self.history_cleared.emit(removed)`, right after the
      usage log is cleared) - and nothing anywhere in the repository ever connects to it.** It is
      a real orphaned signal, the same shape as `rerank_toggled`/`cloud_toggled`. Fixing it means
      either adding a receiver in `shell.py` (most likely: refreshing whatever shows saved-search
      history elsewhere once the log is cleared) or deleting the signal from `settings_view.py`
      if nothing should react to it - and both files are outside this order's file scope, with
      `settings_view.py` explicitly owned by the concurrent pages-reorg session. Recorded as a
      dated exemption (`ORPHANED_PENDING_FIX`) in `test_settings_reachable.py` so the general
      test stays green and provable rather than deleted or weakened; the exemption's own test
      (`test_the_pending_orphan_fix_is_dated_and_still_needed`) fails the moment somebody wires
      it up, which is the signal to delete the exemption and tick this box.

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
