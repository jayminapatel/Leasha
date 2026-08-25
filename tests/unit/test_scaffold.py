"""Generating a new extractor: what it writes, and what it refuses to.

Layer: L0

The generator writes into the application's own source tree, so the tests that
matter most are the ones proving what it will **not** do: never overwrite,
never claim an extension another reader already has, never leave the import out.

That last one is the step people forget by hand, and its failure mode is the
worst kind - the module is perfect, the extension reports unsupported, and
nothing connects the two.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.core.scaffold import ExtractorSpec, apply, plan, project_root


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A throwaway copy of the parts of the tree the generator touches."""
    root = tmp_path / "project"
    (root / "app" / "extract").mkdir(parents=True)

    real = project_root()
    shutil.copy(real / "app" / "extract" / "__init__.py",
                root / "app" / "extract" / "__init__.py")
    shutil.copy(real / "requirements.txt", root / "requirements.txt")
    return root


def spec(**overrides) -> ExtractorSpec:
    base = {
        "name": "cadx",
        "extensions": (".zzz",),
        "module": "thelib",
        "package": "the-lib",
        "provides": "text inside these files",
        "version": "1.2.3",
    }
    base.update(overrides)
    return ExtractorSpec(**base)


# --- what it writes --------------------------------------------------------

def test_it_plans_three_files_and_writes_none_yet(tree: Path):
    """A plan is a description. Nothing changes until it is applied, so it can
    be shown on screen and declined."""
    scaffold = plan(spec(), tree)

    assert {c.action for c in scaffold.changes} == {"create", "amend"}
    assert len(scaffold.changes) == 3
    assert not (tree / "app" / "extract" / "cadx.py").exists()


def test_the_generated_module_carries_the_whole_contract(tree: Path):
    apply(plan(spec(), tree))
    text = (tree / "app" / "extract" / "cadx.py").read_text(encoding="utf-8")

    assert "class CadxExtractor" in text
    assert 'name = "cadx"' in text
    assert '".zzz"' in text
    assert "register(CadxExtractor())" in text, "an unregistered reader is invisible"

    # The library import must be inside extract(), never at module level: at
    # module level a missing package stops the whole app starting.
    assert "import thelib" in text
    body = text.split("def extract(")[1]
    assert "import thelib" in body
    assert not any(
        line.strip() == "import thelib"
        for line in text.split("def extract(")[0].splitlines()
    ), "the library was imported at module level"

    assert "Requirement(" in text and '"the-lib"' in text
    assert "_name_only" in text, "a file that cannot be read must still be findable"
    assert "TODO" in text, "the one part a generator cannot write"


def test_the_import_is_added_so_registration_actually_happens(tree: Path):
    apply(plan(spec(), tree))
    text = (tree / "app" / "extract" / "__init__.py").read_text(encoding="utf-8")

    assert "from app.extract import cadx as cadx" in text


def test_the_import_is_inserted_in_order_not_appended(tree: Path):
    """The file is written alphabetically. A generator that appends turns a
    tidy file into a list with one odd entry at the end, every time."""
    apply(plan(spec(name="aaa_first", extensions=(".zzz",)), tree))
    lines = (tree / "app" / "extract" / "__init__.py").read_text(
        encoding="utf-8").splitlines()

    imports = [i for i, line in enumerate(lines)
               if line.startswith("from app.extract import ")]
    names = [lines[i].split()[3] for i in imports]

    assert "aaa_first" in names
    assert names == sorted(names), f"imports left unsorted: {names}"


def test_the_package_is_pinned(tree: Path):
    apply(plan(spec(), tree))
    text = (tree / "requirements.txt").read_text(encoding="utf-8")

    assert "the-lib==1.2.3" in text
    assert ".zzz" in text, "the pin should say what it is for"


def test_an_unpinned_package_is_still_listed(tree: Path):
    apply(plan(spec(version=""), tree))
    text = (tree / "requirements.txt").read_text(encoding="utf-8")

    assert "the-lib" in text
    assert "the-lib==" not in text


