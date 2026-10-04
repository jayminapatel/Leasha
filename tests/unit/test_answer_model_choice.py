"""Choosing the answering model, and the cheap speed fixes behind it (2026-10-04).

Layer: L1-L8b, no Qt, no network, no model files.

The owner: "the chat is really slow ... if multiple models are available they
should be listed so they can be changed at chat or search time". What is pinned:

* the list - downloaded ONNX chat models and Ollama's text models, picture readers
  and vector models left out - and which entry Settings would use;
* a model loaded ahead of time only when it fits in the memory free now;
* the ONNX runner keeps several prompts' starts, so the router, the answer and the
  next router stop throwing each other's away;
* Ollama is asked with Chat's own window on every call, so it stops reloading the
  model between the router and the answer;
* a model picked by name on the ONNX runner is that model - and the shared one when
  it is the same, never a second copy.
"""

from __future__ import annotations

import gc
from types import SimpleNamespace

import numpy as np

from app.chat import roles
from app.chat.llm import OllamaLLM
from app.chat.roles import (
    InstalledModels, ModelOption, answer_options, default_option, fits_in_memory,
    option_value, parse_option,
)
from app.llm import engines
from app.llm.ollama import OllamaClient
from app.ort import hub
from tests.unit.test_ort_llm import EOS, fake_llm

GB = 1024 ** 3


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def installed() -> InstalledModels:
    return InstalledModels(
        reachable=True,
        names=("llava:latest", "gpt-oss:20b", "mistral:latest", "qwen2.5:1.5b", "llama3.2:1b"),
        # gpt-oss stands in for gemma4: a chat model Ollama also says reads pictures.
        vision=("llava:latest", "gpt-oss:20b"),
        sizes={"llava:latest": 4 * GB, "gpt-oss:20b": 13 * GB, "mistral:latest": 4 * GB,
               "qwen2.5:1.5b": 1 * GB, "llama3.2:1b": int(1.2 * GB)},
        ram_mb=32_000)


def test_an_option_names_its_runner_and_model():
    assert option_value("onnx", "qwen2.5-1.5b-instruct-q4") == "onnx:qwen2.5-1.5b-instruct-q4"
    assert parse_option("ollama:mistral:latest") == ("ollama", "mistral:latest")
    assert parse_option("") == ("", "") and parse_option("mistral") == ("", "")
    assert option_value("ollama", "") == ""


def test_every_model_that_can_answer_is_listed_inside_leasha_first():
    options = answer_options([("qwen2.5-1.5b-instruct-q4", "Qwen 2.5 1.5B, 4-bit", 1705)],
                             installed())
    values = [o.value for o in options]
    assert values[0] == "onnx:qwen2.5-1.5b-instruct-q4"
    assert "ollama:llava:latest" not in values, "a picture reader is Describe's, not Chat's"
    # Ollama's: smallest first, as every other model list in Leasha is ordered.
    assert values[1:] == ["ollama:llama3.2:1b", "ollama:qwen2.5:1.5b", "ollama:gpt-oss:20b",
                          "ollama:mistral:latest"]
    assert "in Leasha" in options[0].label and "1.7 GB" in options[0].label
    assert options[-1].label == "mistral:latest · Ollama · 4.0 GB"
    assert options[0].size_bytes == 1705 * 1024 ** 2


def test_ollama_not_running_lists_the_models_inside_leasha_only():
    options = answer_options([("k", "Model", 800)], InstalledModels(reachable=False))
    assert [o.value for o in options] == ["onnx:k"]


def test_the_list_starts_on_the_model_settings_would_use():
    options = answer_options([("qwen-q4", "Qwen", 1705)], installed())
    assert default_option(options, "onnx", "qwen-q4") == "onnx:qwen-q4"
    assert default_option(options, "ollama", "", "mistral") == "ollama:mistral:latest"
    assert default_option(options, "ollama", "", "phi3") == "", "not installed: nothing pretends"
    assert default_option(options, "onnx", "not-downloaded") == ""


def test_a_model_is_loaded_ahead_only_when_it_fits_in_the_memory_free_now():
    assert fits_in_memory(int(1.7 * GB), free_mb=5_400)
    assert not fits_in_memory(18 * GB, free_mb=20_000), "a 26B model is never loaded unasked"
    assert not fits_in_memory(0, free_mb=5_000) and not fits_in_memory(GB, free_mb=0)
    assert roles.PRELOAD_SHARE == 0.6


def test_an_option_says_what_it_is():
    option = ModelOption("ollama:mistral:latest", "mistral", 4 * GB)
    assert (option.engine, option.name) == ("ollama", "mistral:latest")


# ---------------------------------------------------------------------------
# The ONNX runner keeps several prompts' starts
# ---------------------------------------------------------------------------

