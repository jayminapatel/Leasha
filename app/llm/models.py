"""Choosing an Ollama model, and saying something useful about each one.

Layer: L2 (no Qt, no network)

The model is a *speed* decision far more than a quality one, and this session
proved it the hard way. Interpret rewrites a sentence into `from:chris licence`
— roughly twenty tokens of output against a 1,900-character prompt. A 1.5B model
does that as well as a 7B one and finishes in a fraction of the time; a 20B model
does it no better and can take a minute. But nothing in the application said so,
the default was `mistral`, and the budget was five seconds — so the feature
failed every time on a machine with five perfectly good models installed.

**So the list is not just a list.** Each row carries a hint about what that model
will cost, because a bare dropdown of names asks somebody to guess at exactly the
thing that went wrong.

Nothing here talks to Ollama. `OllamaClient.available_models()` does that; these
functions decide what to *do* with what it returns, which is the part worth
testing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

__all__ = [
    "ModelChoice",
    "describe",
    "parameter_billions",
    "suggested_timeout_s",
    "choose",
    "rank",
    "with_suggestions",
    "install_hint",
    "EMBEDDING_HINTS",
    "SUGGESTED",
    "VISION_SUGGESTED",
    "TIMEOUT_RANGE",
]

#: Models worth having for this job, whether or not they are installed.
#:
#: **Because a dropdown with one entry reads as broken.** Reported as *"change
#: model has only one model"* - and the list was correct: Ollama had exactly one
#: model pulled. Correct and useless. A person looking at a one-line dropdown
#: cannot tell whether the application failed to find the others, whether there
#: are no others, or what they would have to do to get any, and nothing on the
#: panel said.
#:
#: These are listed alongside the installed ones and shown greyed out with the
#: command that would install them - the same treatment embedding models
#: already get, and for the same reason: seeing something you cannot pick, with
#: the reason attached, beats an absence you have to interpret.
#:
#: Kept deliberately short. This is a recommendation, and a recommendation of
#: fifteen things is a list. All three are small, because Interpret rewrites one
#: sentence and a larger model does that no better and much slower.
SUGGESTED: tuple[str, ...] = (
    "qwen2.5:1.5b",
    "llama3.2:3b",
    "phi3.5:3.8b",
)


#: Ollama models that can read a picture, for Describe and the caption trickle
#: (`OLLAMA_VISION_MODEL`). Owner, 2026-09-29: model lists are drop-downs only,
#: with the other options listed and a way to download them. Smallest first,
#: each with its size in parameters, because the size is what decides whether
#: describing a photo takes seconds or a minute. Every name here is matched by
#: `app.chat.roles.VISION_NAME_HINTS`, so once pulled it is recognised as a
#: vision model by the same rule the Chat roles grid uses.
VISION_SUGGESTED: tuple[tuple[str, str], ...] = (
    ("moondream", "1.8B, the smallest and quickest"),
    ("llava", "7B, the usual choice"),
    ("qwen2.5vl", "7B, good at reading text in pictures"),
    ("minicpm-v", "8B"),
    ("llama3.2-vision", "11B, slow without a graphics card"),
)


def install_hint(name: str) -> str:
    """The command that would make `name` available."""
    return f"ollama pull {name}"

#: Substrings marking a model that cannot answer a prompt at all. Offering one
#: for Interpret produces a confusing failure several seconds later, and
#: `nomic-embed-text` sits in most people's `ollama list` precisely because
#: something else pulled it.
EMBEDDING_HINTS = ("embed", "bge", "gte-", "e5-")

#: Seconds. The floor is above the ~8s a cold model takes to answer even a
#: trivial prompt - a smaller budget cannot work and only produces the timeout
#: this whole line of work came from.
TIMEOUT_RANGE = (10, 240)


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One installed model, as a person choosing between them needs it."""

    name: str
    #: Billions of parameters, when the name says. `None` when it does not -
    #: guessing would be worse than admitting ignorance, because the guess would
    #: drive the speed hint.
    billions: Optional[float] = None
    #: True for a model that produces vectors, not text. Listed but not
    #: selectable: seeing it greyed out with a reason beats wondering why the
    #: model you can see in `ollama list` is missing here.
    embedding_only: bool = False
    #: False for a suggestion that is not pulled yet. Also listed, also not
    #: selectable, and labelled with the command that would change that - see
    #: `SUGGESTED`.
    installed: bool = True

    @property
    def label(self) -> str:
        """The dropdown row: name plus the cost hint, or the install command."""
        return describe(self.name, self.billions, self.embedding_only,
                        installed=self.installed)

    @property
    def selectable(self) -> bool:
        """Whether a person may pick it: installed, and able to write text."""
        return self.installed and not self.embedding_only


def parameter_billions(name: str) -> Optional[float]:
    """Parameter count from the tag, or None.

    `qwen2.5:1.5b` -> 1.5, `gpt-oss:20b` -> 20.0, `mistral` -> None.

    Deliberately only reads what the name states. Ollama's API does report a
    size, but it is the *file* size, which conflates parameter count with
    quantisation - a 7B model at q4 and a 3B at q8 are the same number of bytes
    and very different amounts of work.
    """
    match = re.search(r"[:\-_](\d+(?:\.\d+)?)\s*b\b", name.lower())
    if match:
        return float(match.group(1))
    # Some tags carry it without a separator: `llama3.2-3b`, `phi3:mini` has none.
    match = re.search(r"\b(\d+(?:\.\d+)?)b\b", name.lower())
    return float(match.group(1)) if match else None


