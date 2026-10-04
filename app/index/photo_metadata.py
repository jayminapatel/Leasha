r"""Write the people in a photo, and its description, into the photo's metadata.

Layer: L3

2026-10-05, the owner: "it should also store meta data in the photos", then
"go ahead with option a but an option b button which can be used". Option a -
the default - keeps names inside Leasha's index. **This is option b, and it is
the owner's own exception to non-negotiable 10** (read-only against user data,
see the dated note above that rule): it runs only when the person presses
"Write names into photos", never during indexing.

**Where the metadata goes.** XMP, the standard photo tools read (Lightroom,
digiKam, Windows Photos, Apple Photos on import):

- `dc:subject` - keywords: each person's name.
- `Iptc4xmpExt:PersonInImage` - the people shown, the IPTC field made for it.
- `dc:description` - Leasha's description of the picture.

**How, without re-encoding a single pixel** (rule 12: a library where one
exists - Pillow writes XMP only by saving the picture again, which re-compresses
a JPEG; none other is installed):

- JPEG: the XMP packet is an APP1 segment; it is replaced or inserted in the
  byte stream and every other byte is copied as it was.
- PNG: the XMP packet is an `iTXt` chunk named `XML:com.adobe.xmp`; the same.
- Anything else (HEIC, TIFF, WebP ...): a sidecar file beside the photo -
  `IMG_0001.xmp` - the convention Lightroom and digiKam read. The photo itself
  is not touched.
- Or, when the person chooses it, sidecars for everything.

**Existing XMP is kept.** A camera's or phone's own XMP is parsed and only the
three properties above are replaced; the rest is written back as it was.

**Before a photo is changed it is copied** to the backup folder, under its own
path, and the original file times are put back afterwards, so the photo still
sorts by its date in Explorer. `SqliteStore.note_photo_rewritten` records the
new size so the next run does not read 15,000 photos again.
"""

from __future__ import annotations

import os
import shutil
import struct
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from xml.etree import ElementTree as ET

from app.core.logging import logger

__all__ = ["write_photo_metadata", "run_write", "PhotoMetadata", "WriteResult", "build_xmp",
           "merge_xmp", "read_xmp", "sidecar_path", "INSIDE", "SIDECAR"]

_log = logger.bind(component="index.photo_metadata")

INSIDE = "inside"     # into the photo where the format allows, else a sidecar
SIDECAR = "sidecar"   # always a sidecar; no photo is changed

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEY = b"XML:com.adobe.xmp"
#: An APP1 segment's length field is two bytes and counts itself.
_JPEG_MAX_PACKET = 65535 - 2 - len(_XMP_HEADER)

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xml": "http://www.w3.org/XML/1998/namespace",
}
for _prefix, _uri in NS.items():
    if _prefix != "xml":
        ET.register_namespace(_prefix, _uri)

_RDF = "{%s}" % NS["rdf"]
_OURS = ("{%s}subject" % NS["dc"], "{%s}description" % NS["dc"],
         "{%s}PersonInImage" % NS["Iptc4xmpExt"])


@dataclass(frozen=True)
class PhotoMetadata:
    file_id: int
    path: str
    people: tuple[str, ...]
    description: str = ""


@dataclass
class WriteResult:
    written: int = 0
    sidecars: int = 0
    unchanged: int = 0
    failed: int = 0
    backed_up_bytes: int = 0
    problems: list = None

    def __post_init__(self) -> None:
        if self.problems is None:
            self.problems = []


# --- the XMP packet ------------------------------------------------------------------

def _bag(parent: ET.Element, tag: str, values: Sequence[str], kind: str = "Bag") -> None:
    holder = ET.SubElement(parent, tag)
    seq = ET.SubElement(holder, _RDF + kind)
    for value in values:
        ET.SubElement(seq, _RDF + "li").text = value


def _alt(parent: ET.Element, tag: str, text: str) -> None:
    holder = ET.SubElement(parent, tag)
    alt = ET.SubElement(holder, _RDF + "Alt")
    item = ET.SubElement(alt, _RDF + "li")
    item.set("{%s}lang" % NS["xml"], "x-default")
    item.text = text


def _fill(description: ET.Element, meta: PhotoMetadata) -> None:
    for child in list(description):
        if child.tag in _OURS:
            description.remove(child)
    for name in _OURS:
        description.attrib.pop(name, None)
    if meta.people:
        _bag(description, _OURS[0], meta.people)
        _bag(description, _OURS[2], meta.people)
    if meta.description:
        _alt(description, _OURS[1], meta.description)


def build_xmp(meta: PhotoMetadata) -> bytes:
    """A fresh XMP packet carrying only Leasha's three properties."""
    root = ET.Element("{%s}xmpmeta" % NS["x"])
    rdf = ET.SubElement(root, _RDF + "RDF")
    description = ET.SubElement(rdf, _RDF + "Description")
    description.set(_RDF + "about", "")
    _fill(description, meta)
    return _packet(root)


