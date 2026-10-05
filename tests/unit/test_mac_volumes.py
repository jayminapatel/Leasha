"""A scanned drive is found again on a Mac (order `offline-drives-on-a-mac`).

Three kinds of test, and which is which matters:

1. **Anywhere.** The Mac module with its four questions answered by the test,
   and the Windows-named door (`volumes_win`) with the system set to macOS for
   the length of one test. These run on the Windows laptop.
2. **On macOS only, against real disk images.** `hdiutil` makes an APFS, a Mac
   OS Extended, an exFAT and a FAT32 image, mounts it, unmounts it and mounts
   it again. They run on GitHub's Mac and are skipped everywhere else.
3. **Not here at all.** A USB stick in somebody's hand. That is
   `docs/MAC_VERIFICATION.md` 5.3.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

from app.core import volumes_win
from app.core.osbridge import volumes as mac

UUID_A = "0C9E1B5A-6C0E-4D3B-9A52-0D7C2B1F4E11"
UUID_B = "7F3D2A10-11AA-4B6C-8E0F-5A5B6C7D8E9F"
ZERO = "00000000-0000-0000-0000-000000000000"

on_a_mac = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS (real disk images)")


def _identify(root, *, mounts=("/Volumes/Photos",), quick=None, slow=None):
    return mac.identify_macos_root(
        root,
        is_mount=lambda text: text in mounts,
        quick=lambda _t: quick,
        slow=lambda _t: slow,
        fs_name=lambda _t: "exfat",
    )


# -- 1. anywhere: the Mac module ------------------------------------------------

def test_a_mount_point_is_identified_by_its_uuid_and_says_a_mac_made_it():
    found = _identify("/Volumes/Photos", quick=UUID_A.lower())
    assert found == mac.MacVolume(identity="macos-volume:" + UUID_A, label="Photos",
                                  fs_name="exfat")
    assert _identify("/Volumes/Photos/", quick=UUID_A).identity == found.identity


def test_the_slow_route_is_asked_only_when_the_quick_one_has_nothing():
    asked: list = []

    def slow(_text):
        asked.append(1)
        return UUID_B

    args = dict(is_mount=lambda _t: True, fs_name=lambda _t: None)
    assert mac.identify_macos_root("/Volumes/X", quick=lambda _t: UUID_A, slow=slow,
                                   **args).identity.endswith(UUID_A)
    assert asked == []
    for nothing in (None, "", ZERO, "not-a-uuid"):
        assert mac.identify_macos_root("/Volumes/X", quick=lambda _t, n=nothing: n, slow=slow,
                                       **args).identity.endswith(UUID_B)
    assert len(asked) == 4


def test_a_folder_a_windows_path_and_a_volume_with_no_uuid_have_no_identity():
    assert _identify("/Volumes/Photos/2019", quick=UUID_A) is None, "a folder, not a drive"
    assert _identify("/Volumes/Gone", quick=UUID_A) is None, "not mounted"
    assert _identify("E:" + chr(92), mounts=("E:" + chr(92),), quick=UUID_A) is None
    assert _identify("", quick=UUID_A) is None
    assert _identify("/Volumes/Photos", quick=None, slow=None) is None
    assert _identify("/Volumes/Photos", quick=ZERO, slow="rubbish") is None


def test_a_name_with_spaces_and_letters_beyond_ascii_is_kept_whole():
    name = "/Volumes/Été 2019 – photos"
    found = _identify(name, mounts=(name,), quick=UUID_A)
    assert found.label == "Été 2019 – photos"


def test_identifying_never_raises():
    def boom(_text):
        raise OSError("the disk went away")

    assert mac.identify_macos_root("/Volumes/X", is_mount=boom) is None
    assert mac.identify_macos_root("/Volumes/X", is_mount=lambda _t: True, quick=boom) is None
    assert mac.identify_macos_root(None) is None


def test_what_diskutil_says_is_read_and_rubbish_is_refused():
    assert mac.volume_uuid_by_diskutil("/", info={"VolumeUUID": UUID_A.lower()}) == UUID_A
    for info in ({}, {"VolumeUUID": ""}, {"VolumeUUID": ZERO}, {"VolumeUUID": 7},
                 {"DiskUUID": UUID_A}):
        assert mac.volume_uuid_by_diskutil("/", info=info) is None


def test_off_a_mac_neither_route_is_even_tried(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert mac.volume_uuid_by_system_call("/") is None
    assert mac.volume_uuid_by_diskutil("/") is None


def test_the_mounted_roots_are_the_startup_disk_and_the_mount_points_in_volumes(tmp_path):
    for name in ("Photos", "Photos 1", "Macintosh HD", "a-file.txt"):
        (tmp_path / name).mkdir()
    mounts = {"/", str(tmp_path / "Photos"), str(tmp_path / "Photos 1")}
    roots = mac.mounted_macos_roots(tmp_path, is_mount=lambda text: text in mounts)
    assert roots == [Path("/"), tmp_path / "Photos", tmp_path / "Photos 1"]
    # The start-up disk's own entry is a link, not a mount point: listed once.
    assert tmp_path / "Macintosh HD" not in roots


def test_an_unreadable_volumes_folder_is_an_empty_list_not_an_error(tmp_path):
    assert mac.mounted_macos_roots(tmp_path / "missing", is_mount=lambda _t: False) == []

    def boom(_text):
        raise OSError("stale mount")

    (tmp_path / "Stale").mkdir()
    assert mac.mounted_macos_roots(tmp_path, is_mount=boom) == []


# -- 1. anywhere: the door every caller already uses -----------------------------

@pytest.fixture()
def a_mac(monkeypatch):
    """This system is a Mac for one test, with two drives that can be moved."""
    mounted: dict = {}                              # mount point -> uuid

    def identify(root, **_kw):
        text = str(root).replace(chr(92), "/")
        text = text.rstrip("/") if len(text) > 1 else text
        if text not in mounted:
            return None
        return mac.MacVolume(identity=mac.MACOS_IDENTITY_PREFIX + mounted[text],
                             label=os.path.basename(text) or None, fs_name="exfat")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mac, "identify_macos_root", identify)
    monkeypatch.setattr(mac, "mounted_macos_roots", lambda *_a, **_k: [Path(p) for p in mounted])
    return mounted


def _same(path, text: str) -> bool:
    return str(path).replace(chr(92), "/") == text


def test_on_a_mac_the_windows_named_door_answers_with_the_mac_identity(a_mac):
    a_mac["/Volumes/Photos"] = UUID_A
    found = volumes_win.identify_root(Path("/Volumes/Photos"))
    assert found == volumes_win.VolumeIdentity(
        volume_guid="macos-volume:" + UUID_A, fs_label="Photos", fs_name="exfat",
        volume_serial=None)
    assert volumes_win.identify_root(Path("/Volumes/Photos/2019")) is None
    assert [str(p).replace(chr(92), "/") for p in volumes_win.mounted_drive_roots()] == [
        "/Volumes/Photos"]


def test_a_drive_is_found_wherever_it_comes_back_and_two_drives_never_swap(a_mac):
    """0k's "catalogue as E:, remount as F:" and "two different volumes at the
    same letter never collide", said the way a Mac says them."""
    a_mac.update({"/Volumes/Photos": UUID_A, "/Volumes/Photos 1": UUID_B})
    first, second = "macos-volume:" + UUID_A, "macos-volume:" + UUID_B
    assert _same(volumes_win.find_drive_by_guid(first), "/Volumes/Photos")
    assert _same(volumes_win.find_drive_by_guid(second), "/Volumes/Photos 1")
    a_mac.clear()                                    # both unplugged
    assert volumes_win.find_drive_by_guid(first) is None
    a_mac.update({"/Volumes/Photos": UUID_B, "/Volumes/Photos 1": UUID_A})   # the other way round
    assert _same(volumes_win.find_drive_by_guid(first), "/Volumes/Photos 1")
    assert _same(volumes_win.find_drive_by_guid(second), "/Volumes/Photos")


