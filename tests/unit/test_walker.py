"""Layer 3: the walker.

Two properties carry the layer:

1. **Exclusions prune, they do not filter.** Descending into a huge excluded
   subtree and discarding the results afterwards costs the whole subtree. Proved
   by counting how many directories `os.walk` actually visits, not by checking
   the output - a filter-afterwards implementation produces identical output and
   takes a hundred times longer.
2. **A file that changed is never called unchanged.** Everything downstream
   trusts that answer, and a missed edit is invisible: no error, no warning, just
   a document whose new contents are unfindable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.winfs import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
from app.index.walker import (
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_EXCLUDE_GLOBS,
    Candidate,
    WalkConfig,
    content_hash,
    has_changed,
    walk,
)

TEXT = frozenset({".txt", ".md", ".pdf"})


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def names(candidates) -> set[str]:
    return {c.path.name for c in candidates}


# --- what gets picked up ----------------------------------------------------

def test_finds_supported_files_recursively(tmp_path: Path) -> None:
    make_tree(tmp_path, {
        "a.txt": "one", "sub/b.txt": "two", "sub/deep/c.md": "three",
    })
    found = list(walk(WalkConfig(roots=[tmp_path], extensions=TEXT)))
    assert names(found) == {"a.txt", "b.txt", "c.md"}


def test_unsupported_extensions_are_ignored(tmp_path: Path) -> None:
    make_tree(tmp_path, {"keep.txt": "x", "skip.dll": "x", "skip.exe": "x"})
    assert names(walk(WalkConfig(roots=[tmp_path], extensions=TEXT))) == {"keep.txt"}


def test_empty_files_are_skipped(tmp_path: Path) -> None:
    """Zero bytes cannot contain text, and would cost a parse to discover that."""
    make_tree(tmp_path, {"real.txt": "content"})
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert names(walk(WalkConfig(roots=[tmp_path], extensions=TEXT))) == {"real.txt"}


def test_oversized_files_are_skipped(tmp_path: Path) -> None:
    make_tree(tmp_path, {"small.txt": "x", "big.txt": "y" * 5000})
    config = WalkConfig(roots=[tmp_path], extensions=TEXT, max_file_bytes=1000)
    assert names(walk(config)) == {"small.txt"}


def test_missing_root_is_not_fatal(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": "x"})
    config = WalkConfig(roots=[tmp_path, tmp_path / "does-not-exist"], extensions=TEXT)
    assert names(walk(config)) == {"a.txt"}


def test_overlapping_roots_do_not_double_index(tmp_path: Path) -> None:
    make_tree(tmp_path, {"sub/a.txt": "x"})
    config = WalkConfig(roots=[tmp_path, tmp_path / "sub"], extensions=TEXT)
    found = list(walk(config))
    assert len(found) == 1


def test_extensions_default_to_the_extractor_registry(tmp_path: Path) -> None:
    """Adding an extractor must not require editing walker config."""
    make_tree(tmp_path, {"a.txt": "x", "b.dll": "x"})
    assert names(walk(WalkConfig(roots=[tmp_path]))) == {"a.txt"}


# --- exclusions -------------------------------------------------------------

def test_excluded_directories_are_not_descended_into(tmp_path: Path, monkeypatch) -> None:
    """The property that matters. A filter-afterwards implementation produces
    the same output and reads the whole subtree to do it."""
    make_tree(tmp_path, {"keep.txt": "x"})
    for i in range(20):
        (tmp_path / "node_modules" / f"pkg{i}").mkdir(parents=True)
        (tmp_path / "node_modules" / f"pkg{i}" / "readme.txt").write_text("noise")

    visited: list[str] = []
    real_walk = os.walk

    def counting_walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            visited.append(entry[0])
            yield entry

    monkeypatch.setattr(os, "walk", counting_walk)

    found = names(walk(WalkConfig(roots=[tmp_path], extensions=TEXT)))
    assert found == {"keep.txt"}
    assert not any("pkg" in directory for directory in visited), (
        "node_modules subtree was descended into; exclusions must prune, not filter"
    )


def test_office_lock_files_are_skipped(tmp_path: Path) -> None:
    """`~$report.docx` is a live copy of a document already being indexed."""
    make_tree(tmp_path, {"report.txt": "real", "~$report.txt": "lock"})
    assert names(walk(WalkConfig(roots=[tmp_path], extensions=TEXT))) == {"report.txt"}


@pytest.mark.parametrize("noise", ["a.tmp", "b.partial", "Thumbs.db", "c.crdownload"])
def test_transient_files_are_skipped(tmp_path: Path, noise: str) -> None:
    make_tree(tmp_path, {"real.txt": "x", noise: "x"})
    found = names(walk(WalkConfig(roots=[tmp_path], extensions=TEXT | {".tmp", ".partial", ".db", ".crdownload"})))
    assert found == {"real.txt"}


def test_force_include_overrides_an_excluded_name(tmp_path: Path) -> None:
    """Someone's real work living under a folder called `build` should still index."""
    target = tmp_path / "build"
    make_tree(tmp_path, {"build/spec.txt": "real work"})

    config = WalkConfig(roots=[tmp_path], extensions=TEXT)
    assert names(walk(config)) == set()

    config = WalkConfig(roots=[tmp_path], extensions=TEXT, force_include=frozenset({str(target)}))
    assert names(walk(config)) == {"spec.txt"}


