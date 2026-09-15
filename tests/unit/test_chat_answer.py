r"""Generate a cited answer, verify it, retry once if it thins too far.

Layer: L8b

**No test here needs a running Ollama.** Client and embedder are both
fakes, the same discipline every other chat module holds itself to - this
is about the orchestration (size, generate, verify, maybe retry), not about
proving a real model can write a good answer.
"""

from __future__ import annotations

from app.chat.answer import (
    THIN_DROP_RATIO,
    Answer,
    answer_question,
    build_answer_prompt,
)

SUPPORTED = [1.0, 0.0, 0.0]
UNRELATED = [0.0, 1.0, 0.0]


class FakeClient:
    """Returns each reply in `replies` in turn - one per call, so a retry
    can be scripted to answer differently from the first attempt."""

    def __init__(self, replies=(), *, healthy: bool = True, raises: BaseException = None):
        self.replies = list(replies)
        self.healthy = healthy
        self.raises = raises
        self.calls = 0
        self.prompts: list[str] = []

    def health(self, *, force: bool = False) -> bool:
        return self.healthy

    def has_model(self) -> bool:
        return self.healthy

    def generate(self, prompt: str, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        reply = self.replies[self.calls - 1] if self.calls <= len(self.replies) else ""

        class Response:
            text = reply

        return Response()


class FakeEmbedder:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    def embed(self, texts):
        return [self.vectors.get(text, [0.0, 0.0, 1.0]) for text in texts]


PASSAGE = "The tenancy agreement was signed on 3 March 2019 for a deposit of 500 pounds."


# ---------------------------------------------------------------------------
# build_answer_prompt
# ---------------------------------------------------------------------------

def test_the_prompt_numbers_every_passage():
    prompt = build_answer_prompt("q", ["first passage", "second passage"])
    assert "[1] first passage" in prompt
    assert "[2] second passage" in prompt


def test_the_prompt_carries_the_question():
    prompt = build_answer_prompt("what was the deposit", ["a passage"])
    assert "what was the deposit" in prompt


def test_the_tighter_prompt_says_the_previous_attempt_failed():
    plain = build_answer_prompt("q", ["a passage"])
    tighter = build_answer_prompt("q", ["a passage"], tighter=True)
    assert "previous answer" not in plain
    assert "previous answer" in tighter


# ---------------------------------------------------------------------------
# answer_question - the happy path
# ---------------------------------------------------------------------------

def test_a_well_supported_answer_needs_no_retry():
    embedder = FakeEmbedder({
        "The deposit was 500 pounds .": SUPPORTED,
        PASSAGE: SUPPORTED,
    })
    client = FakeClient(["The deposit was 500 pounds [1]."])
    answer = answer_question("what was the deposit", [PASSAGE],
                             embedder=embedder, client=client)
    assert answer.sentences == ("The deposit was 500 pounds .",)
    assert not answer.retried
    assert not answer.thin
    assert client.calls == 1


def test_citations_carry_the_marker_numbers_a_superscript_needs():
    r"""**Found while designing the tab: `.sentences` alone loses the
    receipt.** `verify_answer` strips markers from its rendered text before
    this module ever sees it - a caller drawing a superscript (§3a) needs
    the marker back, not just the clean words."""
    embedder = FakeEmbedder({"The deposit was 500 pounds .": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["The deposit was 500 pounds [1]."])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert len(answer.citations) == 1
    assert answer.citations[0].markers == (1,)
    assert answer.citations[0].text == "The deposit was 500 pounds ."


def test_sentences_is_still_the_plain_text_for_a_caller_that_only_wants_it():
    embedder = FakeEmbedder({"The deposit was 500 pounds .": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["The deposit was 500 pounds [1]."])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.sentences == ("The deposit was 500 pounds .",)


def test_the_passages_offered_are_recorded_on_the_answer():
    embedder = FakeEmbedder({"Answer .": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["Answer [1]."])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.passages == (PASSAGE,)


# ---------------------------------------------------------------------------
# The retry
# ---------------------------------------------------------------------------

def test_a_thin_first_answer_is_retried_once():
    embedder = FakeEmbedder({
        "Uncited guess one .": SUPPORTED, "Uncited guess two .": SUPPORTED,
        "The deposit was 500 pounds .": SUPPORTED,
        PASSAGE: SUPPORTED,
    })
    # First reply: two sentences, neither cited - both dropped, 100% thin.
    # Second (retry) reply: one well-cited sentence.
    client = FakeClient([
        "Uncited guess one. Uncited guess two.",
        "The deposit was 500 pounds [1].",
    ])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.retried
    assert answer.sentences == ("The deposit was 500 pounds .",)
    assert not answer.thin
    assert client.calls == 2


def test_the_retry_uses_the_tighter_prompt():
    embedder = FakeEmbedder({"Uncited.": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["Uncited guess."])
    answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert len(client.prompts) == 2
    assert "previous answer" not in client.prompts[0]
    assert "previous answer" in client.prompts[1]


def test_only_one_retry_is_ever_attempted():
    embedder = FakeEmbedder({"Uncited.": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["Uncited guess one.", "Uncited guess two.", "Uncited guess three."])
    answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert client.calls == 2, "a second retry must never be attempted"


def test_a_still_thin_answer_after_retry_is_reported_as_thin():
    embedder = FakeEmbedder({"Uncited.": SUPPORTED, PASSAGE: SUPPORTED})
    client = FakeClient(["Uncited guess.", "Still uncited."])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.thin
    assert answer.retried
    assert answer.sentences == ()


def test_a_well_cited_but_thinner_than_half_answer_is_not_retried():
    r"""**The threshold is a fraction, not "any drop at all".** One dropped
    sentence out of three - a third, under `THIN_DROP_RATIO` - is ordinary
    caution working as intended, not a failed attempt."""
    embedder = FakeEmbedder({
        "Good one .": SUPPORTED, "Good two .": SUPPORTED, "Bad three .": UNRELATED,
        PASSAGE: SUPPORTED,
    })
    client = FakeClient(["Good one [1]. Good two [1]. Bad three [1]."])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert client.calls == 1, "should not have retried"
    assert answer.sentences == ("Good one .", "Good two .")
    assert not answer.thin


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_an_empty_question_produces_nothing_without_calling_the_model():
    embedder = FakeEmbedder({})
    client = FakeClient(["should never be used"])
    answer = answer_question("   ", [PASSAGE], embedder=embedder, client=client)
    assert answer.sentences == ()
    assert client.calls == 0


def test_no_passages_produces_nothing_without_calling_the_model():
    embedder = FakeEmbedder({})
    client = FakeClient(["should never be used"])
    answer = answer_question("q", [], embedder=embedder, client=client)
    assert answer.sentences == ()
    assert client.calls == 0


def test_a_raising_client_produces_a_thin_empty_answer_not_a_crash():
    embedder = FakeEmbedder({})
    client = FakeClient(raises=RuntimeError("boom"))
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.sentences == ()
    assert answer.thin
    assert not answer.retried, "an empty generation is not retried - there is nothing to retry"


def test_an_unhealthy_client_is_never_asked_and_is_not_retried():
    embedder = FakeEmbedder({})
    client = FakeClient(["reply"], healthy=False)
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert client.calls == 0
    assert answer.thin
    assert not answer.retried


def test_an_empty_reply_is_thin_and_not_retried():
    embedder = FakeEmbedder({})
    client = FakeClient([""])
    answer = answer_question("q", [PASSAGE], embedder=embedder, client=client)
    assert answer.thin
    assert not answer.retried
    assert client.calls == 1