def test_a_drive_scanned_on_windows_is_never_taken_for_one_on_a_mac(a_mac):
    a_mac["/Volumes/Photos"] = UUID_A
    windows_made = chr(92) * 2 + "?" + chr(92) + "Volume{" + UUID_A.lower() + "}" + chr(92)
    assert volumes_win.find_drive_by_guid(windows_made) is None
    assert volumes_win.find_drive_by_guid(UUID_A) is None, "no prefix, no match"


class _Volumes:
    def __init__(self, rows):
        self.rows = rows
        self.statuses: dict = {}

    def list_volumes(self):
        return self.rows

    def set_volume_status(self, volume_id, status):
        self.statuses[volume_id] = status


def test_on_a_mac_a_plugged_in_drive_reads_as_connected_and_an_unplugged_one_does_not(
        a_mac, monkeypatch):
    from app.index import offline_media

    def no_subprocess(*_a, **_k):
        raise AssertionError("nothing here may start a process on a Mac")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(volumes_win, "probe_unc_reachable", no_subprocess)
    a_mac["/Volumes/Photos 1"] = UUID_A
    store = _Volumes([
        {"id": 1, "kind": "drive", "volume_guid": "macos-volume:" + UUID_A},
        {"id": 2, "kind": "drive", "volume_guid": "macos-volume:" + UUID_B},
        {"id": 3, "kind": "network", "identity_key": chr(92) * 2 + "nas" + chr(92) + "photos"},
        {"id": 4, "kind": "drive", "volume_guid": None},
    ])
    online = offline_media.connected_volumes(store)
    assert list(online) == [1] and _same(online[1], "/Volumes/Photos 1")
    assert offline_media.refresh_volume_statuses(store) == {
        1: "ONLINE", 2: "OFFLINE", 3: "OFFLINE", 4: "OFFLINE"}
    assert store.statuses[1] == "ONLINE"


