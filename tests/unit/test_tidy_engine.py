r"""Order 1q section 1 - the Tidy engine: what it offers, what it keeps, what it moves.

Layer: L4

Real store, real files under `tmp_path`, and a fake Recycle Bin that moves files into
a folder of its own - the real one is proved by `test_the_real_recycle_bin_...`, on
Windows only, on a file the test made.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from app.index.walker import content_hash
from app.storage.sqlite_store import SqliteStore
from app.tidy import find, keep, remove
from tests.unit.test_reports_read_only import deny_writes
from tests.unit.timeline_env import add_file

NS = 1_000_000_000


class FakeBin:
    """The RecycleBin prepare/send/restore contract, over a temporary folder."""

    def __init__(self, folder: Path, fixed: bool = True) -> None:
        self.folder, self.fixed, self.sent = folder, fixed, []
        folder.mkdir(parents=True, exist_ok=True)

    def on_fixed_drive(self, path: str) -> bool:
        return self.fixed

    def prepare(self, path: str) -> str:
        return str(self.folder / f"$R{len(self.sent)}{Path(path).suffix}")

    def send_prepared(self, path: str, ref: str) -> str:
        actual = self.send(path)
        assert actual == ref
        return actual

    def send(self, path: str) -> str:
        target = self.folder / f"$R{len(self.sent)}{Path(path).suffix}"
        shutil.move(path, target)
        self.sent.append(path)
        return str(target)

    def put_back(self, ref: str, path: str) -> None:
        if os.path.exists(path):
            raise OSError("taken")
        os.replace(ref, path)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as s:
        yield s


def real(store, folder: Path, name: str, body: bytes, *, mtime: int | None = None, phash=None) -> Path:
    """A file on disk and its row, exactly as the indexer would have left it."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))
    st = path.stat()
    add_file(store, str(path), mtime=st.st_mtime_ns, size=st.st_size,
             content_hash=content_hash(path), phash=phash)
    return path


# -- what is offered ---------------------------------------------------------------

@pytest.mark.slow
def test_copies_are_offered_only_between_files_on_this_computer(store, tmp_path):
    a = real(store, tmp_path / "Pictures", "a.jpg", b"same bytes", mtime=1 * NS)
    b = real(store, tmp_path / "Downloads", "a (1).jpg", b"same bytes", mtime=2 * NS)
    lone = real(store, tmp_path / "Docs", "lone.txt", b"once here")
    vol = store.upsert_volume("serial:X1", kind="drive", name="Archive 2019")
    add_file(store, f"vol://{vol}/a.jpg", mtime=1, size=10, content_hash=content_hash(a), volume_id=vol,
             relative_path=r"Photos\a.jpg")
    add_file(store, f"vol://{vol}/lone.txt", mtime=1, size=9, content_hash=content_hash(lone),
             volume_id=vol)

    sets = find.copy_sets(store)
    assert [sorted(f.path for f in s.files) for s in sets] == [sorted([str(a), str(b)])]
    assert sets[0].elsewhere == (r"Archive 2019: Photos\a.jpg",)
    assert find.copy_totals(store) == (1, len(b"same bytes"))


def test_leftovers_are_partial_downloads_temp_files_and_installers_in_downloads(store, tmp_path):
    keepers = [real(store, tmp_path / "Tools", "setup.exe", b"x"), real(store, tmp_path, "notes.txt", b"y")]
    left = [real(store, tmp_path / "Downloads", "setup.exe", b"z"),
            real(store, tmp_path / "Downloads", "film.mp4.crdownload", b"zz"),
            real(store, tmp_path / "Docs", "~$report.docx", b"o")]
    found = {f.path for f in find.leftovers(store).files}
    assert found == {str(p) for p in left}
    assert not found & {str(p) for p in keepers}


def test_the_finders_read_only(store, tmp_path):
    real(store, tmp_path, "a.txt", b"one")
    real(store, tmp_path / "b", "a.txt", b"one")
    denied = deny_writes(store.conn)
    find.copy_sets(store), find.copy_totals(store), find.similar_sets(store)
    find.large_files(store), find.leftovers(store), find.overview(store, [str(tmp_path)])
    find.sets_for_file(store, str(tmp_path / "a.txt")), remove.batches(store)
    assert denied == []


