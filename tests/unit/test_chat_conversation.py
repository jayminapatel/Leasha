"""Talking to the Chat tab must feel like talking to an AI assistant, and stay Leasha's.

Layer: L8b. The owner's requirement of 2026-09-20 ("the chat has to behave like i am
talking to ai chat like in claude") and his clarification the same day ("the chat should
use local source though"), one test per thing it asks for:

* small talk and instructions about the last answer are answered by the model, streamed,
  with no search and no "not found";
* **a general-sounding question still searches the files first**; only when they have
  nothing does it say so in one plain sentence and offer a labelled general answer;
* the model sees the conversation as role-tagged messages, fitted to its real window;
* a reply that dies part-way keeps what had arrived; Stop stops; Regenerate is warmer;
* the assistant's voice lives in one place and takes the person's own note;
* **nothing here opens a connection to anything but this computer.**

No Ollama is needed: the model is `FakeLLM`, whose conversation calls are recorded.
"""

from __future__ import annotations

import socket

import pytest

from app.chat.config import ChatSettings
from app.chat.engine import NOT_IN_FILES_LEAD, ChatEngine
from app.chat.reconcile import audit_answer
from app.chat.testing import FakeLLM
from app.chat.types import ChatTurn, NarrationEvent, ShelfEvent, SourcesEvent, TokenEvent
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env, ask, converse


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("conversation"))
    yield e
    e.close()


class Spy:
    """Counts how often the archive was searched."""

    def __init__(self, env: Env) -> None:
        self.calls = 0
        self._real = env.search.search
        env.search.search = self._search              # type: ignore[method-assign]
        self._env = env

    def _search(self, *args, **kwargs):
        self.calls += 1
        return self._real(*args, **kwargs)

    def close(self) -> None:
        self._env.search.search = self._real          # type: ignore[method-assign]


@pytest.fixture()
def spy(env):
    s = Spy(env)
    yield s
    s.close()


# =========================================================================== 1. free-form conversation

def test_a_greeting_is_answered_by_the_model_streamed_with_no_search_and_no_refusal(env, spy):
    llm = FakeLLM(chat_replies=["Hello! I can find things in your files, or just talk. What is on your mind?"])
    turn, events = ask(env.engine(llm), "hi")

    assert spy.calls == 0                                             # nothing looked up
    assert turn.kind == "chat" and turn.receipts == [] and turn.result_set is None
    assert turn.text.startswith("Hello!") and "couldn't find" not in turn.text
    kinds = [type(e).__name__ for e in events]
    assert "SourcesEvent" not in kinds and "ShelfEvent" not in kinds  # no sources noise
    streamed = "".join(e.text for e in events if isinstance(e, TokenEvent))
    assert streamed == turn.text and kinds.count("TokenEvent") > 3    # it streamed, in pieces
    assert [e.text for e in events if isinstance(e, NarrationEvent)] == ["Thinking..."]
    assert "first_token_s" in turn.debug["timings"]


@pytest.mark.parametrize("message", [
    "hi", "Hello there!", "thanks!", "thank you so much", "what can you do?", "who are you",
    "how are you?", "ok thanks", "good morning", "bye",
])
def test_social_and_meta_turns_never_touch_the_archive(env, spy, message):
    turn, _events = ask(env.engine(FakeLLM()), message)
    assert turn.kind == "chat" and spy.calls == 0 and turn.debug["route"]["kind"] == "CHAT"


@pytest.mark.parametrize("message", [
    "write a short poem about the sea", "tell me a joke", "what is 12 * 7",
    "what's 15% of 80", "rewrite this so it sounds friendlier: " + "thanks for your email " * 5,
])
def test_writing_and_maths_tasks_that_name_none_of_their_files_need_no_search(env, spy, message):
    turn, _events = ask(env.engine(FakeLLM()), message)
    assert turn.kind == "chat" and spy.calls == 0