def test_a_system_that_is_neither_still_says_nothing_is_connected(monkeypatch):
    from app.index import offline_media

    monkeypatch.setattr(sys, "platform", "linux")
    store = _Volumes([{"id": 1, "kind": "drive", "volume_guid": "macos-volume:" + UUID_A}])
    assert offline_media.connected_volumes(store) == {}


def test_choosing_a_folder_on_a_mac_is_refused_as_not_a_drive(a_mac, tmp_path):
    from app.index import offline_media

    a_mac["/Volumes/Photos"] = UUID_A
    assert offline_media.identify_source(Path("/Volumes/Photos")) == ("drive", {
        "identity_key": "macos-volume:" + UUID_A,
        "volume_guid": "macos-volume:" + UUID_A,
        "fs_label": "Photos",
    })
    assert offline_media.identify_source(tmp_path) is None
    assert offline_media.is_folder_not_a_drive(tmp_path) is True


# -- 2. on macOS only: real disk images ------------------------------------------

def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, timeout=120)


def _make_image(folder: Path, fs: str, name: str, tag: str) -> Path:
    image = folder / f"{tag}.dmg"
    made = _run("hdiutil", "create", "-size", "48m", "-fs", fs, "-volname", name,
                "-ov", str(image))
    if made.returncode != 0:
        pytest.skip(f"hdiutil could not make a {fs} image here: "
                    f"{made.stderr.decode(errors='replace')[:200]}")
    return image


def _attach(image: Path) -> str:
    import plistlib

    done = _run("hdiutil", "attach", "-nobrowse", "-plist", str(image))
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    points = [entity["mount-point"] for entity in plistlib.loads(done.stdout)["system-entities"]
              if entity.get("mount-point")]
    assert len(points) == 1, points
    return points[0]


def _detach(point: str) -> None:
    for _try in range(5):
        if _run("hdiutil", "detach", point, "-force").returncode == 0:
            return
        time.sleep(1.0)


