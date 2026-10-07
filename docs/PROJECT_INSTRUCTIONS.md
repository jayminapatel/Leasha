# Project instructions

**Doc version:** 1.6 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

The standing rules for working on this project. `HANDOFF.md` says where things *are*; this
says how to *work*. Read both before writing code.

If you are an AI assistant starting a fresh session: this document plus `HANDOFF.md` is the
briefing. Read `BUILD_SPEC_V2.md` for the layer you are about to build.

---

## Starting a session

1. Read `HANDOFF.md` - current state, decisions already made, known traps.
2. Read this document.
3. Read the relevant layer in `BUILD_SPEC_V2.md`.
4. Verify before changing anything:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe doctor.py
venv\Scripts\python.exe -m pytest tests -q
```

Never start work on a red suite. If it is already failing, fixing that *is* the work.

## The non-negotiables

These are design constraints, not preferences. Each exists because the alternative failed or
would fail. Do not quietly relax one to make a task easier - if one genuinely needs to change,
change it deliberately, write down why, and update `HANDOFF.md`.

1. **No service in the search hot path.** Search must work with Ollama stopped, crashed or
   never installed. The LLM is for answers and entity extraction only.
2. **Every error states what happened AND how to fix it.** A structured `AppError` with a
   code, plain-English message, suggestion and action type. Never a bare traceback reaching
   the user. Never a silent `except: pass`.
3. **One bad file never halts a 100GB run.** Log it, mark it in the skip ledger, count it,
   continue. `SKIP_CONTINUE` errors are expected in the thousands and are not failures.
4. **Everything long-running is resumable.** Persist the cursor *before* you need it. A crash
   should cost seconds, not hours.
5. **The UI thread never does I/O.** Work happens in a `QThreadPool` worker; results return
   via Qt signals.
6. **SQLite is the authority.** LanceDB is derived and rebuildable from `chunks`. If they
   disagree, SQLite wins and the vectors are rebuilt. Never the reverse.
7. **Every `.ps1` is ASCII-only and saved UTF-8 with a BOM.** PowerShell 5.1 decodes a
   BOM-less file as the ANSI codepage. This has already killed the installer once, silently,
   with no log at all.
8. **Every layer ships a CLI entry point before it ships UI**, so it can be tested headless.
9. **Measure, do not assume.** The performance budget in `BUILD_SPEC_V2.md` is per stage
   precisely so a miss can be attributed. "It feels fast" is not a result.
    *2026-10-05 note on rule 10 below - the owner's one exception.* "go ahead with option a but an option b
    button which can be used": names and descriptions stay in the index (option a), and the
    Photos tab's "Write names into photos…" (and `app.cli photos --write-names`) writes them
    into photos' XMP only when the owner presses it - never during indexing. Sidecar `.xmp`
    files by default, so no photo changes; into JPEG and PNG only when chosen, after a copy of
    each is kept, pixels untouched and the modified time put back
    (`app/index/photo_metadata.py`). The indexer itself stays read-only.
10. **Read-only against user data.** The indexer opens and reads. It never modifies, moves or
    deletes a document or an email.
11. **Anything tunable has a UI, or is not tunable.** No setting ever requires editing a file.
    If a value has no control, it becomes a fixed constant with a comment saying why it is
    fixed and what evidence would change it - so the rule *shrinks* the tuning surface rather
    than growing a sixty-control dialog. `.env` is written by the application, never by the
    user. Every setting must also justify its existence: if nobody will ever change it, it is a
    constant. Destructive settings - index location, chunk size, embedding model - are flows
    that state the cost and confirm, never plain fields.
    See `docs/WORKORDER-everything-tunable-has-a-ui.md`.

12. **A library where one exists; an external converter only where none does.** A library is
    in-process, pinned, ships a wheel, works on a machine with nothing else installed, and fails
    in a way the error contract can describe. A converter is a subprocess that needs a separate
    install, can be missing, can hang, and turns one file into two disk writes. Before adding a
    converter, check for a maintained wheel; then check whether the format is a documented
    container - zip, XML, JSON - that the standard library already reads. ODF, `.fb2`, `.epub`
    and the Google Drive stubs all went that way and none needed a dependency. Only if neither
    applies does a converter get added, and its reason goes in `CONVERTER_JUSTIFIED` in
    `app/extract/converter.py`, where a test enforces that it exists and says something.
    "Nobody has written a reader" is a reason; "the converter was easier" is not.
    See `docs/WORKORDER-libraries-before-converters.md`.

## How a layer gets built

1. Read the layer's section in `BUILD_SPEC_V2.md`.
2. Branch: `git checkout -b layer/2-extraction`.
3. **Write the acceptance tests first**, straight from the spec's checklist. They are the
   definition of done, not an afterthought.
4. Build until they pass.
5. Run the whole suite, not just the new tests.
6. Release: see the checklist below.

**A layer is not done until its acceptance tests pass.** No moving on with a red test, no
"I'll come back to it".

## Testing

- `tests/unit` - fast, no I/O beyond `tmp_path`. Milliseconds.
- `tests/integration` - the layer acceptance tests, named `test_layerN_acceptance.py`, with
  the spec's criteria quoted verbatim in the module docstring.

Test the behaviour that matters, not the implementation. Good examples already in the repo:
killing a process with `os._exit(9)` mid-transaction to prove committed data and the cursor
survive; asserting that a malformed search query returns `[]` rather than raising, because
users type unbalanced quotes constantly.

**When a test fails, work out whether the test or the code is wrong before fixing either.**
Both have happened here. A Porter-stemming assertion was wrong; a NULL into a NOT NULL column
was the code.

## Writing code here

**Comments explain why, not what.** `# increment counter` is noise. "PowerShell returns
everything a function emits, so `return $LASTEXITCODE` hands back `@(stdout, 0)`" is the
comment that stops the bug coming back.

