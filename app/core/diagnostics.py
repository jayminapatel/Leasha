"""Diagnostic bundle: everything needed to troubleshoot, in one file.

Layer: L0

When something goes wrong, the useful information is scattered across the
environment report, the configuration, both store summaries, the recent logs and
the installed package versions. Gathering it by hand is error-prone and tedious.
`app.cli diagnose` collects the lot into a single zip under logs/diagnostics/.

Nothing here should ever raise. A diagnostic tool that fails when things are
broken is worse than useless, so every section is collected inside its own guard
and records its own failure into the bundle.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["build_bundle", "collect_sections"]

#: How many of the most recent files to take from each log folder.
RECENT_LOGS_PER_FOLDER = 3

#: Cap on any single log file copied into the bundle. A 10MB debug log is not
#: more useful than its last megabyte, and a huge zip is hard to share.
MAX_LOG_BYTES = 1_000_000


def _guarded(name: str, collect: Callable[[], Any]) -> Any:
    """Run one collector, converting any failure into a recorded value.

    The failure of a section is itself diagnostic information.
    """
    try:
        return collect()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail
        return {"_collection_failed": f"{type(exc).__name__}: {exc}"}


def _system() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "cwd": str(Path.cwd()),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def _packages() -> dict[str, str]:
    """Installed versions of the packages this app depends on."""
    from importlib import metadata

    wanted = [
        "PyQt6", "lancedb", "fastembed", "pymupdf", "python-docx", "openpyxl",
        "python-pptx", "pywin32", "diskcache", "networkx", "pyvis", "pydantic",
        "python-dotenv", "loguru", "tqdm", "requests", "pyarrow", "numpy",
        "onnxruntime", "pytest",
    ]
    found: dict[str, str] = {}
    for name in wanted:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = "NOT INSTALLED"
    return found


def _git(project_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return (result.stdout or result.stderr).strip()

    return {
        "describe": run("describe", "--tags", "--always", "--dirty"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "status": run("status", "--short"),
        "last_commits": run("log", "--oneline", "-5"),
    }


def _config(settings: Any) -> dict[str, Any]:
    return settings.describe()


def _stores(settings: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}

    if Path(settings.fts_db).is_file():
        from app.storage.sqlite_store import SqliteStore

        with SqliteStore(settings.fts_db) as store:
            out["sqlite"] = store.stats()
            out["sqlite"]["integrity_check"] = store.integrity_check()
            out["sqlite"]["cursors"] = store.all_state()
    else:
        out["sqlite"] = {"_absent": f"{settings.fts_db} does not exist yet"}

    from app.storage.vector_store import VectorStore

    with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        out["vectors"] = vectors.stats()

    return out


def _disk(settings: Any) -> dict[str, Any]:
    import shutil

    out: dict[str, Any] = {}
    for label, path in (("data_path", settings.data_path),
                        ("project_path", settings.project_path)):
        target = Path(path)
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            usage = shutil.disk_usage(str(target))
            out[label] = {
                "measured_at": str(target),
                "free_gb": round(usage.free / 1e9, 1),
                "total_gb": round(usage.total / 1e9, 1),
            }
        except OSError as exc:
            out[label] = {"error": str(exc)}
    return out


def _doctor(project_root: Path) -> Any:
    """Run doctor.py and capture its JSON report."""
    doctor = project_root / "doctor.py"
    if not doctor.is_file():
        return {"_absent": f"{doctor} not found"}
    result = subprocess.run(
        [sys.executable, str(doctor), "--json", "--quick"],
        capture_output=True, text=True, timeout=180, check=False,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
                "returncode": result.returncode}


def collect_sections(settings: Any, project_root: Path) -> dict[str, Any]:
    """Every section, each independently guarded."""
    from app.core.version import build_info

    return {
        "version": _guarded("version", build_info),
        "system": _guarded("system", _system),
        "config": _guarded("config", lambda: _config(settings)),
        "disk": _guarded("disk", lambda: _disk(settings)),
        "stores": _guarded("stores", lambda: _stores(settings)),
        "packages": _guarded("packages", _packages),
        "git": _guarded("git", lambda: _git(project_root)),
        "doctor": _guarded("doctor", lambda: _doctor(project_root)),
    }


#: Never gathered into a bundle. Nesting old bundles inside a new one makes it
#: grow without adding information.
SKIP_FOLDERS = {"diagnostics"}


def _recent_logs(log_dir: Path) -> list[Path]:
    """The newest few files from each log folder, plus anything loose."""
    collected: list[Path] = []
    root = Path(log_dir)
    if not root.is_dir():
        return collected

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if folder.name in SKIP_FOLDERS:
            continue
        files = sorted(
            (f for f in folder.iterdir() if f.is_file() and f.suffix != ".zip"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        collected.extend(files[:RECENT_LOGS_PER_FOLDER])

    collected.extend(f for f in root.iterdir() if f.is_file() and f.suffix in {".log", ".txt"})
    return collected


def _tail_bytes(path: Path, limit: int = MAX_LOG_BYTES) -> bytes:
    """The last `limit` bytes of a file. Recent lines are the useful ones."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
            return b"[... truncated: showing the last %d of %d bytes ...]\n" % (limit, size) + handle.read()
        return handle.read()


