# Working on Leasha in Visual Studio

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3

`docs/VSCODE.md` covers VS Code. This covers full Visual Studio, which needs a solution and a
project file where VS Code needs neither.

## Opening it

Double-click **`Leasha.sln`**. It contains one project, `Leasha.pyproj`.

Visual Studio needs the **Python development** workload. If Solution Explorer shows the project
as unavailable, that workload is missing: Tools → Get Tools and Features → Python development.

## The interpreter

The project points at the venv already, relatively:

```xml
<InterpreterId>MSBuild|venv|$(MSBuildProjectFullPath)</InterpreterId>
```

If Visual Studio reports the environment as missing, the venv has not been created yet. Run
`install.ps1` (or `run-install.cmd`) first, then reload the solution. **Do not point the project
at a global Python installation** - the pins in `requirements.txt` are the tested set, and a
system interpreter will have different versions of PyQt6 and onnxruntime.

## Running and debugging

F5 launches `app\main.py`, the GUI. That is the startup file because it is the application a
person actually uses.

For the CLI, the ready-made debug configurations live in `.vscode/launch.json` and Visual Studio
does not read them. Either use the terminal:

```
venv\Scripts\python.exe -m app.cli stats
venv\Scripts\python.exe -m app.cli doctor --quick
```

or set Project → Properties → Debug → Script Arguments for a one-off.

## Tests

The project declares pytest, so Test Explorer finds the suite under `tests\`. The settings that
matter - `--basetemp=.pytest_tmp` and the `jvm` marker exclusion - live in `pyproject.toml` and
apply either way.

Running the whole suite from the terminal is usually faster:

```
venv\Scripts\python.exe -m pytest tests -q
```

## `Leasha.pyproj` is generated - do not hand-edit it

**Visual Studio shows only the files the project lists.** VS Code shows the folder; Visual
Studio shows the manifest. With 228 Python files that manifest goes stale the first time
somebody adds a module - and a stale one is worse than none, because the new file is absent
from Solution Explorer while every test that imports it passes. Somebody concludes the file
does not exist.

So it is generated from `git ls-files`:

```
venv\Scripts\python.exe scripts\regen_vs_project.py
```

Re-run that after adding or removing files, and before committing if Visual Studio has rewritten
the file itself (it does this when you add a file through the IDE). A test asserts the committed
project matches what the script produces, so a drift is caught in the suite rather than
discovered by the next person to open the solution.

**The project GUID must stay stable.** `Leasha.sln` references the project by GUID; change it
and the solution no longer contains a project.

## What is not committed

`.vs\`, `*.suo` and `*.user` are per-user state - window layout, breakpoints, the IntelliSense
database. `.vs\` alone reaches hundreds of megabytes on a project this size. They are in
`.gitignore`; the solution and project files themselves are committed, because they are the
project definition.
