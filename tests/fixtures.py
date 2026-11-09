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
    # Compressed pixels: random bytes with every 0xFF "stuffed" as FF 00, and
    # restart markers (FF D0-D7) between pieces, just like a real encoder. The
    # DRI segment's interval is chosen so the marker count matches the size.
    pieces = -(-pixels // 4096)
    if pieces > 1:
        mcus = -(-width // 16) * -(-height // 16)  # 4:2:0 -> 16x16 blocks per MCU
        interval = -(-mcus // pieces)
        assert -(-mcus // interval) == pieces, "pick a size with more 16x16 blocks"
        out += _segment(0xDD, struct.pack(">H", interval))
    out += _segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
    scan = []
    for i in range(0, pixels, 4096):
        scan.append(rng.randbytes(min(4096, pixels - i)).replace(b"\xff", b"\xff\x00"))
        scan.append(bytes([0xFF, 0xD0 + (i // 4096) % 8]))
    return out + b"".join(scan[:-1]) + b"\xff\xd9"


# --- MP4 ---------------------------------------------------------------------
# A GoPro-shaped MP4: ftyp, then mdat with video/audio/telemetry interleaved in
# chunks, then the moov index at the end, with real sample tables (stts, stss,
# stsc, stsz, stco). Video samples use the length-prefixed H.264 NAL layout
# ("AVCC") that cameras write, so tools can validate them like real frames.

_EPOCH = datetime(1904, 1, 1)
_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def full(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def _avc1(width: int, height: int) -> bytes:
    sps, pps = b"\x67\x64\x00\x28\xac\xd9", b"\x68\xeb\xe3\xcb"
    avcc = (b"\x01\x64\x00\x28\xff\xe1" + struct.pack(">H", len(sps)) + sps
            + b"\x01" + struct.pack(">H", len(pps)) + pps)
    entry = (b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 16 + struct.pack(">HH", width, height)
             + struct.pack(">II", 0x480000, 0x480000) + b"\x00" * 4 + struct.pack(">H", 1)
             + b"\x00" * 32 + struct.pack(">Hh", 0x18, -1) + box(b"avcC", avcc))
    return box(b"avc1", entry)


def _stbl(entry: bytes, sizes, durations, chunks, sync=None) -> bytes:
    """chunks: list of (file_offset, samples_in_chunk)."""
    runs = []
    for i, (_, n) in enumerate(chunks, start=1):
        if not runs or runs[-1][1] != n:
            runs.append((i, n))
    stts = []
    for d in durations:
        if stts and stts[-1][1] == d:
            stts[-1][0] += 1
        else:
            stts.append([1, d])
    out = full(b"stsd", struct.pack(">I", 1) + entry)
    out += full(b"stts", struct.pack(">I", len(stts)) + b"".join(struct.pack(">II", *e) for e in stts))
    if sync is not None:
        out += full(b"stss", struct.pack(">I", len(sync)) + b"".join(struct.pack(">I", i + 1) for i in sync))
    out += full(b"stsc", struct.pack(">I", len(runs)) + b"".join(struct.pack(">III", f, n, 1) for f, n in runs))
    out += full(b"stsz", struct.pack(">II", 0, len(sizes)) + b"".join(struct.pack(">I", n) for n in sizes))
    out += full(b"stco", struct.pack(">I", len(chunks)) + b"".join(struct.pack(">I", o) for o, _ in chunks))
    return box(b"stbl", out)


def _trak(track_id, handler, timescale, durations, stbl, header, width=0, height=0, movie_ts=1000):
    media = sum(durations)
    movie = media * movie_ts // timescale
    tkhd = full(b"tkhd", struct.pack(">IIIII", 0, 0, track_id, 0, movie) + b"\x00" * 16 + _MATRIX
                + struct.pack(">II", width << 16, height << 16))
    mdhd = full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, media, 0x55C4, 0))
    hdlr = full(b"hdlr", b"\x00" * 4 + handler + b"\x00" * 12 + b"GoPro\x00")
    dinf = box(b"dinf", full(b"dref", struct.pack(">I", 1) + full(b"url ", b"", 0)[:8] + b"\x00\x00\x00\x01"))
    minf = box(b"minf", header + dinf + stbl)
    return box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + minf))


def synthetic_mp4(created: datetime | None, *, frames: int = 200_000, seed: int = 0,
                  width: int = 1920, height: int = 1080, duration_s: float = 12.0,
                  firmware: str = "HD5.02.02.70.00", with_moov: bool = True,
                  large_mdat: bool = False, gop: int = 8) -> bytes:
    """``frames`` is roughly how many bytes of audio/video data to generate."""
    rng = random.Random(seed)
    nv = max(gop, round(duration_s * 30000 / 1001))
    na = max(4, round(duration_s * 48000 / 1024))
    nm = max(1, round(duration_s))
    vsize = max(64, frames * 9 // 10 // nv)
    asize = max(16, (frames - vsize * nv) // na)
    video = []
    for i in range(nv):
        if i % gop == 0:  # keyframe: an SEI NAL then an IDR slice
            sei = b"\x06" + rng.randbytes(15)
            idr = b"\x65" + rng.randbytes(vsize - len(sei) - 9)
            video.append(struct.pack(">I", len(sei)) + sei + struct.pack(">I", len(idr)) + idr)
        else:
            nal = b"\x41" + rng.randbytes(vsize - 5)
            video.append(struct.pack(">I", len(nal)) + nal)
    tracks = [  # samples, duration per sample, timescale, samples per chunk
        [video, 1001, 30000, 4], [[rng.randbytes(asize) for _ in range(na)], 1024, 48000, 6],
        [[b"DEVC" + rng.randbytes(60) for _ in range(nm)], 1000, 1000, 1],
    ]
    ftyp = box(b"ftyp", b"mp41" + struct.pack(">I", 0x20130114) + b"mp41")
    header_len = 16 if large_mdat else 8
    payload, chunks = bytearray(), [[] for _ in tracks]
    nxt, clock = [0] * len(tracks), [0.0] * len(tracks)
    while any(nxt[t] < len(tracks[t][0]) for t in range(len(tracks))):
        t = min((t for t in range(len(tracks)) if nxt[t] < len(tracks[t][0])), key=lambda t: clock[t])
        samples, dur, ts, per = tracks[t]
        take = samples[nxt[t]:nxt[t] + per]
        chunks[t].append((len(ftyp) + header_len + len(payload), len(take)))
        for sample in take:
            payload += sample
        nxt[t] += len(take)
        clock[t] += len(take) * dur / ts
    if large_mdat:
        mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", len(payload) + 16) + payload
    else:
        mdat = box(b"mdat", bytes(payload))
    if not with_moov:
        return ftyp + mdat
    stamp = int((created - _EPOCH).total_seconds()) if created else 0
    mvhd = full(b"mvhd", struct.pack(">IIII", stamp, stamp, 1000, int(duration_s * 1000))
                + struct.pack(">IH", 0x10000, 0x100) + b"\x00" * 10 + _MATRIX + b"\x00" * 24
                + struct.pack(">I", 4))
    mp4a = box(b"mp4a", b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
               + struct.pack(">HHHHI", 2, 16, 0, 0, 48000 << 16))
    gpmd = box(b"gpmd", b"\x00" * 6 + struct.pack(">H", 1))
    traks = [
        _trak(1, b"vide", 30000, [1001] * nv,
              _stbl(_avc1(width, height), [len(v) for v in video], [1001] * nv, chunks[0],
                    sync=list(range(0, nv, gop))),
              full(b"vmhd", b"\x00" * 8, 0), width, height),
        _trak(2, b"soun", 48000, [1024] * na,
              _stbl(mp4a, [asize] * na, [1024] * na, chunks[1]), full(b"smhd", b"\x00" * 4)),
        _trak(3, b"meta", 1000, [1000] * nm,
              _stbl(gpmd, [64] * nm, [1000] * nm, chunks[2]), full(b"nmhd", b"")),
    ]
    udta = box(b"udta", box(b"FIRM", firmware.encode()))
    return ftyp + mdat + box(b"moov", mvhd + udta + b"".join(traks))
