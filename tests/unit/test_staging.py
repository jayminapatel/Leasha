r"""The tree an installed copy has, and the guard that keeps it honest.

Layer: L0

Asked for: *"organize a structure under Leasha which are the master files as if
they are installed in final environment.. i.e. production and run like that so
that can be tested too"*.

`scripts/stage.py` builds that tree. The reason it is a script rather than a
folder somebody maintains is drift: **a second copy of the source that has
quietly gone out of date is worse than none, because it is trusted.** These
tests are the other half of that argument - they assert that the manifest still
covers everything the application actually needs, so a package added next month
cannot be left out of the install and discovered by a user.

The one that earns its place is `test_every_folder_the_application_imports_is_shipped`:
it reads the imports rather than the manifest, so it fails when the *code*
changes, which is the moment the manifest is wrong.
"""

from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import stage as staging  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    r"""One real staging of this checkout, shared by every test that only reads it.

    **Order 1u, 2026-10-13.** Each of those tests used to stage the whole tree
    again - eleven full copies of `app/` for checks that never write to it. A
    test that changes the destination, or stages again, still makes its own.
    """
    dest = tmp_path_factory.mktemp("staged") / "Leasha"
    staging.stage(ROOT, dest, Path(sys.executable))
    return dest


@pytest.fixture(scope="module")
def staged_with_tests(tmp_path_factory):
    """The same, staged `--with-tests`: four read-only checks, one copy of the suite."""
    dest = tmp_path_factory.mktemp("staged_with_tests") / "Leasha"
    staging.stage(ROOT, dest, Path(sys.executable), with_tests=True)
    return dest


@pytest.fixture()
def small_source(tmp_path):
    r"""A source tree with every shipped name in it and almost nothing inside.

    For the tests about *re-staging* - what the manifest makes it delete and
    keep - where the size of `app/` is not what is checked. The real tree's
    re-stage (and the read-only file that once broke it) is still proved, by
    `test_re_staging_does_not_delete_an_index_living_in_the_destination`.
    """
    source = tmp_path / "source"
    folders = {name for name in staging.SHIPPED if (ROOT / name).is_dir()}
    for name in staging.SHIPPED:
        if name in folders:
            (source / name).mkdir(parents=True)
            (source / name / "__init__.py").write_text("", encoding="utf-8")
        else:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(f"{name}\n", encoding="utf-8")
    return source


@functools.lru_cache(maxsize=1)
def _application_trees() -> tuple[tuple[Path, ast.Module], ...]:
    """Every module under `app/`, parsed once for the two checks that read them
    (about 8 s a parse on the owner's laptop, and it was done twice)."""
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted((ROOT / "app").rglob("*.py")))


# --- the manifest cannot drift ---------------------------------------------

def test_every_folder_the_application_imports_is_shipped():
    """**Reads the imports, not the manifest.**

    A test that checks the manifest against itself passes for ever. This one
    fails the day somebody adds `app/reports/` importing a new top-level
    package that ships nowhere - which is the moment the manifest became wrong,
    rather than the day a user finds out.
    """
    ours: set[str] = set()
    for path in ROOT.iterdir():
        # Guarded: a checkout collects folders nobody can stat - a `.pytest_tmp`
        # written by Windows and read from a container, for one. A repo-hygiene
        # check that falls over on somebody's stray directory is a check people
        # start skipping.
        try:
            if path.is_dir() and not path.name.startswith((".", "_")):
                ours.add(path.name)
        except OSError:
            continue
    ours -= set(staging.EXCLUDED) | {"scripts", "docs", "build", "dist"}

    needed: set[str] = set()
    for _path, tree in _application_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                needed |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                needed.add(node.module.split(".")[0])

    missing = sorted((needed & ours) - set(staging.SHIPPED))

    assert not missing, (
        f"{missing} are imported by the application and are not staged - an "
        f"installed copy would fail to start")