def test_find_copies_of_this_opens_that_files_own_set(store, tmp_path):
    a = real(store, tmp_path / "x", "a.txt", b"one")
    real(store, tmp_path / "y", "a.txt", b"one")
    real(store, tmp_path / "z", "other.txt", b"two")
    sets = find.sets_for_file(store, str(a))
    assert [s.kind for s in sets] == ["copies"] and len(sets[0].files) == 2


# -- who stays ---------------------------------------------------------------------

def test_ill_choose_marks_nothing_and_no_rule_marks_a_leftover_or_a_large_file(store, tmp_path):
    real(store, tmp_path / "a", "f.txt", b"dup")
    real(store, tmp_path / "b", "f.txt", b"dup")
    real(store, tmp_path / "Downloads", "x.part", b"partial")
    (copies,) = find.copy_sets(store)
    assert keep.marked_by_rule(copies, "manual") == set()
    for rule in keep.RULES:
        assert keep.marked_by_rule(find.leftovers(store), rule) == set()
        assert keep.marked_by_rule(find.large_files(store), rule) == set()


def test_leasha_picks_the_copy_not_in_downloads_then_the_oldest(store, tmp_path):
    older_in_downloads = real(store, tmp_path / "Downloads", "r.pdf", b"r", mtime=1 * NS)
    kept = real(store, tmp_path / "Documents", "r.pdf", b"r", mtime=5 * NS)
    copy_named = real(store, tmp_path / "Documents", "r (1).pdf", b"r", mtime=3 * NS)
    (tset,) = find.copy_sets(store)
    assert keep.keeper(tset, "leasha").path == str(kept)
    assert keep.marked_by_rule(tset, "leasha") == {str(older_in_downloads), str(copy_named)}
    assert keep.keeper(tset, "oldest").path == str(older_in_downloads)
    assert keep.keeper(tset, "newest").path == str(kept)


@pytest.mark.slow
def test_leasha_keeps_the_photo_with_more_pixels(store, tmp_path):
    from PIL import Image

    big, small = tmp_path / "big.png", tmp_path / "small.png"
    Image.new("RGB", (200, 100), (10, 120, 30)).save(big)
    Image.new("RGB", (50, 25), (10, 120, 30)).save(small)
    for p in (big, small):
        add_file(store, str(p), mtime=p.stat().st_mtime_ns, size=p.stat().st_size,
                 content_hash=content_hash(p), phash="00ff00ff00ff00ff")
    (tset,) = find.similar_sets(store)
    assert keep.keeper(tset, "leasha", keep.qualities(tset)).path == str(big)


# -- what moves --------------------------------------------------------------------

def test_a_set_with_every_file_marked_is_refused(store, tmp_path):
    a = real(store, tmp_path / "a", "f.txt", b"dup")
    b = real(store, tmp_path / "b", "f.txt", b"dup")
    plan = remove.make_plan(find.copy_sets(store), {str(a), str(b)})
    assert plan.items == () and len(plan.refused) == 1


def test_marked_copies_go_to_the_bin_are_journalled_hidden_and_put_back(store, tmp_path):
    a = real(store, tmp_path / "Documents", "f.txt", b"dup", mtime=1 * NS)
    b = real(store, tmp_path / "Downloads", "f.txt", b"dup", mtime=2 * NS)
    bin = FakeBin(tmp_path / "bin")
    sets = find.copy_sets(store)
    plan = remove.make_plan(sets, keep.marked_by_rule(sets[0], "leasha"))
    assert remove.carry_out(store, plan, yes=False, bin=bin).moved == []      # no yes, nothing
    outcome = remove.carry_out(store, plan, yes=True, bin=bin)
    assert [i.file.path for i in outcome.moved] == [str(b)] and a.exists() and not b.exists()
    assert find.copy_sets(store) == []                                        # hidden meanwhile
    (batch,) = remove.batches(store)
    assert (batch.files, batch.waiting) == (1, 1)
    back = remove.put_back(store, batch.batch_id, bin=bin)
    assert len(back.moved) == 1 and b.read_bytes() == b"dup"
    assert remove.batches(store)[0].waiting == 0 and len(find.copy_sets(store)) == 1


