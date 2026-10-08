# Working on this project in VS Code

**Doc version:** 1.4 · **Updated:** 2026-10-08 · **Applies to:** app v1.0.1

## Opening it

Either open the folder of your clone of GitHub `main` (`D:\Local\GitHub\SearchProject` on the
owner's laptop), or double-click **`Leasha.code-workspace`**.

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
| **debugpy** | Breakpoints in the CLI and the Qt app |
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
| Diagnose (bundle for troubleshooting) | Builds a support bundle (`app.cli diagnose`) into `logs\diagnostics\` |
| Doctor / Doctor (quick) | Environment verification; `--quick` skips model loading |
| Run all tests | The whole suite in one pytest process |
| Run the whole suite (3 processes, the project's runner) | `scripts/run_suite.py -j 3`: the suite split over three processes, each with its own temp folder, a crashed or timed-out process reported as such, and an exit code that is red when any part failed or errored. This is what a release and a hand-off run |
| Run acceptance tests for the current layer | Just `tests/integration` |
| Parse-check PowerShell scripts | Real parser plus the encoding audit |
| Install dev tools | pytest, ruff, mypy |
| CLI: stats | Prints the resolved configuration |
| Download models (search and rerank) | `app.cli models download search rerank`, the same route the installers and Settings use |
| Build the installer | `packaging\build.ps1`: PyInstaller, a run of the built program, Inno Setup. Stops if an optional library is missing from the venv |

`Ctrl+Shift+B` is not bound; **`Run all tests` is the default test task**, so
`Ctrl+Shift+P` -> **Tasks: Run Test Task** runs the suite. Before committing, prefer
**Run the whole suite (3 processes, the project's runner)**: on a machine with less than
about 10 GB free a single process holding Qt, onnxruntime and LanceDB can fault rather than
raise, and the runner is what says so.

**Debugging** (`F5`, then pick a configuration): `CLI: stats`, `CLI: stats (json)`,
`CLI: doctor`, `CLI: index (Layer 3)`, `CLI: search (Layer 4)`, `App: desktop shell (Layer 5)`,
`doctor.py` and `Debug the current test file`. The layer in a name is the layer that built it;
all of them work.

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
- The index itself lives at `DATA_PATH`, never inside the project: `%LOCALAPPDATA%\Leasha` by
  default, `D:\Leasha\Data` on the owner's machine; `leasha stats` prints it

`venv/`, `logs/` and `__pycache__` are excluded from search and from the file watcher, so
`Ctrl+Shift+F` searches your code rather than a thousand site-packages files.

## Suggested workflow per layer

1. Read the layer's section in `BUILD_SPEC_V2.md`
2. Work on a branch: `git checkout -b layer/1-storage`
3. Write the acceptance tests first, from the spec's checklist
4. Build until they pass
5. Run the full suite, bump `VERSION`, update `CHANGELOG.md`, tag

Conventions for branches, commits and tags are in `docs/VERSIONING.md`.