@pytest.mark.parametrize("message", [
    "What is a PST file?", "how does version control work", "What is the capital of France?",
])
def test_a_general_sounding_question_still_searches_the_files_first(env, spy, message):
    """Owner, 2026-09-20: the person's own files are the primary basis of every substantive
    answer - including a question that sounds general."""
    turn, _events = ask(env.engine(FakeLLM()), message)
    assert spy.calls >= 1
    assert turn.debug["route"]["kind"] in ("LOOKUP", "FIND", "SYNTHESIS")


def test_when_the_files_have_nothing_it_says_so_in_one_sentence_then_offers_a_labelled_general_answer(env, spy):
    llm = FakeLLM(chat_replies=["A PST file is the file Outlook keeps your mail in."])
    turn, events = ask(env.engine(llm), "What is a PST file?")
    assert spy.calls >= 1
    assert turn.kind == "general" and turn.receipts == []
    first, rest = turn.text.split("\n\n", 1)
    assert first == "I couldn't find that in your files."                # one plain sentence
    assert rest == "**Not from your files:** A PST file is the file Outlook keeps your mail in."
    assert [k for k, _m in llm.chat_calls] == ["general"]
    streamed = "".join(e.text for e in events if isinstance(e, TokenEvent))
    assert streamed == turn.text                                        # the label streams with it
    assert "AI-generated" not in turn.text                              # no banner, ever
    assert NOT_IN_FILES_LEAD.startswith(first)


def test_a_question_about_their_own_affairs_gets_the_searched_account_not_a_general_guess(env, spy):
    llm = FakeLLM()
    turn, _events = ask(env.engine(llm), "What did I agree with my solicitor about the zebra?")
    assert turn.kind == "absence" and "Nothing in Leasha's index matches" in turn.text
    assert [k for k, _m in llm.chat_calls] == []                        # no invented general answer


def test_when_the_files_answer_it_reads_like_an_assistant_and_cites_them(env, spy):
    llm = FakeLLM("extractive")
    turn, events = ask(env.engine(llm), "How much notice must the tenant give?")
    assert turn.kind == "answer" and "two months notice [1]" in turn.text
    assert audit_answer(turn) == []
    assert any(isinstance(e, SourcesEvent) for e in events)
    system = llm.chat_calls[0][1][0]["content"]
    assert "Write flowing prose" in system and "Below are passages from the person's own files" in system
    assert llm.chat_calls[0][1][-1] == {"role": "user", "content": "How much notice must the tenant give?"}


# =========================================================================== 2. real multi-turn memory

def test_the_model_sees_the_conversation_as_role_tagged_messages(env):
    llm = FakeLLM()
    engine = env.engine(llm)
    history: list[ChatTurn] = []
    for message in ("hi", "what can you do?", "thanks"):
        turn, _events = ask(engine, message, history)
        history += [ChatTurn("user", message), turn]
    messages = llm.chat_calls[-1][1]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"] == "hi" and messages[-1]["content"] == "thanks"
    assert messages[2]["content"] == history[1].text                     # what it said is what it sees again


@pytest.mark.parametrize("instruction", [
    "shorter", "translate that to French", "why?", "explain like I'm eight", "continue",
    "make it shorter please", "in bullet points",
])
def test_an_instruction_about_the_last_answer_is_done_from_the_conversation_not_a_search(env, spy, instruction):
    llm = FakeLLM("extractive")
    engine = env.engine(llm)
    first, history = converse(engine, "How much notice must the tenant give?")
    before = spy.calls
    turn, _events = ask(engine, instruction, history)
    assert spy.calls == before                                          # no new search
    assert turn.kind == "chat" and turn.debug["route"]["kind"] == "CHAT"
    kind, messages = llm.chat_calls[-1]
    assert kind == "chat"
    roles = [m["role"] for m in messages]
    assert roles[-3:] == ["user", "assistant", "user"] and messages[-1]["content"] == instruction
    assert "two months notice" in messages[-2]["content"]                 # the answer it must change
    assert "[1]" not in messages[-2]["content"]                          # a stale source number is not shown


