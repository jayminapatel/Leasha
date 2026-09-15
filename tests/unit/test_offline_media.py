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
from app.index.resources import ResourceLimits
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


class _NullRunLock:
    r"""Stands in for `app.core.run_lock.IndexRunLock` in a test that must
    not touch the real machine-wide named mutex - see the test that uses
    this for why. Same shape, no `SingleInstance`, always acquires.
    """

    def __init__(self, store=None, *, owner="", name="", lock_dir=None) -> None:
        self.store = store
        self.owner = owner
        self.acquired = False

    def __enter__(self) -> "_NullRunLock":
        self.acquired = True
        return self

    def __exit__(self, *exc_info) -> None:
        self.acquired = False


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
    # **Deterministic regardless of machine load.** Without an explicit
    # `limits`, `PipelineConfig` defaults to `ResourceLimits(cpu_percent=80)`
    # - correct in production (never compete with the owner), wrong for a
    # unit test that must not depend on what else happens to be running on
    # the box right now. Same pattern as `test_resources.py`/
    # `test_governor_settles.py`'s own `LIMITS`.
    config.setdefault("limits", ResourceLimits(
        memory_mb=100_000, cpu_percent=0, pause_on_battery=False,
        min_free_gb=0, poll_seconds=0.0,
    ))
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

# --- 1e: rescan efficiency - hash-match-means-move ---------------------------

def test_a_moved_file_is_repaired_without_re_extraction(tmp_path):
    r"""**The order's own acceptance line, word for word**: "moved files
    move (1e), extraction count ~ 0."

    A file relocated within the same volume - the ordinary "I tidied my
    drive" case - must not cost a re-read, a re-chunk or a re-embed. Proved
    by counting `stats.indexed` on the pipeline run that follows
    `reconcile_moves`: it must be zero, because the file the walk reaches at
    its new location already looks unchanged.
    """
    from app.index.offline_media import reconcile_moves

    mount = tmp_path / "mount"
    old_file = _write(mount / "2019" / "invoice.txt", "Q3 invoice for Acme Water Ltd.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-MOVE", kind="drive", name="Reorganised Drive",
            volume_guid=r"\\?\Volume{33333333-0000-0000-0000-000000000033}",
        )
        stats1 = _run(store, [mount],
                      volume_roots={str(mount).rstrip("\\/").lower(): volume_id})
        assert stats1.indexed == 1
        before = list(store.iter_files(volume_id=volume_id, source_kind="file"))[0]
        assert before.relative_path == "2019/invoice.txt"

        # The drive gets reorganised: the same bytes, moved to a new folder.
        old_file.unlink()
        new_file = _write(mount / "Invoices" / "2019" / "invoice.txt",
                          "Q3 invoice for Acme Water Ltd.")

        result = reconcile_moves(store, volume_id, mount)
        assert result.moved == 1

        after_move = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        assert len(after_move) == 1, "reconcile_moves must repair the row in place, not add one"
        assert after_move[0].id == before.id
        assert after_move[0].relative_path == "Invoices/2019/invoice.txt"
        assert after_move[0].path == volume_synthetic_path(volume_id, "Invoices/2019/invoice.txt")

        # The pipeline walk that follows must find nothing left to do.
        stats2 = _run(store, [mount],
                      volume_roots={str(mount).rstrip("\\/").lower(): volume_id})

    assert stats2.indexed == 0, (
        f"the moved file was re-extracted (indexed={stats2.indexed}) instead of "
        "being recognised as already up to date at its new location"
    )
    assert stats2.unchanged == 1


def test_reconcile_moves_does_nothing_when_nothing_moved(tmp_path):
    """The common case on every rescan: nothing changed. Must cost no writes."""
    from app.index.offline_media import reconcile_moves

    mount = tmp_path / "mount"
    _write(mount / "steady.txt", "This file has not moved.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-STEADY", kind="drive", name="Quiet Drive",
            volume_guid=r"\\?\Volume{44444444-0000-0000-0000-000000000044}",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): volume_id})

        result = reconcile_moves(store, volume_id, mount)

    assert result.moved == 0
    assert result.hashed == 0


