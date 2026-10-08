"""Two consumer-thread rules in the pipeline, pinned at the source.

Layer: L3

Both were found in the 2026-10-08 review of `app/index/pipeline.py`:

1. `_record_skip` runs the picture steps (CLIP, pHash, a 0.9 s face scan) for
   every photo OCR found no text in - the path most photos take - and did so
   inside the consumer's open write transaction, breaking `WRITE_GROUP_MAX_S`
   on every photo while the window's queued writes waited. `_write_one`
   already committed first; the skip path now does the same.
2. `_maybe_detect_faces` groups faces mid-run on the consumer thread. The
   consumer must never wait on the memory governor (`_disk_ok` says why: the
   workers are blocked holding their chunks, so memory cannot fall), and the
   governor waits for ever after `MAX_SETTLES`. The mid-run drain is unpaced.

Pinned by reading the source because driving a real pipeline to the point of
a memory pause with faces arriving needs the models; the shape is the claim.
"""

from __future__ import annotations

import inspect


def test_the_skip_path_commits_the_write_group_before_any_picture_work():
    from app.index.pipeline import Pipeline

    source = inspect.getsource(Pipeline._record_skip)
    commit = source.index("self._commit_write_group(timed=False)")
    picture = source.index("self._maybe_embed_image(candidate, file_id)")
    assert commit < picture, "the rows must commit before CLIP, pHash and faces run"
    assert "self._media_work_ahead(candidate)" in source[:commit], \
        "and only when there is picture work ahead, as `_write_one` does"


def test_mid_run_face_grouping_never_waits_on_the_governor():
    from app.index.pipeline import Pipeline

    assert "paced" in inspect.signature(Pipeline._drain_face_cluster).parameters
    assert "self._drain_face_cluster(stats, paced=False)" in \
        inspect.getsource(Pipeline._maybe_detect_faces)
    drain = inspect.getsource(Pipeline._drain_face_cluster)
    assert "if paced:" in drain and "wait_while_throttled" in drain.split("if paced:", 1)[1]


def test_the_start_and_end_of_run_drains_keep_their_pacing():
    """`paced` defaults on: only the consumer-thread call opts out."""
    from app.index.pipeline import Pipeline

    assert inspect.signature(Pipeline._drain_face_cluster).parameters["paced"].default is True
    source = inspect.getsource(Pipeline)
    unpaced = source.count("paced=False)")
    assert unpaced == 1, f"{unpaced} unpaced drains; only `_maybe_detect_faces` may skip pacing"
