r"""The owner's own words: "Mum" means a name only the owner knows.

Layer: L8b

Order `smarter-answers` section 4 (owner, 2026-10-12). Said once ("remember Mum
is Jane Example"), shown back, removable; Chat searches with the meaning in place
of the phrase and tells the model what the phrase meant.
"""

from __future__ import annotations

import pytest

from app.chat import my_words
from app.chat.types import NarrationEvent
from app.storage.sqlite_store import SqliteStore
from tests.unit.chat_env import Env, ask


# -- the pure part -------------------------------------------------------------------


@pytest.mark.parametrize("said, taught", [
    ("remember Mum is Jane Example", ("set", "Mum", "Jane Example")),
    ("Please remember that the house means 12 Example Road.", ("set", "the house", "12 Example Road")),
    ("remember \"my accountant\" = Sam Lee", ("set", "my accountant", "Sam Lee")),
    ("forget Mum", ("forget", "Mum", "")),
    ("forget what the house means", ("forget", "the house", "")),
])
def test_teaching_sentences_are_read(said, taught):
    got = my_words.learn(said)
    assert got is not None and (got.action, got.phrase, got.meaning) == taught


@pytest.mark.parametrize("said", [
    "What did Mum say about the deposit?",
    "remember when the boiler broke and the landlord was away for the whole summer is it",
    "remember Mum is Mum",
    "Mum is Jane Example",                     # no "remember": a statement, not an instruction
])
def test_ordinary_sentences_teach_nothing(said):
    assert my_words.learn(said) is None


def test_the_meaning_takes_the_phrases_place_longest_first():
    words = [("Mum", "Jane Example"), ("Mum's house", "12 Example Road")]
    searched, used = my_words.apply("Did Mum's house flood? Ask mum.", words)

    assert searched == "Did 12 Example Road flood? Ask Jane Example."
    assert used == [("Mum's house", "12 Example Road"), ("Mum", "Jane Example")]


def test_a_phrase_inside_another_word_is_left_alone():
    searched, used = my_words.apply("Mumbai summit and chrysanthemum", [("Mum", "Jane")])
    assert searched == "Mumbai summit and chrysanthemum" and used == []


def test_the_model_is_told_what_was_meant():
    assert my_words.note_for_model([("Mum", "Jane Example")]) == \
        'In the person\'s question, "Mum" means Jane Example.'
    assert my_words.note_for_model([]) == ""


# -- the store ---------------------------------------------------------------------------


def test_words_are_kept_changed_and_forgotten(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        store.set_my_word("Mum", "Jane Example")
        store.set_my_word("mum", "Jane Q Example", source="chat")   # same phrase, any case
        assert store.my_words() == [("mum", "Jane Q Example", "chat")]
        assert store.remove_my_word("MUM") is True
        assert store.remove_my_word("Mum") is False
        with pytest.raises(ValueError):
            store.set_my_word("Mum", "  ")


def test_a_reset_keeps_the_owners_words(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        store.set_my_word("Mum", "Jane Example")
        store.clear_index()
        assert store.my_words() == [("Mum", "Jane Example", "settings")]


# -- Chat -----------------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_chat_learns_says_it_back_and_forgets(env):
    engine = env.engine()

    turn, _ = ask(engine, "remember my landlord is Margaret Okafor")
    assert turn.kind == "chat" and '"my landlord" means Margaret Okafor' in turn.text
    assert env.store.my_words() == [("my landlord", "Margaret Okafor", "chat")]

    turn, _ = ask(engine, "forget my landlord")
    assert "forgotten" in turn.text and env.store.my_words() == []


def test_chat_searches_for_the_meaning_and_tells_the_model(env):
    env.store.set_my_word("the flat", "tenancy deposit")
    from app.chat.testing import FakeLLM
    llm = FakeLLM("extractive")
    turn, events = ask(env.engine(llm), "What did we agree with the landlord about the flat?")

    narration = " ".join(e.text for e in events if isinstance(e, NarrationEvent))
    assert '"the flat" as tenancy deposit' in narration
    assert turn.debug["my_words"] == [("the flat", "tenancy deposit")]
    assert "deposit" in turn.debug["plan"]["terms"]
    assert any('"the flat" means tenancy deposit' in str(prompt)
               for _kind, prompt in llm.calls + list(getattr(llm, "chat_calls", [])))
