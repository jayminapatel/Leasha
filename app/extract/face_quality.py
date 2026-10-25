r"""A face too pixelated to keep - the second rule the face finder applies,
after `app.core.face_size`.

Layer: L2

**2026-10-25, the owner:** "if the photo is highly pixelated ignore it". A face
can be big enough in pixels (`big_enough`) and still hold almost no picture:
a WhatsApp thumbnail or forwarded image blown back up to size, a soft or
smeared shot, a JPEG saved so hard it is all 8 x 8 squares. ArcFace's vector
for such a face matches poorly, and the face makes an ugly naming tile.

Two cheap measures on the face's own crop, grey, no model:

* **Detail** - the variance of the Laplacian, read at no more than
  `MEASURE_SIDE` (ArcFace's own 112) on the crop's shorter side. A bigger crop
  is shrunk by averaging, a smaller one is read as it is, never enlarged - so
  a small, sharp face scores on its own pixels and stays `big_enough`'s alone.
  An upscaled or blurred face is smooth at every scale and scores low.
* **Blockiness** - how much stronger the step between neighbouring pixels is
  on the strongest of the eight column (and row) positions than on the
  typical one. JPEG works in 8 x 8 blocks, so a hard-compressed face has a
  step every eighth pixel; a natural face has none. Taking the strongest of
  the eight, not a fixed one, keeps it right after a rotation or crop moved
  the grid.

**Measured 2026-10-25**, through these functions, buffalo_l on the processor.
41 sharp faces (40 of the owner's grouped photos, read only, plus insightface's
own `t1.jpg` and `Tom_Hanks`), each spoilt 13 ways; "match" is the cosine of
the spoilt face's vector to the sharp one's, against grouping's 0.62
(`face_clustering.AUTO_ASSIGN_THRESHOLD`); "dropped" is this rule's share:

=======================  ==============  ===========  =========  =======
kind                     detail p50      block p50    match p50  dropped
=======================  ==============  ===========  =========  =======
sharp original           212             1.05         1.00       0 %
shrunk to 80 px, back     70             1.11         0.99       2 %
JPEG quality 40          199             1.74         0.98       2 %
shrunk to 32 px, back     13             1.04         0.93       7 %
shrunk to 24 px, back      6.4           1.03         0.84      27 %
JPEG quality 10          217             3.49         0.86      61 %
JPEG quality 8           218             4.32         0.80      88 %
shrunk to 16 px, back      2.7           1.03         0.67      80 %
JPEG quality 5           267             6.70         0.67     100 %
24 px thumb, q30, back     5.0           1.05         0.43      51 %
shrunk to 12 px, back      1.4           1.02         0.49      95 %
blur, face side / 20       0.7           1.02         0.47     100 %
=======================  ==============  ===========  =========  =======

Detail alone misses hard JPEG (its blocks are edges, so it scores *higher*)
and blockiness alone misses an upscale or a blur, so the rule is either one.

**The owner's index**, 800 stored faces of 40 px or more drawn at random and
read the same way: detail 1st percentile 6.0, 5th 14.7, median 225;
blockiness median 1.14, 99th percentile 2.2, highest 2.54. A detail line of 8
would have dropped 14 (1.75 %), and the crops showed why not: most were dark,
not pixelated - a clear profile in shadow scores low because the Laplacian
scales with contrast. At 5 it drops 4 (0.5 %): a near-black crop, a smeared
face and two faces lost in shadow, none of them usable as a tile. Blockiness
drops none of his - his photos are camera JPEGs - and is there for the
forwarded and resaved ones. So `LEAST_DETAIL` = 5, under every sharp original
(lowest 8.9) and over most faces shrunk to 16 px or less; `MOST_BLOCKY` = 3,
over every sharp original (highest 1.5) and all but one quality-40 save
(highest 3.1), and under every quality-5 face (lowest 3.3).
No owner image is kept: the tests build their crops from noise.

**Faces found before this rule stay.** A photo is looked at for faces once
(`face_scans`; `Pipeline._maybe_detect_faces` skips one already marked), and
only a changed video has its faces cleared (`clear_faces_for_file`). So the
rule reaches an old face only when its photo leaves the index and is found
again (the `files` row's delete cascades to `faces` and `face_scans`), or the
index is rebuilt. Unlike size, it cannot be applied in the store: it needs the
picture's pixels, which the store does not hold.

Non-negotiable #11's fallback, as `face_size`: nobody will tune this per
machine, so these are constants with the measurement that would move them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["MEASURE_SIDE", "LEAST_DETAIL", "MOST_BLOCKY", "detail", "blockiness",
           "too_pixelated"]

#: The shorter side, in pixels, detail is read at - ArcFace's own input size.
MEASURE_SIDE = 112
#: The least Laplacian variance a face crop needs to be kept.
LEAST_DETAIL = 5.0
#: The most 8-pixel blockiness a face crop may have and be kept.
MOST_BLOCKY = 3.0


def _grey(image: Any) -> np.ndarray:
    """The picture as float32 grey. A 3-channel image is taken as BGR, the
    order `face_detect` reads in; the weights are ITU-R 601's, as OpenCV's."""
    array = np.asarray(image, dtype=np.float32)
    if array.ndim == 3:
        array = array[:, :, 0] * 0.114 + array[:, :, 1] * 0.587 + array[:, :, 2] * 0.299
    return array


def _shrink(grey: np.ndarray, side: int) -> np.ndarray:
    """Averaged down so the shorter side is `side`; returned as it is when it
    already is that or smaller - never enlarged."""
    height, width = grey.shape
    shortest = min(height, width)
    if shortest <= side:
        return grey
    scale = side / shortest
    rows = np.linspace(0, height, max(1, round(height * scale)) + 1).astype(int)[:-1]
    cols = np.linspace(0, width, max(1, round(width * scale)) + 1).astype(int)[:-1]
    summed = np.add.reduceat(np.add.reduceat(grey, rows, axis=0), cols, axis=1)
    counts = np.outer(np.diff(np.append(rows, height)), np.diff(np.append(cols, width)))
    return summed / counts


def detail(crop: Any) -> float:
    """The variance of the 4-neighbour Laplacian of a face crop, read at no
    more than `MEASURE_SIDE` on its shorter side. 0.0 for a crop too small."""
    grey = _shrink(_grey(crop), MEASURE_SIDE)
    if min(grey.shape) < 3:
        return 0.0
    lap = (grey[1:-1, :-2] + grey[1:-1, 2:] + grey[:-2, 1:-1] + grey[2:, 1:-1]
           - 4.0 * grey[1:-1, 1:-1])
    return float(lap.var())


def _axis_blockiness(grey: np.ndarray, axis: int) -> float:
    steps = np.abs(np.diff(grey, axis=axis)).mean(axis=1 - axis)
    if steps.size < 16:
        return 1.0
    phases = np.array([steps[p::8].mean() for p in range(8)])
    typical = float(np.median(phases))
    return float(phases.max() / typical) if typical > 1e-6 else 1.0


def blockiness(crop: Any) -> float:
    """How much the strongest of the eight pixel positions steps more than the
    typical one, across columns and rows averaged. About 1 for a natural face,
    3 and over for a hard-saved JPEG. Read on the crop's own pixels."""
    grey = _grey(crop)
    return (_axis_blockiness(grey, 1) + _axis_blockiness(grey, 0)) / 2.0


def too_pixelated(image: Any, box: tuple[float, float, float, float]) -> bool:
    """Whether the face at `box` (`x1, y1, x2, y2`, the photo's own pixels) in
    `image` has too little real picture to keep: under `LEAST_DETAIL`, or over
    `MOST_BLOCKY`. Never raises - a crop that cannot be read is not judged."""
    try:
        array = np.asarray(image)
        height, width = array.shape[:2]
        x1, y1, x2, y2 = (float(v) for v in box)
        left, top = max(0, int(round(x1))), max(0, int(round(y1)))
        right, bottom = min(width, int(round(x2))), min(height, int(round(y2)))
        if right - left < 3 or bottom - top < 3:
            return False
        crop = array[top:bottom, left:right]
        return detail(crop) < LEAST_DETAIL or blockiness(crop) > MOST_BLOCKY
    except Exception:                               # noqa: BLE001 - a judgement, not the job
        return False
