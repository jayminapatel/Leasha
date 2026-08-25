"""A timeout is not a dead server, and a slow answer is not a permanent one.

Layer: L3

Found by running `leasha ollama --translate` on the owner's machine, which is
the whole argument for that flag existing. The output contradicted itself:

    [  OK  ] it answered a trial question in 0.59s
    ...
    Ollama is not answering, so the words you typed were searched for as-is.

It *was* answering. Three separate bugs stacked up to produce that line.

**One.** `TRANSLATE_TIMEOUT_S` was 5.0, chosen because it felt responsive. The
trial question took 8.2s on a cold model, and the real prompt is ~1,900
characters of generated grammar rather than a sentence. Every translation timed
out, always, on a perfectly healthy Ollama.

**Two.** `generate` wrapped every failure as `ERR_OLLAMA_DOWN`, so a timeout
told the user to go and check a service that was working. A diagnostic that
sends you to the wrong place with confidence is worse than none.

**Three.** `_fallback` cached the failure. Pressing Interpret again returned the
first press's failure instantly, which reads as a dead button rather than a slow
model - and it would have kept doing so after the timeout was fixed.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppErrorException, make_error
from app.search.translate import TRANSLATE_TIMEOUT_S, QueryTranslator, build_prompt


class Client:
    """An Ollama that fails in one chosen way, and counts the attempts."""

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def health(self, *, force: bool = False) -> bool:
        return True

    def has_model(self) -> bool:
        return True

    def generate(self, prompt, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise AppErrorException(self.error)
        raise AssertionError("not used")


def timeout_error():
    return make_error("ERR_OLLAMA_TIMEOUT", "llm.ollama",
                      timeout_s="30", details="mistral did not reply within 30s.")


def down_error():
    return make_error("ERR_OLLAMA_DOWN", "llm.ollama", details="connection refused")


# ---------------------------------------------------------------------------
# One: the budget must fit the work
# ---------------------------------------------------------------------------

def test_the_budget_is_bigger_than_a_cold_model_takes():
    """8.2s was measured on the owner's machine for a *trivial* prompt. Five
    seconds could never have worked, and the feature failed every time."""
    assert TRANSLATE_TIMEOUT_S > 8.2


def test_the_prompt_is_far_longer_than_the_sentence_it_is_about():
    """The reason a sentence-sized budget was wrong. The grammar is generated
    from `commands.py`, so it grows every time a filter is added - and it is
    what the model has to read before it can write a single token."""
    prompt = build_prompt("emails from chris about buying a licence")
    assert len(prompt) > 1000


def test_the_budget_is_not_so_long_that_it_reads_as_a_hang():
    """Interpret is explicit and runs off the UI thread, so waiting is fine -
    but a button that sits there for two minutes is indistinguishable from a
    frozen one."""
    assert TRANSLATE_TIMEOUT_S <= 60


# ---------------------------------------------------------------------------
# Two: say what actually happened
# ---------------------------------------------------------------------------

def test_a_timeout_does_not_claim_the_server_is_down():
    """**The bug.** The user was told to check a service that had just answered
    a trial question in 0.59 seconds."""
    result = QueryTranslator(Client(timeout_error())).translate("find the invoice")
    assert "not answering" not in result.note
    assert "not running" not in result.note


def test_a_timeout_says_it_was_a_timeout():
    result = QueryTranslator(Client(timeout_error())).translate("find the invoice")
    assert result.error is not None
    assert result.error.code == "ERR_OLLAMA_TIMEOUT"
    assert "30" in result.note or "30" in result.error.message


def test_the_suggestion_is_actionable_rather_than_generic():
    """"Try a different model" is sometimes right, but it hides that the budget
    may simply be too small - and it was."""
    error = timeout_error()
    assert "smaller" in error.suggestion or "again" in error.suggestion


def test_a_real_outage_still_says_so():
    """The distinction has to cut both ways, or it is just a renamed error."""
    result = QueryTranslator(Client(down_error())).translate("find the invoice")
    assert result.error.code == "ERR_OLLAMA_DOWN"


@pytest.mark.parametrize("error", [timeout_error(), down_error()])
def test_the_query_is_always_usable_whatever_failed(error):
    """The contract that must survive every change here: a caller never has to
    check anything, because a search that does not run is worse than a blunt
    one."""
    result = QueryTranslator(Client(error)).translate("find the invoice")
    assert result.query == "find the invoice"
    assert result.changed is False


# ---------------------------------------------------------------------------
# Three: a transient failure must not become permanent
# ---------------------------------------------------------------------------

def test_a_timeout_is_retried_on_the_next_press():
    """The model may simply have been cold. Caching the failure made the second
    press return the first press's failure instantly - a dead button."""
    client = Client(timeout_error())
    translator = QueryTranslator(client)
    translator.translate("find the invoice")
    translator.translate("find the invoice")
    assert client.calls == 2