def test_the_test_suite_is_never_imported_at_module_scope():
    """**Not "never imported" - never imported where it cannot fail safely.**

    `app.cli evaluate --builtin` loads a corpus that genuinely ships with the
    tests, and that is a reasonable thing for a source checkout to offer. It
    does it inside the function, in a `try`, and answers with an `AppError`
    saying the corpus is not installed and what to use instead.

    A module-scope import is the one that cannot do that: it fails at start-up,
    before any handler exists, and takes the whole application with it. So the
    check is about *where*, and the first version of this test - which banned
    the import outright - would have made the application worse by forcing a
    working feature out of the source build.
    """
    offenders: list[str] = []
    for path, tree in _application_trees():
        for node in tree.body:                    # top level only
            text = ast.unparse(node) if isinstance(
                node, (ast.Import, ast.ImportFrom)) else ""
            if text.startswith(("import tests", "from tests")):
                offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        f"{offenders} import the test suite at module scope - an installed "
        f"copy would fail to start")


def test_a_guarded_import_of_the_corpus_says_what_to_do_instead():
    """The other half: it must fail *usefully*. An `ImportError` traceback for
    a corpus that was never installed is a bug report about a design decision.
    """
    import inspect

    from app.cli import evaluate

    source = inspect.getsource(evaluate)
    guarded = source.split("from tests.fixtures.evaluation")[1][:600]

    assert "except ImportError" in guarded
    assert "--questions" in guarded, "it does not say what to use instead"


def test_files_the_application_opens_by_path_are_shipped():
    """`doctor.py` is shelled out to by path, `config/` and `assets/` are read
    at runtime. None of them is an import, so the check above cannot see them -
    and each one absent is a feature that fails only once installed."""
    for name in ("doctor.py", "config", "assets", "VERSION"):
        assert name in staging.SHIPPED, f"{name} is read at runtime and is not staged"


# --- what the tree contains -------------------------------------------------

def test_the_application_and_its_configuration_are_there(staged):
    assert (staged / "app" / "cli" / "__init__.py").is_file()
    assert (staged / "config" / "extractors.toml").is_file()
    assert (staged / "VERSION").is_file()
    assert (staged / "doctor.py").is_file()


def test_the_test_suite_and_its_fixtures_are_not(staged):
    """Not a product. Also several hundred megabytes of generated PDFs."""
    assert not (staged / "tests").exists()
    assert not (staged / "tests" / "fixtures").exists()


def test_no_compiled_bytecode_is_copied(staged):
    """`__pycache__` is compiled against a different absolute path. Copying it
    is at best wasted space and at worst a stale module that shadows the source
    beside it."""
    assert not list(staged.rglob("__pycache__"))


def test_bytecode_in_the_source_is_not_copied(tmp_path, small_source):
    """The check above, made to bite. **Found 2026-10-13 (order 1u):** with
    `PYTHONDONTWRITEBYTECODE=1` - set on the owner's laptop - the checkout has no
    `__pycache__` at all, so the test above passed even with `__pycache__`
    taken out of `EXCLUDED`. Here the source has one by construction."""
    cache = small_source / "app" / "__pycache__"
    cache.mkdir()
    (cache / "stale.cpython-312.pyc").write_bytes(b"\x00")
    dest = tmp_path / "Leasha"

    staging.stage(small_source, dest, Path(sys.executable))

    assert (dest / "app" / "__init__.py").is_file()
    assert not list(dest.rglob("__pycache__"))


def test_the_owner_s_env_is_never_copied(staged, tmp_path):
    """**Non-negotiable 11: `.env` is written by the application, never by the
    user - and never by a copy.** It holds this machine's paths; staging it
    would give the installed tree somebody else's index location, which is the
    one setting that must be wrong loudly rather than quietly."""
    assert not (staged / ".env").exists()


def test_the_log_folder_is_empty_rather_than_the_development_one(staged):
    """An installed copy has no history. Copying `logs/` would ship the
    developer's run logs to a user - and one of those now contains the resolved
    settings of the machine it was built on."""
    assert (staged / "logs").is_dir()
    assert not list((staged / "logs").rglob("*.log"))


def test_it_says_it_is_generated(staged):
    """An edit made in a generated folder vanishes without warning, and makes
    the tree disagree with the source it came from. Worth saying where somebody
    will read it."""
    readme = (staged / "README.txt").read_text(encoding="utf-8")

    assert "generated" in readme.lower()
    assert "stage.py" in readme


# --- how it is run ----------------------------------------------------------

