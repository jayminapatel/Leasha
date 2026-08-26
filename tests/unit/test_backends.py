r"""Index tuning §2: the compute seam, and the promises it makes.

**Every machine this suite runs on is a CPU-only Linux box**, so nothing here
can be tested by having a graphics card. That is the reason `choose` takes an
injectable provider list and `with_fallback` takes a builder: the decision and
the degradation are ordinary functions over invented facts, and a test can put
this file on a machine with two GPUs and a driver that fails on the third call.

The one thing measurement cannot settle here is whether the graphics card is
*faster*. §0 says so and this file does not pretend otherwise - what is asserted
is that the choice is honest, that failure is loud, and that switching
processors does not touch what is stored.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.index import backends

ROOT = Path(__file__).resolve().parents[2]


class Machine:
    """A profile, invented. Only the fields the seam reads."""

    def __init__(self, gpus=(), directml=False) -> None:
        self.gpus = gpus
        self.directml_available = directml


NO_GPU = Machine()
GPU_NO_PROVIDER = Machine(gpus=("Iris Xe",))
GPU_READY = Machine(gpus=("Iris Xe",), directml=True)


# --- 2a: the seam decides once, and the CPU is always reachable -------------


@pytest.mark.parametrize("device", [backends.CPU, backends.GPU, backends.AUTO])
def test_the_processor_is_always_in_the_provider_list(device: str) -> None:
    """onnxruntime falls back per operator, so dropping the CPU entry turns a
    graph with one unsupported operator into a failed session on a machine that
    was working a moment ago."""
    assert backends.providers_for(device)[-1] == backends.CPU_PROVIDER


def test_asking_for_the_processor_gets_the_processor_everywhere() -> None:
    """Including on a machine that has a working graphics card - a control that
    is overridden by the hardware is not a control."""
    assert backends.choose(GPU_READY, backends.CPU).device == backends.CPU


def test_auto_uses_the_graphics_card_only_when_it_can_be_used() -> None:
    assert backends.choose(GPU_READY).device == backends.GPU
    assert backends.choose(GPU_NO_PROVIDER).device == backends.CPU
    assert backends.choose(NO_GPU).device == backends.CPU


def test_an_unknown_device_lands_on_auto_rather_than_raising() -> None:
    """A value already in flight is not the place to fail; `validate_settings`
    is, and it does. This keeps a stale `.env` from stopping a search."""
    assert backends.choose(GPU_READY, "cuda").device == backends.GPU


# --- 2b: refused at the control, with the reason ----------------------------


def test_the_two_reasons_the_graphics_card_is_unavailable_read_differently(
) -> None:
    """They send somebody to different places: no adapter is a hardware fact,
    and no provider is one `pip install` away. One message for both would be a
    message that helps in neither case."""
    absent = backends.why_unavailable(NO_GPU)
    unprovided = backends.why_unavailable(GPU_NO_PROVIDER)

    assert absent and unprovided and absent != unprovided
    assert "pip install onnxruntime-directml" in unprovided
    assert backends.why_unavailable(GPU_READY) == ""


def test_asking_for_a_graphics_card_that_is_not_there_says_so() -> None:
    """The control greys the option, so reaching this means the machine changed
    under a stored setting - which is exactly when silence would be worst."""
    choice = backends.choose(NO_GPU, backends.GPU)

    assert choice.device == backends.CPU
    assert choice.fell_back_from == backends.GPU
    assert backends.why_unavailable(NO_GPU) in choice.why


def test_every_choice_carries_a_sentence() -> None:
    """`why` is shown beside the control and printed by `doctor`. A blank one
    is a control nobody can account for."""
    for machine in (NO_GPU, GPU_NO_PROVIDER, GPU_READY):
        for device in backends.DEVICES:
            assert backends.choose(machine, device).why.strip()


def test_the_runtime_truth_beats_the_cached_profile() -> None:
    """The profile is a stored fact and can be a week old; the provider list is
    what onnxruntime says right now."""
    choice = backends.choose(GPU_READY, backends.GPU,
                             available=["CPUExecutionProvider"])

    assert choice.device == backends.CPU
    assert choice.fell_back_from == backends.GPU


# --- 2b: degrade loudly, never crash, never silently ------------------------


def test_a_graphics_card_that_refuses_the_model_falls_back_and_says_so() -> None:
    """H4's discipline. A driver mid-update or a device in use is an ordinary
    Windows situation and may not end an index run."""
    tried: list = []
    problems: list = []

    def build(providers):
        tried.append(providers)
        if backends.DML_PROVIDER in providers:
            raise RuntimeError("the device is in use")
        return "session"

    session, choice = backends.with_fallback(
        build, backends.choose(GPU_READY), problems=problems)

    assert session == "session"
    assert choice.device == backends.CPU
    assert choice.fell_back_from == backends.GPU
    assert len(tried) == 2, "the graphics card must actually have been tried"
    assert problems and "processor" in problems[0]


def test_a_processor_failure_is_raised_rather_than_swallowed() -> None:
    """There is nothing to fall back *to*, and a silent None here would be the
    model-load failure that H4 already had to be taught to report."""
    def build(_providers):
        raise RuntimeError("the model file is missing")

    with pytest.raises(RuntimeError):
        backends.with_fallback(build, backends.choose(NO_GPU))


def test_a_working_graphics_card_is_not_second_guessed() -> None:
    """One build call. A seam that loaded the model twice to check would double
    the slowest part of start-up on every machine that works."""
    calls: list = []

    def build(providers):
        calls.append(providers)
        return "session"

    _session, choice = backends.with_fallback(build, backends.choose(GPU_READY))

    assert choice.device == backends.GPU
    assert len(calls) == 1


# --- 2d: what ran gets recorded, and nothing else changes -------------------


def test_recording_the_provider_never_raises() -> None:
    """It is called from inside a model load. An absent or closed run log is
    not a reason for the model not to load."""
    backends.record_provider("meaning model", backends.choose(NO_GPU))
    backends.record_provider("meaning model", None)


def test_the_run_log_is_told_what_ran_not_what_was_asked_for(monkeypatch
                                                            ) -> None:
    """§2d, and the reason for it: a run that took four hours because the
    graphics card quietly declined is one nobody can diagnose a week later."""
    noted: dict = {}

    class Run:
        def note(self, key, value):
            noted[key] = value

    import app.core.runlog as runlog

    monkeypatch.setattr(runlog, "current", lambda: Run())
    backends.record_provider("meaning model",
                             backends.choose(NO_GPU, backends.GPU))

    (key, value), = noted.items()
    assert "meaning model" in key
    assert value.startswith(backends.CPU)
    assert "asked for the gpu" in value


def test_switching_processors_does_not_invalidate_an_index() -> None:
    r"""**The setting is deliberately not `destructive`**, and this is the
    claim that makes that safe to say.

    `EMBED_MODEL` invalidates every vector because a different model produces
    different vectors. A different *provider* runs the same graph and the same
    weights, to within floating-point noise, so the stored vectors stay
    comparable and no re-embed is owed. If that ever stops being true, this
    test is the place the claim is written down.
    """
    from app.core.settings_registry import by_key

    setting = by_key("EMBED_DEVICE")

    assert setting.destructive is False
    assert setting.flow == "", "a flow would mean it costs something to change"
    assert set(setting.choices) == set(backends.DEVICES)


def test_a_fingerprint_change_does_not_touch_the_vectors() -> None:
    r"""§2d's third clause, asserted where it is decided.

    The fingerprint keys the *profile cache* - it is how `cached_profile` knows
    the machine changed. If it were ever mixed into what identifies a vector,
    plugging in a monitor would silently orphan an index. So the assertion is
    structural: nothing in the vector store consults the profile at all.
    """
    tree = ast.parse((ROOT / "app" / "storage" / "vector_store.py")
                     .read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}

    assert "app.core.compute_profile" not in imported
    assert not any(name.startswith("app.index.backends") for name in imported)


# --- 2: one seam, three consumers -------------------------------------------


@pytest.mark.parametrize("module", [
    Path("app") / "index" / "embedder.py",
    Path("app") / "search" / "rerank.py",
    Path("app") / "extract" / "ocr.py",
])
def test_all_three_model_users_go_through_the_seam(module: Path) -> None:
    """A machine where embedding used the graphics card and reranking did not
    is a machine nobody could reason about. Three copies of the provider list
    is how that happens, so there is one."""
    source = (ROOT / module).read_text(encoding="utf-8")

    assert "backends" in source, module
    assert "DmlExecutionProvider" not in source, (
        f"{module} names a provider directly instead of asking the seam")