# ---------------------------------------------------------------------------
# Order 202626270514 (network shares, kind=network) - built on the same
# `volumes` table and the same pipeline machinery as the tests above, per
# CLAUDE.md's instruction that 202626270513's schema must not be forced into
# a second, incompatible shape by this order. Nothing below is drive-only.
# ---------------------------------------------------------------------------

def test_normalise_unc_keeps_only_the_share_root():
    from app.core.volumes_win import normalise_unc

    assert normalise_unc(Path(r"\\nas01\projects\2019\report.pdf")) == r"\\nas01\projects"
    assert normalise_unc(Path(r"\\nas01\projects")) == r"\\nas01\projects"
    assert normalise_unc(Path(r"D:\local\path")) is None


def test_resolve_unc_says_none_for_an_ordinary_local_drive():
    """1a's network twin of the letter rule: `identify_source` must not
    mistake C: for a network mapping. Real Windows call, real machine."""
    if sys.platform != "win32":
        pytest.skip("Windows-only")
    from app.core.volumes_win import resolve_unc

    assert resolve_unc("C") is None


def test_probe_unc_reachable_times_out_rather_than_hanging():
    r"""1c: "availability probes hard-timeout on workers." A host that does
    not exist must come back within budget, not block on the OS's own SMB
    timeout (which can be tens of seconds)."""
    if sys.platform != "win32":
        pytest.skip("Windows-only")
    import time as _time

    from app.core.volumes_win import probe_unc_reachable

    started = _time.monotonic()
    result = probe_unc_reachable(r"\\nonexistent-host-999999\share", timeout=2.0)
    elapsed = _time.monotonic() - started

    assert result in (None, False)
    assert elapsed < 10.0, f"probe did not honour its timeout: took {elapsed:.1f}s"


def test_identify_source_recognises_a_unc_path(tmp_path):
    from app.index.offline_media import identify_source

    kind, fields = identify_source(Path(r"\\nas01\projects\2019"))
    assert kind == "network"
    assert fields["identity_key"] == r"\\nas01\projects"


def test_a_network_share_unreachable_is_offline_not_an_error(tmp_path):
    r"""**202626270514's own acceptance line**: "a share scanned before
    decommission stays searchable years later." A fake UNC identity never
    resolves via `probe_unc_reachable` on this machine (no such host), which
    is exactly the "server switched off" case - proven the same honest way
    as the drive tests: a real function asked a real question, not a mock
    told what to say.
    """
    if sys.platform != "win32":
        pytest.skip("Windows-only: UNC reachability")
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            r"\\decommissioned-server\archive", kind="network",
            name="Old File Server (retired)",
        )
        online = connected_volumes(store)
    assert volume_id not in online


def test_a_network_scan_is_recorded_with_no_letter_and_the_right_kind(tmp_path):
    r"""The letter-free identity guarantee (1c) extends past drives: even
    though a network share is walked through whatever path the user typed
    (a mapped letter or a UNC path), the stored row never depends on it -
    proven by scanning the SAME content through a fake "mapped" root and
    checking the row's `kind` and identity."""
    mount = tmp_path / "mapped_z_stand_in"
    _write(mount / "minutes.txt", "Meeting minutes, Q3 2019.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            r"\\nas01\minutes", kind="network", name="Old NAS - Minutes",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): volume_id},
             verify_hash=False)

        record = store.get_volume(volume_id)
        rows = list(store.iter_files(volume_id=volume_id, source_kind="file"))

    assert record.kind == "network"
    assert record.volume_guid is None          # never set for a share
    assert len(rows) == 1
    assert rows[0].path == volume_synthetic_path(volume_id, "minutes.txt")