**Every module docstring names its layer.** `Layer: L2`. It is how the tree stays navigable.

**Errors go through the registry.** Add new codes to `ERROR_REGISTRY` in `app/core/errors.py`
with a message, a suggestion and an action type. Never raise a bare exception across a
boundary - use `guard()`.

**No new dependencies without a real reason.** Everything in `requirements.txt` is pinned to a
version verified to ship a Windows wheel. Adding a package to save a dozen lines is a bad
trade; `pydantic-settings` was rejected on exactly those grounds.

**ASCII in `.ps1` files. UTF-8 elsewhere.** Python source is ASCII by convention here too, so
console output cannot fail on a legacy codepage.

## Release checklist

Every release, in order:

1. All acceptance tests for the layer pass.
2. `doctor.py` prints READY on a clean install.
3. Performance budget re-measured and recorded.
4. `VERSION` bumped per `docs/VERSIONING.md`.
5. Documents changed since the last release have their **Doc version**, **Updated** and
   **Applies to** headers bumped. `pytest tests/unit/test_docs_versioned.py` green.
6. **`HANDOFF.md` updated** - state, next layer, any new decision or trap.
   `pytest tests/unit/test_handoff_current.py` green.
7. `CHANGELOG.md` `[Unreleased]` promoted to the new version with today's date.
8. Commit, then annotated tag.

Steps 5 and 6 are enforced by tests rather than trusted to memory, because documentation that
silently goes stale is worse than none - it is confidently wrong, and someone acts on it.

## Git conventions

Conventional Commits, scoped to the package: `core`, `storage`, `extract`, `index`, `search`,
`graph`, `office`, `llm`, `ui`, `installer`, `deps`, `spec`, `docs`.

```
feat(storage): add FTS5 sync triggers for chunks
fix(installer): treat winget UPDATE_NOT_APPLICABLE as success
perf(search): push ext filter into the LanceDB query
docs(handoff): record Layer 1 completion
```

Commit messages explain **why**, in the body, when the reason is not obvious. `git log` is
part of the handoff.

`main` is always green. Work on `layer/<n>-<name>` or `fix/<short-name>`.

## When something breaks

```powershell
venv\Scripts\python.exe -m app.cli diagnose
```

One zip in `logs\diagnostics\` with the environment, config, both store summaries, disk,
package versions, git state and recent logs. `docs/TROUBLESHOOTING.md` explains how to read
it. Attach it when asking for help rather than describing the symptom.

## Working with an AI assistant on this project

What has actually worked here, kept because it worked:

- **Paste `HANDOFF.md` and `docs/PROJECT_INSTRUCTIONS.md` at the start of a new chat.** That
  is the briefing, and it is why these documents exist.
- **Ask for verification, not assertion.** Every dependency version in this repo was checked
  against PyPI and round-trip tested, not recalled. The original draft pins were more than a
  year stale and one predated the API the spec depended on.
- **Send the diagnostic bundle**, or the log file, rather than retyping console output.
  Mangled console text has already cost a round trip here.
- **Expect the assistant to say when it cannot do something.** It cannot run PowerShell on
  your machine. Pretending otherwise wastes a turn.
- **Push back on plausible-looking work.** Two bugs in this repo were caught only because the
  tests were written to fail: `make_error()` raised a `TypeError` on *every* exception
  conversion, and a successful model download was reported as a failure.

## Things that are deliberately not built

Do not add these without an explicit decision to change scope:

- **Cloud APIs** (Graph, SharePoint REST). OAuth, tenants and app registrations would break
  the offline promise. Sync the library locally and index the folder.
- **OCR.** Image-only PDFs are marked, not read.
- **Multi-user, server or web deployment.** Single user, single machine, one process.
- **Telemetry of any kind.** Nothing leaves the machine.
- **An LLM anywhere in the search path.** Answers only.

> **Scope exception, 2026-09-20 - the owner's explicit decision, Chat only.** The Chat tab
> may, **only when the person turns it on and off by default**, augment its answers from the
> web. The list above is otherwise unchanged and still binds everything else: search,
> indexing and reading files stay fully offline and nothing about them phones out. The
> exception is narrow by construction: only a short search query the person can see before it
> is sent leaves the machine (never file names, paths, passages, mail or conversation
> history), local sources are always searched first and the web only adds to them, and a
> guard test fails if a turn with the switch off opens any non-loopback connection. This is
> not telemetry: nothing is sent unless the person asked a question with the switch on. See
> `docs/WORKORDER-202626270611-chat-tab.md`'s dated note.
