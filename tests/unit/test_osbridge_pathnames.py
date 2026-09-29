r"""Paths and letter case, ready for a Mac (work order 0x section 7).

Layer: L0

`app/core/osbridge/pathnames.py` answers two questions the rest of Leasha used
to answer the Windows way everywhere: which character divides folders in a
path, and whether `Report.docx` and `report.docx` are one file or two.

**The first half of this file pins Windows.** The index on the owner's machine
was built from strings the old code produced, and a path or key that changes by
one character stops matching what is stored. So every Windows test here runs
with `sys.platform` set to `"win32"` and compares against *the old code itself*,
copied below as it stood before this section (`_OLD_JOIN`, `_OLD_KEY`), over a
table of awkward inputs - not only against a hand-written expected value.

**The second half proves the Mac and Linux behaviour on a real disk.** This
Linux machine's file system respects case, so a folder holding both
`Report.txt` and `report.txt` really holds two files: the walker must yield
both, and a full index run must keep both rows. With the disk probe faked to
say "ignores case" (what a default Mac disk says, and what Windows always is),
the same folder must give one.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

from app.core.osbridge import pathnames
from app.core.osbridge.pathnames import (
    case_sensitive,
    forget_probed_roots,
    is_windows_shaped,
    join_under,
    path_key,
    same_path,
    separator_for,
)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """Every test starts with no folder probed, and leaves none behind."""
    forget_probed_roots()
    yield
    forget_probed_roots()


def _OLD_JOIN(root, relative: str) -> str:
    """`app/search/federate._join` exactly as it was before order 0x section 7."""
    base = str(root).rstrip("\\/")
    if not relative:
        return base
    return base + "\\" + relative.replace("/", "\\").lstrip("\\")


def _OLD_KEY(path) -> str:
    """The "seen" key every indexer site used before order 0x section 7b."""
    return str(path).lower()


#: Roots written the many ways the owner's machine has been seen to write them.
ROOTS = [
    r"D:\code\leasha", "D:\\code\\leasha\\", "D:/code/leasha", "d:\\CODE\\Leasha",
    r"\\server\share\repo", "C:\\", "C:", r"D:\Mixed/Separators\x",
    "/Users/me/repo", "/Users/me/repo/", "relative\\root", "",
]
#: Paths git reports (always forward slashes), plus the awkward ones.
RELATIVES = [
    "src/OrderService.cs", "/src/leading.cs", "a", "", "deep/er/path/f.txt",
    "src\\already\\back.cs", "Ünïcode/Été.txt",
]


# ---------------------------------------------------------------------------
# Windows: byte for byte what the old code produced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize("relative", RELATIVES)
def test_on_windows_join_under_is_the_old_federate_join_exactly(
        monkeypatch, root, relative):
    monkeypatch.setattr(sys, "platform", "win32")
    assert join_under(root, relative) == _OLD_JOIN(root, relative)


def test_on_windows_the_federated_path_is_pinned_character_for_character(monkeypatch):
    """The literal string, too - in case both copies ever drifted together."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert join_under(r"D:\code\leasha", "src/OrderService.cs") == \
        r"D:\code\leasha\src\OrderService.cs"
    assert join_under("D:\\code\\leasha\\", "/src/a.cs") == r"D:\code\leasha\src\a.cs"


PATHS = [
    r"D:\Docs\Report.DOCX", r"d:\docs\report.docx", "D:/Mixed\\Case/ß.TXT",
    r"\\Server\Share\File.PDF", "/Users/Me/Report.docx", "ÉTÉ\\Été.txt",
    Path("Some/Relative/Thing.TXT"),
]


