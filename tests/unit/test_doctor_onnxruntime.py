r"""Work order 202626130120 (0t) section 7, item 2: the doctor checks.

Against a fabricated site-packages listing only - no real venv, no
network - per the order's own section 8 (what cannot be checked from a
sandbox). doctor.check_onnxruntime_integrity takes site_packages as a
parameter for exactly this reason.
"""

from __future__ import annotations

from pathlib import Path

import doctor


def test_a_single_onnxruntime_install_is_clean(tmp_path: Path) -> None:
    (tmp_path / "onnxruntime-1.24.4.dist-info").mkdir()

    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is True
    assert check.optional is False, "this is a required check, not a WARN"


def test_nothing_installed_yet_is_also_clean(tmp_path: Path) -> None:
    """Before requirements.txt has ever been installed - must not read as
    a fault."""
    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is True


def test_mismatched_versions_fails(tmp_path: Path) -> None:
    """The exact fault from the work order's section 0: the plain wheel
    landed over the DirectML one, at a different version, and both
    dist-infos survived."""
    (tmp_path / "onnxruntime-1.30.0.dist-info").mkdir()
    (tmp_path / "onnxruntime_directml-1.24.4.dist-info").mkdir()

    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is False
    assert "different versions" in check.detail
    assert "1.30.0" in check.detail and "1.24.4" in check.detail
    assert check.fix, "a required failure must carry the repair command"
    assert "force-reinstall" in check.fix


def test_two_distributions_at_the_same_pinned_version_is_healthy(
    tmp_path: Path,
) -> None:
    """section 3's own fix installs onnxruntime-directml unconditionally,
    forever, alongside the plain wheel on any machine with a display
    adapter - so a correctly-pinned DirectML machine always carries two
    dist-info folders. Failing on that would mean doctor could never
    report READY on the hardware this order exists for; verified against
    the real venv this order was built and tested in, which has exactly
    this shape after install.ps1's new section 3 step runs."""
    (tmp_path / "onnxruntime-1.24.4.dist-info").mkdir()
    (tmp_path / "onnxruntime_directml-1.24.4.dist-info").mkdir()

    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is True
    assert "1.24.4" in check.detail


def test_a_stash_remnant_from_a_failed_uninstall_fails(tmp_path: Path) -> None:
    """WinError 5: pip could not finish removing the old package because
    a running Leasha process held onnxruntime.dll open."""
    (tmp_path / "~nnxruntime").mkdir()
    (tmp_path / "~nnxruntime-1.30.0.dist-info").mkdir()
    (tmp_path / "onnxruntime_directml-1.24.4.dist-info").mkdir()

    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is False
    assert "stash" in check.detail
    assert "~nnxruntime" in check.detail


def test_an_unrelated_stash_directory_is_ignored(tmp_path: Path) -> None:
    """Only onnxruntime's own stash prefixes count - a leftover from some
    other package's failed uninstall is not this check's business."""
    (tmp_path / "~ip-cache").mkdir()
    (tmp_path / "onnxruntime-1.24.4.dist-info").mkdir()

    check = doctor.check_onnxruntime_integrity(tmp_path)

    assert check.ok is True


def test_a_missing_site_packages_directory_is_read_as_nothing_installed(
    tmp_path: Path,
) -> None:
    check = doctor.check_onnxruntime_integrity(tmp_path / "does-not-exist")

    assert check.ok is True


class _Machine:
    def __init__(self, gpus=(), directml=False, probe_failed=False):
        self.gpus = gpus
        self.directml_available = directml
        self.gpu_probe_failed = probe_failed


class _Gpu:
    def __init__(self, name):
        self.name = name


def test_gpu_provider_intent_is_silent_with_no_adapter() -> None:
    check = doctor.check_gpu_provider_intent(_Machine())

    assert check.ok is True
    assert check.optional is True


def test_gpu_provider_intent_is_silent_when_it_has_the_provider() -> None:
    machine = _Machine(gpus=(_Gpu("Iris Xe"),), directml=True)

    check = doctor.check_gpu_provider_intent(machine)

    assert check.ok is True


def test_gpu_provider_intent_warns_when_the_adapter_has_no_provider() -> None:
    """The case that could not fail a run before - doctor already printed
    this under "Machine" as text; now it is a Check that can WARN."""
    machine = _Machine(gpus=(_Gpu("Iris Xe"),), directml=False)

    check = doctor.check_gpu_provider_intent(machine)

    assert check.ok is False
    assert check.optional is True, "WARN, not a hard failure - CPU still works"
    assert "Iris Xe" in check.detail
    assert "force-reinstall" in check.fix


def test_gpu_provider_intent_is_silent_when_the_probe_could_not_run() -> None:
    """'Could not look' is not 'it is gone' - the same distinction
    app.index.backends.why_unavailable already draws for the identical
    reason."""
    machine = _Machine(probe_failed=True)

    check = doctor.check_gpu_provider_intent(machine)

    assert check.ok is True

