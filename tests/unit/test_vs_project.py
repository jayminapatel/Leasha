r"""`Leasha.pyproj` and `Leasha.sln` describe the project they claim to.

Visual Studio shows only the files its project file lists, so a stale manifest
is worse than no manifest: a new module is missing from Solution Explorer while
every test that imports it passes, and somebody concludes the file does not
exist. Generating it solves that; this makes sure it was actually re-generated.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJ = ROOT / "Leasha.pyproj"
SOLUTION = ROOT / "Leasha.sln"
GENERATOR = ROOT / "scripts" / "regen_vs_project.py"

MSBUILD_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"


def tree() -> ElementTree.Element:
    return ElementTree.parse(PYPROJ).getroot()


def listed(tag: str) -> set[str]:
    return {
        element.get("Include", "")
        for element in tree().iter(f"{MSBUILD_NS}{tag}")
    }


def prop(name: str) -> str:
    element = tree().find(f".//{MSBUILD_NS}{name}")
    assert element is not None, f"<{name}> is missing from Leasha.pyproj"
    return (element.text or "").strip()


# --- the files exist and parse ----------------------------------------------

def test_the_project_and_solution_both_exist() -> None:
    assert PYPROJ.is_file(), "Leasha.pyproj is missing"
    assert SOLUTION.is_file(), "Leasha.sln is missing"


def test_the_project_is_valid_xml() -> None:
    """Visual Studio reports a malformed project as 'unavailable' with no
    explanation, which is a bad half hour for whoever hits it."""
    ElementTree.parse(PYPROJ)


# --- the solution and project agree -----------------------------------------

def test_the_solution_references_the_project_by_its_real_guid() -> None:
    """A mismatched GUID gives a solution that contains no project, and the
    error message does not say so."""
    guid = prop("ProjectGuid")
    text = SOLUTION.read_text(encoding="utf-8-sig")
    assert guid.upper() in text.upper(), (
        f"Leasha.sln does not reference {guid}. The project GUID changed, or "
        "the solution was edited by hand."
    )
    assert "Leasha.pyproj" in text


def test_the_solution_declares_a_python_project() -> None:
    """The Python Tools project-type GUID. Without it Visual Studio does not
    know what kind of project this is and will not load it."""
    text = SOLUTION.read_text(encoding="utf-8-sig")
    assert "888888A0-9F3D-457C-B088-3A5042F75D52" in text.upper()


# --- it points at real things -----------------------------------------------

def test_the_startup_file_exists() -> None:
    """F5 launching a file that is not there is a poor first impression."""
    startup = prop("StartupFile").replace("\\", "/")
    assert (ROOT / startup).is_file(), f"StartupFile {startup} does not exist"


def test_the_interpreter_is_the_projects_own_venv() -> None:
    """Relative, so the project works from any clone.

    An absolute path here is the same trap the venv itself has: it works on the
    machine that wrote it and nowhere else.
    """
    assert prop("InterpreterId") == "MSBuild|venv|$(MSBuildProjectFullPath)"
    interpreter = tree().find(f".//{MSBUILD_NS}Interpreter")
    assert interpreter is not None
    assert interpreter.get("Include") == "venv\\"


def test_tests_are_discoverable_from_test_explorer() -> None:
    assert prop("TestFramework").lower() == "pytest"
    assert (ROOT / prop("UnitTestRootDirectory")).is_dir()


@pytest.mark.parametrize("attribute", ["Compile", "Content"])
def test_every_listed_file_exists(attribute: str) -> None:
    """A listed file that is gone shows in Solution Explorer with a warning
    glyph and is the usual sign the generator has not been re-run."""
    missing = sorted(
        include for include in listed(attribute)
        if include and not (ROOT / include.replace("\\", "/")).is_file()
    )
    assert not missing, (
        f"{len(missing)} {attribute} entries name files that do not exist, "
        f"starting with {missing[:3]}. Run scripts/regen_vs_project.py."
    )


def test_no_source_file_is_missing_from_the_project() -> None:
    """The failure this whole arrangement exists to prevent.

    A module absent from the project is invisible in Solution Explorer while
    the tests that import it pass - so it looks deleted, and the next person
    writes it again.
    """
    result = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    tracked = {
        line.strip().replace("/", "\\")
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("logs/")
    }
    missing = sorted(tracked - listed("Compile"))
    assert not missing, (
        f"{len(missing)} tracked Python files are not in Leasha.pyproj, "
        f"starting with {missing[:3]}. Run scripts/regen_vs_project.py."
    )


def test_the_committed_project_matches_what_the_generator_produces() -> None:
    """Byte-for-byte, so drift is caught here rather than by whoever next opens
    the solution.

    Visual Studio rewrites the file when a file is added through the IDE. That
    is fine and expected - this test says to re-run the generator before
    committing, which restores the ordering.
    """
    before = PYPROJ.read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(GENERATOR)], cwd=ROOT,
                   capture_output=True, text=True, check=True)
    after = PYPROJ.read_text(encoding="utf-8")
    if before != after:
        PYPROJ.write_text(before, encoding="utf-8")     # leave the tree as found
        pytest.fail(
            "Leasha.pyproj is out of date. Run:\n"
            "    venv\\Scripts\\python.exe scripts\\regen_vs_project.py"
        )


# --- the VS Code side -------------------------------------------------------

def test_the_vscode_workspace_is_named_after_the_application() -> None:
    """It read 'Local Knowledge Graph' until now - the name the project had
    before the graph was removed and the scope narrowed to search."""
    import json
    import re

    path = ROOT / "Leasha.code-workspace"
    assert path.is_file(), "Leasha.code-workspace is missing"
    assert not (ROOT / "SearchProject.code-workspace").exists(), (
        "the old workspace file is still there; two workspaces means two "
        "different sets of settings depending on which one gets opened"
    )

    # Comments are legal in a .code-workspace and json.loads does not allow
    # them, so they come out before parsing.
    text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
    workspace = json.loads(text)
    assert workspace["folders"][0]["name"] == "Leasha"