@pytest.mark.parametrize("path", PATHS)
def test_on_windows_path_key_is_str_lower_exactly(monkeypatch, path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert path_key(path) == _OLD_KEY(path)


def test_on_windows_case_sensitive_is_false_and_never_touches_the_disk(
        monkeypatch, tmp_path):
    """No probe on Windows: a disk call there would be new behaviour and cost."""
    monkeypatch.setattr(sys, "platform", "win32")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the disk was asked on Windows")

    # `pathnames.os` is the one shared `os` module, so these three are replaced
    # for everything, pytest included. They are put back inside the test, before
    # `tmp_path` is removed: on Windows `shutil.rmtree` calls `os.lstat` and
    # `os.scandir` and lets anything but an `OSError` through, which turned this
    # passing test into an ERROR at teardown on the Windows CI (2026-09-29).
    with monkeypatch.context() as disk:
        disk.setattr(pathnames.os, "scandir", forbidden)
        disk.setattr(pathnames.os, "lstat", forbidden)
        disk.setattr(pathnames.os.path, "isdir", forbidden)
        assert case_sensitive(tmp_path) is False
        assert case_sensitive(r"D:\Anything") is False
        # And a key under that folder is still just lower-cased.
        key = path_key(tmp_path / "Report.TXT")
    assert key == str(tmp_path / "Report.TXT").lower()


def test_on_windows_the_separator_is_always_a_backslash(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    for root in ROOTS:
        assert separator_for(root) == "\\"


# ---------------------------------------------------------------------------
# Mac and Linux: the separator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_mac_root_is_joined_with_forward_slashes(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    assert join_under("/Users/me/repo", "src/b.cs") == "/Users/me/repo/src/b.cs"
    assert join_under("/Users/me/repo/", "/src/b.cs") == "/Users/me/repo/src/b.cs"
    assert join_under("/Users/me/repo", "") == "/Users/me/repo"
    assert separator_for("/Users/me") == "/"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("root", [r"D:\code\leasha", "d:/code", r"\\server\share"])
def test_a_windows_shaped_root_keeps_backslashes_anywhere(monkeypatch, platform, root):
    """An index copied from Windows, or a test's `D:\\...` path, on a Mac/Linux."""
    monkeypatch.setattr(sys, "platform", platform)
    for relative in RELATIVES:
        assert join_under(root, relative) == _OLD_JOIN(root, relative)


def test_windows_shape_is_recognised_and_a_posix_path_never_is():
    assert is_windows_shaped(r"D:\a") and is_windows_shaped("c:/a")
    assert is_windows_shaped(r"\\server\share") and is_windows_shaped("C:")
    assert not is_windows_shaped("/Users/me") and not is_windows_shaped("docs\\a")
    assert not is_windows_shaped("/D:/odd")


def test_the_federated_search_uses_the_system_separator_on_a_mac(monkeypatch):
    """The real call site, `federate._join`, not only the helper."""
    from app.search.federate import _join

    monkeypatch.setattr(sys, "platform", "darwin")
    assert _join("/Users/me/leasha", "src/a.py") == "/Users/me/leasha/src/a.py"
    monkeypatch.setattr(sys, "platform", "win32")
    assert _join(r"D:\code\leasha", "src/a.py") == r"D:\code\leasha\src\a.py"


# ---------------------------------------------------------------------------
# Mac and Linux: letter case, probed per folder
# ---------------------------------------------------------------------------

@pytest.fixture
def here(tmp_path):
    """`tmp_path` as a path a Mac or Linux could have written: relative to it.

    On Windows `tmp_path` is `D:\\a\\...`, and a Windows-shaped path takes the
    Windows rule (case ignored, no probe) whatever `sys.platform` says - which
    is right, and is what made the probe tests below pass or fail on the Windows
    CI without ever reaching the probe (2026-09-29). A relative path is shaped
    like no system in particular, so working inside `tmp_path` makes the Mac and
    Linux branches run on every machine, against a real folder. On Linux it
    changes nothing that is checked.

    A fixture of its own, not `monkeypatch.chdir`, so the old folder is back
    before `tmp_path` is removed: Windows will not delete the folder a process
    is standing in.
    """
    before = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield Path(".")
    finally:
        os.chdir(before)


def _disk_respects_case(folder: Path) -> bool:
    """Does the disk under `folder` keep `x` and `X` apart? Asked directly."""
    (folder / "caseprobe").write_text("a", encoding="utf-8")
    try:
        return not (folder / "CASEPROBE").exists()
    finally:
        (folder / "caseprobe").unlink()


@pytest.fixture
def two_by_case(tmp_path):
    """A folder holding `Report.txt` and `report.txt` - two files, if it can."""
    root = tmp_path / "Docs"
    root.mkdir()
    if not _disk_respects_case(root):
        pytest.skip("this machine's temporary folder ignores letter case")
    (root / "Report.txt").write_text("upper", encoding="utf-8")
    (root / "report.txt").write_text("lower", encoding="utf-8")
    return root


def test_the_probe_finds_a_case_sensitive_folder_on_this_linux_disk(
        monkeypatch, two_by_case):
    monkeypatch.setattr(sys, "platform", "linux")
    assert case_sensitive(two_by_case) is True


def test_the_probe_sees_case_sensitivity_with_only_one_spelling_present(
        monkeypatch, tmp_path):
    """The usual case: `Report.txt` exists and `rEPORT.TXT` is simply absent."""
    monkeypatch.setattr(sys, "platform", "darwin")
    if not _disk_respects_case(tmp_path):
        pytest.skip("this machine's temporary folder ignores letter case")
    (tmp_path / "Report.txt").write_text("x", encoding="utf-8")
    assert case_sensitive(tmp_path) is True


def test_the_probe_reports_a_folder_that_ignores_case(monkeypatch, here):
    """Faked: the swapped spelling finds the *same* file, as on APFS or NTFS."""
    monkeypatch.setattr(sys, "platform", "darwin")
    folder = here / "Docs"
    folder.mkdir()
    (folder / "Report.txt").write_text("x", encoding="utf-8")
    real_lstat = os.lstat

    def insensitive_lstat(path, *args, **kwargs):
        # Resolve any spelling to the real entry, the way a case-insensitive
        # disk would.
        folder, name = os.path.split(str(path))
        for entry in os.listdir(folder):
            if entry.lower() == name.lower():
                return real_lstat(os.path.join(folder, entry))
        return real_lstat(path)

    monkeypatch.setattr(pathnames.os, "lstat", insensitive_lstat)
    assert case_sensitive(folder) is False
    assert pathnames._CASE_BY_ROOT == {str(folder): False}   # probed, not assumed


def test_an_empty_folder_is_probed_through_its_own_name(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    if not _disk_respects_case(tmp_path):
        pytest.skip("this machine's temporary folder ignores letter case")
    empty = tmp_path / "Empty"
    empty.mkdir()
    assert case_sensitive(empty) is True


@pytest.mark.parametrize("platform, expected", [("darwin", False), ("linux", True)])
def test_a_folder_that_cannot_be_probed_gets_the_system_default(
        monkeypatch, here, platform, expected):
    """Nothing with a letter in it to swap: APFS's default on a Mac, ext4's on Linux."""
    monkeypatch.setattr(sys, "platform", platform)
    folder = here / "123"
    folder.mkdir()
    (folder / "456.789").write_text("x", encoding="utf-8")
    assert case_sensitive(folder) is expected


def test_a_missing_folder_is_not_remembered(monkeypatch, here):
    """A drive plugged in later must be probed properly, not given a stale guess."""
    monkeypatch.setattr(sys, "platform", "linux")
    later = here / "Later"
    case_sensitive(later)
    assert str(later) not in pathnames._CASE_BY_ROOT
    later.mkdir()
    (later / "File.txt").write_text("x", encoding="utf-8")
    case_sensitive(later)
    assert str(later) in pathnames._CASE_BY_ROOT


def test_the_answer_is_remembered_and_the_disk_asked_once(monkeypatch, here):
    monkeypatch.setattr(sys, "platform", "linux")
    folder = here / "Docs"
    folder.mkdir()
    (folder / "File.txt").write_text("x", encoding="utf-8")
    calls = []
    real = pathnames._probe
    monkeypatch.setattr(pathnames, "_probe", lambda root: calls.append(root) or real(root))
    case_sensitive(folder)
    case_sensitive(folder)
    case_sensitive(str(folder) + "/")
    assert len(calls) == 1


def test_path_key_follows_the_folder_it_is_in(monkeypatch):
    """Keys keep case under a case-sensitive folder and fold it under the other."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(pathnames._CASE_BY_ROOT, "/Volumes/Work", True)
    monkeypatch.setitem(pathnames._CASE_BY_ROOT, "/Users/me", False)
    pathnames._ROOTS_LONGEST_FIRST[:] = ["/Volumes/Work", "/Users/me"]

    assert path_key("/Volumes/Work/Report.txt") == "/Volumes/Work/Report.txt"
    assert path_key("/Users/me/Report.txt") == "/users/me/report.txt"
    # Spelled differently from the probed folder: still that folder, on a disk
    # that ignores case.
    assert path_key("/USERS/ME/Report.txt") == "/users/me/report.txt"
    # A name that merely *starts* like a folder is not inside it.
    assert path_key("/Volumes/WorkOld/A.txt") == "/volumes/workold/a.txt"
    # Under no probed folder: the Mac default, which folds case.
    assert path_key("/elsewhere/A.txt") == "/elsewhere/a.txt"
    assert not same_path("/Volumes/Work/A.txt", "/Volumes/Work/a.txt")
    assert same_path("/Users/me/A.txt", "/Users/me/a.txt")


def test_the_innermost_folder_decides(monkeypatch):
    """A case-sensitive disk mounted inside a folder that ignores case."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(pathnames._CASE_BY_ROOT, "/data", False)
    monkeypatch.setitem(pathnames._CASE_BY_ROOT, "/data/mnt/cs", True)
    pathnames._ROOTS_LONGEST_FIRST[:] = ["/data/mnt/cs", "/data"]
    assert path_key("/data/mnt/cs/A.txt") == "/data/mnt/cs/A.txt"
    assert path_key("/data/other/A.txt") == "/data/other/a.txt"


def test_a_windows_shaped_path_folds_case_on_every_system(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert path_key(r"D:\Docs\A.TXT") == r"d:\docs\a.txt"
    assert case_sensitive(r"D:\Docs") is False


# ---------------------------------------------------------------------------
# The walker and a whole index run, on a real case-sensitive folder
# ---------------------------------------------------------------------------

def _walk_names(root: Path) -> list[str]:
    from app.index.walker import WalkConfig, walk

    return sorted(c.path.name for c in walk(WalkConfig(
        roots=[root], extensions=frozenset({".txt"}))))


def test_the_walker_yields_both_files_that_differ_only_by_case(two_by_case):
    """The bug this section fixes: the second spelling was dropped as a duplicate."""
    assert _walk_names(two_by_case) == ["Report.txt", "report.txt"]


def test_a_folder_that_ignores_case_still_yields_one(monkeypatch, two_by_case):
    """Faked "ignores case" (a default Mac disk; Windows always): one key, one file.

    The folder on disk really has two files, so this is the strongest check that
    the rule comes from the probe: tell it the disk folds case and the walker
    treats the second spelling as the file it has already seen, exactly as the
    old `.lower()` did.
    """
    monkeypatch.setattr(pathnames, "_probe", lambda _root: False)
    assert len(_walk_names(two_by_case)) == 1


def test_overlapping_roots_are_still_walked_once(two_by_case):
    """The reason the "seen" set exists at all, unchanged by keeping case."""
    from app.index.walker import WalkConfig, walk

    found = list(walk(WalkConfig(roots=[two_by_case, two_by_case],
                                 extensions=frozenset({".txt"}))))
    assert len(found) == 2


def test_the_scan_counts_what_the_walker_will_index(two_by_case):
    from app.index.scan import ScanConfig, scan

    result = scan(ScanConfig(roots=[two_by_case]))
    assert result.total.files == 2


class _NullVectors:
    """The vector store is not what these tests are about."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _pipeline(store, root):
    from app.index.embedder import Embedder, l2_normalise
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
                for t in texts]

    config = PipelineConfig(walk=WalkConfig(roots=[root],
                                            extensions=frozenset({".txt"})))
    return Pipeline(store, _NullVectors(), Embedder(dim=8, encoder=encode), config)


def test_a_whole_index_run_keeps_both_files_and_a_rerun_prunes_neither(
        two_by_case, tmp_path):
    """Walker, pipeline "seen" set and the clean-up pass agree on one key.

    The second run is the dangerous one: its clean-up deletes any row whose key
    the walk did not record. Had the walker kept case while the clean-up
    lower-cased, `Report.txt` would be looked up as `report.txt`... and a file
    that is still there could be deleted.
    """
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, two_by_case).run()
        first = sorted(Path(r.path).name for r in store.iter_files())
        stats = _pipeline(store, two_by_case).run()
        second = sorted(Path(r.path).name for r in store.iter_files())

    assert first == ["Report.txt", "report.txt"]
    assert second == first
    assert not stats.deleted


def test_a_deleted_spelling_is_pruned_and_its_twin_kept(two_by_case, tmp_path):
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, two_by_case).run()
        (two_by_case / "Report.txt").unlink()
        _pipeline(store, two_by_case).run()
        left = sorted(Path(r.path).name for r in store.iter_files())

    assert left == ["report.txt"]


def test_archive_resume_keys_are_unchanged_on_windows(monkeypatch):
    """A cursor saved on the owner's machine must still be found (0w 3b)."""
    import hashlib

    from app.index.interrupted import ARCHIVE_RESUME_PREFIX
    from app.index.pipeline import _archive_resume_key

    monkeypatch.setattr(sys, "platform", "win32")
    path = Path(r"D:\Mail\Archive 2019.PST")
    old = ARCHIVE_RESUME_PREFIX + hashlib.blake2b(
        str(path).lower().encode("utf-8"), digest_size=16).hexdigest()
    assert _archive_resume_key(path) == old


def test_archive_resume_keys_differ_by_case_in_a_case_sensitive_folder(
        monkeypatch, two_by_case):
    from app.index.pipeline import _archive_resume_key

    monkeypatch.setattr(sys, "platform", "linux")
    case_sensitive(two_by_case)
    assert _archive_resume_key(two_by_case / "Mail.pst") != \
        _archive_resume_key(two_by_case / "mail.pst")
