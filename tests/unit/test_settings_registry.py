"""Non-negotiable 11, enforced.

> Anything tunable has a UI, or is not tunable.

The rule this file exists to keep honest is the one `docs/REVIEW-2026-08-25.md`
found broken elsewhere: a thing can be designed, documented, believed, and never
wired up. A search cache was declared, described in a docstring, and passed to
nothing at any of its three construction sites for the life of the project.

So the check here is not "does the registry parse". It is **"does every key the
application actually reads have a control"** - a diff between two independent
sources of truth, `config.py` and the registry, which drift the moment somebody
adds a setting and forgets the UI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import settings_registry as reg
from app.core.env_writer import apply_values, render, write_env
from app.core.errors import AppErrorException

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_SOURCE = PROJECT_ROOT / "app" / "core" / "config.py"

#: Keys `config.py` derives rather than exposes. Paths under DATA_PATH are
#: computed from it, so giving each its own control would let a user point the
#: vectors at one drive and the database at another - a broken index with no
#: error, which is worse than not offering the choice.
DERIVED = frozenset({
    "VECTOR_PATH", "FTS_DB", "CACHE_PATH", "MODEL_CACHE",
    "STATE_PATH", "PROJECT_PATH", "LOG_PATH",
})


def env_keys_read_by_config() -> set[str]:
    """Every `.env` key `config.py` reads, parsed from its source.

    Parsing the source rather than importing keeps this test honest: it sees
    what the file does, not what an import happens to expose.
    """
    source = CONFIG_SOURCE.read_text(encoding="utf-8")
    found = set(re.findall(r'values\.get\("([A-Z_][A-Z0-9_]*)"', source))
    found |= set(re.findall(r'_require\(values,\s*"([A-Z_][A-Z0-9_]*)"\)', source))
    return found - DERIVED


# --- the rule itself --------------------------------------------------------

def test_every_setting_config_reads_has_a_control() -> None:
    """The test that makes non-negotiable 11 real."""
    reading = env_keys_read_by_config()
    registered = set(reg.keys())

    missing = sorted(reading - registered)
    assert not missing, (
        "These settings are read from .env but have no UI control, which "
        "breaks non-negotiable 11:\n  "
        + "\n  ".join(missing)
        + "\n\nEither add a Setting to app/core/settings_registry.py, or stop "
          "reading the key and make it a fixed constant with a comment."
    )


def test_no_control_for_a_setting_nothing_reads() -> None:
    """A control wired to a key the application ignores is a lie to the user.

    `rerank_toggled` and `cloud_toggled` were both emitted and connected to
    nothing; this is the shape of that bug at the settings layer.
    """
    reading = env_keys_read_by_config()
    orphans = sorted(set(reg.keys()) - reading)
    assert not orphans, (
        "These have controls but config.py never reads them, so changing them "
        "would do nothing:\n  " + "\n  ".join(orphans)
    )


def test_every_setting_names_a_real_surface() -> None:
    for setting in reg.SETTINGS:
        assert setting.surface in reg.SURFACES, (
            f"{setting.key} names surface {setting.surface!r}, which is not in "
            f"SURFACES - so no panel will build a control for it"
        )


def test_every_setting_is_in_a_known_group() -> None:
    for setting in reg.SETTINGS:
        assert setting.group in reg.GROUPS, f"{setting.key}: unknown group {setting.group!r}"


# --- shape ------------------------------------------------------------------

def test_keys_are_unique() -> None:
    keys = [setting.key for setting in reg.SETTINGS]
    assert len(keys) == len(set(keys)), "duplicate key in the registry"


@pytest.mark.parametrize("setting", reg.SETTINGS, ids=lambda s: s.key)
def test_every_setting_explains_itself(setting: reg.Setting) -> None:
    """A control with no explanation is a control nobody dares touch."""
    assert setting.label.strip()
    assert setting.help.strip(), f"{setting.key} has no help text"
    assert setting.kind in {"bool", "int", "choice", "text", "path"}


@pytest.mark.parametrize("setting", reg.SETTINGS, ids=lambda s: s.key)
def test_numeric_settings_have_bounds(setting: reg.Setting) -> None:
    """An unbounded spin box lets someone set 0 workers or 4 million MB."""
    if setting.kind == "int":
        assert setting.minimum is not None and setting.maximum is not None, (
            f"{setting.key} is numeric with no bounds"
        )
        assert setting.minimum <= setting.default <= setting.maximum


@pytest.mark.parametrize("setting", reg.SETTINGS, ids=lambda s: s.key)
def test_choice_settings_offer_their_default(setting: reg.Setting) -> None:
    if setting.kind == "choice":
        assert setting.choices, f"{setting.key} is a choice with no choices"
        assert setting.default in setting.choices


@pytest.mark.parametrize("setting", reg.SETTINGS, ids=lambda s: s.key)
def test_destructive_settings_are_flows(setting: reg.Setting) -> None:
    """Index location and the meaning model change what the index *is*.

    A plain field there can silently repoint at an empty index or invalidate
    every vector, so the registry requires a named flow instead.
    """
    if setting.destructive:
        assert setting.flow, (
            f"{setting.key} is destructive but names no flow - it would be "
            f"rendered as an ordinary control"
        )


def test_restart_settings_are_declared() -> None:
    """Anything needing a restart must say so, or the change appears to work."""
    restarting = {setting.key for setting in reg.needs_restart()}
    assert "EMBED_MODEL" in restarting
    assert "DATA_PATH" in restarting


def test_by_group_covers_everything_once() -> None:
    grouped = reg.by_group()
    total = sum(len(items) for items in grouped.values())
    assert total == len(reg.SETTINGS)


# --- round trip -------------------------------------------------------------

def test_every_default_survives_a_round_trip(tmp_path: Path) -> None:
    """Set, save, read back, unchanged.

    The View menu currently drops `group_by_document` when the text size
    changes, because preferences are rebuilt positionally. This is that class of
    bug, caught at the settings layer.
    """
    env = tmp_path / ".env"
    env.write_text("DATA_PATH=D:/index\n", encoding="utf-8")

    written = apply_values(env, {s.key: s.default for s in reg.SETTINGS})

    for setting in reg.SETTINGS:
        expected = setting.default
        if setting.kind == "bool":
            expected = "true" if expected else "false"
        assert written[setting.key] == str(expected), (
            f"{setting.key} did not survive: wrote {expected!r}, read "
            f"{written[setting.key]!r}"
        )


def test_booleans_write_lowercase(tmp_path: Path) -> None:
    """`str(True)` is 'True', which config._as_bool rejects."""
    env = tmp_path / ".env"
    written = apply_values(env, {"RERANK_ENABLED": True, "INDEX_LOW_PRIORITY": False})
    assert written["RERANK_ENABLED"] == "true"
    assert written["INDEX_LOW_PRIORITY"] == "false"


def test_writing_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    """A newer build's settings, or the installer's, must not be deleted."""
    env = tmp_path / ".env"
    env.write_text(
        "# Generated by install.ps1\n"
        "DATA_PATH=D:/index\n"
        "\n"
        "SOMETHING_FROM_A_NEWER_BUILD=keep-me\n",
        encoding="utf-8",
    )

    write_env(env, {"MIN_FREE_GB": 9})
    text = env.read_text(encoding="utf-8")

    assert "# Generated by install.ps1" in text
    assert "SOMETHING_FROM_A_NEWER_BUILD=keep-me" in text
    assert "MIN_FREE_GB=9" in text
    assert "DATA_PATH=D:/index" in text


def test_editing_happens_in_place(tmp_path: Path) -> None:
    """A hand-ordered file keeps its order; no duplicate key is appended."""
    env = tmp_path / ".env"
    env.write_text("MIN_FREE_GB=5\nDATA_PATH=D:/index\n", encoding="utf-8")
    write_env(env, {"MIN_FREE_GB": 42})
    lines = [l for l in env.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines[0] == "MIN_FREE_GB=42"
    assert sum(1 for l in lines if l.startswith("MIN_FREE_GB=")) == 1


def test_no_bom_is_written(tmp_path: Path) -> None:
    """A BOM makes the first key read as an unmatchable name."""
    env = tmp_path / ".env"
    write_env(env, {"MIN_FREE_GB": 5})
    assert not env.read_bytes().startswith(b"\xef\xbb\xbf")


def test_an_existing_bom_is_not_propagated(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfDATA_PATH=D:/index\n")
    write_env(env, {"MIN_FREE_GB": 5})
    raw = env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"DATA_PATH=D:/index" in raw


def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """A 100GB run must not litter, and neither must a settings save."""
    env = tmp_path / ".env"
    for value in range(5):
        write_env(env, {"MIN_FREE_GB": value})
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_unwritable_target_raises_an_app_error(tmp_path: Path) -> None:
    """Never a bare OSError from a settings screen."""
    blocked = tmp_path / "a_file"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(AppErrorException) as caught:
        write_env(blocked / "nested" / ".env", {"MIN_FREE_GB": 1})
    error = caught.value.error
    assert error.code == "ERR_CONFIG_INVALID"
    assert error.suggestion


def test_render_is_pure() -> None:
    """`render` touches no disk, so the writer can be reasoned about."""
    before = "DATA_PATH=D:/index\n"
    after = render(before, {"MIN_FREE_GB": 3})
    assert before == "DATA_PATH=D:/index\n"
    assert "MIN_FREE_GB=3" in after


def test_config_can_read_what_the_writer_wrote(tmp_path: Path) -> None:
    """The two halves must agree, or settings silently do nothing.

    This is the wiring test in miniature: the writer and the reader are separate
    modules that have never been proven to speak the same dialect.
    """
    from app.core.config import load_settings

    data = tmp_path / "data"
    project = tmp_path / "project"
    data.mkdir()
    project.mkdir()

    env = tmp_path / ".env"
    env.write_text(
        f"DATA_PATH={data.as_posix()}\n"
        f"PROJECT_PATH={project.as_posix()}\n"
        f"LOG_PATH={(project / 'logs').as_posix()}\n",
        encoding="utf-8",
    )

    write_env(env, {
        "RERANK_ENABLED": False,
        "MIN_FREE_GB": 11,
        "INDEX_SCHEDULE": "daily",
        "OLLAMA_MODEL": "llama3",
    })

    settings = load_settings(env)
    assert settings.rerank_enabled is False
    assert settings.min_free_gb == 11
    assert settings.index_schedule == "daily"
    assert settings.ollama_model == "llama3"
