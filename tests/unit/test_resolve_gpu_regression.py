r"""Work order 202626130120 (0t) section 6, wired into resolve_for_run.

resolve.gpu_regression is the one place both the CLI and the window
already resolve the machine off the UI thread, before a Pipeline is even
built - see resolve.py's own docstring for why the notice has to be
computed there rather than from cached_profile's return value.
"""

from __future__ import annotations

import json

import pytest

from app.core.compute_profile import ComputeProfile, GpuAdapter, PROFILE_STATE_KEY
from app.index import resolve
from app.index.resolve import resolve_for_run


@pytest.fixture(autouse=True)
def _this_machine_is_already_tested(monkeypatch):
    r"""No real device test inside a resolve.

    **119 s for four tests (order 1u, 2026-10-13).** `resolve_for_run` first calls
    `test_this_machine_if_new`, and `_Settings` below has no `state_path`, so the
    stored result can never be found and every call counted as an untested
    machine. On a laptop with a usable graphics card each of the four calls then
    ran every model on the processor and the card for real - about 30 s each - and
    the result was saved nowhere. What this file checks is the lost-provider
    notice; the once-per-machine test is pinned in `test_model_devices.py`
    (`test_a_run_tests_an_untested_machine_only_when_run_models_on_is_automatic`).
    """
    monkeypatch.setattr(resolve, "test_this_machine_if_new", lambda settings: False)


class _Store:
    def __init__(self, state=None):
        self._state = dict(state or {})

    def get_state(self, key, default=""):
        return self._state.get(key, default)

    def set_state(self, key, value):
        self._state[key] = value


class _Settings:
    index_tuning_mode = "defaults"
    index_workers = 0
    onnx_intra_op_threads = 0
    embed_batch = 0
    data_path = None


HAD_DML = ComputeProfile(
    logical_processors=4, physical_cores=4, ram_mb=8192,
    gpus=(GpuAdapter(name="Intel Iris Xe", directml=True),))


def _store_with(profile: ComputeProfile) -> _Store:
    return _Store({PROFILE_STATE_KEY: json.dumps(profile.as_dict())})


def test_store_none_is_unaffected_exactly_as_every_existing_caller_needs(
) -> None:
    """Every test in test_index_tuning_acceptance.py calls with store=None
    and an invented profile= - this must stay exactly as quiet as before."""
    found = resolve_for_run(_Settings(), store=None, profile=HAD_DML)

    assert found.gpu_regression_notice == ""
    assert "gpu_lost" not in found.why


def test_a_machine_that_lost_the_provider_gets_the_notice(monkeypatch) -> None:
    import app.core.compute_profile as compute_profile_module

    lost = ComputeProfile(
        logical_processors=4, physical_cores=4, ram_mb=8192,
        gpus=(GpuAdapter(name="Intel Iris Xe", directml=False),))
    monkeypatch.setattr(
        compute_profile_module, "detect", lambda index_path=None: lost)

    store = _store_with(HAD_DML)
    found = resolve_for_run(_Settings(), store=store, profile=HAD_DML)

    assert found.gpu_regression_notice
    assert "Intel Iris Xe" in found.gpu_regression_notice
    assert found.why["gpu_lost"] == found.gpu_regression_notice


def test_a_machine_that_never_had_it_stays_quiet_with_a_real_store(
    monkeypatch,
) -> None:
    """No cached profile at all - a fresh install, not a regression."""
    import app.core.compute_profile as compute_profile_module

    no_gpu = ComputeProfile(logical_processors=4, physical_cores=4, ram_mb=8192)
    monkeypatch.setattr(
        compute_profile_module, "detect", lambda index_path=None: no_gpu)

    store = _Store()
    found = resolve_for_run(_Settings(), store=store, profile=no_gpu)

    assert found.gpu_regression_notice == ""


def test_a_broken_store_never_fails_the_resolve(monkeypatch) -> None:
    """A notice is not worth a run - see resolve._gpu_regression's own
    docstring."""
    class _BrokenStore:
        def get_state(self, key, default=""):
            raise RuntimeError("the database is locked")

    found = resolve_for_run(_Settings(), store=_BrokenStore(), profile=HAD_DML)

    assert found.gpu_regression_notice == ""
    assert found.workers >= 1, "a broken gpu check must not break tuning"

