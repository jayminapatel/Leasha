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


# ---------------------------------------------------------------------------
# 2026-10-08 (owner): one box per model Leasha needs, from
# app/core/model_catalogue.py, and one [Run] line each.
# ---------------------------------------------------------------------------

def _section(name: str) -> list[str]:
    lines, inside = [], False
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("[") and line.strip() in SECTIONS:
            inside = line.strip() == f"[{name}]"
            continue
        if inside and line.strip() and not line.lstrip().startswith(";"):
            lines.append(line)
    return lines


def _task_name(key: str) -> str:
    # Inno allows letters, digits, underscores and slashes in a task name.
    return "models\\" + key.replace("-", "_")


def _catalogue(tmp_path):
    from types import SimpleNamespace

    from app.core import model_catalogue

    # Leasha's own defaults, as on a new install: nothing chosen yet.
    settings = SimpleNamespace(embed_model="BAAI/bge-small-en-v1.5",
                               rerank_model="Xenova/ms-marco-MiniLM-L-6-v2",
                               model_cache=tmp_path / "models", state_path=tmp_path / "state",
                               transcribe_model="base", chat_model="", chat_engine="onnx")
    return model_catalogue.needed_models(settings)


def test_every_needed_model_has_one_task_ticked_as_the_catalogue_says(tmp_path):
    from app.cli.models import _size

    tasks = _section("Tasks")
    assert any(line.startswith('Name: "models"; Description: "Download models now')
               for line in tasks)
    children = [line for line in tasks if line.startswith('Name: "models\\')]
    models = _catalogue(tmp_path)
    assert len(children) == len(models), children
    for model, line in zip(models, children, strict=True):          # in the catalogue's order
        assert line.startswith(f'Name: "{_task_name(model.key)}";'), (model.key, line)
        assert model.title in line, line
        assert f"({_size(model.approx_mb)})" in line, (model.key, line)
        assert ("Flags: unchecked" in line) is (not model.install_default), line
        if model.optional_note:
            assert model.optional_note.strip('.').replace('"', "'") in line, line


def test_every_needed_model_has_one_run_line_that_never_fails_the_install(tmp_path):
    run = _section("Run")
    for model in _catalogue(tmp_path):
        lines = [line for line in run if f"Tasks: {_task_name(model.key)}" in line]
        assert len(lines) == 1, (model.key, lines)
        line = lines[0]
        assert line.startswith('Filename: "{app}\\leasha-cli.exe"; '
                               f'Parameters: "models download {model.key}";'), line
        assert "Flags: waituntilterminated runhidden;" in line, line
        # The settings file has to exist before Leasha can say where models go:
        # [Run] comes before CurStepChanged(ssPostInstall) (probe, 2026-10-08).
        assert "BeforeInstall: WriteSettingsFile;" in line, line
    assert not [line for line in run if "fetch_at_install" in line]


def test_the_settings_file_is_still_written_when_no_model_is_ticked():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "procedure WriteSettingsFile();" in text
    after = text.split("procedure CurStepChanged(CurStep: TSetupStep);", 1)[1]
    assert "ssPostInstall" in after[:200] and "WriteSettingsFile();" in after[:200]


def test_the_installer_shows_the_leasha_pictures_at_every_scaling():
    """2026-10-08, the owner: Leasha's own pictures instead of Inno's plain
    ones. Every file named must exist, at Inno's size for its scaling, or the
    compile fails - or, worse, a picture is stretched on a 150% screen."""
    from PIL import Image

    import importlib.util

    # By path: `packaging` is also the name of a library this venv has.
    spec = importlib.util.spec_from_file_location(
        "make_installer_art", SCRIPT.parent / "make_installer_art.py")
    art = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(art)
    SCALES, SMALL, WIZARD, _scaled = art.SCALES, art.SMALL, art.WIZARD, art._scaled

    text = SCRIPT.read_text(encoding="utf-8")
    for key, base in (("WizardImageFile", WIZARD), ("WizardSmallImageFile", SMALL)):
        line = next(l for l in text.splitlines() if l.startswith(key + "="))
        files = line.split("=", 1)[1].split(",")
        assert len(files) == len(SCALES), key
        for name, percent in zip(files, SCALES):
            path = SCRIPT.parent / name.replace("\\", "/")
            assert path.is_file(), path
            assert Image.open(path).size == _scaled(base, percent), (path, percent)
