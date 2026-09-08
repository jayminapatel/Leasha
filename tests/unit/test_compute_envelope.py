r"""Index tuning §1 and §3: what the machine is, and what that allows.

Every bound in that order derives from detection, so these are the tests that
decide whether anything downstream can be trusted. They run against **invented
profiles**, deliberately: the machines that matter - a 2P+8E laptop, a
dual-core netbook, a 32-core workstation - are not the one running the suite,
and a bound that is only ever checked on the test machine is a bound nobody has
checked.
"""

from __future__ import annotations

import json

import pytest

from app.core import envelope as env
from app.core.compute_profile import (
    OVERRIDE_ENV,
    PROFILE_STATE_KEY,
    ComputeProfile,
    GpuAdapter,
    cached_profile,
    detect,
)
from app.index.resources import default_workers
from app.storage.sqlite_store import SqliteStore

#: The owner's machine, from the order's §0 measurement.
OWNER = ComputeProfile(logical_processors=12, physical_cores=10,
                       performance_cores=2, efficiency_cores=8, ram_mb=32460,
                       avx2=True, index_disk="ssd")
SMALL = ComputeProfile(logical_processors=2, physical_cores=2, ram_mb=4096)
BIG = ComputeProfile(logical_processors=64, physical_cores=32, ram_mb=131_072)


# --- 1a: detection ----------------------------------------------------------


def test_detection_answers_without_raising() -> None:
    """It runs at startup on machines nobody has seen."""
    profile = detect()

    assert profile.logical_processors >= 1
    assert isinstance(profile.unknowns, tuple)


def test_what_cannot_be_detected_is_named() -> None:
    """A silent gap in a profile is a wrong number waiting to be believed by
    everything that derives from it."""
    profile = detect(index_path=None)

    # No index path was given, so the disk cannot be classified - and that must
    # show as an empty answer rather than as a confident guess.
    assert profile.index_disk == ""


def test_the_fingerprint_follows_the_machine_not_the_moment() -> None:
    """Free space changes hourly; a cache invalidated by a download is a cache
    that is never warm."""
    from dataclasses import replace

    assert OWNER.fingerprint() == replace(OWNER).fingerprint()
    assert OWNER.fingerprint() != replace(OWNER, ram_mb=16_000).fingerprint()
    assert OWNER.fingerprint() != replace(OWNER, performance_cores=4).fingerprint()


def test_a_hybrid_is_recognised_as_one() -> None:
    assert OWNER.hybrid
    assert not SMALL.hybrid, "no split reported means no split claimed"


def test_a_profile_survives_a_round_trip() -> None:
    with_gpu = ComputeProfile(
        logical_processors=8, physical_cores=8, ram_mb=16_000,
        gpus=(GpuAdapter(name="Intel Iris Xe", vram_mb=128, directml=True),))

    back = ComputeProfile.from_dict(json.loads(json.dumps(with_gpu.as_dict())))

    assert back == with_gpu
    assert back.directml_available


def test_rubbish_is_not_a_profile() -> None:
    for payload in (None, [], {"logical_processors": "lots"}, {"nope": 1}):
        result = ComputeProfile.from_dict(payload)
        assert result is None or isinstance(result, ComputeProfile)


# --- 1c: the escape hatch ---------------------------------------------------


def test_an_override_replaces_detection_and_says_so(tmp_path, monkeypatch) -> None:
    """The only file-edited tunable in this order, and it exists because when
    detection is wrong nothing downstream can be argued with."""
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(BIG.as_dict()), encoding="utf-8")
    monkeypatch.setenv(OVERRIDE_ENV, str(path))

    profile = detect()

    assert profile.physical_cores == 32
    assert profile.overridden, "an override that does not announce itself is a "\
        "mystery that costs an afternoon"


def test_an_unreadable_override_falls_back_to_detection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(OVERRIDE_ENV, str(tmp_path / "missing.json"))

    profile = detect()

    assert not profile.overridden
    assert profile.logical_processors >= 1


# --- the cache, and the future-machine story --------------------------------


def test_the_profile_is_cached(tmp_path) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        first = cached_profile(store)
        assert store.get_state(PROFILE_STATE_KEY, "")
        assert cached_profile(store).fingerprint() == first.fingerprint()


def test_a_different_machine_re_derives_everything(tmp_path) -> None:
    """The same index folder opened on another box must not keep the first
    one's numbers - that is what the fingerprint is for."""
    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(PROFILE_STATE_KEY, json.dumps(BIG.as_dict()))

        profile = cached_profile(store)

        assert profile.fingerprint() != BIG.fingerprint()
        assert profile.physical_cores != 32 or profile.ram_mb != BIG.ram_mb


# --- 3a: the envelope -------------------------------------------------------


