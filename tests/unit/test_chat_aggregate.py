r"""L8b §1d: "AGGREGATE answers are computed, not generated."

Layer: L8b

**No test here needs a running Ollama** - same rule every other chat module
holds itself to. The count itself is proved against a real `SqliteStore`
(see `test_aggregate_count.py` for the SQL correctness); these tests are
about the answer wrapped around it.
"""

from __future__ import annotations

import pytest

from app.chat.aggregate import (
    AggregateAnswer,
    answer_aggregate,
    build_phrasing_prompt,
)
from app.storage.sqlite_store import SqliteStore


class FakeClient:
    def __init__(self, reply: str = "", *, healthy: bool = True, raises: BaseException = None):
        self.reply = reply
        self.healthy = healthy
        self.raises = raises
        self.calls = 0

    def health(self, *, force: bool = False) -> bool:
        return self.healthy

    def has_model(self) -> bool:
        return self.healthy

    def generate(self, prompt: str, **_kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises

        class Response:
            text = self.reply

        return Response()


class FakeTranslator:
    def __init__(self, query: str):
        self.query = query
        self.calls: list[str] = []

    def translate(self, sentence: str, store=None):
        self.calls.append(sentence)

        class _T:
            query = self.query

        return _T()


def _store(tmp_path, *, files=3):
    store = SqliteStore(tmp_path / "index.db").connect()
    for n in range(files):
        file_id = store.upsert_file(
            path=rf"C:\work\invoice{n}.pdf", size_bytes=10, mtime_ns=0, ext="pdf")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "an invoice"}])
        store.mark_indexed(file_id)
    return store


# ---------------------------------------------------------------------------
# The count itself reaches the answer
# ---------------------------------------------------------------------------

def test_the_computed_count_is_correct(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        answer = answer_aggregate("how many invoices are there", store=store)
        assert answer.count == 3
    finally:
        store.close()


def test_no_client_gives_the_plain_template(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        answer = answer_aggregate("how many invoices", store=store)
        assert answer.sentence == "3 matching documents."
        assert not answer.used_model
    finally:
        store.close()


def test_the_plain_template_uses_the_singular_correctly(tmp_path):
    store = _store(tmp_path, files=1)
    try:
        answer = answer_aggregate("how many invoices", store=store)
        assert answer.sentence == "1 matching document."
    finally:
        store.close()


def test_zero_matches_is_a_real_sentence_not_an_error(tmp_path):
    store = _store(tmp_path, files=0)
    try:
        answer = answer_aggregate("how many spaceships", store=store)
        assert answer.count == 0
        assert answer.sentence == "0 matching documents."
    finally:
        store.close()


def test_a_translator_is_used_to_build_the_query(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        translator = FakeTranslator("type:pdf")
        answer = answer_aggregate("how many pdfs do I have",
                                  store=store, translator=translator)
        assert answer.query == "type:pdf"
        assert translator.calls == ["how many pdfs do I have"]
        assert answer.count == 3
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Model phrasing - validated against the count it was handed
# ---------------------------------------------------------------------------

def test_a_valid_phrasing_is_used(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient("You have 3 invoices in your archive.")
        answer = answer_aggregate("how many invoices", store=store, client=client)
        assert answer.sentence == "You have 3 invoices in your archive."
        assert answer.used_model
    finally:
        store.close()


def test_a_wrong_number_is_rejected_in_favour_of_the_template(tmp_path):
    r"""**The load-bearing case.** A model stating a different number,
    however fluent the sentence, must never reach the screen."""
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient("You have 5 invoices in your archive.")
        answer = answer_aggregate("how many invoices", store=store, client=client)
        assert answer.sentence == "3 matching documents."
        assert not answer.used_model
    finally:
        store.close()


def test_a_second_number_nearby_is_rejected_even_if_the_real_one_is_present(tmp_path):
    """"3 invoices, or maybe around 5" states two numbers - which one is the
    answer is exactly the ambiguity this function exists to never allow onto
    the screen."""
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient("You have 3 invoices, or possibly closer to 5.")
        answer = answer_aggregate("how many invoices", store=store, client=client)
        assert not answer.used_model
        assert answer.sentence == "3 matching documents."
    finally:
        store.close()


def test_no_number_at_all_is_rejected(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient("You have several invoices on file.")
        answer = answer_aggregate("how many invoices", store=store, client=client)
        assert not answer.used_model
    finally:
        store.close()


def test_a_raising_client_falls_back_to_the_template(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient(raises=RuntimeError("boom"))
        answer = answer_aggregate("how many invoices", store=store, client=client)
        assert answer.sentence == "3 matching documents."
    finally:
        store.close()


def test_an_unhealthy_client_is_never_asked(tmp_path):
    store = _store(tmp_path, files=3)
    try:
        client = FakeClient("3 invoices.", healthy=False)
        answer_aggregate("how many invoices", store=store, client=client)
        assert client.calls == 0
    finally:
        store.close()


def test_a_number_with_a_thousands_separator_is_recognised(tmp_path):
    """`count_matching` can return a number in the thousands - the model may
    write it back with a comma, and that must still validate."""
    from app.chat.aggregate import _validate_phrasing

    assert _validate_phrasing("You have 1,234 documents.", 1234) == "You have 1,234 documents."


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_states_the_number_and_the_question():
    prompt = build_phrasing_prompt("how many invoices", 7)
    assert "7" in prompt
    assert "how many invoices" in prompt


def test_aggregate_answer_is_a_real_dataclass():
    answer = AggregateAnswer(question="q", query="q", count=0, sentence="0 matching documents.")
    assert answer.used_model is False
