r"""The videos-and-recordings tail uses the run's text-in-pictures helper.

Layer: L2

Order 1h item 1c (2026-10-10). The tail's own `Pipeline` installed a helper of
its own, which first cleared the outer run's hook, and left the hook empty when
it closed - so the outer run's end-of-run OCR ran inside the index process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.extract import ocr
from app.index import media_backlog


@pytest.fixture
def backlog():
    built = media_backlog._backlog_pipeline_class().__new__(
        media_backlog._backlog_pipeline_class())
    built.config = SimpleNamespace(read_processes=True)
    yield built
    ocr.set_engine_process(None)


def test_the_tail_leaves_the_outer_runs_helper_installed(backlog, monkeypatch) -> None:
    def outer_helper(source):
        return None

    started = []
    monkeypatch.setattr("app.index.ocr_process.OcrProcess",
                        lambda **kw: started.append(kw) or SimpleNamespace(ocr=None, close=lambda: None))
    ocr.set_engine_process(outer_helper)

    backlog._install_ocr_helper()
    backlog._close_ocr_helper()

    assert ocr._engine_process is outer_helper, "the outer run's pictures still go to its helper"
    assert started == [], "no second helper process"


def test_the_tail_leaves_photo_descriptions_and_picture_text_to_the_outer_run(backlog) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("the outer run does this straight after the tail")

    backlog.store = SimpleNamespace(conn=SimpleNamespace(execute=refuse))
    assert backlog._drain_photo_tags(SimpleNamespace(enrichment_counts={})) is None
    assert backlog._drain_picture_text(SimpleNamespace(enrichment_counts={})) is None
