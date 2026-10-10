"""Interpret in the window runs the rules first, as the composition was built to.

Layer: L5 (pytest-qt, the shared `gui_mainwindow`: a real window over a real store
holding a pdf and two txt files). Work order model-sequencing, item 3d (2026-10-10).

`QueryTranslator` reads a sentence with the rules (`translate_rules.read`) before any
model, and hands the model only the words they leave - but only when it has the
index to check real file types and senders against. The window built it without
one, so every press sent the whole sentence to the model. These tests take the
window's own translator and check it has the store and uses it.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.gui


class _RecordingClient:
    """A model that records what it is asked and answers with a fixed query."""

    model = "fake"

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def health(self, *, force: bool = False) -> bool:
        return True

    def has_model(self) -> bool:
        return True

    def generate(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)

        class Response:
            text = self.reply

        return Response()


@pytest.fixture()
def translator(gui_mainwindow, monkeypatch):
    _app, window, store, _engine = gui_mainwindow
    translator = window._translator
    client = _RecordingClient()
    # The window is shared by the module: everything changed here is put back.
    monkeypatch.setattr(translator, "client", client)
    monkeypatch.setattr(translator, "enabled", True)
    monkeypatch.setattr(translator, "_cache", {})
    return translator, client, store


def test_the_window_s_translator_is_given_the_index(translator):
    built, _client, store = translator
    assert built.store is store


def test_pdfs_from_last_year_is_read_by_the_rules_and_the_model_is_asked_for_less(translator):
    built, client, _store = translator
    last = date.today().year - 1

    result = built.translate("pdfs from last year")

    assert "type:pdf" in result.query
    assert f"after:{last}-01-01" in result.query and f"before:{last}-12-31" in result.query
    # Nothing, or only what the rules left: never the type or the date again.
    for prompt in client.prompts:
        sentence = prompt.rsplit("Sentence:", 1)[-1]
        assert "pdfs" not in sentence and "last year" not in sentence


def test_the_evaluate_command_gives_its_translators_the_index():
    """2026-10-10 (order 1h 3d): `app.cli evaluate --interpret` built its
    `QueryTranslator` without the store, so the rules-first step never ran there
    and the run measured something no person gets. Read from the source: both
    of its translators are given the store."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "cli" / "evaluate.py").read_text(
        encoding="utf-8")
    built = source.count("QueryTranslator(")
    assert built == 2 and source.count("store=store") >= 2, (
        "every QueryTranslator in evaluate.py is given the store")
