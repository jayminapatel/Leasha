"""Text typed into the app is the same size as the rest of the settings page.

2026-10-10. The owner: the size of free text is not consistent with the settings page.
Measured with the real theme applied: labels, drop-downs, number fields, check boxes, buttons
and the multi-line text boxes were 13.1px, but every `QLineEdit` - the address, shortcut, key and
note boxes in Settings - was 14.9px, because the base rule said `large` for all of them though
its own comment says only the search box should be. Now the one large input is `#searchBox`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QTextBrowser, QTextEdit, QTimeEdit, QWidget,
)

from app.ui import theme  # noqa: E402


@pytest.fixture(params=["light", "dark"])
def themed(request):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet(request.param))
    yield app
    app.setStyleSheet("")


def _size(widget: QWidget) -> float:
    widget.ensurePolished()
    return round(widget.font().pointSizeF(), 2)


def test_every_kind_of_input_is_the_size_of_the_text_around_it(themed):
    reference = _size(QLabel("x"))
    for kind in (QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox,
                 QTimeEdit, QCheckBox, QPushButton):
        assert _size(kind()) == reference, f"{kind.__name__} is not body size"


def test_the_search_box_is_the_one_input_that_is_larger(themed):
    box = QLineEdit("x")
    box.setObjectName("searchBox")
    assert _size(box) > _size(QLineEdit("x"))
    assert _size(box) == pytest.approx(float(theme.font_sizes()["large"][:-2]), abs=0.01)


def _settings(root):
    # 2026-10-10: a temporary folder, never the owner's. This read
    # "D:/Leasha/Data", and building the Settings boxes from it created that
    # folder's cache, fts, models, state and vectors on every run - the
    # owner's machine had a D:\Leasha it no longer uses (non-negotiable #10).
    return SimpleNamespace(
        data_path=str(root), model_cache=str(root / "models"), embed_model="m",
        embed_dim=384, cloud_content_cap_mb=1024, chat_engine="ollama", chat_model="",
        ollama_url="http://127.0.0.1:11434", ollama_model="qwen2.5:1.5b", chat_max_rounds=3,
        chat_context_tokens=4096, chat_verify_strictness=70, chat_web_enabled=False,
        chat_web_ask_first=True, chat_style_note="", chat_planner_model="", chat_router_model="",
        chat_web_provider="auto", chat_web_searxng_url="", chat_web_brave_key="",
        chat_web_show_query=True, video_indexing_enabled=False, audio_transcription_enabled=False,
        transcribe_model="base", video_keyframe_interval_s=60, video_keyframe_cap=200,
        mini_search_enabled=True, mini_search_hotkey="Ctrl+Shift+Space")


def test_the_settings_boxes_hold_nothing_that_is_not_body_size(themed, tmp_path):
    """Every input and button in the boxes that have text boxes is the same size - so a
    text box added to one of them later cannot be larger or smaller than its neighbours."""
    from app.ui.widgets.chat_box import ChatBox
    from app.ui.widgets.editor_box import EditorBox
    from app.ui.widgets.media_box import MediaBox
    from app.ui.widgets.model_box import ModelBox
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox
    from app.ui.widgets.storage_box import StorageBox

    settings = _settings(tmp_path / "Data")
    host = QWidget()
    boxes = [StorageBox(settings), ModelBox(lambda: SimpleNamespace(available_models=lambda: [])),
             ChatBox(settings), MediaBox(settings), SearchBehaviourBox(settings), EditorBox(settings)]
    for box in boxes:
        box.setParent(host)
    body = _size(QLabel("x"))
    kinds = (QLineEdit, QComboBox, QSpinBox, QCheckBox, QPushButton, QPlainTextEdit, QTextEdit)
    checked = 0
    for box in boxes:
        for widget in box.findChildren(QWidget):
            if isinstance(widget, kinds) and not widget.objectName().startswith("qt_"):
                assert _size(widget) == body, (
                    f"{type(box).__name__}: {type(widget).__name__} "
                    f"'{widget.objectName() or widget.accessibleName()}' is {_size(widget)}pt, "
                    f"not {body}pt")
                checked += 1
    assert checked > 20, "the test found too few controls to mean anything"


# -- the body text size is chosen, and a new size applies without a restart ----------------------

@pytest.fixture
def twelve():
    """Whatever a test chooses, the next test starts from the default."""
    theme.set_text_size(theme.DEFAULT_TEXT_PX)
    yield
    theme.set_text_size(theme.DEFAULT_TEXT_PX)


def test_the_body_text_is_12px_unless_chosen_otherwise(twelve):
    assert theme.text_size() == 12 == theme.DEFAULT_TEXT_PX
    assert theme.font_sizes(9.0)["body"] == "9.0pt"      # 9pt is 12px at 96 dpi


def test_choosing_a_size_moves_the_whole_scale_together(twelve):
    base = {name: float(value[:-2]) for name, value in theme.font_sizes(9.0).items()}
    theme.set_text_size(18)
    bigger = {name: float(value[:-2]) for name, value in theme.font_sizes(9.0).items()}
    for name in base:
        assert bigger[name] == pytest.approx(base[name] * 1.5, rel=0.02), name


def test_a_size_outside_the_range_or_not_a_number_is_made_safe(twelve):
    low, high = theme.TEXT_PX_RANGE
    assert theme.set_text_size(1) == low
    assert theme.set_text_size(500) == high
    assert theme.set_text_size("banana") == theme.DEFAULT_TEXT_PX
    assert theme.set_text_size(None) == theme.DEFAULT_TEXT_PX
    assert theme.set_text_size("15") == 15


def test_a_new_stylesheet_resizes_widgets_that_already_exist(themed, twelve):
    """The change is live: the same widgets, restyled, no restart and no rebuild."""
    label, box, area = QLabel("x"), QLineEdit("x"), QPlainTextEdit("x")
    before = [_size(w) for w in (label, box, area)]
    assert before == [9.0, 9.0, 9.0]

    theme.set_text_size(16)
    themed.setStyleSheet(theme.stylesheet("dark"))
    for widget in (label, box, area):
        widget.ensurePolished()
    after = [_size(w) for w in (label, box, area)]
    assert after == [12.0, 12.0, 12.0]


def test_the_text_size_control_is_in_appearance_and_applies_at_once(themed, twelve):
    from app.ui.widgets.window_box import WindowBox

    box = WindowBox()
    low, high = theme.TEXT_PX_RANGE
    assert box.text_size.objectName() == "UI_TEXT_SIZE"
    assert (box.text_size.minimum(), box.text_size.maximum()) == (low, high)
    assert box.text_size.value() == 12

    sent: list[int] = []
    box.text_size_changed.connect(sent.append)
    box.text_size.setValue(15)
    assert sent == [15]

    box.load(False, False, text_size=11)               # showing what was saved is not a change
    assert box.text_size.value() == 11 and sent == [15]


def test_the_controller_remembers_the_size_and_restyles_the_window(monkeypatch, themed, twelve):
    """What the shell calls when the box changes: save it, set it, restyle - nothing else."""
    from app.ui.controllers import settings_controller as module

    saved: dict[str, str] = {}
    monkeypatch.setattr(module, "save_state",
                        lambda store, key, value, **kw: saved.__setitem__(key, value))
    restyled: list[int] = []
    window = SimpleNamespace(_store=object(), _text_size=12,
                             _apply_theme=lambda: restyled.append(theme.text_size()))
    controller = module.SettingsController.__new__(module.SettingsController)
    controller._w = window

    controller._text_size_changed(15)

    assert saved == {"ui:text_size": "15"}
    assert window._text_size == 15 and restyled == [15], "the sheet was rebuilt at the old size"
