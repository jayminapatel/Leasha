r"""`Dockerfile` and `Makefile` can be filtered for, not merely found.

Layer: L2 / L3

From `docs/WORKORDER-inbound-ui-fixes.md` §4.

`NAMED_FILES` made these indexable - a repository indexed without them is
missing the file that says how it is built - and in doing so made them
**unfilterable**. `files.ext` was `''` for every one, `distinct_values` skips
those rows, and `type:` matches on that column. So they were in the index and
searchable by content, and could not be narrowed to, offered in the `/type`
menu, or named in a query at all.

A gap created by fixing something else, which is the usual way, and invisible
from either end: the content search works, so nothing looks broken.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.extract.source_types import NAMED_FILES, indexed_ext
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

LONG_AGO = 3600


# --- the column value -------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("notes.txt", "txt"),
    ("Program.CS", "cs"),
    ("Makefile", "makefile"),
    ("Dockerfile", "dockerfile"),
    (".gitignore", "gitignore"),
    (".editorconfig", "editorconfig"),
])
def test_the_stored_type_is_the_suffix_or_the_name(name, expected):
    assert indexed_ext(name) == expected


def test_a_leading_dot_is_stripped():
    """Consistent with every other value in the column, none of which carry
    one - and with what somebody would type."""
    assert not indexed_ext(".gitignore").startswith(".")


def test_an_extensionless_file_nobody_named_gets_nothing():
    """The walker only admits named files, but a stray value in this column
    would be offered in the menu as though it were a type."""
    assert indexed_ext("some-random-binary") == ""
    assert indexed_ext("core") == ""


def test_a_windows_path_is_split_on_windows_separators():
    r"""**The fifth time this project has been caught by this.**

    `PurePosixPath(r"D:\Repo\Dockerfile").name` is the whole string, so a
    membership test against `NAMED_FILES` silently never matches off Windows -
    and every test of it passes for the wrong reason, because the production
    path is a `WindowsPath` where it happens to work.
    """
    assert indexed_ext(r"D:\Repo\Dockerfile") == "dockerfile"
    assert indexed_ext(r"D:\Repo\src\.gitignore") == "gitignore"
    assert indexed_ext("/home/x/Makefile") == "makefile"


def test_every_named_file_produces_something_filterable():
    """Not one of them may come out empty - an empty value here is exactly the
    state this fixes."""
    empty = sorted(name for name in NAMED_FILES if not indexed_ext(name))

    assert empty == []


# --- through a real run -----------------------------------------------------

class NullVectors:
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


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def indexed(tmp_path):
    root = tmp_path / "repo"
    _write(root / "Makefile", "build:\n\tgo build ./...\n")
    _write(root / "Dockerfile", "FROM alpine\nRUN apk add curl\n")
    _write(root / ".gitignore", "venv/\n__pycache__/\n")
    _write(root / "main.py", "print('hello')\n")

    with SqliteStore(tmp_path / "index.db") as store:
        Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1),
        ).run()
        yield store


def test_a_named_file_lands_with_a_real_type(indexed):
    kinds = {
        Path(record.path).name: record.ext
        for record in indexed.iter_files(source_kind="file")
    }

    assert kinds["Makefile"] == "makefile"
    assert kinds["Dockerfile"] == "dockerfile"
    assert kinds[".gitignore"] == "gitignore"
    assert kinds["main.py"] == "py"


def test_the_type_menu_now_offers_them(indexed):
    r"""**The end this was reported from.** `distinct_values` filters
    `WHERE ext <> ''`, so before this these rows could not appear in `/type`
    however the menu was built."""
    from app.ui.presenter import value_suggestions

    offered = value_suggestions(indexed, "type")

    assert "makefile" in offered
    assert "dockerfile" in offered


def test_they_can_actually_be_filtered_for(indexed):
    """A value offered in a menu that matches nothing is how a working filter
    looks broken - so the value and the filter are tested together."""
    rows = indexed.distinct_values("ext", prefix="docker", limit=10)

    assert "dockerfile" in [str(row).lower() for row in rows]


def test_the_backfill_is_stated_rather_than_assumed(indexed):
    """`ext` is derived and an unchanged file is not rewritten, so a corpus
    indexed before this keeps its empty values until `--force`. Asserting the
    docstring says so, because that sentence is the difference between a known
    limitation and "the setting did not work"."""
    assert "--force" in (indexed_ext.__doc__ or "")