def test_a_file_that_changed_since_it_was_indexed_stays(store, tmp_path):
    a = real(store, tmp_path / "a", "f.txt", b"dup")
    b = real(store, tmp_path / "b", "f.txt", b"dup")
    sets = find.copy_sets(store)
    b.write_bytes(b"new!")
    outcome = remove.carry_out(store, remove.make_plan(sets, {str(b)}), yes=True, bin=FakeBin(tmp_path / "bin"))
    assert outcome.moved == [] and b.exists() and "changed" in outcome.left[0][1]
    assert a.exists()


def test_a_copy_is_never_moved_when_the_one_that_stays_has_gone_or_changed(store, tmp_path):
    a = real(store, tmp_path / "a", "f.txt", b"dup")
    b = real(store, tmp_path / "b", "f.txt", b"dup")
    sets = find.copy_sets(store)
    a.write_bytes(b"different")                   # the copy that stays is no longer the same bytes
    outcome = remove.carry_out(store, remove.make_plan(sets, {str(b)}), yes=True, bin=FakeBin(tmp_path / "bin"))
    assert outcome.moved == [] and b.exists() and "stays" in outcome.left[0][1]


def test_nothing_off_a_fixed_drive_moves(store, tmp_path):
    a = real(store, tmp_path / "a", "f.txt", b"dup")
    b = real(store, tmp_path / "b", "f.txt", b"dup")
    sets = find.copy_sets(store)
    outcome = remove.carry_out(store, remove.make_plan(sets, {str(b)}), yes=True,
                               bin=FakeBin(tmp_path / "bin", fixed=False))
    assert outcome.moved == [] and a.exists() and b.exists()


def test_a_big_batch_asks_twice(store, tmp_path, monkeypatch):
    monkeypatch.setattr(remove, "BIG_FILES", 1)
    files = [real(store, tmp_path / str(n), "f.txt", b"dup") for n in range(3)]
    sets = find.copy_sets(store)
    plan = remove.make_plan(sets, {str(files[1]), str(files[2])})
    first = remove.carry_out(store, plan, yes=True, bin=FakeBin(tmp_path / "bin"))
    assert first.asked_again and first.moved == [] and all(p.exists() for p in files)
    second = remove.carry_out(store, plan, yes=True, second_yes=True, bin=FakeBin(tmp_path / "bin"))
    assert len(second.moved) == 2 and files[0].exists()


def test_put_back_never_overwrites_a_file_now_in_that_place(store, tmp_path):
    real(store, tmp_path / "a", "f.txt", b"dup")
    b = real(store, tmp_path / "b", "f.txt", b"dup")
    bin = FakeBin(tmp_path / "bin")
    sets = find.copy_sets(store)
    outcome = remove.carry_out(store, remove.make_plan(sets, {str(b)}), yes=True, bin=bin)
    b.write_bytes(b"a new file in the old place")
    back = remove.put_back(store, outcome.batch_id, bin=bin)
    assert back.moved == [] and b.read_bytes() == b"a new file in the old place"


# -- the real Recycle Bin -----------------------------------------------------------

