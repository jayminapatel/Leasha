r"""The walker must offer files to all three format tiers, not just tier 3.

**This disabled two thirds of the format system, silently.**

`config/extractors.toml` describes three ways a file gets read: an extension
routed to a registered extractor, an external converter, and a parser written in
Python. Only the last puts an extension in `REGISTRY` - and `resolved_extensions`
returned `REGISTRY` alone, while the walker skips any file whose suffix is not in
that set.

So 27 text and code types and **every converter format** were configured,
reported ready by `doctor`, shown enabled in Settings, and never indexed.
Installing LibreOffice could not have helped: no `.doc` ever reached a converter.
"""

from __future__ import annotations

import pytest

from app.core.formats import load_rules
from app.index.walker import WalkConfig


def walkable() -> frozenset[str]:
    return WalkConfig(roots=[]).resolved_extensions()


def test_converter_formats_are_walked() -> None:
    """The whole of tier 2. Without this the converters are decoration."""
    missing = sorted(set(load_rules().converters) - walkable())
    assert not missing, f"{missing} route to a converter and are never offered one"


def test_config_routed_extensions_are_walked() -> None:
    """Tier 1: an extension pointed at an existing extractor by configuration."""
    missing = sorted(set(load_rules().extensions) - walkable())
    assert not missing, f"{missing} are routed in config and never reach the walk"


def test_the_code_registry_is_still_included() -> None:
    from app.extract import supported_extensions

    assert supported_extensions() <= walkable()


@pytest.mark.parametrize("extension", [".doc", ".ppt", ".dwg", ".vb", ".tex", ".aspx"])
def test_specific_formats_that_were_invisible(extension: str) -> None:
    assert extension in walkable()


def test_a_disabled_route_stays_disabled(tmp_path) -> None:
    """The switch has to keep working, or this fix trades one bug for another."""
    from app.core.formats import load_rules as load

    body = '''
schema_version = 1
[extensions]
".aspx" = { extractor = "plaintext", enabled = false }
'''
    packaged = tmp_path / "extractors.toml"
    packaged.write_text(body, encoding="utf-8")
    rules = load(packaged=packaged)
    assert rules.is_enabled(".aspx") is False


def test_an_explicit_extension_set_still_wins() -> None:
    """A caller that names the extensions means those and no others."""
    only = frozenset({".txt"})
    assert WalkConfig(roots=[], extensions=only).resolved_extensions() == only
