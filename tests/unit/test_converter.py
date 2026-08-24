"""Running external programs, and the narrow set of ways that is allowed.

Layer: L2

One implementation, and LibreOffice alone covers `.doc`, `.xls`, `.ppt`, `.rtf`,
`.pages`, `.numbers`, `.key`, `.wpd` and `.pub` — a dozen dead formats added by
editing a text file rather than writing a parser for each.

That leverage is the argument for Tier 2, and it also makes this the most
dangerous module here, because it runs programs. Most of what follows is
therefore about what it **refuses** to do, and each is asserted rather than
trusted: a security property nobody tests is a security property that erodes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.core.formats import ConverterRule, load_rules
from app.extract import converter as module
from app.extract.converter import (
    ALLOWED_BINARIES,
    available_binaries,
    convert,
    resolve_binary,
)

HAS_SOFFICE = shutil.which("soffice") is not None


def rule_for(binary: str, *args: str) -> ConverterRule:
    return ConverterRule(
        extension=".doc", command=(binary, *args),
        produces="{stem}.txt", then="plaintext",
    )


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("binary", ["curl", "sh", "bash", "cmd", "powershell",
                                    "python", "rm", "del", "wget", ""])
def test_anything_off_the_allow_list_is_refused(binary, tmp_path):
    """**The property this module exists to guarantee.**

    Config chooses among allowed converters; it cannot introduce one.
    `extractors.toml` is a file a person edits, and on a shared or synced
    machine it is a file *someone else* might edit — so a configuration format
    that can name any executable is a way to run anything.
    """
    source = tmp_path / "doc.doc"
    source.write_text("x", encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        convert(source, rule_for(binary, "{input}"))

    assert caught.value.error.code == "ERR_CONVERTER_BLOCKED"
    assert binary in caught.value.error.details or "empty" in caught.value.error.details


def test_a_blocked_binary_is_refused_before_anything_is_resolved():
    """It refuses without looking. Whether `curl` is installed is not a question
    this application should help anybody answer."""
    assert resolve_binary("curl") is None
    assert resolve_binary("sh") is None
    assert resolve_binary("rm") is None


def test_the_allow_list_lives_in_code_not_configuration():
    """Adding to it is a commit with a diff and a reviewer. Adding to a TOML
    file is a text edit that may not even be made by the machine's owner."""
    import ast

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "ALLOWED_BINARIES" for t in node.targets)
    ]
    assert assigned, "ALLOWED_BINARIES must be a literal in this module"
    assert isinstance(assigned[0].value, ast.Call), "it should be frozenset({...})"


