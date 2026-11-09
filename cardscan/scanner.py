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

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterator

from . import jpeg, mp4
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
        return cls(**d)


def identify(reader, offset: int, kind: str) -> Finding | None:
    """Try to read a file of the given kind at ``offset``."""
    if kind == "jpeg":
        j = jpeg.parse(reader, offset)
        if not j:
            return None
        camera = _camera_name(j.make, j.model)
        return Finding(offset, "jpeg", j.size, j.complete, _iso(j.taken), camera,
                       j.width, j.height, None, j.note)
    m = mp4.parse(reader, offset)
    if not m:
        return None
    if m.camera:
        camera = m.camera
    elif m.firmware:
        camera = f"GoPro firmware {m.firmware}" if m.firmware.startswith("HD") else m.firmware
    else:
        camera = f"MP4 ({m.brand})"
    return Finding(offset, "mp4", m.size, m.complete, _iso(m.created), camera,
                   m.width, m.height, m.duration_s, m.note)


def scan(reader, align: int = 512,
         progress: Callable[[int, int, int], None] | None = None) -> Iterator[Finding]:
    """Yield every photo and video found, in the order they sit on the card.

    ``progress(bytes_done, photos, videos)`` is called after each chunk.
    """
    sigs = ((jpeg.SIGNATURE, 0, "jpeg"), (mp4.SIGNATURE, mp4.SIGNATURE_OFFSET, "mp4"))
    photos = videos = 0
    pos = 0
    while pos < reader.size:
        data = reader.read(pos, CHUNK + _OVERLAP)
        if not data:
            break
        starts = []
        for sig, back, kind in sigs:
            i = data.find(sig)
            while i != -1:
                start = pos + i - back
                # Hits in the overlap belong to the next chunk; skip them here.
                if pos <= start < pos + CHUNK and start % align == 0:
                    starts.append((start, kind))
                i = data.find(sig, i + 1)
        for start, kind in sorted(starts):
            found = identify(reader, start, kind)
            if found:
                if found.kind == "jpeg":
                    photos += 1
                else:
                    videos += 1
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
