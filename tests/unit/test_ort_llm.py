"""`app/ort/llm.py` and `app/llm/engines.py` - the chat model inside Leasha.

Layer: L2. No model files: a fake loaded model (scripted decoder, a tokenizer
that maps characters to ids) stands in for Qwen, so what is tested is the
protocol Chat and Interpret rely on, not the model's wording.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.errors import AppErrorException
from app.llm import engines
from app.ort import hub, llm as ort_llm
from app.ort.llm import OnnxLLM, chatml, first_json

EOS = 0


class CharTokenizer:
    """id = ord(char) + 1; 0 is the end token."""

    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=[ord(c) + 1 for c in text])

    def decode(self, ids, skip_special_tokens=True):
        return "".join(chr(i - 1) for i in ids if i > 0)


class ScriptedDecoder:
    """Answers `reply` one character per step, then the end token."""

    def __init__(self, reply: str, *, delay_s: float = 0.0) -> None:
        self.reply = [ord(c) + 1 for c in reply] + [EOS]
        self.delay_s = delay_s
        self.calls = []
        self.positions = []
        self.produced = 0
        self.past = {}

    def reset(self):
        self.produced = 0
        self.past = {}

    def step(self, inputs):
        import time

        self.calls.append({k: v.shape for k, v in inputs.items()})
        self.positions.append(inputs["position_ids"][0].tolist())
        grown = sum(v.shape[2] for v in list(self.past.values())[:1]) + inputs["input_ids"].shape[1]
        self.past = {"past_key_values.0.key": np.zeros((1, 2, grown, 4), np.float32)}
        if self.delay_s:
            time.sleep(self.delay_s)
        logits = np.zeros(200, np.float32)
        logits[self.reply[min(self.produced, len(self.reply) - 1)]] = 9.0
        self.produced += 1
        return logits


def fake_llm(monkeypatch, reply: str, **kw) -> OnnxLLM:
    client = OnnxLLM(None, **kw)
    loaded = SimpleNamespace(decoder=ScriptedDecoder(reply), tokenizer=CharTokenizer(),
                             eos=[EOS], on_gpu=False, prompt_format="chatml")
    monkeypatch.setattr(client, "_ensure", lambda: loaded)
    client._fake = loaded
    return client


def test_chatml_adds_a_system_turn_and_opens_the_assistant():
    text = chatml([{"role": "user", "content": "hi"}])
    assert text.startswith("<|im_start|>system\n")
    assert "<|im_start|>user\nhi<|im_end|>\n" in text
    assert text.endswith("<|im_start|>assistant\n")
    own = chatml([{"role": "system", "content": "S"}, {"role": "user", "content": "q"}])
    assert own.count("<|im_start|>system") == 1 and "S<|im_end|>" in own


def test_first_json_finds_the_object_in_chatter():
    assert json.loads(first_json('Sure! {"queries": ["a", "b {c}"]} hope that helps')) == {
        "queries": ["a", "b {c}"]}
    assert first_json("no json here") == "no json here"


def test_generate_returns_a_completion(monkeypatch):
    client = fake_llm(monkeypatch, "from:chris licence")
    result = client.generate("emails from chris about a licence", max_tokens=64)
    assert result.text == "from:chris licence"
    assert result.model == hub.QWEN_1_5B.key and result.elapsed_s >= 0


def test_the_decoder_gets_ids_mask_and_positions(monkeypatch):
    client = fake_llm(monkeypatch, "ok")
    client.generate("q")
    first, second = client._fake.decoder.calls[0], client._fake.decoder.calls[1]
    assert set(first) == {"input_ids", "attention_mask", "position_ids"}
    assert first["input_ids"][1] > 1 and second["input_ids"] == (1, 1)
    assert second["attention_mask"][1] == first["input_ids"][1] + 1


def test_stop_strings_end_the_reply_and_are_not_included(monkeypatch):
    client = fake_llm(monkeypatch, "LOOKUP\nthen more")
    assert client.generate("route", stop=["\n"]).text == "LOOKUP"


def test_streaming_yields_the_whole_reply_in_pieces(monkeypatch):
    client = fake_llm(monkeypatch, "Hello there")
    pieces = list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert len(pieces) > 1 and "".join(pieces) == "Hello there"


def test_json_mode_hands_back_parseable_json(monkeypatch):
    client = fake_llm(monkeypatch, 'Here you go: {"a": 1}')
    assert json.loads(client.generate("plan", json_mode=True).text) == {"a": 1}


def test_a_reply_the_clock_cuts_short_is_the_local_timeout(monkeypatch):
    """Not the partial text: half a query in Interpret's box is worse than none."""
    client = fake_llm(monkeypatch, "x" * 100)
    client._fake.decoder.delay_s = 0.02
    with pytest.raises(AppErrorException) as caught:
        client.generate("slow", timeout=0.1, max_tokens=100)
    assert caught.value.error.code == "ERR_LOCAL_MODEL_TIMEOUT"
    assert "Ollama" not in caught.value.error.message


