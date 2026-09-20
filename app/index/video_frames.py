r"""What the pipeline does with the pictures a video produced, beyond the one mean vector.

Layer: L3

Work order 202626270515. `app/extract/media.py` leaves a video's scene-change
pictures in a temporary folder and `Pipeline._maybe_embed_video` holds them until
the CLIP step is done. Two things are done with them here, both optional and both
in a module of their own so the pipeline carries a call and not the logic:

**Per-frame CLIP** (`store_frames`). The image table is keyed by file, so a video
there is one vector - the mean of its pictures - which can say *which film* and
not *which minute*. Each picture's own vector is also written, keyed by file and
second (`VideoFrameVectorStore`), so "the birthday cake" can find the film and
12:41. Written *after* the mean, and a failure here never costs the mean or the
file.

**Faces on video frames** (`detect_faces`). "Videos with Daddy" works through the
same piles a photograph's faces join: each picture goes through the same detector,
each face becomes a `faces` row for the video's file, and the clustering drain
that already runs gives it a pile, a suggestion or a new pile. **Switch-gated
exactly like a photograph's** - the caller asks `people_recognition_enabled`
first and this module names `app.extract.face_detect` only after that passes, so
off means the import never happens.

**Known limits, said here so nobody finds them by surprise:**

  * A face row stores a pixel box into a *file*. For a video the picture is gone
    once the CLIP step has used it, so the Photo Tagger's crop tile for a
    video-derived face is a placeholder - a face joins its pile by what it looks
    like, and a person naming a pile still sees the photographs' faces in it.
    Showing the crop would need the moment on the `faces` row (a schema change),
    which this order does not make.
  * Videos indexed before Recognise people was switched on are not scanned by the
    backfill drain (it reads photographs). A video is scanned when it is read, so
    `--force` on those files does it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from app.core.logging import logger

__all__ = ["store_frames", "detect_faces"]

log = logger.bind(component="index.video_frames")


def store_frames(
    image_vectors: Any,
    file_id: int,
    seconds: Sequence[float],
    vectors: Sequence[Sequence[float]],
    *,
    ext: str = "",
    mtime_ns: int = 0,
) -> int:
    """Write each picture's CLIP vector under `(file, second)`. Returns rows written.

    `image_vectors` is the pipeline's `ImageVectorStore`; the frame table is
    reached through its `video_frames()`. A store without one (a test double, an
    older caller) writes nothing and says nothing: a lane that is not configured
    behaves like a lane that does not exist (H4).
    """
    factory = getattr(image_vectors, "video_frames", None)
    if not callable(factory) or not len(vectors):
        return 0
    frames = factory()
    if frames is None or not hasattr(frames, "replace_frames"):
        return 0
    moments = [(float(s), v) for s, v in zip(seconds, vectors)]
    return int(frames.replace_frames(int(file_id), moments, ext=ext,
                                     mtime_ns=mtime_ns) or 0)


def detect_faces(
    store: Any,
    file_id: int,
    paths: Sequence[str],
    *,
    detector: Optional[Callable[[Path], Sequence[Any]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Find faces in a video's pictures and record them against its file. Returns faces.

    **Only ever called with `people_recognition_enabled` already checked** - see
    the module docstring. `detector` is injected by tests; the real one is
    `face_detect.detect_faces`, imported here and nowhere earlier.

    One unreadable picture costs that picture (H4), and the file is marked
    scanned whatever was found, because "looked and found nobody" is a fact the
    backfill must not re-ask.
    """
    if detector is None:
        from app.extract import face_detect

        if not face_detect.available():
            return 0
        detector = face_detect.detect_faces

    clear = getattr(store, "clear_faces_for_file", None)
    if callable(clear):
        clear(int(file_id))                         # a re-read film has different pictures
    kept: list[Any] = []
    for path in paths:
        if should_stop is not None and should_stop():
            return len(kept)                        # not marked scanned: it was cut short
        if len(kept) >= MAX_FACES_PER_VIDEO:
            break
        try:
            detections = detector(Path(path))
        except Exception as exc:                    # noqa: BLE001 - one picture, not the video
            log.debug("no faces read from a frame of file {}: {}", file_id, exc)
            continue
        for detection in detections:
            if len(kept) >= MAX_FACES_PER_VIDEO or _seen_already(detection, kept):
                continue
            store.add_face(file_id, detection.bbox, detection.embedding)
            kept.append(detection)
    store.mark_face_scanned(int(file_id))
    return len(kept)


#: One person on screen for ten minutes is two hundred pictures and two hundred
#: near-identical faces; kept as they are they would swamp a pile and slow every
#: clustering pass. A face this similar to one already kept from the same film is
#: the same person in another frame. **A constant, not a setting** (rule 11).
#:
#: **Measured, and the first guess was wrong.** It was 0.7. On 12 pictures of one
#: real 63-second family film (insightface buffalo_l, 2026-09-20) the same
#: people's faces in different frames scored 0.5-0.72 against each other and the
#: two false or unrelated detections 0.09 and 0.13 - so 0.7 merged one pair of
#: ten faces and kept nine "distinct" people. 0.5 is the low edge of what one
#: person scored; the evidence to change it is a film where two different people
#: are merged (relatives can score higher against each other than strangers), or
#: a corpus-wide sample that puts the same-person floor elsewhere. One video, one
#: sample: recorded, not proven.
SAME_FACE_SIMILARITY = 0.5

#: The most faces recorded from one video, so a crowd scene or a two-hour meeting
#: cannot write thousands of rows. Evidence to change it: a real family film where
#: the people named in a search are missing.
MAX_FACES_PER_VIDEO = 20


def _seen_already(detection: Any, kept: Sequence[Any]) -> bool:
    """Is this face (near enough) one already kept from this video? Pure."""
    if not kept:
        return False
    import numpy as np

    try:
        mine = np.frombuffer(detection.embedding, dtype="float32")
        for other in kept:
            theirs = np.frombuffer(other.embedding, dtype="float32")
            if mine.shape == theirs.shape and float(np.dot(mine, theirs)) >= SAME_FACE_SIMILARITY:
                return True
    except Exception:                               # noqa: BLE001 - keep it rather than lose a face
        return False
    return False
