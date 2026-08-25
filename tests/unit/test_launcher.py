"""The launcher, and the `.\\` that nobody warned anybody about.

Layer: L0

Reported the first time it was used:

    leasha : The term 'leasha' is not recognized as the name of a cmdlet...

PowerShell does not run commands from the current directory - a deliberate
protection against a malicious `ls.exe` in a folder you happen to be standing
in. So `leasha` fails where `.\\leasha` works, and every document written for it
said `leasha`.

That is a documentation bug rather than a code one, which is exactly the kind
nothing catches. These tests catch it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("leasha.cmd", "add-to-path.ps1", "add-to-path.cmd")


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_exists(name):
    assert (ROOT / name).is_file(), f"{name} is referenced by the docs"


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_is_ascii_only(name):
    """PowerShell 5.1 decodes a BOM-less file as ANSI, and one em dash became a
    smart quote and killed the installer at parse time. ASCII sidesteps it
    entirely, and the rule applies to `.cmd` for the same reason."""
    raw = (ROOT / name).read_bytes()
    body = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw
    try:
        body.decode("ascii")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{name} contains non-ASCII at byte {exc.start}")


def test_the_powershell_script_carries_a_bom():
    """The other half of the same rule: PowerShell 5.1 needs the BOM to know a
    UTF-8 file is UTF-8."""
    assert (ROOT / "add-to-path.ps1").read_bytes()[:3] == b"\xef\xbb\xbf"


def test_add_to_path_edits_the_user_path_not_the_machine_one():
    """No administrator rights, and nothing changed for anybody else who uses
    the computer. A launcher is not worth an elevation prompt."""
    text = (ROOT / "add-to-path.ps1").read_text(encoding="utf-8-sig")

    assert '"User"' in text
    assert '"Machine"' not in text, "this must never need admin rights"


def test_add_to_path_can_be_undone():
    """Anything that edits a persistent environment variable must be
    reversible, or it is a change somebody cannot take back."""
    text = (ROOT / "add-to-path.ps1").read_text(encoding="utf-8-sig")
    assert "-Remove" in text or "$Remove" in text


def test_add_to_path_is_safe_to_run_twice():
    """Run twice, it must not add a second copy - a PATH that grows every time
    somebody runs the installer is a slow, invisible mess."""
    text = (ROOT / "add-to-path.ps1").read_text(encoding="utf-8-sig")
    assert "already on your PATH" in text


@pytest.mark.parametrize("document", ["README.md", "docs/TROUBLESHOOTING.md", "HANDOFF.md"])
def test_no_document_tells_somebody_to_type_bare_leasha(document):
    """The documentation bug itself, pinned.

    Every example must be `.\\leasha`, because that is what works in a fresh
    PowerShell before anybody has touched their PATH. Prose *about* the command
    is fine - it is the copy-pasteable lines that have to be right.
    """
    text = (ROOT / document).read_text(encoding="utf-8")

    offenders = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Only lines somebody would copy: an indented or bare command, not prose
        # and not a backticked mention inside a sentence.
        if not (stripped.startswith("leasha ") or stripped == "leasha"):
            continue
        # The troubleshooting guide quotes the failing error verbatim, and it
        # has to - somebody will paste that text in looking for it. Caught by
        # this test on the first run, which is a fair sign the check is tight
        # rather than that it is wrong.
        if "is not recognized" in stripped or stripped.startswith("leasha :"):
            continue
        offenders.append(f"line {number}: {stripped}")

    assert not offenders, (
        f"{document} tells somebody to type bare `leasha`, which PowerShell "
        f"refuses to run from the current folder:\n  " + "\n  ".join(offenders)
    )


def test_the_troubleshooting_guide_answers_the_actual_error_message():
    """Somebody hitting this will paste the error into a search, so the guide
    has to contain the words the error actually uses."""
    text = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")

    assert "is not recognized" in text
    assert "add-to-path" in text


def test_the_launcher_forwards_to_the_cli_and_the_window():
    """`leasha` on its own opens the window; anything else is a CLI command.
    A launcher that only did one would send people back to typing
    `venv\\Scripts\\python.exe -m app.cli`."""
    text = (ROOT / "leasha.cmd").read_text(encoding="utf-8")

    assert "app.main" in text
    assert "app.cli" in text
    assert "%~dp0" in text, "it must work from any working directory"


def test_the_launcher_says_what_to_do_when_nothing_is_installed():
    text = (ROOT / "leasha.cmd").read_text(encoding="utf-8")
    assert "run-install" in text


# ---------------------------------------------------------------------------
# What the installer writes into .env
# ---------------------------------------------------------------------------

#: Keys the installer must NOT write. Every one of these is a *tuning* choice
#: that lives as a default in `app/core/config.py`, and a value in `.env`
#: always wins - so pinning one here freezes it for every existing install and
#: makes the code default unreachable.
MUST_NOT_PIN = ("RERANK_MODEL", "RERANK_TOP_N", "AND_TERM_LIMIT", "EMBED_BATCH")


def _generated_env_block() -> str:
    """The here-string install.ps1 writes to `.env`, comments stripped."""
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("key", MUST_NOT_PIN)
def test_the_installer_does_not_pin_a_tuning_default(key):
    """`RERANK_MODEL=BAAI/bge-reranker-base` was written into every `.env`.

    It pinned every installed copy to the slowest of the four models measured -
    **9.2x slower than the default, for identical scores** on the evaluation
    corpus. The default in `config.py` was then changed to the fast one, and it
    did nothing for anybody, because `.env` always wins. The measurement said
    "9.2x faster" and no machine ever got it.

    This is not about that one key. Any tuning value the installer writes can
    never be improved afterwards for the people who already ran it - which is
    everybody. Paths and identifiers must be written; choices must not.
    """
    assert f"{key}=" not in _generated_env_block(), (
        f"install.ps1 writes {key} into .env, which overrides the default in "
        f"config.py for every existing install and can never be changed. "
        f"Leave it unset and let the code default apply."
    )


def test_the_installer_still_writes_the_settings_that_are_not_choices():
    """The guard above must not be satisfied by writing nothing at all."""
    block = _generated_env_block()

    for key in ("DATA_PATH", "FTS_DB", "VECTOR_PATH", "EMBED_MODEL", "EMBED_DIM"):
        assert f"{key}=" in block, f"install.ps1 no longer writes {key}"