def merge_xmp(existing: bytes, meta: PhotoMetadata) -> bytes:
    """`existing` with Leasha's three properties replaced; everything else
    kept. A packet that cannot be read is replaced by a fresh one."""
    text = existing.decode("utf-8", "replace")
    start = text.find("<x:xmpmeta")
    if start < 0:
        start = text.find("<rdf:RDF")
    end_meta = text.rfind("</x:xmpmeta>")
    body = text[start:end_meta + len("</x:xmpmeta>")] if start >= 0 and end_meta > 0 else ""
    try:
        root = ET.fromstring(body) if body else None
    except ET.ParseError:
        root = None
    if root is None:
        return build_xmp(meta)
    rdf = root if root.tag == _RDF + "RDF" else root.find(_RDF + "RDF")
    if rdf is None:
        return build_xmp(meta)
    descriptions = rdf.findall(_RDF + "Description")
    for extra in descriptions[1:]:
        for child in list(extra):
            if child.tag in _OURS:
                extra.remove(child)
    description = descriptions[0] if descriptions else ET.SubElement(rdf, _RDF + "Description")
    description.set(_RDF + "about", description.get(_RDF + "about", ""))
    _fill(description, meta)
    if root.tag != "{%s}xmpmeta" % NS["x"]:
        wrapper = ET.Element("{%s}xmpmeta" % NS["x"])
        wrapper.append(root)
        root = wrapper
    return _packet(root)


def _packet(root: ET.Element) -> bytes:
    body = ET.tostring(root, encoding="unicode")
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body
            + '\n<?xpacket end="w"?>').encode("utf-8")


# --- JPEG ------------------------------------------------------------------------------

def _jpeg_segments(data: bytes) -> Optional[list[tuple[int, int, int]]]:
    """`(marker, start, end)` for each segment before the image data, or None
    when this is not a JPEG this module can walk safely."""
    if data[:2] != b"\xff\xd8":
        return None
    out, pos = [], 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xD9 or marker == 0xDA:          # end, or start of scan
            out.append((marker, pos, pos))
            return out
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        out.append((marker, pos, pos + 2 + length))
        pos += 2 + length
    return None


def _jpeg_xmp(data: bytes, segments: list) -> Optional[tuple[int, int, bytes]]:
    for marker, start, end in segments:
        if marker == 0xE1 and data[start + 4:start + 4 + len(_XMP_HEADER)] == _XMP_HEADER:
            return start, end, data[start + 4 + len(_XMP_HEADER):end]
    return None


def _jpeg_with(data: bytes, meta: PhotoMetadata) -> bytes:
    segments = _jpeg_segments(data)
    if segments is None:
        raise ValueError("not a JPEG this can change safely")
    found = _jpeg_xmp(data, segments)
    packet = merge_xmp(found[2], meta) if found else build_xmp(meta)
    if len(packet) > _JPEG_MAX_PACKET:
        raise ValueError("its XMP would be larger than one JPEG segment holds")
    segment = b"\xff\xe1" + struct.pack(">H", 2 + len(_XMP_HEADER) + len(packet)) \
        + _XMP_HEADER + packet
    if found:
        start, end, _old = found
        return data[:start] + segment + data[end:]
    # After the SOI and any APP0 (JFIF) / EXIF APP1, as cameras order them.
    insert = 2
    for marker, start, end in segments:
        if marker in (0xE0, 0xE1):
            insert = end
        else:
            break
    return data[:insert] + segment + data[insert:]


# --- PNG -------------------------------------------------------------------------------

