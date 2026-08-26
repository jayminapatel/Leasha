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
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

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
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return Check(
        "Running inside the project venv", in_venv,
        f"prefix={sys.prefix}",
        fix=r"Run it as: venv\Scripts\python.exe doctor.py  (not a system python)",
    )


def check_platform() -> Check:
    ok = sys.platform == "win32"
    return Check(
        "Windows platform", ok, sys.platform,
        fix="This build targets Windows 10/11 only - PST ingestion via Outlook MAPI has no cross-platform equivalent.",
    )


PACKAGES = [
    ("PyQt6.QtCore", "PyQt6"),
    ("lancedb", "lancedb"),
    ("fastembed", "fastembed"),
    ("pymupdf", "pymupdf"),
    ("docx", "python-docx"),
    ("openpyxl", "openpyxl"),
    ("pptx", "python-pptx"),
    ("diskcache", "diskcache"),
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


def check_embedding_model(quick: bool = False) -> Check:
    if quick:
        return Check("Embedding model (skipped: --quick)", True, optional=True)
    model_cache = env_path("MODEL_CACHE") or str(PROJECT_ROOT / "models")
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
    model_cache = env_path("MODEL_CACHE") or str(PROJECT_ROOT / "models")
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
        statuses = format_health(rules)
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


def check_outlook() -> Check:
    if sys.platform != "win32":
        return Check("Outlook MAPI (PST)", False, "not Windows",
                     fix="OPTIONAL - PST indexing is Windows-only.", optional=True)
    try:
        import win32com.client
        win32com.client.Dispatch("Outlook.Application")
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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all(quick: bool = False) -> list[Check]:
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
    ]
    checks.append(check_index_location())
    checks += check_data_paths()
    checks += [
        check_disk(),
        check_lancedb_roundtrip(),
        check_embedding_model(quick),
        check_rerank_model(quick),
        check_git(),
        check_resources(),
        check_pst_direct(),
        check_outlook(),
        check_ollama(),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Leasha environment.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--quick", action="store_true", help="skip model loading (much faster)")
    args = parser.parse_args()

    checks = run_all(quick=args.quick)
    hard_failures = [c for c in checks if not c.ok and not c.optional]
    soft_failures = [c for c in checks if not c.ok and c.optional]

    if args.json:
        print(json.dumps({
            "ready": not hard_failures,
            "required_failures": len(hard_failures),
            "optional_failures": len(soft_failures),
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
