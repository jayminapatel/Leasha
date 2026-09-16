r"""Work order 0 section 6, P8: rerank off the critical path, or off by
default until it is async.

Layer: L0 (the setting) / L4 (the reranker own reading of it) / L5 (the
toolbar and Settings controls).

Closed on the latency half of the evidence, per the P8 note the work
order already carries: the owner own rerank-bench run measured 0.46s per
search against a 300ms warm budget for the whole search, on the smallest of
four models measured - already over budget before anything here changed. A
second run on this machine (2026-09-15) came back at 8.03s, UNSTABLE, on a
build machine under heavy concurrent load - not a clean number, but not one
that argues the other way either. Building the asynchronous two-phase path
(fused results paint first, the reorder arrives after and is shown) is a
results-surface change that belongs with the search-experience order; this
is the other half the note names - default it off, leave the toolbar switch
exactly where M12 already put it.

Every layer that used to assume on gets its own test here, because the
default has shipped wrong before: RERANK_MODEL was pinned into every
install for a year after the code default moved past it (see
test_setting_defaults.py), and a default that is only right in one of
four places is not right.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.config import Settings, load_settings
from app.core.settings_registry import by_key
from app.search.rerank import Reranker


def test_the_settings_dataclass_defaults_to_off():
    assert Settings.model_fields["rerank_enabled"].default is False


def _env_dir():
    tmp_path = Path(tempfile.mkdtemp())
    data = tmp_path / "data"
    project = tmp_path / "project"
    data.mkdir()
    project.mkdir()
    return tmp_path, data, project


def test_load_settings_defaults_to_off_when_unset():
    tmp_path, data, project = _env_dir()
    env_file = tmp_path / ".env"
    body = ["DATA_PATH=" + data.as_posix(), "PROJECT_PATH=" + project.as_posix()]
    env_file.write_text(chr(10).join(body) + chr(10), encoding="utf-8")

    settings = load_settings(env_file)

    assert settings.rerank_enabled is False


def test_an_explicit_env_value_still_wins():
    tmp_path, data, project = _env_dir()
    env_file = tmp_path / ".env"
    body = ["DATA_PATH=" + data.as_posix(), "PROJECT_PATH=" + project.as_posix(),
            "RERANK_ENABLED=true"]
    env_file.write_text(chr(10).join(body) + chr(10), encoding="utf-8")

    settings = load_settings(env_file)

    assert settings.rerank_enabled is True


def test_the_settings_registry_entry_defaults_to_off():
    setting = by_key("RERANK_ENABLED")
    assert setting is not None
    assert setting.default is False


def test_the_reranker_reads_the_new_default_when_settings_lacks_the_field():
    class _BareSettings:
        pass

    reranker = Reranker.from_settings(_BareSettings())

    assert reranker.enabled is False


def test_the_toolbar_checkbox_starts_unchecked(qtbot):
    from app.ui.widgets.search_bar import build_rerank

    box = build_rerank(None, lambda checked: None)
    qtbot.addWidget(box)

    assert box.isChecked() is False
