r"""Which processor runs the models — CPU today, the GPU when it earns it.

Layer: L3 — one seam, three consumers: the embedder, the reranker and OCR.

**One runtime, one decision, three users.** All three are ONNX sessions, and
before this they each reached for their own provider list. A machine where
embedding used the GPU and reranking did not would be a machine nobody could
reason about, so the choice is made once, here.

**`auto` measures rather than assumes.** The order's §0 is explicit about this:
whether 96 Iris Xe execution units beat this particular CPU on a small embed
model is not knowable from the specification sheet, and this file does not
pretend otherwise. `auto` prefers the GPU only when one is present *and* the
provider is installed, and any failure falls back to the CPU **with a notice** -
H4's discipline exactly: degrade loudly, never crash, never silently.

`gpu` on a machine without one is refused at the control with its reason
showing, not at runtime - an unreachable illegal state rather than a runtime
surprise, which is §3c's rule applied to a choice rather than a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "AUTO",
    "CPU",
    "DEVICES",
    "GPU",
    "Choice",
    "choose",
    "providers_for",
    "record_provider",
    "why_unavailable",
    "with_fallback",
]

_log = logger.bind(component="index.backends")

AUTO = "auto"
CPU = "cpu"
GPU = "gpu"

#: What `EMBED_DEVICE` accepts. A closed set, so the control is a choice rather
#: than a text box somebody can put a typo into.
DEVICES = (AUTO, CPU, GPU)

#: onnxruntime's name for the DirectML provider.
DML_PROVIDER = "DmlExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"


@dataclass(frozen=True)
class Choice:
    """Which processor, which providers, and the sentence explaining it."""

    device: str
    providers: tuple[str, ...]
    why: str
    #: Set when the request could not be honoured and this is the fallback.
    #: The caller turns it into the notice - silence here would be H4's bug
    #: reintroduced through a different door.
    fell_back_from: str = ""

    @property
    def is_gpu(self) -> bool:
        return self.device == GPU


def record_provider(what: str, choice: Optional[Choice]) -> None:
    """Put the processor a model actually loaded on into the run log.

    **What ran, not what was asked for.** §2d's requirement, and the reason it
    is a requirement: a run that took four hours because the graphics card
    quietly declined and nobody said so is a run nobody can diagnose a week
    later. The run log is the artefact that survives the session, so the fact
    goes there rather than only into a log line.

    Never raises. A run log that is absent, closed, or of an older shape is not
    a reason for a model load to fail.
    """
    if choice is None:
        return
    try:
        from app.core.runlog import current

        run = current()
        if run is None:
            return
        detail = choice.why
        if choice.fell_back_from:
            detail = f"{detail} (asked for the {choice.fell_back_from})"
        run.note(f"{what} ran on", f"{choice.device} - {detail}")
    except Exception:                            # noqa: BLE001 - never fatal
        _log.debug("the provider could not be recorded in the run log")


def why_unavailable(profile: Any) -> str:
    """Why the GPU cannot be used here, or `""` when it can.

    The control shows this beside a greyed option. Two different causes need
    two different sentences, because they send somebody to different places:
    no adapter is a hardware fact, and no provider is one `pip install` away.
    """
    if not getattr(profile, "gpus", ()):
        return "no display adapter was detected"
    if not getattr(profile, "directml_available", False):
        return ("this installation has no DirectML provider - "
                "pip install onnxruntime-directml")
    return ""


def providers_for(device: str) -> tuple[str, ...]:
    """The onnxruntime provider list for a device.

    **The CPU provider is always last, never absent.** onnxruntime falls back
    through the list per operator, so a graph with one operator DirectML cannot
    run still executes - dropping the CPU entry would turn that into a failed
    session on a machine that was working a moment ago.
    """
    if device == GPU:
        return (DML_PROVIDER, CPU_PROVIDER)
    return (CPU_PROVIDER,)


def choose(profile: Any, requested: str = AUTO, *,
           available: Optional[Any] = None) -> Choice:
    """Which processor to use, and the sentence that explains it.

    `available` is injectable so the decision is testable without onnxruntime
    installed, and without a GPU - which is every machine this suite runs on.
    """
    wanted = str(requested or AUTO).strip().lower()
    if wanted not in DEVICES:
        wanted = AUTO

    blocked = why_unavailable(profile)
    if available is not None:
        # An explicit provider list overrides what the profile believes: the
        # profile is a cached fact and this is the runtime truth.
        if DML_PROVIDER not in tuple(available):
            blocked = blocked or "the DirectML provider is not loaded"
        elif not blocked:
            blocked = ""

    if wanted == CPU:
        return Choice(CPU, providers_for(CPU), "asked for the processor")

    if wanted == GPU:
        if blocked:
            # **Refused at the control, so this is the belt and not the
            # braces.** Reaching it means the machine changed under a stored
            # setting - a GPU removed, a provider uninstalled - which is
            # exactly when saying so matters.
            return Choice(CPU, providers_for(CPU),
                          f"the graphics card was asked for but {blocked}",
                          fell_back_from=GPU)
        return Choice(GPU, providers_for(GPU), "asked for the graphics card")

    if blocked:
        return Choice(CPU, providers_for(CPU),
                      f"the processor, because {blocked}")
    return Choice(GPU, providers_for(GPU),
                  "the graphics card, which this machine has and can use")


def with_fallback(build: Any, choice: Choice, *,
                  problems: Optional[list] = None) -> tuple[Any, Choice]:
    """Build a session with `choice`, or fall back to the CPU **and say so**.

    `build(providers)` is whatever creates the session. A GPU that is present,
    advertised and then fails on first use is an ordinary Windows situation -
    a driver mid-update, a device in use, an adapter that reports DirectML and
    then refuses a graph - and none of those may end an index run.

    Returns the session and the choice that actually applied, so the caller can
    record the provider per run rather than the one that was requested.
    """
    try:
        return build(choice.providers), choice
    except Exception as exc:                     # noqa: BLE001 - see docstring
        if not choice.is_gpu:
            raise
        message = (f"the graphics card would not run the model "
                   f"({type(exc).__name__}), so the processor is being used "
                   f"instead")
        _log.warning("{}: {}", message, exc)
        if problems is not None:
            try:
                problems.append(message)
            except Exception:                    # noqa: BLE001
                pass
        fallback = Choice(CPU, providers_for(CPU), message, fell_back_from=GPU)
        return build(fallback.providers), fallback