def test_default_exclusions_cover_the_usual_suspects() -> None:
    for name in ("node_modules", ".git", "AppData", "$Recycle.Bin", "venv"):
        assert name in DEFAULT_EXCLUDE_DIRS
    assert "~$*" in DEFAULT_EXCLUDE_GLOBS


# --- cloud placeholders -----------------------------------------------------

def test_cloud_placeholders_are_skipped_without_being_read(tmp_path: Path) -> None:
    """Opening a placeholder is what triggers the download, so the decision has
    to come from the stat that was already performed."""
    placeholder = Candidate(
        path=tmp_path / "cloud.txt", size_bytes=10, mtime_ns=1,
        attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    )
    local = Candidate(path=tmp_path / "local.txt", size_bytes=10, mtime_ns=1, attributes=0)

    assert placeholder.is_cloud_placeholder
    assert not local.is_cloud_placeholder


def test_candidate_without_attributes_is_not_a_placeholder(tmp_path: Path) -> None:
    """Off Windows there are no attribute bits; that is not a placeholder."""
    assert not Candidate(path=tmp_path / "x.txt", size_bytes=1, mtime_ns=1).is_cloud_placeholder


# --- prioritisation ---------------------------------------------------------

def test_nominated_folders_sort_first(tmp_path: Path) -> None:
    """Search should become useful in minutes, not after the whole corpus."""
    make_tree(tmp_path, {
        "archive/old.txt": "x", "current/now.txt": "x", "other/misc.txt": "x",
    })
    config = WalkConfig(
        roots=[tmp_path], extensions=TEXT,
        priority_roots=[tmp_path / "current", tmp_path / "archive"],
    )
    ordered = sorted(walk(config), key=Candidate.sort_key)
    assert [c.path.name for c in ordered] == ["now.txt", "old.txt", "misc.txt"]


def test_sort_order_is_deterministic(tmp_path: Path) -> None:
    """A resumed run picks up where it stopped only if the order is stable."""
    make_tree(tmp_path, {f"f{i}.txt": "x" for i in range(20)})
    config = WalkConfig(roots=[tmp_path], extensions=TEXT)
    first = [c.path for c in sorted(walk(config), key=Candidate.sort_key)]
    second = [c.path for c in sorted(walk(config), key=Candidate.sort_key)]
    assert first == second


# --- laziness ---------------------------------------------------------------

def test_walk_is_lazy(tmp_path: Path) -> None:
    """100GB must start producing work immediately, not after full enumeration."""
    make_tree(tmp_path, {f"f{i}.txt": "x" for i in range(200)})
    walker = walk(WalkConfig(roots=[tmp_path], extensions=TEXT))
    first = next(walker)
    assert first.path.suffix == ".txt"


# --- hashing ----------------------------------------------------------------

def test_hash_is_stable_and_content_addressed(tmp_path: Path) -> None:
    one = tmp_path / "a.txt"
    two = tmp_path / "b.txt"
    one.write_text("identical", encoding="utf-8")
    two.write_text("identical", encoding="utf-8")
    assert content_hash(one) == content_hash(two)

    two.write_text("different", encoding="utf-8")
    assert content_hash(one) != content_hash(two)


