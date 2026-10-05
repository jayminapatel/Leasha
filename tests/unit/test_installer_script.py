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


def test_the_settings_it_writes_are_only_where_things_live():
    """2026-10-06: it wrote OLLAMA_MODEL=mistral and RERANK_ENABLED=true, copied
    from install.ps1, overriding Leasha's own defaults (qwen2.5:1.5b, off)."""
    text = SCRIPT.read_text(encoding="utf-8")
    written = [line.split(":= '", 1)[1].split("=")[0] for line in text.splitlines()
               if "Lines[" in line and ":= '" in line and "=" in line.split(":= '", 1)[1]]
    assert sorted(written) == ["DATA_PATH", "LOG_PATH", "PROJECT_PATH"], written