def test_a_reader_with_no_library_skips_the_requirement_and_the_pin(tree: Path):
    scaffold = plan(spec(module="", package=""), tree)
    apply(scaffold)

    text = (tree / "app" / "extract" / "cadx.py").read_text(encoding="utf-8")
    assert "Requirement(" not in text
    assert "standard library" in text
    assert len(scaffold.changes) == 2, "no pin is needed"


def test_the_notes_say_what_is_left_to_do(tree: Path):
    notes = " ".join(plan(spec(), tree).notes)

    assert "pip install the-lib" in notes
    assert "TODO" in notes
    assert "Restart" in notes, "extractors register on import; a running app misses it"


# --- what it refuses -------------------------------------------------------

def test_it_never_overwrites_an_existing_reader(tree: Path):
    apply(plan(spec(), tree))

    with pytest.raises(AppErrorException) as caught:
        plan(spec(extensions=(".yyy",)), tree)
    assert "already exists" in caught.value.error.render()


def test_it_refuses_an_extension_another_reader_claims(tree: Path):
    """Two readers claiming one extension raises at startup. Better here, where
    the person can still choose something else."""
    with pytest.raises(AppErrorException) as caught:
        plan(spec(extensions=(".pdf",)), tree)

    rendered = caught.value.error.render()
    assert ".pdf" in rendered and "pdf" in rendered


@pytest.mark.parametrize("bad", ["", "CAD", "9lives", "my reader", "class", "a"])
def test_it_refuses_an_unusable_reader_name(tree: Path, bad):
    with pytest.raises(AppErrorException):
        plan(spec(name=bad), tree)


@pytest.mark.parametrize("bad", ["zzz", ".", ".ZZZ", ".z z", "..zzz", ""])
def test_it_refuses_something_that_is_not_an_extension(tree: Path, bad):
    with pytest.raises(AppErrorException):
        plan(spec(extensions=(bad,)), tree)


def test_it_refuses_a_package_with_no_import_name(tree: Path):
    """python-docx imports as `docx`, pymupdf as `fitz`. Guessing would
    generate a module that fails on the one line nobody looks at."""
    with pytest.raises(AppErrorException) as caught:
        plan(spec(module="", package="the-lib"), tree)
    assert "import" in caught.value.error.render()


def test_applying_twice_does_not_clobber_the_first(tree: Path):
    scaffold = plan(spec(), tree)
    apply(scaffold)
    (tree / "app" / "extract" / "cadx.py").write_text("edited by hand", encoding="utf-8")

    with pytest.raises(AppErrorException):
        apply(scaffold)

    assert (tree / "app" / "extract" / "cadx.py").read_text(
        encoding="utf-8") == "edited by hand"


def test_a_second_plan_does_not_duplicate_the_import(tree: Path):
    apply(plan(spec(), tree))
    init = tree / "app" / "extract" / "__init__.py"
    before = init.read_text(encoding="utf-8").count("import cadx")

    # A different reader, same tree: the first import must not be repeated.
    apply(plan(spec(name="other", extensions=(".yyy",), package="", module=""), tree))

    assert init.read_text(encoding="utf-8").count("import cadx") == before


# --- the generated module has to be real Python ----------------------------

def test_the_generated_module_compiles(tree: Path):
    """A template with a formatting mistake produces a file that looks right
    and cannot be imported. Compiling it here is the cheapest possible check."""
    apply(plan(spec(), tree))
    source = (tree / "app" / "extract" / "cadx.py").read_text(encoding="utf-8")

    compile(source, "cadx.py", "exec")


def test_the_generated_module_passes_the_projects_own_lint(tree: Path):
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not installed")

    import subprocess

    apply(plan(spec(), tree))
    result = subprocess.run(
        [ruff, "check", "--isolated", "--select", "E,F",
         str(tree / "app" / "extract" / "cadx.py")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout
