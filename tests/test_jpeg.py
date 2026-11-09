import random
import unittest
from datetime import datetime

from cardscan import jpeg
from tests.fixtures import BytesReader, synthetic_jpeg

WHEN = datetime(2025, 12, 14, 15, 30, 12)


class JpegTests(unittest.TestCase):
    def test_measures_an_intact_photo_exactly(self):
        photo = synthetic_jpeg(WHEN, pixels=200_000, seed=1)
        card = photo + random.Random(9).randbytes(5000)
        info = jpeg.parse(BytesReader(card), 0)
        self.assertTrue(info.complete)
        self.assertEqual(info.size, len(photo))

    def test_reads_camera_and_date_from_exif(self):
        info = jpeg.parse(BytesReader(synthetic_jpeg(WHEN)), 0)
        self.assertEqual((info.make, info.model), ("GoPro", "HERO5 Black"))
        self.assertEqual(info.taken, WHEN)
        self.assertEqual((info.width, info.height), (4000, 3000))

    def test_big_endian_exif(self):
        info = jpeg.parse(BytesReader(synthetic_jpeg(WHEN, big_endian=True)), 0)
        self.assertEqual(info.taken, WHEN)

    def test_embedded_thumbnail_does_not_end_the_photo_early(self):
        # GoPro JPEGs carry a small JPEG thumbnail (its own FF D8 ... FF D9)
        # inside EXIF. Hopping by segment length must jump right over it.
        photo = synthetic_jpeg(WHEN, thumbnail=True)
        self.assertIn(b"\xff\xd8", photo[4:])
        info = jpeg.parse(BytesReader(photo), 0)
        self.assertEqual(info.size, len(photo))

    def test_photo_overwritten_part_way_by_a_newer_photo(self):
        old = synthetic_jpeg(WHEN, pixels=300_000, seed=2)
        new = synthetic_jpeg(datetime(2026, 2, 20, 9, 0), seed=3)
        cut = 64 * 4096  # the new file starts on a cluster boundary inside the old one
        card = old[:cut] + new
        damaged = jpeg.parse(BytesReader(card), 0)
        self.assertFalse(damaged.complete)
        self.assertEqual(damaged.size, cut)
        self.assertEqual(damaged.taken, WHEN)
        intact = jpeg.parse(BytesReader(card), cut)
        self.assertTrue(intact.complete)
        self.assertEqual(intact.size, len(new))

    def test_photo_running_into_erased_space(self):
        old = synthetic_jpeg(WHEN, pixels=300_000, seed=4)
        cut = 100_000
        info = jpeg.parse(BytesReader(old[:cut] + b"\x00" * 100_000), 0)
        self.assertFalse(info.complete)
        self.assertLessEqual(info.size, cut)
        self.assertGreater(info.size, cut - 512)

    def test_header_only_is_not_worth_reporting(self):
        photo = synthetic_jpeg(WHEN)
        sos = photo.index(b"\xff\xda")
        self.assertIsNone(jpeg.parse(BytesReader(photo[:sos] + b"\x00" * 4096), 0))

    def test_rejects_things_that_merely_start_like_a_jpeg(self):
        rng = random.Random(5)
        for _ in range(200):
            junk = b"\xff\xd8\xff" + rng.randbytes(20_000)
            info = jpeg.parse(BytesReader(junk), 0)
            self.assertTrue(info is None or not info.complete)

    def test_video_thumbnail_without_exif(self):
        thm = synthetic_jpeg(with_exif=False, width=160, height=120, pixels=3000)
        info = jpeg.parse(BytesReader(thm), 0)
        self.assertTrue(info.complete)
        self.assertIsNone(info.taken)
        self.assertEqual(info.width, 160)

    def test_unset_camera_clock_gives_no_date(self):
        self.assertIsNone(jpeg._parse_date("0000:00:00 00:00:00"))


if __name__ == "__main__":
    unittest.main()