def _run(client, prompt: str) -> int:
    """Generate once; how many prompt tokens were read (the first step's width)."""
    decoder = client._fake.decoder
    decoder.calls.clear(); decoder.positions.clear(); decoder.produced = 0
    decoder.reply = [ord("k") + 1, EOS]
    client.generate(prompt)
    return decoder.calls[0]["input_ids"][1]


ROUTER = "Classify the question into exactly one word. LOOKUP, FIND, AGGREGATE.\nQuestion: {}"
ANSWER = "You are Leasha. Answer from the numbered passages, with their numbers.\n{}"


def test_the_router_keeps_its_start_while_the_answer_is_written(monkeypatch):
    """Router, answer, then the next question's router: only the new question is read.
    With one kept prompt the answer threw the router's away (2026-10-04)."""
    client = fake_llm(monkeypatch, "ok")
    first_router = _run(client, ROUTER.format("how much was the boiler quote"))
    first_answer = _run(client, ANSWER.format("[1] the quote was 3,450 pounds"))
    second_router = _run(client, ROUTER.format("who approved it"))
    # What is read: the new question, then the chat format's closing (33 characters).
    assert second_router == len("who approved it") + 33 < first_router
    answer_again = _run(client, ANSWER.format("[1] Chris approved it on 14 March"))
    assert answer_again == len("Chris approved it on 14 March") + 33 < first_answer, \
        "the answer's instructions were kept too"


def test_no_more_than_three_prompts_are_kept_and_the_oldest_goes(monkeypatch):
    from app.ort import llm as ort_llm

    client = fake_llm(monkeypatch, "ok")
    for start in ("alpha ", "bravo ", "charlie ", "delta "):
        _run(client, start * 4)
    assert len(client._slots) == ort_llm.PREFIX_SLOTS
    assert _run(client, "alpha " * 4) > 6, "the oldest was let go"


def test_a_kept_prompt_that_is_the_start_of_a_newer_one_is_not_kept_twice(monkeypatch):
    client = fake_llm(monkeypatch, "ok")
    past = {"past_key_values.0.key": np.zeros((1, 2, 3, 4), np.float32)}
    client._keep_prefix([1, 2, 3], past)
    client._keep_prefix([1, 2, 3, 4, 5], past)
    client._keep_prefix([9, 9], past)
    # Dated note, 2026-10-04, code review: a slot also carries when it was kept.
    assert [slot[0] for slot in client._slots] == [[1, 2, 3, 4, 5], [9, 9]]


def test_the_kept_prompts_stay_inside_their_memory_budget(monkeypatch):
    from app.ort import llm as ort_llm

    monkeypatch.setattr(ort_llm, "PREFIX_BUDGET_BYTES", 1)
    client = fake_llm(monkeypatch, "ok")
    _run(client, "one prompt")
    _run(client, "another prompt")
    assert len(client._slots) == 1, "over budget, only the newest is kept"


# ---------------------------------------------------------------------------
# Ollama: one window for every call
# ---------------------------------------------------------------------------

def test_ollama_is_asked_with_chats_window_on_every_call():
    """The router's `generate` used Ollama's default window and the answer Chat's,
    and Ollama reloads a model whose window changes - 3.9-4.9 s, twice a question."""
    sent = []

    def transport(method, url, payload=None, timeout=None):
        sent.append((url, payload))
        return {"response": "LOOKUP", "models": [{"name": "qwen2.5:1.5b"}]}

    client = OllamaClient("http://x:1", "qwen2.5:1.5b", transport=transport)
    client.health = lambda force=False: True
    llm = OllamaLLM(client, num_ctx=8192)
    llm.generate("Classify", max_tokens=6)
    llm.warm()
    bodies = [p for url, p in sent if url.endswith("/api/generate")]
    assert [b["options"].get("num_ctx") for b in bodies] == [8192, 8192]
    plain = OllamaClient("http://x:1", "m", transport=transport)
    plain.health = lambda force=False: True
    plain.generate("Interpret")
    # Dated note, 2026-10-04, code review: a client built with no window sends none,
    # but Interpret's is no longer one - `engines.text_model` gives it Chat's
    # (`test_chat_model_reliability.py`).
    assert "num_ctx" not in sent[-1][1]["options"], "Interpret keeps Ollama's default"


def test_a_client_that_takes_no_window_is_still_called():
    calls = []
    client = SimpleNamespace(generate=lambda prompt, max_tokens=None: calls.append(prompt) or "x",
                             warm=lambda: True, model="m")
    llm = OllamaLLM(client, num_ctx=8192)
    assert llm.generate("p", max_tokens=3) == "x" and calls == ["p"] and llm.warm()


# ---------------------------------------------------------------------------
# A model picked by name on the ONNX runner
# ---------------------------------------------------------------------------

def _settings(cache) -> SimpleNamespace:
    return SimpleNamespace(chat_engine="onnx", model_cache=str(cache), embed_device="cpu")


