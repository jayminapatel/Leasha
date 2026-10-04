r"""Every index run is set up by the same code (`app/index/run_setup.py`).

Layer: L3

2026-10-04, the owner: "where ever possible the same code should run for
functions so they are all consistent and standard". What these pin, without a
window (the window's half is `test_run_setup_window.py`):

* **Which pass** - one rule (`pass_for`) for every kind of run, with both
  settings normalised; the store is asked about the images pass only when the
  answer could change it.
* **What a finished run leaves** - a text pass under "after-run" makes the
  next run the images pass, an images pass clears it, and a stopped run, a
  retry, a watch batch or another schedule changes nothing.
* **The command line keeps those records** as the window does, and reads the
  saved cloud opt-ins and the saved PST reader for the saved folders.
* **Offline Media** builds its run from the same configuration, with the
  picture embedder.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index.run_setup import (
    CLOUD_SWITCH_STATE_KEY, IMAGES_DUE_STATE, LAST_RUN_STATE, NOW,
    PST_BACKEND_STATE_KEY, SCHEDULED, WATCH, apply_saved_pst_backend,
    build_pipeline_config, images_due_after, pass_for, pass_for_store,
    read_images_due, record_pass, saved_cloud_content_keys,
)
from app.storage.sqlite_store import SqliteStore


def _settings(when: str = "with-run", what: str = "both") -> SimpleNamespace:
    return SimpleNamespace(index_ocr_pass=when, index_ocr_mode=what)


class _Store:
    """A store holding a few states, counting the reads."""

    def __init__(self, **state: str) -> None:
        self.state = dict(state)
        self.reads = 0

    def get_state(self, key: str, default: str = "") -> str:
        self.reads += 1
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


class _Broken:
    def __getattr__(self, name):
        raise RuntimeError("this database is having a day")


# ---------------------------------------------------------------------------
# Which pass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("when,what,kind,due,expected", [
    ("with-run", "both", SCHEDULED, False, "both"),
    ("with-run", "text", SCHEDULED, True, "text"),
    ("after-run", "both", SCHEDULED, False, "text"),
    ("after-run", "both", SCHEDULED, True, "images"),
    ("manual", "both", SCHEDULED, True, "text"),
    # A watch batch is never the images pass: it would leave the saved
    # documents unread.
    ("after-run", "both", WATCH, True, "text"),
    ("with-run", "both", WATCH, False, "both"),
    # 2026-10-04, code review: nor under "with-run" with INDEX_OCR_MODE=images.
    ("with-run", "images", WATCH, False, "text"),
    ("with-run", "images", SCHEDULED, False, "images"),
    # A retry or an offline drive reads as INDEX_OCR_MODE says, now.
    ("after-run", "both", NOW, True, "both"),
    ("manual", "text", NOW, False, "text"),
])
def test_one_rule_says_which_pass_for_every_kind_of_run(when, what, kind, due, expected):
    assert pass_for(_settings(when, what), kind, images_due=due) == expected


def test_the_settings_are_normalised_and_nonsense_is_the_default():
    """`_ocr_mode_for_run` compared the raw setting; the command line did not."""
    assert pass_for(_settings(" After-Run ", "BOTH"), images_due=True) == "images"
    assert pass_for(_settings("whenever", "everything")) == "both"
    assert pass_for(SimpleNamespace()) == "both"


def test_the_store_is_asked_only_when_the_answer_could_change_the_pass():
    store = _Store(**{IMAGES_DUE_STATE: "1"})
    assert pass_for_store(_settings("with-run"), store) == "both"
    assert pass_for_store(_settings("after-run"), store, WATCH) == "text"
    assert pass_for_store(_settings("after-run"), store, NOW) == "both"
    assert store.reads == 0
    assert pass_for_store(_settings("after-run"), store) == "images"
    assert store.reads == 1
    assert read_images_due(_Broken()) is False, "unreadable is a text pass"


# ---------------------------------------------------------------------------
# What a finished run leaves
# ---------------------------------------------------------------------------

def test_a_finished_text_pass_makes_the_images_due_and_the_images_pass_clears_it():
    after = _settings("after-run")
    store = _Store()
    assert record_pass(store, after, "text") is True
    assert store.state[IMAGES_DUE_STATE] == "1"
    assert record_pass(store, after, "images") is False
    assert store.state[IMAGES_DUE_STATE] == ""


@pytest.mark.parametrize("settings,kind,finished", [
    (_settings("after-run"), SCHEDULED, False),     # stopped: carried on next time
    (_settings("after-run"), NOW, True),            # a retry, an offline drive
    (_settings("after-run"), WATCH, True),          # a watch batch
    (_settings("with-run"), SCHEDULED, True),
    (_settings("manual"), SCHEDULED, True),
])
def test_nothing_else_changes_the_next_pass(settings, kind, finished):
    store = _Store(**{IMAGES_DUE_STATE: "1"})
    assert images_due_after(settings, "text", kind=kind, finished=finished) is None
    assert record_pass(store, settings, "text", kind=kind, finished=finished) is None
    assert store.state == {IMAGES_DUE_STATE: "1"}


def test_recording_never_fails_the_run():
    assert record_pass(_Broken(), _settings("after-run"), "text") is True


# ---------------------------------------------------------------------------
# What the window saved
# ---------------------------------------------------------------------------

def test_the_saved_cloud_opt_ins_count_only_while_the_master_switch_is_on():
    from app.index.walker import CLOUD_CONTENT_STATE_KEY

    opted = json.dumps(["d:\\onedrive\\docs"])
    assert saved_cloud_content_keys(_Store(**{CLOUD_CONTENT_STATE_KEY: opted})) == frozenset()
    on = _Store(**{CLOUD_CONTENT_STATE_KEY: opted, CLOUD_SWITCH_STATE_KEY: "on"})
    assert saved_cloud_content_keys(on) == frozenset({"d:\\onedrive\\docs"})
    assert saved_cloud_content_keys(_Broken()) == frozenset()


@pytest.fixture()
def pst_reader():
    from app.extract.base import extractor_for

    extractor = extractor_for(Path("x.pst"))
    if extractor is None:
        pytest.skip("no PST reader registered here")
    before = extractor.backend
    try:
        yield extractor
    finally:
        extractor.backend = before


def test_the_saved_pst_reader_is_applied_and_nothing_saved_changes_nothing(pst_reader):
    pst_reader.backend = "auto"
    assert apply_saved_pst_backend(_Store()) is None
    assert apply_saved_pst_backend(_Store(**{PST_BACKEND_STATE_KEY: "nonsense"})) is None
    assert apply_saved_pst_backend(_Broken()) is None
    assert pst_reader.backend == "auto"
    assert apply_saved_pst_backend(_Store(**{PST_BACKEND_STATE_KEY: "Outlook"})) == "outlook"
    assert pst_reader.backend == "outlook"


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

def test_the_configuration_is_built_from_a_partial_settings_object(tmp_path):
    """Offline Media's callers and tests hand it a stand-in with a handful of
    fields; it must build what a full `Settings` with those values builds."""
    from app.index.resolve import Resolved
    from app.index.walker import own_paths

    settings = SimpleNamespace(data_path=tmp_path / "data", log_path=tmp_path / "logs",
                               index_ocr_pass="after-run")
    config = build_pipeline_config(settings, [tmp_path / "docs"],
                                   tuned=Resolved(workers=2, embed_batch=8))
    assert config.walk.exclude_paths == own_paths(settings) != frozenset()
    assert config.ocr_mode == "text"
    assert config.file_time_limit_s == 120 and config.stall_limit_s == 600
    assert config.limits.workers == 2 and config.embed_batch == 8


# ---------------------------------------------------------------------------
# The command line keeps the records the window keeps
# ---------------------------------------------------------------------------

def _cli(capsys, *argv) -> tuple[int, str]:
    from app.cli import main

    code = main(list(argv))
    return code, capsys.readouterr().out


@pytest.fixture()
def after_run_env(temp_env: Path) -> Path:
    with temp_env.open("a", encoding="utf-8") as handle:
        handle.write("\nINDEX_OCR_PASS=after-run\nINDEX_CPU_PERCENT=0\n"
                     "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n")
    return temp_env


def _db(env_file: Path) -> Path:
    from app.core.config import load_settings

    return Path(load_settings(env_file).fts_db)


def test_a_command_line_text_pass_makes_its_next_run_the_images_pass(
        capsys, after_run_env, tmp_path) -> None:
    """Before, only the window knew: `app.cli index` under "after-run" was the
    text pass every time, and its own advice was to type `--only-ocr`."""
    # *Note, 2026-10-04, code review:* the folder is saved as the folder set
    # first. A run over folders that are not the saved set no longer takes
    # the "after-run" turn - see the test below.
    from app.cli import ROOTS_STATE_KEY

    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("pump station report", encoding="utf-8")
    db = _db(after_run_env)
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(db) as store:
        store.set_state(ROOTS_STATE_KEY, str(root))
    run = ("--env", str(after_run_env), "--json", "index", str(root),
           "--workers", "1", "--fake-embedder-for-bench")

    passes = []
    for _ in range(3):
        code, out = _cli(capsys, *run)
        assert code == 0
        passes.append(json.loads(out)["ocr_mode"])
        with SqliteStore(_db(after_run_env)) as store:
            passes.append(store.get_state(IMAGES_DUE_STATE, ""))
    assert passes == ["text", "1", "images", "", "text", "1"]


def test_a_run_over_other_folders_neither_reads_nor_records_the_images_pass(
        capsys, after_run_env, tmp_path) -> None:
    """2026-10-04, code review: a command-line run over one folder read the
    index-wide "images due" flag and wrote it, so one folder's text pass made
    the next run over every folder the images pass (and one folder's images
    pass, which read every held scan in the index, cleared it). Such a run
    reads as `INDEX_OCR_MODE` says, like "Index now", and records nothing."""
    from app.cli import ROOTS_STATE_KEY

    saved, other = tmp_path / "saved", tmp_path / "other"
    for folder in (saved, other):
        folder.mkdir()
        (folder / "notes.txt").write_text("pump station report", encoding="utf-8")
    db = _db(after_run_env)
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(db) as store:
        store.set_state(ROOTS_STATE_KEY, f"{saved}|{other}")
        store.set_state(IMAGES_DUE_STATE, "1")
    code, out = _cli(capsys, "--env", str(after_run_env), "--json", "index", str(other),
                     "--workers", "1", "--fake-embedder-for-bench")
    assert code == 0
    assert json.loads(out)["ocr_mode"] == "both", "not the saved set's images pass"
    with SqliteStore(db) as store:
        assert store.get_state(IMAGES_DUE_STATE, "") == "1", "and nothing recorded"


def test_one_folder_s_images_pass_reads_only_that_folder_s_held_files(tmp_path) -> None:
    """2026-10-04, code review: the images pass reads its work back from the
    index (`ERR_NO_TEXT_LAYER` rows, the held-pictures archive list), and a
    run over one folder read every folder's."""
    from app.core.errors import make_error
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    for folder in (mine, theirs):
        folder.mkdir()
        (folder / "scan.pdf").write_bytes(b"%PDF-1.4 nothing")
        (folder / "mail.pst").write_bytes(b"!BDN")
    with SqliteStore(tmp_path / "index.db") as store:
        for folder in (mine, theirs):
            pdf = str(folder / "scan.pdf")
            file_id = store.upsert_file(pdf, size_bytes=1, mtime_ns=1)
            store.mark_skipped(file_id, make_error("ERR_NO_TEXT_LAYER", "t", path=pdf))
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.store = store
        pipeline.config = PipelineConfig(walk=WalkConfig(roots=[mine]), ocr_mode="images")
        pipeline._log = SimpleNamespace(debug=lambda *a, **k: None)
        pipeline._held_archives().note(mine / "mail.pst", held=2, finished=True)
        pipeline._held_archives().note(theirs / "mail.pst", held=2, finished=True)
        queued = [c.path for c in pipeline._no_text_layer_candidates()]
        queued += [c.path for c in pipeline._held_archive_candidates()]
    assert sorted(queued) == sorted([mine / "scan.pdf", mine / "mail.pst"])


