"""What the model remembers of the conversation, fitted to what it can read.

Layer: L8b (pure text). Owner requirement 2026-09-20, point 2: real multi-turn memory - the
model sees the conversation as role-tagged messages, a sliding window sized from its real
context, older turns trimmed by budget and **never cut mid-sentence**.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.chat import memory
from app.chat.memory import clip_to_sentences, estimate_tokens, pack, strip_markers, turn_text
from app.chat.types import ChatTurn


def U(text: str) -> ChatTurn:
    return ChatTurn("user", text)


def A(text: str, **kw) -> ChatTurn:
    return ChatTurn("assistant", text, **kw)


def roles(packed) -> list[str]:
    return [m["role"] for m in packed.messages]


def test_the_conversation_is_role_tagged_with_the_system_first_and_the_question_last():
    packed = pack("SYSTEM", [U("hi"), A("Hello!"), U("what can you do?"), A("Lots.")], "shorter",
                  window_tokens=4096)
    assert roles(packed) == ["system", "user", "assistant", "user", "assistant", "user"]
    assert packed.messages[0]["content"] == "SYSTEM" and packed.messages[-1]["content"] == "shorter"
    assert packed.dropped == 0 and packed.kept == 4 and not packed.digest


def test_an_old_answers_source_numbers_are_not_shown_to_the_next_answer():
    """Each answer numbers its own sources; a stale [1] would be cited again."""
    packed = pack("S", [U("q"), A("The deposit is 950 pounds [1][2]. It is held in a scheme [2].")], "and?",
                  window_tokens=4096)
    assert packed.messages[2]["content"] == "The deposit is 950 pounds. It is held in a scheme."
    assert strip_markers("Yes [1, 2].") == "Yes."


def test_a_listing_turn_tells_the_model_which_files_it_showed():
    rows = [SimpleNamespace(path="C:/Photos/beach-1.jpg"), SimpleNamespace(path="C:/Photos/beach-2.jpg")]
    found = A("Here are the 2 documents matching the beach photos.", kind="find", result_set=rows)
    packed = pack("S", [U("show me the beach photos"), found], "open the second one", window_tokens=4096)
    assert "beach-1.jpg; beach-2.jpg" in packed.messages[2]["content"]      # "the second one" has a referent


def test_a_failed_turn_takes_the_question_it_did_not_answer_with_it():
    history = [U("first"), A("Fine."), U("second"), A("Ollama is down.", kind="error"), U("third"), A("Ok.")]
    packed = pack("S", history, "fourth", window_tokens=4096)
    texts = [m["content"] for m in packed.messages[1:]]
    assert texts == ["first", "Fine.", "third", "Ok.", "fourth"]              # never two questions in a row
    assert roles(packed) == ["system", "user", "assistant", "user", "assistant", "user"]


def test_consecutive_messages_from_one_side_are_joined_and_an_answer_never_leads():
    packed = pack("S", [A("orphan"), U("a"), U("b"), A("c")], "d", window_tokens=4096)
    assert [m["content"] for m in packed.messages[1:]] == ["a\n\nb", "c", "d"]


def test_an_empty_history_is_just_the_system_and_the_question():
    packed = pack("S", [], "hello", window_tokens=2048)
    assert roles(packed) == ["system", "user"] and packed.kept == 0


# --------------------------------------------------------------------------- the window

def _long_talk(turns: int = 14) -> list[ChatTurn]:
    out: list[ChatTurn] = []
    for n in range(turns):
        out += [U(f"Tell me about topic number {n}. Please be thorough about it."),
                A(f"Topic {n} is interesting. " + "It has many parts to explain. " * 8)]
    return out


def test_the_window_is_respected_and_the_newest_turns_are_the_ones_kept():
    packed = pack("S" * 900, _long_talk(), "thanks", window_tokens=1800, reserve_tokens=400)
    assert packed.tokens <= 1800 - 400
    assert packed.dropped > 0 and packed.kept > 0
    assert "Topic 13 is interesting." in packed.messages[-2]["content"]       # the newest survives
    assert packed.messages[-1]["content"] == "thanks"


def test_a_bigger_window_remembers_more():
    small = pack("S", _long_talk(), "x", window_tokens=1500, reserve_tokens=300)
    large = pack("S", _long_talk(), "x", window_tokens=6000, reserve_tokens=300)
    assert large.kept > small.kept and large.dropped < small.dropped


def test_dropped_turns_are_summarised_not_silently_lost():
    packed = pack("S", _long_talk(), "x", window_tokens=1800, reserve_tokens=400)
    system = packed.messages[0]["content"]
    assert system.startswith("S\n\nEarlier in this conversation")
    assert "- The person said: Tell me about topic number" in system
    assert "- You replied: Topic" in system
    assert packed.digest and packed.as_dict()["digest"] is True


def test_the_digest_is_bounded_and_says_when_even_it_is_cut():
    packed = pack("S", _long_talk(60), "x", window_tokens=2000, reserve_tokens=400)
    assert estimate_tokens(packed.digest) <= memory.DIGEST_MAX_TOKENS + 40
    assert "older still not shown" in packed.digest


def test_the_turn_at_the_edge_is_cut_at_a_sentence_never_mid_sentence():
    one_long = A("First point is made here. Second point is made here. Third point is made here. "
                 "Fourth point is made here. Fifth point is made here. Sixth point is made here.")
    packed = pack("S" * 300, [U("go"), one_long], "and?", window_tokens=700, reserve_tokens=150)
    kept = [m["content"] for m in packed.messages[1:-1] if m["role"] == "assistant"]
    assert kept, "the cut turn is still there"
    for text in kept:
        assert text.rstrip().endswith((".", "...")), text
    assert not any(text.endswith(("Thir", "Fou", "Fif")) for text in kept)


def test_a_tiny_window_still_returns_a_usable_conversation():
    packed = pack("S" * 3000, _long_talk(), "hello", window_tokens=1024, reserve_tokens=700)
    assert packed.messages[0]["role"] == "system" and packed.messages[-1]["content"] == "hello"
    assert roles(packed)[1:2] in (["user"], [])                              # and it never starts on an answer


def test_clip_to_sentences_keeps_whole_sentences_from_the_start_or_the_end():
    text = "Alpha is first. Beta is second. Gamma is third. Delta is fourth."
    head = clip_to_sentences(text, 12)
    assert head.startswith("Alpha") and head.endswith(".") and "Delta" not in head
    tail = clip_to_sentences(text, 12, keep="end")
    assert tail.startswith("... ") and tail.endswith("Delta is fourth.") and "Alpha" not in tail
    assert clip_to_sentences(text, 500) == text
    cut = clip_to_sentences("word " * 200, 20)                                # no sentence at all: a word boundary
    assert cut.endswith("...") and len(cut) < 120 and not cut.rstrip(". ").endswith("wor")


def test_turn_text_hides_errors_and_blanks():
    assert turn_text(A("boom", kind="error")) == "" and turn_text(A("   ")) == ""
    assert turn_text(U("hi")) == "hi"
