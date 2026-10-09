r"""After renaming D:\Local\GitHub\SearchProject to D:\Local\GitHub\Leasha: fix
everything that had the old path baked in.

Run with the venv's own Python, from anywhere:

    D:\Local\GitHub\Leasha\venv\Scripts\python.exe D:\Local\GitHub\Leasha\scripts\rename_fixup.py

`--check` only reports what it would change. Written 2026-10-09 by the session
that could not do the rename itself: its own process held the folder.

What it fixes, and why each exists:
  1. .env            PROJECT_PATH and LOG_PATH are absolute (HANDOFF section 2).
  2. the venv        activate / activate.bat / Activate.ps1 / pyvenv.cfg name the
                     venv's path; every console-script launcher (pip.exe,
                     pytest.exe, ...) embeds the venv python's path and breaks
                     the moment the folder moves. They are regenerated with
                     pip's own script maker, not by reinstalling 45 packages.
  3. shortcuts       Start Menu: Leasha.lnk (target, folder, icon) and the WSL
                     shortcut's icon.
  4. the workspace   Leasha.code-workspace names the folder and carries a note
                     saying the folder was deliberately not renamed.
  5. documents       the ones that state the laptop path as a fact today;
                     records (dated notes, orders, the changelog) are left.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

OLD = r"D:\Local\GitHub\SearchProject"
NEW = r"D:\Local\GitHub\Leasha"
CHECK = "--check" in sys.argv
ROOT = Path(NEW if Path(NEW).is_dir() else OLD)
changed: list[str] = []


def say(text: str) -> None:
    print(("would: " if CHECK else "done:  ") + text)


def rewrite(path: Path, pairs: list[tuple[str, str]], *, bump: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    new = text
    for old, repl in pairs:
        new = new.replace(old, repl)
    if bump and new != text:
        m = re.search(r"\*\*Doc version:\*\* (\d+)\.(\d+)", new)
        if m:
            new = new.replace(m.group(0), f"**Doc version:** {m.group(1)}.{int(m.group(2)) + 1}", 1)
    if new == text:
        return
    if not CHECK:
        path.write_text(new, encoding="utf-8", newline="\n")
    changed.append(str(path))
    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    say(f"{shown}: {sum(text.count(o) for o, _ in pairs)} replacement(s)")


# --- 0. preconditions -----------------------------------------------------
if not CHECK and (Path(OLD).exists() or not Path(NEW).is_dir()):
    sys.exit(f"rename first: {OLD} must be gone and {NEW} must exist")
print(f"{'checking' if CHECK else 'fixing'} {ROOT}")

# --- 1. .env ----------------------------------------------------------------
rewrite(ROOT / ".env", [(OLD, NEW)])

# --- 2. the venv -------------------------------------------------------------
venv = ROOT / "venv"
for name in ("Scripts/activate", "Scripts/activate.bat", "Scripts/Activate.ps1", "pyvenv.cfg"):
    p = venv / name
    if p.exists():
        rewrite(p, [(OLD, NEW), (OLD.replace("\\", "/"), NEW.replace("\\", "/"))])

if not CHECK:
    from importlib.metadata import distributions

    from pip._vendor.distlib.scripts import ScriptMaker

    maker = ScriptMaker(None, str(venv / "Scripts"))
    maker.executable = str(venv / "Scripts" / "python.exe")
    maker.variants = {""}
    maker.clobber = True
    made = 0
    for dist in distributions():
        for group in ("console_scripts", "gui_scripts"):
            specs = [f"{ep.name} = {ep.value}" for ep in dist.entry_points if ep.group == group]
            if not specs:
                continue
            try:
                made += len(maker.make_multiple(specs, options={"gui": group == "gui_scripts"}))
            except Exception as exc:                     # one package, not the fix
                print(f"  could not regenerate {dist.metadata['Name']}'s launchers: {exc}")
    say(f"venv launchers regenerated: {made} files")
    for orphan in ("pyuic6.exe", "pylupdate6.exe"):     # PyQt6 leftovers, no package owns them
        p = venv / "Scripts" / orphan
        if p.exists():
            p.unlink()
            say(f"removed orphan launcher {orphan}")
else:
    exes = list((venv / "Scripts").glob("*.exe"))
    say(f"regenerate {len(exes)} venv launchers with pip's script maker")

# --- 3. shortcuts ------------------------------------------------------------
try:
    import os

    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    for lnk in (menu / "Leasha.lnk", menu / "Leasha (WSL).lnk"):
        if not lnk.exists():
            continue
        s = shell.CreateShortcut(str(lnk))
        fields = {"TargetPath": s.TargetPath, "WorkingDirectory": s.WorkingDirectory,
                  "IconLocation": s.IconLocation, "Arguments": s.Arguments}
        touched = [k for k, v in fields.items() if OLD.lower() in str(v).lower()]
        if touched:
            if not CHECK:
                for k in touched:
                    setattr(s, k, re.sub(re.escape(OLD), NEW.replace("\\", r"\\"), str(fields[k]), flags=re.I))
                s.Save()
            say(f"{lnk.name}: {', '.join(touched)}")
except Exception as exc:                                 # the shortcuts are a convenience
    print(f"  shortcuts not updated: {exc}")

# --- 4. the workspace ----------------------------------------------------------
rewrite(ROOT / "Leasha.code-workspace", [
    (OLD, NEW),
    ("  // The folder on disk is still D:\\SearchProject, deliberately: the venv has\n"
     "  // that path baked into pip.exe, activate.bat and every console script, so\n"
     "  // moving it means rebuilding the environment for no functional gain.\n",
     "  // The folder on disk is D:\\Local\\GitHub\\Leasha since 2026-10-09 (it was\n"
     "  // SearchProject; the venv's baked-in paths were regenerated in place by\n"
     "  // scripts/rename_fixup.py, see HANDOFF section 2).\n"),
])

# --- 5. documents that state the path as a fact today --------------------------
for rel in ("CLAUDE.md", "docs/TROUBLESHOOTING.md", "docs/VSCODE.md",
            "LOCAL_KNOWLEDGE_GRAPH_V2.md"):
    rewrite(ROOT / rel, [(OLD, NEW)], bump=True)
rewrite(ROOT / "docs/VSCODE.md", [
    ("The folder on disk is still `SearchProject`; the workspace presents it as **Leasha**, which is",
     "The folder on disk is `Leasha` too (renamed 2026-10-09); the workspace presents it as **Leasha**, which is"),
])
rewrite(ROOT / "docs/PROJECT_INSTRUCTIONS.md", [("cd D:\\SearchProject", "cd " + NEW)], bump=True)
rewrite(ROOT / "install.ps1", [("cd D:\\SearchProject", "cd " + NEW),
                                ('-ProjectPath ""D:\\SearchProject""', f'-ProjectPath ""{NEW}""')])
# HANDOFF section 2: the state table, and a dated note above the 2026-09-30 one.
rewrite(ROOT / "HANDOFF.md", [
    ("| Code, docs, venv | `D:\\SearchProject` |", f"| Code, docs, venv | `{NEW}` |"),
    ("| Machine-specific config | `D:\\SearchProject\\.env`", f"| Machine-specific config | `{NEW}\\.env`"),
    ("| Logs and diagnostics | `D:\\SearchProject\\logs\\`", f"| Logs and diagnostics | `{NEW}\\logs\\`"),
    ("> **2026-09-30 - the owner's laptop moved.**",
     "> **2026-10-09 - the folder is `D:\\Local\\GitHub\\Leasha`.** Renamed from `SearchProject` on the\n"
     "> owner's instruction, after the stale second clone of that name was deleted. The venv was not\n"
     "> rebuilt: `scripts/rename_fixup.py` rewrote `.env`, the activate scripts and `pyvenv.cfg`,\n"
     "> regenerated every launcher with pip's script maker, and repointed the Start Menu shortcuts,\n"
     "> the workspace file and the documents below. Read `D:\\SearchProject` and\n"
     "> `D:\\Local\\GitHub\\SearchProject` in older notes as this path.\n"
     ">\n"
     "> **2026-09-30 - the owner's laptop moved.**"),
], bump=True)

print()
print(f"{len(changed)} file(s) {'would be' if CHECK else ''} changed")
if not CHECK:
    print("next: run the checks -")
    print(f"  {venv}\\Scripts\\pip.exe --version")
    print(f"  {venv}\\Scripts\\python.exe -m pytest tests/unit/test_env_never_loses_paths.py "
          "tests/unit/test_docs_versioned.py tests/unit/test_handoff_current.py tests/unit/test_vs_project.py -q")
