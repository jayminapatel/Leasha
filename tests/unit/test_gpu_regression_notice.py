r"""Work order 202626130120 (0t) section 7, item 3: the three-way decision.

provider present, provider lost, probe failed. The middle one warns; the
other two are silent - and so is a fourth case worth naming separately,
a machine that never had a working provider at all.
"""

from __future__ import annotations

from app.index import backends


class _Gpu:
    def __init__(self, name):
        self.name = name


class _Machine:
    def __init__(self, gpus=(), directml=False, probe_failed=False):
        self.gpus = gpus
        self.directml_available = directml
        self.gpu_probe_failed = probe_failed


HAD_IT = _Machine(gpus=(_Gpu("Intel Iris Xe"),), directml=True)


def test_still_has_it_is_silent() -> None:
    still_has = _Machine(gpus=(_Gpu("Intel Iris Xe"),), directml=True)

    assert backends.gpu_regression_notice(HAD_IT, still_has) == ""


def test_had_it_and_lost_it_fires() -> None:
    """The one case this exists for: the machine from the work order's
    own evidence."""
    lost = _Machine(gpus=(), directml=False)

    notice = backends.gpu_regression_notice(HAD_IT, lost)

    assert notice
    assert "Intel Iris Xe" in notice
    assert "onnxruntime-directml" in notice
    assert backends.DIRECTML_PIN in notice


def test_never_had_it_is_silent_even_with_no_gpu_now() -> None:
    """Must not fire for a machine that never had a GPU - the work order's
    own section 6 wording."""
    never_had = _Machine()
    still_none = _Machine()

    assert backends.gpu_regression_notice(never_had, still_none) == ""


def test_never_had_it_is_silent_even_if_a_gpu_shows_up_now() -> None:
    """Gaining a card is not a regression - nothing to warn about."""
    never_had = _Machine()
    gained = _Machine(gpus=(_Gpu("New Card"),), directml=True)

    assert backends.gpu_regression_notice(never_had, gained) == ""


def test_a_failed_probe_is_silent_not_a_loss() -> None:
    """'Could not look' is not 'it is gone' - compute_profile._gpus
    returns gpu_probe_failed for exactly this reason, and a false alarm on
    a busy machine would be worse than saying nothing."""
    could_not_look = _Machine(gpus=(), directml=False, probe_failed=True)

    assert backends.gpu_regression_notice(HAD_IT, could_not_look) == ""


def test_no_stored_profile_at_all_is_silent() -> None:
    """A fresh machine with nothing cached yet - not a regression, a first
    run."""
    lost = _Machine(gpus=(), directml=False)

    assert backends.gpu_regression_notice(None, lost) == ""


def test_a_missing_fresh_profile_is_silent() -> None:
    assert backends.gpu_regression_notice(HAD_IT, None) == ""


def test_the_fix_command_is_exact_and_copyable() -> None:
    """A command somebody can paste, not a description of one - matching
    every other Fix in this codebase's error contract."""
    lost = _Machine(gpus=(), directml=False)

    notice = backends.gpu_regression_notice(HAD_IT, lost)

    assert "pip install --force-reinstall --no-deps" in notice