def test_a_follow_up_about_the_files_searches_again_with_the_conversation_behind_it(env, spy):
    llm = FakeLLM("extractive")
    engine = env.engine(llm)
    turn, history = converse(engine, "What is the car insurance excess?", "And the annual premium?")
    assert turn.debug["route"]["kind"] == "FOLLOWUP" and turn.kind == "answer"
    kind, messages = llm.chat_calls[-1]
    assert kind == "archive" and [m["role"] for m in messages][-3:] == ["user", "assistant", "user"]
    assert "Question: " in messages[0]["content"]                        # the follow-up, made standalone


def test_the_window_is_the_models_real_context_and_older_turns_go_whole_not_mid_sentence(env):
    llm = FakeLLM(window=1800)
    engine = env.engine(llm)
    history: list[ChatTurn] = []
    for n in range(12):
        history += [ChatTurn("user", f"Tell me about topic number {n}. Please be thorough about it."),
                    ChatTurn("assistant", f"Topic {n} is interesting. " + "It has many parts to explain. " * 6)]
    turn, _events = ask(engine, "thanks", history)
    memory = turn.debug["memory"]
    assert memory["dropped"] > 0 and memory["kept"] > 0
    messages = llm.chat_calls[-1][1]
    assert sum(len(m["content"]) for m in messages) // 3 < 1800            # it fits
    assert messages[-1]["content"] == "thanks"
    system = messages[0]["content"]
    assert "Earlier in this conversation" in system and "The person said: Tell me about topic number 8." in system
    assert "older still not shown" in system                              # and it says when even the digest is cut
    for m in messages[1:-1]:                                            # every kept turn ends whole
        assert m["content"].rstrip().endswith((".", "!", "?", "..."))


# =========================================================================== 4. streaming

def test_stop_ends_a_conversation_reply_promptly_and_keeps_what_arrived(env):
    llm = FakeLLM(chat_replies=["word " * 400], chunk=4)
    seen = {"n": 0}

    def stop() -> bool:
        seen["n"] += 1
        return seen["n"] > 10

    turn, events = ask(env.engine(llm), "hi", should_stop=stop)
    assert turn.kind == "chat" and turn.partial and "stopped" in turn.notes
    assert 0 < len(turn.text) < 400 and seen["n"] < 40


def test_a_reply_that_dies_part_way_keeps_its_partial_text_and_says_why_in_plain_words(env):
    llm = FakeLLM(chat_replies=["The deposit rules are these: " + "and more " * 30], chunk=5, fail_after=6)
    turn, _events = ask(env.engine(llm), "hello there")
    assert turn.partial and turn.kind == "chat"
    assert turn.text.startswith("The deposit rules")                    # what had arrived is kept ...
    assert turn.notes and "Ollama" in turn.notes[-1]                      # ... with one plain sentence and the fix


def test_a_model_that_is_down_says_so_in_one_plain_sentence_with_the_fix(env):
    turn, _events = ask(env.engine(FakeLLM(up=False)), "hi")
    assert turn.kind == "error" and "Ollama is not running" in turn.text and "ollama serve" in turn.text


def test_fast_and_thoughtful_change_how_long_a_thinking_model_may_think(env):
    for style, expected in (("fast", "off"), ("thoughtful", "medium")):
        llm = FakeLLM(thinks=True)
        env.engine(llm).ask("hi", [], lambda _e: None, lambda: False, style=style)
        assert llm.think_asked == [expected], style


# =========================================================================== 6. message actions

def test_regenerate_asks_again_a_little_warmer_so_the_answer_differs(env):
    llm = FakeLLM()
    engine = env.engine(llm)
    for variant in (0, 1, 2):
        engine.ask("hi", [], lambda _e: None, lambda: False, variant=variant)
    assert llm.temperatures == sorted(llm.temperatures) and len(set(llm.temperatures)) == 3
    assert max(llm.temperatures) <= 1.0


def test_a_short_title_comes_from_the_fast_model_and_junk_is_refused(env):
    engine = env.engine(FakeLLM(script=lambda prompt: '"Deposit rules."\nignored'))
    assert engine.title("What did we agree about the deposit?", "Two months rent.") == "Deposit rules"
    long = env.engine(FakeLLM(script=lambda prompt: "This is far too long to be anything like a title at all"))
    assert long.title("q", "a") == ""                                     # the tab falls back to the first words
    assert env.engine(FakeLLM(up=False)).title("q", "a") == ""


