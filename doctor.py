#!/usr/bin/env python3
"""
doctor.py - Local Knowledge Graph V2 environment verification.

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
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, asdict
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
    ("networkx", "networkx"),
    ("pyvis", "pyvis"),
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


def check_data_paths() -> list[Check]:
    out: list[Check] = []
    data_path = env_path("DATA_PATH")
    if not data_path:
        out.append(Check("Index folders exist and are writable", False,
                         "DATA_PATH is not set",
                         fix="Re-run install.ps1 - it writes DATA_PATH into .env."))
        return out

    for sub in ("vectors", "fts", "cache", "models", "state"):
        p = Path(data_path) / sub
        try:
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
        return int(float(env_path("REQUIRED_FREE_GB", str(DEFAULT_REQUIRED_FREE_GB))))
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
    model_name = env_path("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    try:
        os.environ["FASTEMBED_CACHE_PATH"] = model_cache
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name, cache_dir=model_cache)
        vec = list(model.embed(["doctor probe"]))[0]
        expected = int(env_path("EMBED_DIM", "384"))
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
    model_name = env_path("RERANK_MODEL", "BAAI/bge-reranker-base")
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


def check_outlook() -> Check:
    if sys.platform != "win32":
        return Check("Outlook MAPI (PST)", False, "not Windows",
                     fix="OPTIONAL - PST indexing is Windows-only.", optional=True)
    try:
        import win32com.client
        win32com.client.Dispatch("Outlook.Application")
        return Check("Outlook MAPI available (PST ingestion)", True, optional=True)
    except Exception as exc:
        return Check(
            "Outlook MAPI (PST ingestion)", False, f"{type(exc).__name__}: {exc}",
            fix="OPTIONAL - needed only to index .pst archives; every other file type indexes normally. "
                "Either install Outlook, or convert PST to EML with XstReader and index the EML folder. "
                "Do NOT substitute extract-msg: it reads .msg files only, not .pst.",
            optional=True,
        )


def check_ollama() -> Check:
    url = env_path("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        import urllib.request
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "?") for m in payload.get("models", [])]
        want = env_path("OLLAMA_MODEL", "mistral")
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
    checks += [
        check_fts5(),
        check_sqlite_wal(),
    ]
    checks += check_data_paths()
    checks += [
        check_disk(),
        check_lancedb_roundtrip(),
        check_embedding_model(quick),
        check_rerank_model(quick),
        check_outlook(),
        check_ollama(),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Local Knowledge Graph V2 environment.")
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
            mark, colour = "PASS", ""
        elif c.optional:
            mark, colour = "WARN", ""
        else:
            mark, colour = "FAIL", ""
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
            print(f"  - {c.name}")
        print("Apply the FIX lines above, then re-run doctor.py.")
        return 1

    if soft_failures:
        print(f"READY - with {len(soft_failures)} optional component(s) unavailable:")
        for c in soft_failures:
            print(f"  - {c.name}")
        print("These degrade features, not core search. Start the app.")
    else:
        print("READY - everything verified, including optional components. Start the app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
