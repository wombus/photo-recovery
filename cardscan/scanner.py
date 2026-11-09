"""Sweep a whole card image for the start of photos and videos.

This ignores the filesystem entirely. After a format, the filesystem's index
says the card is empty, but the bytes of the old files are still sitting in
the data area until something new is written over them. So we read the raw
bytes front to back and look for file *signatures*:

    JPEG  FF D8 FF ...        at the very start of the file
    MP4   ?? ?? ?? ?? ftyp    "ftyp" four bytes in

FAT32 and exFAT always start a file at the beginning of a cluster, and
clusters are whole multiples of 512-byte sectors. So a real file can only
begin at an offset divisible by 512. Checking that throws away nearly all
the accidental matches inside video data without missing a single real file.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Callable, Iterator

from . import jpeg, mp4, salvage
from .reader import BlockReader

CHUNK = 32 << 20
_OVERLAP = 16  # so a signature split across two chunks is still seen


@dataclass
class Finding:
    offset: int  # byte offset on the card where the file starts
    kind: str  # "jpeg" or "mp4"
    size: int  # bytes to carve
    complete: bool
    taken: str | None  # ISO date/time from the camera's own clock (no time zone)
    camera: str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    note: str = ""
    thumb_at: int | None = None  # whole-picture preview inside a damaged photo
    thumb_size: int | None = None
    moov_at: int | None = None  # set for videos rebuilt from a surviving index

    @property
    def taken_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.taken) if self.taken else None

    @property
    def ext(self) -> str:
        return "jpg" if self.kind == "jpeg" else "mp4"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def identify(reader, offset: int, kind: str, verify: bool = False) -> Finding | None:
    """Try to read a file of the given kind at ``offset``.

    ``kind`` is "jpeg", "mp4", or "moov" (a video index whose file start may be
    gone). ``verify`` checks every frame of an intact-looking video in place.
    """
    if kind == "jpeg":
        j = jpeg.parse(reader, offset)
        if not j:
            return None
        camera = _camera_name(j.make, j.model)
        thumb = (j.thumb_at, j.thumb_size) if not j.complete else (None, None)
        return Finding(offset, "jpeg", j.size, j.complete, _iso(j.taken), camera,
                       j.width, j.height, None, j.note, *thumb)
    if kind == "moov":
        return _from_index(reader, offset)
    m = mp4.parse(reader, offset)
    if not m:
        return None
    found = Finding(offset, "mp4", m.size, m.complete, _iso(m.created), _video_camera(m.camera, m.firmware, m.brand),
                    m.width, m.height, m.duration_s, m.note)
    if verify and m.complete:
        _verify_frames(reader, found)
    return found


def _from_index(reader, at: int) -> Finding | None:
    p = salvage.plan(reader, at)
    if not p:
        return None
    ix, v = p.index, p.video
    camera = _video_camera(None, ix.firmware, "")
    width, height = (v.width, v.height) if v else (None, None)
    if not p.frames:
        return Finding(at, "mp4", 0, False, _iso(ix.created), camera, width, height,
                       round(p.total_s, 2), "only this video's index survived; its frames were overwritten",
                       moov_at=at)
    start = p.base + v.offsets[p.start]
    if p.start == 0:
        note = "file header was overwritten; every frame survived and is rebuilt from the index"
    else:
        note = (f"beginning overwritten; the last {p.recovered_s:.1f}s of {p.total_s:.1f}s "
                f"are rebuilt from the surviving index")
    return Finding(start, "mp4", ix.at + ix.size - start, p.start == 0, _iso(ix.created), camera,
                   width, height, p.recovered_s, note, moov_at=at)


def _verify_frames(reader, f: Finding) -> None:
    """Check every video frame of a complete-looking file where it sits."""
    pos, end = f.offset, f.offset + f.size
    moov_at = None
    while pos + 8 <= end:  # find this file's own moov box
        size, typ = int.from_bytes(reader.read(pos, 4), "big"), reader.read(pos + 4, 4)
        if size == 1:
            size = int.from_bytes(reader.read(pos + 8, 8), "big")
        if typ == b"moov":
            moov_at = pos
            break
        if size < 8:
            return
        pos += size
    index = salvage.read_index(reader, moov_at) if moov_at is not None else None
    videos = [t for t in index.tracks if t.handler == b"vide" and t.nal_len] if index else []
    if not videos:
        return
    v = max(videos, key=lambda t: len(t.sizes))
    bad = [i for i in range(len(v.sizes))
           if not salvage._frame_ok(reader, f.offset + v.offsets[i], v.sizes[i], v.nal_len)]
    if bad:
        when = sum(v.durations[:bad[0]]) / v.timescale
        f.complete = False
        f.note = f"{len(bad)} of {len(v.sizes)} video frames are damaged, the first at {when:.1f}s"


def _video_camera(name: str | None, firmware: str | None, brand: str) -> str:
    if name:
        return name
    if firmware:
        return f"GoPro firmware {firmware}" if firmware.startswith("HD") else firmware
    return f"MP4 ({brand})" if brand else "unknown camera"


class _Covered:
    """Byte ranges already explained by a file we found, so we don't re-report
    pieces of it (a photo's embedded thumbnail, a video's own index)."""

    def __init__(self):
        self.starts: list[int] = []
        self.ranges: list[tuple[int, int, str]] = []

    def add(self, f: Finding) -> None:
        end = max(f.offset + f.size, (f.moov_at or 0) + 8)
        i = bisect.bisect(self.starts, f.offset)
        self.starts.insert(i, f.offset)
        self.ranges.insert(i, (f.offset, end, f.kind))

    def inside(self, pos: int, kinds: tuple[str, ...] | None = None) -> bool:
        i = bisect.bisect_right(self.starts, pos)
        for start, end, kind in self.ranges[max(0, i - 4):i]:
            if start < pos < end and (kinds is None or kind in kinds):
                return True
        return False


def scan(reader, align: int = 512, deep: bool = False,
         progress: Callable[[int, int, int], None] | None = None) -> Iterator[Finding]:
    """Yield every photo and video found, in the order they sit on the card.

    ``deep`` also looks at every byte offset (not just sector boundaries), so
    pictures embedded inside other files are found too, and checks every frame
    of intact-looking videos. ``progress(bytes_done, photos, videos)`` is called
    after each chunk.
    """
    step = 1 if deep else align
    sigs = ((jpeg.SIGNATURE, 0, "jpeg", step), (mp4.SIGNATURE, mp4.SIGNATURE_OFFSET, "mp4", step),
            (b"moov", 4, "moov", 1))  # an index sits wherever its frames happened to end
    covered = _Covered()
    photos = videos = 0
    pos = 0
    while pos < reader.size:
        data = reader.read(pos, CHUNK + _OVERLAP)
        if not data:
            break
        starts = []
        for sig, back, kind, every in sigs:
            i = data.find(sig)
            while i != -1:
                start = pos + i - back
                # Hits in the overlap belong to the next chunk; skip them here.
                if pos <= start < pos + CHUNK and start % every == 0:
                    starts.append((start, kind))
                i = data.find(sig, i + 1)
        for start, kind in sorted(starts):
            if kind == "moov" and covered.inside(start, ("mp4",)):
                continue  # the index of a video we already have
            if deep and kind != "moov" and covered.inside(start):
                continue  # e.g. the thumbnail inside a photo we already have
            found = identify(reader, start, kind, verify=deep)
            if found:
                if found.kind == "jpeg":
                    photos += 1
                else:
                    videos += 1
                covered.add(found)
                yield found
        pos += CHUNK
        if progress:
            progress(min(pos, reader.size), photos, videos)


def identify_file(path: str) -> Finding | None:
    """Identify a single already-recovered file (e.g. one PhotoRec produced)."""
    with BlockReader(path) as r:
        return identify(r, 0, "jpeg") or identify(r, 0, "mp4")


def _camera_name(make: str | None, model: str | None) -> str:
    if make and model:
        return model if model.lower().startswith(make.lower()) else f"{make} {model}"
    return model or make or "unknown camera"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None