@pytest.mark.parametrize("profile", [OWNER, SMALL, BIG])
def test_auto_matches_the_default_that_is_running_today(profile) -> None:
    r"""**The regression this guards against is a silent halving.**

    The first version divided weighted capacity and produced two workers on the
    owner's 2P+8E machine, against the four `default_workers` has been using -
    on the strength of an E-core discount borrowed from inference, which is not
    what an extraction worker does. Extraction waits on the disk as much as it
    computes.
    """
    assert env.index_workers(profile).auto == default_workers(
        profile.physical_cores)


def test_inference_threads_are_weighted_because_that_work_is_not() -> None:
    """The distinction §0 draws: a thread doing inference on an E-core really
    does deliver a fraction of a P-core's throughput."""
    assert env.capacity(OWNER) == pytest.approx(5.2)
    assert env.capacity(OWNER) < OWNER.physical_cores

    flat = ComputeProfile(logical_processors=12, physical_cores=10,
                          ram_mb=32460)
    assert env.capacity(flat) == 10, "no reported split means no discount"


def test_the_ceiling_is_the_machine_not_a_constant() -> None:
    assert env.index_workers(SMALL).ceiling == 2
    assert env.index_workers(BIG).ceiling == 32


def test_every_bound_carries_a_reason() -> None:
    """A ceiling somebody cannot account for is one they route around by
    editing a file."""
    for build in env.ENVELOPES.values():
        bounds = build(OWNER)
        assert bounds.why.strip()
        assert bounds.floor <= bounds.auto <= bounds.ceiling


def test_memory_bounds_follow_the_memory() -> None:
    assert env.index_memory_mb(SMALL).auto < env.index_memory_mb(OWNER).auto
    assert env.embed_batch(SMALL).auto < env.embed_batch(BIG).auto


def test_an_unknown_machine_still_produces_usable_bounds() -> None:
    """Detection failing must not mean an indexer that cannot start."""
    nothing = ComputeProfile()

    for build in env.ENVELOPES.values():
        bounds = build(nothing)
        assert bounds.auto >= 1
        assert bounds.ceiling >= bounds.floor


# --- 3b: intent, and clamping that says so ----------------------------------


def test_auto_is_the_stored_intent_not_a_number() -> None:
    r"""Storing the derived value would freeze this machine's answer into the
    index, and the same index on another box would run on the old arithmetic."""
    bounds = env.index_workers(OWNER)

    assert bounds.resolve(env.AUTO) == (bounds.auto, None)
    assert bounds.resolve(None) == (bounds.auto, None)


def test_an_explicit_value_stands_when_it_fits() -> None:
    bounds = env.index_workers(BIG)

    assert bounds.resolve(12) == (12, None)


def test_a_clamp_names_the_old_value_the_new_one_and_the_reason() -> None:
    r"""This fires exactly when somebody has carried an index to a smaller
    machine and is wondering why - so "your setting was changed" alone is a
    message that teaches nobody anything."""
    value, notice = env.index_workers(SMALL).clamp(99)

    assert value == 2
    assert notice and "99" in notice and "2" in notice
    assert "core" in notice


def test_moving_to_a_bigger_machine_leaves_values_alone() -> None:
    """Ceilings rise; nothing is rewritten."""
    assert env.index_workers(BIG).clamp(8) == (8, None)
    assert env.index_workers(SMALL).clamp(8)[0] == 2


def test_a_value_that_is_not_a_number_falls_back_to_auto() -> None:
    bounds = env.index_workers(OWNER)

    assert bounds.clamp("plenty") == (bounds.auto, None)


# --- 3c: warn, do not block -------------------------------------------------


def test_oversubscription_is_named_with_its_numbers() -> None:
    warning = env.oversubscription_warning(OWNER, 9, 8)

    assert warning
    assert "9" in warning and "8" in warning and "12" in warning


def test_a_configuration_that_fits_says_nothing() -> None:
    assert env.oversubscription_warning(OWNER, 3, 4) is None


def test_it_warns_rather_than_refusing() -> None:
    """A suboptimal machine configuration is the person's right, informed.
    What is not their right is being slower and not knowing."""
    assert isinstance(env.oversubscription_warning(OWNER, 20, 20), str)


def test_a_setting_with_no_machine_bound_has_no_envelope() -> None:
    """A theme or a schedule does not depend on the hardware, and the caller
    shows the registry's static bounds for those."""
    assert env.for_setting("INDEX_SCHEDULE", OWNER) is None
    assert env.for_setting("INDEX_WORKERS", OWNER) is not None


# --- 2026-09-08: a probe that did not run is not a probe that found nothing --
#
# From `logs/runs/run-20260908-055844-window.log`: at 06:06:38, under 80-95%
# CPU, OCR ran "on cpu - the processor, because no display adapter was
# detected" while the embedder in the same process had loaded on the GPU at
# startup. The PowerShell adapter probe had timed out; the empty answer was
# indistinguishable from "no graphics card".


