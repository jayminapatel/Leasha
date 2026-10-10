r"""Every `Pipeline` subclass that replaces the walk takes the walk's arguments.

Layer: L3

2026-10-10. `Pipeline._produce` began passing `mark_first_band` to
`_candidates` (e773e41, review item W1: the folders marked "first" are read
while the rest is walked). The two subclasses that hand the run a list instead
of a walk - the media tail (`media_backlog.BacklogPipeline`) and "Retry with a
longer time limit" (`timed_out_retry.RetryPipeline`) - kept the old signature,
so each died in its walker with a TypeError: no video was read again, and ten
tests in `test_media.py` went red. These pin both halves of the contract: the
argument is taken, and when it is set the end-of-band marker comes first, so
the files go to the list that is actually read.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index import pipeline as module
from app.index.media_backlog import _backlog_pipeline_class
from app.index.timed_out_retry import _retry_pipeline_class
from app.index.walker import Candidate


def _files(tmp_path: Path) -> list[Candidate]:
    return [Candidate(path=tmp_path / name, size_bytes=1, mtime_ns=1, priority=100)
            for name in ("a.mp4", "b.mp4")]


def _backlog(tmp_path: Path):
    built = _backlog_pipeline_class().__new__(_backlog_pipeline_class())
    built._seen_paths = set()
    built._queued = _files(tmp_path)
    return built


def _retry(tmp_path: Path):
    built = _retry_pipeline_class().__new__(_retry_pipeline_class())
    built._seen_paths = set()
    built._retry_plan = SimpleNamespace(candidates=_files(tmp_path))
    return built


@pytest.mark.parametrize("make", [_backlog, _retry], ids=["media_tail", "timed_out_retry"])
def test_the_override_takes_the_argument_the_base_class_is_called_with(make, tmp_path):
    base = inspect.signature(module.Pipeline._candidates).parameters
    override = inspect.signature(type(make(tmp_path))._candidates).parameters
    assert set(base) <= set(override)


@pytest.mark.parametrize("make", [_backlog, _retry], ids=["media_tail", "timed_out_retry"])
def test_with_the_marked_folders_on_the_marker_comes_before_every_file(make, tmp_path):
    out = list(make(tmp_path)._candidates(mark_first_band=True))
    assert out[0] is module._FIRST_BAND_END
    assert [c.path.name for c in out[1:]] == ["a.mp4", "b.mp4"]


@pytest.mark.parametrize("make", [_backlog, _retry], ids=["media_tail", "timed_out_retry"])
def test_without_them_only_the_files_come(make, tmp_path):
    out = list(make(tmp_path)._candidates(mark_first_band=False))
    assert [c.path.name for c in out] == ["a.mp4", "b.mp4"]
