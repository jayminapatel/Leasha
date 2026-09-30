r"""doctor.py's `.dwg` backlog count - §5b.

Layer: n/a (doctor.py is the root diagnostic script, imported directly because
`pyproject.toml` puts the repo root on `pythonpath`).

`_pending_counts_by_ext` is the one piece of new logic doctor.py needed for
"a count of .dwg files waiting": a single `GROUP BY` over the `files` table
that already exists (non-negotiable 6), read-only, and never able to stop the
rest of the report from printing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import doctor
from app.storage.sqlite_store import FileStatus, SqliteStore


def _store_with(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """`rows` is `(path, ext, status)`. Returns the db file doctor should find."""
    db_path = tmp_path / "fts" / "knowledge.db"
    with SqliteStore(db_path) as store:
        for index, (path, ext, status) in enumerate(rows):
            store.upsert_file(
                path, size_bytes=10, mtime_ns=index, ext=ext, status=status,
            )
    return db_path


@pytest.fixture
def data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `load_settings` - the mechanism `_pending_counts_by_ext` actually
    uses - at `tmp_path`, without touching the real `.env` or the real index."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_PATH={tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.core.config.find_env_file", lambda _explicit=None: env_file
    )
    return tmp_path


def test_counts_dwg_files_waiting_by_extension(data_path: Path):
    _store_with(data_path, [
        ("a.dwg", "dwg", FileStatus.NAME_ONLY),
        ("b.dwg", "dwg", FileStatus.NAME_ONLY),
        ("c.dwg", "dwg", FileStatus.INDEXED),   # not waiting - must not count
        ("d.docx", "docx", FileStatus.NAME_ONLY),
    ])

    counts = doctor._pending_counts_by_ext()

    assert counts.get(".dwg") == 2
    assert counts.get(".docx") == 1


def test_no_index_yet_is_an_empty_dict_not_an_exception(data_path: Path):
    """Before the first index run there is no `knowledge.db` at all - this must
    read as "nothing to report", never as a doctor crash."""
    assert doctor._pending_counts_by_ext() == {}


def test_no_env_file_at_all_is_also_an_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.core.config.find_env_file",
        lambda _explicit=None: tmp_path / "does-not-exist.env",
    )

    assert doctor._pending_counts_by_ext() == {}


def test_a_broken_store_never_raises(data_path: Path, monkeypatch):
    """Whatever goes wrong opening the real index, the rest of `doctor` must
    still be able to print its report."""
    (data_path / "fts").mkdir(parents=True, exist_ok=True)
    (data_path / "fts" / "knowledge.db").write_bytes(b"not a sqlite file at all")

    assert doctor._pending_counts_by_ext() == {}


def test_check_file_formats_passes_the_counts_through(
    data_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The plumbing into `format_health`, not just the counting function on
    its own - a count computed but never handed over is as useless as none.

    `check_file_formats` reads `DATA_PATH` through `env_path`, which is
    doctor's *other* env mechanism (`ENV`, computed once at import from the
    real `.env`) - separate from `load_settings`, which `_pending_counts_by_ext`
    uses. Both have to point at the fixture for this one test to mean anything.
    """
    # Five *different* paths: `upsert_file` is keyed on the path, so five
    # copies of one path were one row, and the detail said "1 .dwg file".
    _store_with(data_path, [(f"plan-{n}.dwg", "dwg", FileStatus.NAME_ONLY) for n in range(5)])

    real_env_path = doctor.env_path
    monkeypatch.setattr(
        doctor, "env_path",
        lambda key, default="": (
            str(data_path) if key == "DATA_PATH" else real_env_path(key, default)
        ),
    )

    checks = doctor.check_file_formats()
    dwg_rows = [c for c in checks if c.name.strip().startswith(".dwg")]

    if not dwg_rows:
        pytest.skip("no .dwg converter binary route is blocked on this machine")
    assert "5" in dwg_rows[0].detail


