"""Control: the same Interpret sentences and JSON request through Ollama's qwen2.5:1.5b."""
import json
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm.ollama import OllamaClient  # noqa: E402
from app.search.translate import QueryTranslator  # noqa: E402

client = OllamaClient("http://127.0.0.1:11434", "qwen2.5:1.5b", timeout=300)
print("ollama health:", client.health(), "has model:", client.has_model())
tr = QueryTranslator(client, timeout_s=120, enabled=True)
tr.warm()
for sentence in ("emails from chris about a licence last year",
                 "the photos from the beach holiday in 2019"):
    t = time.time(); r = tr.translate(sentence)
    print(f"[ollama] interpret {time.time()-t:.1f}s {sentence!r} -> {getattr(r, 'query', None)!r}")
t = time.time()
reply = client.generate('Give JSON {"queries": [three short search queries]} for finding: '
                        'the quote for the new boiler', json_mode=True, max_tokens=120)
try:
    ok = isinstance(json.loads(reply.text), dict)
except ValueError:
    ok = False
print(f"[ollama] json {time.time()-t:.1f}s parses={ok} -> {reply.text[:140]!r}")
