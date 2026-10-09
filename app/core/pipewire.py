r"""Length-prefixed pickled frames over a pipe, between two Leasha processes.

Layer: L0.

The same framing `app/index/read_process.py` and `app/index/ocr_process.py`
use between the indexer and its helpers, kept here for the helpers that live
below the index layer (the chat model host, `app/ort/llm_host.py`, and its
proxy in `app/llm/`), which may not import from L3. Both ends are this
application, and nothing but the peer writes the pipe, so a pickle is safe.
"""

from __future__ import annotations

import pickle
import struct
from typing import Any, Optional

__all__ = ["read", "write"]

_HEADER = struct.Struct("<I")


def write(stream: Any, message: Any) -> None:
    """One frame onto `stream`, flushed. Raises what the pipe raises
    (`BrokenPipeError`, `ValueError` on a closed stream): the caller decides
    what a gone peer means."""
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(data)) + data)
    stream.flush()


def _read_exact(stream: Any, size: int) -> Optional[bytes]:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read(stream: Any) -> Any:
    """The next message, or None when the other end has gone."""
    head = _read_exact(stream, _HEADER.size)
    if head is None:
        return None
    body = _read_exact(stream, _HEADER.unpack(head)[0])
    if body is None:
        return None
    return pickle.loads(body)
