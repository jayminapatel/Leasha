r"""The model's real context window, for §1c's context budget.

Layer: L8b (consumer); the method itself lives in `app/llm/ollama.py`.

`_get` is monkeypatched rather than a real socket - the same seam
`test_ui_responsiveness.py` tests `_budget` through, one level up.
"""

from __future__ import annotations

from app.llm.ollama import OllamaClient

TAGS_PAYLOAD = {
    "models": [
        {"name": "mistral:latest", "details": {"context_length": 32768}},
        {"name": "qwen2.5:1.5b", "details": {"context_length": 32768}},
        {"name": "llama3:latest", "details": {"context_length": 8192}},
    ]
}


def _client(model: str, payload=TAGS_PAYLOAD, *, raises=None) -> OllamaClient:
    client = OllamaClient(model=model)
    if raises is not None:
        def broken(path, timeout):
            raise raises
        client._get = broken
    else:
        client._get = lambda path, timeout: payload
    return client


def test_the_exact_tag_is_matched():
    assert _client("mistral:latest").context_length() == 32768


def test_the_bare_name_matches_the_full_tag():
    """`mistral` and `mistral:latest` are the same model - the same rule
    `has_model()` already applies, mirrored here so the two never
    disagree about which entry answers for one configured name."""
    assert _client("mistral").context_length() == 32768


def test_a_model_not_in_the_list_returns_none():
    assert _client("nonexistent:model").context_length() is None


def test_an_unreachable_server_returns_none_rather_than_raising():
    assert _client("mistral", raises=RuntimeError("connection refused")).context_length() is None


def test_a_missing_context_length_field_returns_none():
    """An older Ollama server, or a model whose details omit it."""
    payload = {"models": [{"name": "mistral:latest", "details": {}}]}
    assert _client("mistral:latest", payload).context_length() is None


def test_a_non_integer_context_length_is_ignored():
    payload = {"models": [{"name": "mistral:latest",
                           "details": {"context_length": "a lot"}}]}
    assert _client("mistral:latest", payload).context_length() is None


def test_different_models_report_their_own_length():
    assert _client("llama3:latest").context_length() == 8192
    assert _client("qwen2.5:1.5b").context_length() == 32768