def test_a_reply_that_ends_in_time_is_not_a_timeout(monkeypatch):
    client = fake_llm(monkeypatch, "quick")
    assert client.generate("fast", timeout=30).text == "quick"


def test_a_missing_model_says_download_not_ollama(tmp_path):
    client = OnnxLLM(tmp_path)
    assert not client.has_model() and not client.health()
    assert client.available_models() == []
    with pytest.raises(AppErrorException) as caught:
        client.generate("anything")
    assert caught.value.error.code == "ERR_LOCAL_MODEL_MISSING"
    assert "Ollama" not in caught.value.error.message


def test_a_downloaded_model_is_listed(tmp_path):
    snapshot = tmp_path / "models--onnx-community--Qwen2.5-1.5B-Instruct" / "snapshots" / "abc"
    for name in hub.QWEN_1_5B.files():
        (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / name).write_bytes(b"x")
    client = OnnxLLM(tmp_path)
    assert client.has_model() and client.available_models() == [hub.QWEN_1_5B.key]


def test_pictures_are_refused_plainly(monkeypatch):
    client = fake_llm(monkeypatch, "x")
    with pytest.raises(AppErrorException) as caught:
        client.generate("describe", images=["AAAA"])
    assert caught.value.error.code == "ERR_LOCAL_MODEL_FAILED"


def test_the_engine_setting_chooses_the_client():
    engines.reset_shared()
    onnx = engines.text_model(SimpleNamespace(chat_engine="onnx", model_cache="", embed_device="cpu"))
    assert getattr(onnx, "engine", "") == "onnx"
    again = engines.text_model(SimpleNamespace(chat_engine="onnx", model_cache="", embed_device="cpu"))
    assert again is onnx, "Interpret and Chat share one model inside Leasha"
    ollama = engines.text_model(SimpleNamespace(chat_engine="ollama", ollama_url="http://x:1",
                                                ollama_model="m"))
    assert type(ollama).__name__ == "OllamaClient" and ollama.model == "m"
    assert engines.engine_of(SimpleNamespace()) == "onnx", "the default is inside Leasha"
    engines.reset_shared()


def test_describe_goes_to_florence_when_the_engine_is_onnx(monkeypatch, tmp_path):
    from app.extract import florence_tagger, vision_caption

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "describe", lambda path: "A red ball on grass.")
    client = OnnxLLM(tmp_path)
    assert vision_caption.available(client)
    result = vision_caption.describe_image(tmp_path / "x.jpg", client)
    assert result.caption == "A red ball on grass." and result.model == "Florence-2"
    monkeypatch.setattr(florence_tagger, "available", lambda: False)
    assert "Florence-2" in vision_caption.unavailable_reason(client)


def test_the_module_says_which_engine_it_is():
    assert ort_llm.ENGINE == "onnx" and OnnxLLM.engine == "onnx"


def test_a_second_prompt_reads_only_what_it_does_not_share(monkeypatch):
    """The long instructions Interpret sends every time are read once; the
    next request reads only its own sentence, at the right positions."""
    client = fake_llm(monkeypatch, "ok")
    decoder = client._fake.decoder
    client.generate("Rewrite as a search: emails from chris")
    first_prompt = decoder.calls[0]["input_ids"][1]
    decoder.calls.clear(); decoder.positions.clear(); decoder.reply = [ord("k") + 1, EOS]
    decoder.produced = 0
    client.generate("Rewrite as a search: photos of the beach")
    second = decoder.calls[0]["input_ids"][1]
    assert 0 < second < first_prompt, "only the part after the shared start is read"
    assert decoder.positions[0][0] > 0, "reading resumes where the shared part ends"
    assert decoder.calls[0]["attention_mask"][1] == decoder.positions[0][-1] + 1


def test_a_different_prompt_starts_from_the_beginning(monkeypatch):
    client = fake_llm(monkeypatch, "ok")
    decoder = client._fake.decoder
    client.generate("alpha")
    decoder.calls.clear(); decoder.positions.clear(); decoder.produced = 0
    client.chat_stream  # noqa: B018 - same client
    list(client.chat_stream([{"role": "system", "content": "totally different"},
                             {"role": "user", "content": "beta"}]))
    # Only the opening "<|im_start|>system\n" is shared (this fake tokenizer makes
    # one token per character), so reading starts right after it.
    assert decoder.positions[0][0] == len("<|im_start|>system\n")
