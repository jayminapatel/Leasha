"""The diagnostic bundle.

A diagnostic tool that fails when things are broken is worse than useless, so
the central property under test is: it produces a bundle even when the thing it
is describing is in pieces.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.core.config import load_settings
from app.core.diagnostics import build_bundle, collect_sections
from app.core.logging import LOG_SUBDIRS, ensure_log_dirs


def test_log_folders_are_created_with_a_readme(tmp_path: Path) -> None:
    dirs = ensure_log_dirs(tmp_path / "logs")
    for name in LOG_SUBDIRS:
        assert dirs[name].is_dir()
    readme = (tmp_path / "logs" / "README.txt").read_text(encoding="utf-8")
    assert "diagnose" in readme
    for name in LOG_SUBDIRS:
        assert name in readme


def test_bundle_contains_the_expected_parts(temp_env: Path, project_root: Path) -> None:
    settings = load_settings(temp_env)
    bundle = build_bundle(settings, project_root)

    assert bundle.is_file()
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert "report.json" in names
        assert "summary.txt" in names
        assert "env.txt" in names

        report = json.loads(archive.read("report.json"))
        for section in ("version", "system", "config", "disk", "stores", "packages", "git"):
            assert section in report

        summary = archive.read("summary.txt").decode("utf-8")
        assert "diagnostic summary" in summary


def test_bundle_does_not_nest_previous_bundles(temp_env: Path, project_root: Path) -> None:
    settings = load_settings(temp_env)
    build_bundle(settings, project_root)
    second = build_bundle(settings, project_root)

    with zipfile.ZipFile(second) as archive:
        assert not [n for n in archive.namelist() if n.endswith(".zip")], \
            "a bundle nested inside a bundle grows without adding information"


def test_collection_survives_a_broken_section(temp_env: Path, project_root: Path) -> None:
    """The failure of one section is recorded, not propagated."""
    settings = load_settings(temp_env)

    class Exploding:
        """Stands in for settings whose every attribute access fails."""

        def __getattr__(self, name: str):
            raise RuntimeError(f"simulated failure reading {name}")

    sections = collect_sections(Exploding(), project_root)

    assert "_collection_failed" in sections["config"]
    # Sections that do not touch settings still succeed, so a bundle from a
    # badly broken install still carries useful information.
    assert "platform" in sections["system"]
    assert isinstance(sections["packages"], dict)


def test_bundle_written_to_an_explicit_path(temp_env: Path, project_root: Path, tmp_path: Path) -> None:
    settings = load_settings(temp_env)
    target = tmp_path / "custom" / "bundle.zip"
    result = build_bundle(settings, project_root, out_path=target)
    assert result == target
    assert target.is_file()


def test_large_logs_are_truncated_not_dropped(temp_env: Path, project_root: Path) -> None:
    """A 50MB debug log should contribute its tail, not nothing and not 50MB."""
    from app.core.diagnostics import MAX_LOG_BYTES

    settings = load_settings(temp_env)
    dirs = ensure_log_dirs(Path(settings.log_path))
    noisy = dirs["app"] / "huge.log"
    noisy.write_bytes(b"x" * (MAX_LOG_BYTES + 500_000) + b"THE-IMPORTANT-LAST-LINE")

    bundle = build_bundle(settings, project_root)
    with zipfile.ZipFile(bundle) as archive:
        content = archive.read("logs/app/huge.log")
        assert b"THE-IMPORTANT-LAST-LINE" in content, "the tail is the useful part"
        assert b"truncated" in content
        assert len(content) < MAX_LOG_BYTES + 1000