def is_embedding_model(name: str) -> bool:
    """Does the name say this model makes vectors rather than text? A guess
    from `EMBEDDING_HINTS`; Ollama's listing does not say, so a name is all
    there is to go on, and a wrong guess only greys out one row."""
    lowered = name.lower()
    return any(hint in lowered for hint in EMBEDDING_HINTS)


def suggested_timeout_s(billions: Optional[float]) -> int:
    """A budget that fits the model, rounded to something readable.

    **The number that was wrong.** Five seconds was chosen because it felt
    responsive, against a job that takes tens of seconds on a 7B model - so the
    feature never once succeeded. These are deliberately generous: Interpret is
    an explicit button running off the UI thread, and the first call of a session
    also pays to load the model into memory. A budget shorter than the work is
    not responsiveness, it is a feature that cannot run.

    Unknown size gets the middle of the road rather than the floor, for the same
    reason: the cost of waiting too long is impatience, and the cost of waiting
    too little is a feature that appears broken.
    """
    # **These went up after a measurement.** qwen2.5:1.5b answered in 3.7s from
    # the command line with the model already warm, so 15s looked generous - and
    # then timed out in the GUI, because the first call of a session also loads
    # the model into memory and that is where the time goes. A budget that only
    # works on the second attempt is not a budget.
    # **And again, 2026-09-30, for the model inside Leasha.** Qwen 2.5 1.5B at
    # 4 bits on the owner's processor: 35.1s for the first press after starting
    # (7.7s of it loading), 8.6s after that. 30s gave up on every first press.
    # The two sizes above it, and the unknown one, moved up to stay in order.
    if billions is None:
        return 50
    if billions <= 2:
        return 45
    if billions <= 4:
        return 50
    if billions <= 9:
        return 60
    if billions <= 20:
        return 90
    return 150


def describe(name: str, billions: Optional[float] = None, embedding_only: bool = False,
             *, installed: bool = True) -> str:
    """One line: the name, and what it will cost you.

    The hint is the point. A bare list of names asks somebody to guess at the
    speed difference, and guessing at it is what produced a five-second budget
    for a thirty-second job.

    A model that is *not* installed carries the command instead of the speed
    note - what it would cost is the wrong question until it exists.
    """
    if not installed:
        return f"{name} — not installed:  {install_hint(name)}"
    if embedding_only:
        return f"{name} — makes vectors, cannot answer questions"
    if billions is None:
        return name
    if billions <= 2:
        return f"{name} — small and fast, ample for rewriting a query"
    if billions <= 9:
        return f"{name} — capable, a few seconds a query"
    if billions <= 20:
        return f"{name} — large, expect to wait"
    return f"{name} — very large, likely too slow for this"


def rank(names: Iterable[str]) -> list[ModelChoice]:
    """Installed models as choices, smallest usable first.

    Smallest first because for this job small is the *right* answer and the
    order of a dropdown is a recommendation whether or not it is meant as one.
    Embedding models sort to the bottom, since they cannot be chosen at all.
    """
    choices = [
        ModelChoice(
            name=name,
            billions=parameter_billions(name),
            embedding_only=is_embedding_model(name),
        )
        for name in names
        if str(name).strip()
    ]
    return sorted(
        choices,
        key=lambda choice: (
            choice.embedding_only,
            # Unknown size sorts after every known one rather than first: it
            # cannot be recommended, so it should not sit at the top.
            choice.billions if choice.billions is not None else float("inf"),
            choice.name,
        ),
    )


def with_suggestions(installed: Iterable[str]) -> list[ModelChoice]:
    """`rank(installed)`, then the suggestions that are not installed.

    **So the dropdown is never a single line with no explanation.** With one
    model pulled the list was one row long and correct, and read as a fault -
    *"change model has only one model"*. There was nothing on the panel saying
    whether others existed, whether the probe had failed, or what would produce
    more.

    The additions are shown and not selectable, carrying `ollama pull …` in the
    label. That is exactly how embedding models are already handled, and the
    reasoning carries across: a name you cannot choose, with the reason
    attached, answers the question. An absence does not.

    Matching ignores the tag, because `qwen2.5:1.5b` installed as
    `qwen2.5:1.5b-instruct-q4_0` is the same suggestion already taken, and
    offering to pull something somebody has is worse than offering nothing.
    """
    ranked = rank(installed)
    have = {choice.name for choice in ranked}
    have |= {choice.name.split(":")[0] for choice in ranked}

    extra = [
        ModelChoice(name=name, billions=parameter_billions(name), installed=False)
        for name in SUGGESTED
        if name not in have and name.split(":")[0] not in have
    ]
    return ranked + extra


def choose(configured: str, installed: Sequence[str]) -> tuple[str, str]:
    """`(model, note)` — what to actually use, and what to tell the person.

    **The configured model always wins, even when the probe found nothing.**
    A failed probe means Ollama was not answering *at that moment*; silently
    swapping the model because of it would change a setting the person chose,
    which is the failure this codebase keeps running into. So the note explains,
    and the value stands.

    `mistral` and `mistral:latest` are the same model. People write the short
    form, Ollama reports the long one, and treating them as different is how a
    correctly-configured model gets reported as missing.
    """
    wanted = (configured or "").strip()
    if not wanted:
        usable = [choice for choice in rank(installed) if choice.selectable]
        if usable:
            return usable[0].name, f"No model set, so using {usable[0].name}."
        return "", "No model set, and none is installed. Run: ollama pull qwen2.5:1.5b"

    if not installed:
        return wanted, "Could not reach Ollama, so the list is what was saved last."

    bare = wanted.split(":")[0]
    for name in installed:
        if name == wanted or name.split(":")[0] == bare:
            return wanted, ""

    return wanted, (
        f"'{wanted}' is not installed. Pull it with 'ollama pull {wanted}', "
        "or choose one of the models listed."
    )
