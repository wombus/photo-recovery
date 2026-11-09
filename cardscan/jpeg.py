"""Recognise a JPEG photo by its own internal structure.

A JPEG is a chain of *segments*. Each starts with a two-byte marker (0xFF then
a type byte) and, for most types, a two-byte big-endian length:

    FF D8                      SOI   start of image (no length)
    FF E1 <len> "Exif\\0\\0"…  APP1  EXIF metadata: camera model, date taken
    FF DB <len> …              DQT   quantisation tables
    FF C0 <len> …              SOF0  frame header: width and height
    FF C4 <len> …              DHT   Huffman tables
    FF DA <len> …              SOS   start of scan, then the compressed pixels
    …compressed pixel data…          (a real 0xFF byte in here is written FF 00)
    FF D9                      EOI   end of image

Every segment says how long it is, so we can hop from header to header without
decoding anything. The compressed pixel data ends at the first FF that is not
followed by 00 or a restart code (D0-D7). That is how we find where a photo
ends when there is no filesystem left to tell us.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from datetime import datetime

SIGNATURE = b"\xff\xd8\xff"
MAX_SIZE = 64 << 20  # GoPro JPEGs are 2-6 MB; nothing real is bigger than this

# Frame headers (baseline, progressive, ...) that carry width/height.
_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
# Segments that may legitimately come straight after SOI. Requiring one of
# these rejects random byte runs that merely happen to start with FF D8 FF.
_FIRST = frozenset(range(0xE0, 0xF0)) | {0xDB, 0xC4, 0xDD, 0xFE} | _SOF
# Inside compressed data: an FF followed by anything except 00 or RST0-7.
_NEXT_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7]")
# A whole sector of zeros never occurs in compressed pixels; erased or never
# written flash does look like this, so it marks where real data stops.
_BLANK = b"\x00" * 512
_BLOCK = 1 << 20


@dataclass
class JpegInfo:
    size: int  # bytes to carve, starting at the SOI marker
    complete: bool  # True when we reached a proper end-of-image marker
    width: int | None = None
    height: int | None = None
    make: str | None = None
    model: str | None = None
    taken: datetime | None = None
    note: str = ""


def parse(reader, offset: int, max_size: int = MAX_SIZE) -> JpegInfo | None:
    """Describe the JPEG starting at ``offset``, or return None if there isn't one.

    A photo whose header survived but whose end was damaged is returned with
    ``complete=False`` and ``size`` set to where the damage starts: carving that
    much still shows the undamaged top part of the picture.
    """
    head = reader.read(offset, 4)
    if len(head) < 4 or head[:3] != SIGNATURE or head[3] not in _FIRST:
        return None

    info = JpegInfo(size=0, complete=False)
    limit = min(offset + max_size, reader.size)
    pos = offset + 2
    in_pixels = False  # have we reached the compressed image data yet?
    fill = 0

    for _ in range(4096):  # real files have a few dozen segments at most
        b = reader.read(pos, 4)
        if len(b) < 2 or b[0] != 0xFF:
            return _cut(info, offset, pos, in_pixels, "data stops looking like a JPEG here")
        marker = b[1]
        if marker == 0xFF:  # optional fill byte before a marker
            fill += 1
            if fill > 16:
                return _cut(info, offset, pos, in_pixels, "erased (0xFF) area")
            pos += 1
            continue
        fill = 0
        if marker == 0xD9:  # EOI: the end of the image
            if not in_pixels:
                return None
            info.size, info.complete = pos + 2 - offset, True
            return info
        if marker == 0xD8:
            return _cut(info, offset, pos, in_pixels, "another photo was written over the rest")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            pos += 2
            continue
        if marker == 0x00 or len(b) < 4:
            return _cut(info, offset, pos, in_pixels, "data stops looking like a JPEG here")

        length = (b[2] << 8) | b[3]
        if length < 2 or pos + 2 + length > limit:
            return _cut(info, offset, pos, in_pixels, "segment length runs off the end")
        body = pos + 4
        if marker == 0xE1 and info.taken is None and info.model is None:
            data = reader.read(body, length - 2)
            if data.startswith(b"Exif\x00\x00"):
                tags = read_exif(data[6:])
                info.make, info.model = tags.get("make"), tags.get("model")
                info.taken = _parse_date(tags.get("taken"))
        elif marker in _SOF and length >= 7:
            sof = reader.read(body, 5)
            if len(sof) == 5:
                info.height, info.width = struct.unpack(">HH", sof[1:5])
        pos += 2 + length

        if marker == 0xDA:  # SOS: compressed pixels follow the header
            if info.width is None:
                return None
            in_pixels = True
            pos, why = _skip_pixels(reader, pos, limit)
            if why != "marker":
                note = "the rest of the card here is blank" if why == "blank" else "no end marker found"
                return _cut(info, offset, pos, in_pixels, note)

    return _cut(info, offset, pos, in_pixels, "too many segments")


def _cut(info: JpegInfo, offset: int, pos: int, in_pixels: bool, note: str) -> JpegInfo | None:
    """A damaged photo is only worth keeping if some pixels survived."""
    if not in_pixels:
        return None
    info.size, info.complete, info.note = pos - offset, False, note
    return info


def _skip_pixels(reader, pos: int, limit: int) -> tuple[int, str]:
    """Return (offset, reason) for whatever ends the compressed data at ``pos``."""
    while pos < limit:
        # Read one extra byte so an FF at the very end of a block is checked
        # together with the byte that follows it.
        block = reader.read(pos, min(_BLOCK, limit - pos) + 1)
        if len(block) < 2:
            return pos + len(block), "end"
        m = _NEXT_MARKER.search(block)
        z = block.find(_BLANK)
        if z != -1 and (m is None or z < m.start()):
            return pos + z, "blank"
        if m:
            return pos + m.start(), "marker"
        pos += len(block) - 1
    return limit, "end"


# --- EXIF ------------------------------------------------------------------
# EXIF is a small TIFF file inside APP1: a byte-order mark ("II" little-endian
# or "MM" big-endian), then directories ("IFDs") of 12-byte tag entries.
# IFD0 holds Make/Model and a pointer to the Exif IFD, which holds the date
# the shutter fired (DateTimeOriginal).

_TAG_MAKE, _TAG_MODEL, _TAG_DATETIME = 0x010F, 0x0110, 0x0132
_TAG_EXIF_IFD, _TAG_ORIGINAL, _TAG_DIGITIZED = 0x8769, 0x9003, 0x9004


def read_exif(tiff: bytes) -> dict:
    """Return {'make', 'model', 'taken'} from an EXIF block (keys may be missing)."""
    if tiff[:4] == b"II*\x00":
        e = "<"
    elif tiff[:4] == b"MM\x00*":
        e = ">"
    else:
        return {}
    try:
        ifd0 = _ifd(tiff, struct.unpack_from(e + "I", tiff, 4)[0], e)
        out = {}
        for tag, key in ((_TAG_MAKE, "make"), (_TAG_MODEL, "model")):
            value = _ascii(tiff, ifd0.get(tag), e)
            if value:
                out[key] = value
        dates = []
        ptr = ifd0.get(_TAG_EXIF_IFD)
        if ptr:
            sub = _ifd(tiff, struct.unpack(e + "I", ptr[2])[0], e)
            dates += [_ascii(tiff, sub.get(_TAG_ORIGINAL), e), _ascii(tiff, sub.get(_TAG_DIGITIZED), e)]
        dates.append(_ascii(tiff, ifd0.get(_TAG_DATETIME), e))
        taken = next((d for d in dates if d), None)
        if taken:
            out["taken"] = taken
        return out
    except struct.error:
        return {}


def _ifd(tiff: bytes, off: int, e: str) -> dict:
    count = struct.unpack_from(e + "H", tiff, off)[0]
    entries = {}
    for i in range(min(count, 512)):
        p = off + 2 + 12 * i
        if p + 12 > len(tiff):
            break
        tag, typ, n = struct.unpack_from(e + "HHI", tiff, p)
        entries[tag] = (typ, n, tiff[p + 8:p + 12])
    return entries


def _ascii(tiff: bytes, entry, e: str) -> str | None:
    if not entry or entry[0] != 2:  # type 2 = ASCII
        return None
    _, n, raw = entry
    if n <= 4:
        data = raw[:n]
    else:
        start = struct.unpack(e + "I", raw)[0]
        data = tiff[start:start + n]
    text = data.split(b"\x00")[0].decode("latin-1").strip()
    return text or None


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None  # e.g. "0000:00:00 00:00:00" from a camera with no clock set
