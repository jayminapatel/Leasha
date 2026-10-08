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

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
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


datas = tree("app") + tree("assets", code_too=True) + tree("config", code_too=True)
for name in ("VERSION", "doctor.py", "LICENSE", ".env.example"):
    datas.append((str(ROOT / name), "."))
for name in ("THIRD_PARTY_NOTICES.md", "USER_GUIDE.html"):
    datas.append((str(ROOT / "docs" / name), "docs"))

binaries = []
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
for package in ("av", "pypff", "reverse_geocoder", "insightface"):
    try:
        __import__(package)
    except Exception:
        continue
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "packaging" / "leasha_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # torch, transformers and friends: installed on the laptop for something else,
    # never imported by Leasha, and 420 MB pulled in through other libraries'
    # optional imports (first build, 2026-10-05: 1.6 GB).
    excludes=["PyQt6", "PyQt5", "PySide2", "tkinter", "pytest", "tests", "IPython",
              "torch", "torchvision", "transformers", "timm"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "assets" / "leasha.ico")
window = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Leasha",
             console=False, icon=icon, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="leasha-cli",
          console=True, icon=icon, upx=False)

COLLECT(window, cli, a.binaries, a.datas, name="Leasha", upx=False)
