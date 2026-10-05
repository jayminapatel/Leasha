#!/usr/bin/env python3
"""
doctor.py - Leasha environment verification.

Every check that fails states WHAT failed, WHY, and HOW to fix it.
Optional components (rerank model, Outlook, Ollama) can fail without
blocking readiness - only hard requirements gate the app.

Usage:
    venv\\Scripts\\python.exe doctor.py
    venv\\Scripts\\python.exe doctor.py --json      # machine-readable
    venv\\Scripts\\python.exe doctor.py --quick     # skip model load (fast)

Exit codes:
    0  READY      - all required checks passed
    1  NOT READY  - at least one required check failed
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import sys
import sysconfig
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Output encoding: when stdout is redirected (as it is under the installer's
# transcript) Python falls back to the ANSI codepage, and any non-ASCII byte
# raises UnicodeEncodeError mid-report. Never let the report die on a dash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"

MIN_PYTHON = (3, 12)
DEFAULT_REQUIRED_FREE_GB = 150


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    optional: bool = False


# ---------------------------------------------------------------------------
# .env loading - stdlib only, so doctor works before pip install runs
# ---------------------------------------------------------------------------

def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"')
    return values


ENV = load_env()


def env_path(key: str, default: str = "") -> str:
    return ENV.get(key, os.environ.get(key, default))


def env_setting(key: str, fallback: str = "") -> str:
    """`.env` if it says so, otherwise **the declared default**, not a copy.

    `doctor` kept its own defaults - `env_path("RERANK_MODEL",
    "BAAI/bge-reranker-base")` and five more. When `RERANK_MODEL` was removed
    from `.env` so that the faster default could apply, doctor went on
    reporting the model it had been given as a fallback, and loading it. It
    said `BAAI/bge-reranker-base` while search used
    `Xenova/ms-marco-MiniLM-L-6-v2`.

    **A diagnostic that reports its own defaults instead of the
    application's is worse than no diagnostic**, because it is trusted. This
    one sent the owner looking for a bug in a config change that had worked.

    `settings_registry` is stdlib-only on purpose, so importing it here does
    not compromise doctor's ability to run before the dependencies are proven.
    The import is still guarded: a doctor that cannot start is a doctor that
    cannot tell you why nothing starts.
    """
    try:
        from app.core.settings_registry import by_key
    except Exception:                       # doctor must always run
        return env_path(key, fallback)
    setting = by_key(key)
    declared = fallback if setting is None else str(setting.default)
    return env_path(key, declared)


# ---------------------------------------------------------------------------
# Checks - required
# ---------------------------------------------------------------------------

def check_env_file() -> Check:
    if ENV_FILE.exists() and ENV.get("DATA_PATH"):
        return Check(".env present with DATA_PATH", True, ENV["DATA_PATH"])
    return Check(
        ".env present with DATA_PATH", False,
        f"not found or incomplete at {ENV_FILE}",
        fix="Re-run install.ps1 from this folder - it writes .env. "
            "Or create it by hand with DATA_PATH=<your index location>.",
    )


def check_python() -> Check:
    v = sys.version_info
    found = f"{v.major}.{v.minor}.{v.micro}"
    ok = (v.major, v.minor) >= MIN_PYTHON
    return Check(
        f"Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}", ok, f"found {found} at {sys.executable}",
        fix="winget install --id Python.Python.3.12 -e   then delete the venv folder and re-run install.ps1",
    )


def check_in_venv() -> Check:
    # 2026-10-05: a packaged build has no venv by design - the installer's own
    # folder is its environment - so there it passes and says which.
    if getattr(sys, "frozen", False):
        return Check("Running inside the project venv", True,
                     f"packaged build at {Path(sys.executable).parent}")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return Check(
        "Running inside the project venv", in_venv,
        f"prefix={sys.prefix}",
        fix=r"Run it as: venv\Scripts\python.exe doctor.py  (not a system python)",
    )


def check_platform() -> Check:
    # Order 0x section 0d (2026-09-27): a Mac is platform two. Files, photos and
    # code are meant to work there; only live Outlook mail (MAPI, Windows-only)
    # does not. So on macOS this check passes and says what is missing, instead
    # of failing the whole doctor run over one mail source. The Windows result
    # below is exactly what it always was - Windows is platform one - and any
    # other platform still fails as before. (UNCONFIRMED on macOS: nobody has
    # run doctor.py on a real Mac yet; see docs/MAC_VERIFICATION.md.)
    if sys.platform == "darwin":
        return Check(
            "Windows platform", True,
            "darwin - macOS: files, photos and code are supported; mail from a running "
            "Outlook needs Windows (point Leasha at .pst, .mbox or .eml files instead)",
        )
    ok = sys.platform == "win32"
    return Check(
        "Windows platform", ok, sys.platform,
        fix="This build targets Windows 10/11 only - PST ingestion via Outlook MAPI has no cross-platform equivalent.",
    )


PACKAGES = [
    ("PySide6.QtCore", "PySide6"),
    ("lancedb", "lancedb"),
    ("fastembed", "fastembed"),
    ("pymupdf", "pymupdf"),
    ("docx", "python-docx"),
    ("openpyxl", "openpyxl"),
    ("pptx", "python-pptx"),
    # 2026-10-05: `diskcache` taken off. Nothing in `app/` imports it any more
    # (the search cache is in memory - `search/engine.py` says why), so the
    # packaged build rightly leaves it out and this line made its health check
    # say NOT READY for a package Leasha does not use. requirements.txt still
    # installs it; harmless.
    ("pydantic", "pydantic"),
    ("dotenv", "python-dotenv"),
    ("loguru", "loguru"),
    ("tqdm", "tqdm"),
    ("requests", "requests"),
]


def check_packages() -> list[Check]:
    out: list[Check] = []
    for module, pipname in PACKAGES:
        try:
            importlib.import_module(module)
            out.append(Check(f"import {module}", True))
        except Exception as exc:
            out.append(Check(
                f"import {module}", False, f"{type(exc).__name__}: {exc}",
                fix=rf'"{sys.executable}" -m pip install {pipname}',
            ))
    return out


def check_pywin32() -> Check:
    """pywin32 is required for PST, but its absence is an optional failure."""
    try:
        importlib.import_module("win32com.client")
        return Check("import win32com (pywin32)", True, optional=True)
    except Exception as exc:
        return Check(
            "import win32com (pywin32)", False, f"{type(exc).__name__}: {exc}",
            fix=(rf'OPTIONAL - only needed for PST email indexing. '
                 rf'"{sys.executable}" -m pip install pywin32 '
                 rf'&& "{sys.executable}" Scripts\pywin32_postinstall.py -install'),
            optional=True,
        )


def check_fts5() -> Check:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        con.execute("INSERT INTO t(body) VALUES ('hello world')")
        row = con.execute("SELECT body FROM t WHERE t MATCH 'hello'").fetchone()
        con.close()
        if not row:
            raise sqlite3.OperationalError("FTS5 table created but MATCH returned nothing")
        return Check("SQLite FTS5 (create + match)", True, f"sqlite {sqlite3.sqlite_version}")
    except Exception as exc:
        return Check(
            "SQLite FTS5", False, f"{type(exc).__name__}: {exc}",
            fix="This Python's bundled sqlite3 lacks FTS5. Install CPython 3.12 from python.org "
                "(its sqlite3 includes FTS5), then recreate the venv.",
        )


def check_sqlite_wal() -> Check:
    try:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wal_probe.db"
            con = sqlite3.connect(db)
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            con.close()
        ok = str(mode).lower() == "wal"
        return Check("SQLite WAL mode", ok, f"journal_mode={mode}",
                     fix="WAL is required for concurrent index writes + searches. "
                         "If this fails the temp drive may be a network share - set TEMP to a local disk.")
    except Exception as exc:
        return Check("SQLite WAL mode", False, f"{type(exc).__name__}: {exc}",
                     fix="Check that %TEMP% points at a writable local drive.")


# ---------------------------------------------------------------------------
# Work order 202626130120 (0t): the onnxruntime install must be exactly one
# thing, and this installation's DirectML provider must match intent.
# ---------------------------------------------------------------------------

_ONNXRUNTIME_DIST_RE = re.compile(
    r"^(onnxruntime|onnxruntime_directml)-([^-]+)\.dist-info$", re.IGNORECASE
)


def _onnxruntime_state(
    site_packages: Path,
) -> tuple[list[tuple[str, str]], list[str]]:
    """([(dist-info name, version)], stray "~" stash names).

    Pure and read-only: no import, no network, nothing but a directory
    listing - so a test can fabricate site_packages with tmp_path and never
    touch a real venv. site_packages is a parameter for exactly that reason.
    """
    found: list[tuple[str, str]] = []
    stashes: list[str] = []
    if not site_packages.is_dir():
        return found, stashes
    for entry in sorted(site_packages.iterdir()):
        if not entry.is_dir():
            continue
        lname = entry.name.lower()
        if lname.startswith("~") and "nnxruntime" in lname:
            # pip's own stash prefix for a package it could not finish
            # removing - see the module docstring's WinError 5 story.
            stashes.append(entry.name)
            continue
        match = _ONNXRUNTIME_DIST_RE.match(entry.name)
        if match:
            found.append((entry.name, match.group(2)))
    return found, stashes


def check_onnxruntime_integrity(site_packages: Optional[Path] = None) -> Check:
    """One coherent onnxruntime install, or this is a required failure.

    Work order 202626130120 (0t) section 5's first item, and the whole reason
    it says "fails, not merely narrates": onnxruntime and
    onnxruntime-directml unpack into the same directory and silently
    overwrite each other, and pip list cannot tell you which one actually
    won.

    **Two distributions are not automatically a fault.** Section 3's own fix
    installs onnxruntime-directml unconditionally, forever, alongside the
    plain wheel on any machine with a display adapter - so a healthy,
    correctly-pinned DirectML machine always carries two dist-info folders,
    and a check that failed on that would mean doctor could never report
    READY on the very hardware this order exists for. What actually signals
    the fault from the work order's own section 0 evidence is a **version
    mismatch** between them (1.30.0 next to 1.24.4) - that is pip having
    resolved the two wheels independently rather than as the matched pair
    install.ps1 now guarantees. Matching versions is the sanctioned state;
    differing versions, or a stash directory a failed uninstall left behind,
    is the fault - not a warning - because the binaries on disk cannot be
    trusted to match either .dist-info until it is repaired.
    """
    if site_packages is None:
        site_packages = Path(sysconfig.get_paths()["purelib"])
    found, stashes = _onnxruntime_state(site_packages)
    names = [name for name, _version in found]
    versions = {version for _name, version in found}

    repair_fix = (
        rf'Close Leasha first - a running process holding onnxruntime.dll is '
        rf'why this happens (WinError 5). Then delete the stray "~" '
        rf'directories under {site_packages} by hand, and run: '
        rf'"{sys.executable}" -m pip install --force-reinstall --no-deps '
        rf'onnxruntime-directml==1.24.4'
    )

    if stashes:
        return Check(
            "onnxruntime install is coherent", False,
            f"a failed uninstall left stash directories behind: {', '.join(stashes)}",
            fix=repair_fix,
        )
    if len(names) > 1 and len(versions) > 1:
        return Check(
            "onnxruntime install is coherent", False,
            f"two onnxruntime distributions at different versions are "
            f"installed at once: {', '.join(names)}",
            fix=repair_fix,
        )
    return Check(
        "onnxruntime install is coherent", True,
        ", ".join(names) if names else "not installed",
    )


def check_gpu_provider_intent(profile: Any = None) -> Check:
    """An adapter is present but this installation cannot use it for DirectML.

    Work order 202626130120 (0t) section 5's second item: `doctor` already
    prints the same facts as profile text under "Machine", which "cannot fail
    a run" - this is the same comparison as a proper Check that can WARN.
    Distinguished from *no adapter* (nothing to compare - not this
    installation's fault) and from a failed probe (`gpu_probe_failed` -
    "could not look" is not "it is gone"), the same distinction
    `app.index.backends.why_unavailable` draws for the identical reason.

    `profile` is injectable so a test can supply an invented machine; real
    callers leave it to detect() - itself read-only and quick, so this check
    keeps doctor's "never imports the models, never runs an index" promise.
    """
    try:
        if profile is None:
            from app.core.compute_profile import detect

            profile = detect(env_path("DATA_PATH") or None)
    except Exception as exc:
        return Check("GPU provider matches this machine", True,
                     f"could not be checked: {exc}", optional=True)

    if getattr(profile, "gpu_probe_failed", False):
        return Check("GPU provider matches this machine", True,
                     "the graphics card check did not run this time - not "
                     "reported as a loss", optional=True)
    if not getattr(profile, "gpus", ()):
        return Check("GPU provider matches this machine", True,
                     "no display adapter - nothing to compare", optional=True)
    if getattr(profile, "directml_available", False):
        return Check("GPU provider matches this machine", True,
                     "an adapter is present and DirectML is available",
                     optional=True)

    name = profile.gpus[0].name if profile.gpus else "the graphics card"
    return Check(
        "GPU provider matches this machine", False,
        f"a display adapter is present ({name}) but this installation has "
        f"no DirectML provider",
        fix=(rf'OPTIONAL - CPU is used instead and search still works. To '
             rf'use the graphics card: "{sys.executable}" -m pip install '
             rf'--force-reinstall --no-deps onnxruntime-directml==1.24.4'),
        optional=True,
    )


def index_privacy(data_path: str) -> str:
    r"""Whether the index sits somewhere private to this account, or shared.

    **Factual, and it does not judge.** A shared location is a legitimate
    choice - the owner's own install is `D:\Leasha\Data` and stays that way.
    What was missing is anybody being *told* which one they have, on a machine
    where two people share a login and one of them assumed otherwise.

    `%LOCALAPPDATA%` and a per-user profile are ACL'd by Windows to one
    account. Anything else - another drive, a `C:\ProgramData`, a network
    share - is readable by whoever can read that path.
    """
    if not data_path:
        return ""
    lowered = data_path.replace("/", "\\").lower().rstrip("\\")

    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        root = os.environ.get(key, "")
        if root and lowered.startswith(root.replace("/", "\\").lower().rstrip("\\")):
            return "private to this account"

    if sys.platform != "win32":
        home = os.path.expanduser("~").replace("/", "\\").lower().rstrip("\\")
        if home and lowered.startswith(home):
            return "private to this account"

    return "shared location"


def compute_profile_lines() -> list[str]:
    r"""The machine, verbatim, plus which backend that implies and why.

    **1b's whole point is that a wrong detection is visible before it can be
    argued with.** A profile printed only when something goes wrong is a
    profile nobody has read when it matters, so this prints on every run.

    Imported lazily: `doctor` is dependency-free by design and must work on a
    half-built venv, and `compute_profile` reaches for `psutil`.
    """
    try:
        from app.core.compute_profile import ComputeProfile, detect
    except Exception as exc:                     # noqa: BLE001
        return [f"  (the compute profile could not be read: {exc})"]

    try:
        profile = detect(env_path("DATA_PATH") or None)
    except Exception as exc:                     # noqa: BLE001
        return [f"  (detection failed: {exc})"]

    cores = f"{profile.physical_cores} cores / {profile.logical_processors} threads"
    if profile.hybrid:
        cores += (f"  ({profile.performance_cores}P + "
                  f"{profile.efficiency_cores}E - a thread on an E-core does "
                  f"a fraction of a P-core's work)")

    lines = [
        f"  CPU    {cores}",
        f"  RAM    {profile.ram_mb / 1024:.1f} GB" if profile.ram_mb
        else "  RAM    unknown",
        f"  AVX2   {'yes' if profile.avx2 else 'no'}",
        f"  Disk   index volume is {profile.index_disk or 'unknown'}",
    ]
    for gpu in profile.gpus:
        vram = f"{gpu.vram_mb:,} MB" if gpu.vram_mb else "VRAM unknown"
        usable = "DirectML available" if gpu.directml else "no DirectML provider"
        lines.append(f"  GPU    {gpu.name} - {vram}, {usable}")
    if not profile.gpus:
        lines.append("  GPU    none detected")

    lines.append(f"  Backend that would be chosen: {_backend_choice(profile)}")
    if profile.overridden:
        lines.append(f"  ** OVERRIDDEN by {os.environ.get('COMPUTE_PROFILE_OVERRIDE')} "
                     f"- these are not this machine's numbers **")
    if profile.unknowns:
        # **Said out loud.** A silent gap in a profile is a wrong number
        # waiting to be believed by everything that derives from it.
        lines.append("  Could not detect: " + ", ".join(profile.unknowns))
    lines.append(f"  Fingerprint: {profile.fingerprint()}")
    return lines


def _backend_choice(profile: Any) -> str:
    """Which embedding backend `auto` would pick here, and the reason.

    The reason matters more than the answer: somebody looking at a slow index
    run wants to know *why* it is on the CPU, and "no DirectML provider in this
    installation" and "no DX12 adapter" send them to different places.
    """
    if not profile.gpus:
        return "CPU (no display adapter detected)"
    if not profile.directml_available:
        return ("CPU (an adapter is present, but this installation has no "
                "DirectML provider - `pip install onnxruntime-directml`)")
    return ("GPU would be tried first, then measured against the CPU - see "
            "the index-tuning order's 5a")


def check_index_location() -> Check:
    """One line saying where the index is and who else can read it.

    Always passes: this reports, it does not gate. An installation on a shared
    drive is a choice somebody may have made deliberately, and `doctor` failing
    over it would be `doctor` having an opinion about the owner's own machine.
    """
    data_path = env_path("DATA_PATH")
    if not data_path:
        return Check("Index location", True, "not set yet")
    where = index_privacy(data_path)
    return Check("Index location", True, f"{data_path} ({where})")


def check_data_paths() -> list[Check]:
    out: list[Check] = []
    data_path = env_path("DATA_PATH")
    if not data_path:
        out.append(Check("Index folders exist and are writable", False,
                         "DATA_PATH is not set",
                         fix="Re-run install.ps1 - it writes DATA_PATH into .env."))
        return out

    # **Refuse a path from another platform rather than creating it.**
    #
    # A backslash and a colon are ordinary filename characters on Linux and
    # macOS, so `Path("D:\\Data\\vectors").mkdir(parents=True)` there does not
    # fail: it makes a directory whose *name* is a drive letter and a path, in
    # whatever folder happened to be current. That is how
    # `D:\KnowledgeGraphData` - with `cache`, `fts`, `models`, `state` and
    # `vectors` inside it - ended up sitting in the project folder, twice.
    #
    # `app/core/config.py` has had this guard for a while; `doctor` never went
    # through it, because it is deliberately dependency-free so it can run on a
    # half-built venv. Dependency-free is right; skipping the rule is not.
    if sys.platform != "win32" and re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", data_path):
        out.append(Check(
            "Index folders exist and are writable", False,
            f"DATA_PATH is '{data_path}', which is a Windows path, and this "
            f"is {sys.platform}",
            fix="Point DATA_PATH at a path for this platform, or run on "
                "Windows. Creating it here would make a folder whose name "
                "contains a drive letter and backslashes.",
        ))
        return out

    for sub in ("vectors", "fts", "cache", "models", "state"):
        p = Path(data_path) / sub
        try:
            # Created if absent, which is deliberate: `doctor` is also what
            # somebody runs after moving the index, and reporting "missing" for
            # a folder it could have made would send them to make it by hand.
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            out.append(Check(f"Writable: {p}", True))
        except Exception as exc:
            out.append(Check(
                f"Writable: {p}", False, f"{type(exc).__name__}: {exc}",
                fix=f"Create {p} manually and grant your user write access, "
                    f"or choose a different index location and re-run install.ps1.",
            ))
    return out


def required_free_gb() -> int:
    """Threshold shared with install.ps1 -RequiredFreeGB, via .env."""
    try:
        return int(float(env_setting("REQUIRED_FREE_GB")))
    except ValueError:
        return DEFAULT_REQUIRED_FREE_GB


def check_disk() -> Check:
    need = required_free_gb()
    data_path = env_path("DATA_PATH") or str(PROJECT_ROOT)
    target = Path(data_path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        free_gb = shutil.disk_usage(str(target)).free / 1e9
    except Exception as exc:
        return Check(f"Disk space on {data_path}", False, f"{type(exc).__name__}: {exc}",
                     fix="Confirm the index drive exists and is connected.")
    ok = free_gb >= need
    return Check(
        f"Disk space >= {need}GB on {target}", ok, f"{free_gb:.0f}GB free",
        fix=f"A 100GB corpus needs roughly {need}GB for vectors + FTS + cache + models. "
            f"Free space, point DATA_PATH at a bigger drive in .env, "
            f"or lower REQUIRED_FREE_GB in .env if you know the corpus is smaller.",
    )


def check_lancedb_roundtrip() -> Check:
    try:
        import lancedb  # noqa: F401
        with tempfile.TemporaryDirectory() as td:
            db = lancedb.connect(td)
            tbl = db.create_table(
                "probe",
                data=[{"id": 1, "vector": [0.1] * 384},
                      {"id": 2, "vector": [0.2] * 384}],
            )
            hits = tbl.search([0.1] * 384).limit(1).to_list()
            if not hits:
                raise RuntimeError("vector search returned no rows")
        return Check("LanceDB write + vector search", True)
    except Exception as exc:
        return Check(
            "LanceDB write + vector search", False, f"{type(exc).__name__}: {exc}",
            fix=rf'"{sys.executable}" -m pip install --force-reinstall lancedb==0.37.1',
        )


def model_cache_dir() -> str:
    """Where the models are: `MODEL_CACHE` if `.env` pins it, else `DATA_PATH\\models`.

    `.env` pins `DATA_PATH` and leaves `MODEL_CACHE` to derive from it
    (`app/core/config.py`). The two model checks read the key alone and fell
    back to `<project>\\models`. A working copy that had been installed into
    still had that folder; a fresh clone does not, so on 2026-09-30 `doctor`
    went online and fetched 150 MB into the project while the same models sat
    in the index folder. Stdlib only, like the rest of this file.
    """
    pinned = env_path("MODEL_CACHE")
    if pinned:
        return pinned
    data_path = env_path("DATA_PATH")
    if data_path:
        return str(Path(data_path) / "models")
    return str(PROJECT_ROOT / "models")


def check_embedding_model(quick: bool = False) -> Check:
    if quick:
        return Check("Embedding model (skipped: --quick)", True, optional=True)
    model_cache = model_cache_dir()
    model_name = env_setting("EMBED_MODEL")
    try:
        os.environ["FASTEMBED_CACHE_PATH"] = model_cache
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name, cache_dir=model_cache)
        vec = list(model.embed(["doctor probe"]))[0]
        expected = int(env_setting("EMBED_DIM"))
        if len(vec) != expected:
            raise RuntimeError(f"expected {expected} dimensions, got {len(vec)}")
        return Check("Embedding model loads and embeds", True, f"{model_name}, dim={len(vec)}")
    except Exception as exc:
        return Check(
            "Embedding model loads and embeds", False, f"{type(exc).__name__}: {exc}",
            fix="The first run needs internet once to download ~130MB into "
                f"{model_cache}; after that the app is fully offline. "
                "Check connectivity or your proxy, then re-run install.ps1.",
        )


# ---------------------------------------------------------------------------
# Checks - optional
# ---------------------------------------------------------------------------

def check_rerank_model(quick: bool = False) -> Check:
    if quick:
        return Check("Rerank model (skipped: --quick)", True, optional=True)
    model_cache = model_cache_dir()
    model_name = env_setting("RERANK_MODEL")
    try:
        os.environ["FASTEMBED_CACHE_PATH"] = model_cache
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        model = TextCrossEncoder(model_name, cache_dir=model_cache)
        list(model.rerank("probe query", ["probe document"]))
        return Check("Rerank model loads and scores", True, model_name, optional=True)
    except Exception as exc:
        return Check(
            "Rerank model", False, f"{type(exc).__name__}: {exc}",
            fix="OPTIONAL - reranking is a quality toggle in Settings and search works without it. "
                "Re-run install.ps1 to download it (~1.1GB).",
            optional=True,
        )


def check_onnx_models() -> list[Check]:
    """The models Leasha runs itself on ONNX Runtime (2026-09-29): downloaded or not.

    A disk look only - nothing is loaded and nothing is fetched. Each is
    optional: search works without any of them, and each says what it is for
    and where its Download button is. The chat model is listed only when it
    is the engine in use (`CHAT_ENGINE`).
    """
    try:
        from app.ort import hub
    except Exception as exc:                           # noqa: BLE001 - reported, never fatal
        return [Check("Models inside Leasha (ONNX)", False, f"{type(exc).__name__}: {exc}",
                      optional=True)]
    # The settings' own answer: MODEL_CACHE is usually derived from DATA_PATH,
    # not written in .env, so reading the key alone looks in the wrong place.
    try:
        from app.core.config import load_settings

        cache = str(load_settings(create_dirs=False, check_writable=False).model_cache)
    except Exception:                                  # noqa: BLE001
        cache = model_cache_dir()
    engine = (env_setting("CHAT_ENGINE") or "onnx").strip().lower()
    wanted = [(hub.FLORENCE, "photo tags and Describe are", "Settings, Models, photo model")]
    try:
        from app.ort.whisper import SIZES, resolve_size

        size = (env_setting("TRANSCRIBE_MODEL") or "base").strip()
        speech = SIZES.get(size, hub.WHISPER_BASE)
        found = resolve_size(size, [Path(cache), Path(cache) / "whisper"])
        if found is not None:
            speech = found[0]
    except Exception:                                  # noqa: BLE001 - never fatal
        speech = hub.WHISPER_BASE
    wanted.append((speech, "speech in recordings is", "Settings, Videos and recordings, speech"))
    if engine != "ollama":
        wanted.append((hub.QWEN_1_5B_Q4, "Chat and Interpret are", "Settings, Models, Chat model"))
    checks = []
    for model, purpose, where in wanted:
        either = {id(hub.FLORENCE): (hub.FLORENCE, hub.FLORENCE_INT8),
                  id(hub.QWEN_1_5B_Q4): (hub.QWEN_1_5B_Q4, hub.QWEN_1_5B)}
        folder = hub.resolve_any(either.get(id(model), (model,)), Path(cache))
        if folder is not None:
            checks.append(Check(f"{model.label} (ONNX)", True, "downloaded", optional=True))
        else:
            checks.append(Check(
                f"{model.label} (ONNX)", False, f"not downloaded - {purpose} off until it is",
                fix=f"OPTIONAL - {where}, Download (about {model.approx_mb} MB). "
                    "Search works without it.",
                optional=True))
    return checks


def check_resources() -> Check:
    """Is the resource governor actually able to govern?

    Without psutil the disk guard and the worker cap still apply, but the
    memory, CPU and battery ceilings silently do nothing - and a limit that
    silently does nothing is worse than no limit, because it is believed.
    """
    from app.index.resources import default_workers, psutil_available

    active = psutil_available()
    return Check(
        name="resource governor",
        ok=active,
        optional=True,
        detail=(
            f"active - memory, CPU and battery ceilings apply; "
            f"default workers {default_workers()}"
            if active else
            "psutil is missing, so the memory, CPU and battery ceilings are INACTIVE. "
            "Disk space and the worker cap still apply."
        ),
        fix="" if active else r"venv\Scripts\python.exe -m pip install psutil",
    )


def _pending_counts_by_ext() -> dict[str, int]:
    """How many files of each extension are indexed by name only, right now.

    §5b: the count that goes in the `.dwg` row - "N files waiting" beats a bare
    "install this" the moment somebody is deciding whether the fix is worth
    doing today. Read from the index itself (the `files` table, the existing
    authority per non-negotiable 6), never guessed and never a separate
    counting pass - one `GROUP BY` over a table that already exists.

    Returns `{}` on anything short of success: no `.env`, no index built yet,
    a store that will not open. `check_file_formats` must not lose the rows it
    already had over a number it could not get - this is a nicety, not a
    requirement, and the format-health row still reads correctly without it.
    """
    try:
        from app.core.config import load_settings
        from app.storage.sqlite_store import FileStatus, SqliteStore

        # `create_dirs` stays at its default: `Settings` validates that
        # `vectors`, `fts`, `cache`, `models` and `state` exist, and
        # `check_data_paths` a few checks below makes exactly these same
        # empty folders for exactly this reason - see its own comment. Making
        # them here changes nothing `check_data_paths` would not already have
        # made. `check_writable` is skipped: this only ever reads, per
        # non-negotiable 10, and has no reason to prove a folder is writable.
        settings = load_settings(check_writable=False)
        if not settings.fts_db.is_file():
            # No index has ever been built here - not opening `SqliteStore`
            # is what keeps it that way; opening it would create an empty
            # database and migrate it, which is one more thing than a doctor
            # run should leave behind.
            return {}
        with SqliteStore(settings.fts_db) as store:
            rows = store.conn.execute(
                "SELECT ext, COUNT(*) AS n FROM files WHERE status = ? "
                "GROUP BY ext", (FileStatus.NAME_ONLY,),
            ).fetchall()
        return {f".{row['ext']}": int(row["n"]) for row in rows if row["ext"]}
    except Exception:                                          # noqa: BLE001
        return {}


def check_file_formats() -> list[Check]:
    """One check per file type that cannot currently read its files.

    **Replaces three hand-written probes** (OCR, Visio/Project, converters),
    each of which had to be written and remembered separately. This is driven by
    what the extractors themselves declare, so a format added later - with its
    own library - is reported here and in Settings without either being edited.
    That is the whole point: adding a file type should be one commit, not four.

    Healthy formats collapse into a single summary line. A list of forty PASS
    rows is a list nobody reads, and the two that matter hide in it.
    """
    try:
        from app.core.format_health import BLOCKED, DEGRADED, format_health, summarise
        from app.core.formats import load_rules

        data_path = env_path("DATA_PATH")
        rules = load_rules(Path(data_path) if data_path else None)
        statuses = format_health(rules, counts=_pending_counts_by_ext())
    except Exception as exc:                                  # noqa: BLE001
        return [Check(
            "File type health", False, f"{type(exc).__name__}: {exc}",
            fix="Could not read the file-type configuration. Delete "
                "extractors.toml in your index folder to restore the shipped "
                "defaults - nothing else is lost by removing it.",
            optional=True,
        )]

    counts = summarise(statuses)
    checks: list[Check] = [Check(
        "File types ready", True,
        detail=(
            f"{counts['ready']} ready, {counts['degraded']} degraded, "
            f"{counts['blocked']} blocked, {counts['off']} switched off"
        ),
        optional=True,
    )]

    # Only the ones that need a decision. Blocked first: those read nothing.
    for status in statuses:
        if status.state not in (BLOCKED, DEGRADED):
            continue
        checks.append(Check(
            f"  {status.extension} ({status.reader})",
            ok=False,
            detail=status.detail,
            fix=status.fix or "No fix available - this is the best this format can do.",
            optional=True,
        ))
    return sorted(checks, key=lambda c: (c.ok, c.name))


def check_converters() -> Check:
    """Which Tier 2 converter binaries are installed.

    Optional by nature: every converter ships disabled, and a machine with none
    of them simply indexes fewer formats. Reported so Settings can offer to
    enable exactly the ones that would work - switching on a format whose binary
    is absent means it fails on every file.
    """
    from app.extract.converter import available_binaries

    found = available_binaries()
    present = [name for name, path in found.items() if path]
    return Check(
        "Document converters (optional)",
        True,
        # **Name the formats that actually depend on this.** It used to say
        # ".doc, .xls, .ppt and friends", which stopped being true when .xls
        # and .rtf moved to xlrd and striprtf - and an over-broad warning is one
        # people install software they do not need in order to silence.
        detail=(", ".join(present) if present else
                "none installed - .doc, .ppt, .pub, .wpd and Apple iWork files "
                "will be indexed by name only. Everything else is read without it"),
        optional=True,
        fix=("Install LibreOffice to read .doc, .ppt and other pre-2007 Office "
             "formats: https://www.libreoffice.org/download/" if not present else ""),
    )


def check_git() -> Check:
    """Git on PATH, which the release process assumes and nothing else checks.

    Optional, because the app runs perfectly without it: `build_info()` degrades
    to a version with no commit. But every convention in docs/VERSIONING.md -
    branch per layer, conventional commits, tags per release - silently stops
    working, and the failure looks like "git is not recognized" long after the
    installer said it was done.

    The usual cause is not a missing install: `install.ps1` installs Git via
    winget, but a PowerShell window opened *before* that inherits the old PATH
    and keeps it until it is closed. So the fix leads with refreshing PATH.
    """
    import subprocess

    location = shutil.which("git")
    if location is None:
        return Check(
            "Git on PATH", False, "not found",
            fix=(
                "Usually a stale PATH rather than a missing install: a PowerShell window opened "
                "before Git was installed keeps the old PATH until it is closed. Refresh it in "
                "place with:  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') "
                "+ ';' + [Environment]::GetEnvironmentVariable('Path','User')   - or just open a "
                "new terminal. If it is genuinely absent:  winget install --id Git.Git -e"
            ),
            optional=True,
        )

    try:
        version = subprocess.run(
            [location, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except Exception as exc:                              # noqa: BLE001
        return Check("Git on PATH", False, f"{type(exc).__name__}: {exc}",
                     fix="Git is on PATH but would not run. Reinstall: winget install --id Git.Git -e",
                     optional=True)

    return Check("Git on PATH", True, f"{version} at {location}", optional=True)


def new_outlook_present() -> bool:
    """True if the 'new Outlook' web app is installed.

    Worth knowing because it is a trap, not a help: new Outlook has no COM or
    MAPI automation and cannot open .pst files at all. Someone who has it and
    reads "Outlook not found" will reasonably conclude the check is broken.
    """
    if sys.platform != "win32":
        return False
    local = os.environ.get("LOCALAPPDATA", "")
    if local and Path(local, "Microsoft", "WindowsApps", "olk.exe").exists():
        return True
    return shutil.which("olk") is not None


def check_pst_direct() -> Check:
    """Whether archives can be read without Outlook.

    Optional either way - between this and Outlook, at least one route usually
    exists - but which one you have changes what the app can do, so it is worth
    stating plainly rather than discovering mid-index.
    """
    try:
        import pypff

        return Check("Direct .pst reading (libpff)", True,
                     f"pypff {pypff.get_version()}", optional=True)
    except ImportError:
        return Check(
            "Direct .pst reading (libpff)", False, "not installed",
            fix=("OPTIONAL. Without it, .pst archives are read through classic Outlook, which "
                 "must be installed and holds a lock on the file while reading. To read "
                 "archives directly instead: pip install libpff-python - on Windows this "
                 "compiles and needs Build Tools for Visual Studio. The live mailbox (.ost) "
                 "needs Outlook either way."),
            optional=True,
        )


def classic_outlook_registered() -> bool:
    r"""True if classic Outlook's automation object is registered on this machine.

    **2026-09-30: read from the registry, never by starting Outlook.** This
    check used `win32com.client.Dispatch("Outlook.Application")`, which *starts*
    Outlook when it is not running. Settings' health check runs `doctor.py
    --json --quick`, and so does the test suite, so Outlook kept opening on the
    owner's laptop by itself - and an Outlook attached to the archives is what
    locks them against direct reading. `HKEY_CLASSES_ROOT\Outlook.Application
    \CLSID` is what `Dispatch` itself looks up; it exists exactly when classic
    Outlook is installed.
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID") as key:
            return bool(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return False


def check_outlook() -> Check:
    if sys.platform != "win32":
        return Check("Outlook MAPI (PST)", False, "not Windows",
                     fix="OPTIONAL - PST indexing is Windows-only.", optional=True)
    try:
        if not classic_outlook_registered():
            raise LookupError("Outlook.Application is not registered")
        return Check("Classic Outlook MAPI available (PST ingestion)", True, optional=True)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if new_outlook_present():
            detail += "  -- 'new Outlook' IS installed, but it cannot do this"
            fix = (
                "OPTIONAL, but note the trap: you have the NEW Outlook, which is a web app with "
                "no COM or MAPI automation and no .pst support whatsoever. It cannot index mail "
                "however it is configured. Switch to CLASSIC Outlook with the toggle in its "
                "top-right corner (installing it if necessary), then re-run. Every other file "
                "type indexes normally either way."
            )
        else:
            fix = (
                "OPTIONAL - needed only to index .pst archives; every other file type indexes "
                "normally. Install CLASSIC Outlook - the new Outlook is a web app and cannot "
                "open .pst files - or convert PST to EML with XstReader and index that folder. "
                "Do NOT substitute extract-msg: it reads .msg files only, not .pst."
            )
        return Check("Classic Outlook MAPI (PST ingestion)", False, detail, fix=fix, optional=True)


def check_ollama() -> Check:
    url = env_setting("OLLAMA_URL").rstrip("/")
    try:
        import urllib.request
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "?") for m in payload.get("models", [])]
        want = env_setting("OLLAMA_MODEL")
        has_want = any(m.split(":")[0] == want.split(":")[0] for m in models)
        if not has_want:
            return Check(
                f"Ollama reachable but '{want}' not pulled", False,
                f"installed: {', '.join(models) if models else 'none'}",
                fix=f"OPTIONAL - search works without it. Run: ollama pull {want}",
                optional=True,
            )
        return Check("Ollama reachable with model", True, ", ".join(models), optional=True)
    except Exception as exc:
        return Check(
            "Ollama (AI answers, entity extraction)", False, f"not reachable at {url} ({type(exc).__name__})",
            fix="OPTIONAL - search never calls the LLM, so this only disables AI answers and "
                "LLM entity extraction (the graph falls back to co-occurrence). "
                "To enable: winget install --id Ollama.Ollama -e, make sure the Ollama service is running, "
                "then: ollama pull mistral",
            optional=True,
        )


def check_nightly_status() -> Check:
    """Order 0m section 5b: "`doctor` shows when the nightly last passed."

    Reads `logs/nightly.log`'s last line rather than re-running anything -
    `tools/nightly.py` is the owner's own opt-in scheduled task, and this
    only ever reports what it already found. Optional, because a machine
    that has never opted into the nightly task has never had a chance to
    produce this file at all - that is a normal state, not a failure.
    """
    log_path = PROJECT_ROOT / "logs" / "nightly.log"
    if not log_path.is_file():
        return Check(
            "Nightly system loop", False, "never run on this machine",
            fix="OPTIONAL - opt in with the installer's nightly-task step, or run by hand: "
                r"venv\Scripts\python.exe tools\nightly.py",
            optional=True,
        )
    try:
        last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    except (OSError, IndexError) as exc:
        return Check("Nightly system loop", False, f"log unreadable ({exc})", optional=True)

    passed = " PASS " in last_line
    return Check(f"Nightly system loop last {'passed' if passed else 'failed'}",
                passed, last_line, optional=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all(quick: bool = False, machine: Optional[list[str]] = None) -> list[Check]:
    """Every check, in order. `machine` collects the profile lines instead of
    printing them - `--json` hands it one, because anything printed before the
    JSON made the output unreadable to the window's health check (2026-09-30:
    `ERR_UNEXPECTED ... exited 0 without valid JSON`, on every run)."""
    checks: list[Check] = [
        check_python(),
        check_in_venv(),
        check_platform(),
        check_env_file(),
    ]
    checks += check_packages()
    checks.append(check_pywin32())
    checks.append(check_converters())
    checks += check_file_formats()
    checks += [
        check_fts5(),
        check_sqlite_wal(),
        # Work order 202626130120 (0t) section 5: required, because two
        # onnxruntime distributions (or a failed-uninstall stash) mean the
        # binaries on disk cannot be trusted, whether or not this machine has
        # a graphics card at all.
        check_onnxruntime_integrity(),
    ]
    # The machine, before the checks that depend on it - so a wrong detection
    # is read first rather than inferred from a surprising result below.
    profile = compute_profile_lines()
    if machine is None:
        print()
        print("Machine")
        for line in profile:
            print(line)
    else:
        machine.extend(profile)
    # Optional: an adapter present with no DirectML provider is a WARN, not a
    # print-only line under "Machine" above that "cannot fail a run" - see
    # the function's own docstring for the three-way distinction.
    checks.append(check_gpu_provider_intent())
    checks.append(check_index_location())
    checks += check_data_paths()
    checks += [
        check_disk(),
        check_lancedb_roundtrip(),
        check_embedding_model(quick),
        check_rerank_model(quick),
        *check_onnx_models(),
        check_git(),
        check_resources(),
        check_pst_direct(),
        check_outlook(),
        check_ollama(),
        check_nightly_status(),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Leasha environment.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--quick", action="store_true", help="skip model loading (much faster)")
    args = parser.parse_args()

    machine: list[str] = []
    checks = run_all(quick=args.quick, machine=machine if args.json else None)
    hard_failures = [c for c in checks if not c.ok and not c.optional]
    soft_failures = [c for c in checks if not c.ok and c.optional]

    if args.json:
        print(json.dumps({
            "ready": not hard_failures,
            "required_failures": len(hard_failures),
            "optional_failures": len(soft_failures),
            "machine": machine,
            "checks": [asdict(c) for c in checks],
        }, indent=2))
        return 1 if hard_failures else 0

    width = max(len(c.name) for c in checks) + 2
    for c in checks:
        if c.ok:
            mark = "PASS"
        elif c.optional:
            mark = "WARN"
        else:
            mark = "FAIL"
        line = f"[{mark}] {c.name.ljust(width)}"
        if c.detail:
            line += f" {c.detail}"
        print(line)
        if not c.ok:
            print(f"       FIX: {c.fix}")

    print()
    if hard_failures:
        print(f"NOT READY - {len(hard_failures)} required check(s) failed.")
        for c in hard_failures:
            print(f"  \u00b7 {c.name}")
        print("Apply the FIX lines above, then re-run doctor.py.")
        return 1

    # Bullets are written with a middle dot rather than a hyphen. Terminal
    # output gets pasted back into the shell far more often than anyone admits,
    # and a line starting with "-" is a PowerShell parse error about a unary
    # operator - which reads as a crash in this program rather than a paste
    # accident. The dot is inert.
    if soft_failures:
        print(f"READY - with {len(soft_failures)} optional component(s) unavailable:")
        for c in soft_failures:
            print(f"  \u00b7 {c.name}")
            if c.fix:
                print(f"      to enable: {c.fix}")
        print("These degrade features, not core search.")
    else:
        print("READY - everything verified, including optional components.")

    # Say what to type. "Start the app" leaves the reader to guess, and the
    # guess is usually to paste this message back into the prompt.
    print()
    print("Start it with:")
    print(r"  venv\Scripts\python.exe -m app.main        (the window)")
    print(r"  venv\Scripts\python.exe -m app.cli --help  (the command line)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