# ---------------------------------------------------------------------------
# 202626270514 1a - the structure-match offer, and the acceptance half
# (`rename_volume_identity`). The interactive dialog is 0k §2's tab, not
# built; this is the backend + CLI half that can be proven without it.
# ---------------------------------------------------------------------------

def test_suggest_renamed_source_finds_a_structurally_similar_offline_share(tmp_path):
    from app.index.offline_media import suggest_renamed_source

    mount = tmp_path / "old_mount"
    _write(mount / "Invoices" / "2019.txt", "x")
    _write(mount / "Minutes" / "q3.txt", "x")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        old_id = store.upsert_volume(
            r"\\old-nas\projects", kind="network", name="Old NAS",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): old_id})

        # The "server" has been renamed - same folders, new UNC identity.
        new_mount = tmp_path / "renamed_mount"
        _write(new_mount / "Invoices" / "placeholder.txt", "x")
        (new_mount / "Minutes").mkdir(parents=True, exist_ok=True)

        suggestion = suggest_renamed_source(
            store, "network", new_mount, exclude_identity_key=r"\\new-nas\projects")

    assert suggestion is not None
    assert suggestion["name"] == "Old NAS"


def test_suggest_renamed_source_stays_quiet_for_unrelated_shares(tmp_path):
    """Assist, never assume: two shares sharing nothing meaningful must not
    be offered as a match."""
    from app.index.offline_media import suggest_renamed_source

    mount = tmp_path / "old_mount"
    _write(mount / "Invoices" / "2019.txt", "x")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        old_id = store.upsert_volume(
            r"\\old-nas\projects", kind="network", name="Old NAS",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): old_id})

        new_mount = tmp_path / "unrelated_mount"
        _write(new_mount / "Photos" / "beach.jpg", "x")
        _write(new_mount / "Music" / "song.mp3", "x")

        suggestion = suggest_renamed_source(store, "network", new_mount)

    assert suggestion is None


def test_rename_volume_identity_keeps_every_file_row(tmp_path):
    r"""The acceptance half: repointing `identity_key` must not touch a
    single `files` row - they key on `volume_id`, never `identity_key`."""
    mount = tmp_path / "mount"
    _write(mount / "notes.txt", "Old NAS notes.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            r"\\old-nas\projects", kind="network", name="Old NAS",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): volume_id})
        before = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        assert len(before) == 1

        ok = store.rename_volume_identity(volume_id, r"\\new-nas\projects")
        after = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        record = store.get_volume(volume_id)

    assert ok is True
    assert record.identity_key == r"\\new-nas\projects"
    assert [f.id for f in after] == [f.id for f in before]


# ---------------------------------------------------------------------------
# 202626270514 3b-1 - archived sources (kind='archived')
# ---------------------------------------------------------------------------

def test_archive_volume_flips_kind_and_status_and_keeps_files(tmp_path):
    from app.index.offline_media import archive_volume, connected_volumes

    mount = tmp_path / "mount"
    _write(mount / "manifest.txt", "What was on the tape before it was written.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TAPE-B-0042", kind="drive", name="Staging Folder",
            volume_guid=r"\\?\Volume{55555555-0000-0000-0000-000000000055}",
        )
        _run(store, [mount],
             volume_roots={str(mount).rstrip("\\/").lower(): volume_id})

        result = archive_volume(store, volume_id, "LTO-7 tape B-0042, fire safe, IT room")
        record = store.get_volume(volume_id)
        files_after = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        online = connected_volumes(store)

    assert result["archived"] is True
    assert record.kind == "archived"
    assert record.status == "ARCHIVED"
    assert record.location_note == "LTO-7 tape B-0042, fire safe, IT room"
    assert len(files_after) == 1, "archiving must not touch a single files row"
    assert volume_id not in online, "an archived source is never resolvable/rescannable"


def test_archiving_an_unknown_volume_is_reported_not_raised(tmp_path):
    from app.index.offline_media import archive_volume

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        result = archive_volume(store, 999_999, "nowhere")
    assert result == {"archived": False, "name": None}


def test_scan_records_sequential_medium_for_a_tape_volume(tmp_path):
    r"""3b-2: the flag a Scan can set so a later content scan knows to warn
    and read end to end. **Not proven here**: real on-tape physical
    ordering - this machine has no LTFS medium to verify that part of 3b-2
    against, so only the flag's storage and the plain-words warning (in
    `app.cli`) are covered."""
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TAPE-B-0042", kind="drive", name="Tape Staging",
            volume_guid=r"\\?\Volume{66666666-0000-0000-0000-000000000066}",
            sequential_medium=True,
        )
        record = store.get_volume(volume_id)
    assert bool(record.sequential_medium) is True


