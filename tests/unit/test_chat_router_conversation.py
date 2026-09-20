"""The router's conversation class: narrow on purpose, retrieval first.

Layer: L8b (no Qt, no model, no store). Owner, 2026-09-20: "the chat should use local source though" -
so **only** social or meta turns, instructions about the previous answer, and writing/maths/code tasks
that name none of the person's files skip the archive. Everything else - including a general-sounding
question - is searched first.
"""

from __future__ import annotations

import pytest

from app.chat.router import CHAT, CLASSES, FOLLOWUP, chat_reason, route_question
from app.chat.types import ChatTurn

BEFORE = [ChatTurn("user", "What did we agree with the landlord about the deposit?"),
          ChatTurn("assistant", "The deposit is two months rent [1].")]


def kind(text, history=()):
    return route_question(text, list(history)).effective


@pytest.mark.parametrize("text", [
    "hi", "Hi!", "hello", "Hello there", "hey", "good morning", "Good evening!", "howdy",
    "thanks", "Thanks!", "thank you", "thank you so much", "cheers", "ok thanks", "great, thanks",
    "bye", "goodbye", "see you later", "that's all",
    "how are you", "How are you?", "who are you", "what are you", "what's your name",
    "what can you do", "What can you do?", "what do you do", "how do you work", "can you help me",
    "help", "what should I ask you", "are you there",
])
def test_social_and_meta_turns_are_conversation(text):
    assert kind(text) == CHAT and kind(text, BEFORE) == CHAT


@pytest.mark.parametrize("text", [
    "shorter", "Shorter please", "make it shorter", "make that a bit shorter", "longer", "more detail",
    "simpler", "simplify that", "explain that again", "explain it simpler", "explain like I'm eight",
    "eli5", "explain it like I am five", "translate that to French", "translate it into Spanish", "in French",
    "in bullet points", "as a table", "continue", "go on", "keep going", "carry on", "why?", "why is that",
    "how so", "are you sure", "what do you mean", "rephrase that", "say that again", "say it differently",
    "give me an example", "another one", "tl;dr", "summarise that", "no, I meant the second one",
])
def test_an_instruction_about_the_previous_answer_is_conversation(text):
    assert kind(text, BEFORE) == CHAT
    assert route_question(text, BEFORE).by == "rules"


@pytest.mark.parametrize("text", [
    "write a short poem about the sea", "write me a limerick", "tell me a joke", "give me a joke",
    "compose a haiku about autumn", "brainstorm names for a bakery", "come up with a slogan for tea",
    "what is 12 * 7", "what's 15% of 80", "12 * 7", "calculate 45 + 17",
    "translate hello to French", "translate good morning into German",
    "rewrite this so it sounds friendlier: " + "Thanks for your email and for the report. " * 3,
    "proofread the following: " + "This are a sentense with mistaks in it, please fix them all. " * 2,
    "what does this code do?\n```python\nprint(1)\n```",
])
def test_writing_maths_and_code_that_name_none_of_their_files_need_no_search(text):
    assert kind(text) == CHAT


@pytest.mark.parametrize("text", [
    # A general-sounding question STILL searches first (owner's clarification)
    "What is a PST file?", "what is the capital of France?", "How does version control work?",
    "why is the sky blue", "explain what a mortgage is", "how do I convert a PST to mbox",
    "what is a purchase order",
    # about the person's own files and mail
    "What did we agree with the landlord about the deposit?", "how many PDFs did Dave send in 2019",
    "show me the photos from the beach in 2015", "do I have anything from the solicitor?",
    "summarise everything about the Leeds audit", "find the tenancy agreement",
    "who is my landlord", "when does the tenancy start",
    # a writing task that reaches for their files searches them
    "write an email to my landlord about the deposit", "draft a reply to the email from Dave",
    "write a summary of my documents about the audit", "summarise my emails from last week",
])
def test_everything_else_searches_first(text):
    assert kind(text) != CHAT, route_question(text).explain()
    assert kind(text, BEFORE) != CHAT


def test_a_follow_up_about_the_files_is_still_a_follow_up_not_conversation():
    for text in ("and the second one?", "and what about 2019?", "what about my tax return?"):
        route = route_question(text, BEFORE)
        assert route.kind == FOLLOWUP and route.effective != CHAT


def test_instructions_are_conversation_even_with_no_earlier_answer_the_model_says_there_is_nothing_yet():
    assert kind("shorter") == CHAT and kind("why?") == CHAT and kind("continue") == CHAT


def test_a_greeting_followed_by_a_real_question_is_a_question():
    assert kind("hi, what did we agree about the deposit?") != CHAT
    assert kind("thanks - and how much was the deposit?") != CHAT
    assert kind("hello there! how many PDFs did Dave send in 2019") == "AGGREGATE"   # the hello does not hide the count


def test_a_greeting_followed_by_a_task_is_the_task():
    assert kind("hi, tell me a joke") == CHAT and kind("hey - write a short poem about rain") == CHAT


@pytest.mark.parametrize("text", [
    "lol", "haha", "wow", "hmm", "oh no", "sorry", "never mind", "nice one", "no worries",
    "I'm bored", "i am so tired", "that's funny", "that's really interesting", "you're right", "I love it",
    "I feel stuck",
])
def test_reactions_and_feelings_are_talk_not_a_search(text):
    assert kind(text) == CHAT and route_question(text).by == "rules"


@pytest.mark.parametrize("text", [
    "I'm looking for the tenancy agreement", "I need the tax return", "i think the deposit was 900",
    "I want the photos from the beach", "I'm after the boiler quote",
])
def test_a_first_person_sentence_that_wants_something_is_still_searched(text):
    assert kind(text) != CHAT


def test_the_decision_is_explained_in_plain_words():
    route = route_question("thanks!", BEFORE)
    assert route.kind == CHAT and route.by == "rules"
    assert route.explain().startswith("Routed as CHAT by rule:")
    assert chat_reason("hello", []) and chat_reason("What is a PST file?", []) is None


def test_chat_is_one_of_the_classes_and_the_router_model_is_never_asked_to_pick_it():
    from app.chat import router

    assert CHAT in CLASSES
    assert "CHAT" not in router._ROUTER_PROMPT and "conversation" not in router._ROUTER_PROMPT.lower()
    seen = []

    class Model:
        def generate(self, prompt, **kw):
            seen.append(prompt)
            return "CHAT"                                # even if a small model says it

    assert route_question("kids at the beach 2015 holiday photos album", [], llm=Model()).kind != CHAT
