"""The Chat tab's pins, removals and speed reach the REAL engine.

Layer: L8b. Work order `202626270611-chat-tab` sections 3a, 3c and 3d.

**Why this file exists.** The tab's Qt tests run against `FakeChatEngine`, and the
real `ChatEngine.ask` had no `scope` or `style` parameter: `supported_kwargs` quietly
dropped both, so pinning a document, removing one and choosing Fast or Thoughtful did
nothing at all while every test stayed green. These tests put the real engine, a real
store and a real search behind the same calls and assert that what the tab hands over
changes what is retrieved and which model answers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat.config import ChatSettings
from app.chat.engine import ChatEngine
from app.chat.testing import FakeLLM
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env

INSURANCE = "C:/Archive/Insurance/insurance-renewal.pdf"
TENANCY = "C:/Archive/Tenancy/tenancy-agreement-2024.pdf"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("wiring"))
    yield e
    e.close()


def _ask(engine, question, **kwargs):
    events: list = []
    turn = engine.ask(question, [], events.append, lambda: False, **kwargs)
    return turn, events


def _path_of(env, name):
    row = env.store.conn.execute("SELECT path FROM files WHERE path LIKE ?", (f"%{name}",)).fetchone()
    assert row, f"the fixture has no {name}"
    return row[0]


# =========================================================================== scope (the pins)

def test_a_pinned_document_is_looked_at_first_for_a_question_that_is_not_a_follow_up(env):
    """Without a pin the corpus is searched; with the document pinned, its own
    passages answer and no corpus search is run at all."""
    question = "What is the car insurance excess?"
    plain, _ = _ask(env.engine("extractive"), question)
    assert plain.debug["queries"], "the unpinned question must search the corpus"

    path = _path_of(env, "insurance-renewal.pdf")
    pinned, _ = _ask(env.engine("extractive"), question, scope=[path])
    assert pinned.debug.get("scope") == "shelf" and pinned.debug["queries"] == []
    assert pinned.receipts and pinned.receipts[0].name == "insurance-renewal.pdf"


def test_a_pin_that_does_not_answer_falls_back_to_the_corpus(env):
    """"Look here first" - not "only here". The pinned insurance document says
    nothing about a tenant's notice period, so the tenancy agreement still answers."""
    path = _path_of(env, "insurance-renewal.pdf")
    turn, _ = _ask(env.engine("extractive"), "How much notice must the tenant give?", scope=[path])
    assert turn.debug["queries"], "the shelf could not answer, so the corpus was searched"
    assert any(r.name == "tenancy-agreement-2024.pdf" for r in turn.receipts)


def test_scope_belongs_to_one_question_and_does_not_leak_into_the_next(env):
    engine = env.engine("extractive")
    path = _path_of(env, "insurance-renewal.pdf")
    _ask(engine, "What is the car insurance excess?", scope=[path])
    later, _ = _ask(engine, "What is the car insurance excess?")            # no scope this time
    assert later.debug["queries"], "the earlier pin was still in force"


def test_a_path_the_index_does_not_know_is_ignored_not_fatal(env):
    turn, _ = _ask(env.engine("extractive"), "What is the car insurance excess?",
                   scope=["C:/nowhere/at-all.pdf"], removed=["C:/also/gone.pdf"])
    assert turn.kind in ("answer", "absence") and turn.receipts


# =========================================================================== removed

def test_a_document_taken_off_the_shelf_is_never_used_as_a_source(env):
    path = _path_of(env, "tenancy-agreement-2024.pdf")
    turn, _ = _ask(env.engine("extractive"), "How much notice must the tenant give?", removed=[path])
    assert all(r.name != "tenancy-agreement-2024.pdf" for r in turn.receipts)


def test_removing_and_pinning_the_same_document_leaves_it_out(env):
    """The person took it off after pinning it: out of scope means out of scope."""
    path = _path_of(env, "insurance-renewal.pdf")
    turn, _ = _ask(env.engine("extractive"), "What is the car insurance excess?",
                   scope=[path], removed=[path])
    assert turn.debug.get("scope") != "shelf"
    assert all(r.name != "insurance-renewal.pdf" for r in turn.receipts)


# =========================================================================== style (Fast / Thoughtful)

def _real_roles_engine(env, monkeypatch, installed, **settings):
    """The engine with roles resolved from a list of installed models, and every
    model client a fake that records its name - Ollama itself is not needed to
    ask which model *would* answer."""
    engine = ChatEngine(env.search, env.store, None, ChatSettings(today=fx.TODAY, **settings))
    monkeypatch.setattr(engine, "installed_models", lambda: list(installed))

    def client(name):
        fake = FakeLLM("extractive")
        fake.model = name
        return fake

    monkeypatch.setattr(engine, "_client_for", client)
    return engine


def test_fast_and_thoughtful_choose_different_models_when_two_are_installed(env, monkeypatch):
    installed = ["llama3.2:1b", "llama3.1:8b"]
    engine = _real_roles_engine(env, monkeypatch, installed)
    modes = engine.suggest_modes()
    assert modes["fast"] != modes["thoughtful"], "the fixture must offer a real choice"

    fast, _ = _ask(engine, "How much notice must the tenant give?", style="fast")
    assert fast.debug["style"] == {"style": "fast", "model": modes["fast"]}
    assert engine.roles().answerer == modes["fast"]

    thoughtful, _ = _ask(engine, "How much notice must the tenant give?", style="thoughtful")
    assert thoughtful.debug["style"] == {"style": "thoughtful", "model": modes["thoughtful"]}
    assert engine.roles().answerer == modes["thoughtful"]


def test_an_explicit_chat_model_wins_and_the_turn_says_so(env, monkeypatch):
    engine = _real_roles_engine(env, monkeypatch, ["llama3.2:1b", "llama3.1:8b"],
                                answer_model="llama3.1:8b")
    turn, _ = _ask(engine, "How much notice must the tenant give?", style="fast")
    assert "ignored" in turn.debug["style"] and "CHAT_MODEL" in turn.debug["style"]["ignored"]
    assert engine.roles().answerer == "llama3.1:8b"


def test_no_style_leaves_the_models_alone(env, monkeypatch):
    engine = _real_roles_engine(env, monkeypatch, ["llama3.2:1b", "llama3.1:8b"])
    before = engine.roles().answerer
    turn, _ = _ask(engine, "How much notice must the tenant give?")
    assert "style" not in turn.debug and engine.roles().answerer == before


def test_a_fixed_test_model_is_not_swapped_by_a_style(env):
    turn, _ = _ask(env.engine("extractive"), "How much notice must the tenant give?", style="thoughtful")
    assert "style" not in turn.debug


def test_the_real_ask_accepts_what_the_tab_hands_it():
    """The regression that hid all of this: `supported_kwargs` drops a keyword the
    engine does not declare. Declared now, so it is kept."""
    from app.ui.controllers.chat_controller import supported_kwargs

    kept = supported_kwargs(SimpleNamespace(ask=ChatEngine.ask.__get__(object())),
                            scope=["a"], removed=["b"], style="fast", nonsense=1)
    assert set(kept) == {"scope", "removed", "style"}
