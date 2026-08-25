r"""What the Code tab counts as code.

Layer: L0 / L1

Reported from the window: *"on the code search ... at the moment its bringing
files which are not code"*. `code_files` narrows on `repo_id` and `source_kind`
and nothing else, so the tab listed everything *located* in a repository - the
PDFs, spreadsheets, images and logs that happen to live in the folder.
`app.cli repos` has warned about exactly this since repositories were added.

**The rule these tests exist to protect is the asymmetry**: this decides what is
*listed*, and must never decide what is *read*. Getting it backwards would turn
a tidy-up of one tab into silent loss of search coverage, and the person doing
it would have no reason to suspect anything.
"""

from __future__ import annotations

import pytest

from app.core.code_types import (
    BUILD_GROUPS,
    DEFAULT_PRESET,
    DOC_GROUPS,
    PRESET_LABELS,
    SOURCE_GROUPS,
    describe,
    dump_choice,
    extensions_for,
    group_names,
    groups_for,
    parse_choice,
)
from app.extract.source_types import BY_ECOSYSTEM


# --- the presets ------------------------------------------------------------

def test_the_default_is_source_config_and_build():
    """Agreed with the owner. It is what "the code of this project" means to
    most people: the languages plus the files that build them."""
    assert DEFAULT_PRESET == "build"
    groups = set(groups_for(DEFAULT_PRESET))
    assert set(SOURCE_GROUPS) <= groups
    assert set(BUILD_GROUPS) <= groups
    assert not set(DOC_GROUPS) & groups, "documentation is off by default"


@pytest.mark.parametrize("extension,shown", [
    ("cs", True), ("pkb", True), ("cbl", True), ("ps1", True),
    ("tf", True), ("dockerfile", True), ("makefile", True), ("csproj", True),
    ("pdf", False), ("xlsx", False), ("png", False), ("pst", False),
    ("md", False), ("csv", False),
])
def test_what_the_default_shows_and_hides(extension, shown):
    """The list somebody actually sees. Named one by one because "382 types"
    is not something anybody can check."""
    wanted = extensions_for(DEFAULT_PRESET)
    assert wanted is not None
    assert (extension in wanted) is shown


def test_documentation_is_the_boundary_case_and_is_offered():
    """`.md` and a 200MB CSV export are the same group, which is why it is a
    choice rather than a decision made silently."""
    with_docs = extensions_for("docs")
    assert with_docs is not None
    assert "md" in with_docs
    assert "md" not in (extensions_for("build") or ())


def test_everything_means_no_filter_rather_than_every_group():
    r"""**`None` is not the empty set, and the difference is load-bearing.**

    `None` tells the caller to add no `WHERE ext IN (...)` clause at all, so a
    file type nobody has classified is still listed - which is what "everything
    in the repository" has to mean. Returning every known extension instead
    would quietly hide anything the catalogue has not heard of.
    """
    assert extensions_for("all") is None


def test_a_preset_that_resolves_to_nothing_shows_everything():
    """The failure direction matters: a Code tab that has silently hidden a
    language is far harder to notice than one showing a stray PDF."""
    assert extensions_for("custom", []) is None


def test_custom_keeps_only_groups_that_exist():
    """A group renamed in `source_types.py` must not silently filter to
    nothing - it falls out, and the rest still apply."""
    chosen = ["Web", "A Group That Was Renamed"]

    assert groups_for("custom", chosen) == ("Web",)
    assert extensions_for("custom", chosen) == frozenset(
        e.lstrip(".") for e in BY_ECOSYSTEM["Web"])


def test_named_files_arrive_with_the_build_group():
    """`Makefile` and `Dockerfile` are stored under their own name rather than
    an extension - see `source_types.indexed_ext` - and are exactly what
    somebody switching on "config and build" means."""
    wanted = extensions_for("build") or frozenset()

    assert {"makefile", "dockerfile"} <= wanted
    assert not any(e.startswith(".") for e in wanted), (
        "`files.ext` holds no leading dot; a filter with one matches nothing")


