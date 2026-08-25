"""The diagnostic bundle.

A diagnostic tool that fails when things are broken is worse than useless, so
the central property under test is: it produces a bundle even when the thing it
is describing is in pieces.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

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


def test_env_in_bundle_is_redacted(temp_env: Path, project_root: Path) -> None:
    """A secret added to `.env` must never reach a bundle, and unknown keys
    are redacted by default - the allowlist is opt-in, not opt-out."""
    settings = load_settings(temp_env)
    env_path = Path(settings.env_file)
    env_text = env_path.read_text(encoding="utf-8-sig")
    env_path.write_text(
        env_text
        + "\n# my token: hunter2-in-a-comment\n"
        + "SOME_FUTURE_TOKEN=hunter2\n"
        + "MYSTERY_SETTING=surprising\n",
        encoding="utf-8",
    )

    bundle = build_bundle(settings, project_root)
    with zipfile.ZipFile(bundle) as archive:
        content = archive.read("env.txt").decode("utf-8")

    assert "hunter2" not in content, "secret value leaked into the bundle"
    assert "surprising" not in content, "unknown keys must be redacted by default"
    assert "SOME_FUTURE_TOKEN=<redacted>" in content, "key names should survive"
    assert "MYSTERY_SETTING=<redacted>" in content
    # Allowlisted, non-sensitive settings keep their values - that is the
    # diagnostic point of shipping the file at all.
    assert "EMBED_MODEL=" in content and "<redacted>" not in content.split("EMBED_MODEL=")[1].splitlines()[0]


def test_redact_env_drops_comments_and_blanks() -> None:
    from app.core.diagnostics import redact_env

    out = redact_env("# password=oops\n\nOLLAMA_MODEL=mistral\nX_AUTH=abc\n")
    assert "oops" not in out
    assert "OLLAMA_MODEL=mistral" in out
    assert "X_AUTH=<redacted>" in out


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


# ---------------------------------------------------------------------------
# doctor must report the application's defaults, not its own
# ---------------------------------------------------------------------------

def test_doctor_reports_the_declared_default_not_a_copy_of_it(monkeypatch):
    """`doctor` said `BAAI/bge-reranker-base` while search used MiniLM.

    It kept its own fallbacks - `env_path("RERANK_MODEL",
    "BAAI/bge-reranker-base")` and five more. Removing the key from `.env` so
    the faster default could apply worked, and doctor went on reporting *and
    loading* the model it had been handed as a fallback.

    A diagnostic that reports its own defaults is worse than no diagnostic,
    because it is trusted. This one sent the owner hunting a bug in a config
    change that had already worked.
    """
    import doctor
    from app.core.settings_registry import by_key

    monkeypatch.setattr(doctor, "ENV", {}, raising=False)
    monkeypatch.delenv("RERANK_MODEL", raising=False)

    assert doctor.env_setting("RERANK_MODEL") == str(by_key("RERANK_MODEL").default)


@pytest.mark.parametrize("key", [
    "RERANK_MODEL", "EMBED_MODEL", "EMBED_DIM",
    "OLLAMA_URL", "OLLAMA_MODEL", "REQUIRED_FREE_GB",
])
def test_doctor_does_not_hardcode_a_declared_default(key, monkeypatch):
    """Every key doctor reports and the registry declares must agree."""
    import doctor
    from app.core.settings_registry import by_key

    monkeypatch.setattr(doctor, "ENV", {}, raising=False)
    monkeypatch.delenv(key, raising=False)
    declared = by_key(key)
    assert declared is not None, f"{key} is no longer in the registry"

    assert doctor.env_setting(key) == str(declared.default)


def test_env_still_wins_over_the_declared_default(monkeypatch):
    """The registry is the *fallback*, not an override.

    Someone who deliberately pins a model in `.env` must still get it, or this
    fix would have swapped one wrong answer for another.
    """
    import doctor

    monkeypatch.setattr(doctor, "ENV", {"RERANK_MODEL": "someone/deliberate"},
                        raising=False)

    assert doctor.env_setting("RERANK_MODEL") == "someone/deliberate"


def test_doctor_still_works_if_the_registry_cannot_be_imported(monkeypatch):
    """A doctor that cannot start cannot tell you why nothing starts.

    It runs before the dependencies are proven, so the import is guarded and
    the caller's fallback is used instead.
    """
    import builtins

    import doctor

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if "settings_registry" in name:
            raise ImportError("pretend the app is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(doctor, "ENV", {}, raising=False)
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    monkeypatch.setattr(builtins, "__import__", refuse)

    assert doctor.env_setting("RERANK_MODEL", "fallback/model") == "fallback/model"
