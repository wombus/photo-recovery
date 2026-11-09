"""Recognise an MP4 video (GoPro .MP4 and .LRV files) by walking its boxes.

An MP4 file is a sequence of *boxes* (also called atoms). Each box starts with
an 8-byte header: a 4-byte big-endian size (which counts the header too) and a
4-byte ASCII type. A size of 1 means a 64-bit size follows the type.

    ftyp   "this is an MP4"              <- the signature we search for
    mdat   the audio/video frames        (almost all of the file)
    moov   the index: where each frame lives, duration, creation date

Because each box says how big it is, we can hop ftyp -> mdat -> moov and know
exactly where the file ends. The catch is that a video cannot play without its
moov index. Action cameras usually write moov at the *end*, once recording
stops, so a video whose tail was overwritten keeps its frames but loses the
index. Tools such as untrunc can rebuild it (see README).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

SIGNATURE = b"ftyp"
SIGNATURE_OFFSET = 4  # "ftyp" sits 4 bytes into the file, after the size field

_TOP_LEVEL = frozenset({b"ftyp", b"mdat", b"moov", b"free", b"skip", b"wide", b"udta",
                        b"uuid", b"meta", b"pdin", b"moof", b"mfra", b"sidx"})
_AFTER_END = frozenset({b"free", b"skip", b"udta", b"uuid", b"meta"})
_MAX_MOOV = 64 << 20
_EPOCH = datetime(1904, 1, 1)  # MP4 timestamps count seconds from 1904
_HERO = re.compile(rb"HERO\d{1,2}(?: [A-Z][a-z]+)?")


@dataclass
class Mp4Info:
    size: int
    complete: bool
    brand: str = ""
    created: datetime | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    firmware: str | None = None
    camera: str | None = None
    note: str = ""


def parse(reader, offset: int) -> Mp4Info | None:
    """Describe the MP4 starting at ``offset``, or return None if there isn't one."""
    head = reader.read(offset, 16)
    if len(head) < 16 or head[4:8] != SIGNATURE:
        return None
    ftyp_size = struct.unpack(">I", head[:4])[0]
    brand = head[8:12]
    if not 16 <= ftyp_size <= 1024 or not all(32 <= c < 127 for c in brand):
        return None

    info = Mp4Info(size=0, complete=False, brand=brand.decode("ascii").strip())
    pos, seen, moov, note = offset, set(), None, ""
    while pos + 8 <= reader.size:
        h = reader.read(pos, 16)
        if len(h) < 8:
            break
        size, typ = struct.unpack(">I4s", h[:8])
        header = 8
        if size == 1:  # 64-bit "largesize" follows
            if len(h) < 16:
                break
            size, header = struct.unpack(">Q", h[8:16])[0], 16
        elif size == 0:  # "runs to the end of the file"
            size = reader.size - pos
        if typ not in _TOP_LEVEL or size < header:
            break  # not a box: the file ended at pos
        if pos != offset and typ == b"ftyp":
            break  # the next video starts here
        if b"mdat" in seen and moov and typ not in _AFTER_END:
            break
        if pos + size > reader.size:
            note = f"the {typ.decode()} box runs past the end of the card"
            seen.add(typ)
            pos = reader.size
            break
        if typ == b"moov":
            moov = (pos + header, pos + size)
        seen.add(typ)
        pos += size

    if b"mdat" not in seen and moov is None:
        return None  # only a stray header survived
    info.size = pos - offset
    info.complete = b"mdat" in seen and moov is not None and not note
    if moov:
        _read_moov(info, reader, *moov)
    if not note and not info.complete:
        note = ("video frames survived but the moov index did not (needs repair)"
                if moov is None else "index survived but the video frames did not")
    info.note = note
    return info


def _children(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = struct.unpack_from(">Q", buf, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield typ, pos + header, pos + size
        pos += size


def _read_moov(info: Mp4Info, reader, start: int, end: int) -> None:
    if end - start > _MAX_MOOV:
        return
    buf = reader.read(start, end - start)
    for typ, s, e in _children(buf, 0, len(buf)):
        if typ == b"mvhd" and e - s >= 20:
            if buf[s] == 1 and e - s >= 32:
                created, _, timescale, duration = struct.unpack_from(">QQIQ", buf, s + 4)
            else:
                created, _, timescale, duration = struct.unpack_from(">IIII", buf, s + 4)
            if created:
                when = _EPOCH + timedelta(seconds=created)
                if 1990 <= when.year <= 2100:
                    info.created = when
            if timescale:
                info.duration_s = round(duration / timescale, 2)
        elif typ == b"trak":
            for t2, s2, e2 in _children(buf, s, e):
                if t2 == b"tkhd":
                    off = s2 + (88 if buf[s2] == 1 else 76)
                    if off + 8 <= e2:
                        w, h = struct.unpack_from(">II", buf, off)
                        if (w >> 16) > (info.width or 0):  # audio tracks are 0x0
                            info.width, info.height = w >> 16, h >> 16
        elif typ == b"udta":
            for t2, s2, e2 in _children(buf, s, e):
                if t2 == b"FIRM":  # GoPro firmware, e.g. HD5.02.02.70.00 on a HERO5 Black
                    info.firmware = buf[s2:e2].split(b"\x00")[0].decode("latin-1").strip() or None
    m = _HERO.search(buf)
    if m:
        info.camera = m.group().decode("ascii")
