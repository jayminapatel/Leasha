r"""The privacy-defaults order: per-account index, empty roots, one paragraph.

The decisions this tests were the owner's, taken 2026-08-27, and two of them
are promises rather than features:

* **nothing is ever added to the index unasked** - the roots list starts empty
  and a first run *offers* folders it never adds;
* **no default ever reaches another user's profile** - typing one in is
  allowed, because the machine belongs to whoever is at it, but a suggestion
  that wanders into `C:\Users\someone-else` is the failure this order exists
  to make impossible.

The paragraph is tested too. A promise about privacy that says two different
things in two places is worse than one that says nothing, so the installer's
copy and the README's are held to each other word for word.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.ui.presenter import (
    SUGGESTED_FOLDERS,
    nothing_indexed_yet,
    owns_path,
    suggested_roots,
)

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (ROOT / "install.ps1").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


# --- 1a: where the index goes -----------------------------------------------


def test_the_installer_defaults_the_index_to_a_per_account_folder() -> None:
    """`%LOCALAPPDATA%` is ACL'd by Windows to one account, which is the whole
    mechanism: two people on one machine get two private indexes and nothing
    had to be built to separate them."""
    assert "Join-Path $env:LOCALAPPDATA" in INSTALLER
    assert "D:\\KnowledgeGraphData" not in _default_line(), (
        "the old shared default is still what Enter chooses")


def _default_line() -> str:
    """The line that decides what pressing Enter does."""
    match = re.search(r'^\s*\$default\s*=\s*(.+)$', INSTALLER, re.MULTILINE)
    assert match, "install.ps1 no longer has a $default for the index location"
    return match.group(0)


def test_an_existing_env_is_left_alone_without_asking() -> None:
    r"""The grandfather rule, and the owner's own machine is the fixture.

    `D:\Leasha\Data` and his roots stay exactly as they are. Somebody
    re-running the installer to repair a venv must not be asked where their
    index lives, and must certainly not be defaulted onto a new location -
    which would quietly start a second index and leave the first one orphaned.
    """
    assert "$ExistingDataPath" in INSTALLER
    assert re.search(r"DATA_PATH\\s\*=\\s\*\(\.\+\?\)", INSTALLER) or \
        "DATA_PATH" in INSTALLER

    # The prompt must sit behind the "did we already have one" check.
    existing_at = INSTALLER.index("$ExistingDataPath = \"\"")
    prompt_at = INSTALLER.index("Index location [Enter for")
    assert existing_at < prompt_at, (
        "the installer asks before it looks at the existing .env")


def test_the_installer_reads_data_path_before_it_offers_a_default() -> None:
    """Reading the file is not enough if the answer is thrown away."""
    block = INSTALLER[INSTALLER.index("$ExistingDataPath = \"\""):
                      INSTALLER.index("Index location [Enter for")]
    assert "if (-not $DataPath)" in block
    assert "$DataPath = $candidate" in block


# --- 1b/1c: roots start empty and suggestions stay inside this profile -------


def test_the_suggestions_are_the_four_ordinary_folders() -> None:
    assert SUGGESTED_FOLDERS == ("Documents", "Desktop", "Downloads", "Pictures")


def test_only_folders_that_exist_are_offered() -> None:
    """Offering a folder that is not there makes the list look broken."""
    home = r"C:\Users\jaymin"
    present = {rf"{home}\Documents".lower(), rf"{home}\Pictures".lower()}
    got = suggested_roots(home=home, exists=lambda p: p.lower() in present)
    assert got == [rf"{home}\Documents", rf"{home}\Pictures"]


def test_nothing_outside_this_account_is_ever_suggested() -> None:
    r"""The one guarantee in this order that is not a convenience."""
    home = r"C:\Users\jaymin"
    for candidate in suggested_roots(home=home, exists=lambda _p: True):
        assert owns_path(candidate, home=home), candidate


@pytest.mark.parametrize(
    "candidate,owned",
    [
        (r"C:\Users\jaymin\Documents", True),
        (r"C:/Users/JAYMIN/Downloads", True),          # case and separators
        (r"C:\Users\jaymin", True),
        (r"C:\Users\someone-else\Documents", False),
        (r"C:\Users\jaymin-two\Documents", False),     # prefix, not a parent
        (r"D:\Shared", False),
        (r"\\server\share", False),
        ("", False),
    ],
)
def test_owns_path_decides_correctly(candidate: str, owned: bool) -> None:
    """`jaymin-two` is the case a `startswith` gets wrong, and it is the one
    that would hand somebody else's folder to the wrong person."""
    assert owns_path(candidate, home=r"C:\Users\jaymin") is owned


def test_an_empty_roots_list_says_so_in_plain_words() -> None:
    """A blank list and a broken application look identical."""
    said = nothing_indexed_yet([])
    assert said
    assert "nothing to search" in said
    assert "Choose a folder" in said


def test_a_configured_install_is_told_nothing() -> None:
    assert nothing_indexed_yet([r"D:\Docs"]) == ""


# --- 2a: one paragraph, in both places, word for word -----------------------

#: The sentences that must appear in both, normalised for wrapping and for the
#: hyphen the console prints instead of an em dash.
PARAGRAPH_SENTENCES = (
    "Everything Leasha indexes and everything you search stays on this "
    "computer",
    "each account gets its own private index",
    "you find your files, others find theirs, and Windows keeps them apart",
    "anyone using it can find anything it can read",
    "give each person their own Windows account before installing",
    "so shared spaces stay shared and private ones stay out",
)


