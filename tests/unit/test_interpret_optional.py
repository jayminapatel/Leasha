"""Interpretation is optional, and "off" means nothing is contacted.

Layer: L3

Ollama is the only part of this application that talks to another process, and
most machines have none. The promise is that the app is entirely local, so the
feature has to be off until somebody asks for it - and "off" has to mean *no
network*, not a quiet failure after a probe.

Off by default also removes a whole class of confusion: a button that cannot
work, sitting in the search bar, for somebody who never wanted the feature and
has no way to know why it is there.
"""

from __future__ import annotations

import pytest

from app.search.translate import QueryTranslator


class Tripwire:
    """Any call at all is a failure. Off must mean off."""

    model = "qwen2.5:1.5b"

    def health(self, **_kwargs):
        raise AssertionError("probed Ollama while interpretation was switched off")

    def has_model(self):
        raise AssertionError("probed Ollama while interpretation was switched off")

    def generate(self, *_args, **_kwargs):
        raise AssertionError("called Ollama while interpretation was switched off")

    def set_model(self, name):
        self.model = name


class Working:
    model = "qwen2.5:1.5b"
    def __init__(self):
        self.calls = 0
    def health(self, **_kwargs):
        return True
    def has_model(self):
        return True
    def generate(self, _prompt, **_kwargs):
        self.calls += 1
        class Reply:
            text = "from:chris licence"
        return Reply()
    def set_model(self, name):
        self.model = name


# ---------------------------------------------------------------------------
# Off
# ---------------------------------------------------------------------------

def test_it_is_off_unless_asked_for():
    """The default. Most machines have no Ollama at all."""
    assert QueryTranslator(Working()).enabled is False


def test_nothing_is_contacted_while_off():
    """**The guarantee.** Not "fails gracefully" - does not reach out at all.
    Somebody who has turned this off should be able to watch the process and see
    it talk to nothing."""
    result = QueryTranslator(Tripwire()).translate("emails from chris")
    assert result.query == "emails from chris"


def test_the_button_is_not_offered_while_off():
    assert QueryTranslator(Tripwire()).available() is False


def test_the_note_says_where_to_turn_it_on():
    """A fallback that does not say why is indistinguishable from a broken
    feature."""
    note = QueryTranslator(Tripwire()).translate("emails from chris").note
    assert "switched off" in note.lower()
    assert "settings" in note.lower()


def test_the_query_is_still_perfectly_usable_while_off():
    """The contract that must survive every change here: a caller never checks
    anything, because a search that does not run is worse than a blunt one."""
    result = QueryTranslator(Tripwire()).translate("  emails from chris  ")
    assert result.query == "emails from chris"
    assert result.changed is False
    assert result.error is None


def test_an_empty_sentence_while_off_does_not_raise():
    assert QueryTranslator(Tripwire()).translate("").query == ""


# ---------------------------------------------------------------------------
# On
# ---------------------------------------------------------------------------

def test_switching_it_on_makes_it_work():
    client = Working()
    translator = QueryTranslator(client, enabled=True)
    assert translator.translate("emails from chris").query == "from:chris licence"
    assert client.calls == 1


def test_it_can_be_switched_on_at_runtime():
    """Ticking the box must take effect on the next press, not the next launch -
    the natural thing to do after turning it on is to try it."""
    client = Working()
    translator = QueryTranslator(client)
    translator.translate("emails from chris")
    assert client.calls == 0

    translator.reconfigure(enabled=True)
    translator.translate("emails from chris")
    assert client.calls == 1


def test_switching_it_off_at_runtime_stops_it_immediately():
    client = Working()
    translator = QueryTranslator(client, enabled=True)
    translator.reconfigure(enabled=False)
    assert translator.translate("something else").changed is False


def test_switching_off_forgets_what_the_model_said():
    """A cached translation would otherwise keep being served after the feature
    was turned off, which looks exactly like the switch not working."""
    client = Working()
    translator = QueryTranslator(client, enabled=True)
    translator.translate("emails from chris")

    translator.reconfigure(enabled=False)
    result = translator.translate("emails from chris")
    assert result.query == "emails from chris", "a cached answer survived the switch"


def test_reconfiguring_without_naming_the_switch_leaves_it_alone():
    """Changing the model must not silently turn the feature on or off."""
    translator = QueryTranslator(Working(), enabled=True)
    translator.reconfigure(timeout_s=45.0)
    assert translator.enabled is True

    translator.reconfigure(enabled=False)
    translator.reconfigure(model="llama3")
    assert translator.enabled is False


@pytest.mark.parametrize("enabled", [True, False])
def test_the_switch_never_changes_what_a_search_receives(enabled):
    """Search itself must be identical either way - this feature only ever
    rewrites the text in the box before a search starts."""
    result = QueryTranslator(Working() if enabled else Tripwire(),
                             enabled=enabled).translate("pump station")
    assert isinstance(result.query, str) and result.query
