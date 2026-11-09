import unittest
from datetime import datetime

from cardscan import mp4
from tests.fixtures import BytesReader, synthetic_jpeg, synthetic_mp4

WHEN = datetime(2025, 12, 14, 16, 2, 40)


class Mp4Tests(unittest.TestCase):
    def test_intact_gopro_video(self):
        video = synthetic_mp4(WHEN, duration_s=42.5)
        info = mp4.parse(BytesReader(video + b"\x00" * 4096), 0)
        self.assertTrue(info.complete)
        self.assertEqual(info.size, len(video))
        self.assertEqual(info.created, WHEN)
        self.assertEqual(info.duration_s, 42.5)
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertEqual(info.firmware, "HD5.02.02.70.00")

    def test_low_res_proxy_is_recognised_by_resolution(self):
        info = mp4.parse(BytesReader(synthetic_mp4(WHEN, width=848, height=480)), 0)
        self.assertEqual((info.width, info.height), (848, 480))

    def test_64_bit_box_sizes(self):
        video = synthetic_mp4(WHEN, large_mdat=True)
        info = mp4.parse(BytesReader(video), 0)
        self.assertTrue(info.complete)
        self.assertEqual(info.size, len(video))

    def test_video_whose_index_was_lost(self):
        video = synthetic_mp4(WHEN, with_moov=False)
        info = mp4.parse(BytesReader(video + b"\x00" * 8192), 0)
        self.assertFalse(info.complete)
        self.assertEqual(info.size, len(video))
        self.assertIn("moov", info.note)

    def test_stops_where_the_next_file_starts(self):
        first = synthetic_mp4(WHEN, seed=1)
        second = synthetic_mp4(datetime(2026, 2, 20), seed=2)
        info = mp4.parse(BytesReader(first + second), 0)
        self.assertEqual(info.size, len(first))

    def test_photo_after_a_video_is_not_swallowed(self):
        video = synthetic_mp4(WHEN)
        info = mp4.parse(BytesReader(video + synthetic_jpeg(WHEN)), 0)
        self.assertEqual(info.size, len(video))

    def test_not_an_mp4(self):
        self.assertIsNone(mp4.parse(BytesReader(b"\x00\x00\x00\x10ftyp\x01\x02\x03\x04" + b"\x00" * 100), 0))


if __name__ == "__main__":
    unittest.main()