def build_bundle(
    settings: Any,
    project_root: Path,
    *,
    out_path: Optional[Path] = None,
) -> Path:
    """Write a diagnostic zip and return its path."""
    from app.core.logging import ensure_log_dirs

    dirs = ensure_log_dirs(Path(settings.log_path))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(out_path) if out_path else dirs["diagnostics"] / f"diagnostics-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)

    sections = collect_sections(settings, project_root)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("report.json", json.dumps(sections, indent=2, default=str))
        bundle.writestr("summary.txt", _summary(sections))

        for log_file in _recent_logs(Path(settings.log_path)):
            try:
                relative = log_file.relative_to(Path(settings.log_path))
            except ValueError:
                relative = Path(log_file.name)
            try:
                bundle.writestr(f"logs/{relative.as_posix()}", _tail_bytes(log_file))
            except OSError as exc:
                bundle.writestr(f"logs/{relative.as_posix()}.unreadable.txt", str(exc))

        env_file = getattr(settings, "env_file", None)
        if env_file and Path(env_file).is_file():
            try:
                bundle.writestr("env.txt", Path(env_file).read_text(encoding="utf-8-sig"))
            except OSError as exc:
                bundle.writestr("env.txt", f"unreadable: {exc}")

    return target


def _summary(sections: dict[str, Any]) -> str:
    """A short human-readable header, so the zip is useful without tooling."""
    lines = ["Local Knowledge Graph V2 - diagnostic summary", "=" * 45, ""]

    version = sections.get("version", {})
    if isinstance(version, dict):
        lines.append(f"version   : {version.get('version', '?')}")
        if version.get("git"):
            lines.append(f"git       : {version['git']}")

    system = sections.get("system", {})
    if isinstance(system, dict):
        lines.append(f"platform  : {system.get('platform', '?')}")
        lines.append(f"python    : {str(system.get('python', '?')).splitlines()[0]}")
        lines.append(f"in venv   : {system.get('in_venv', '?')}")

    doctor = sections.get("doctor", {})
    if isinstance(doctor, dict) and "ready" in doctor:
        lines.append("")
        lines.append(f"doctor    : {'READY' if doctor['ready'] else 'NOT READY'}"
                     f"  ({doctor.get('required_failures', '?')} required, "
                     f"{doctor.get('optional_failures', '?')} optional failures)")
        for check in doctor.get("checks", []):
            if not check.get("ok"):
                kind = "WARN" if check.get("optional") else "FAIL"
                lines.append(f"  [{kind}] {check.get('name')}: {check.get('detail', '')}")

    stores = sections.get("stores", {})
    if isinstance(stores, dict):
        sqlite_stats = stores.get("sqlite", {})
        vector_stats = stores.get("vectors", {})
        lines.append("")
        if isinstance(sqlite_stats, dict) and "files_total" in sqlite_stats:
            lines.append(f"files     : {sqlite_stats.get('files_total')}  {sqlite_stats.get('files')}")
            lines.append(f"chunks    : {sqlite_stats.get('chunks_total')} "
                         f"({sqlite_stats.get('chunks_embedded')} embedded)")
            lines.append(f"integrity : {sqlite_stats.get('integrity_check')}")
            if sqlite_stats.get("skipped_by_code"):
                lines.append(f"skipped   : {sqlite_stats['skipped_by_code']}")
        if isinstance(vector_stats, dict):
            lines.append(f"vectors   : {vector_stats.get('rows', '?')} rows, "
                         f"dim {vector_stats.get('dim', '?')}")

    lines += ["", "Full detail in report.json; recent logs under logs/.", ""]
    return "\n".join(lines)
