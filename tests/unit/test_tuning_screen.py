r"""Index tuning §4: the screen, and the promises it makes about your settings.

The rules under test are the ones a tuning screen breaks by accident.

**It must not write settings while it is being built.** The first working
version wrote `INDEX_WORKERS=1` and `EMBED_DEVICE=auto` into `.env` on the way
to being shown: narrowing a spin box's range clamps its value, clamping emits
`valueChanged`, and the debounced writer duly saved a number nobody chose. On a
machine whose cores could not be detected the ceiling is one core, so somebody's
stored six became one just by opening the window. Both tests below exist because
that happened.

**An unknown machine means no local limit, not a limit of one.** Detection is
asynchronous, so for the first moments there is no profile at all - and treating
a missing profile as a machine with zero cores is how the above got its `1`.

**Switching modes has to be reversible**, or nobody will try Manual. A value
typed there is kept, stored but inert, when the mode moves away from it.

Most of the wording is asserted in `test_tuning.py` against `app/ui/tuning.py`,
which is Qt-free. What is here is the behaviour that only exists once the
widgets do.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the tuning screen is Qt")

from app.core.compute_profile import ComputeProfile, GpuAdapter   # noqa: E402
from app.ui.widgets.tuning_box import TuningBox                   # noqa: E402
from app.ui.widgets.tuning_groups import ComputeBox, ResourcesBox  # noqa: E402

TEN_CORES = ComputeProfile(
    logical_processors=12, physical_cores=10, performance_cores=2,
    efficiency_cores=8, ram_mb=32 * 1024, avx2=True, index_disk="ssd")

WITH_GPU = ComputeProfile(
    logical_processors=16, physical_cores=8, ram_mb=16 * 1024, avx2=True,
    index_disk="ssd",
    gpus=(GpuAdapter(name="Iris Xe", vram_mb=0, directml=True),))


class Stored:
    """A `Settings`, as much of one as the screen reads."""

    embed_device = "auto"
    index_workers = 6
    onnx_intra_op_threads = 0
    embed_batch = 0
    embed_quantised = False
    index_memory_mb = 4000
    index_cpu_percent = 80
    min_free_gb = 5
    required_free_gb = 300
    index_low_priority = True
    index_pause_on_battery = True
    index_two_phase = True
    embed_dedup = True
    index_bulk_fts = "auto"
    index_ocr_pass = "with-run"
    index_tuning_mode = "defaults"
    index_ocr_mode = "both"
    archive_recheck_days = 30
    index_name_only = True
    archive_read_inside = True
    archive_max_mb = 100
    pdf_ocr_pages = 0


@pytest.fixture()
def box(qtbot=None):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    return TuningBox(Stored())


# --- the bug that wrote to .env on the way in -------------------------------


def test_building_the_screen_saves_nothing(box) -> None:
    """**The one that must never regress.** A settings panel whose mere
    existence rewrites settings is worse than one with no controls at all."""
    saved: list = []
    box.changed.connect(saved.append)
    box.coverage_changed.connect(saved.append)
    box.flush_pending()

    assert saved == []


def test_a_machine_arriving_saves_nothing(box) -> None:
    """Detection lands seconds after the window opens, and re-bounds every
    control. That is a clamp on every value, which is an emission on every
    value, which was the second half of the same bug."""
    saved: list = []
    box.changed.connect(saved.append)

    box.set_profile(TEN_CORES, free_gb=400)
    box.flush_pending()

    assert saved == []


def test_an_undetected_machine_does_not_clamp_a_stored_value(box) -> None:
    r"""`envelope.for_setting` answers for *any* object: missing fields read as
    zero, and a machine with zero cores earns a ceiling of one. Asking it
    before detection has answered showed `1` over a stored `6` - and then
    swallowed the next edit, because setting 3 on a range of (0, 1) clamps to 1
    and emits nothing at all."""
    assert box.compute.workers.value() == 6
    assert box.compute.workers.maximum() > 1


def test_a_detected_machine_narrows_the_ceiling(box) -> None:
    box.set_profile(TEN_CORES, free_gb=400)

    assert box.compute.workers.maximum() == 10
    assert box.compute.workers.value() == 6, "and keeps what was stored"


# --- 4a: three modes, and switching is reversible ---------------------------


def test_manual_is_the_only_mode_that_lets_you_type(box) -> None:
    box.set_profile(TEN_CORES)

    box.mode.setCurrentIndex(box.mode.findData("manual"))
    assert box.compute.workers.isEnabled()

    box.mode.setCurrentIndex(box.mode.findData("defaults"))
    assert not box.compute.workers.isEnabled()


def test_leaving_manual_keeps_the_value_stored_but_inert(box) -> None:
    """**The condition for having a Manual mode at all.** Somebody tries it,
    sets four things, decides against it, and gets their four things back by
    switching to Manual again - rather than by remembering them."""
    box.set_profile(TEN_CORES)
    box.mode.setCurrentIndex(box.mode.findData("manual"))
    box.compute.workers.setValue(7)

    box.mode.setCurrentIndex(box.mode.findData("defaults"))

    assert box.compute.workers.value() == 7
    assert box.compute.workers.specialValueText() == "Auto (4)", (
        "and the resolved value is what is shown")


def test_the_mode_is_itself_saved(box) -> None:
    """Otherwise Manual silently reverts on the next start, which is exactly
    the quiet undo that makes people distrust a settings screen."""
    saved: list = []
    box.changed.connect(saved.append)

    box.mode.setCurrentIndex(box.mode.findData("manual"))

    assert {"INDEX_TUNING_MODE": "manual"} in saved


def test_auto_says_it_has_nothing_measured_yet(box) -> None:
    """Auto-tune exists before §5 has measured anything. Resolving exactly as
    Defaults does and saying so is honest; implying a precision it has not got
    is not."""
    from app.ui.tuning import resolve

    _value, why = resolve("INDEX_WORKERS", "auto", 0, TEN_CORES)

    assert "nothing measured yet" in why


# --- 4c: the resolved value is visible without changing mode ----------------


def test_auto_shows_the_number_it_chose(box) -> None:
    """`0` meaning "we decided something and are not telling you what" is the
    settings-screen failure this whole order exists to end."""
    box.set_profile(TEN_CORES)

    assert box.compute.workers.specialValueText() == "Auto (4)"
    assert box.compute.batch.specialValueText() == "Auto (256)"


# --- 4c/4e: greyed with the reason, warned inline ---------------------------


def test_the_graphics_card_is_greyed_with_its_reason(box) -> None:
    box.set_profile(TEN_CORES)                    # no adapter at all
    index = box.compute.embed_device.findData("gpu")
    item = box.compute.embed_device.model().item(index)

    assert not item.isEnabled()
    assert "no display adapter" in item.toolTip()


def test_a_usable_graphics_card_is_offered(box) -> None:
    box.set_profile(WITH_GPU)
    index = box.compute.embed_device.findData("gpu")

    assert box.compute.embed_device.model().item(index).isEnabled()


def test_the_smaller_model_file_is_greyed_on_a_graphics_card(box) -> None:
    """Quantisation buys nothing there, and a checkbox that does nothing is
    worse than one that says why."""
    box.set_profile(WITH_GPU)
    box.compute.embed_device.setCurrentIndex(
        box.compute.embed_device.findData("gpu"))

    assert not box.compute.quantised.isEnabled()
    assert "no gain on a graphics card" in box.compute.quantised.toolTip()


def test_oversubscription_warns_inline_and_never_blocks(box) -> None:
    """§3c: a suboptimal machine is the person's right, informed. What is not
    their right is being slower and not knowing. A modal at that moment would
    interrupt the very comparison they are making."""
    box.set_profile(TEN_CORES)
    box.mode.setCurrentIndex(box.mode.findData("manual"))
    box.compute.workers.setValue(10)
    box.compute.threads.setValue(12)
    box.compute._warn(TEN_CORES)                  # noqa: SLF001 - same package

    # `isVisibleTo`, not `isVisible`: nothing here is on screen, and a widget
    # inside an unshown parent reports invisible whatever it was asked to be.
    assert box.compute.warning.isVisibleTo(box.compute)
    assert "slower, not faster" in box.compute.warning.text()
    assert box.compute.workers.value() == 10, "warned, not overruled"


# --- 4c-2: a floor larger than the disk was settable ------------------------


def test_the_free_space_floors_cannot_exceed_the_drive() -> None:
    """A floor larger than the disk stops every run on a machine that is
    working perfectly well."""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    resources = ResourcesBox(Stored())

    resources.apply_profile(TEN_CORES, free_gb=120)

    assert resources.min_free_gb.maximum() == 120
    assert resources.required_free_gb.maximum() == 120
    assert "120 GB free" in resources.note.text()


def test_every_ceiling_says_what_happens_when_it_is_reached() -> None:
    """The `indexing_settings.py` rule, inherited: a ceiling that pauses must
    say "pauses", or somebody sets it far too high out of fear of losing a run
    and it protects nothing."""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    resources = ResourcesBox(Stored())

    assert "PAUSES" in resources.memory_mb.toolTip()
    assert "PAUSE" in resources.cpu_percent.toolTip()
    assert "STOPS" in resources.min_free_gb.toolTip()


# --- the values reaching the writer are registry keys -----------------------


def test_the_compute_group_emits_registry_keys() -> None:
    """The window turns them into `.env` lines by upper-casing the field name,
    so a key that is not the registry's is a setting that silently goes
    nowhere - which is exactly the U6 failure the coverage tests exist for."""
    from PySide6.QtWidgets import QApplication

    from app.core.settings_registry import keys

    QApplication.instance() or QApplication([])
    declared = set(keys())

    assert set(ComputeBox(Stored()).values()) <= declared
    assert set(ResourcesBox(Stored()).values()) <= declared