# --- order 0x section 0d: the platform check on each platform ------------------
#
# The check used to fail anywhere but Windows. A Mac is now platform two, so it
# passes there and says what is missing (live Outlook mail). Windows must read
# exactly as before, and anything else must still fail - these three tests pin
# all three answers, so a later edit cannot quietly change one of them.


def test_the_platform_check_passes_on_windows_exactly_as_before(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    check = doctor.check_platform()
    assert check.ok is True
    assert check.name == "Windows platform"
    assert check.detail == "win32"


def test_the_platform_check_passes_on_a_mac_and_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    check = doctor.check_platform()
    assert check.ok is True
    assert "macOS" in check.detail
    assert "Outlook" in check.detail, "the one missing source must be named"


def test_the_platform_check_still_fails_on_any_other_platform(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    check = doctor.check_platform()
    assert check.ok is False
    assert "Windows 10/11" in check.fix


# -- the Outlook check never starts Outlook (2026-09-30) --------------------------

def test_the_outlook_check_never_starts_outlook(monkeypatch):
    """Owner, 2026-09-30: *"i think you are opening outlook"*. The check used
    `Dispatch("Outlook.Application")`, which starts Outlook; Settings' health
    check and the test suite both run doctor. It reads the registry instead."""
    win32com = pytest.importorskip("win32com.client")

    def refuse(*_a, **_k):
        raise AssertionError("doctor started a COM server")
    monkeypatch.setattr(win32com, "Dispatch", refuse)
    monkeypatch.setattr(win32com, "DispatchEx", refuse)
    monkeypatch.setattr(doctor, "classic_outlook_registered", lambda: True)
    assert doctor.check_outlook().ok
    monkeypatch.setattr(doctor, "classic_outlook_registered", lambda: False)
    monkeypatch.setattr(doctor, "new_outlook_present", lambda: False)
    check = doctor.check_outlook()
    assert not check.ok and check.optional and "not registered" in check.detail


# -- the model checks look where the models are (2026-09-30) ----------------------

def _fake_fastembed(monkeypatch, seen: list[str]):
    """A `fastembed` that records the cache folder it was handed and fetches nothing."""
    import sys
    import types

    class TextEmbedding:
        def __init__(self, _name, cache_dir=None):
            seen.append(str(cache_dir))

        def embed(self, texts):
            return [[0.0] * int(doctor.env_setting("EMBED_DIM")) for _ in texts]

    class TextCrossEncoder:
        def __init__(self, _name, cache_dir=None):
            seen.append(str(cache_dir))

        def rerank(self, _query, documents):
            return [0.0 for _ in documents]

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = TextEmbedding
    rerank = types.ModuleType("fastembed.rerank")
    cross = types.ModuleType("fastembed.rerank.cross_encoder")
    cross.TextCrossEncoder = TextCrossEncoder
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross)
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "")     # the checks set it; put it back after


def test_the_model_checks_read_the_index_folder_when_model_cache_is_not_pinned(
    tmp_path, monkeypatch
):
    """`.env` pins `DATA_PATH` only; `MODEL_CACHE` derives from it. The two
    checks read the `MODEL_CACHE` key alone and fell back to the project's own `models`.
    On a fresh clone that folder is absent, so `doctor` went online and fetched
    150 MB into the working copy while the models sat in the index folder."""
    monkeypatch.delenv("MODEL_CACHE", raising=False)
    monkeypatch.setattr(doctor, "ENV", {"DATA_PATH": str(tmp_path)})
    seen: list[str] = []
    _fake_fastembed(monkeypatch, seen)

    assert doctor.check_embedding_model().ok
    assert doctor.check_rerank_model().ok

    assert seen == [str(tmp_path / "models")] * 2


def test_a_pinned_model_cache_still_wins(tmp_path, monkeypatch):
    pinned = tmp_path / "elsewhere"
    monkeypatch.setattr(
        doctor, "ENV", {"DATA_PATH": str(tmp_path), "MODEL_CACHE": str(pinned)}
    )
    seen: list[str] = []
    _fake_fastembed(monkeypatch, seen)

    assert doctor.check_embedding_model().ok

    assert seen == [str(pinned)]