def test_a_command_line_run_over_the_saved_folders_uses_what_the_window_saved(
        capsys, after_run_env, tmp_path, monkeypatch, pst_reader) -> None:
    """The cloud opt-ins (master switch and per folder) and the PST reader the
    window saved, and the time the schedule counts from."""
    import app.cli.index as cli_index
    from app.cli import ROOTS_STATE_KEY
    from app.index.walker import CLOUD_CONTENT_STATE_KEY

    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("pump station report", encoding="utf-8")
    key = str(root).rstrip("\\/").lower()
    db = _db(after_run_env)
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(db) as store:
        store.set_state(ROOTS_STATE_KEY, str(root))
        store.set_state(CLOUD_SWITCH_STATE_KEY, "on")
        store.set_state(CLOUD_CONTENT_STATE_KEY, json.dumps([key]))
        store.set_state(PST_BACKEND_STATE_KEY, "outlook")
    pst_reader.backend = "auto"

    built: list = []
    real = cli_index.build_pipeline_config

    def spy(*args, **kwargs):
        built.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_index, "build_pipeline_config", spy)
    code, _out = _cli(capsys, "--env", str(after_run_env), "index", "--quiet",
                      "--workers", "1", "--fake-embedder-for-bench")
    assert code == 0
    assert built[0]["cloud_content_roots"] == frozenset({key})
    assert pst_reader.backend == "outlook", "the run read the saved PST reader"
    with SqliteStore(db) as store:
        assert store.get_state(LAST_RUN_STATE, ""), "the schedule counts from it"

    # A folder named on the command line is this run's own: no saved opt-ins,
    # and it is not the schedule's whole run.
    with SqliteStore(db) as store:
        store.set_state(LAST_RUN_STATE, "")
    code, _out = _cli(capsys, "--env", str(after_run_env), "index", str(root),
                      "--quiet", "--workers", "1", "--fake-embedder-for-bench")
    assert code == 0
    assert built[1]["cloud_content_roots"] == frozenset()
    with SqliteStore(db) as store:
        assert store.get_state(LAST_RUN_STATE, "") == ""


