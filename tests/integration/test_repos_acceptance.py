"""Repository awareness acceptance tests.

The criteria from `WORKORDER-git-search-backend.md` §11, verbatim:

  T1  A tree containing `repo/.git/` and `repo/src/a.py` yields one `repos`
      row, `kind='work'`, and `a.py` has its `repo_id`
  T2  A `.git` **file** containing `gitdir: ../.git/modules/lib` yields
      `kind='submodule'`; one containing any other `gitdir:` yields
      `kind='worktree'`
  T3  A file **directly in the repository root** is attributed, not only files
      in subdirectories. This is the ordering trap in §4.2
  T4  Nested repositories: a file under `repo/vendor/inner/` where both have a
      `.git` is attributed to `inner`, not to `repo`
  T5  An indexed root *below* a repository root attributes every file to the
      enclosing repository
  T6  A `.git` file that is empty, binary or unreadable is not a repository,
      and the walk completes with every other file indexed
  T7  `repo:name` and `repo:<root path>` both return that repository's files;
      an unknown name returns `[]`, never an error
  T8  `scope="code"` returns only files with a `repo_id`; `scope="documents"`
      still returns them too
  T9  Migrating a v5 index to v6 preserves every existing `files` row, and
      their `repo_id` is NULL until the next index run
  T10 Deleting a `repos` row leaves its files indexed and searchable with
      `repo_id` NULL
  T11 A walk over a tree with no `.git` anywhere writes nothing to `repos` and
      costs no measurable extra time - assert the walk still completes within
      the existing budget

**T11 is the one that protects everybody else.** This work is only justified
while it is free: `.git` is already in `DEFAULT_EXCLUDE_DIRS`, so the walker
stands next to the evidence on every pass. Noticing it must cost a membership
test against a list `os.walk` has already built, and nothing more.

Embedding uses an injected encoder, as Layer 3's tests do. What is under test
is attribution, not vectors.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig, enclosing_repo, walk
from app.search.keyword import _filter_sql
from app.search.query import parse_query
from app.storage.migrations import apply_migrations, read_version
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore

DIM = 384


def fake_encoder(texts):
    out = []
    for text in texts:
        seed = float(abs(hash(text)) % 1000)
        out.append(l2_normalise([math.sin(seed + i) for i in range(DIM)]))
    return out


def age(root: Path, *, seconds: int = 3600) -> None:
    """Backdate every file, out of the walker's recent-edit window."""
    when = time.time() - seconds
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (when, when))


@pytest.fixture()
def stores(tmp_path: Path):
    store = SqliteStore(tmp_path / "index.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=DIM).connect()
    yield store, vectors
    store.close()
    vectors.close()


def index(stores, root: Path, **overrides):
    store, vectors = stores
    config = PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".py", ".md", ".txt"})),
        workers=overrides.pop("workers", 2),
        # The free-space floor is a real check and not what these are about. A
        # build machine short of disk would otherwise stop every run at
        # `seen: 1, indexed: 0` and the failure would read as broken
        # attribution rather than a full disk.
        min_free_gb=overrides.pop("min_free_gb", 0),
        **overrides,
    )
    return Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder), config).run()


def attribution(store) -> dict[str, str | None]:
    """`basename -> repository name`, for every indexed file."""
    rows = store.conn.execute("""
        SELECT f.path, r.name FROM files f
        LEFT JOIN repos r ON r.id = f.repo_id
    """).fetchall()
    return {Path(row["path"]).name: row["name"] for row in rows}


def matching(store, query: str) -> list[str]:
    """Basenames the filters allow. The real `_filter_sql`, not an imitation."""
    where, params = _filter_sql(parse_query(query))
    rows = store.conn.execute(
        f"SELECT path FROM files f WHERE 1=1{where}", params).fetchall()
    return sorted(Path(row["path"]).name for row in rows)


def scoped(store, scope: str) -> list[str]:
    where, params = _filter_sql(parse_query("").scoped(scope))
    rows = store.conn.execute(
        f"SELECT path FROM files f WHERE 1=1{where}", params).fetchall()
    return sorted(Path(row["path"]).name for row in rows)


# --- T1 ---------------------------------------------------------------------

