r"""Describe: an on-demand, richer photo caption from a vision-capable Ollama
model. Work order 0i (`202626270511`) section 3.

Layer: L2/L5

`app/llm/ollama.py`'s own contract (never raise anything but
`AppErrorException(ERR_OLLAMA_DOWN/ERR_OLLAMA_TIMEOUT)`, never block past its
timeout) is proved in `test_translate_timeout.py`; this file proves the
different thing section 3 adds on top of it - `describe_image` never raises
at all (a `None` return is the worst case a worker sees), `available()`/
`unavailable_reason()` answer the two questions a greyed button needs, and
the `images` field actually reaches the transport `OllamaClient.generate`
calls.
"""

from __future__ import annotations

import base64

from app.extract import vision_caption
from app.llm.ollama import OllamaClient


class _FakeClient:
    """Duck-types `OllamaClient` - the pattern `test_translate_timeout.py`
    already uses for the same reason: `available()`/`describe_image` only
    ever call three methods, so a fake need only offer those three."""

    def __init__(self, *, healthy=True, has_model=True, text="A dog on a beach.",
                 raises=None):
        self.url = "http://127.0.0.1:11434"
        self.model = "llava"
        self._healthy = healthy
        self._has_model = has_model
        self._text = text
        self._raises = raises
        self.calls: list[dict] = []

    def health(self, *, force: bool = False) -> bool:
        return self._healthy

    def has_model(self) -> bool:
        return self._has_model

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        from app.llm.ollama import OllamaResponse
        return OllamaResponse(text=self._text, model=self.model, elapsed_s=0.1)


# ---------------------------------------------------------------------------
# available() / unavailable_reason()
# ---------------------------------------------------------------------------

def test_available_true_when_healthy_and_model_installed():
    assert vision_caption.available(_FakeClient()) is True


def test_available_false_when_ollama_is_down():
    assert vision_caption.available(_FakeClient(healthy=False)) is False


def test_available_false_when_model_not_installed():
    assert vision_caption.available(_FakeClient(has_model=False)) is False


def test_available_never_raises_on_a_broken_client():
    class Explodes:
        def health(self, *, force=False):
            raise RuntimeError("boom")

    assert vision_caption.available(Explodes()) is False


def test_unavailable_reason_names_ollama_down():
    reason = vision_caption.unavailable_reason(_FakeClient(healthy=False))
    assert "not running" in reason


def test_unavailable_reason_names_the_missing_model_and_the_fix():
    reason = vision_caption.unavailable_reason(_FakeClient(has_model=False))
    assert "llava" in reason
    assert "ollama pull" in reason


# ---------------------------------------------------------------------------
# describe_image()
# ---------------------------------------------------------------------------

def test_describe_image_returns_the_caption(tmp_path):
    from PIL import Image

    photo = tmp_path / "beach.jpg"
    Image.new("RGB", (8, 8), "blue").save(photo)

    client = _FakeClient(text="A dog runs on a sandy beach.")
    result = vision_caption.describe_image(photo, client)

    assert result is not None
    assert result.caption == "A dog runs on a sandy beach."
    assert result.model == "llava"
    assert result.elapsed_s >= 0


def test_describe_image_sends_the_photo_as_base64(tmp_path):
    from PIL import Image

    photo = tmp_path / "beach.jpg"
    Image.new("RGB", (8, 8), "blue").save(photo)
    raw = photo.read_bytes()

    client = _FakeClient()
    vision_caption.describe_image(photo, client)

    assert len(client.calls) == 1
    sent = client.calls[0]["images"]
    assert sent == [base64.b64encode(raw).decode("ascii")]


def test_describe_image_returns_none_for_a_missing_file(tmp_path):
    client = _FakeClient()
    assert vision_caption.describe_image(tmp_path / "does-not-exist.jpg", client) is None
    assert client.calls == []                    # never even asked


def test_describe_image_returns_none_rather_than_raising_when_ollama_fails(tmp_path):
    from PIL import Image
    from app.core.errors import AppErrorException, make_error

    photo = tmp_path / "beach.jpg"
    Image.new("RGB", (8, 8), "blue").save(photo)

    error = make_error("ERR_OLLAMA_DOWN", "llm.ollama", details="down")
    client = _FakeClient(raises=AppErrorException(error))

    assert vision_caption.describe_image(photo, client) is None


def test_describe_image_returns_none_for_an_empty_reply(tmp_path):
    from PIL import Image

    photo = tmp_path / "beach.jpg"
    Image.new("RGB", (8, 8), "blue").save(photo)

    client = _FakeClient(text="   ")
    assert vision_caption.describe_image(photo, client) is None


# ---------------------------------------------------------------------------
# The transport itself: `images` reaches the wire.
# ---------------------------------------------------------------------------

def test_ollama_client_generate_puts_images_in_the_payload():
    seen = {}

    def transport(method, url, payload, timeout):
        if method == "GET":
            return {"models": [{"name": "llava:latest"}]}
        seen.update(payload)
        return {"response": "A photo.", "model": "llava"}

    client = OllamaClient(model="llava", transport=transport)
    client.generate("describe this", images=["QUJD"])

    assert seen.get("images") == ["QUJD"]


def test_ollama_client_generate_omits_images_when_none_given():
    seen = {}

    def transport(method, url, payload, timeout):
        if method == "GET":
            return {"models": [{"name": "mistral:latest"}]}
        seen.update(payload)
        return {"response": "ok", "model": "mistral"}

    client = OllamaClient(model="mistral", transport=transport)
    client.generate("hello")

    assert "images" not in seen


# ---------------------------------------------------------------------------
# The label constant, and why it must not collide with Florence's own.
# ---------------------------------------------------------------------------

def test_ai_caption_label_is_distinct_from_florence_description():
    assert vision_caption.AI_CAPTION_LABEL == "AI caption"
    assert vision_caption.AI_CAPTION_LABEL != "AI description"