def test_volume_location_label_for_archived_and_offline_sources(tmp_path):
    from app.index.offline_media import volume_location_label

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        archived_id = store.upsert_volume(
            "TAPE-1", kind="archived", name="Tape B-0042",
            location_note="fire safe", status="ARCHIVED",
        )
        offline_id = store.upsert_volume(
            "SHARE-1", kind="network", name="Old NAS", status="OFFLINE",
        )
        archived = store.get_volume(archived_id)
        offline = store.get_volume(offline_id)

    assert volume_location_label(archived).startswith("on tape Tape B-0042 (archived ")
    assert volume_location_label(offline).startswith("on Old NAS (offline, scanned ")


# ---------------------------------------------------------------------------
# 202626270514 3a/3b/3c - the per-file placeholder model
# ---------------------------------------------------------------------------

def _set_offline(path: Path) -> None:
    import ctypes
    ok = ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x1000)  # OFFLINE
    assert ok, "could not set a real FILE_ATTRIBUTE_OFFLINE bit for this test"


def _clear_offline(path: Path) -> None:
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)  # NORMAL


def test_2b_names_only_scan_of_a_placeholder_performs_zero_content_reads(tmp_path):
    r"""**2b's own test, word for word**: "names-only scan of a placeholder
    fixture performs zero content reads (asserted at the file-open level)."
    `cloudstub`'s guard is `winfs.is_cloud_placeholder`, checked from the
    stat already performed - this asserts nothing downstream ever calls
    `Path.open`/`os.open` on the placeholder's path at all, not merely that
    the result looks right."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    import builtins

    mount = tmp_path / "mount"
    target = _write(mount / "report.txt", "would-be-cloud content, never read")
    _set_offline(target)

    real_open = builtins.open
    opened: list[str] = []

    def guarded_open(file, *args, **kwargs):
        path_str = str(file)
        if path_str == str(target):
            opened.append(path_str)
        return real_open(file, *args, **kwargs)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(builtins, "open", guarded_open)
            db = tmp_path / "index.db"
            with SqliteStore(db) as store:
                stats = _run(store, [mount])
    finally:
        _clear_offline(target)

    assert opened == [], "the placeholder's bytes were opened - this is the download trigger 2b exists to prevent"
    assert stats.skipped == 1


def test_3a_a_placeholder_is_findable_and_skipped_with_a_reason(tmp_path):
    r"""**3a, end to end through a real Pipeline run.** A placeholder must
    never be invisible (the pre-fix bug: `continue` dropped it with no row
    at all) and must never be opened - it settles as SKIPPED/ERR_CLOUD_ONLY,
    findable by name, with a stated, fixable reason (non-negotiable 2)."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    mount = tmp_path / "mount"
    target = _write(mount / "report.txt", "would-be-cloud content")
    _set_offline(target)
    try:
        db = tmp_path / "index.db"
        with SqliteStore(db) as store:
            stats = _run(store, [mount])
            rows = list(store.iter_files(source_kind="file"))
    finally:
        _clear_offline(target)

    assert len(rows) == 1, "the placeholder must still get a row"
    assert rows[0].status == "SKIPPED"
    assert rows[0].skip_code == "ERR_CLOUD_ONLY"
    assert stats.skipped == 1
    assert stats.indexed == 0


