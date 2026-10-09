"""Record how long each test file took, so `run_suite.py` can balance the next run.

Layer: L0 - repo tooling, no app imports.

A pytest plugin, loaded by the runner with `-p scripts.suite_durations`. It sums the
setup, call and teardown durations of every test by the file it lives in, keeps every
test that took a second or more together with whether it was marked `slow` (the
marker's own definition in `pyproject.toml` is "takes more than a second"), and at the
end of the session writes it all as JSON to the path in `LEASHA_SUITE_DURATIONS`.
Without that variable the plugin registers nothing, so `pytest tests/unit/test_x.py`
by hand is unchanged.

A part that crashes never reaches `pytest_sessionfinish` and writes nothing; the
runner keeps the previous numbers for its files. That is the right outcome: a crash
says nothing about how long the files would have taken.

Work order `suite-speed` §1a.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

#: The environment variable naming the file to write. The runner sets one per part.
ENV_VAR = "LEASHA_SUITE_DURATIONS"

#: A test at or over this many seconds is listed by name, with its `slow` mark.
SLOW_SECONDS = 1.0


class Recorder:
    """The plugin object: pytest calls its hook methods for the session it is registered in."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.by_file: dict[str, float] = defaultdict(float)
        self.by_test: dict[str, float] = defaultdict(float)
        self.marked_slow: set[str] = set()
        self.started = time.time()

    def add(self, nodeid: str, seconds: float) -> None:
        self.by_file[nodeid.split("::", 1)[0]] += seconds
        self.by_test[nodeid] += seconds

    def payload(self) -> dict:
        slow = {name: round(seconds, 3) for name, seconds in self.by_test.items()
                if seconds >= SLOW_SECONDS}
        return {
            "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed": round(time.time() - self.started, 1),
            "files": {name: round(seconds, 3) for name, seconds in sorted(self.by_file.items())},
            "slow_tests": {name: {"seconds": seconds, "marked": name in self.marked_slow}
                           for name, seconds in sorted(slow.items(), key=lambda kv: -kv[1])},
        }

    # -- pytest hooks -------------------------------------------------------------------

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            if "slow" in item.keywords:
                self.marked_slow.add(item.nodeid)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Three reports per test (setup, call, teardown); their durations add up.
        self.add(report.nodeid, float(getattr(report, "duration", 0.0) or 0.0))

    def pytest_sessionfinish(self) -> None:
        write_json(self.target, self.payload())


def write_json(target: Path, payload: dict) -> None:
    """Write whole or not at all: a reader never sees half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(target)


def pytest_configure(config: pytest.Config) -> None:
    target = os.environ.get(ENV_VAR, "").strip()
    if target:
        config.pluginmanager.register(Recorder(Path(target)), "leasha-suite-durations")
