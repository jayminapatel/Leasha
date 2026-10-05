"""packaging/installer.iss keeps to the two rules Inno Setup reads it by.

2026-10-06. The first compile stopped twice at the same line: a wrapped line in
the [Code] section began with "[" ("Invalid section tag") and, once that was
fixed, with "#13#10" ("Unknown preprocessor directive"). Inno reads either at
the start of any line, however it is indented. The owner's own build failed on
the first. Inno Setup is not on the test machines, so this reads the file.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "installer.iss"
SECTIONS = {"[Setup]", "[Messages]", "[Files]", "[Icons]", "[Tasks]", "[Run]", "[Code]",
            "[UninstallDelete]", "[InstallDelete]", "[Registry]", "[Dirs]", "[Languages]"}
DIRECTIVES = ("#define", "#ifndef", "#ifdef", "#endif", "#else", "#include", "#if ")


def test_only_section_headers_start_with_a_bracket():
    wrong = [(n, line) for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), 1)
             if line.lstrip().startswith("[") and line.strip() not in SECTIONS]
    assert not wrong, wrong


def test_only_preprocessor_directives_start_with_a_hash():
    wrong = [(n, line) for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), 1)
             if line.lstrip().startswith("#") and not line.lstrip().startswith(DIRECTIVES)]
    assert not wrong, wrong


def test_the_build_script_refuses_a_build_with_a_settings_file_in_it():
    text = (SCRIPT.parent / "build.ps1").read_text(encoding="utf-8-sig")
    assert '_internal\.env' in text and "throw" in text.split('_internal\.env', 1)[1][:200]
