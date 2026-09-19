"""The model seam and the model roles.

Layer: L8b. Work order `202626270611-chat-tab` sections 1c (context from the model's real
window), 3d (Fast/Thoughtful) and 4d (roles, one model by default, cheap model first).
Nothing here talks to a network: every transport is injected.
"""

from __future__ import annotations

import json

import pytest

from app.chat.config import ChatSettings, MAX_ROUNDS_CEILING
from app.chat.context import CONTEXT_TOKEN_CAP, budget_chars, window_of
from app.chat.llm import OllamaLLM, as_llm
from app.chat.roles import resolve_roles, suggest_modes
from app.chat.testing import FakeLLM
from app.core.errors import AppErrorException
from app.llm.ollama import OllamaClient

INSTALLED = ["qwen2.5:1.5b", "llama3.2:1b", "mistral:latest", "gemma4:26b", "nomic-embed-text:latest"]


# --------------------------------------------------------------------------- roles

def test_default_is_one_model_everywhere_when_nothing_small_is_installed():
    roles = resolve_roles(["mistral:latest"], configured="mistral")
    assert roles.router == roles.planner == roles.answerer == "mistral"
    assert roles.distinct() == ("mistral",)


def test_a_small_installed_model_takes_the_cheap_roles_and_the_configured_one_answers():
    roles = resolve_roles(INSTALLED, configured="mistral")
    assert roles.answerer == "mistral"
    assert roles.router == roles.planner == "llama3.2:1b"           # smallest first
    assert any("small model" in note for note in roles.notes)


def test_a_small_answerer_does_not_bring_a_second_model_along():
    roles = resolve_roles(INSTALLED, configured="mistral", answerer="qwen2.5:1.5b")
    assert roles.distinct() == ("qwen2.5:1.5b",)


def test_explicit_roles_win_exactly_as_typed_even_when_not_installed():
    roles = resolve_roles(INSTALLED, configured="mistral", router="qwen2.5:1.5b",
                          planner="phi3.5:3.8b", answerer="llama3:8b")
    assert (roles.router, roles.planner, roles.answerer) == ("qwen2.5:1.5b", "phi3.5:3.8b", "llama3:8b")
    assert any("phi3.5:3.8b" in n and "ollama pull" in n for n in roles.notes)


def test_a_missing_configured_model_falls_back_to_something_affordable_and_says_so():
    roles = resolve_roles(["qwen2.5:1.5b", "gemma4:26b"], configured="mistral")
    assert roles.answerer != "gemma4:26b"                            # never choose a 26B for somebody
    assert any("'mistral' is not installed" in n for n in roles.notes)


def test_embedding_models_are_never_chosen():
    roles = resolve_roles(["nomic-embed-text:latest"], configured="nomic-embed-text")
    assert "embed" not in roles.router or roles.router == "nomic-embed-text"      # only if configured by hand
    assert suggest_modes(["nomic-embed-text:latest"]) == {}


def test_fast_and_thoughtful_map_to_installed_models():
    modes = suggest_modes(INSTALLED, configured="mistral")
    assert modes == {"fast": "llama3.2:1b", "thoughtful": "mistral:latest"}
    assert suggest_modes(["qwen2.5:1.5b"]) == {"fast": "qwen2.5:1.5b", "thoughtful": "qwen2.5:1.5b"}


# --------------------------------------------------------------------------- settings

def test_chat_settings_have_defaults_and_clamp_wild_values():
    default = ChatSettings.from_settings(None)
    assert default.max_rounds == 3 and default.verify_threshold == pytest.approx(0.7)
    wild = ChatSettings.from_settings({"chat_max_rounds": 500, "chat_verify_strictness": 1})
    assert wild.max_rounds == MAX_ROUNDS_CEILING and wild.verify_threshold == pytest.approx(0.3)
    assert ChatSettings.from_settings({"CHAT_MAX_ROUNDS": "0"}).max_rounds == 1
    assert ChatSettings.from_settings({"chat_max_rounds": "banana"}).max_rounds == 3


def test_chat_settings_read_a_real_settings_object():
    from app.core.config import Settings

    fields = Settings.model_fields
    for name in ("chat_model", "chat_router_model", "chat_planner_model",
                 "chat_max_rounds", "chat_verify_strictness"):
        assert name in fields
    cfg = ChatSettings.from_settings(type("S", (), {
        "chat_model": "qwen2.5:1.5b", "chat_router_model": "", "chat_planner_model": "",
        "chat_max_rounds": 2, "chat_verify_strictness": 80, "ollama_url": "http://x:1",
        "ollama_model": "mistral"})())
    assert cfg.answer_model == "qwen2.5:1.5b" and cfg.max_rounds == 2
    assert cfg.verify_threshold == pytest.approx(0.8) and cfg.ollama_url == "http://x:1"


