r"""Index tuning §4: every sentence the screen shows, tested without Qt.

The tuning screen is mostly *claims*: what this machine is, what a control
resolves to, what happens when a limit is reached, where the last run's time
went. Each is something somebody will act on, so each is asserted here rather
than inside a widget constructor where it could only be eyeballed.

**Unknowns have to read as unknown.** Detection is allowed to fail - a
locked-down machine, no PowerShell - and "0 GB" reads as a fault in the machine
rather than a gap in what was detected. Several tests below are only about that
distinction.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.compute_profile import ComputeProfile, GpuAdapter
from app.ui import tuning

ROOT = Path(__file__).resolve().parents[2]

OWNER = ComputeProfile(
    logical_processors=12, physical_cores=10, performance_cores=2,
    efficiency_cores=8, ram_mb=32 * 1024, avx2=True, index_disk="ssd")


# --- 4b: the machine, in words somebody can check against the box ----------


def test_the_machine_line_distinguishes_fast_cores_from_efficient_ones() -> None:
    r"""Ten cores that are two fast and eight slow schedule differently from
    ten of one kind - the distinction that made §0 necessary - and somebody
    comparing this line against a benchmark needs to see it."""
    line = tuning.machine_line(OWNER)

    assert "10 cores (2 fast, 8 efficient) / 12 threads" in line
    assert "32 GB" in line and "SSD" in line


def test_nothing_detected_says_unknown_rather_than_zero() -> None:
    line = tuning.machine_line(ComputeProfile())

    assert "cores unknown" in line
    assert "memory unknown" in line
    assert "0 GB" not in line, "which would read as a broken machine"


def test_a_graphics_card_that_cannot_be_used_says_so_on_the_line() -> None:
    """Present and unusable is the case that confuses people most, so it is
    said where they are looking rather than only in a greyed tooltip."""
    seen = ComputeProfile(gpus=(GpuAdapter(name="Iris Xe", vram_mb=0),))

    assert "not usable" in tuning.machine_line(seen)


def test_a_usable_graphics_card_is_named_with_its_memory() -> None:
    ready = ComputeProfile(
        gpus=(GpuAdapter(name="NVIDIA RTX 4070", vram_mb=12 * 1024,
                         directml=True),))

    line = tuning.machine_line(ready)
    assert "NVIDIA RTX 4070, 12 GB" in line
    assert "not usable" not in line


def test_a_missing_avx2_earns_a_place_on_the_line() -> None:
    """Without it the ONNX runtime falls back to slower kernels, and somebody
    wondering why an old machine is slow deserves the actual reason."""
    assert "no AVX2" in tuning.machine_line(ComputeProfile(avx2=False))


def test_an_overridden_profile_says_so_loudly() -> None:
    """`COMPUTE_PROFILE_OVERRIDE` exists for testing. A screen quietly
    describing an invented machine as though it were this one would make every
    number on it a lie."""
    assert "OVERRIDDEN" in tuning.machine_line(
        ComputeProfile(overridden=True))


# --- 4a: the mode rule, which is the whole of the mode switch ---------------


def test_defaults_and_auto_ignore_the_stored_value_without_discarding_it() -> None:
    for mode in (tuning.DEFAULTS, tuning.AUTO):
        value, _why = tuning.resolve("INDEX_WORKERS", mode, 9, OWNER)
        assert value == 4, mode


def test_manual_uses_the_stored_value_clamped_to_the_machine() -> None:
    value, why = tuning.resolve("INDEX_WORKERS", tuning.MANUAL, 99, OWNER)

    assert value == 10
    assert "99 lowered to 10" in why, "and a clamp is never silent"


def test_auto_prefers_a_measured_rate_when_there_is_one() -> None:
    """§5's territory. Until it has measured anything, Auto resolves exactly as
    Defaults does and says so - honest about a feature that is present and not
    yet informed."""
    value, why = tuning.resolve("INDEX_WORKERS", tuning.AUTO, 0, OWNER,
                                measured={"INDEX_WORKERS": 6})

    assert value == 6
    assert "measured on this machine" in why


def test_a_setting_with_no_envelope_keeps_its_stored_value() -> None:
    """Most settings do not depend on the hardware, and a mode switch has no
    business changing a schedule."""
    value, why = tuning.resolve("INDEX_SCHEDULE", tuning.DEFAULTS, 7, OWNER)

    assert (value, why) == (7, "")


def test_auto_shows_the_number_it_chose() -> None:
    assert tuning.resolved_text("INDEX_WORKERS", tuning.DEFAULTS, 0, OWNER) \
        == "Auto (4)"
    assert tuning.resolved_text("INDEX_WORKERS", tuning.MANUAL, 6, OWNER) == "6"


# --- 4e: what happens at the limit -----------------------------------------


def test_every_ceiling_names_its_consequence() -> None:
    """Somebody choosing a memory ceiling who does not know that exceeding it
    **pauses** will set it far too high out of fear of losing a run, and then
    it protects nothing at all."""
    assert "PAUSES" in tuning.limit_note("INDEX_MEMORY_MB")
    assert "PAUSES" in tuning.limit_note("INDEX_CPU_PERCENT")
    assert "STOPS" in tuning.limit_note("MIN_FREE_GB")
    assert "warning, not a refusal" in tuning.limit_note("REQUIRED_FREE_GB")


def test_a_switch_needs_no_limit_note() -> None:
    """It is its own explanation, and inventing a sentence for it would be
    noise on a screen that already has a lot of them."""
    assert tuning.limit_note("INDEX_LOW_PRIORITY") == ""


# --- 4c-3: cost hints, and the one number this project has measured --------


def test_the_ocr_cost_comes_from_the_measured_seconds_a_page() -> None:
    """3.6 seconds a page is measured. Everything without a measurement stays
    silent rather than inventing a figure - an invented cost hint is worse than
    none, because it gets quoted back."""
    assert tuning.cost_hint("INDEX_OCR_MODE") == \
        "about 600 minutes per 10,000 scanned pages"
    assert tuning.cost_hint("INDEX_MEMORY_MB") == ""


def test_a_measured_rate_replaces_the_estimate() -> None:
    hint = tuning.cost_hint("PDF_OCR_PAGES", {"ocr_seconds_per_page": 1.8})

    assert "300 minutes" in hint


# --- 4f: the footer --------------------------------------------------------


def test_the_footer_says_so_when_nothing_has_run() -> None:
    assert "nothing measured" in tuning.footer_text(None)


def test_the_footer_puts_the_largest_stage_first() -> None:
    """The stage worth acting on should not need looking for."""
    text = tuning.footer_text({
        "stages": {"extract": 41, "embed": 52, "write": 7},
        "chunks_per_minute": 2140,
        "resolved": {"workers": 4, "device": "cpu"},
    })

    assert text.index("embed 52%") < text.index("extract 41%")
    assert "2,140 chunks/min" in text
    assert "workers 4, device cpu" in text


def test_the_footer_survives_a_run_that_recorded_no_stages() -> None:
    """Which is every run until §6a starts measuring them. Saying so beats
    inventing a split."""
    text = tuning.footer_text({"chunks_per_minute": 900, "resolved": {}})

    assert "900 chunks/min" in text


def test_the_footer_never_raises_on_nonsense() -> None:
    """It is drawn from a record read off disk, and a corrupt one must not take
    the Indexing page with it."""
    for run in ({"stages": "not a mapping"}, {"stages": {"a": "x"}},
                {"resolved": 7}, {}):
        assert isinstance(tuning.footer_text(run), str)


# --- the guard --------------------------------------------------------------


def test_the_tuning_presenter_imports_no_qt() -> None:
    """The point of the split: every sentence above is testable on a machine
    with no display, which is where this suite mostly runs."""
    tree = ast.parse((ROOT / "app" / "ui" / "tuning.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("PyQt"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("PyQt"), alias.name
