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


@pytest.mark.slow
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


@pytest.fixture(autouse=True)
def _no_cpu_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """These runs never wait for the machine to be free.

    **A hang, not a failure (2026-10-13).** The benchmark measures what
    `app.cli index` does, resource governor included, and the governor pauses a
    run while other programs use more than `INDEX_CPU_PERCENT` (80) of the
    machine - "Indexing waits until it is free". In `scripts/run_suite.py` the
    other programs are the suite's own parts: four pytest processes on one
    machine. So a run here sat in `resources.wait_while_throttled` for as long
    as the rest of the suite kept the CPU busy. Seen directly: this file's last
    test alone, with other test runs on the machine, still paused after fifteen
    minutes ("87% CPU used by other programs" in the run's own log, the stack in
    `wait_while_throttled`). The part killed after 3600 s on
    `feat/enrichment-that-finds` had the same symptom; its logs were not kept.

    Only the CPU ceiling goes, through the one place every run reads its limits
    (`build_pipeline_config` -> `limits_from_settings`): workers, priority and
    everything else this file checks stay exactly what a real run gets. Not
    `full_speed`, which also changes the worker count and priority. The
    governor's own CPU rule is tested in `test_resources.py`.
    """
    from app.index import resources

    real = resources.limits_from_settings
    monkeypatch.setattr(resources, "limits_from_settings",
                        lambda settings: replace(real(settings), cpu_percent=0))


@pytest.fixture()
def no_person_env(tmp_path: Path) -> Path:
    """Point the runner at a `.env` that does not exist, so it cannot find a
    real model and a developer's own setup cannot change the test."""
    return tmp_path / "missing.env"


@pytest.mark.slow
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


