r"""The code file types this application reads, and the ones it must not.

Layer: L2

Asked for: *"add all types of code files from microsoft, oracle etc, and others
which are stored and configure them and select by default."*

**A list this long is only worth having if it is also correct**, and the two
ways it goes wrong are opposite. Too narrow and a repository is silently
part-indexed - the state this replaced, where a PL/SQL shop had none of its
packages read because only `.sql` was in the set. Too wide and the index fills
with the bytes of binaries that happen to carry a text-ish extension, which
makes every search worse and is much harder to notice.

So the tests here are mostly about the second failure and about the entries that
*cannot work*: an extension with a dot in the middle, a name that Python's
`suffix` will never produce, a type claimed by two extractors at once.
"""

from __future__ import annotations

import pytest

from app.extract.source_types import (
    ALL_SOURCE_EXTENSIONS,
    BY_ECOSYSTEM,
    NAMED_FILES,
)


# --- the ecosystems the owner named ----------------------------------------

@pytest.mark.parametrize("extension,why", [
    (".cs", "C#"),
    (".vb", "VB.NET"),
    (".csproj", "the project file, where the package versions live"),
    (".props", "shared MSBuild properties"),
    (".sln", "the solution"),
    (".xaml", "WPF and MAUI markup"),
    (".resx", "string resources"),
    (".config", "web.config and app.config"),
    (".ps1", "PowerShell"),
    (".psm1", "a PowerShell module"),
    (".dtsx", "an SSIS package - the ETL logic is inside the XML"),
    (".rdl", "an SSRS report, including its SQL"),
    (".bicep", "Azure infrastructure"),
    (".bas", "a VBA module export"),
])
def test_microsoft(extension, why):
    assert extension in ALL_SOURCE_EXTENSIONS, f"{extension} - {why}"


@pytest.mark.parametrize("extension,why", [
    (".pks", "a package specification"),
    (".pkb", "a package body - where the logic actually is"),
    (".prc", "a standalone procedure"),
    (".fnc", "a function"),
    (".trg", "a trigger"),
    (".tps", "an object type specification"),
    (".ctl", "a SQL*Loader control file"),
    (".ldt", "an FNDLOAD data file - where an EBS customisation is defined"),
    (".ora", "tnsnames.ora and listener.ora"),
])
def test_oracle(extension, why):
    assert extension in ALL_SOURCE_EXTENSIONS, f"{extension} - {why}"


@pytest.mark.parametrize("extension,why", [
    (".cbl", "COBOL"),
    (".cpy", "a copybook - the record layouts nobody remembers to index"),
    (".rpgle", "RPG IV"),
    (".sqlrpgle", "RPG with embedded SQL"),
    (".jcl", "job control"),
    (".pli", "PL/I"),
])
def test_ibm(extension, why):
    assert extension in ALL_SOURCE_EXTENSIONS, f"{extension} - {why}"


@pytest.mark.parametrize("extension,why", [
    (".st", "IEC Structured Text"),
    (".scl", "Siemens SCL"),
    (".awl", "Siemens STL"),
    (".l5x", "a Rockwell Logix export - rung comments and tag descriptions"),
])
def test_industrial_control(extension, why):
    """**Included because of what this application is for.** A plant's logic
    lives in these, they are plain text, and no general-purpose indexer would
    think to include them."""
    assert extension in ALL_SOURCE_EXTENSIONS, f"{extension} - {why}"


def test_the_original_thirty_four_are_all_still_there():
    """The list was replaced wholesale. Losing one of the common types while
    adding three hundred rare ones would be a poor trade and an easy mistake."""
    before = {
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
        ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml",
        ".py", ".js", ".ts", ".sql", ".ps1", ".bat", ".cmd", ".sh",
        ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
        ".html", ".htm", ".css",
    }

    assert before <= ALL_SOURCE_EXTENSIONS


# --- entries that could not work -------------------------------------------

def test_every_extension_is_something_suffix_can_produce():
    """**`Path.suffix` returns one dot and one segment.** An entry like
    `.dacpac.xml` or `.tar.gz` can never match anything, and looks entirely
    correct sitting in the list - which is the sort of line that survives for
    years."""
    wrong = sorted(e for e in ALL_SOURCE_EXTENSIONS
                   if not e.startswith(".") or "." in e[1:] or not e[1:])

    assert wrong == []


def test_no_extension_has_whitespace_or_a_capital():
    """Matching is `path.suffix.lower()`, so an upper-case entry is dead."""
    wrong = sorted(e for e in ALL_SOURCE_EXTENSIONS
                   if e != e.lower().strip())

    assert wrong == []


def test_named_files_are_names_not_extensions():
    """`NAMED_FILES` is matched against the whole filename. An entry like
    `.gitignore` belongs here precisely *because* `Path(".gitignore").suffix`
    is empty - but `makefile` with a dot in front would match nothing."""
    for name in NAMED_FILES:
        assert name == name.lower().strip()
        assert name, "an empty name matches every extensionless file"