def test_every_group_named_in_a_preset_really_exists():
    """A typo here filters to fewer types than intended and nothing says so."""
    known = set(BY_ECOSYSTEM)
    missing = sorted(
        name for preset in ("source", "build", "docs")
        for name in groups_for(preset) if name not in known)

    assert missing == []


def test_the_groups_cover_the_catalogue():
    """Source, build and documentation between them account for every group -
    so "everything except documentation" is a complete statement rather than
    one that quietly drops an ecosystem."""
    assert set(SOURCE_GROUPS) | set(BUILD_GROUPS) | set(DOC_GROUPS) == set(group_names())


# --- storage ----------------------------------------------------------------

def test_a_choice_survives_a_round_trip():
    assert parse_choice(dump_choice("custom", ["Web"])) == ("custom", ("Web",))


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"preset": "banana"}'])
def test_an_unreadable_choice_is_the_default(raw):
    """Never an exception, and never "show nothing"."""
    preset, _chosen = parse_choice(raw)
    assert preset == DEFAULT_PRESET


def test_every_preset_has_a_label():
    for key in (*("source", "build", "docs", "all"), "custom"):
        assert PRESET_LABELS[key].strip()


# --- the promise this must keep ---------------------------------------------

def test_it_says_that_nothing_is_removed_from_the_index():
    r"""**The sentence is the feature.**

    There are now two file-type editors on the Settings page. The one below
    decides what is *read* - unticking `.pdf` there makes PDFs unsearchable.
    This one decides what one tab *lists*. Somebody who mixes them up either
    loses search coverage or wonders why unticking changed nothing, and the
    only thing standing between them and that is this wording.
    """
    text = describe(DEFAULT_PRESET)

    assert "indexed and searchable" in text
    assert "Code tab" in text


def test_it_never_touches_the_reading_configuration():
    """Asserted structurally: nothing in this module writes anything, and it
    imports no configuration writer. A future edit that reached for
    `save_overrides` would be the mistake this whole design is avoiding."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/core/code_types.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else
        getattr(node.func, "id", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    # **Parsed, not grepped.** The docstring names `extractors.toml` on purpose
    # - explaining the distinction is the point of the module - so a text search
    # fails on its own documentation. What matters is what it *calls*.
    for forbidden in ("save_overrides", "with_override", "set_state",
                      "write_env", "apply_values"):
        assert forbidden not in called, (
            f"code_types.py calls {forbidden} - it decides what is listed, "
            f"never what is read or written")


# --- through the store ------------------------------------------------------

def test_the_filter_reaches_the_query(tmp_path):
    """End to end, because the extensions are only right if they are in the
    shape `files.ext` stores - no dot, lower case."""
    from app.core.code_types import STATE_KEY
    from app.storage.sqlite_store import SqliteStore
    from app.ui.presenter import code_type_filter

    def basename(path: str) -> str:
        """**Not `Path(...).name`.** These are Windows paths and the tests run
        anywhere: `PurePosixPath(r"D:\\Repo\\Order.cs").name` is the whole
        string. The sixth time this project has been caught by it, and the
        first time in a test of my own."""
        return str(path).replace("\\", "/").rstrip("/").rpartition("/")[2]

    with SqliteStore(tmp_path / "i.db") as store:
        repo = store.upsert_repo(r"D:\Repo\leasha", kind="work")
        for name, ext in [("Order.cs", "cs"), ("notes.pdf", "pdf"),
                          ("Dockerfile", "dockerfile"), ("README.md", "md")]:
            store.mark_indexed(store.upsert_file(
                rf"D:\Repo\leasha\{name}", size_bytes=10, mtime_ns=1,
                ext=ext, repo_id=repo, parent_dir=r"D:\Repo\leasha"))

        def listed() -> list[str]:
            return sorted(basename(row["path"]) for row in store.code_files(
                "", ext=code_type_filter(store), limit=50))

        assert listed() == ["Dockerfile", "Order.cs"], "the default let a PDF through"

        store.set_state(STATE_KEY, dump_choice("all"))
        assert "notes.pdf" in listed(), "'everything' must still mean everything"

        store.set_state(STATE_KEY, dump_choice("source"))
        assert listed() == ["Order.cs"]