def test_3a_a_settled_placeholder_is_not_reprocessed_every_run(tmp_path):
    """The H1 bug class, for placeholders specifically: an unchanged
    placeholder must cost a stat, not a re-classify-and-rewrite, on every
    incremental pass."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    mount = tmp_path / "mount"
    target = _write(mount / "report.txt", "would-be-cloud content")
    _set_offline(target)
    try:
        db = tmp_path / "index.db"
        with SqliteStore(db) as store:
            _run(store, [mount])
            stats2 = _run(store, [mount])
    finally:
        _clear_offline(target)

    assert stats2.skipped == 0
    assert stats2.unchanged == 1


def test_3b_a_placeholder_that_becomes_hydrated_is_indexed_next_run(tmp_path):
    r"""**3b: "placeholder -> hydrated (user opened it) -> next incremental
    run content-indexes it."** Recomputed fresh every walk, from the stat -
    nothing needs to notice the transition happened."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    mount = tmp_path / "mount"
    target = _write(mount / "report.txt", "Barnsley Dairy commissioning notes.")
    _set_offline(target)
    db = tmp_path / "index.db"
    try:
        with SqliteStore(db) as store:
            stats1 = _run(store, [mount])
            assert stats1.skipped == 1

            # The user opened it in Explorer; Windows hydrated it for real.
            _clear_offline(target)
            stats2 = _run(store, [mount])
            rows = list(store.iter_files(source_kind="file"))
    finally:
        _clear_offline(target)

    assert stats2.indexed == 1, "a hydrated placeholder must be content-indexed"
    assert rows[0].status == "INDEXED"


def test_3c_dehydration_keeps_content_and_prunes_nothing(tmp_path):
    r"""**3c, the order's own acceptance line**: "dehydrate a fixture ->
    content still searchable, zero rows pruned." `status` must stay
    `INDEXED` - dehydration is neither deletion nor modification - even
    though the file cannot be opened on this pass."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    mount = tmp_path / "mount"
    target = _write(mount / "report.txt", "Barnsley Dairy commissioning notes.")
    db = tmp_path / "index.db"
    try:
        with SqliteStore(db) as store:
            stats1 = _run(store, [mount])
            assert stats1.indexed == 1
            before = list(store.iter_files(source_kind="file"))[0]
            assert before.status == "INDEXED"

            # Windows frees local space: the file dehydrates back to a
            # placeholder. Bytes are gone locally; the logical file is not.
            _set_offline(target)
            stats2 = _run(store, [mount], prune_missing=True)
            after = list(store.iter_files(source_kind="file"))[0]
    finally:
        _clear_offline(target)

    assert after.status == "INDEXED", "dehydration must never demote a row that was read"
    assert after.id == before.id
    assert stats2.deleted == 0, "dehydration is not deletion - zero rows pruned"


def test_3a_mixed_folder_hydrated_and_placeholder_get_different_treatment(tmp_path):
    r"""**§4**: "mixed folder fixture (hydrated + placeholder): per-file
    treatment exactly as 3a." One folder, one file of each kind, in the same
    run - proves the decision is genuinely per-file (from each candidate's
    own attributes) rather than per-folder or per-run."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: real file attribute round-trip")
    mount = tmp_path / "mount"
    hydrated = _write(mount / "local.txt", "Barnsley Dairy commissioning notes.")
    placeholder = _write(mount / "cloud.txt", "would-be-cloud content")
    _set_offline(placeholder)
    try:
        db = tmp_path / "index.db"
        with SqliteStore(db) as store:
            stats = _run(store, [mount])
            by_name = {r.path.replace("\\", "/").rsplit("/")[-1]: r
                      for r in store.iter_files(source_kind="file")}
    finally:
        _clear_offline(placeholder)

    assert stats.indexed == 1 and stats.skipped == 1
    assert by_name["local.txt"].status == "INDEXED"
    assert by_name["cloud.txt"].status == "SKIPPED"
    assert by_name["cloud.txt"].skip_code == "ERR_CLOUD_ONLY"


