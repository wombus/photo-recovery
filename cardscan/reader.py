"""Read-only, sector-aligned access to a disk image or a raw card device."""

from __future__ import annotations

import os

SECTOR = 512
_WINDOW = 1 << 20  # read ahead 1 MiB so many small header reads cost one disk read


def _round_up(n: int) -> int:
    return -(-n // SECTOR) * SECTOR


class BlockReader:
    """Random-access reads from an image file or block device. Never writes.

    Two details matter for recovery work:

    * The source is opened ``"rb"``. Nothing in this project ever opens it
      for writing, so scanning cannot damage what you are trying to save.
    * Raw devices on macOS (``/dev/rdiskN``) and Windows
      (``\\\\.\\PhysicalDriveN``) reject reads that don't start and end on a
      512-byte sector boundary. Image files don't care, but routing every
      read through one aligned path means the scanner behaves the same on both.
    """

    def __init__(self, path: str, size: int | None = None):
        self.path = path
        self._f = open(path, "rb", buffering=0)
        self.size = size if size is not None else self._detect_size()
        self._win_start = 0
        self._win = b""

    def _detect_size(self) -> int:
        size = os.fstat(self._f.fileno()).st_size
        if size == 0:  # block devices report 0 here, so ask the device itself
            try:
                size = self._f.seek(0, os.SEEK_END)
            except OSError:
                size = 0
        if size <= 0:
            raise OSError(f"Could not work out how big {self.path} is; pass --size BYTES")
        return size

    def read(self, offset: int, length: int) -> bytes:
        """Return up to ``length`` bytes starting at ``offset`` (short at the end)."""
        if length <= 0 or offset < 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        rel = offset - self._win_start
        if 0 <= rel and rel + length <= len(self._win):
            return self._win[rel:rel + length]
        start = offset - offset % SECTOR
        end = min(_round_up(offset + max(length, _WINDOW)), _round_up(self.size))
        self._win = self._read_exact(start, end - start)
        self._win_start = start
        rel = offset - start
        return self._win[rel:rel + length]

    def _read_exact(self, start: int, n: int) -> bytes:
        self._f.seek(start)
        parts, got = [], 0
        while got < n:
            block = self._f.read(n - got)
            if not block:
                break
            parts.append(block)
            got += len(block)
        return b"".join(parts)

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "BlockReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