def test_t1_a_work_repository_is_detected_and_its_files_attributed(stores, tmp_path):
    root = tmp_path / "corpus"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "src").mkdir()
    (root / "repo" / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    repos = store.repos_list()
    assert len(repos) == 1
    assert repos[0]["name"] == "repo"
    assert repos[0]["kind"] == "work"
    assert attribution(store)["a.py"] == "repo"


# --- T2 ---------------------------------------------------------------------

@pytest.mark.parametrize("contents, expected", [
    ("gitdir: ../.git/modules/lib", "submodule"),
    ("gitdir: ../../.git/modules/deeply/nested", "submodule"),
    ("gitdir: /var/repos/main/.git/worktrees/feature", "worktree"),
    ("gitdir: ../elsewhere/.git", "worktree"),
    # Windows separators, because these files are written by Windows git.
    ("gitdir: ..\\..\\.git\\modules\\lib", "submodule"),
])
def test_t2_a_git_file_is_a_repository_and_its_kind_comes_from_the_target(
        stores, tmp_path, contents, expected):
    """**`.git` is not always a directory.**

    In a submodule or a linked worktree it is a *file*. Anything looking only
    at `os.walk`'s subdirectory list walks past both, which is the trap this
    case exists to close.
    """
    root = tmp_path / "corpus"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / ".git").write_text(contents, encoding="utf-8")
    (root / "lib" / "code.py").write_text("value = 2\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    repos = store.repos_list()
    assert len(repos) == 1, f"{contents!r} was not detected as a repository"
    assert repos[0]["kind"] == expected
    assert attribution(store)["code.py"] == "lib"


# --- T3 ---------------------------------------------------------------------

def test_t3_a_file_in_the_repository_root_is_attributed(stores, tmp_path):
    """**The ordering trap.**

    `os.walk(topdown=True)` visits a repository root in the same iteration
    that yields the files sitting directly in it. Detection placed after the
    filename loop leaves exactly those files unattributed while everything in
    subdirectories is attributed correctly - which reads as flakiness and gets
    blamed on the pipeline.

    Both files are asserted, because a version that attributes only the nested
    one passes any test that looks at just `deep.py`.
    """
    root = tmp_path / "corpus"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "at_the_root.py").write_text("top = 1\n", encoding="utf-8")
    (root / "repo" / "src").mkdir()
    (root / "repo" / "src" / "deep.py").write_text("deep = 2\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    seen = attribution(store)
    assert seen["at_the_root.py"] == "repo", "the file in the repository root was missed"
    assert seen["deep.py"] == "repo"


# --- T4 ---------------------------------------------------------------------

def test_t4_a_nested_repository_wins_over_its_parent(stores, tmp_path):
    """Longest matching prefix, not first match.

    A submodule's files are inside its parent's tree, so a first-match search
    over an unordered list attributes them to whichever root it happened to
    see first - right or wrong depending on dictionary order.
    """
    root = tmp_path / "corpus"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "outer.py").write_text("outer = 1\n", encoding="utf-8")
    inner = root / "repo" / "vendor" / "inner"
    (inner / ".git").mkdir(parents=True)
    (inner / "deep.py").write_text("inner = 2\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    seen = attribution(store)
    assert seen["deep.py"] == "inner", "the nested repository lost to its parent"
    assert seen["outer.py"] == "repo"
    assert {r["name"] for r in store.repos_list()} == {"repo", "inner"}


# --- T5 ---------------------------------------------------------------------

def test_t5_an_indexed_root_below_a_repository_root_still_attributes(stores, tmp_path):
    """`D:\\SearchProject\\app` has no `.git` beneath it and is still in one.

    A walk that only looks downwards attributes none of these files, and the
    downward walk is the only one there is - so this is entirely on
    `enclosing_repo`.
    """
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    (repo / "app").mkdir()
    (repo / "app" / "inside.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "app" / "sub").mkdir()
    (repo / "app" / "sub" / "deeper.py").write_text("y = 2\n", encoding="utf-8")
    age(repo)

    # The indexed root is `app`, not `project`.
    index(stores, repo / "app")
    store, _ = stores

    seen = attribution(store)
    assert seen["inside.py"] == "project"
    assert seen["deeper.py"] == "project"


def test_t5_enclosing_repo_stops_at_the_ceiling_and_returns_none(tmp_path):
    """The pure function, on its own. No repository anywhere above."""
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    assert enclosing_repo(deep, ceiling=tmp_path) is None


# --- T6 ---------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    b"",                                   # empty
    b"\x00\xff\xfe\x01binary garbage",     # binary
    b"this is not a gitdir pointer",       # text, wrong contents
    b"gitdir:",                            # the prefix and nothing else
    b"gitdir:   ",                         # whitespace only
])
def test_t6_a_broken_git_file_is_not_a_repository_and_the_walk_completes(
        stores, tmp_path, payload):
    """Detection is a convenience. It may never fail a walk.

    The posture `walk()` already states for a directory it cannot read: a
    permission error on one folder is not a reason to abandon a walk.
    """
    root = tmp_path / "corpus"
    (root / "odd").mkdir(parents=True)
    (root / "odd" / ".git").write_bytes(payload)
    (root / "odd" / "still_indexed.py").write_text("a = 1\n", encoding="utf-8")
    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "other.py").write_text("b = 2\n", encoding="utf-8")
    age(root)

    stats = index(stores, root)
    store, _ = stores

    assert store.repos_list() == []
    seen = attribution(store)
    assert seen["still_indexed.py"] is None
    assert seen["other.py"] is None
    assert stats.stopped_early is None, "the walk did not complete"