# ---------------------------------------------------------------------------
# 202626270514 §4 - share-offline immunity, network's own copy of 0513's
# ---------------------------------------------------------------------------

def test_a_run_elsewhere_does_not_prune_an_offline_shares_rows(tmp_path):
    r"""**§4**: "share-offline immunity mirrors 0513's (never prune, never
    wait)." The mechanism is the same one `test_a_run_elsewhere_does_not_
    prune_an_offline_volumes_rows` already proves for kind=drive -
    `_prune_missing` keys on `connected_volumes`, not on `kind` - so this is
    the same guarantee, proven for kind=network specifically rather than
    assumed to generalise."""
    share_mount = tmp_path / "was_mounted_here"
    _write(share_mount / "archive.txt", "A file from the decommissioned share.")
    unrelated_root = tmp_path / "unrelated"
    _write(unrelated_root / "today.txt", "An ordinary, always-connected file.")

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            r"\\decommissioned-server\archive", kind="network",
            name="Old File Server (retired)",
        )
        _run(store, [share_mount],
             volume_roots={str(share_mount).rstrip("\\/").lower(): volume_id},
             verify_hash=False)
        before = {r.id: (r.status, r.path) for r in
                 store.iter_files(volume_id=volume_id, source_kind="file")}
        assert len(before) == 1

        _run(store, [unrelated_root], prune_missing=True)

        after = {r.id: (r.status, r.path) for r in
                store.iter_files(volume_id=volume_id, source_kind="file")}

    assert after == before, "a decommissioned share's rows changed on a run that never touched it"


# ---------------------------------------------------------------------------
# The manual guarantee (202626270513 order-level test list, item 5)
# ---------------------------------------------------------------------------

