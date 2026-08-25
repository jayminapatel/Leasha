r"""Getting back to a default, which used to be impossible.

Layer: L0 (the reading) / L5 (the affordance)

**`.env` always beats the default in `config.py`.** So a setting written down
once is pinned for ever, and no improved default can reach that machine again.
The worked example is not hypothetical: the installer wrote
`RERANK_MODEL=BAAI/bge-reranker-base` into every install, the code default was
later changed to a model measured **9.2x faster**, and not one machine got it.
A real measurement, a shipped fix, and nobody received it.

The operation to undo that has existed since `75c48ae` -
`apply_values(path, {"KEY": None})` removes a key - and nothing called it.

The property under test that matters most is the last one: **`None` removes and
the default *value* re-pins.** The two calls differ by one character, look
identical on screen, and only one of them is the fix.
"""

from __future__ import annotations

import pytest

from app.core.env_writer import apply_values, pinned_keys
from app.core.settings_registry import by_key, keys

ENV = """\
# A file with comments and a key nobody here has heard of.
DATA_PATH=D:\\Data
RERANK_ENABLED=true
RERANK_MODEL=BAAI/bge-reranker-base
SOMEBODY_ELSES_KEY=leave me alone
"""


@pytest.fixture()
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ENV, encoding="utf-8")
    return path


# --- what is pinned ---------------------------------------------------------

def test_the_keys_written_down_are_the_pinned_ones(env):
    found = pinned_keys(env, keys())

    assert "RERANK_MODEL" in found
    assert "RERANK_ENABLED" in found


def test_a_key_this_build_does_not_know_is_not_offered(env):
    """A line another installer added is not something this window should offer
    to remove - it cannot say what removing it would do."""
    assert "SOMEBODY_ELSES_KEY" not in pinned_keys(env, keys())


def test_they_come_back_in_the_order_they_appear(env):
    found = pinned_keys(env, keys())

    assert found.index("RERANK_ENABLED") < found.index("RERANK_MODEL")


def test_a_key_set_to_exactly_the_default_is_still_pinned(tmp_path):
    """**The one somebody would get wrong by reading the controls instead.**

    A control showing the default value looks untouched, and the key is still
    written down - so it still stops a better default from ever arriving. What
    matters is whether the line exists, not what it says.
    """
    setting = by_key("RERANK_TOP_N")
    path = tmp_path / ".env"
    path.write_text(f"RERANK_TOP_N={setting.default}\n", encoding="utf-8")

    assert pinned_keys(path, keys()) == ["RERANK_TOP_N"]


def test_a_file_that_cannot_be_read_pins_nothing(tmp_path):
    """The safe answer: the button is disabled rather than offering to remove
    lines from a file nobody can open."""
    assert pinned_keys(tmp_path / "not-there.env", keys()) == []


def test_a_key_repeated_is_reported_once(tmp_path):
    path = tmp_path / ".env"
    path.write_text("RERANK_TOP_N=10\nRERANK_TOP_N=20\n", encoding="utf-8")

    assert pinned_keys(path, keys()) == ["RERANK_TOP_N"]


# --- what resetting does ----------------------------------------------------

def test_none_removes_the_line_so_the_code_default_applies_again(env):
    apply_values(env, {"RERANK_MODEL": None})

    assert "RERANK_MODEL" not in pinned_keys(env, keys())
    assert "RERANK_MODEL" not in env.read_text(encoding="utf-8")


def test_writing_the_default_value_back_is_the_bug_not_the_fix(env):
    """**The two calls differ by one character and look identical on screen.**

    `{"RERANK_MODEL": setting.default}` re-pins the key at today's default,
    which means the next improvement to it never arrives - the exact failure
    this feature exists to end.
    """
    setting = by_key("RERANK_MODEL")
    apply_values(env, {"RERANK_MODEL": setting.default})

    assert "RERANK_MODEL" in pinned_keys(env, keys()), (
        "writing the default back should still pin it - if this ever stops "
        "being true, the reset path can be simplified")


def test_resetting_one_setting_leaves_the_others_alone(env):
    apply_values(env, {"RERANK_MODEL": None})
    remaining = pinned_keys(env, keys())

    assert "RERANK_ENABLED" in remaining
    assert "DATA_PATH" in remaining


def test_resetting_everything_leaves_lines_this_build_does_not_own(env):
    """`env_writer` preserves keys it has never heard of - a newer installer's
    settings must survive an older window restoring defaults."""
    apply_values(env, {key: None for key in pinned_keys(env, keys())})

    assert "SOMEBODY_ELSES_KEY" in env.read_text(encoding="utf-8")


def test_the_comments_survive(env):
    """A settings file that loses its comments each time a control moves is a
    file nobody can read afterwards."""
    apply_values(env, {"RERANK_ENABLED": None})

    assert "nobody here has heard of" in env.read_text(encoding="utf-8")


def test_removing_a_key_that_is_not_there_is_harmless(env):
    """The button offers every pinned key at once; a file edited between the
    button being built and being pressed is normal."""
    apply_values(env, {"EMBED_DIM": None})          # never written

    assert "DATA_PATH" in pinned_keys(env, keys())


# --- every registry default is describable ---------------------------------

@pytest.mark.parametrize("key", sorted(keys()))
def test_every_setting_has_a_default_worth_showing(key):
    """The reset menu names what it is going back to. A menu entry saying only
    "Reset" asks somebody to remember what the default was."""
    setting = by_key(key)

    assert setting is not None
    assert setting.default is not None or setting.kind == "text", (
        f"{key} has no default, so 'reset to default' has nothing to say")