# =========================================================================== 7. personality

def test_the_voice_is_in_one_place_and_takes_the_persons_own_note(env):
    from app.chat import prompts

    llm = FakeLLM()
    engine = env.engine(llm, style_note="Keep answers short. I am a beginner.")
    ask(engine, "hi")
    system = llm.chat_calls[0][1][0]["content"]
    assert system.startswith(prompts.PERSONA)                            # the built-in voice ...
    assert "The person also asked: Keep answers short. I am a beginner." in system   # ... plus theirs
    assert 'Never open with "As an AI"' in system
    assert "Never claim to have read, opened or searched a file unless passages" in system


def test_a_style_note_is_a_setting_that_follows_the_tuning_mode():
    assert ChatSettings.from_settings({"chat_style_note": " be brief "}).style_note == "be brief"
    assert ChatSettings.from_settings({"index_tuning_mode": "defaults", "chat_style_note": "x"}).style_note == ""
    assert ChatSettings.from_settings({"index_tuning_mode": "manual", "chat_context_tokens": 99999}).context_tokens == 32768
    assert ChatSettings.from_settings({"chat_context_tokens": 100}).context_tokens == 2048


# =========================================================================== fully local

class _Blocked(AssertionError):
    pass


@pytest.fixture()
def only_this_computer(monkeypatch):
    """Any attempt to open a connection to a host that is not this computer fails the test."""
    seen: list[str] = []
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def local(host: object) -> bool:
        return str(host) in ("127.0.0.1", "localhost", "::1", "", "None")

    def create_connection(address, *args, **kwargs):
        seen.append(str(address[0]))
        if not local(address[0]):
            raise _Blocked(f"the chat path tried to connect to {address[0]!r}")
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        seen.append(str(host))
        if not local(host):
            raise _Blocked(f"the chat path tried to look up {host!r}")
        return real_gai(host, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


def test_a_whole_conversation_never_reaches_beyond_this_computer(env, only_this_computer):
    engine = env.engine(FakeLLM("extractive"))
    history: list[ChatTurn] = []
    for message in ("hi", "How much notice must the tenant give?", "shorter",
                    "What is a PST file?", "Do I have my passport scan?", "how many PDFs do I have?"):
        turn, _events = ask(engine, message, history)
        history += [ChatTurn("user", message), turn]
        assert turn.kind != "error", message
    assert all(host in ("127.0.0.1", "localhost", "::1", "", "None") for host in only_this_computer)


def test_the_real_model_client_only_ever_addresses_localhost(env, only_this_computer):
    """The same turns through `OllamaLLM` itself, with a recording transport in place of the
    network: every URL it would have called is on this computer."""
    from urllib.parse import urlparse

    from app.chat.llm import OllamaLLM
    from app.llm.ollama import OllamaClient

    urls: list[str] = []

    def transport(method, url, payload, timeout):
        urls.append(url)
        return {"models": [{"name": "mistral:latest"}]}

    def stream(url, payload, budget):
        urls.append(url)
        for piece in ("Hello", " there", "!"):
            yield {"message": {"content": piece}, "done": False}
        yield {"done": True}

    client = OllamaClient("http://127.0.0.1:11434", "mistral", transport=transport)
    llm = OllamaLLM(client, stream_transport=stream, show_transport=lambda name: {"model_info": {}})
    turn, _events = ask(env.engine(llm), "hi")
    assert turn.kind == "chat" and turn.text == "Hello there!"
    assert urls and all(urlparse(u).hostname in ("127.0.0.1", "localhost") for u in urls)
    assert any(u.endswith("/api/chat") for u in urls)                     # conversation goes through /api/chat


def test_the_settings_text_for_the_chat_models_says_they_run_on_this_computer():
    from app.core import settings_registry as reg

    for key in ("CHAT_MODEL", "CHAT_ROUTER_MODEL", "CHAT_PLANNER_MODEL"):
        setting = next(s for s in reg.SETTINGS if s.key == key)
        assert "on this computer" in setting.help, key
