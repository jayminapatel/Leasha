r"""The indexing benchmark (work order 0x item 5a) makes the same corpus every
time and produces a report that carries its conditions.

Layer: L3

Two promises are pinned here, because the benchmark is worthless without them:

* **The corpus is repeatable.** "Before" and "after" numbers for 2d and 5b-5d
  are only comparable if both ran over the same bytes. The digest in the
  manifest is that proof, and it must not depend on the day, the machine's
  clock or the random boundary the email library would otherwise invent.
* **Every report says what it was measured under.** Non-negotiable 9: a number
  without its conditions is not a result. A report missing the corpus, the
  embedder kind or the machine would be quoted without them within a week.

Everything runs on the `tiny` size with the fake embedder, so no model is
needed and the whole file takes a few seconds.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.index import pipeline_bench
from app.index.pipeline_bench import (
    FAKE_MODEL_NAME,
    BenchOptions,
    format_report,
    run_pipeline_bench,
)
from app.index.synthetic_corpus import CORPUS_DIR, MANIFEST_NAME, SIZES, generate, load_manifest

TINY = SIZES["tiny"]


# --- the generator -----------------------------------------------------------


def test_the_same_seed_makes_the_same_bytes(tmp_path: Path) -> None:
    """Two corpora from one spec are identical, file for file."""
    first = generate(tmp_path / "a", TINY, size_name="tiny")
    second = generate(tmp_path / "b", TINY, size_name="tiny")

    assert first.digest == second.digest
    assert first.files == second.files and first.bytes == second.bytes
    # Checked directly too, so a digest bug cannot hide a difference.
    names = sorted(p.relative_to(tmp_path / "a" / CORPUS_DIR).as_posix()
                   for p in (tmp_path / "a" / CORPUS_DIR).rglob("*") if p.is_file())
    for name in names:
        assert ((tmp_path / "a" / CORPUS_DIR / name).read_bytes()
                == (tmp_path / "b" / CORPUS_DIR / name).read_bytes()), name


def test_a_different_seed_makes_a_different_corpus(tmp_path: Path) -> None:
    """Otherwise the seed would be decoration."""
    first = generate(tmp_path / "a", TINY)
    other = generate(tmp_path / "b", replace(TINY, seed=2))

    assert first.digest != other.digest


def test_the_corpus_has_every_shape_the_order_asks_for(tmp_path: Path) -> None:
    """Documents, a big mbox, a zip holding a zip, and an eml folder."""
    import zipfile

    manifest = generate(tmp_path / "c", TINY)
    root = tmp_path / "c" / CORPUS_DIR

    for ext in ("txt", "md", "html", "docx", "mbox", "zip", "eml"):
        assert manifest.by_extension.get(ext), f"no .{ext} in the corpus"
    mbox = (root / "mail" / "archive.mbox").read_bytes()
    assert mbox.count(b"\nFrom bench@example.org ") + 1 == TINY.mbox_messages
    assert manifest.attachments > 0
    with zipfile.ZipFile(root / "archives" / "backup.zip") as outer:
        assert "backup/nested.zip" in outer.namelist()
        assert len(outer.namelist()) == TINY.zip_members + 1
    # The manifest sits beside the corpus, so it is never indexed.
    assert (tmp_path / "c" / MANIFEST_NAME).is_file()
    assert load_manifest(tmp_path / "c").digest == manifest.digest


def test_a_folder_with_files_in_it_is_refused(tmp_path: Path) -> None:
    """The generator never writes over, or deletes, anything it did not make."""
    (tmp_path / CORPUS_DIR).mkdir()
    (tmp_path / CORPUS_DIR / "precious.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate(tmp_path, TINY)
    assert (tmp_path / CORPUS_DIR / "precious.txt").read_text(encoding="utf-8") == "mine"


# --- the runner --------------------------------------------------------------


@pytest.fixture()
def no_person_env(tmp_path: Path) -> Path:
    """Point the runner at a `.env` that does not exist, so it cannot find a
    real model and a developer's own setup cannot change the test."""
    return tmp_path / "missing.env"


