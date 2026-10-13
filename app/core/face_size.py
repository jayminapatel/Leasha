r"""The smallest face worth keeping - one number the face finder and the People
window both read.

Layer: L0

**2026-10-14, the owner's index.** 377 faces found, 228 of them in no group,
and the People window gave no way to name those. Measured on that index: the
unsorted faces' median shortest side was 21 pixels against 180 for the faces
that did group, and 159 of the 228 were under 40. ArcFace reads a face at
112 x 112; a face a fifth of that is upscaled noise, matches nothing, and is too
small for a person to recognise in a crop either. So the finder drops them
(`app.extract.face_detect`), and the store leaves any found before this rule out
of "Faces seen once" and the "still to sort" count. A face that small already
in a group stays where it is - it matched, which is the evidence that counts.

Non-negotiable #11's fallback: nobody will tune this per machine, so it is a
constant, with the measurement that would move it named here.
"""

from __future__ import annotations

__all__ = ["SMALLEST_FACE_PX", "big_enough"]

#: The shortest side, in the photo's own pixels, a face must have to be kept.
SMALLEST_FACE_PX = 40


def big_enough(width: float, height: float) -> bool:
    """Whether a face box is at least `SMALLEST_FACE_PX` on its shorter side."""
    return min(float(width), float(height)) >= SMALLEST_FACE_PX