def test_hash_streams_rather_than_loading(tmp_path: Path) -> None:
    """The corpus holds multi-gigabyte archives; reading one whole to hash it
    would be its own outage."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    assert content_hash(big, chunk_bytes=64 * 1024)
    assert content_hash(big, chunk_bytes=64 * 1024) == content_hash(big, chunk_bytes=1024)


# --- change detection -------------------------------------------------------

def candidate_for(path: Path, priority: int = 100) -> Candidate:
    stat = path.stat()
    return Candidate(path=path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                     priority=priority)


def test_a_new_file_is_changed_and_gets_hashed(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    target.write_text("hello", encoding="utf-8")

    changed, digest = has_changed(candidate_for(target), known_mtime_ns=None, known_size=None)
    assert changed
    assert digest == content_hash(target)


def test_unchanged_file_is_not_read_at_all(tmp_path: Path, monkeypatch) -> None:
    """The whole point: an unchanged 100GB corpus costs a directory walk, not a
    read of every byte."""
    target = tmp_path / "same.txt"
    target.write_text("hello", encoding="utf-8")
    candidate = candidate_for(target)

    def explode(*_args, **_kwargs):
        raise AssertionError("an unchanged file must not be opened")

    monkeypatch.setattr(Path, "open", explode)

    changed, digest = has_changed(
        candidate,
        known_mtime_ns=candidate.mtime_ns,
        known_size=candidate.size_bytes,
        known_hash="stored",
    )
    assert not changed
    assert digest == "stored"


def test_edited_file_is_detected(tmp_path: Path) -> None:
    target = tmp_path / "edited.txt"
    target.write_text("before", encoding="utf-8")
    before = candidate_for(target)
    before_hash = content_hash(target)

    target.write_text("after, and longer", encoding="utf-8")
    after = candidate_for(target)

    changed, digest = has_changed(
        after, known_mtime_ns=before.mtime_ns, known_size=before.size_bytes,
        known_hash=before_hash,
    )
    assert changed
    assert digest != before_hash


def test_touched_but_identical_file_is_not_reindexed(tmp_path: Path) -> None:
    """The case that decides whether an incremental pass is cheap.

    robocopy, a restore from backup, cloud sync and archive extraction all reset
    mtime while leaving content identical. Trusting mtime alone would re-index
    the entire corpus every time any of those happened.
    """
    target = tmp_path / "touched.txt"
    target.write_text("identical content", encoding="utf-8")
    original = candidate_for(target)
    digest = content_hash(target)

    os.utime(target, ns=(original.mtime_ns + 5_000_000_000, original.mtime_ns + 5_000_000_000))
    touched = candidate_for(target)
    assert touched.mtime_ns != original.mtime_ns, "mtime really did move"

    changed, new_hash = has_changed(
        touched, known_mtime_ns=original.mtime_ns, known_size=original.size_bytes,
        known_hash=digest,
    )
    assert not changed, "same bytes, so no re-index however the timestamp moved"
    assert new_hash == digest


def test_same_size_different_content_is_caught(tmp_path: Path) -> None:
    """Size is a weak signal on its own - an edit that preserves length is
    exactly what a careless implementation misses."""
    target = tmp_path / "swap.txt"
    target.write_text("aaaa", encoding="utf-8")
    original = candidate_for(target)
    digest = content_hash(target)

    os.utime(target, ns=(original.mtime_ns + 1_000_000_000,) * 2)
    target.write_text("bbbb", encoding="utf-8")
    edited = candidate_for(target)
    assert edited.size_bytes == original.size_bytes

    changed, _ = has_changed(
        edited, known_mtime_ns=original.mtime_ns, known_size=original.size_bytes,
        known_hash=digest,
    )
    assert changed


def test_fast_mode_trusts_the_cheap_tier(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("content", encoding="utf-8")
    candidate = candidate_for(target)

    changed, digest = has_changed(
        candidate, known_mtime_ns=candidate.mtime_ns - 1, known_size=candidate.size_bytes,
        known_hash="stored", verify_hash=False,
    )
    assert changed and digest is None


def test_unreadable_file_is_treated_as_changed(tmp_path: Path, monkeypatch) -> None:
    """So the pipeline attempts it and produces a proper per-file AppError,
    rather than the walker silently deciding it was fine."""
    target = tmp_path / "locked.txt"
    target.write_text("content", encoding="utf-8")
    candidate = candidate_for(target)

    def refuse(*_args, **_kwargs):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(Path, "open", refuse)

    changed, digest = has_changed(
        candidate, known_mtime_ns=candidate.mtime_ns + 1, known_size=99,
        known_hash="stored",
    )
    assert changed and digest is None


# --- candidate shape --------------------------------------------------------

def test_candidate_exposes_what_the_files_table_needs(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "doc.PDF"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    candidate = candidate_for(target)

    assert candidate.ext == ".pdf", "normalised, because the registry keys on lowercase"
    assert candidate.parent_dir == str(tmp_path / "sub")
    assert candidate.size_bytes == 1