def _flat(text: str) -> str:
    """Wrapping, quoting and console colour arguments removed."""
    text = re.sub(r'-ForegroundColor \w+', " ", text)
    text = text.replace('Write-Host "', " ").replace('"', " ")
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


@pytest.mark.parametrize("sentence", PARAGRAPH_SENTENCES)
def test_the_paragraph_is_in_the_readme(sentence: str) -> None:
    assert sentence in _flat(README), sentence


@pytest.mark.parametrize("sentence", PARAGRAPH_SENTENCES)
def test_the_paragraph_is_in_the_installer(sentence: str) -> None:
    """Shown on the index-location step, where the choice is actually made."""
    assert sentence in _flat(INSTALLER), sentence


def test_the_installer_shows_it_where_the_choice_is_made() -> None:
    """In the file is not the same as on the screen."""
    block = INSTALLER[INSTALLER.index("if (-not $DataPath) {"):
                      INSTALLER.index("Index location [Enter for")]
    assert "Write-SharedComputerNotice" in block


# --- 2b: doctor says where the index is and who can read it -----------------


def _doctor():
    import importlib
    import sys

    sys.path.insert(0, str(ROOT))
    return importlib.import_module("doctor")


@pytest.mark.parametrize(
    "path,expected",
    [
        (r"C:\Users\jaymin\AppData\Local\Leasha", "private to this account"),
        (r"C:/Users/jaymin/AppData/Local/Leasha", "private to this account"),
        (r"D:\Leasha\Data", "shared location"),
        (r"\\server\share\Leasha", "shared location"),
        (r"C:\Users\someone-else\AppData\Local\Leasha", "shared location"),
    ],
)
def test_doctor_says_whether_the_index_is_private(
    path: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\jaymin\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\jaymin\AppData\Roaming")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\jaymin")
    assert doctor.index_privacy(path) == expected


def test_doctor_reports_rather_than_judges() -> None:
    r"""A shared location is a legitimate choice - the owner's own install is
    `D:\Leasha\Data` and decision 4 says it stays that way. `doctor` failing
    over it would be `doctor` having an opinion about somebody's machine."""
    doctor = _doctor()
    check = doctor.check_index_location()
    assert check.ok is True
    assert check.fix == ""


def test_the_location_line_is_part_of_the_report() -> None:
    """A check nothing calls reports nothing."""
    source = (ROOT / "doctor.py").read_text(encoding="utf-8")
    assert "checks.append(check_index_location())" in source


# --- the installer still parses ---------------------------------------------


def test_the_installer_has_balanced_braces() -> None:
    """`run-install.cmd` parse-checks before running, and this is the cheapest
    approximation of that available without PowerShell."""
    text = re.sub(r"#.*", "", INSTALLER)
    assert text.count("{") == text.count("}"), "unbalanced braces in install.ps1"
    assert text.count("(") == text.count(")"), "unbalanced parens in install.ps1"


def test_the_here_string_that_writes_env_is_still_closed() -> None:
    assert INSTALLER.count('@"') == INSTALLER.count('"@')


# --- the panel: offered, never taken ----------------------------------------


@pytest.fixture()
def roots_box(qt_app, monkeypatch):
    """A RootsBox with two suggestions that certainly exist."""
    from app.ui.widgets import roots_box as module

    monkeypatch.setattr(module, "suggested_roots",
                        lambda: [r"C:\Users\jaymin\Documents",
                                 r"C:\Users\jaymin\Pictures"])
    return module.RootsBox()


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_first_run_adds_nothing_at_all(roots_box) -> None:
    """**The promise.** Suggestions are offered; the index stays empty until
    somebody clicks. A default that indexed four folders on first launch would
    be indexing somebody's Downloads before they had agreed to anything."""
    assert roots_box.current_roots() == []


def test_a_first_run_says_nothing_is_indexed_yet(roots_box) -> None:
    assert roots_box.empty.isVisibleTo(roots_box)
    assert "nothing to search" in roots_box.empty.text()


def test_clicking_one_suggestion_adds_exactly_that_one(roots_box) -> None:
    roots_box._accept_suggestion(r"C:\Users\jaymin\Documents")
    assert roots_box.current_roots() == [r"C:\Users\jaymin\Documents"]


def test_add_all_four_adds_the_ones_that_were_offered(roots_box) -> None:
    roots_box._add_every_suggestion()
    assert roots_box.current_roots() == [r"C:\Users\jaymin\Documents",
                                         r"C:\Users\jaymin\Pictures"]


def test_an_existing_install_is_never_offered_anything(roots_box) -> None:
    """"Existing installs (roots already present) never see this."""
    roots_box.set_roots([r"D:\SearchData"])
    assert not roots_box.empty.isVisibleTo(roots_box)
    assert not roots_box.suggest_all.isVisibleTo(roots_box)


def test_the_offer_comes_back_if_the_last_folder_is_removed(roots_box) -> None:
    """Otherwise somebody who cleared the list is left with an empty panel and
    no way back to the suggestions."""
    roots_box.set_roots([r"D:\SearchData"])
    roots_box.set_roots([])
    assert roots_box.empty.isVisibleTo(roots_box)