# --- T7 ---------------------------------------------------------------------

def test_t7_the_repo_filter_matches_by_name_and_by_root_path(stores, tmp_path):
    root = tmp_path / "corpus"
    (root / "leasha" / ".git").mkdir(parents=True)
    (root / "leasha" / "one.py").write_text("a = 1\n", encoding="utf-8")
    (root / "tools" / ".git").mkdir(parents=True)
    (root / "tools" / "two.py").write_text("b = 2\n", encoding="utf-8")
    (root / "loose").mkdir()
    (root / "loose" / "three.py").write_text("c = 3\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    assert matching(store, "repo:leasha") == ["one.py"]
    assert matching(store, "repo:LEASHA") == ["one.py"], "name match is case-sensitive"
    assert matching(store, f"repo:{root / 'leasha'}") == ["one.py"], "path match failed"
    # Several at once, ORed - a file is in exactly one repository, so ANDing
    # repeated names could never match anything.
    assert matching(store, "repo:leasha,tools") == ["one.py", "two.py"]
    # Unknown: empty, never an error.
    assert matching(store, "repo:nosuchproject") == []


# --- T8 ---------------------------------------------------------------------

def test_t8_the_code_scope_is_repository_membership_not_file_type(stores, tmp_path):
    """And `documents` must keep meaning what it means.

    The scope is a narrowing offered to the person searching, not a partition
    of the corpus - somebody looking through Documents for a connection string
    they know they wrote must still find it.
    """
    root = tmp_path / "corpus"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "code.py").write_text("a = 1\n", encoding="utf-8")
    # A .md inside a repository IS code scope; a .py outside it is NOT.
    (root / "repo" / "README.md").write_text("# notes\n", encoding="utf-8")
    (root / "downloads").mkdir()
    (root / "downloads" / "orphan.py").write_text("b = 2\n", encoding="utf-8")
    (root / "downloads" / "notes.txt").write_text("plain\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores

    assert scoped(store, "code") == ["README.md", "code.py"]
    assert scoped(store, "documents") == [
        "README.md", "code.py", "notes.txt", "orphan.py",
    ], "documents quietly stopped including repository files"


# --- T9 ---------------------------------------------------------------------

def test_t9_migrating_v5_to_v6_preserves_every_file_row(tmp_path):
    """The path that happens for real: an existing index, not a fresh one."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)

    # Rewind to exactly what v5 left behind.
    conn.execute("DROP INDEX IF EXISTS idx_files_repo")
    conn.execute("ALTER TABLE files DROP COLUMN repo_id")
    conn.execute("DROP TABLE IF EXISTS repos")
    conn.execute("UPDATE schema_version SET version = 5")
    conn.executemany(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, status, "
        "source_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(f"/x{n}.py", "/", "py", 1, n, "INDEXED", "file") for n in range(40)])
    conn.commit()
    assert read_version(conn) == 5

    apply_migrations(conn)

    assert read_version(conn) == 6
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 40
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE repo_id IS NULL").fetchone()[0] == 40
    # Re-runnable: a half-finished migration must be repeatable.
    apply_migrations(conn)
    conn.close()


# --- T10 --------------------------------------------------------------------

def test_t10_deleting_a_repository_leaves_its_files_indexed(stores, tmp_path):
    """`ON DELETE SET NULL`, deliberately not `CASCADE`.

    A repository that is moved, deleted or unmounted must not take the indexed
    content of its files with it - re-reading 40,000 files because a folder was
    renamed is not a behaviour anybody would ask for.
    """
    root = tmp_path / "corpus"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "kept.py").write_text("a = 1\n", encoding="utf-8")
    age(root)

    index(stores, root)
    store, _ = stores
    before = store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert before > 0

    with store.write() as conn:
        conn.execute("DELETE FROM repos")

    row = store.conn.execute(
        "SELECT path, repo_id FROM files WHERE path LIKE '%kept.py'").fetchone()
    assert row is not None, "the file was deleted with its repository"
    assert row["repo_id"] is None
    assert store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == before
    # ...and still searchable, which is the point.
    assert matching(store, "kept") != [] or scoped(store, "documents") == ["kept.py"]


# --- T11 --------------------------------------------------------------------

def test_t11_a_tree_with_no_git_writes_nothing_and_costs_nothing(tmp_path):
    """**The one that protects everybody else.**

    This work is only justified while it is free. `.git` is already in
    `DEFAULT_EXCLUDE_DIRS`, so the walker stands next to the evidence on every
    pass - noticing it must cost a membership test against a list `os.walk`
    has already built, and nothing more.

    Compared against the same walk with detection switched off, on the same
    tree, rather than against an absolute number - an absolute budget on a
    shared CI machine measures the machine.
    """
    root = tmp_path / "corpus"
    for n in range(30):
        directory = root / f"dir{n:02d}"
        directory.mkdir(parents=True)
        for m in range(20):
            (directory / f"f{m:02d}.py").write_text(f"v = {m}\n", encoding="utf-8")
    age(root)

    def timed(sink):
        config = WalkConfig(roots=[root], extensions=frozenset({".py"}),
                            repo_sink=sink)
        start = time.perf_counter()
        count = sum(1 for _ in walk(config))
        return count, time.perf_counter() - start

    # Warm the directory cache so the first run does not pay for both.
    timed(None)

    without_count, without = timed(None)
    sink: dict[str, str] = {}
    with_count, with_detection = timed(sink)

    assert sink == {}, "a tree with no .git wrote to the repository sink"
    assert with_count == without_count == 600

    # Generous, deliberately: this asserts "no new order of cost", not a
    # microbenchmark. A second walk of the tree - the thing this design exists
    # to avoid - would be far outside it.
    assert with_detection < without * 3 + 0.5, (
        f"detection cost {with_detection:.3f}s against {without:.3f}s without")


def test_t11_detection_adds_no_io_when_there_is_no_dot_git(tmp_path, monkeypatch):
    """The claim behind T11, asserted directly rather than by a clock.

    A timing test on a busy machine can pass while the code opens a file per
    directory. This fails if detection reads anything at all from a tree that
    holds no `.git`.
    """
    root = tmp_path / "corpus"
    for n in range(5):
        (root / f"dir{n}").mkdir(parents=True)
        (root / f"dir{n}" / "a.py").write_text("v = 1\n", encoding="utf-8")
    age(root)

    opened: list[str] = []
    real_open = Path.open

    def watched(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched)
    sink: dict[str, str] = {}
    list(walk(WalkConfig(roots=[root], extensions=frozenset({".py"}),
                         repo_sink=sink)))

    assert sink == {}
    assert not [p for p in opened if p.endswith(".git")], (
        f"detection opened a .git that does not exist: {opened}")


# --- hidden files: `.git` is hidden on Windows -------------------------------

def _hide(path: Path) -> bool:
    """Set FILE_ATTRIBUTE_HIDDEN. True if it took effect."""
    if os.name != "nt":
        return False
    import ctypes

    hidden_attribute = 0x02
    return bool(ctypes.windll.kernel32.SetFileAttributesW(
        str(path), hidden_attribute))


def test_the_walker_never_consults_the_hidden_attribute(stores, tmp_path):
    """**Hidden files are indexed, deliberately.**

    `.git` is created with FILE_ATTRIBUTE_HIDDEN by git on Windows, so a walker
    that skipped hidden entries would find no repositories at all on the only
    platform this ships to - and the whole feature would pass every test here
    and do nothing on a real machine.

    It would also lose real content: `.env`, dotfiles, and anything a user has
    hidden are still theirs and still worth finding.

    Asserted structurally as well as behaviourally, because the behavioural
    half cannot run off Windows: nothing in the walk path reads the hidden bit.
    """
    from app.index import walker as walker_module

    source = Path(walker_module.__file__).read_text(encoding="utf-8")
    assert "HIDDEN" not in source.upper().replace("FILE_ATTRIBUTE_HIDDEN_", ""), (
        "the walker started consulting the hidden attribute")

    # No blanket dotfile exclusion either - that is the POSIX equivalent and
    # would hide `.git` just as effectively.
    assert ".*" not in walker_module.DEFAULT_EXCLUDE_GLOBS
    assert "*" not in walker_module.DEFAULT_EXCLUDE_GLOBS


def test_a_hidden_file_is_still_indexed(stores, tmp_path):
    """A dot-prefixed file with no directory involved."""
    root = tmp_path / "corpus"
    root.mkdir()
    hidden = root / ".hidden_notes.txt"
    hidden.write_text("the connection string is here\n", encoding="utf-8")
    (root / "plain.txt").write_text("ordinary\n", encoding="utf-8")
    _hide(hidden)
    age(root)

    index(stores, root)
    store, _ = stores

    assert set(attribution(store)) == {".hidden_notes.txt", "plain.txt"}


@pytest.mark.skipif(os.name != "nt", reason="FILE_ATTRIBUTE_HIDDEN is Windows-only")
def test_a_hidden_git_directory_is_still_detected(stores, tmp_path):
    """The real shape on the target platform: git marks `.git` hidden."""
    root = tmp_path / "corpus"
    git_dir = root / "repo" / ".git"
    git_dir.mkdir(parents=True)
    (root / "repo" / "a.py").write_text("value = 1\n", encoding="utf-8")
    assert _hide(git_dir), "could not set the hidden attribute"
    age(root)

    index(stores, root)
    store, _ = stores

    assert [r["kind"] for r in store.repos_list()] == ["work"]
    assert attribution(store)["a.py"] == "repo"


@pytest.mark.skipif(os.name != "nt", reason="FILE_ATTRIBUTE_HIDDEN is Windows-only")
def test_a_hidden_git_file_is_still_detected(stores, tmp_path):
    """And the submodule form, which is a hidden *file* rather than a folder."""
    root = tmp_path / "corpus"
    (root / "lib").mkdir(parents=True)
    marker = root / "lib" / ".git"
    marker.write_text("gitdir: ../.git/modules/lib", encoding="utf-8")
    (root / "lib" / "code.py").write_text("value = 2\n", encoding="utf-8")
    assert _hide(marker), "could not set the hidden attribute"
    age(root)

    index(stores, root)
    store, _ = stores

    assert [r["kind"] for r in store.repos_list()] == ["submodule"]
    assert attribution(store)["code.py"] == "lib"


# --- a vector write that produced nothing must not report success -----------

def test_a_failed_vector_write_leaves_the_file_pending(stores, tmp_path):
    """**How a file ends up INDEXED with no vector, permanently.**

    `VectorStore.add` returns how many rows it wrote, and the pipeline ignored
    it - marking the chunks embedded and the file INDEXED regardless. A write
    that produced nothing was therefore recorded as complete, and because the
    file was then INDEXED and unchanged, every later run skipped it. Stuck for
    good, with the only symptom being that meaning-based search quietly covered
    less of the corpus than it claimed.

    PENDING is the state that gets retried, so that is where such a file
    belongs. The chunks are already written, so the retry costs only the
    embedding.
    """
    root = tmp_path / "corpus"
    root.mkdir()
    for n in range(4):
        (root / f"f{n}.txt").write_text(f"document {n} about pumps", encoding="utf-8")
    age(root)

    store, vectors = stores
    from app.index.pipeline import Pipeline, PipelineConfig

    config = PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".txt"})),
        workers=1, min_free_gb=0,
    )
    pipeline = Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder),
                        config)
    # The write silently does nothing, which is what a full disk or a LanceDB
    # hiccup looks like from here.
    real_add = vectors.add
    vectors.add = lambda **_kwargs: 0
    try:
        pipeline.run()
    finally:
        vectors.add = real_add

    statuses = store.stats()["files"]
    assert statuses.get("INDEXED", 0) == 0, (
        "a file was marked INDEXED with no vector, and will never be retried")
    assert statuses.get("PENDING", 0) == 4
    assert store.stats()["chunks_embedded"] == 0


def test_the_retry_recovers_it_on_the_next_run(stores, tmp_path):
    """PENDING is only the right answer if a later run actually fixes it."""
    root = tmp_path / "corpus"
    root.mkdir()
    for n in range(4):
        (root / f"f{n}.txt").write_text(f"document {n} about pumps", encoding="utf-8")
    age(root)

    store, vectors = stores
    from app.index.pipeline import Pipeline, PipelineConfig

    config = PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".txt"})),
        workers=1, min_free_gb=0,
    )
    broken = Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder), config)
    real_add = vectors.add
    vectors.add = lambda **_kwargs: 0
    broken.run()
    assert store.stats()["files"].get("INDEXED", 0) == 0

    # A normal run, with the vector store working again. Restored explicitly:
    # the fixture hands both pipelines the *same* VectorStore, so a patch left
    # in place makes the second run fail for the first run's reason - which is
    # what the first version of this test did.
    vectors.add = real_add
    Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder), config).run()

    stats = store.stats()
    assert stats["files"].get("PENDING", 0) == 0
    assert stats["chunks_embedded"] == stats["chunks_total"]
    assert vectors.count() == stats["chunks_total"]
