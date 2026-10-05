"""The installer's model download: both models, skipped when present, never fatal.

Order 202626082213 §4.2 step 4 and acceptance A3, 2026-10-05. Nothing is
downloaded here: the fetcher and the presence check are stand-ins.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core import model_fetch

SETTINGS = SimpleNamespace(embed_model="BAAI/bge-small-en-v1.5",
                           rerank_model="Xenova/ms-marco-MiniLM-L-6-v2", model_cache="C:/m")


def test_both_models_are_fetched_into_the_model_folder():
    fetched, said = [], []
    code = model_fetch.fetch_at_install(
        SETTINGS, say=said.append, is_present=lambda *a, **k: False,
        fetcher=lambda kind, name, **kw: fetched.append((kind, name, kw["model_cache"])) or "done")
    assert code == 0
    assert fetched == [("embed", "BAAI/bge-small-en-v1.5", "C:/m"),
                       ("rerank", "Xenova/ms-marco-MiniLM-L-6-v2", "C:/m")]
    assert "The search model is ready." in said and "The reranker is ready." in said


def test_a_model_already_here_is_not_fetched_again():
    fetched, said = [], []
    model_fetch.fetch_at_install(
        SETTINGS, say=said.append, is_present=lambda kind, *a, **k: kind == "embed",
        fetcher=lambda kind, name, **kw: fetched.append(kind))
    assert fetched == ["rerank"]
    assert any("already here" in line for line in said)


def test_a_failed_download_is_said_and_the_install_goes_on():
    said = []

    def offline(*_a, **_k):
        raise OSError("no network")

    code = model_fetch.fetch_at_install(SETTINGS, say=said.append,
                                        is_present=lambda *a, **k: False, fetcher=offline)
    assert code == 0
    assert any("did not download: no network" in line for line in said)
    assert any("Settings > Models can download it later" in line for line in said)


def test_settings_that_cannot_be_read_are_said_too(monkeypatch):
    import app.core.config as config

    def broken(*_a, **_k):
        raise RuntimeError("no .env")

    monkeypatch.setattr(config, "load_settings", broken)
    said = []
    assert model_fetch.fetch_at_install(say=said.append) == 0
    assert "no .env" in said[0]
