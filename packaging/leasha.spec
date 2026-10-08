# PyInstaller build of Leasha: one folder, two programs (order 202626082213 §4.1).
#
#   Leasha.exe       the window, no console
#   leasha-cli.exe   console; runs `-m`, `-c` and scripts for the window's own
#                    children, and `app.cli` commands from a terminal
#
# Built by packaging/build.ps1. The folder mirrors the repository under
# `_internal`, so `project_root()` (parents[2] of app/core/config.py) is
# `_internal` and every asset, config file and the `.env` are found there
# exactly as in a checkout.
# ruff: noqa

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
# Stale bytecode from the checkout must not ride along: it was compiled against
# the checkout's absolute paths, and PyInstaller compiles its own.
SKIP_DIRS = {"__pycache__"}


def tree(folder, code_too=False):
    """Every file under `folder`, placed at the same relative path."""
    found = []
    for path in (ROOT / folder).rglob("*"):
        if not path.is_file() or SKIP_DIRS & set(path.parts):
            continue
        if not code_too and path.suffix in (".py", ".pyc"):
            continue
        found.append((str(path), str(path.parent.relative_to(ROOT))))
    return found


# The Analysis follows imports, never files read by path - so every non-code
# file under app/ (icons, prompts, the settings registry's data) and the whole
# of assets/ and config/ are listed here, at the paths the code expects.
datas = tree("app") + tree("assets", code_too=True) + tree("config", code_too=True)
for name in ("VERSION", "doctor.py", "LICENSE", ".env.example"):
    datas.append((str(ROOT / name), "."))
for name in ("THIRD_PARTY_NOTICES.md", "USER_GUIDE.html"):
    datas.append((str(ROOT / "docs" / name), "docs"))

binaries = []
# Every module under app/: the entry point reaches `app.cli` and `app.main` through
# runpy by name, and the extractor registry and the window import parts by string,
# none of which the analyser's bytecode scan can follow.
hiddenimports = collect_submodules("app")

# Packages that load parts of themselves by name, or carry data files.
for package in ("lancedb", "onnxruntime", "fastembed", "rapidocr_onnxruntime",
                "extract_msg", "ezdxf", "mcp", "tokenizers", "pywt"):
    try:
        d, b, h = collect_all(package)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

# Optional packages: in the build when this machine has them, as on the owner's.
# insightface (2026-10-08): the faces model is downloadable from the installer and
# Settings now, and without the library in the build it could never be used.
OPTIONAL_PACKAGES = ("av", "pypff", "reverse_geocoder", "insightface")
missing_optional = []
for package in OPTIONAL_PACKAGES:
    try:
        __import__(package)
    except Exception:
        missing_optional.append(package)
        continue
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

# 2026-10-08 review: "in the build when this machine has them" meant two builds
# of one commit could ship different things, and the installer offers every
# user the People-in-photos model whether or not the library made it in. A
# missing optional now stops the build and names the pip line, unless the
# person building says in so many words that a smaller build is what they want
# (build.ps1 -AllowMissingOptional sets the variable).
if missing_optional and not os.environ.get("LEASHA_BUILD_ALLOW_MISSING"):
    raise SystemExit(
        "leasha.spec: these optional packages are not installed in the build venv, so "
        "the installer would ship without them: " + ", ".join(missing_optional) + ". "
        "Install them (see requirements.txt, the optional sections) or run "
        "packaging\\build.ps1 -AllowMissingOptional to build a smaller Leasha deliberately.")

a = Analysis(
    [str(ROOT / "packaging" / "leasha_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # PyQt6/PyQt5/PySide2: the laptop venv carried PyQt6 beside PySide6 for a while
    # (HANDOFF); a second binding in the build is a copy of Qt's DLLs nobody loads.
    # torch, transformers and friends: installed on the laptop for something else,
    # never imported by Leasha, and 420 MB pulled in through other libraries'
    # optional imports (first build, 2026-10-05: 1.6 GB).
    excludes=["PyQt6", "PyQt5", "PySide2", "tkinter", "pytest", "tests", "IPython",
              "torch", "torchvision", "transformers", "timm"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "assets" / "leasha.ico")
# Two programs from one PYZ. `exclude_binaries=True` keeps the DLLs out of each
# .exe so COLLECT ships one copy beside both. `upx=False`: a packer rewrites the
# Qt and onnxruntime DLLs, and rewritten binaries are what antivirus and Smart
# App Control look at first - the build is unsigned and needs no second strike.
window = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Leasha",
             console=False, icon=icon, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="leasha-cli",
          console=True, icon=icon, upx=False)

COLLECT(window, cli, a.binaries, a.datas, name="Leasha", upx=False)
