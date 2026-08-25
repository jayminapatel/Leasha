r"""Loading the model before the first press, not during it.

Layer: L4/L8a

The owner passed on a piece of advice: *"By default, Ollama unloads models from
system memory after 5 minutes of inactivity"*. It applies here, with one
correction, and the correction is the whole design.

**Warming at startup would be wrong.** Interpretation is optional, off by
default, and most machines have no Ollama at all; loading a model into VRAM for
somebody who never presses the button is a cost they did not ask for, in an
application whose promise is that it does nothing until asked.

**Not warming at all is also wrong.** The load costs 8.2s here against a
translate budget of five seconds, so the first press after any quiet period
times out - and `ERR_OLLAMA_TIMEOUT` reads as a broken model rather than a cold
one, which sends the diagnosis in exactly the wrong direction. That has already
happened once in this project, with the connect/read timeouts.

So: warm on the transition - when somebody switches it on, and at startup only
if it was already on - and tell Ollama to hold the model for thirty minutes
rather than five.
"""

from __future__ import annotations

from app.llm.ollama import KEEP_ALIVE
from app.search.translate import QueryTranslator


class FakeClient:
    """Only what the translator touches."""

    model = "mistral"

    def __init__(self, *, raises: bool = False, warms: bool = True):
        self._raises = raises
        self._warms = warms
        self.warmed = 0

    def warm(self, **_kwargs):
        self.warmed += 1
        if self._raises:
            raise RuntimeError("connection refused")
        return self._warms

    def health(self, **_kwargs):
        return True

    def has_model(self):
        return True


# --- when it happens --------------------------------------------------------

def test_switching_it_on_is_the_moment_to_warm():
    translator = QueryTranslator(FakeClient(), enabled=False)

    translator.reconfigure(enabled=True)

    assert translator.just_enabled is True


def test_saving_the_same_setting_again_does_not_warm_again():
    """Settings are saved on every edit of the timeout or the model box. A
    network round trip per keystroke in a settings panel is exactly the shape
    of slowness this application has a rule against."""
    translator = QueryTranslator(FakeClient(), enabled=False)
    translator.reconfigure(enabled=True)
    translator.just_enabled = False              # the caller consumed it

    translator.reconfigure(enabled=True)

    assert translator.just_enabled is False


def test_choosing_a_different_model_warms_the_new_one():
    """The old model was warm and is now the wrong one. Trying it immediately
    is the first thing anybody does after switching."""
    translator = QueryTranslator(FakeClient(), enabled=True)
    translator.just_enabled = False

    translator.reconfigure(model="llama3")

    assert translator.just_enabled is True


def test_switching_it_off_does_not_warm():
    translator = QueryTranslator(FakeClient(), enabled=True)
    translator.just_enabled = False

    translator.reconfigure(enabled=False)

    assert translator.just_enabled is False


def test_a_new_install_has_nothing_to_warm():
    """Off by default, and nothing contacts a service that is not there."""
    assert QueryTranslator(FakeClient(), enabled=False).just_enabled is False


# --- what warming does ------------------------------------------------------

def test_warming_reaches_the_client():
    client = FakeClient()
    translator = QueryTranslator(client, enabled=True)

    assert translator.warm() is True
    assert client.warmed == 1


def test_it_does_nothing_when_the_feature_is_off():
    """**The guarantee that keeps the promise.** Off means nothing is loaded
    and nothing is contacted, whatever else calls this."""
    client = FakeClient()

    assert QueryTranslator(client, enabled=False).warm() is False
    assert client.warmed == 0


def test_a_client_that_cannot_be_warmed_is_not_an_error():
    """Warming is an optimisation, and an optimisation that can fail a search
    is not one. The cost of failing here is a slow first press - which is where
    we started, so nothing is lost."""
    assert QueryTranslator(FakeClient(raises=True), enabled=True).warm() is False


def test_a_client_with_no_warm_at_all_is_fine():
    """`QueryTranslator` takes any client-shaped object, including the fakes in
    a dozen other tests. A method that must exist is a contract nobody was
    told about."""
    class Old:
        model = "x"

        def health(self, **_kw):
            return True

        def has_model(self):
            return True

    assert QueryTranslator(Old(), enabled=True).warm() is False


def test_there_is_no_client_at_all():
    assert QueryTranslator(None, enabled=True).warm() is False


# --- how long it stays warm -------------------------------------------------

def test_keep_alive_is_longer_than_ollama_s_own_default():
    """Five minutes is Ollama's default and is shorter than the gap between two
    searches. Any value under it leaves the reload paid on nearly every press,
    which is the problem this exists to remove."""
    assert KEEP_ALIVE.endswith("m")
    assert int(KEEP_ALIVE.rstrip("m")) > 5


def test_every_generate_carries_keep_alive():
    """**On the request, not configured once.** `keep_alive` is a property of
    the call; a server restarted underneath us would otherwise silently go back
    to five minutes and nothing would say so."""
    import inspect

    from app.llm.ollama import OllamaClient

    source = inspect.getsource(OllamaClient.generate)

    assert "keep_alive" in source
