"""Rebuild the surviving end of a video whose beginning was overwritten.

GoPro writes each video as ftyp, then mdat (the frames), then moov (the index)
at the end. When new footage overwrites a card from the start, the one old
video sitting on the edge of the overwritten area loses its *beginning* but
keeps its *end*, and the end includes the complete index.

The index lists every frame's size and its position relative to the start of
the original file. That is enough to:

1. Work out where the file must have started on the card. The last frame ends
   right where the index begins, so start = index position - end of last frame.
2. Check every frame in place. Each H.264 frame is a chain of length-prefixed
   pieces ("NAL units") whose lengths must add up exactly to the frame size the
   index records, which random or overwritten data essentially never does.
   Walking back from the end, the first frame that fails marks the edge of the
   overwritten area.
3. Write a new, playable MP4 from the first keyframe after that edge, with
   audio from the same moment, and a fresh index describing just those frames.

PhotoRec and similar tools cannot do this: they need a file's beginning.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

from .mp4 import _children

_EPOCH = datetime(1904, 1, 1)
_MAX_MOOV = 64 << 20
_MAX_GAP = 1 << 20  # how far before the index we look for the end of the frames
_VIDEO_CODECS = {b"avc1": "avc", b"avc3": "avc", b"hvc1": "hevc", b"hev1": "hevc"}


@dataclass
class Track:
    handler: bytes  # b"vide", b"soun", b"meta" (GoPro telemetry), ...
    codec: bytes
    timescale: int
    trak: bytes  # original trak payload, reused when rebuilding
    stsd: bytes  # original sample description payload (codec settings)
    sizes: list
    offsets: list  # relative to the start of the original file
    durations: list
    ctts: list | None
    ctts_version: int
    sync: set | None  # keyframe indices; None means every sample is a keyframe
    nal_len: int | None
    width: int = 0
    height: int = 0


@dataclass
class Index:
    at: int  # card offset of the moov box
    size: int
    mvhd: bytes
    timescale: int
    duration: int
    created: datetime | None
    tracks: list
    keep: list  # other moov children worth copying (udta: GoPro firmware etc.)
    firmware: str | None


@dataclass
class Plan:
    index: Index
    base: int | None  # where the original file started on the card
    video: Track | None
    first_ok: int  # first video frame (in order) that is intact through to the end
    start: int  # first keyframe at or after first_ok: where playback can begin
    total_s: float
    recovered_s: float

    @property
    def frames(self) -> int:
        return len(self.video.sizes) - self.start if self.video and self.base is not None else 0


# --- reading the index -------------------------------------------------------

def read_index(reader, at: int) -> Index | None:
    head = reader.read(at, 8)
    if len(head) < 8 or head[4:8] != b"moov":
        return None
    size = struct.unpack(">I", head[:4])[0]
    if not 64 <= size <= _MAX_MOOV or at + size > reader.size:
        return None
    buf = reader.read(at + 8, size - 8)
    kids = list(_children(buf, 0, len(buf)))
    if not kids or kids[-1][2] != len(buf):
        return None  # children must tile the box exactly, or it isn't a real moov
    mvhd, tracks, keep, firmware = None, [], [], None
    for typ, s, e in kids:
        if typ == b"mvhd":
            mvhd = buf[s:e]
        elif typ == b"trak":
            t = _read_track(buf[s:e])
            if t:
                tracks.append(t)
        elif typ == b"udta":
            keep.append((typ, buf[s:e]))
            for t2, s2, e2 in _children(buf, s, e):
                if t2 == b"FIRM":
                    firmware = buf[s2:e2].split(b"\x00")[0].decode("latin-1").strip() or None
    if not mvhd or not tracks or len(mvhd) < 20:
        return None
    if mvhd[0] == 1 and len(mvhd) >= 32:
        created, _, timescale, duration = struct.unpack_from(">QQIQ", mvhd, 4)
    else:
        created, _, timescale, duration = struct.unpack_from(">IIII", mvhd, 4)
    when = _EPOCH + timedelta(seconds=created) if created else None
    if when and not 1990 <= when.year <= 2100:
        when = None
    return Index(at, size, mvhd, timescale or 1000, duration, when, tracks, keep, firmware)


def _read_track(buf: bytes) -> Track | None:
    handler = codec = None
    timescale, tables, width, height = 0, {}, 0, 0
    for typ, s, e in _children(buf, 0, len(buf)):
        if typ == b"tkhd":
            off = s + (88 if buf[s] == 1 else 76)
            if off + 8 <= e:
                w, h = struct.unpack_from(">II", buf, off)
                width, height = w >> 16, h >> 16
        elif typ == b"mdia":
            for t2, s2, e2 in _children(buf, s, e):
                if t2 == b"mdhd" and e2 - s2 >= 20:
                    timescale = struct.unpack_from(">I", buf, s2 + (20 if buf[s2] == 1 else 12))[0]
                elif t2 == b"hdlr" and e2 - s2 >= 12:
                    handler = buf[s2 + 8:s2 + 12]
                elif t2 == b"minf":
                    for t3, s3, e3 in _children(buf, s2, e2):
                        if t3 == b"stbl":
                            tables = {t4: buf[s4:e4] for t4, s4, e4 in _children(buf, s3, e3)}
    try:
        stsd = tables[b"stsd"]
        codec = stsd[12:16]
        sizes = _stsz(tables[b"stsz"])
        n = len(sizes)
        chunk_offsets = _chunk_offsets(tables)
        offsets = _sample_offsets(tables[b"stsc"], chunk_offsets, sizes)
        durations = _expand(tables[b"stts"], n)
        ctts, ctts_version = None, 0
        if b"ctts" in tables:
            ctts_version = tables[b"ctts"][0]
            ctts = _expand(tables[b"ctts"], n, signed=ctts_version == 1)
        sync = None
        if b"stss" in tables:
            count = struct.unpack_from(">I", tables[b"stss"], 4)[0]
            sync = {i - 1 for i in struct.unpack_from(f">{count}I", tables[b"stss"], 8)}
    except (KeyError, struct.error, ValueError):
        return None
    if not handler or not timescale or len(offsets) != n or n == 0:
        return None
    return Track(handler, codec, timescale, buf, stsd, sizes, offsets, durations, ctts,
                 ctts_version, sync, _nal_length_size(stsd, codec), width, height)


def _stsz(b: bytes) -> list:
    fixed, count = struct.unpack_from(">II", b, 4)
    return [fixed] * count if fixed else list(struct.unpack_from(f">{count}I", b, 12))


def _chunk_offsets(tables: dict) -> list:
    if b"co64" in tables:
        count = struct.unpack_from(">I", tables[b"co64"], 4)[0]
        return list(struct.unpack_from(f">{count}Q", tables[b"co64"], 8))
    count = struct.unpack_from(">I", tables[b"stco"], 4)[0]
    return list(struct.unpack_from(f">{count}I", tables[b"stco"], 8))


def _sample_offsets(stsc: bytes, chunks: list, sizes: list) -> list:
    count = struct.unpack_from(">I", stsc, 4)[0]
    runs = [struct.unpack_from(">III", stsc, 8 + 12 * i)[:2] for i in range(count)]
    offsets, k, r = [], 0, 0
    for ci, pos in enumerate(chunks, start=1):
        while r + 1 < len(runs) and runs[r + 1][0] <= ci:
            r += 1
        for _ in range(runs[r][1] if runs else 0):
            if k >= len(sizes):
                break
            offsets.append(pos)
            pos += sizes[k]
            k += 1
    return offsets


def _expand(b: bytes, n: int, signed: bool = False) -> list:
    count = struct.unpack_from(">I", b, 4)[0]
    out = []
    for i in range(count):
        c, v = struct.unpack_from(">Ii" if signed else ">II", b, 8 + 8 * i)
        out.extend([v] * c)
    if len(out) < n:
        out.extend([out[-1] if out else 0] * (n - len(out)))
    return out[:n]


def _nal_length_size(stsd: bytes, codec: bytes) -> int | None:
    kind = _VIDEO_CODECS.get(codec)
    if kind == "avc":
        i = stsd.find(b"avcC")
        return (stsd[i + 8] & 3) + 1 if i != -1 and i + 8 < len(stsd) else None
    if kind == "hevc":
        i = stsd.find(b"hvcC")
        return (stsd[i + 25] & 3) + 1 if i != -1 and i + 25 < len(stsd) else None
    return None


# --- checking frames in place --------------------------------------------------

def _frame_ok(reader, pos: int, size: int, n: int) -> bool:
    """Does a valid chain of length-prefixed NAL units fill exactly ``size`` bytes?"""
    if pos < 0 or size < n + 1 or pos + size > reader.size:
        return False
    p, end = pos, pos + size
    for _ in range(4096):
        h = reader.read(p, n + 1)
        if len(h) < n + 1:
            return False
        length = int.from_bytes(h[:n], "big")
        if length == 0 or p + n + length > end or h[n] & 0x80:  # top bit of a NAL header is always 0
            return False
        p += n + length
        if p == end:
            return True
    return False


def plan(reader, at: int) -> Plan | None:
    index = read_index(reader, at)
    if not index:
        return None
    total = index.duration / index.timescale
    videos = [t for t in index.tracks if t.handler == b"vide" and t.nal_len]
    if not videos:
        return Plan(index, None, None, 0, 0, total, 0.0)
    video = max(videos, key=lambda t: len(t.sizes))
    base = _locate(reader, index, video)
    n = len(video.sizes)
    if base is None:
        return Plan(index, None, video, n, n, total, 0.0)

    first_ok = n
    for i in range(n - 1, -1, -1):  # walk back from the end until a frame is damaged
        if not _frame_ok(reader, base + video.offsets[i], video.sizes[i], video.nal_len):
            break
        first_ok = i
    start = first_ok
    if video.sync is not None:
        start = min((i for i in video.sync if i >= first_ok), default=n)
    recovered = sum(video.durations[start:]) / video.timescale
    return Plan(index, base, video, first_ok, start, total, round(recovered, 2))


def _locate(reader, index: Index, video: Track) -> int | None:
    """Find where the original file started, by checking the last frames in place."""
    end = max(o + s for t in index.tracks for o, s in zip(t.offsets, t.sizes))
    expected = index.at - end  # if the index directly follows the last sample
    last = max(range(len(video.sizes)), key=lambda i: video.offsets[i])
    n = video.nal_len
    lo = max(0, expected - _MAX_GAP)
    window_at = lo + video.offsets[last]
    window = reader.read(window_at, expected - lo + n + 1)
    checks = sorted(range(len(video.sizes)), key=lambda i: video.offsets[i])[-3:]
    for d in range(expected - lo + 1):
        base = expected - d
        rel = base + video.offsets[last] - window_at
        if rel < 0 or rel + n + 1 > len(window):
            continue
        length = int.from_bytes(window[rel:rel + n], "big")
        if length == 0 or length + n > video.sizes[last] or window[rel + n] & 0x80:
            continue  # cheap pre-check before the full one below
        if all(_frame_ok(reader, base + video.offsets[i], video.sizes[i], n) for i in checks):
            return base
    return None


# --- writing a new, playable file ---------------------------------------------

def _box(kind: bytes, payload: bytes) -> bytes:
    if len(payload) + 8 > 0xFFFFFFFF:
        return struct.pack(">I", 1) + kind + struct.pack(">Q", len(payload) + 16) + payload
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _full(kind: bytes, version: int, body: bytes) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + body)


def _rle(values: list, signed: bool = False) -> bytes:
    runs = []
    for v in values:
        if runs and runs[-1][1] == v:
            runs[-1][0] += 1
        else:
            runs.append([1, v])
    fmt = ">Ii" if signed else ">II"
    return struct.pack(">I", len(runs)) + b"".join(struct.pack(fmt, c, v) for c, v in runs)


def _with_duration(body: bytes, kind: bytes, value: int) -> bytes:
    b = bytearray(body)
    v1 = b[0] == 1
    at = {b"mvhd": (24, 16), b"mdhd": (24, 16), b"tkhd": (28, 20)}[kind][0 if v1 else 1]
    struct.pack_into(">Q" if v1 else ">I", b, at, value if v1 else min(value, 0xFFFFFFFF))
    return bytes(b)


def rebuild(reader, at: int, out_path: str) -> Plan | None:
    """Write the playable surviving part of the video indexed at ``at``."""
    p = plan(reader, at)
    if not p or p.frames == 0:
        return None
    index, video, base = p.index, p.video, p.base
    t0 = sum(video.durations[:p.start]) / video.timescale
    boundary = base + video.offsets[p.first_ok]

    kept = [(video, p.start, 0.0)]
    for track in index.tracks:
        if track.handler != b"soun":
            continue  # telemetry/timecode tracks aren't needed to watch the video
        clock, first = 0, None
        for i, d in enumerate(track.durations):
            if clock / track.timescale >= t0 - 1e-6 and base + track.offsets[i] >= boundary:
                first = i
                break
            clock += d
        if first is not None:
            kept.append((track, first, clock / track.timescale - t0))

    with open(out_path, "wb") as out:
        out.write(_box(b"ftyp", b"mp41" + b"\x00" * 4 + b"mp41isomavc1"))
        mdat_at = out.tell()
        out.write(struct.pack(">I4sQ", 1, b"mdat", 0))
        new_offsets = []
        for track, first, _ in kept:
            offsets = []
            for i in range(first, len(track.sizes)):
                offsets.append(out.tell())
                out.write(reader.read(base + track.offsets[i], track.sizes[i]))
            new_offsets.append(offsets)
        mdat_end = out.tell()
        out.seek(mdat_at + 8)
        out.write(struct.pack(">Q", mdat_end - mdat_at))
        out.seek(mdat_end)

        movie_ts = index.timescale
        traks, movie_total = [], 0
        for (track, first, gap), offsets in zip(kept, new_offsets):
            media = sum(track.durations[first:])
            movie = media * movie_ts // track.timescale
            edts = None
            if gap > 0.001:  # audio starts a moment after the video: say so explicitly
                g = round(gap * movie_ts)
                elst = struct.pack(">I", 2) + struct.pack(">Iihh", g, -1, 1, 0) + struct.pack(">Iihh", movie, 0, 1, 0)
                edts = _box(b"edts", _full(b"elst", 0, elst))
                movie += g
            movie_total = max(movie_total, movie)
            stbl = _new_stbl(track, first, offsets)
            traks.append(_box(b"trak", _rewrite(track.trak, stbl, movie, media, edts)))
        mvhd = _with_duration(index.mvhd, b"mvhd", movie_total)
        out.write(_box(b"moov", _box(b"mvhd", mvhd) + b"".join(traks)
                       + b"".join(_box(t, body) for t, body in index.keep)))
    return p


def _new_stbl(track: Track, first: int, offsets: list) -> bytes:
    sizes = track.sizes[first:]
    parts = [_box(b"stsd", track.stsd), _full(b"stts", 0, _rle(track.durations[first:]))]
    if track.ctts is not None:
        parts.append(_full(b"ctts", track.ctts_version, _rle(track.ctts[first:], track.ctts_version == 1)))
    if track.sync is not None:
        keys = [i - first + 1 for i in range(first, len(track.sizes)) if i in track.sync]
        parts.append(_full(b"stss", 0, struct.pack(f">I{len(keys)}I", len(keys), *keys)))
    parts.append(_full(b"stsc", 0, struct.pack(">IIII", 1, 1, 1, 1)))  # one sample per chunk
    parts.append(_full(b"stsz", 0, struct.pack(f">II{len(sizes)}I", 0, len(sizes), *sizes)))
    parts.append(_full(b"co64", 0, struct.pack(f">I{len(offsets)}Q", len(offsets), *offsets)))
    return b"".join(parts)


def _rewrite(trak: bytes, stbl: bytes, movie: int, media: int, edts: bytes | None) -> bytes:
    """Copy a trak box, swapping in new tables and durations."""
    def walk(buf: bytes) -> bytes:
        out = []
        for typ, s, e in _children(buf, 0, len(buf)):
            body = buf[s:e]
            if typ in (b"mdia", b"minf"):
                out.append(_box(typ, walk(body)))
            elif typ == b"tkhd":
                out.append(_box(typ, _with_duration(body, typ, movie)))
                if edts:
                    out.append(edts)
            elif typ == b"edts":
                continue  # the old edit list described the old start
            elif typ == b"mdhd":
                out.append(_box(typ, _with_duration(body, typ, media)))
            elif typ == b"stbl":
                out.append(_box(typ, stbl))
            else:
                out.append(_box(typ, body))
        return b"".join(out)
    return walk(trak)
