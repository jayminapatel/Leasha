"""The ONNX chat model on the real Qwen2.5-1.5B int8: load, first token, speed, and the
three things Leasha asks of it (Interpret, a Chat answer, the planner's JSON)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ort.llm import OnnxLLM  # noqa: E402


def _cache() -> Path:
    from app.core.config import load_settings

    return Path(load_settings(create_dirs=False, check_writable=False).model_cache)



# Optional first argument: a catalogue key (order 1c item 8 checks one Gemma and one
# Llama 3 model this way). None given: the chat model Leasha would choose.
MODEL = sys.argv[1] if len(sys.argv) > 1 else ""
llm = OnnxLLM(_cache(), MODEL, device="auto", timeout=300)
t = time.time()
ok = llm.warm()
print(f"model: {MODEL or '(default)'}")
print(f"load: {time.time()-t:.1f}s ok={ok} health={llm.health()} models={llm.available_models()}")

# Speed: stream a longer answer, time the first piece and the rate.
t = time.time(); first = None; text = ""
for piece in llm.chat_stream([{"role": "user", "content":
                               "In three sentences, what is a PST file and why do people keep them?"}],
                              temperature=0.0, max_tokens=120):
    if first is None:
        first = time.time() - t
    text += piece
total = time.time() - t
tokens = len(llm._ensure().tokenizer.encode(text, add_special_tokens=False).ids)
print(f"chat: first piece {first:.2f}s, {tokens} tokens in {total:.1f}s "
      f"= {tokens/max(total-first,1e-6):.1f} tok/s after the first\n  -> {text.strip()}")

# Interpret: the translator's real prompt shape is a one-line query; stop at newline.
from app.search.translate import QueryTranslator  # noqa: E402
tr = QueryTranslator(llm, timeout_s=60, enabled=True)
for sentence in ("emails from chris about a licence last year",
                 "the photos from the beach holiday in 2019"):
    t = time.time(); result = tr.translate(sentence)
    print(f"interpret: {time.time()-t:.1f}s  {sentence!r} -> "
          f"{getattr(result, 'query', None)!r} error={getattr(result, 'error', None)}")

# Planner-style JSON.
t = time.time()
reply = llm.generate('Give JSON {"queries": [three short search queries]} for finding: '
                     'the quote for the new boiler', json_mode=True, max_tokens=120)
try:
    parsed = json.loads(reply.text); ok = isinstance(parsed, dict)
except ValueError:
    ok = False
print(f"json: {time.time()-t:.1f}s parses={ok} -> {reply.text}")
