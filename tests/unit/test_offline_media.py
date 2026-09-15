r"""Offline Media I (drives), work order 202626270513 - section 1 and its tests.

Layer: L1/L3

Covers the deep part of the order - identity and storage - end to end through
a real `Pipeline` run, the same way `test_archive_run.py` proves H8 rather
than reading the code and trusting it:

* letter roulette (1a-1c): the same catalogued volume, walked at two
  different mount points, is one row - never two - and nothing anywhere
  stores the letter;
* offline immunity (1d): a run over an unrelated root must not prune a
  single row belonging to a volume that is not currently connected;
* the real Windows GUID round-trip (1a), against whatever fixed drive this
  machine actually has - `app.core.volumes_win` is the one module in this
  order that cannot be faked, so it gets one real test rather than zero.

A volume's `volume_guid` in every test below is a value no real Windows
drive will ever have, which is what makes it "not currently connected" in
`connected_volumes()` without mocking anything - the same trick a stray
`.git` played on `test_repo_attribution.py`, used deliberately here.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.offline_media import connected_volumes, delete_volume, resolve_file_path
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore, volume_synthetic_path

LONG_AGO = 3600


class NullVectors:
    """No LanceDB in these tests - only SQLite's side of the contract."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


def _write(path: Path, text: str = "Barnsley Dairy commissioning notes.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


def _run(store, roots, *, volume_roots=None, **config):
    pipeline = Pipeline(
        store, NullVectors(), _embedder(),
        PipelineConfig(
            walk=WalkConfig(roots=list(roots), volume_roots=volume_roots or {}),
            workers=1, **config,
        ),
    )
    return pipeline.run()


# --- 1a-1c: identity, storage, letter roulette -------------------------------

def test_the_same_volume_walked_at_two_mount_points_is_one_row(tmp_path):
    r"""**The acceptance test named in the order, word for word**: "catalogue
    as E:, remount fixture as F: -> ... dedup all correct."

    Two different temp directories stand in for two different mount points of
    the *same* catalogued drive - the whole point being that identity comes
    from `volume_id` (resolved from the volume GUID, 1a), never from where the
    walk happened to find it.
    """
    mount_e = tmp_path / "mount_e"
    mount_f = tmp_path / "mount_f"
    _write(mount_e / "reports" / "q3.txt", "Northern pump station report.")
    _write(mount_f / "reports" / "q3.txt", "Northern pump station report.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-0001", kind="drive", name="Projects 2019",
            volume_guid=r"\\?\Volume{00000000-0000-0000-0000-000000000001}",
        )

        _run(store, [mount_e],
             volume_roots={str(mount_e).rstrip("\\/").lower(): volume_id})
        rows_after_e = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        assert len(rows_after_e) == 1
        assert rows_after_e[0].path == volume_synthetic_path(volume_id, "reports/q3.txt")
        # **The letter is nowhere.** Neither temp path's drive/root string
        # appears in the stored identity - a grep-shaped guard, made literal.
        assert "mount_e" not in rows_after_e[0].path
        assert "mount_f" not in rows_after_e[0].path

        _run(store, [mount_f],
             volume_roots={str(mount_f).rstrip("\\/").lower(): volume_id})
        rows_after_f = list(store.iter_files(volume_id=volume_id, source_kind="file"))

    assert len(rows_after_f) == 1, (
        "the same file, walked at a different mount point, produced a second "
        "row - the exact bug class 1c exists to kill"
    )
    assert rows_after_f[0].id == rows_after_e[0].id
    assert rows_after_f[0].relative_path == "reports/q3.txt"


def test_two_different_volumes_at_the_same_letter_never_collide(tmp_path):
    r"""The other half of the same guarantee: volume A unplugged, volume B
    plugged in at the letter A used to have, must never be read as one file.

    Both walks use the *same* temp directory (standing in for "the same
    drive letter, two different physical drives on two different days") but
    different `volume_id`s - the situation an absolute-path key cannot tell
    apart, and the reason `path` is never that key for these rows.
    """
    mount_point = tmp_path / "mount"
    _write(mount_point / "notes.txt", "Volume A's own note.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_a = store.upsert_volume(
            "TEST-GUID-A", kind="drive", name="Drive A",
            volume_guid=r"\\?\Volume{aaaaaaaa-0000-0000-0000-000000000001}",
        )
        _run(store, [mount_point],
             volume_roots={str(mount_point).rstrip("\\/").lower(): volume_a})

        # Volume A is unplugged; volume B, a different physical drive,
        # happens to land on the same letter and happens to have a file at
        # the same relative path.
        _write(mount_point / "notes.txt", "Volume B's own, unrelated note.")
        volume_b = store.upsert_volume(
            "TEST-GUID-B", kind="drive", name="Drive B",
            volume_guid=r"\\?\Volume{bbbbbbbb-0000-0000-0000-000000000002}",
        )
        _run(store, [mount_point],
             volume_roots={str(mount_point).rstrip("\\/").lower(): volume_b})

        rows_a = list(store.iter_files(volume_id=volume_a, source_kind="file"))
        rows_b = list(store.iter_files(volume_id=volume_b, source_kind="file"))

    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0].id != rows_b[0].id
    assert rows_a[0].path != rows_b[0].path


# --- 1d: offline is not deleted ----------------------------------------------