def test_the_dotfiles_really_do_have_no_suffix():
    """The premise of `NAMED_FILES`, asserted rather than assumed. If Python
    ever started reporting `.gitignore` as a suffix, these entries would be
    duplicated in two mechanisms and one of them would be wrong."""
    from pathlib import Path

    for name in (".gitignore", ".editorconfig", "Makefile", "Dockerfile"):
        assert Path(name).suffix == "", f"{name} has a suffix after all"


# --- what must never be in the list ----------------------------------------

@pytest.mark.parametrize("extension", [
    ".exe", ".dll", ".pdb", ".so", ".dylib", ".obj", ".lib",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".dat", ".idx", ".db", ".sqlite", ".bin",
    ".fmb", ".rdf", ".acd", ".mlx", ".msapp", ".accdb", ".mdb",
    ".png", ".jpg", ".pdf", ".docx", ".xlsx", ".pst",
])
def test_binary_and_already_claimed_types_are_absent(extension):
    """**Two different reasons, one list.**

    A binary routed to the plain-text reader is caught by the NUL sniff and
    reported as `ERR_NO_TEXT_LAYER` - so it fails loudly rather than polluting
    the index. But a type that is *usually* binary should not be routed here at
    all, or that honest report becomes the common case and nobody reads it.

    And `.pdf`, `.docx`, `.pst` have real parsers. Claiming one here would be
    caught by `register`, which refuses a second claim - but only at import
    time, and only if somebody runs it.
    """
    assert extension not in ALL_SOURCE_EXTENSIONS


@pytest.mark.parametrize("extension", [".map", ".lock", ".sum", ".min"])
def test_generated_output_is_absent(extension):
    """Real text, enormous, and containing nothing a person searches for.
    Indexing it makes the results worse rather than more complete."""
    assert extension not in ALL_SOURCE_EXTENSIONS


def test_nothing_here_is_claimed_by_a_real_parser():
    """The check `register` makes, made before import order can hide it.

    A `.docx` in this set would be handed to the plain-text reader, which would
    find a zip, sniff NULs and report a corrupt file - for every Word document
    in the corpus.
    """
    import app.extract  # noqa: F401 - populates the registry
    from app.extract.base import REGISTRY

    clashes = {
        extension: type(REGISTRY[extension]).__name__
        for extension in sorted(ALL_SOURCE_EXTENSIONS)
        if extension in REGISTRY
        and type(REGISTRY[extension]).__name__ != "PlainTextExtractor"
    }

    assert clashes == {}


# --- the groups are the reviewable unit ------------------------------------

def test_the_union_is_the_groups_and_nothing_else():
    """No extension added to the flat set without a group to explain it -
    which is what makes the list reviewable by somebody who knows one
    ecosystem and not the others."""
    union: set = set()
    for group in BY_ECOSYSTEM.values():
        union |= group

    assert union == set(ALL_SOURCE_EXTENSIONS)


def test_every_group_has_a_name_worth_reading():
    for name, group in BY_ECOSYSTEM.items():
        assert name.strip() and group, name


def test_the_list_is_substantially_larger_than_it_was():
    """A floor, not a target. It exists so that a refactor which silently
    reverted this to the original thirty-four fails rather than passes."""
    assert len(ALL_SOURCE_EXTENSIONS) > 250


# --- reaching the walk ------------------------------------------------------

def test_the_walk_admits_a_file_with_no_extension_at_all(tmp_path):
    """**The half that routing by extension cannot do.** Without it a
    repository is indexed without the file that says how it is built.

    Asked of `readable`, not of the yielded set: since §1a of the archives
    order every file in the folder is yielded, so "is it there?" no longer
    distinguishes anything. The question this test has always been asking is
    whether the walk will *open* it.
    """
    from app.index.walker import WalkConfig, walk

    for name in ("Makefile", ".gitignore", "a.pkb", "b.exe", "c.tmp"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    opened = {c.path.name for c in walk(WalkConfig(roots=(tmp_path,))) if c.readable}

    assert {"Makefile", ".gitignore", "a.pkb"} <= opened
    assert "b.exe" not in opened and "c.tmp" not in opened


def test_an_extensionless_file_nobody_named_is_still_skipped(tmp_path):
    """The trap in folding names into the extension set: the walk compares
    `path.suffix`, which is `""` for *every* extensionless file, so a set
    containing `""` admits Unix binaries, lock files and scratch files alike."""
    from app.index.walker import WalkConfig, walk

    (tmp_path / "some-random-binary").write_bytes(b"\x7fELF")

    opened = {c.path.name for c in walk(WalkConfig(roots=(tmp_path,))) if c.readable}

    assert "some-random-binary" not in opened