import subprocess
import sys

from app.core import compute_profile as cp

IRIS = (GpuAdapter(name="Intel Iris Xe", vram_mb=128),)


@pytest.fixture
def windows_with_no_earlier_answer(monkeypatch):
    """Pretend to be Windows with nothing remembered from an earlier probe.
    Everything else `detect()` asks on win32 (topology, AVX2) fails through
    its own guard on Linux and is recorded as an unknown, which is fine."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cp, "_last_known_adapters", None)
    monkeypatch.setattr(cp, "_directml_provider_available", lambda: True)


def _timing_out(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="powershell", timeout=kwargs.get("timeout", 15))


class _Done:
    returncode = 0
    stdout = '{"Name":"Intel Iris Xe","AdapterRAM":134217728}'


@pytest.fixture
def warnings(monkeypatch):
    from app.core.logging import logger

    lines: list[str] = []
    handle = logger.add(lambda m: lines.append(m.record["message"]),
                        level="WARNING", format="{message}")
    yield lines
    logger.remove(handle)


def test_a_timed_out_probe_is_flagged_not_reported_as_no_adapters(
        windows_with_no_earlier_answer, monkeypatch, warnings) -> None:
    monkeypatch.setattr(cp.subprocess, "run", _timing_out)

    profile = detect()

    assert profile.gpus == ()
    assert profile.gpu_probe_failed, "an empty answer from a probe that did " \
        "not run must not look like a machine with no graphics card"
    assert "display adapters" in profile.unknowns
    assert any("timed out after 15s" in line for line in warnings)


def test_a_timed_out_probe_reuses_the_answer_from_an_earlier_one(
        windows_with_no_earlier_answer, monkeypatch, warnings) -> None:
    """The embedder, OCR and the image model each call `detect()`. When the
    first probe worked and a later one times out under load, the later one
    must get the same answer - the machine did not lose its graphics card."""
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Done())
    first = detect()
    assert [g.name for g in first.gpus] == ["Intel Iris Xe"]
    assert not first.gpu_probe_failed

    monkeypatch.setattr(cp.subprocess, "run", _timing_out)
    later = detect()

    assert [g.name for g in later.gpus] == ["Intel Iris Xe"]
    assert later.directml_available
    assert later.gpu_probe_failed, "still says the card was not re-checked"
    assert later.fingerprint() == first.fingerprint()
    assert any("timed out after 15s - using the last known answer (1 adapters)"
               in line for line in warnings)


def test_a_probe_that_ran_and_found_nothing_is_not_a_failure(
        windows_with_no_earlier_answer, monkeypatch) -> None:
    class Nothing:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: Nothing())

    profile = detect()

    assert profile.gpus == ()
    assert not profile.gpu_probe_failed


def test_the_cache_keeps_stored_adapters_when_the_probe_could_not_run(
        tmp_path, monkeypatch, warnings) -> None:
    """Before this, the empty `gpus` changed the fingerprint, the store read
    as "a different machine", and the cache was overwritten with a GPU-less
    profile - a timeout deleting a hardware fact."""
    from dataclasses import replace

    stored = replace(OWNER, gpus=IRIS)
    unchecked = replace(OWNER, gpus=(), gpu_probe_failed=True,
                        unknowns=("display adapters",))
    monkeypatch.setattr(cp, "detect", lambda index_path=None: unchecked)

    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(PROFILE_STATE_KEY, json.dumps(stored.as_dict()))

        profile = cached_profile(store)

        assert profile.gpus == IRIS
        assert profile.fingerprint() == stored.fingerprint()
        kept = ComputeProfile.from_dict(json.loads(store.get_state(PROFILE_STATE_KEY, "")))
        assert kept is not None and kept.gpus == IRIS, "the cache was not overwritten"
    assert any("using the last known answer from the stored profile (1 adapters)"
               in line for line in warnings)


def test_the_cache_does_not_invent_adapters_it_never_had(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    unchecked = replace(OWNER, gpus=(), gpu_probe_failed=True)
    monkeypatch.setattr(cp, "detect", lambda index_path=None: unchecked)

    with SqliteStore(tmp_path / "index.db") as store:
        profile = cached_profile(store)

    assert profile.gpus == ()
    assert profile.gpu_probe_failed

    from app.index.backends import why_unavailable

    assert why_unavailable(profile) == "the graphics card check could not run"


def test_a_profile_written_before_the_flag_existed_still_loads() -> None:
    payload = OWNER.as_dict()
    del payload["gpu_probe_failed"]

    back = ComputeProfile.from_dict(payload)

    assert back is not None
    assert back.gpu_probe_failed is False
    assert back.fingerprint() == OWNER.fingerprint()