def test_a_run_elsewhere_does_not_prune_an_offline_volumes_rows(tmp_path):
    r"""**The other acceptance test named in the order**: "full run with the
    volume absent -> zero prunes, zero changes to its rows."

    `volume_guid` here is a value no real Windows drive can ever report, so
    `connected_volumes()` correctly - and truthfully, not via a mock - finds
    it not connected on whatever machine runs this test.
    """
    volume_mount = tmp_path / "was_mounted_here"
    _write(volume_mount / "archive.txt", "A file from the unplugged drive.")
    unrelated_root = tmp_path / "unrelated"
    _write(unrelated_root / "today.txt", "An ordinary, always-connected file.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-OFFLINE", kind="drive", name="Old Laptop Drive",
            volume_guid=r"\\?\Volume{ffffffff-0000-0000-0000-0000000000ff}",
        )
        _run(store, [volume_mount],
             volume_roots={str(volume_mount).rstrip("\\/").lower(): volume_id})
        before = {r.id: (r.status, r.path) for r in
                 store.iter_files(volume_id=volume_id, source_kind="file")}
        assert len(before) == 1

        # The drive is now "unplugged": nothing about it is on this walk's
        # roots at all, which is the situation `_prune_missing` sees on
        # every ordinary run over the folders that ARE still connected.
        _run(store, [unrelated_root], prune_missing=True)

        after = {r.id: (r.status, r.path) for r in
                store.iter_files(volume_id=volume_id, source_kind="file")}

    assert after == before, "an offline volume's rows changed on a run that never touched it"


def test_connected_volumes_reports_a_fake_drive_as_not_connected(tmp_path):
    """`connected_volumes` against a real Windows machine, for a volume whose
    GUID cannot possibly match anything actually mounted."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: volume GUID resolution")
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-NEVER-MOUNTED", kind="drive", name="Nowhere",
            volume_guid=r"\\?\Volume{deadbeef-dead-beef-dead-beefdeadbeef}",
        )
        online = connected_volumes(store)
    assert volume_id not in online


# --- 2c: delete cascade -------------------------------------------------------

def test_delete_volume_removes_every_row_and_leaves_the_files_alone(tmp_path):
    r"""**"Nothing on the drive itself is touched."** `delete_volume` never
    calls anything under `app.core.volumes_win`, never resolves a mount
    point, and never opens a path - it only deletes SQLite rows. Proved here
    by asserting the fixture file is still readable afterwards, not merely by
    reading the code.
    """
    mount = tmp_path / "mount"
    fixture = _write(mount / "keep.txt", "Still on the drive after Delete.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-DELETE", kind="drive", name="To Be Forgotten",
            volume_guid=r"\\?\Volume{11111111-0000-0000-0000-000000000011}",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): volume_id})
        assert len(store.volume_file_ids(volume_id)) == 1

        result = delete_volume(store, NullVectors(), volume_id)

        assert result["deleted"] is True
        assert result["files"] == 1
        assert store.get_volume(volume_id) is None
        assert store.volume_file_ids(volume_id) == []

    assert fixture.read_text(encoding="utf-8") == "Still on the drive after Delete."


def test_deleting_an_unknown_volume_is_reported_not_raised(tmp_path):
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        result = delete_volume(store, NullVectors(), 999_999)
    assert result == {"deleted": False, "files": 0, "name": None}


# --- 1b: path resolution at the last moment ----------------------------------

def test_resolve_file_path_is_none_when_the_volume_is_not_connected(tmp_path):
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-RESOLVE", kind="drive", name="Somewhere Else",
            volume_guid=r"\\?\Volume{22222222-0000-0000-0000-000000000022}",
        )
        file_id = store.upsert_file(
            volume_synthetic_path(volume_id, "notes/a.txt"),
            size_bytes=10, mtime_ns=1, status="INDEXED", source_kind="file",
            volume_id=volume_id, relative_path="notes/a.txt",
        )
        record = store.get_file_by_id(file_id)
        resolved = resolve_file_path(store, record)
    assert resolved is None


def test_resolve_file_path_is_unchanged_for_an_ordinary_file(tmp_path):
    """1b: "fixed internal disks keep absolute paths untouched"."""
    db = tmp_path / "index.db"
    real_path = str(tmp_path / "D_drive_stand_in" / "doc.pdf")
    with SqliteStore(db) as store:
        file_id = store.upsert_file(
            real_path, size_bytes=10, mtime_ns=1, status="INDEXED", source_kind="file",
        )
        record = store.get_file_by_id(file_id)
        resolved = resolve_file_path(store, record)
    assert resolved == Path(real_path)


# --- app.core.volumes_win: the one module that cannot be faked ---------------

def test_the_real_fixed_drive_on_this_machine_round_trips_through_its_guid():
    r"""**Verify, never guess.** `app.core.volumes_win` is the only module in
    this order that talks to real Windows APIs, so it is the one place a
    fake or a mock would prove nothing. This finds whatever fixed drive the
    test machine actually has, asks its GUID, and asks the same question
    Leasha asks on every rescan: "which letter is this volume at right now?"
    """
    if sys.platform != "win32":
        pytest.skip("Windows-only: volume GUID resolution")
    from app.core.volumes_win import find_drive_by_guid, identify_root, mounted_drive_roots

    roots = mounted_drive_roots()
    assert roots, "no mounted drive letters found on this machine at all"

    root = roots[0]
    identity = identify_root(root)
    assert identity is not None
    assert identity.volume_guid and identity.volume_guid.startswith(r"\\?\Volume{")

    found = find_drive_by_guid(identity.volume_guid)
    assert found == root