def test_the_command_never_reaches_a_shell():
    """Filenames come from the corpus being indexed - precisely the input not to
    trust. A shell would interpret `;`, `&&`, `|`, backticks and globs in one."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "shell=True" not in code
    assert "shell=False" in code
    for dangerous in ("os.system", "os.popen", "subprocess.call(", "check_output("):
        assert dangerous not in code, f"{dangerous} must not appear here"


def test_a_filename_with_shell_metacharacters_stays_one_argument(tmp_path):
    """The property `shell=False` buys, demonstrated rather than asserted about.

    Each argument stays one argument, so a path containing `;` or a space is
    passed through intact instead of being reinterpreted.
    """
    awkward = tmp_path / "report; rm -rf ~.doc"
    command = module._build_command(
        ("soffice", "--convert-to", "txt", "{input}"),
        "/usr/bin/soffice", awkward, tmp_path,
    )
    assert command[-1] == str(awkward)
    assert len(command) == 4, "the filename must not be split into several arguments"


def test_a_missing_binary_is_reported_rather_than_run(tmp_path):
    source = tmp_path / "doc.doc"
    source.write_text("x", encoding="utf-8")

    original = shutil.which
    module.shutil.which = lambda _name: None
    try:
        with pytest.raises(AppErrorException) as caught:
            convert(source, rule_for("soffice", "{input}"))
        assert caught.value.error.code == "ERR_CONVERTER_MISSING"
    finally:
        module.shutil.which = original


def test_the_timeout_is_capped_regardless_of_configuration():
    """A converter that has not finished in five minutes is stuck, and a 100GB
    run cannot afford to discover that one file at a time."""
    assert module.MAX_TIMEOUT_S <= 300


# ---------------------------------------------------------------------------
# What it ships as
# ---------------------------------------------------------------------------

def test_every_shipped_converter_names_an_allowed_binary():
    """The check that stops the config and the allow-list drifting apart."""
    for extension, rule in load_rules().converters.items():
        assert rule.binary in ALLOWED_BINARIES, (
            f"{extension} names '{rule.binary}', which is not allowed"
        )


def test_every_shipped_converter_is_disabled():
    """The binary may not be installed, and a format that fails on every file is
    worse than one that says plainly it is off."""
    enabled = [e for e, r in load_rules().converters.items() if r.enabled]
    assert not enabled, f"these ship enabled and should not: {enabled}"


def test_available_binaries_reports_every_allowed_one():
    """Settings offers to enable exactly the converters whose binary was found,
    so nobody switches on a format that will then fail on every file."""
    found = available_binaries()
    assert set(found) == set(ALLOWED_BINARIES)


# ---------------------------------------------------------------------------
# A real conversion, when the machine has one
# ---------------------------------------------------------------------------

@pytest.fixture
def real_doc(tmp_path):
    """A genuine `.doc`, made by asking LibreOffice for one."""
    source = tmp_path / "leeds.txt"
    source.write_text(
        "Leeds site safety report. Findings from the annual inspection.",
        encoding="utf-8",
    )
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "doc",
         "--outdir", str(tmp_path), str(source)],
        capture_output=True, timeout=180, check=False,
    )
    produced = tmp_path / "leeds.doc"
    if not produced.is_file():
        pytest.skip("LibreOffice did not produce a .doc here")
    return produced


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice is not installed")
def test_a_real_document_converts_and_the_text_comes_back(real_doc):
    rule = load_rules().converter_for(".doc")

    with convert(real_doc, rule) as result:
        text = result.path.read_text(encoding="utf-8", errors="replace")
        assert "Leeds site safety report" in text
        assert Path(result.binary).is_absolute(), "the resolved path is logged, so it must be real"


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice is not installed")
def test_the_temporary_directory_is_removed(real_doc):
    """A 100GB run leaking one directory per converted file fills the disk, and
    the failure then appears somewhere else entirely."""
    rule = load_rules().converter_for(".doc")

    with convert(real_doc, rule) as result:
        folder = result.path.parent
        assert folder.is_dir()
    assert not folder.exists()


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice is not installed")
def test_closing_twice_is_harmless(real_doc):
    result = convert(real_doc, load_rules().converter_for(".doc"))
    result.close()
    result.close()


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice is not installed")
def test_the_document_points_at_the_original_not_the_temporary_file(real_doc):
    """A result linking to `/tmp/leasha-convert-xyz/report.txt` is worse than no
    result: it looks like an answer and cannot be opened."""
    from app.extract.converter import extract_via_converter

    documents = list(extract_via_converter(real_doc, load_rules().converter_for(".doc")))

    assert documents
    assert documents[0].path == real_doc
    assert documents[0].meta["converted_by"] in ("soffice", "soffice.bin")


def test_an_unknown_target_extractor_fails_precisely(tmp_path):
    from app.extract.converter import extract_via_converter

    source = tmp_path / "doc.doc"
    source.write_text("x", encoding="utf-8")
    rule = ConverterRule(".doc", ("soffice", "{input}"), "{stem}.txt", "telepathy")

    with pytest.raises(AppErrorException) as caught:
        list(extract_via_converter(source, rule))
    assert "telepathy" in caught.value.error.details


# ---------------------------------------------------------------------------
# Reached through the ordinary extraction path
# ---------------------------------------------------------------------------

def test_a_disabled_converter_leaves_the_type_unsupported(tmp_path):
    """As shipped. An unconfigured `.doc` must stay ERR_UNSUPPORTED_TYPE rather
    than becoming ERR_CONVERTER_MISSING on every file in the corpus - the first
    says "this app does not do that", the second says "something is broken"."""
    import app.extract  # noqa: F401
    from app.extract.base import extract

    source = tmp_path / "old.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0not really a doc")

    with pytest.raises(AppErrorException) as caught:
        list(extract(source))
    assert caught.value.error.code == "ERR_UNSUPPORTED_TYPE"


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice is not installed")
def test_an_enabled_converter_is_used_by_the_ordinary_extract(real_doc):
    """The wiring: `extract()` tries a converter before calling a type
    unsupported, so every caller gets Tier 2 without knowing it exists."""
    import dataclasses

    import app.core.formats as formats
    import app.extract  # noqa: F401
    from app.extract.base import extract

    original = formats.load_rules

    def with_doc_enabled(*args, **kwargs):
        rules = original(*args, **kwargs)
        converters = dict(rules.converters)
        converters[".doc"] = dataclasses.replace(converters[".doc"], enabled=True)
        return dataclasses.replace(rules, converters=converters)

    formats.load_rules = with_doc_enabled
    try:
        documents = list(extract(real_doc))
    finally:
        formats.load_rules = original

    assert documents
    assert "Leeds site safety report" in documents[0].text
    assert documents[0].path == real_doc
