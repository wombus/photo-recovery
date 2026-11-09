"""Build GoPro-like JPEG and MP4 files byte by byte, for tests and the simulator.

The files are structurally valid (every segment/box is well formed and sized
correctly) with random bytes standing in for compressed pixels and frames.
"""

from __future__ import annotations

import random
import struct
from datetime import datetime


class BytesReader:
    """Same interface as cardscan.reader.BlockReader, backed by memory."""

    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            return b""
        return self.data[offset:offset + length]


# --- JPEG ------------------------------------------------------------------

def _ascii(text: str) -> bytes:
    return text.encode("latin-1") + b"\x00"


def _ifd_len(entries) -> int:
    data = sum(len(v) + len(v) % 2 for _, _, _, v in entries if len(v) > 4)
    return 2 + 12 * len(entries) + 4 + data


def _pack_ifd(entries, at: int, e: str, next_ifd: int = 0) -> bytes:
    """Pack one TIFF directory at file offset ``at``, its out-of-line data after it."""
    entries = sorted(entries)
    data_at = at + 2 + 12 * len(entries) + 4
    head, data = struct.pack(e + "H", len(entries)), b""
    for tag, typ, count, value in entries:
        if len(value) <= 4:
            field = value.ljust(4, b"\x00")
        else:
            field = struct.pack(e + "I", data_at + len(data))
            data += value + b"\x00" * (len(value) % 2)
        head += struct.pack(e + "HHI", tag, typ, count) + field
    return head + struct.pack(e + "I", next_ifd) + data


def exif_block(taken: datetime | None, make: str = "GoPro", model: str = "HERO5 Black",
               thumbnail: bytes | None = None, big_endian: bool = False) -> bytes:
    """The TIFF structure that follows b'Exif\\0\\0' in a JPEG's APP1 segment."""
    e = ">" if big_endian else "<"
    stamp = _ascii(taken.strftime("%Y:%m:%d %H:%M:%S")) if taken else None
    ifd0 = [(0x010F, 2, len(_ascii(make)), _ascii(make)),
            (0x0110, 2, len(_ascii(model)), _ascii(model)),
            (0x8769, 4, 1, b"\x00" * 4)]  # placeholder: pointer to the Exif IFD
    if stamp:
        ifd0.append((0x0132, 2, len(stamp), stamp))
    exif = [(0x9003, 2, len(stamp), stamp), (0x9004, 2, len(stamp), stamp)] if stamp else \
           [(0xA002, 4, 1, struct.pack(e + "I", 4000))]
    exif_at = 8 + _ifd_len(ifd0)
    ifd0 = [(t, ty, c, struct.pack(e + "I", exif_at)) if t == 0x8769 else (t, ty, c, v)
            for t, ty, c, v in ifd0]
    ifd1_at = exif_at + _ifd_len(exif)
    out = (b"II*\x00" if e == "<" else b"MM\x00*") + struct.pack(e + "I", 8)
    out += _pack_ifd(ifd0, 8, e, next_ifd=ifd1_at if thumbnail else 0)
    out += _pack_ifd(exif, exif_at, e)
    if thumbnail:
        thumb_at = ifd1_at + 2 + 12 * 2 + 4
        ifd1 = [(0x0201, 4, 1, struct.pack(e + "I", thumb_at)),
                (0x0202, 4, 1, struct.pack(e + "I", len(thumbnail)))]
        out += _pack_ifd(ifd1, ifd1_at, e) + thumbnail
    return out


def _segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def synthetic_jpeg(taken: datetime | None = None, *, pixels: int = 40_000, seed: int = 0,
                   width: int = 4000, height: int = 3000, model: str = "HERO5 Black",
                   with_exif: bool = True, thumbnail: bool = True,
                   big_endian: bool = False) -> bytes:
    rng = random.Random(seed)
    out = b"\xff\xd8"
    if with_exif:
        thumb = synthetic_jpeg(pixels=300, seed=seed + 1, width=160, height=120,
                               with_exif=False) if thumbnail else None
        out += _segment(0xE1, b"Exif\x00\x00" + exif_block(taken, model=model, thumbnail=thumb,
                                                             big_endian=big_endian))
    else:
        out += _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    out += _segment(0xDB, b"\x00" + bytes(rng.randrange(1, 100) for _ in range(64)))
    out += _segment(0xC0, b"\x08" + struct.pack(">HH", height, width)
                    + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    out += _segment(0xC4, b"\x00" + bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0])
                    + bytes(range(12)))
    out += _segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
    # Compressed pixels: random bytes with every 0xFF "stuffed" as FF 00, and
    # restart markers (FF D0-D7) every few KB, just like a real encoder.
    scan = []
    for i in range(0, pixels, 4096):
        scan.append(rng.randbytes(min(4096, pixels - i)).replace(b"\xff", b"\xff\x00"))
        scan.append(bytes([0xFF, 0xD0 + (i // 4096) % 8]))
    return out + b"".join(scan[:-1]) + b"\xff\xd9"


# --- MP4 -------------------------------------------------------------------

_EPOCH = datetime(1904, 1, 1)


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def synthetic_mp4(created: datetime | None, *, frames: int = 200_000, seed: int = 0,
                  width: int = 1920, height: int = 1080, duration_s: float = 12.0,
                  firmware: str = "HD5.02.02.70.00", with_moov: bool = True,
                  large_mdat: bool = False) -> bytes:
    rng = random.Random(seed)
    stamp = int((created - _EPOCH).total_seconds()) if created else 0
    ftyp = box(b"ftyp", b"mp41" + struct.pack(">I", 0x20130114) + b"mp41")
    payload = rng.randbytes(frames)
    if large_mdat:
        mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", len(payload) + 16) + payload
    else:
        mdat = box(b"mdat", payload)
    if not with_moov:
        return ftyp + mdat
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", stamp, stamp, 1000, int(duration_s * 1000))
               + struct.pack(">IH", 0x10000, 0x100) + b"\x00" * 10 + matrix + b"\x00" * 24
               + struct.pack(">I", 2))
    tkhd = box(b"tkhd", b"\x00\x00\x00\x07" + struct.pack(">IIIII", stamp, stamp, 1, 0, int(duration_s * 1000))
               + b"\x00" * 8 + b"\x00" * 8 + matrix + struct.pack(">II", width << 16, height << 16))
    udta = box(b"udta", box(b"FIRM", firmware.encode()))
    moov = box(b"moov", mvhd + box(b"trak", tkhd) + udta)
    return ftyp + mdat + moov