def test_a_fake_run_produces_a_well_formed_report(tmp_path: Path, no_person_env: Path) -> None:
    """The report has its conditions, its results, and says FAKE everywhere
    the embedder matters."""
    report = run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        env_file=no_person_env, work_dir=tmp_path / "work"))

    json.dumps(report)                           # plain data, all the way down
    assert report["conditions"]["embedder"]["kind"] == "FAKE"
    assert report["conditions"]["embedder"]["model"] == FAKE_MODEL_NAME
    assert "FAKE" in report["label"]
    for field in ("os", "cpu_count_logical", "python"):
        assert report["conditions"]["machine"][field], field
    assert report["conditions"]["corpus"]["digest"]
    assert report["conditions"]["corpus"]["size_name"] == "tiny"

    results = report["results"]
    assert results["documents"] == results["expected_documents"], (
        "the pipeline found a different number of documents than the corpus holds")
    assert results["chunks"] > 0 and results["vectors"] == results["chunks"]
    assert results["wall_s"] > 0
    assert results["files_per_min"] > 0 and results["chunks_per_min"] > 0
    assert set(results["stages_s"]) >= {"write", "embed"}
    assert results["stopped_early"] is None

    table = format_report(report)
    assert table.splitlines()[2].startswith("Measured under:")
    assert "FAKE EMBEDDER" in table
    # Nothing is left behind unless asked for.
    assert not (tmp_path / "work").exists()


def test_the_real_embedder_is_refused_when_no_model_is_downloaded(
        tmp_path: Path, no_person_env: Path) -> None:
    """Never a surprise 130MB download from a benchmark."""
    with pytest.raises(ValueError, match="not downloaded"):
        run_pipeline_bench(BenchOptions(
            corpus_folder=tmp_path / "corpus", size="tiny", embedder="real",
            env_file=no_person_env))


def test_a_folder_holding_a_different_corpus_is_refused(
        tmp_path: Path, no_person_env: Path) -> None:
    """The report would otherwise describe one corpus and measure another."""
    generate(tmp_path / "corpus", replace(TINY, seed=5))
    with pytest.raises(ValueError, match="different counts"):
        run_pipeline_bench(BenchOptions(
            corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
            env_file=no_person_env))


def test_the_probe_reports_heartbeat_lateness(
        tmp_path: Path, no_person_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2d's instrument: idle and indexing lateness from the lag monitor."""
    monkeypatch.setattr(pipeline_bench, "IDLE_BASELINE_S", 0.3)
    report = run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        probe=True, env_file=no_person_env))

    probe = report["responsiveness"]
    for phase in ("idle", "indexing"):
        for key in ("beats", "p50_ms", "p90_ms", "p99_ms", "worst_ms", "stalls"):
            assert isinstance(probe[phase][key], int), (phase, key)
    assert probe["idle"]["beats"] > 0
    assert "NOT the real Indexing page" in probe["method"]
    assert "heartbeat lateness" in format_report(report)


def test_the_command_prints_parseable_json(tmp_path: Path, no_person_env: Path,
                                           capsys: pytest.CaptureFixture) -> None:
    """`--json` stdout is the report and nothing else (progress is on stderr)."""
    from app import cli

    code = cli.main(["bench-pipeline", "--size", "tiny", "--embedder", "fake",
                     "--corpus", str(tmp_path / "corpus"),
                     "--env", str(no_person_env), "--json"])

    assert code == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["tool"] == "app.cli bench-pipeline"


def test_nothing_inside_the_throwaway_folder_is_left_open(
        tmp_path: Path, no_person_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Windows cannot delete an open file; Linux can.** So a handle left open
    inside the throwaway folder only shows up on Windows, as a folder that
    survives the run - which is exactly how the benchmark's own log files
    escaped for a while. This asks the operating system which files the process
    still holds inside the folder at the moment it is about to be deleted, so
    the same mistake fails here too, on any system."""
    import psutil

    import app.index.pipeline_bench as bench

    work = tmp_path / "work"
    held: list[str] = []
    real_rmtree = bench.shutil.rmtree

    def spy(path, *args, **kwargs):
        held.extend(f.path for f in psutil.Process().open_files()
                    if Path(f.path).is_relative_to(work))
        return real_rmtree(path, *args, **kwargs)

    # **A handle this process may not even look at must not end the check.**
    # psutil lists the open files and then asks the disk whether each is a
    # file; one it may not ask about raises, and the whole list is lost. On the
    # Windows CI that was `C:\\$Extend\\$Deleted\\...` - a file some earlier
    # test deleted while it was still open, which NTFS parks there until the
    # handle closes - and psutil.AccessDenied failed this test before it had
    # looked at `work` at all (2026-09-29). Such a path is counted as held only
    # if it is inside `work`, so nothing the check is for can slip past it.
    real_isfile = psutil._psplatform.isfile_strict

    def isfile_or_ours(path):
        try:
            return real_isfile(path)
        except PermissionError:
            return Path(path).is_relative_to(work)

    monkeypatch.setattr(psutil._psplatform, "isfile_strict", isfile_or_ours)
    monkeypatch.setattr(bench.shutil, "rmtree", spy)
    run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        env_file=no_person_env, work_dir=work))

    assert held == [], f"still open when the folder was deleted: {held}"
    assert not work.exists()
