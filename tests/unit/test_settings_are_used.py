r"""The third leg: a setting has to *do* something.

`test_settings_registry.py` proves `config.py` reads the key.
`test_settings_reachable.py` proves a control exists that writes it.
**Neither proves the value changes anything**, and that gap is exactly where
this project's recurring bug lives.

It has shipped three times now. `rerank_toggled` and `cloud_toggled` were both
emitted into nothing. `docs/REVIEW-2026-08-25.md` found a search cache
declared, documented in a docstring, and passed to no constructor for the life
of the project. And the Index Tuning screen added four strategy controls that
were declared, read into `Settings`, given controls that saved them - and
consumed by nothing at all.

Every one of those passed both existing tests. A thing can be designed,
documented, read at startup, shown on screen, saved to `.env`, and still be
inert.

**What this checks:** the `Settings` attribute is mentioned somewhere in `app/`
outside `config.py` and the registry. That is a weak test on purpose - it
proves the value is *reached*, not that it is obeyed - but the failure it
catches is total inertness, which is the failure that keeps happening. A
stronger check would need to execute the pipeline, which is what the acceptance
tests are for.

`NOT_YET_BUILT` is the pressure valve, and it is deliberately uncomfortable:
each entry has to say what is missing and be dated, so a control shipped ahead
of its feature is a line somebody has to look at rather than a silence.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.core import settings_registry as reg

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: Files that only *declare*, *load* or *display* a setting.
#:
#: **The panels are in here, and that is the point.** A widget reading
#: `settings.index_two_phase` to tick its own checkbox is the display side,
#: which `test_settings_reachable` already proves; counting it as use would let
#: a control that shows a value nothing acts on satisfy every test in the
#: suite - which is precisely how four inert strategy switches shipped.
DECLARING = {
    "config.py", "settings_registry.py", "env_writer.py", "defaults.py",
    # The settings panels: they load a value into a control and emit it back.
    "tuning_groups.py", "tuning_box.py", "long_run_box.py",
    "indexing_settings.py", "search_box.py", "storage_box.py",
    "window_box.py", "model_box.py", "file_types.py", "settings_view.py",
    "environment_box.py", "roots_box.py", "code_types_box.py",
    "search_behaviour_box.py",
}

#: Names that reach a setting **as a literal string** rather than by attribute
#: access.
#:
#: `policy.preferences` walks `SETTING_FIELDS` and calls `getattr(settings,
#: field)` with a loop variable, which no AST scanner can see - the same trap
#: the tuning screen's spin boxes fell into with `setObjectName`. So the
#: literal keys of that table count: a `"search_fix_spelling"` written down in
#: `app/` is evidence the value is reached, which is all this test claims.
def _literal_strings(tree: Any) -> set:
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}

#: Settings whose control exists before the behaviour behind it does.
#:
#: **Each line says what is missing and when it was added.** A control that
#: saves a value nothing reads is a control that lies, so this list is a
#: promise with a name on it rather than an exemption. Empty is the goal.
NOT_YET_BUILT: dict[str, str] = {}


def _settings_fields() -> dict[str, str]:
    """`{registry key: Settings attribute}`, by the naming rule `config` uses."""
    return {setting.key: setting.key.lower() for setting in reg.SETTINGS}


def _mentions() -> dict[str, set[str]]:
    """Every `Settings` attribute name mentioned per file under `app/`.

    Attribute access, `getattr` with a literal, and keyword arguments, because
    the pipeline receives most of these as constructor keywords rather than by
    reading `settings.x` directly.
    """
    found: dict[str, set[str]] = {}
    for path in APP.rglob("*.py"):
        if path.name in DECLARING:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                      # pragma: no cover
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.Call):
                # `getattr(settings, "index_two_phase", True)`
                if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                    for argument in node.args[1:2]:
                        if isinstance(argument, ast.Constant) and isinstance(
                                argument.value, str):
                            names.add(argument.value)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        found[str(path.relative_to(ROOT))] = names | _literal_strings(tree)
    return found


MENTIONS = _mentions()


@pytest.mark.parametrize("key", sorted(_settings_fields()), ids=lambda k: k)
def test_every_setting_is_read_by_something(key: str) -> None:
    """A value saved to `.env` that nothing consults is a control that lies."""
    field = _settings_fields()[key]
    where = [path for path, names in MENTIONS.items() if field in names]

    if key in NOT_YET_BUILT:
        assert not where, (
            f"{key} is on the NOT_YET_BUILT list but {where[0]} reads it now - "
            f"remove the line, the promise has been kept")
        return

    assert where, (
        f"{key} is declared, has a control, is saved to .env - and nothing in "
        f"app/ ever reads `settings.{field}`.\n"
        f"  That is U6: a setting that is designed, documented and inert.\n"
        f"  Either wire it up, or add it to NOT_YET_BUILT with what is missing."
    )


def test_the_not_yet_built_list_says_what_is_missing() -> None:
    """An exemption without a reason becomes permanent."""
    for key, reason in NOT_YET_BUILT.items():
        assert len(reason) > 25, f"{key} is exempt without saying what for"
        assert reg.by_key(key) is not None, f"{key} is not a setting any more"