def test_a_picked_onnx_model_is_that_model_and_freed_when_left(tmp_path):
    """Dated note, 2026-10-04, code review: "freed when left" was a weak reference,
    which the chat engine - asking afresh for every call - let go between the router
    and the answer, so a picked model could load twice in one question. The client is
    kept now, and what is *loaded* is bounded by `ort.llm.MAX_RESIDENT`
    (`test_chat_model_reliability.py`); `reset_shared` unloads what it forgets."""
    engines.reset_shared()
    try:
        shared = engines.text_model(_settings(tmp_path))
        gemma = "hf:onnx-community/gemma-3-1b-it-ONNX:4-bit"
        picked = engines.text_model(_settings(tmp_path), onnx_model=gemma)
        assert picked is not shared and picked._asked == gemma
        assert engines.text_model(_settings(tmp_path), onnx_model=gemma) is picked
        del picked
        gc.collect()
        assert engines.text_model(_settings(tmp_path), onnx_model=gemma)._asked == gemma
        assert len(engines._chosen) == 1, "the same client again, not a second one"
    finally:
        engines.reset_shared()
    assert not engines._chosen


def test_picking_the_model_the_shared_client_serves_is_the_shared_client(tmp_path, monkeypatch):
    engines.reset_shared()
    try:
        shared = engines.text_model(_settings(tmp_path))
        shared._loaded, shared._loaded_key, shared._serving = object(), shared._model, "q4-key"
        monkeypatch.setattr(engines, "chat_key", lambda name: name)
        assert engines.text_model(_settings(tmp_path), onnx_model="q4-key") is shared
        assert not engines._chosen, "one copy in memory, not two"
    finally:
        engines.reset_shared()


def test_a_name_that_is_not_an_onnx_chat_model_means_the_shared_one(tmp_path):
    engines.reset_shared()
    try:
        shared = engines.text_model(_settings(tmp_path))
        assert engines.text_model(_settings(tmp_path), onnx_model="mistral") is shared
        assert engines.chat_key(hub.FLORENCE.key) == "", "a photo model cannot chat"
        assert engines.chat_key(hub.QWEN_1_5B_Q4.key) == hub.QWEN_1_5B_Q4.key
    finally:
        engines.reset_shared()


def test_the_chat_engine_hands_the_picked_onnx_model_to_the_runner(monkeypatch):
    from app.chat.config import ChatSettings
    from app.chat.engine import ChatEngine

    seen = []
    monkeypatch.setattr(engines, "text_model",
                        lambda settings, **kw: seen.append(kw.get("onnx_model")) or SimpleNamespace())
    engine = ChatEngine(None, None, settings=ChatSettings(onnx_model="gemma-key"))
    engine._client_for("anything")
    assert seen == ["gemma-key"]
    assert ChatSettings.from_settings({}).onnx_model == "", "never read from .env"


def test_warming_the_chat_engine_loads_the_answering_model_and_never_raises():
    from app.chat.engine import ChatEngine

    warmed = []
    llm = SimpleNamespace(model="m", health=lambda force=False: True,
                          warm=lambda: warmed.append(1) or True)
    assert ChatEngine(None, None, llm=llm).warm() and warmed == [1]

    def broken():
        raise RuntimeError("no")

    assert ChatEngine(None, None, llm=SimpleNamespace(model="m", health=lambda force=False: True,
                                                      warm=broken)).warm() is False


def test_the_menu_lists_a_downloaded_onnx_model_without_asking_ollama(tmp_path, monkeypatch):
    """`ollama=False` (the start-up preload with nothing of Ollama's in use) sends nothing."""
    from app.ui import tasks

    snapshot = tmp_path / "models--onnx-community--Qwen2.5-1.5B-Instruct" / "snapshots" / "abc"
    for name in hub.QWEN_1_5B_Q4.files():
        (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / name).write_bytes(b"x")
    monkeypatch.setattr("app.chat.llm.probe_installed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked Ollama")))
    menu = tasks.answer_model_menu(SimpleNamespace(model_cache=str(tmp_path), chat_engine="onnx",
                                                   ollama_model="mistral"), ollama=False)
    values = [o.value for o in menu["options"]]
    assert f"onnx:{hub.QWEN_1_5B_Q4.key}" in values
    assert menu["default"] == f"onnx:{hub.QWEN_1_5B_Q4.key}"
    assert menu["free_mb"] > 0


def test_the_decoder_cache_is_sliced_from_the_prompt_that_shares_most(monkeypatch):
    client = fake_llm(monkeypatch, "ok")
    _run(client, "aaaa bbbb")
    _run(client, "zzzz yyyy")
    loaded = client._fake
    text = client._prompt([{"role": "user", "content": "aaaa cccc"}])
    reuse, kept = client._shared_prefix(loaded, [ord(c) + 1 for c in text])
    assert reuse == text.index("cccc"), "from the prompt that starts 'aaaa', not the newest"
    assert kept["past_key_values.0.key"].shape[2] >= reuse
    assert isinstance(kept["past_key_values.0.key"], np.ndarray)