def test_a_retry_takes_the_same_pass_on_the_command_line_as_anywhere():
    from app.cli import build_parser
    from app.cli.index import _ocr_mode

    args = build_parser().parse_args(["index", "--retry-timed-out=pdf", "--"])
    settings = _settings("after-run", "both")
    assert _ocr_mode(args, settings, images_due=True, retry=True) == "both"
    assert _ocr_mode(args, settings, images_due=True) == "images"
    flagged = build_parser().parse_args(["index", "--skip-ocr", "D:/x"])
    assert _ocr_mode(flagged, settings, images_due=True) == "text", "a flag still wins"


# ---------------------------------------------------------------------------
# Offline Media
# ---------------------------------------------------------------------------

def test_an_offline_media_scan_is_built_like_every_other_run(tmp_path, monkeypatch):
    """It built its own `PipelineConfig`: no pass, no time limits, no reader
    processes - and no picture embedder, so a drive's photos never reached
    picture search."""
    import app.index.pipeline as pipeline_module
    from app.index.offline_media import run_scoped_pipeline
    from app.index.walker import own_paths
    from tests.unit.test_offline_media import _NullRunLock, _fake_settings

    seen: dict = {}

    class _Pipeline:
        def __init__(self, store, vectors, embedder, config, **kwargs):
            seen.update(config=config, **kwargs)

        def run(self, on_progress=None):
            return "ran"

    monkeypatch.setattr(pipeline_module, "Pipeline", _Pipeline)
    monkeypatch.setattr("app.core.run_lock.IndexRunLock", _NullRunLock)
    settings = _fake_settings(tmp_path)
    settings.index_ocr_pass = "after-run"
    settings.index_file_time_limit_s = 45
    settings.index_read_processes = True
    root = tmp_path / "drive"
    root.mkdir()
    with SqliteStore(tmp_path / "index.db") as store:
        assert run_scoped_pipeline(settings, store, root, 7,
                                   run_lock_owner="command-line") == "ran"

    config = seen["config"]
    assert config.ocr_mode == "both", "the whole drive now: it is unplugged afterwards"
    assert config.file_time_limit_s == 45 and config.read_processes is True
    assert config.walk.volume_roots == {str(root).rstrip("\\/").lower(): 7}
    # 2026-10-04, code review: its clean-up is this drive's rows, not the index.
    assert config.prune_missing and config.prune_under == ("leasha-volume://7/",)
    assert config.walk.exclude_paths == own_paths(settings)
    assert seen["image_embedder"] is not None and seen["image_vectors"] is not None