def _png_chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    out, pos = [], len(_PNG_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        out.append((kind, pos, end))
        pos = end
        if kind == b"IEND":
            break
    return out


def _png_with(data: bytes, meta: PhotoMetadata) -> bytes:
    chunks = _png_chunks(data)
    existing = None
    for kind, start, end in chunks:
        if kind == b"iTXt" and data[start + 8:start + 8 + len(_PNG_KEY) + 1] == _PNG_KEY + b"\x00":
            existing = (start, end)
            break
    old_packet = b""
    if existing:
        body = data[existing[0] + 8:existing[1] - 4]
        # keyword \0 compression-flag compression-method lang \0 translated \0 text
        parts = body.split(b"\x00", 1)[1]
        flag = parts[0]
        rest = parts[2:].split(b"\x00", 2)
        text = rest[2] if len(rest) == 3 else b""
        old_packet = zlib.decompress(text) if flag == 1 else text
    packet = merge_xmp(old_packet, meta) if old_packet else build_xmp(meta)
    body = _PNG_KEY + b"\x00" + b"\x00\x00" + b"\x00" + b"\x00" + packet
    chunk = struct.pack(">I", len(body)) + b"iTXt" + body \
        + struct.pack(">I", zlib.crc32(b"iTXt" + body) & 0xFFFFFFFF)
    if existing:
        return data[:existing[0]] + chunk + data[existing[1]:]
    first_end = chunks[0][2]                         # after IHDR
    return data[:first_end] + chunk + data[first_end:]


# --- reading back (for tests and for "what is in it now") -------------------------------

def read_xmp(path: Path) -> bytes:
    """The XMP packet in a JPEG or PNG, or in its sidecar; b"" when none."""
    side = sidecar_path(path)
    data = Path(path).read_bytes()
    if data[:2] == b"\xff\xd8":
        segments = _jpeg_segments(data) or []
        found = _jpeg_xmp(data, segments)
        if found:
            return found[2]
    elif data.startswith(_PNG_SIGNATURE):
        for kind, start, end in _png_chunks(data):
            if kind == b"iTXt" and data[start + 8:start + 8 + len(_PNG_KEY)] == _PNG_KEY:
                body = data[start + 8:end - 4].split(b"\x00", 1)[1]
                rest = body[2:].split(b"\x00", 2)
                text = rest[2] if len(rest) == 3 else b""
                return zlib.decompress(text) if body[0] == 1 else text
    return side.read_bytes() if side.exists() else b""


def sidecar_path(path: Path) -> Path:
    """`IMG_0001.HEIC` -> `IMG_0001.xmp`, the Adobe convention - unless another
    photo of the same name already uses that sidecar, when it is
    `IMG_0001.HEIC.xmp`, digiKam's."""
    path = Path(path)
    plain = path.with_suffix(".xmp")
    siblings = [p for p in path.parent.glob(path.stem + ".*")
                if p.suffix.lower() != ".xmp" and p != path] if path.parent.exists() else []
    return path.with_name(path.name + ".xmp") if siblings else plain


# --- writing -----------------------------------------------------------------------------

def _backup(path: Path, backup_root: Path) -> int:
    drive = path.drive.rstrip(":").rstrip("\\") or "root"
    relative = Path(*path.parts[1:]) if path.anchor else path
    target = backup_root / drive / relative
    if target.exists():
        return 0                                       # the first copy is the original
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target.stat().st_size


def _write_sidecar(path: Path, meta: PhotoMetadata) -> bool:
    side = sidecar_path(path)
    old = side.read_bytes() if side.exists() else b""
    packet = merge_xmp(old, meta) if old else build_xmp(meta)
    if packet == old:
        return False
    temp = side.with_name(side.name + ".leasha-tmp")
    temp.write_bytes(packet)
    os.replace(temp, side)
    return True


def write_photo_metadata(items: Iterable[PhotoMetadata], *, backup_root: Path,
                         where: str = INSIDE,
                         on_written: Optional[Callable[[PhotoMetadata, int, int], None]] = None,
                         progress: Optional[Callable[[int], None]] = None,
                         should_stop: Optional[Callable[[], bool]] = None) -> WriteResult:
    """Write each photo's people and description. Never raises for one photo.

    `on_written(meta, new_size, mtime_ns)` is told each photo that changed, so
    the index can record its new size. `progress(done)` after each one."""
    result = WriteResult()
    backup_root = Path(backup_root)
    for done, meta in enumerate(items, start=1):
        if should_stop is not None and should_stop():
            break
        path = Path(meta.path)
        try:
            ext = path.suffix.lower()
            inside = where == INSIDE and ext in (".jpg", ".jpeg", ".jpe", ".png")
            if not inside:
                if _write_sidecar(path, meta):
                    result.sidecars += 1
                else:
                    result.unchanged += 1
            else:
                data = path.read_bytes()
                changed = _jpeg_with(data, meta) if ext != ".png" else _png_with(data, meta)
                if changed == data:
                    result.unchanged += 1
                else:
                    stat = path.stat()
                    result.backed_up_bytes += _backup(path, backup_root)
                    temp = path.with_name(path.name + ".leasha-tmp")
                    temp.write_bytes(changed)
                    os.replace(temp, path)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    result.written += 1
                    if on_written is not None:
                        on_written(meta, len(changed), stat.st_mtime_ns)
        except Exception as exc:                       # noqa: BLE001 - one photo, not all
            result.failed += 1
            if len(result.problems) < 50:
                result.problems.append(f"{path.name}: {exc}")
            _log.warning("could not write names into {}: {}", path, exc)
        if progress is not None:
            progress(done)
    return result


def run_write(store: Any, file_ids: Optional[Sequence[int]], *, where: str,
              backup_root: Path, progress: dict, stop: threading.Event) -> Any:
    """Read what to write, then write it. **Worker.** `progress` is shared with
    the window: `{"total": n, "done": n}`."""
    rows = store.photo_metadata_rows(file_ids)
    progress["total"] = len(rows)
    items = [PhotoMetadata(file_id, path, people, description)
             for file_id, path, people, description in rows]
    return write_photo_metadata(
        items, backup_root=backup_root, where=where,
        # The index learns each photo's new size, so the next run reads none again.
        on_written=lambda meta, size, mtime: store.note_photo_rewritten(
            meta.file_id, size, mtime),
        progress=lambda count: progress.__setitem__("done", count),
        should_stop=stop.is_set)