def test_a_version_2_bin_record_is_read(tmp_path):
    import struct

    from app.core.osbridge.recycle import read_bin_record

    original = r"C:\Users\me\Downloads\f.txt"
    name = original.encode("utf-16-le") + b"\x00\x00"
    filetime = 116444736000000000 + 1_700_000_000 * 10_000_000
    info = tmp_path / "$IABC123.txt"
    info.write_bytes(struct.pack("<qqqi", 2, 42, filetime, len(name) // 2) + name)
    record = read_bin_record(info)
    assert (record.original_path, record.size_bytes, int(record.deleted_at)) == (original, 42, 1_700_000_000)
    assert Path(record.bin_path).name == "$RABC123.txt"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Recycle Bin")
def test_the_real_recycle_bin_takes_a_file_and_gives_it_back(tmp_path):
    from app.core.osbridge.recycle import RecycleBin

    path = tmp_path / "leasha-tidy-test.txt"
    path.write_text("mine, made by this test")
    recycle = RecycleBin()
    if not recycle.on_fixed_drive(str(path)):
        pytest.skip("the test folder is not on a fixed drive")
    ref = recycle.send(str(path))
    assert not path.exists() and os.path.exists(ref)
    recycle.put_back(ref, str(path))
    assert path.read_text() == "mine, made by this test" and not os.path.exists(ref)
    unc = "\\\\server\\share\\x.txt"
    assert recycle.on_fixed_drive(unc) is False


def test_a_name_like_a_copy_loses_even_when_every_copy_is_in_downloads(store, tmp_path):
    """Found grabbing the page from a scratch index under AppData\Temp: every copy
    was "in a temp folder", and the tie went to "Report - Copy.pdf" by its path."""
    real(store, tmp_path / "Downloads", "Report - Copy.pdf", b"r", mtime=1 * NS)
    plain = real(store, tmp_path / "Downloads" / "x", "Report.pdf", b"r", mtime=1 * NS)
    real(store, tmp_path / "Downloads", "Report (1).pdf", b"r", mtime=1 * NS)
    (tset,) = find.copy_sets(store)
    assert keep.keeper(tset, "leasha").path == str(plain)


def test_large_files_are_only_the_ones_over_the_floor(store, tmp_path):
    small = real(store, tmp_path, "small.bin", b"x" * 10)
    assert find.large_files(store).files == ()
    assert [f.path for f in find.large_files(store, at_least=1).files] == [str(small)]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Recycle Bin's record")
def test_a_file_that_went_is_never_reported_left_even_when_its_record_is_slow(tmp_path, monkeypatch):
    """One run under load found no `$I` record at once (it was there 24 s later), and the
    file that had gone was reported "left where it was", with no journal row."""
    from app.core.osbridge import recycle

    held = tmp_path / "bin" / "$RSLOW.txt"
    held.parent.mkdir()
    path = tmp_path / "f.txt"
    path.write_text("x")
    monkeypatch.setattr(recycle, "_FIND_FOR_S", 0.1)
    bin = recycle.RecycleBin()
    monkeypatch.setattr(bin, "_send_windows", lambda p: os.replace(p, held))
    monkeypatch.setattr(bin, "_find", lambda p, since: None)
    ref = bin.send(str(path))
    assert ref.startswith(recycle.LAZY) and not path.exists()
    found = recycle.BinRecord(str(path), str(held), 0.0, 1)
    monkeypatch.setattr(bin, "_find", lambda p, since: found)
    bin.put_back(ref, str(path))
    assert path.read_text() == "x" and not held.exists()


def _set(*paths_and_mtimes):
    from types import SimpleNamespace

    files = [SimpleNamespace(path=p, name=p.rsplit("\\", 1)[-1], mtime_ns=m, size_bytes=10)
             for p, m in paths_and_mtimes]
    return SimpleNamespace(kind="copies", files=files)


def test_a_whole_set_under_a_temp_folder_still_has_its_spare_copy():
    r"""Found by the full suite (2026-10-11): its scratch folders sit under AppData\Local\Temp, so both
    copies looked spare and the older one - the Downloads one - was kept. Only what lies below the
    folder the copies share says which is the spare."""
    from app.tidy import keep

    root = r"C:\Users\a\AppData\Local\Temp\run\t1" + "\\"
    tset = _set((root + r"Documents\r.pdf", 5), (root + r"Downloads\r.pdf", 1))
    assert keep.keeper(tset, "leasha").path.endswith(r"Documents\r.pdf")
    assert keep.shared_folder([f.path for f in tset.files]).lower() == root.rstrip("\\").lower()


def test_a_real_temp_copy_is_still_spare_against_a_document():
    from app.tidy import keep

    tset = _set((r"C:\Users\a\AppData\Local\Temp\r.pdf", 1), (r"C:\Users\a\Documents\r.pdf", 5))
    assert keep.keeper(tset, "leasha").path.endswith(r"Documents\r.pdf")


def test_copies_on_different_drives_share_no_folder_and_still_choose():
    from app.tidy import keep

    tset = _set((r"C:\Downloads\r.pdf", 1), (r"D:\Documents\r.pdf", 5))
    assert keep.shared_folder([f.path for f in tset.files]) == ""
    assert keep.keeper(tset, "leasha").path.startswith("D:")
