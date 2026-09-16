r"""Every face UI label passes the plain-words deny-list. Work order 0j's own
test list.

Layer: L5 (checked without Qt - static text review, the same shape
`test_search_policy.py`'s own jargon check already uses)

`test_search_policy.py`'s own comment: "a second copy of these words is a
second guard that can disagree with this one" - so this imports the one list
rather than declaring a third.
"""

from __future__ import annotations

from tests.unit.test_search_policy import SURFACE_JARGON  # noqa: E402


def _texts():
    """Every label/help/tooltip string this order added, gathered from
    source rather than retyped - a retyped copy could drift from what
    actually ships and pass while the real string fails."""
    texts: list[tuple[str, str]] = []

    from app.core import settings_registry as reg

    for setting in reg.SETTINGS:
        if setting.key in (
            "OLLAMA_VISION_MODEL", "CAPTION_TRICKLE_ENABLED",
            "PEOPLE_RECOGNITION_ENABLED",
        ):
            texts.append((setting.key, f"{setting.label} {setting.help}"))

    from app.search.commands import COMMANDS

    for command in COMMANDS:
        if command.name == "who":
            texts.append(("command:who", f"{command.summary} {command.value_hint}"))

    return texts


def test_settings_and_command_wording_speaks_plain_english():
    for name, text in _texts():
        lowered = text.lower()
        for jargon in SURFACE_JARGON:
            assert jargon not in lowered, (name, jargon, text)


def test_settings_and_command_wording_is_not_empty():
    """Guards against the gather-by-key list above silently finding nothing,
    the same "guard against a false pass" shape `test_there_are_documents_to
    _check` uses for the doc-header sweep."""
    assert len(_texts()) >= 4


def test_the_people_recognition_switch_names_exactly_what_it_stores():
    """The order's own guardrails: "the whole feature's off-switch also
    states, plainly, what stored data the switch governs"."""
    from app.core import settings_registry as reg

    setting = next(s for s in reg.SETTINGS if s.key == "PEOPLE_RECOGNITION_ENABLED")
    lowered = setting.help.lower()
    assert "off by default" in lowered
    assert "forget this person" in lowered
    assert setting.default is False