# --------------------------------------------------------------------------- the context budget

def test_the_context_budget_follows_the_models_real_window_and_is_capped_for_speed():
    small_total, small_each = budget_chars(2048, "a short question", sources=4)
    big_total, big_each = budget_chars(32768, "a short question", sources=4)
    assert small_total < big_total
    assert big_total == int(CONTEXT_TOKEN_CAP * 3.2)                 # the speed cap, not the window
    assert small_each == small_total // 4
    tiny_total, tiny_each = budget_chars(300, "x", sources=6)
    assert tiny_total > 0 and tiny_each >= 300                       # never nothing


def test_a_passage_window_is_whole_sentences_around_the_question_words():
    text = ("Nothing relevant here at all. " * 10 + "The deposit is 950 pounds. "
            + "More filler follows here too. " * 10)
    window = window_of(text, ["deposit", "950"], 120)
    assert "The deposit is 950 pounds." in window and len(window) <= 120
    assert window in text                                             # a slice: what the model saw is verbatim
    assert window_of("short", ["x"], 100) == "short"


# --------------------------------------------------------------------------- streaming and the window

class _FakeTransport:
    """The Ollama HTTP surface, without HTTP."""

    def __init__(self, pieces, *, native_ctx=8192, fail=False):
        self.pieces, self.native_ctx, self.fail = pieces, native_ctx, fail
        self.posts, self.streams = [], []

    def __call__(self, method, url, payload, timeout):
        self.posts.append((method, url))
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen2.5:1.5b"}]}
        return {"response": "ok"}

    def stream(self, url, payload, timeout):
        self.streams.append(payload)
        if self.fail:
            raise ConnectionError("connection reset by peer")
        for i, piece in enumerate(self.pieces):
            yield {"response": piece, "done": i == len(self.pieces) - 1}

    def show(self, name):
        return {"model_info": {"qwen2.context_length": self.native_ctx}}


def _llm(transport):
    client = OllamaClient("http://127.0.0.1:11434", "qwen2.5:1.5b", transport=transport)
    return OllamaLLM(client, stream_transport=transport.stream, show_transport=transport.show)


def test_streaming_yields_pieces_and_asks_ollama_for_the_window_it_budgeted():
    t = _FakeTransport(["The ", "deposit ", "is 950."])
    llm = _llm(t)
    assert "".join(llm.stream("prompt", max_tokens=50, stop=["x"])) == "The deposit is 950."
    options = t.streams[0]["options"]
    assert options["num_ctx"] == 4096 and options["num_predict"] == 50 and options["stop"] == ["x"]
    assert t.streams[0]["stream"] is True and t.streams[0]["keep_alive"] == "30m"


def test_stop_ends_the_stream_between_pieces():
    t = _FakeTransport(["a"] * 100)
    seen = []
    for piece in _llm(t).stream("p", should_stop=lambda: len(seen) >= 3):
        seen.append(piece)
    assert len(seen) == 3


def test_a_dead_connection_is_a_plain_words_error_not_a_traceback():
    with pytest.raises(AppErrorException) as caught:
        list(_llm(_FakeTransport([], fail=True)).stream("p"))
    assert caught.value.error.code == "ERR_OLLAMA_DOWN"
    assert "ollama serve" in caught.value.error.render()


def test_the_window_is_the_smaller_of_what_the_model_has_and_what_ollama_gives():
    assert _llm(_FakeTransport([], native_ctx=32768)).context_window() == 4096     # Ollama's default
    assert _llm(_FakeTransport([], native_ctx=2048)).context_window() == 2048      # the model's own limit


def test_an_unknown_window_falls_back_conservatively_and_is_cached():
    def no_show(name):
        raise RuntimeError("no /api/show")

    client = OllamaClient("http://127.0.0.1:11434", "qwen2.5:1.5b", transport=_FakeTransport([]))
    llm = OllamaLLM(client, show_transport=no_show)
    assert llm.context_window() == 4096                                # min(2 * DEFAULT_WINDOW, num_ctx)
    assert llm._window_cache


def test_as_llm_wraps_a_client_and_leaves_a_double_alone():
    client = OllamaClient("http://127.0.0.1:1", "x")
    assert isinstance(as_llm(client), OllamaLLM)
    fake = FakeLLM()
    assert as_llm(fake) is fake and as_llm(None) is None
