"""Videos whose beginning was overwritten, rebuilt from their surviving index."""

import contextlib
import io
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime

from cardscan import mp4, salvage
from cardscan.cli import main
from cardscan.scanner import scan
from tests.fixtures import BytesReader, synthetic_jpeg, synthetic_mp4

DEC = datetime(2025, 12, 14, 15, 0)
LEAD = 1 << 20  # junk before the video on the "card"


def card_with_video(video: bytes, overwritten: float, seed: int = 0) -> tuple[bytes, int]:
    """Place a video on a card, then overwrite its first ``overwritten`` fraction."""
    rng = random.Random(seed)
    card = bytearray(rng.randbytes(LEAD) + video + rng.randbytes(1 << 16))
    cut = LEAD + int(len(video) * overwritten)
    card[:cut] = rng.randbytes(cut)
    return bytes(card), cut


def video_track(video: bytes) -> salvage.Track:
    index = salvage.read_index(BytesReader(video), video.find(b"moov") - 4)
    return next(t for t in index.tracks if t.handler == b"vide")


class SalvageTests(unittest.TestCase):
    def test_finds_the_start_and_the_first_intact_frame(self):
        video = synthetic_mp4(DEC, frames=400_000, duration_s=10, seed=1)
        v = video_track(video)
        for fraction in (0.001, 0.3, 0.75):
            card, cut = card_with_video(video, fraction)
            p = salvage.plan(BytesReader(card), card.find(b"moov") - 4)
            self.assertEqual(p.base, LEAD)
            first_ok = min(i for i, o in enumerate(v.offsets) if LEAD + o >= cut)
            self.assertEqual(p.first_ok, first_ok, fraction)
            self.assertIn(p.start, v.sync)
            self.assertGreaterEqual(p.start, first_ok)

    def test_rebuilt_file_holds_exactly_the_surviving_frames(self):
        video = synthetic_mp4(DEC, frames=400_000, duration_s=10, seed=2)
        v = video_track(video)
        card, _ = card_with_video(video, 0.4)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "rebuilt.mp4")
            p = salvage.rebuild(BytesReader(card), card.find(b"moov") - 4, out)
            with open(out, "rb") as fh:
                rebuilt = fh.read()
        info = mp4.parse(BytesReader(rebuilt), 0)
        self.assertTrue(info.complete)
        self.assertEqual(info.created, DEC)
        self.assertEqual(info.firmware, "HD5.02.02.70.00")
        new = video_track(rebuilt)
        self.assertEqual(len(new.sizes), len(v.sizes) - p.start)
        for k, i in enumerate(range(p.start, len(v.sizes))):
            self.assertEqual(rebuilt[new.offsets[k]:new.offsets[k] + new.sizes[k]],
                             video[v.offsets[i]:v.offsets[i] + v.sizes[i]])
        self.assertEqual(new.sync, {i - p.start for i in v.sync if i >= p.start})
        handlers = {t.handler for t in salvage.read_index(BytesReader(rebuilt), rebuilt.find(b"moov") - 4).tracks}
        self.assertEqual(handlers, {b"vide", b"soun"})  # telemetry track dropped

    def test_scan_reports_the_rebuildable_tail(self):
        video = synthetic_mp4(DEC, frames=400_000, duration_s=10, seed=3)
        card, _ = card_with_video(video, 0.5)
        found = list(scan(BytesReader(card)))
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual((f.kind, f.taken, f.complete), ("mp4", DEC.isoformat(), False))
        self.assertIn("beginning overwritten", f.note)
        self.assertTrue(3 < f.duration_s < 6)

    def test_intact_video_is_not_reported_twice(self):
        card = bytearray(1 << 20)
        video = synthetic_mp4(DEC, seed=4)
        card[4096:4096 + len(video)] = video
        self.assertEqual(len(list(scan(BytesReader(bytes(card))))), 1)

    def test_index_whose_frames_are_gone(self):
        video = synthetic_mp4(DEC, frames=200_000, seed=5)
        moov = video.find(b"moov") - 4
        card = random.Random(1).randbytes(LEAD + moov) + video[moov:]
        found = list(scan(BytesReader(card)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].size, 0)
        self.assertIn("only this video's index survived", found[0].note)

    def test_deep_scan_checks_every_frame_of_intact_looking_videos(self):
        video = bytearray(synthetic_mp4(DEC, frames=400_000, seed=6))
        middle = len(video) // 2
        video[middle:middle + 20_000] = random.Random(2).randbytes(20_000)  # damaged mid-file
        card = bytearray(1 << 20)
        card[4096:4096 + len(video)] = video
        normal = list(scan(BytesReader(bytes(card))))[0]
        deep = list(scan(BytesReader(bytes(card)), deep=True))[0]
        self.assertTrue(normal.complete)  # its structure alone looks fine...
        self.assertFalse(deep.complete)  # ...but some frames don't check out
        self.assertIn("video frames are damaged", deep.note)

    def test_deep_scan_finds_pictures_at_any_offset_without_duplicates(self):
        card = bytearray(random.Random(3).randbytes(1 << 20))
        photo = synthetic_jpeg(DEC, seed=7)  # has an embedded thumbnail
        hidden = synthetic_jpeg(DEC, seed=8, thumbnail=False)
        card[4096:4096 + len(photo)] = photo
        card[300_001:300_001 + len(hidden)] = hidden  # not on a sector boundary
        normal = list(scan(BytesReader(bytes(card))))
        deep = list(scan(BytesReader(bytes(card)), deep=True))
        self.assertEqual([f.offset for f in normal], [4096])
        self.assertEqual([f.offset for f in deep], [4096, 300_001])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealVideoTest(unittest.TestCase):
    def test_real_h264_video_with_its_beginning_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "real.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "8",
                            "-c:v", "libx264", "-g", "15", "-bf", "2", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", src], check=True)
            with open(src, "rb") as fh:
                card, _ = card_with_video(fh.read(), 0.4)
            image = os.path.join(tmp, "card.img")
            with open(image, "wb") as fh:
                fh.write(card)
            results, out = os.path.join(tmp, "results"), os.path.join(tmp, "out")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["scan", image, "--out", results]), 0)
                self.assertEqual(main(["extract", os.path.join(results, "findings.json"), "--all", "--out", out]), 0)
            (name,) = os.listdir(out)
            self.assertTrue(name.endswith("_REBUILT.mp4"))
            decode = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.join(out, name), "-f", "null", "-"],
                                    capture_output=True, text=True)
            self.assertEqual(decode.returncode, 0)
            self.assertEqual(decode.stderr, "")  # every frame decodes cleanly
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    os.path.join(out, name)], capture_output=True, text=True)
            self.assertGreater(float(probe.stdout), 3.0)


if __name__ == "__main__":
    unittest.main()