def test_the_launchers_start_in_their_own_folder(staged):
    """**`cd /d "%~dp0"`, and it is the whole point.**

    Everything the application finds - `.env`, `logs\\`, `config\\` - is
    resolved from its own location. A launcher that inherits whatever directory
    a shortcut happened to start in is how a staged tree quietly reads the
    development tree's configuration, which is exactly the confusion this
    exercise exists to remove.
    """
    for name in (staging.STAGED_WINDOW, staging.STAGED_CLI, "leasha.cmd"):
        text = (staged / name).read_text(encoding="utf-8")
        assert 'cd /d "%~dp0"' in text, f"{name} runs from wherever it was called"


def test_the_window_and_the_command_line_have_separate_launchers(staged):
    assert "app.main" in (staged / staging.STAGED_WINDOW).read_text(encoding="utf-8")
    assert "app.cli" in (staged / staging.STAGED_CLI).read_text(encoding="utf-8")


# --- staging again ----------------------------------------------------------

def test_staging_twice_removes_what_the_first_run_wrote(tmp_path, monkeypatch, small_source):
    """A stale file from a previous layout is the thing that makes a staged
    tree lie about what ships - so anything the last staging wrote and this one
    does not is removed."""
    dest = tmp_path / "Leasha"
    monkeypatch.setattr(staging, "SHIPPED", ("app", "VERSION", "leasha.cmd"))
    staging.stage(small_source, dest, Path(sys.executable))
    assert (dest / "leasha.cmd").exists()

    # The layout changes: leasha.cmd is no longer shipped.
    monkeypatch.setattr(staging, "SHIPPED", ("app", "VERSION"))
    staging.stage(small_source, dest, Path(sys.executable))

    assert not (dest / "leasha.cmd").exists(), "a file dropped from SHIPPED survived"
    assert (dest / "VERSION").exists()


def test_re_staging_does_not_delete_an_index_living_in_the_destination(tmp_path):
    """**The one that would have cost a hundred gigabytes.**

    `--dest D:\\Leasha` makes that folder a real installation, and the
    documented thing to do next is move the index to `D:\\Leasha\\Data`. Staging
    used to `rmtree` the whole destination, so re-staging after a typo fix would
    have deleted the index, the `.env` and the venv - silently, with no
    confirmation and no way back.
    """
    dest = tmp_path / "Leasha"
    staging.stage(ROOT, dest, Path(sys.executable))

    # An installation grows these. None of them came from staging.
    index = dest / "Data"
    for name in ("vectors", "fts", "cache", "models", "state"):
        (index / name).mkdir(parents=True)
    (index / "fts" / "knowledge.db").write_bytes(b"SQLite format 3\x00")
    (dest / ".env").write_text("DATA_PATH=D:\\Leasha\\Data\n", encoding="utf-8")
    (dest / "venv").mkdir()
    (dest / "venv" / "marker").write_text("two gigabytes of wheels", encoding="utf-8")
    (dest / "logs").mkdir(exist_ok=True)
    (dest / "logs" / "run.log").write_text("history", encoding="utf-8")

    staging.stage(ROOT, dest, Path(sys.executable))

    assert (index / "fts" / "knowledge.db").is_file(), "the index was deleted"
    assert (dest / ".env").is_file(), "the configuration was deleted"
    assert (dest / "venv" / "marker").is_file(), "the venv was deleted"
    assert (dest / "logs" / "run.log").is_file(), "the logs were deleted"


def test_a_destination_nobody_staged_is_not_emptied(tmp_path, small_source):
    """Somebody typed the wrong path. Overwrite what collides; touch nothing
    else. Deleting a stranger's folder is never the right response to a typo."""
    dest = tmp_path / "SomeoneElsesFolder"
    dest.mkdir()
    (dest / "important.txt").write_text("not ours", encoding="utf-8")

    staging.stage(small_source, dest, Path(sys.executable))

    assert (dest / "important.txt").is_file()
    assert (dest / "app").is_dir()


def test_no_generated_launcher_collides_with_a_shipped_name(tmp_path):
    r"""**Windows filenames are case-insensitive, and `Leasha.cmd` was a bug.**

    The generated `Leasha.cmd` and the shipped `leasha.cmd` are one file on
    Windows, and the generated one was written last. So the real launcher - the
    one that checks for a venv, says "Leasha is not installed yet", and passes
    arguments to the CLI - was replaced by a stub hardcoding the interpreter
    that happened to run the staging script. On a production tree with its own
    venv, that ran the *development* interpreter.
    """
    generated = {staging.STAGED_WINDOW.lower(), staging.STAGED_CLI.lower()}
    shipped = {name.lower() for name in staging.SHIPPED}
    assert not (generated & shipped), (
        f"{generated & shipped} is both shipped and generated; on Windows "
        "these are the same file and the generated one wins"
    )


