"""The suite does not depend on whether the laptop is plugged in.

2026-10-05: with the laptop on battery, every test that starts a real index
run waited for mains power until its time limit killed it. `tests/conftest.py`
now tells every test the machine is plugged in.
"""

from __future__ import annotations


def test_every_test_sees_mains_power():
    import psutil

    assert psutil.sensors_battery().power_plugged is True


def test_so_the_governor_does_not_pause_a_tests_run_for_the_battery():
    from app.index.resources import SystemProbe

    assert SystemProbe().read().on_battery is False


def test_a_test_about_the_battery_can_still_say_otherwise(monkeypatch):
    import psutil

    class _OnBattery:
        percent = 40.0
        secsleft = 3600
        power_plugged = False

    monkeypatch.setattr(psutil, "sensors_battery", lambda: _OnBattery())
    assert psutil.sensors_battery().power_plugged is False