def test_a_dead_server_is_not_re_probed_every_press():
    """The behaviour the cache was added for, which must survive the fix: a
    machine with no Ollama should not pay for a probe on every click."""
    client = Client(down_error())
    translator = QueryTranslator(client)
    translator.translate("find the invoice")
    translator.translate("find the invoice")
    assert client.calls == 1


def test_a_missing_client_is_still_not_re_probed():
    translator = QueryTranslator(None)
    first = translator.translate("find the invoice")
    second = translator.translate("find the invoice")
    assert first.query == second.query == "find the invoice"


# ---------------------------------------------------------------------------
# Four: the generation was never bounded
# ---------------------------------------------------------------------------

class Recorder:
    """Captures the options a translation asks for."""

    def __init__(self):
        self.kwargs = {}

    def health(self, *, force: bool = False) -> bool:
        return True

    def has_model(self) -> bool:
        return True

    def generate(self, prompt, **kwargs):
        self.kwargs = kwargs

        class Reply:
            text = "from:chris licence"
        return Reply()


def test_generation_is_capped():
    """**The root cause of a thirty-second wait for a ten-token answer.**

    Ollama's `num_predict` is unlimited for /api/generate, so a model asked for
    one line was free to write paragraphs of explanation - and `clean_output`
    then discarded everything after the first line. The caller waited for text
    that was thrown away.
    """
    from app.search.translate import MAX_QUERY_TOKENS

    client = Recorder()
    QueryTranslator(client).translate("emails from chris about a licence")
    assert client.kwargs.get("max_tokens") == MAX_QUERY_TOKENS


def test_the_cap_is_generous_for_a_query_but_still_a_cap():
    """`from:chris licence` is four tokens. The cap only has to stop an essay."""
    from app.search.translate import MAX_QUERY_TOKENS

    assert 16 <= MAX_QUERY_TOKENS <= 128


def test_generation_stops_at_the_end_of_the_line():
    """Cheaper than generating to the cap and truncating: the prompt asks for
    one line and only the first is kept, so everything after it is waste by
    definition."""
    client = Recorder()
    QueryTranslator(client).translate("emails from chris about a licence")
    assert client.kwargs.get("stop") == ["\n"]


def test_the_budget_still_travels_with_the_request():
    client = Recorder()
    QueryTranslator(client, timeout_s=17.0).translate("find the invoice")
    assert client.kwargs.get("timeout") == 17.0


def test_reconfiguring_changes_the_budget_and_forgets_the_old_answers():
    """A cached translation was produced by the *previous* model. Keeping it
    would make a newly-chosen model appear to do nothing on any sentence tried
    before - and trying the same sentence again is the first thing anybody does
    after switching."""
    client = Recorder()
    translator = QueryTranslator(client, timeout_s=5.0)
    translator.translate("find the invoice")

    translator.reconfigure(timeout_s=45.0)
    translator.translate("find the invoice")

    assert translator.timeout_s == 45.0
    assert client.kwargs.get("timeout") == 45.0, "the second call must not be a cache hit"


def test_reconfiguring_points_the_client_at_the_new_model():
    class WithModel(Recorder):
        model = "mistral"

        def set_model(self, name):
            self.model = name

    client = WithModel()
    QueryTranslator(client).reconfigure(model="qwen2.5:1.5b")
    assert client.model == "qwen2.5:1.5b"


def test_changing_the_model_clears_the_health_cache():
    """`health()` caches for ten seconds, and that answer was about the old
    model. A stale yes lets generate proceed against a model that is not
    installed, so the failure arrives seconds later from a call that had already
    been told everything was fine."""
    from app.llm.ollama import OllamaClient

    client = OllamaClient(transport=lambda *_a, **_k: {"models": []})
    client.health()
    client.set_model("qwen2.5:1.5b")
    assert client._healthy_until == 0.0
    assert client.model == "qwen2.5:1.5b"


def test_setting_an_empty_model_keeps_the_current_one():
    """A blank from a half-loaded dropdown must not wipe a working setting."""
    from app.llm.ollama import OllamaClient

    client = OllamaClient(model="mistral")
    client.set_model("")
    assert client.model == "mistral"