@on_a_mac
def test_the_startup_disk_has_an_identity_and_both_routes_agree_on_it(tmp_path):
    quick = mac.volume_uuid_by_system_call("/")
    slow = mac.volume_uuid_by_diskutil("/")
    assert quick and slow and quick == slow, (quick, slow)
    found = volumes_win.identify_root(Path("/"))
    assert found is not None and found.volume_guid == "macos-volume:" + quick
    assert volumes_win.find_drive_by_guid(found.volume_guid) == Path("/")
    assert volumes_win.identify_root(tmp_path) is None, "a folder is not a drive"
    assert Path("/") in volumes_win.mounted_drive_roots()

    def timed(ask) -> float:
        started = time.perf_counter()
        for _ in range(5):
            ask("/")
        return (time.perf_counter() - started) / 5 * 1000

    warnings.warn(f"[mac-volumes] startup disk: getattrlist {timed(mac.volume_uuid_by_system_call):.3f} ms, "
                  f"diskutil {timed(mac.volume_uuid_by_diskutil):.1f} ms per call",
                  stacklevel=1)


@on_a_mac
@pytest.mark.parametrize("fs, name", [
    ("APFS", "LeashaApfs"),
    ("HFS+", "LeashaHfs"),
    ("ExFAT", "LEASHAEXF"),
    ("MS-DOS FAT32", "LEASHAFAT"),
])
def test_a_real_volume_keeps_its_identity_through_unplugging(tmp_path, fs, name):
    """Mounted, unmounted and mounted again, three times: one identity, found
    each time it is there and not found when it is not."""
    image = _make_image(tmp_path, fs, name, "one")
    seen: list = []
    point = ""
    try:
        for _round in range(3):
            point = _attach(image)
            quick = mac.volume_uuid_by_system_call(point)
            slow = mac.volume_uuid_by_diskutil(point)
            found = volumes_win.identify_root(Path(point))
            assert found is not None, f"{fs}: no identity (getattrlist {quick}, diskutil {slow})"
            assert quick is None or slow is None or quick == slow, (fs, quick, slow)
            seen.append((found.volume_guid, quick, slow, found.fs_name))
            assert volumes_win.find_drive_by_guid(found.volume_guid) == Path(point)
            assert volumes_win.identify_root(Path(point) / "nowhere") is None
            _detach(point)
            point = ""
            assert volumes_win.find_drive_by_guid(found.volume_guid) is None, "unplugged"
    finally:
        if point:
            _detach(point)
    assert len({identity for identity, *_rest in seen}) == 1, seen
    warnings.warn(f"[mac-volumes] {fs}: identity {seen[0][0]}, getattrlist "
                  f"{'answered' if seen[0][1] else 'had nothing'}, diskutil "
                  f"{'answered' if seen[0][2] else 'had nothing'}, file system "
                  f"{seen[0][3]!r}", stacklevel=1)


@on_a_mac
def test_two_real_drives_of_one_name_are_told_apart_whichever_mounts_first(tmp_path):
    """Both are called LEASHATWIN, so macOS mounts whichever comes second as
    "LEASHATWIN 1". Plugged in the other way round, each is still itself."""
    one = _make_image(tmp_path, "ExFAT", "LEASHATWIN", "one")
    two = _make_image(tmp_path, "ExFAT", "LEASHATWIN", "two")
    points: list = []
    try:
        points = [_attach(one), _attach(two)]
        first = volumes_win.identify_root(Path(points[0])).volume_guid
        second = volumes_win.identify_root(Path(points[1])).volume_guid
        assert first != second
        was = {first: points[0], second: points[1]}
        for point in points:
            _detach(point)
        points = [_attach(two), _attach(one)]            # the other way round
        assert volumes_win.identify_root(Path(points[0])).volume_guid == second
        assert volumes_win.identify_root(Path(points[1])).volume_guid == first
        assert volumes_win.find_drive_by_guid(first) == Path(points[1])
        assert volumes_win.find_drive_by_guid(second) == Path(points[0])
        warnings.warn(f"[mac-volumes] twins: first time {was}, second time "
                      f"{ {second: points[0], first: points[1]} }", stacklevel=1)
    finally:
        for point in points:
            _detach(point)