def test_the_shipped_launcher_survives_staging(staged):
    """The consequence of the above, asserted against a real staged tree."""
    body = (staged / "leasha.cmd").read_text(encoding="utf-8")
    assert "not installed yet" in body, "the real launcher was overwritten"


def test_a_generated_launcher_prefers_the_trees_own_venv(staged):
    """One file works before and after `run-install.cmd`.

    Without this, a launcher staged against the development venv keeps using it
    forever - so the production tree reads the development tree's packages while
    claiming to be a clean install.
    """
    body = (staged / staging.STAGED_WINDOW).read_text(encoding="utf-8")
    assert "venv\\Scripts\\python.exe" in body
    assert body.index("venv\\Scripts\\python.exe") < body.index(str(sys.executable)), (
        "the staged interpreter is tried before the tree's own venv"
    )


def test_the_manifest_records_what_was_written(staged):
    """It is what makes the next staging safe, so it has to be there."""
    import json

    manifest = json.loads((staged / staging.MANIFEST).read_text(encoding="utf-8"))
    assert "app" in manifest["wrote"]
    assert staging.STAGED_WINDOW in manifest["wrote"]
    assert "logs" not in manifest["wrote"], (
        "logs is in the manifest, so a re-stage would delete a running "
        "installation's log history"
    )


def test_a_corrupt_manifest_is_treated_as_no_manifest(tmp_path, small_source):
    """Failing towards 'delete nothing' is the only safe direction here."""
    dest = tmp_path / "Leasha"
    staging.stage(small_source, dest, Path(sys.executable))
    (dest / staging.MANIFEST).write_text("{not json", encoding="utf-8")
    (dest / "user-data.txt").write_text("keep me", encoding="utf-8")

    staging.stage(small_source, dest, Path(sys.executable))

    assert (dest / "user-data.txt").is_file()


def test_it_refuses_to_stage_over_the_source():
    """Pointed at the checkout, staging into itself is a recursive copy at best."""
    with pytest.raises(SystemExit):
        staging.stage(ROOT, ROOT, Path(sys.executable))


# --- --with-tests -----------------------------------------------------------

def test_tests_are_absent_by_default(staged):
    """A product does not ship its test suite."""
    assert not (staged / "tests").exists()
    assert not (staged / "pyproject.toml").exists()


def test_with_tests_stages_the_suite_and_its_configuration(staged_with_tests):
    """`pyproject.toml` is not optional: it carries the pytest settings, and
    without it the suite runs differently in the staged tree than in
    development - which makes any difference in the results meaningless."""
    dest = staged_with_tests

    assert (dest / "tests" / "unit").is_dir()
    assert (dest / "tests" / "conftest.py").is_file()
    assert (dest / "pyproject.toml").is_file()


def test_with_tests_keeps_the_fixtures(staged_with_tests):
    """`fixtures` is in EXCLUDED so it is skipped inside `app/`. Inside `tests/`
    it is the content - a suite without its fixtures fails on the first file."""
    dest = staged_with_tests
    assert (dest / "tests" / "fixtures").is_dir()


def test_with_tests_still_excludes_bytecode(staged_with_tests):
    dest = staged_with_tests
    assert not list((dest / "tests").rglob("__pycache__"))


def test_the_readme_says_when_a_tree_is_not_what_ships(staged_with_tests):
    """A tree with tests in it is not a production tree, and somebody looking at
    it later should not have to guess which kind they are holding."""
    dest = staged_with_tests
    assert "WITH TESTS" in (dest / "README.txt").read_text(encoding="utf-8")


def test_a_name_in_the_manifest_that_does_not_exist_is_reported(
        tmp_path, monkeypatch, small_source):
    """Named but absent means the manifest and the repository have drifted -
    which is the failure staging exists to make visible, not to hide."""
    monkeypatch.setattr(staging, "SHIPPED", ("app", "no-such-file"))
    written = staging.stage(small_source, tmp_path / "Leasha", Path(sys.executable))

    assert any("MISSING" in line and "no-such-file" in line for line in written)