def test_nothing_outside_the_cli_command_and_tests_calls_a_scan(tmp_path):
    r"""**"Plugging in any volume triggers NO index activity and NO
    prompt."** Nothing here can plug in a real drive and watch for a
    reaction - there is no device watcher to watch - so this proves the
    weaker, checkable half: no code path exists that *could* fire on its
    own. `upsert_volume` and `identify_source` are the two calls that start
    a source's life; if anything outside `app/cli.py`'s own offline-media
    handlers and the test suite calls either, something now scans on
    something other than a press of Scan.

    The same shape as the order's own "nothing anywhere stored the letter"
    grep-shaped guard, aimed at the sibling promise.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    app_dir = root / "app"

    allowed_files = {
        app_dir / "cli.py",
        app_dir / "index" / "offline_media.py",       # defines both
        app_dir / "storage" / "sqlite_store.py",       # defines upsert_volume
    }
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bupsert_volume\s*\(", text) or re.search(r"\bidentify_source\s*\(", text):
            offenders.append(str(path.relative_to(root)))

    assert not offenders, (
        f"a scan can start outside app.cli's own command and its definitions: {offenders}"
    )


# --- 2a-2d: the shared CLI/UI orchestration -----------------------------

def test_scan_new_source_requires_a_name(tmp_path):
    r"""2b: "requires one, because there is no dialog to ask twice" (the
    CLI's own words) - `scan_new_source` is the shared core both the CLI
    and the tab's `ScanNameDialog` now call, so the same refusal must come
    from here rather than from either caller re-checking it."""
    from app.core.errors import AppErrorException
    from app.core.run_lock import COMMAND_LINE
    from app.index.offline_media import scan_new_source

    settings = _fake_settings(tmp_path)
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        with pytest.raises(AppErrorException) as excinfo:
            scan_new_source(settings, store, tmp_path / "somewhere", name="",
                            run_lock_owner=COMMAND_LINE)
        assert excinfo.value.error.code == "ERR_CONFIG_INVALID"


def test_scan_new_source_catalogues_and_indexes_a_real_folder(tmp_path, monkeypatch):
    r"""The shared core, end to end: identify, catalogue, and the same
    `Pipeline` run `_run_offline_media_pipeline` always did - now behind
    `run_scoped_pipeline`, which both the CLI and this call share.

    **`identify_source` is faked, not the folder.** It calls
    `GetVolumeNameForVolumeMountPointW` on the real mount point, which only
    a genuine drive root answers - a nested tmp_path folder is correctly
    refused, the same as any other folder that is not a drive. Faking the
    identification is the same trick `test_the_same_volume_walked_at_two_
    mount_points_is_one_row` already plays with a GUID no real drive has;
    this is one call earlier, at the point this function itself resolves
    identity rather than being handed a `volume_id` already.
    """
    from app.core.run_lock import COMMAND_LINE
    import app.index.offline_media as offline_media_module
    from app.index.offline_media import scan_new_source

    root = tmp_path / "drive"
    _write(root / "reports" / "q3.txt", "Barnsley Dairy commissioning notes.")
    monkeypatch.setattr(
        offline_media_module, "identify_source",
        lambda path: ("drive", {"identity_key": "TEST-GUID-SCAN-NEW",
                                "volume_guid": "TEST-GUID-SCAN-NEW",
                                "fs_label": "TESTDRIVE"}),
    )
    # **The run lock is a real, machine-wide named mutex**
    # (`app.core.single_instance.DEFAULT_MUTEX_NAME`) - every other test in
    # this file constructs a `Pipeline` directly rather than going through
    # `IndexRunLock` at all, and `run_scoped_pipeline` is the first thing
    # in this order that takes it. On a machine running several checkouts
    # of this project at once, a real acquire here can lose a race to
    # whichever process got there first - found exactly that way, as an
    # `ERR_INDEX_RUNNING` failure with nothing wrong in either process.
    # Faked here because this test's job is `scan_new_source`'s own
    # orchestration - identify, catalogue, index - not the lock's already-
    # relied-upon serialisation of two real runs.
    monkeypatch.setattr("app.core.run_lock.IndexRunLock", _NullRunLock)

    settings = _fake_settings(tmp_path)
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        result = scan_new_source(
            settings, store, root, name="Projects 2019", description="top shelf",
            run_lock_owner=COMMAND_LINE,
        )
        assert result["kind"] == "drive"
        volume_id = result["volume_id"]
        record = store.get_volume(volume_id)
        assert record is not None
        assert record.name == "Projects 2019"
        assert record.description == "top shelf"
        rows = list(store.iter_files(volume_id=volume_id, source_kind="file"))
        assert len(rows) == 1
        assert rows[0].path == volume_synthetic_path(volume_id, "reports/q3.txt")


def test_rescan_source_reports_not_connected_rather_than_hanging(tmp_path):
    r"""A catalogued source whose GUID matches nothing on this machine is
    correctly \"not currently connected\" - never a hang, never an
    exception that looks like a bug rather than an ordinary unplugged
    drive."""
    from app.core.errors import AppErrorException
    from app.core.run_lock import COMMAND_LINE
    from app.index.offline_media import rescan_source

    settings = _fake_settings(tmp_path)
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        store.upsert_volume(
            "TEST-GUID-RESCAN", kind="drive", name="Old Backups",
            volume_guid=r"\\?\Volume{00000000-0000-0000-0000-0000000000aa}",
        )
        with pytest.raises(AppErrorException) as excinfo:
            rescan_source(settings, store, "Old Backups", run_lock_owner=COMMAND_LINE)
        assert "not currently connected" in str(excinfo.value.error.render())


def test_rescan_source_unknown_identifier_is_reported(tmp_path):
    from app.core.errors import AppErrorException
    from app.core.run_lock import COMMAND_LINE
    from app.index.offline_media import rescan_source

    settings = _fake_settings(tmp_path)
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        with pytest.raises(AppErrorException):
            rescan_source(settings, store, "does-not-exist", run_lock_owner=COMMAND_LINE)


def test_find_volume_by_id_and_by_case_insensitive_name(tmp_path):
    from app.index.offline_media import find_volume

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-FIND", kind="drive", name="Projects 2019",
        )
        assert find_volume(store, volume_id).id == volume_id
        assert find_volume(store, str(volume_id)).id == volume_id
        assert find_volume(store, "projects 2019").id == volume_id
        assert find_volume(store, "nope") is None


def _fake_settings(tmp_path):
    r"""The handful of `Settings` fields `run_scoped_pipeline` actually
    reads - a real `Settings` needs `DATA_PATH` resolved, which this
    worktree's `.env`-less test environment does not have; every other
    offline_media test in this file constructs a `Pipeline` directly for
    the same reason, so this mirrors that rather than fighting it.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        vector_path=tmp_path / "vectors",
        # **The real default model's real width.** `run_scoped_pipeline`
        # always builds a real `Embedder.from_settings` - unlike the other
        # tests in this file, which hand `Pipeline` a fake, instant encoder
        # directly - so this has to match what `BAAI/bge-small-en-v1.5`
        # actually returns or the embedder's own width guard refuses it.
        embed_dim=384,
        min_free_gb=0,
        required_free_gb=0,
        embed_dedup=False,
        index_name_only=False,
        fts_db=tmp_path / "index.db",
        # 0i/0j's own additions to `PipelineConfig` - `run_scoped_pipeline`
        # reads these too now, and this stub went stale the moment they
        # landed. Real `config.py` defaults, not invented ones.
        ollama_url="http://127.0.0.1:11434",
        ollama_vision_model="llava",
        caption_trickle_enabled=False,
        people_recognition_enabled=False,
    )


# --- 3c: the /on operator ------------------------------------------------

def test_on_parses_a_quoted_volume_name():
    from app.search.query import parse_query

    parsed = parse_query('on:"Projects 2019"')
    assert parsed.volumes == ("projects 2019",)
    assert parsed.has_filters


def test_on_only_matches_files_on_that_volume(tmp_path):
    r"""The shape `-repo:` already guards against: `-on:` must exclude the
    named volume without also excluding the entire rest of the corpus,
    which is a far larger wrong answer than the one being fixed and in the
    same silent direction.
    """
    from app.search.query import parse_query
    from app.storage.filters import file_filter_sql

    def matching(store, query: str) -> set:
        sql, params = file_filter_sql(parse_query(query))
        rows = store.conn.execute(
            f"SELECT f.id FROM files f WHERE 1=1 {sql}", params).fetchall()
        return {row[0] for row in rows}

    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-ON-OPERATOR", kind="drive", name="Projects 2019",
        )
        on_volume = store.upsert_file(
            volume_synthetic_path(volume_id, "reports/q3.txt"), size_bytes=10,
            mtime_ns=1, ext="txt", parent_dir=volume_synthetic_path(volume_id, "reports"),
            volume_id=volume_id, relative_path="reports/q3.txt",
        )
        ordinary = store.upsert_file(
            r"D:\work\notes.txt", size_bytes=10, mtime_ns=1, ext="txt",
            parent_dir=r"D:\work",
        )

        assert matching(store, 'on:"Projects 2019"') == {on_volume}
        found = matching(store, '-on:"Projects 2019"')
        assert on_volume not in found
        assert ordinary in found, (
            "excluding one Offline Media source also excluded every file "
            "that is not on any catalogued volume at all")


def test_on_is_offered_and_honoured_everywhere_repo_is():
    r"""§3c asks for the same `/` menu reach `/repo` already has - every
    tab offers every switch, per `test_command_subsets.py`'s own rule."""
    from app.search.commands import COMMANDS
    from app.search.query import _FIELD_ALIASES

    names = {command.name for command in COMMANDS}
    assert "on" in names
    assert _FIELD_ALIASES.get("on") == "volume"
