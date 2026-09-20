"""The conversation seam: Ollama's `/api/chat`, streamed, with thinking and plain-words errors.

Layer: L8b. Owner requirement 2026-09-20 (point 2: prefer `/api/chat` with role-tagged messages to
`/api/generate` with a flattened prompt; point 8: errors read like a person). Nothing here talks to
a network: every transport is injected, or `requests.post` is replaced.
"""

from __future__ import annotations

import pytest

from app.chat.llm import OllamaLLM
from app.core.errors import AppErrorException
from app.llm.ollama import OllamaClient

MESSAGES = [{"role": "system", "content": "Be kind."}, {"role": "user", "content": "hi"}]


def make(model="mistral", *, capabilities=("completion",), lines=(), num_ctx=8192, native=32768):
    """An `OllamaLLM` whose network is a recorded list of payloads and canned lines."""
    sent: list[tuple[str, dict, float]] = []

    def transport(method, url, payload, timeout):
        return {"models": [{"name": model}]}

    def stream(url, payload, budget):
        sent.append((url, payload, budget))
        yield from lines

    def show(name):
        return {"capabilities": list(capabilities), "model_info": {"x.context_length": native}}

    llm = OllamaLLM(OllamaClient("http://127.0.0.1:11434", model, transport=transport), num_ctx=num_ctx,
                    stream_transport=stream, show_transport=show)
    return llm, sent


