# Working on this project in VS Code

**Doc version:** 1.1 · **Updated:** 2026-10-01 · **Applies to:** app v0.3.3

## Opening it

> *Note, 1 October 2026:* on the owner's laptop the folder is now `D:\Local\GitHub\SearchProject`, a clone of
> GitHub `main` (moved out of Google Drive on 30 September 2026). Read `D:\SearchProject` below as
> wherever your clone is.

Either open the folder `D:\SearchProject`, or double-click **`Leasha.code-workspace`**.

The folder on disk is still `SearchProject`; the workspace presents it as **Leasha**, which is
the application's name. Renaming the folder would mean rebuilding the venv - `pip.exe`,
`activate.bat` and every console script have the absolute path compiled into them - for no
functional gain.

Visual Studio users: open **`Leasha.sln`** instead. See `docs/VISUALSTUDIO.md`.

On first open VS Code will offer the recommended extensions from
`.vscode/extensions.json`. Accept them:

| Extension | Why |
|---|---|
| **Python** + **Pylance** | Language support, and the Test Explorer |
| **debugpy** | Breakpoints in the CLI and, later, the Qt app |
| **Ruff** | Fast linting, configured in `pyproject.toml` |
| **PowerShell** | Editing and debugging `install.ps1` |
| **SQLite Viewer** | Opening `knowledge.db` and browsing the index directly |
| **Even Better TOML** | `pyproject.toml` |

## The one thing to check

Bottom-right of the status bar, confirm the interpreter reads
**`venv\Scripts\python.exe`**, not a system Python. `.vscode/settings.json` sets it, but if the
venv is rebuilt VS Code can lose the association:

> `Ctrl+Shift+P` -> **Python: Select Interpreter** -> the one under `.\venv\Scripts\`

If `import app` shows a squiggle, that is almost always the interpreter, not the code.

## Install the dev tools once

`pytest` and friends are development-only and not part of `requirements.txt`:

> `Ctrl+Shift+P` -> **Tasks: Run Task** -> **Install dev tools**

Or in a terminal: `venv\Scripts\python.exe -m pip install -r requirements-dev.txt`

## Running things

**Tasks** (`Ctrl+Shift+P` -> Tasks: Run Task):

| Task | What it does |
|---|---|
| Install / repair environment | Runs `run-install.cmd`. Safe to re-run; completed steps are skipped |
| Preflight (no downloads) | Cheap local checks only, nothing installed |
| Doctor / Doctor (quick) | Environment verification; `--quick` skips model loading |
| Run all tests | The whole suite |
| Run acceptance tests for the current layer | Just `tests/integration` |
| Parse-check PowerShell scripts | Real parser plus the encoding audit |
| Install dev tools | pytest, ruff, mypy |
| CLI: stats | Prints the resolved configuration |

`Ctrl+Shift+B` is not bound; **`Run all tests` is the default test task**, so
`Ctrl+Shift+P` -> **Tasks: Run Test Task** runs the suite.

> *Note, 1 October 2026:* Layers 3, 4 and 5 landed long ago; the `index`, `search` and desktop configurations
> work, whatever their names in `.vscode/launch.json` still say. There is also `CLI: stats (json)`,
> and a **Diagnose** task that builds a support bundle.

**Debugging** (`F5`, then pick a configuration): `CLI: stats`, `CLI: doctor`, `doctor.py`,
`Debug the current test file`, and placeholders for `index`, `search` and the desktop shell
that will start working as Layers 3, 4 and 5 land.

Every configuration sets `justMyCode: false`, so you can step into pydantic, lancedb or
fastembed when something misbehaves in a dependency rather than in our code.

**Testing panel:** the beaker icon in the sidebar discovers tests automatically. Individual
tests can be run and debugged from the gutter.

## Encoding: the setting that matters

`.vscode/settings.json` contains:

```jsonc
"[powershell]": {
  "files.encoding": "utf8bom",
  "files.eol": "\r\n"
}
```

This is not tidiness. Windows PowerShell 5.1 decodes a BOM-less file using the ANSI codepage,
so an em dash saved as UTF-8 without a BOM becomes a smart quote, which PowerShell treats as a
string delimiter. That is exactly what killed `install.ps1` at parse time, producing no log,
no venv and no output of any kind. This setting makes it impossible to reintroduce by editing
a `.ps1` here.

The `Parse-check PowerShell scripts` task is the backstop: it fails any `.ps1` that has
non-ASCII bytes and no BOM, before the script is ever run.

## Layout worth knowing

- `app/` is the package. Every module's docstring names the layer that owns it
- `tests/unit` runs in milliseconds; `tests/integration` holds the layer acceptance tests
- `logs/` holds both app logs and installer transcripts; it is gitignored
- `.env` is machine-specific and gitignored. `.env.example` is the shared template
- The index itself lives at `DATA_PATH` (`D:\KnowledgeGraphData`), never inside the project
  (*note, 1 October 2026:* `%LOCALAPPDATA%\Leasha` by default, `D:\Leasha\Data` on the owner's
  machine; `leasha stats` prints it)

`venv/`, `logs/` and `__pycache__` are excluded from search and from the file watcher, so
`Ctrl+Shift+F` searches your code rather than a thousand site-packages files.

## Suggested workflow per layer

1. Read the layer's section in `BUILD_SPEC_V2.md`
2. Work on a branch: `git checkout -b layer/1-storage`
3. Write the acceptance tests first, from the spec's checklist
4. Build until they pass
5. Run the full suite, bump `VERSION`, update `CHANGELOG.md`, tag

Conventions for branches, commits and tags are in `docs/VERSIONING.md`.
