"""UI phase B, Settings (2026-10-13): one search, one row, and no setting pinned at its default.

Layer: L5, offscreen. The owner: "every screen and settings look and feel like using different
apps". Three faults, each held here by a test that failed before its fix:

1. **The filter found settings but could not take you to one.** It now lists what it found, and
   Enter or a click opens the category, the fold, scrolls, focuses and lights the row.
2. **Every box laid its rows out its own way.** Every row on Settings and Settings > Accounts is
   built by `widgets/settings_row.py`, and no box calls `addRow` itself.
3. **Boxes pinned defaults in `.env`.** Phase A found the Search behaviour box saving all thirteen
   keys on any change; the owner's `.env` then held 28 more default-equal keys from the indexing,
   OCR, archive, video and MCP controls. Each box now sends only the key that moved, and the
   writer turns a default-equal value into a removal.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[2]

ENV = """\
DATA_PATH={d}
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
"""


def _qt():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _process(app, n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


@pytest.fixture()
def env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    return env


@pytest.fixture()
def settings(env_file, monkeypatch):
    """From the file alone: `config.py` lets the process environment win over `.env`, and an
    earlier test in the same run may have left a key there (`EMBED_DEVICE=cpu` did)."""
    from app.core import settings_registry as reg
    from app.core.config import load_settings

    for setting in reg.SETTINGS:
        if setting.key not in ("DATA_PATH", "PROJECT_PATH", "LOG_PATH"):
            monkeypatch.delenv(setting.key, raising=False)
    return load_settings(env_file)


# --- 3. no default pinned --------------------------------------------------------------------

#: `MODEL_CACHE`'s registry default is "" - "with the index" - and `config.py` resolves that to
#: the index's own `models` folder, so the two spellings mean the same thing.
_SAME_MEANING = {"MODEL_CACHE"}


def test_every_registry_default_is_the_default_config_uses(settings):
    """`unpin_defaults` removes a default-equal line, which is only right while the registry's
    default is what `config.py` falls back to with no line. Checked for every key."""
    from app.core import settings_registry as reg
    from app.ui.widgets.defaults import same_as_default

    differ = []
    for setting in reg.SETTINGS:
        if setting.key in reg.protected() or setting.key in _SAME_MEANING:
            continue
        name = setting.key.lower()
        if not hasattr(settings, name):
            continue
        value = getattr(settings, name)
        if not same_as_default(setting.key, value):
            differ.append(f"{setting.key}: registry {setting.default!r}, config {value!r}")
    assert not differ, "\n".join(differ)


def test_a_default_equal_value_becomes_a_removal():
    from app.ui.widgets.defaults import unpin_defaults

    got = unpin_defaults({
        "MCP_PORT": 8737, "MCP_AUTOSTART": False, "ARCHIVE_MAX_MB": "100",
        "INDEX_OCR_MODE": "both", "VIDEO_KEYFRAME_CAP": 200,            # defaults
        "INDEX_WORKERS": 4, "RERANK_ENABLED": True,                     # chosen
        "EMBED_MODEL": "BAAI/bge-small-en-v1.5",                        # protected: never removed
        "NOT_A_SETTING": "x", "SEARCH_AUTO_CHIPS": None,                # unknown; already removal
    })
    assert got == {
        "MCP_PORT": None, "MCP_AUTOSTART": None, "ARCHIVE_MAX_MB": None,
        "INDEX_OCR_MODE": None, "VIDEO_KEYFRAME_CAP": None,
        "INDEX_WORKERS": 4, "RERANK_ENABLED": True,
        "EMBED_MODEL": "BAAI/bge-small-en-v1.5",
        "NOT_A_SETTING": "x", "SEARCH_AUTO_CHIPS": None,
    }


def test_the_writer_removes_a_line_set_to_its_default(env_file, monkeypatch):
    """Through `SettingsController._settings_changed`, the one funnel every box writes by."""
    from app.core.env_writer import pinned_keys
    from app.ui import state_writes
    from app.ui.controllers import settings_controller as sc

    env_file.write_text(env_file.read_text(encoding="utf-8")
                        + "MCP_PORT=9000\nARCHIVE_MAX_MB=250\n", encoding="utf-8")
    ran = []
    monkeypatch.setattr(sc, "CallableWorker",
                        lambda fn, *args, **_k: SimpleNamespace(fn=fn, args=args))
    monkeypatch.setattr(state_writes, "start",
                        lambda worker, **_k: ran.append(worker.fn(*worker.args)))
    window = SimpleNamespace(_settings=SimpleNamespace(env_file=env_file), _show_error=print)
    sc.SettingsController._settings_changed(SimpleNamespace(_w=window), {
        "MCP_PORT": 8737, "ARCHIVE_MAX_MB": 250, "VIDEO_KEYFRAME_CAP": 200})

    assert ran, "nothing was written"
    pinned = pinned_keys(env_file)
    assert "MCP_PORT" not in pinned, "the port went back to its default and stayed pinned"
    assert "VIDEO_KEYFRAME_CAP" not in pinned, "a default was written down"
    assert "ARCHIVE_MAX_MB" in pinned


def test_the_restore_button_counts_a_default_as_unpinned(env_file):
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets.defaults import restore_button

    _qt()
    env_file.write_text(env_file.read_text(encoding="utf-8") + "MCP_PORT=9000\n", encoding="utf-8")
    holder = QWidget()
    button = restore_button(holder, env_file, lambda _v: None)
    assert "MCP_PORT" in button.toolTip()
    button.note_written({"MCP_PORT": 8737})
    assert not button.isEnabled(), "a port set back to its default still counted as pinned"


def _one_change(box, control_change, flush=None) -> list:
    sent: list = []
    box.changed.connect(sent.append)
    control_change()
    if flush is not None:
        flush()
    return sent


def test_compute_resources_and_strategy_send_only_the_key_that_moved(settings):
    from app.ui.widgets.tuning_groups import ComputeBox, ResourcesBox, StrategyBox

    _qt()
    compute = ComputeBox(settings)
    sent = _one_change(compute, lambda: compute.threads.setValue(3), compute.flush)
    assert sent == [{"ONNX_INTRA_OP_THREADS": 3}]

    resources = ResourcesBox(settings)
    sent = _one_change(resources, lambda: resources.cpu_percent.setValue(55), resources.flush)
    assert sent == [{"INDEX_CPU_PERCENT": 55}]

    strategy = StrategyBox(settings)
    sent = _one_change(strategy, lambda: strategy.dedup.setChecked(not strategy.dedup.isChecked()),
                       strategy.flush)
    assert len(sent) == 1 and set(sent[0]) == {"EMBED_DEDUP"}


def test_a_clamp_by_the_machine_is_not_sent_with_the_next_change(settings):
    """A narrowed range moves a value nobody chose; the next real change must not carry it."""
    from app.ui.widgets.tuning_groups import ResourcesBox

    _qt()
    box = ResourcesBox(settings)
    box.apply_profile(None, free_gb=2)          # floors may not exceed 2 GB free
    sent = _one_change(box, lambda: box.cpu_percent.setValue(60), box.flush)
    assert sent == [{"INDEX_CPU_PERCENT": 60}]


def test_converter_and_devices_send_only_the_key_that_moved(settings):
    from app.ui.widgets.converter_box import ConverterBox
    from app.ui.widgets.device_box import DeviceBox

    _qt()
    converter = ConverterBox(settings)
    sent = _one_change(converter, lambda: converter.timeout.setValue(200), converter.flush)
    assert sent == [{"CONVERTER_TIMEOUT_S": 200}]

    devices = DeviceBox(settings)
    combo = devices.combos["ocr"]
    sent = _one_change(devices, lambda: combo.setCurrentIndex(combo.findData("cpu")))
    assert sent == [{"DEVICE_OCR": "cpu"}]


def test_what_gets_read_relays_only_the_lever_that_moved(settings):
    from app.ui.widgets.tuning_box import TuningBox

    _qt()
    tuning = TuningBox(settings)
    sent: list = []
    tuning.coverage_changed.connect(sent.append)
    tuning.coverage.archive_max_mb.setValue(321)
    assert sent == [{"archive_max_mb": 321}], "the OCR and archive levers went with it"


def test_media_editor_and_search_send_only_the_key_that_moved(settings):
    from app.ui.widgets.editor_box import EditorBox
    from app.ui.widgets.media_box import MediaBox
    from app.ui.widgets.search_box import SearchBox

    _qt()
    media = MediaBox(settings)
    sent = _one_change(media, lambda: media.video.setChecked(True))
    assert sent == [{"VIDEO_INDEXING_ENABLED": True}]

    editor = EditorBox(settings)
    sent = _one_change(editor, lambda: editor.command.editingFinished.emit())
    assert sent == [], "leaving the command box unchanged wrote both editor keys"

    search = SearchBox(settings)
    sent = _one_change(search, lambda: search.rerank_top_n.setValue(40), search._save.flush)
    assert sent == [{"RERANK_TOP_N": 40}]


def test_leaving_the_port_box_unchanged_writes_nothing(settings):
    from app.ui.widgets.mcp_box import McpBox

    _qt()
    box = McpBox(settings)
    sent = _one_change(box, lambda: box.port.editingFinished.emit())
    assert sent == [], "a Tab past the port wrote MCP_PORT=8737 into .env"
    box.port.setValue(9001)
    box.port.editingFinished.emit()
    assert sent == [{"MCP_PORT": 9001}]


def test_a_reset_on_screen_is_what_the_box_last_sent(settings):
    """After a right-click reset the box must send the old value again if it is chosen again."""
    from app.ui.widgets.defaults import show_default
    from app.ui.widgets.tuning_groups import ResourcesBox

    _qt()
    box = ResourcesBox(settings)
    sent = _one_change(box, lambda: box.cpu_percent.setValue(55), box.flush)
    show_default(box, ["INDEX_CPU_PERCENT"])            # back to 80 on screen, signals held
    box.cpu_percent.setValue(55)
    box.flush()
    assert sent == [{"INDEX_CPU_PERCENT": 55}, {"INDEX_CPU_PERCENT": 55}]


# --- 2. one row helper -----------------------------------------------------------------------

#: Every file that lays out a settings row on Settings or Settings > Accounts.
ROW_FILES = (
    "app/ui/settings_view.py",
    "app/ui/widgets/search_box.py",
    "app/ui/widgets/search_behaviour_box.py",
    "app/ui/widgets/editor_box.py",
    "app/ui/widgets/media_box.py",
    "app/ui/widgets/mcp_box.py",
    "app/ui/widgets/storage_box.py",
    "app/ui/widgets/window_box.py",
    "app/ui/widgets/chat_box.py",
    "app/ui/widgets/model_box.py",
    "app/ui/accounts/globals_box.py",
    "app/ui/accounts/enrich_settings.py",
    "app/ui/accounts/panel.py",
)


@pytest.mark.parametrize("relative", ROW_FILES)
def test_no_settings_box_lays_out_a_row_its_own_way(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    own = [node.lineno for node in ast.walk(tree)
           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr == "addRow"]
    assert not own, f"{relative} calls addRow itself at lines {own}; use widgets/settings_row.py"


def test_a_row_helper_row_carries_its_label_and_hint():
    from PySide6.QtWidgets import QFormLayout, QLabel, QSpinBox, QWidget

    from app.ui.widgets.settings_row import ROW_PROPERTY, add_row

    _qt()
    holder = QWidget()
    form = QFormLayout(holder)
    spin = add_row(form, QSpinBox(), "How many", hint="What the number does.")
    assert spin.property(ROW_PROPERTY) is True
    assert form.labelForField(spin).text() == "How many"
    hints = holder.findChildren(QLabel, "settingsHint")
    assert [h.text() for h in hints] == ["What the number does."]
    assert form.rowCount() == 2


# --- 1. Search all settings ------------------------------------------------------------------

@pytest.fixture()
def view(settings, tmp_path):
    from app.storage.sqlite_store import SqliteStore
    from app.ui.settings_view import SettingsView

    _qt()
    store = SqliteStore(settings.fts_db).connect()
    page = SettingsView(settings, store)
    page.resize(1000, 700)
    page.show()
    yield page
    page.close()
    store.close()


@pytest.mark.slow
def test_the_search_lists_what_it_found_with_its_category(view):
    from app.ui.settings_view import CATEGORY_SEARCH

    app = _qt()
    view.filter_box.setText("results to rerank")
    _process(app)
    assert view.filter_found.isVisible() and view.filter_goto.isVisible()
    lines = [view.filter_found.item(i).text() for i in range(view.filter_found.count())]
    assert lines and lines[0] == f"Results to rerank  ·  {CATEGORY_SEARCH}"
    view.filter_box.clear()
    _process(app)
    assert not view.filter_found.isVisible() and not view.filter_goto.isVisible()


@pytest.mark.slow
def test_enter_takes_you_to_the_setting_even_under_advanced(view):
    """`RERANK_TOP_N` sits in Search's Advanced fold, closed by default."""
    from app.ui.settings_view import CATEGORY_APPEARANCE, CATEGORY_SEARCH
    from app.ui.widgets.settings_search import FOUND_PROPERTY

    app = _qt()
    view._nav.show_category(CATEGORY_APPEARANCE)
    view.filter_box.setText("results to rerank")
    _process(app)
    view.filter_box.returnPressed.emit()
    _process(app)

    target = view.findChild(object, "RERANK_TOP_N")
    assert view.filter_box.text() == "", "the search stayed in the box"
    assert view._nav.current_category() == CATEGORY_SEARCH
    assert target.isVisible(), "the setting is still folded away or hidden"
    assert target.property(FOUND_PROPERTY) is True, "the row was not lit"
    label = view._form_label_for(target)
    assert label is not None and label.property(FOUND_PROPERTY) is True
    scroll = view._nav.scroll
    top = target.mapTo(scroll.widget(), target.rect().topLeft()).y()
    shown = scroll.verticalScrollBar().value()
    assert shown <= top <= shown + scroll.viewport().height(), "the row is off the screen"


@pytest.mark.slow
def test_a_click_on_a_found_line_goes_there(view):
    from app.ui.settings_view import CATEGORY_STORAGE

    app = _qt()
    view.filter_box.setText("cloud content download limit")
    _process(app)
    rows = [view.filter_found.item(i) for i in range(view.filter_found.count())]
    item = next(i for i in rows if i.text().endswith(CATEGORY_STORAGE))
    view.filter_found.itemClicked.emit(item)
    _process(app)
    assert view._nav.current_category() == CATEGORY_STORAGE
    assert view.filter_box.text() == ""


@pytest.mark.slow
def test_the_search_has_its_own_accessible_name(view):
    assert view.filter_box.accessibleName() == "Search all settings"