def content(*pieces):
    return [{"message": {"role": "assistant", "content": p}, "done": False} for p in pieces] + [
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"}]


def test_the_conversation_goes_to_api_chat_as_role_tagged_messages_with_the_window_stated():
    llm, sent = make(lines=content("Hello", " there", "!"))
    assert "".join(llm.chat_stream(MESSAGES, temperature=0.6, max_tokens=300)) == "Hello there!"
    url, payload, _budget = sent[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert payload["messages"] == MESSAGES and payload["stream"] is True and payload["model"] == "mistral"
    assert payload["options"] == {"temperature": 0.6, "num_ctx": 8192, "num_predict": 300}
    assert payload["keep_alive"] == "30m" and "prompt" not in payload


def test_the_window_is_the_smaller_of_what_the_model_was_built_for_and_what_ollama_is_asked_for():
    assert make(num_ctx=8192, native=32768)[0].context_window() == 8192
    assert make(num_ctx=8192, native=4096)[0].context_window() == 4096


def test_chat_collects_a_whole_reply_for_the_small_jobs():
    llm, _sent = make(lines=content("A ", "title"))
    reply = llm.chat(MESSAGES)
    assert reply.text == "A title" and reply.model == "mistral"


def test_single_prompts_still_use_api_generate_for_routing_and_titles():
    llm, sent = make(lines=[{"response": "LOOKUP", "done": True}])
    assert "".join(llm.stream("Classify the question")) == "LOOKUP"
    assert sent[0][0].endswith("/api/generate") and sent[0][1]["prompt"] == "Classify the question"


# --------------------------------------------------------------------------- thinking

def thinking_lines():
    return [{"message": {"thinking": "let me think..."}, "done": False},
            {"message": {"thinking": " and more"}, "done": False},
            {"message": {"content": "Answer."}, "done": False}, {"done": True}]


def test_a_model_that_cannot_think_is_never_sent_think():
    llm, sent = make("llama3", capabilities=("completion",), lines=content("ok"))
    list(llm.chat_stream(MESSAGES, think="off"))
    assert "think" not in sent[0][1]                      # Ollama refuses the field from a model that cannot


def test_the_gpt_oss_family_takes_a_level_and_cannot_be_switched_off():
    for asked, sent_value in (("off", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")):
        llm, sent = make("gpt-oss:20b", capabilities=("completion", "thinking"), lines=content("ok"))
        list(llm.chat_stream(MESSAGES, think=asked))
        assert sent[0][1]["think"] == sent_value, asked


def test_other_thinking_models_take_a_boolean():
    llm, sent = make("gemma4:26b", capabilities=("completion", "thinking"), lines=content("ok"))
    list(llm.chat_stream(MESSAGES, think="off"))
    list(llm.chat_stream(MESSAGES, think="medium"))
    assert [p["think"] for _u, p, _b in sent] == [False, True]


def test_leaving_think_unset_sends_nothing_and_the_reasoning_is_never_yielded():
    llm, sent = make("gpt-oss:20b", capabilities=("thinking",), lines=thinking_lines())
    assert "".join(llm.chat_stream(MESSAGES)) == "Answer."      # only the reply, not the reasoning
    assert "think" not in sent[0][1]


def test_stop_works_during_the_long_silent_stretch_of_reasoning():
    llm, _sent = make("gpt-oss:20b", capabilities=("thinking",), lines=thinking_lines())
    asked = {"n": 0}

    def stop() -> bool:
        asked["n"] += 1
        return True

    assert list(llm.chat_stream(MESSAGES, should_stop=stop)) == []
    assert asked["n"] == 1                                       # asked on the first line, which held no reply yet


def test_stop_ends_the_stream_between_pieces():
    llm, _sent = make(lines=content(*"abcdefgh"))
    seen = []
    for piece in llm.chat_stream(MESSAGES, should_stop=lambda: len(seen) >= 3):
        seen.append(piece)
    assert seen == ["a", "b", "c"]


# --------------------------------------------------------------------------- failures, in plain words

def test_ollama_not_running_is_the_one_known_error_with_the_fix():
    llm = OllamaLLM(OllamaClient("http://127.0.0.1:9", "mistral", timeout=1, connect_timeout=0.2))
    with pytest.raises(AppErrorException) as raised:
        list(llm.chat_stream(MESSAGES))
    assert raised.value.error.code == "ERR_OLLAMA_DOWN" and "ollama serve" in raised.value.error.render()


def test_a_reply_that_dies_part_way_says_ollama_went_away_and_keeps_the_pieces_sent_before():
    def dying(url, payload, budget):
        yield {"message": {"content": "The deposit rules are "}, "done": False}
        raise ConnectionError("reset by peer")

    llm = OllamaLLM(OllamaClient("http://127.0.0.1:11434", "mistral",
                                 transport=lambda *a: {"models": [{"name": "mistral"}]}),
                    stream_transport=dying)
    got: list[str] = []
    with pytest.raises(AppErrorException) as raised:
        for piece in llm.chat_stream(MESSAGES):
            got.append(piece)
    assert got == ["The deposit rules are "] and raised.value.error.code == "ERR_OLLAMA_DOWN"


def test_a_timeout_mid_reply_is_the_plain_timeout_sentence():
    class ReadTimeout(Exception):
        pass

    def slow(url, payload, budget):
        yield {"message": {"content": "Hm"}, "done": False}
        raise ReadTimeout("read timed out")

    llm = OllamaLLM(OllamaClient("http://127.0.0.1:11434", "mistral",
                                 transport=lambda *a: {"models": []}), stream_transport=slow)
    with pytest.raises(AppErrorException) as raised:
        list(llm.chat_stream(MESSAGES, timeout=7))
    assert raised.value.error.code == "ERR_OLLAMA_TIMEOUT" and "7s" in raised.value.error.message


def test_an_error_line_from_ollama_is_a_plain_sentence_not_a_traceback():
    llm, _sent = make(lines=[{"error": "model requires more system memory (12 GiB) than is available"}])
    with pytest.raises(AppErrorException) as raised:
        list(llm.chat_stream(MESSAGES))
    assert "Traceback" not in raised.value.error.render() and raised.value.error.message


def test_a_model_that_is_not_installed_says_so_and_gives_the_pull_command(monkeypatch):
    class Reply:
        status_code = 404
        text = '{"error":"model \'nope\' not found"}'

        def json(self):
            return {"error": "model 'nope' not found"}

        def close(self):
            pass

    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply())
    llm = OllamaLLM(OllamaClient("http://127.0.0.1:11434", "nope",
                                 transport=lambda *a: {"models": [{"name": "nope"}]}))
    with pytest.raises(AppErrorException) as raised:
        list(llm.chat_stream(MESSAGES))
    error = raised.value.error
    assert error.code == "ERR_OLLAMA_MODEL_MISSING"
    assert error.message == "The model 'nope' is not installed in Ollama."
    assert "ollama pull nope" in error.suggestion and error.action_payload == "ollama pull nope"
