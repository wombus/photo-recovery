"""Checks against genuine GoPro files, from the projects that publish them.

GoPro's gpmf-parser repository ships sample clips straight off HERO5, HERO6
and HERO7 cameras, and ExifTool's test suite includes a real GoPro photo (its
picture shrunk to 8x8, its metadata intact). They're downloaded on first run,
pinned to an exact commit and SHA-256, and cached in tests/.samples. Without
network access these tests skip.
"""

import hashlib
import os
import random
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from cardscan import jpeg, mp4, salvage
from cardscan.reader import BlockReader
from cardscan.scanner import scan
from tests.fixtures import BytesReader

_GPMF = "https://raw.githubusercontent.com/gopro/gpmf-parser/941671f02687d4b4e7a9a36397c0b62223cffeb5/samples/"
_EXIFTOOL = "https://raw.githubusercontent.com/exiftool/exiftool/5580ea444440a0183e903f97f2a212a3cae05859/t/images/"
SAMPLES = {
    "hero5.mp4": (_GPMF + "hero5.mp4", "04e45b2f41dff195b525fa18b7ed5517e5fc2d651127d1b57d1d931de306f915"),
    "hero6.mp4": (_GPMF + "hero6.mp4", "84aebc4e370ef9081f9015bf310d7a858d431258f6b0f2160d731c4308249c67"),
    "hero7.mp4": (_GPMF + "hero7.mp4", "3c593b8f08090e3ec246178c34737d11569330d75df8225e3ee08036c6a04d0b"),
    "GoPro.jpg": (_EXIFTOOL + "GoPro.jpg", "d4f9d433ae69deffd764e23b709174d3d3304f1a767f6f20d1b868b4219180c4"),
}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".samples")


def sample(name: str) -> bytes:
    url, digest = SAMPLES[name]
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            data = fh.read()
    else:
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
        except (OSError, urllib.error.URLError) as e:
            raise unittest.SkipTest(f"can't download {name}: {e}")
    if hashlib.sha256(data).hexdigest() != digest:
        raise AssertionError(f"{name} doesn't match its pinned SHA-256; delete {path} and retry")
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        with open(path + ".part", "wb") as fh:
            fh.write(data)
        os.replace(path + ".part", path)
    return data


def on_card(clip: bytes, lead: int = 4 << 20, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    return rng.randbytes(lead) + clip + rng.randbytes(1 << 20)


class RealGoProTests(unittest.TestCase):
    def test_intact_clips_are_recognised_and_every_frame_checks_out(self):
        expected = {  # creation date, firmware
            "hero5.mp4": ("2017-04-17T10:31:43", "HD5.02.02.00.00"),
            "hero6.mp4": ("2018-01-24T11:27:32", "HD6.01.01.60.00"),
            # This camera's clock was never set: GoPro falls back to 2016-01-01.
            "hero7.mp4": ("2016-01-01T02:27:42", "HD7.01.01.80.00"),
        }
        for name, (taken, firmware) in expected.items():
            clip = sample(name)
            info = mp4.parse(BytesReader(clip), 0)
            self.assertEqual(info.firmware, firmware)
            found = list(scan(BytesReader(on_card(clip)), deep=True))
            self.assertEqual(len(found), 1, name)
            self.assertTrue(found[0].complete, found[0].note)
            self.assertEqual((found[0].taken, found[0].size), (taken, len(clip)))

    def test_gopro_layout_puts_the_index_last(self):
        # The whole idea of rebuilding a video's surviving end depends on this.
        for name in ("hero5.mp4", "hero6.mp4", "hero7.mp4"):
            clip = sample(name)
            moov = clip.rfind(b"moov") - 4
            self.assertEqual(moov + int.from_bytes(clip[moov:moov + 4], "big"), len(clip))
            self.assertEqual(clip[4:8], b"ftyp")
            self.assertEqual(clip[24:28], b"mdat")

    def test_hero5_clip_with_its_beginning_overwritten(self):
        clip = sample("hero5.mp4")
        lead = 4 << 20
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            original_frames = _frame_hashes(clip, tmp) if ffmpeg else None
            for fraction in (0.25, 0.5, 0.9):
                card = bytearray(on_card(clip, lead))
                cut = lead + int(len(clip) * fraction)
                card[:cut] = random.Random(1).randbytes(cut)
                reader = BytesReader(bytes(card))
                (found,) = list(scan(reader, deep=True))
                self.assertIn("beginning overwritten", found.note)
                out = os.path.join(tmp, f"rebuilt{fraction}.mp4")
                p = salvage.rebuild(reader, found.moov_at, out)
                self.assertEqual(p.base, lead)  # found the original start exactly
                self.assertGreater(p.frames, 0)
                with open(out, "rb") as fh:
                    rebuilt = fh.read()
                self.assertTrue(mp4.parse(BytesReader(rebuilt), 0).complete)
                if ffmpeg:
                    frames = _frame_hashes(rebuilt, tmp)
                    self.assertEqual(len(frames), p.frames)
                    # Every recovered frame decodes to exactly the original picture.
                    self.assertEqual(frames, original_frames[-len(frames):])

    def test_real_gopro_photo_metadata(self):
        data = sample("GoPro.jpg")
        info = jpeg.parse(BytesReader(data), 0)
        self.assertTrue(info.complete)
        self.assertEqual(info.size, len(data))
        self.assertEqual((info.make, info.model), ("GoPro", "HERO6 Black"))  # big-endian EXIF
        self.assertEqual(info.taken.isoformat(), "2017-10-03T10:58:29")


def _frame_hashes(clip: bytes, tmp: str) -> list:
    path = os.path.join(tmp, "clip.mp4")
    with open(path, "wb") as fh:
        fh.write(clip)
    run = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True)
    if run.stderr:
        raise AssertionError(f"decode errors: {run.stderr[:500]}")
    return [line.rsplit(",", 1)[1].strip() for line in run.stdout.splitlines() if not line.startswith("#")]


if __name__ == "__main__":
    unittest.main()
