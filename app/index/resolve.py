r"""What the tuning settings come to, for the run that is about to start.

Layer: L3

**One function, called by both entry points.** The Index Tuning screen resolves
`0` to `Auto (4)` for display; without this, the run itself did not - it read
`settings.index_workers` and got the literal `0`, so the mode switch changed
what the screen said and nothing about what happened. A control that is
believed and ignored is worse than one that is absent.

The resolution is the same one the screen shows, by construction: both call
`app/core/envelope.py`, and in Auto both are handed the same measured rates.
There is no second copy of the arithmetic to drift.

Reads the store once, for the profile and the rates. Everything else is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core import envelope
from app.core.logging import logger

__all__ = ["Resolved", "resolve_for_run"]

_log = logger.bind(component="index.resolve")

#: The tunables whose stored `0` means "decide for this machine".
_AUTO_AT_ZERO = ("INDEX_WORKERS", "ONNX_INTRA_OP_THREADS", "EMBED_BATCH")


@dataclass
class Resolved:
    """The numbers this run will actually use, and why."""

    workers: int = 0
    onnx_threads: int = 0
    embed_batch: int = 0
    #: `{key: sentence}` - what each number is and where it came from. Printed
    #: by the CLI and recorded in the run log, because a run whose settings
    #: cannot be reconstructed afterwards is a run nobody can learn from.
    why: dict = None                             # type: ignore[assignment]
    #: True when the rates came from a measurement rather than the heuristics.
    measured: bool = False
    #: Work order 202626130120 (0t) section 6: "" unless this machine had a
    #: working DirectML provider last time and genuinely does not have one
    #: now. Carried on `Resolved` rather than looked up again beside the
    #: Pipeline, because this is the one place both the CLI and the window
    #: already resolve the machine off the UI thread before a run starts.
    gpu_regression_notice: str = ""

    def __post_init__(self) -> None:
        if self.why is None:
            self.why = {}

    def as_dict(self) -> dict:
        return {
            "workers": self.workers,
            "onnx_threads": self.onnx_threads,
            "embed_batch": self.embed_batch,
            "measured": self.measured,
        }


def test_this_machine_if_new(settings: Any) -> bool:
    r"""Test each model on the processor and the graphics card before a run on a
    machine never tested here. True when it tested.

    2026-10-05, the owner: "this test mechanism should be on every indexing
    option so it can get tested and the setting set". Every run - Start, a
    folder's "Index now", the schedule, the command line - resolves its tuning
    here first, so every one of them tests an untested machine, once; the
    result is kept per machine (`model_devices`). Only while "Run models on"
    is Automatic: somebody who chose a processor outright is not overruled,
    and Test this machine (Indexing > Tuning > Devices) is there for them.
    Runs on the caller's worker, under the window's "Checking your hardware".
    Never raises, as `resolve_for_run` promises.
    """
    try:
        if str(getattr(settings, "embed_device", "auto") or "auto") != "auto":
            return False
        from app.core.model_devices import needs_test

        if not needs_test(settings):
            return False
        from app.index.device_test import gpu_usable, run_device_test

        if not gpu_usable()[0]:
            return False
        run_device_test(settings)
        return True
    except Exception as exc:                         # noqa: BLE001 - see docstring
        _log.warning("this machine was not tested before the run: {}", exc)
        return False


def resolve_for_run(settings: Any, store: Any = None,
                    profile: Any = None) -> Resolved:
    r"""The effective tuning numbers for one run.

    **Manual is the only mode that uses the stored numbers.** Defaults and
    Auto-tune both mean "the envelope decides", so a value somebody typed in
    Manual is kept and inert while the mode is elsewhere - which is what makes
    the mode switch a reversible experiment rather than something that eats
    what you typed.

    Never raises: a store that cannot be read, a machine that cannot be
    detected, an envelope that has nothing to say - each of them lands on the
    stored value, which is the behaviour from before any of this existed.
    """
    test_this_machine_if_new(settings)
    mode = str(getattr(settings, "index_tuning_mode", "defaults") or "defaults")
    stored = {
        "INDEX_WORKERS": int(getattr(settings, "index_workers", 0) or 0),
        "ONNX_INTRA_OP_THREADS": int(
            getattr(settings, "onnx_intra_op_threads", 0) or 0),
        "EMBED_BATCH": int(getattr(settings, "embed_batch", 0) or 0),
    }

    profile = profile if profile is not None else _profile(store, settings)
    # **A machine nothing is known about is not a machine with one core**, and
    # the envelope cannot tell the difference: it reads missing fields as zero
    # and hands back a ceiling of 1. Left alone that clamped somebody's six
    # workers to one on any machine detection could not examine - the same bug
    # the tuning screen had, in the code path that actually runs the index.
    if profile is not None and not (
            getattr(profile, "physical_cores", 0)
            or getattr(profile, "logical_processors", 0)):
        _log.info("this machine's cores could not be counted, so the stored "
                  "tuning values stand")
        profile = None

    rates = _rates(store, profile) if mode == "auto" else None

    found = Resolved(measured=rates is not None)
    for key in _AUTO_AT_ZERO:
        value, why = _one(key, mode, stored[key], profile, rates)
        found.why[key] = why
        if key == "INDEX_WORKERS":
            found.workers = value
        elif key == "ONNX_INTRA_OP_THREADS":
            found.onnx_threads = value
        else:
            found.embed_batch = value

    # The warning belongs to the run as much as to the screen: somebody who
    # tuned by hand and then started a run from the command line never saw the
    # inline one.
    note = envelope.oversubscription_warning(
        profile, found.workers, found.onnx_threads)
    if note:
        _log.warning("{}", note)
        found.why["oversubscribed"] = note

    # Work order 202626130120 (0t) section 6. Only when there is a real store
    # to read "last time" from - every test in this module calls with
    # `store=None` and an invented `profile=`, which must stay exactly as
    # quiet as it is today.
    if store is not None:
        gpu_notice = _gpu_regression(store, settings)
        if gpu_notice:
            _log.warning("{}", gpu_notice)
            found.why["gpu_lost"] = gpu_notice
            found.gpu_regression_notice = gpu_notice
    return found


def _one(key: str, mode: str, stored: int, profile: Any,
         rates: Any) -> tuple[int, str]:
    """One knob: `(value, why)`, resolved exactly as the screen resolves it."""
    bounds = envelope.for_setting(key, profile, rates) if profile is not None \
        else None
    if bounds is None:
        return stored, "no local bound is known, so the stored value stands"

    if mode == "manual" and stored:
        value, notice = bounds.clamp(stored)
        return value, notice or f"set by hand: {bounds.why}"

    # **`0` means auto in every mode, including Manual.** Somebody in Manual
    # who left a control alone has not chosen a number, and giving them a
    # literal zero workers would be a run that never starts.
    if stored and mode != "manual":
        return bounds.auto, f"{bounds.why} (your {stored} applies in Manual)"
    return bounds.auto, bounds.why


def _profile(store: Any, settings: Any) -> Optional[Any]:
    """This machine's compute profile: the store's cached one when there is a
    store, a fresh detection otherwise. None when it cannot be read, which
    leaves the stored tuning values standing."""
    try:
        from app.core.compute_profile import cached_profile, detect

        if store is None:
            return detect(getattr(settings, "data_path", None))
        return cached_profile(store, getattr(settings, "data_path", None))
    except Exception as exc:                     # noqa: BLE001 - never fatal
        _log.debug("this machine could not be examined: {}", exc)
        return None


def _gpu_regression(store: Any, settings: Any) -> str:
    r"""Work order 202626130120 (0t) section 6's three-way check.

    Compares the profile `store` had cached **before** this call against a
    genuinely fresh `detect()` - never the value `cached_profile` returns,
    because a DirectML loss alone does not change `ComputeProfile.fingerprint`
    (it hashes adapter name and VRAM, not provider availability), so
    `cached_profile` can return the *old*, still-rosy stored profile on
    exactly the machine this check exists for. Only reached when a store is
    given, and only detects again when the stored profile shows the provider
    was ever available - the common case (never had a GPU, or nothing cached
    yet) costs nothing beyond one state read.

    Never raises: any failure here is a missed notice, not a failed run.
    """
    try:
        from app.core.compute_profile import detect, stored_profile

        stored = stored_profile(store)
        if stored is None or not stored.directml_available:
            return ""
        fresh = detect(getattr(settings, "data_path", None))
        from app.index.backends import gpu_regression_notice

        return gpu_regression_notice(stored, fresh)
    except Exception as exc:                     # noqa: BLE001 - never fatal
        _log.debug("could not check for a lost graphics-card provider: {}", exc)
        return ""


def _rates(store: Any, profile: Any) -> Optional[Any]:
    """The measured rates for this profile (Auto mode only), or None - which
    the envelope reads as "use the heuristics"."""
    if store is None or profile is None:
        return None
    try:
        from app.core.measured import for_profile

        return for_profile(store, profile)
    except Exception as exc:                     # noqa: BLE001
        _log.debug("the measured rates could not be read: {}", exc)
        return None
