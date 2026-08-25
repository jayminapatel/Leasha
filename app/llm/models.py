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
    "EMBEDDING_HINTS",
    "TIMEOUT_RANGE",
]

#: Substrings marking a model that cannot answer a prompt at all. Offering one
#: for Interpret produces a confusing failure several seconds later, and
#: `nomic-embed-text` sits in most people's `ollama list` precisely because
#: something else pulled it.
EMBEDDING_HINTS = ("embed", "bge", "gte-", "e5-")

#: Seconds. The floor is above the ~8s a cold model takes to answer even a
#: trivial prompt - a smaller budget cannot work and only produces the timeout
#: this whole line of work came from.
TIMEOUT_RANGE = (10, 180)


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

    @property
    def label(self) -> str:
        return describe(self.name, self.billions, self.embedding_only)

    @property
    def selectable(self) -> bool:
        return not self.embedding_only


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
    if billions is None:
        return 30
    if billions <= 2:
        return 15
    if billions <= 4:
        return 20
    if billions <= 9:
        return 30
    if billions <= 20:
        return 60
    return 120


def describe(name: str, billions: Optional[float] = None, embedding_only: bool = False) -> str:
    """One line: the name, and what it will cost you.

    The hint is the point. A bare list of names asks somebody to guess at the
    speed difference, and guessing at it is what produced a five-second budget
    for a thirty-second job.
    """
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
