"""The models folder is a setting of its own, and an index move leaves it alone.

2026-10-10. The downloaded models lived inside the index folder, so there was no
way to keep them on another drive, and moving the index moved them too. Now
`MODEL_CACHE` has a control in Storage. A folder chosen there is a choice, not
part of the index: the move keeps it where it is and keeps its `.env` line. The
default - the models inside the index folder - still moves with the index.
"""

from __future__ import annotations

from pathlib import Path

from app.core import settings_registry as reg
from app.core.env_writer import apply_values
from app.core.index_move import (
    MOVE,
    _pinned_model_cache,
    models_stay_put,
    perform_move,
    plan_move,
)


def _index(root: Path) -> Path:
    """A minimal index folder: the five subdirectories, with one file in each."""
    for name in ("vectors", "fts", "cache", "models", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "marker.bin").write_bytes(b"x")
    return root


def _env(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_models_folder_is_declared_as_a_path_setting_in_storage() -> None:
    declared = {setting.key: setting for setting in reg.SETTINGS}
    assert "MODEL_CACHE" in declared
    assert declared["MODEL_CACHE"].kind == "path"
    assert declared["MODEL_CACHE"].surface == "settings.storage"


def test_the_default_models_folder_is_not_a_choice(tmp_path: Path) -> None:
    source = tmp_path / "data"
    assert models_stay_put(source, source / "models") is False
    assert models_stay_put(source, None) is False
    assert models_stay_put(source, "") is False


def test_a_models_folder_chosen_elsewhere_is_a_choice(tmp_path: Path) -> None:
    source = tmp_path / "data"
    assert models_stay_put(source, tmp_path / "elsewhere") is True


def test_a_move_leaves_a_separately_chosen_models_folder_where_it_is(tmp_path: Path) -> None:
    source = _index(tmp_path / "old")
    destination = tmp_path / "new"
    chosen = tmp_path / "models-on-another-drive"
    chosen.mkdir()
    (chosen / "model.onnx").write_bytes(b"model")
    env = _env(tmp_path / ".env", [
        f"DATA_PATH={source}",
        f"VECTOR_PATH={source / 'vectors'}",
        f"MODEL_CACHE={chosen}",
    ])

    perform_move(source, destination, MOVE, env)

    assert (destination / "vectors" / "marker.bin").is_file()
    assert (source / "models" / "marker.bin").is_file(), "the default models folder moved"
    assert not (destination / "models").exists()
    assert (chosen / "model.onnx").is_file(), "the chosen models folder was touched"
    text = env.read_text(encoding="utf-8")
    assert f"MODEL_CACHE={chosen}" in text, "the chosen models folder lost its .env line"
    assert f"DATA_PATH={destination}" in text
    assert "VECTOR_PATH=" not in text, "the index sub-paths still pin the old drive"


def test_a_move_still_moves_the_default_models_folder_with_the_index(tmp_path: Path) -> None:
    source = _index(tmp_path / "old")
    destination = tmp_path / "new"
    env = _env(tmp_path / ".env", [f"DATA_PATH={source}"])

    perform_move(source, destination, MOVE, env)

    assert (destination / "models" / "marker.bin").is_file()
    assert not (source / "models").exists()
    assert "MODEL_CACHE" not in env.read_text(encoding="utf-8")


def test_the_plan_names_a_kept_models_folder_as_not_moved(tmp_path: Path) -> None:
    source = _index(tmp_path / "old")
    destination = tmp_path / "new"

    kept = plan_move(source, destination, MOVE, keep_models=True)
    moved = plan_move(source, destination, MOVE)

    assert "models" not in kept.moved and "models" in moved.moved
    assert "MODEL_CACHE" not in kept.env_keys_removed
    assert "MODEL_CACHE" in moved.env_keys_removed


def test_the_models_line_is_read_from_env(tmp_path: Path) -> None:
    env = _env(tmp_path / ".env", ["DATA_PATH=D:\\Data", 'MODEL_CACHE="D:\\Models"'])
    assert _pinned_model_cache(env) == Path("D:\\Models")
    assert _pinned_model_cache(tmp_path / "missing.env") is None


def test_writing_the_models_folder_and_removing_it_round_trips(tmp_path: Path) -> None:
    env = _env(tmp_path / ".env", ["DATA_PATH=D:\\Data"])

    written = apply_values(env, {"MODEL_CACHE": "E:\\Models"})
    assert written["MODEL_CACHE"] == "E:\\Models"

    removed = apply_values(env, {"MODEL_CACHE": None})
    assert "MODEL_CACHE" not in removed
    assert removed["DATA_PATH"] == "D:\\Data"