@pytest.mark.slow
def test_a_busy_machine_does_not_pause_these_runs(
        tmp_path: Path, no_person_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine reads as fully busy for the first few looks, as it does while
    the rest of the suite runs beside this file. Without `_no_cpu_ceiling` the
    governor pauses on the first look and holds for its `quiet_seconds`; with
    the machine busy for real, it held for as long as the suite ran."""
    import psutil

    busy = iter([100.0] * 3)
    monkeypatch.setattr(psutil, "cpu_percent", lambda *a, **k: next(busy, 0.0))

    report = run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        env_file=no_person_env, work_dir=tmp_path / "work"))

    assert report["results"]["pauses"] == 0, report["results"]
    assert report["results"]["documents"] == report["results"]["expected_documents"]


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


@pytest.mark.slow
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


@pytest.mark.slow
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


#: Run in a child process by `_files_held_inside`: prints, as JSON, the files the
#: process `argv[1]` holds open inside the folder `argv[2]`.
_HELD_INSIDE = r"""
import json, sys
from pathlib import Path
import psutil

import os

pid = int(sys.argv[1])


# The folder in every spelling it may come back in - as given (perhaps the short 8.3 form,
# JAYMIN~1.PAT) and fully resolved - with the letter case folded. Each handle's path is then only
# compared as text: resolving every path the process holds would touch the disk once a handle,
# which took over 180 s in a long-lived process (review of #83).
forms = {os.path.normcase(os.path.abspath(sys.argv[2])), os.path.normcase(os.path.realpath(sys.argv[2]))}
work = Path(sys.argv[2])


def inside(path):
    p = os.path.normcase(str(path))
    return any(p == f or p.startswith(f.rstrip(os.sep) + os.sep) for f in forms)


# **A handle this process may not even look at must not end the check.**
# psutil lists the open files and then asks the disk whether each is a
# file; one it may not ask about raises, and the whole list is lost. On the
# Windows CI that was `C:\$Extend\$Deleted\...` - a file some earlier
# test deleted while it was still open, which NTFS parks there until the
# handle closes - and psutil.AccessDenied failed the check before it had
# looked at `work` at all (2026-09-29). Such a path is counted as held only
# if it is inside `work`, so nothing the check is for can slip past it.
real_isfile = psutil._psplatform.isfile_strict

def isfile_or_ours(path):
    try:
        return real_isfile(path)
    except PermissionError:
        return inside(path)

psutil._psplatform.isfile_strict = isfile_or_ours
print(json.dumps([f.path for f in psutil.Process(pid).open_files()
                  if inside(f.path)]))
"""


def _files_held_inside(work: Path) -> list[str]:
    """The files this process holds open inside `work`, asked of the system
    from a child process.

    **Never from inside the test process (2026-10-13).** On Windows psutil's
    `open_files` asks each handle its name on a helper thread and, if that
    thread has not answered in 100 ms, kills it with `TerminateThread`
    (psutil 7.1.3, `arch/windows/proc_handles.c`). A thread killed that way
    can die holding the process heap's lock or half-way through an
    allocation, and what follows is a hang or an access violation somewhere
    else, later. That is a risk a test may not bring into a pytest process
    that `scripts/run_suite.py` keeps alive for a hundred more files. Run
    from a child, the helper threads are the child's: the parent's handles
    are duplicated into it, and whatever a killed thread leaves behind ends
    with the child. The time limit turns a child that stalls into a failure
    with a reason, not a part that sits until it is killed.
    """
    import os
    import subprocess
    import sys

    # 2026-10-16: up to three children of 60 s each, not one of 180 s. One child hung
    # past 180 s in a full run (the killed-thread hazard above, in the child as
    # designed) and the next, fresh, would have answered: a hang belongs to the child
    # it happened in. Three that all hang still fail, with the reason.
    for attempt in range(3):
        try:
            done = subprocess.run(
                [sys.executable, "-c", _HELD_INSIDE, str(os.getpid()), str(work)],
                capture_output=True, text=True, timeout=60, check=False)
            break
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise AssertionError("the open-files check hung in three children in a row")
    assert done.returncode == 0, f"the open-files check failed: {done.stderr[-2000:]}"
    return json.loads(done.stdout)


@pytest.mark.slow
def test_nothing_inside_the_throwaway_folder_is_left_open(
        tmp_path: Path, no_person_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Windows cannot delete an open file; Linux can.** So a handle left open
    inside the throwaway folder only shows up on Windows, as a folder that
    survives the run - which is exactly how the benchmark's own log files
    escaped for a while. This asks the operating system which files the process
    still holds inside the folder at the moment it is about to be deleted, so
    the same mistake fails here too, on any system."""
    import app.index.pipeline_bench as bench

    work = tmp_path / "work"
    held: list[str] = []
    real_rmtree = bench.shutil.rmtree

    def spy(path, *args, **kwargs):
        # **Only the removal of `work` itself is the moment in question.**
        # `bench.shutil` is the one `shutil` module, so this patch sees every
        # `rmtree` in the process - including the archive reader's
        # `TemporaryDirectory` for each zip member, which the python.org
        # build's `tempfile` routes through `shutil.rmtree` (Debian's binds
        # its own copy at import, which is why Linux never saw them). Those
        # run mid-index, with the database and logs rightly open, and on
        # the Windows CI they filled this list with 14 x 5 "leaks" that were
        # nothing of the kind (2026-09-29).
        if Path(path) == work:
            held.extend(_files_held_inside(work))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(bench.shutil, "rmtree", spy)
    run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        env_file=no_person_env, work_dir=work))

    assert held == [], f"still open when the folder was deleted: {held}"
    assert not work.exists()


@pytest.mark.slow
def test_the_open_files_check_sees_a_file_held_open(tmp_path: Path) -> None:
    """The child-process check is not blind: a file this process has open
    inside the folder is reported, and one outside it is not."""
    inside = tmp_path / "work" / "held.log"
    inside.parent.mkdir()
    outside = tmp_path / "elsewhere.log"
    with inside.open("w", encoding="utf-8"), outside.open("w", encoding="utf-8"):
        held = _files_held_inside(tmp_path / "work")

    assert [Path(p).name for p in held] == ["held.log"], held
