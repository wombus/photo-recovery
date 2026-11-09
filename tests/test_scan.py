import os
import random
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from cardscan import scanner
from cardscan.reader import BlockReader
from tests.fixtures import BytesReader, synthetic_jpeg, synthetic_mp4

DEC = datetime(2025, 12, 14, 14, 0)


def place(card: bytearray, offset: int, data: bytes) -> None:
    card[offset:offset + len(data)] = data


class ScanTests(unittest.TestCase):
    def test_finds_every_file_in_card_order(self):
        rng = random.Random(1)
        card = bytearray(rng.randbytes(3 << 20))  # leftover junk everywhere else
        files = {
            4096: synthetic_jpeg(DEC, seed=1),
            512 * 900: synthetic_mp4(DEC, seed=2),
            512 * 3000: synthetic_jpeg(datetime(2026, 2, 1), seed=3),
        }
        for off, data in files.items():
            place(card, off, data)
        found = list(scanner.scan(BytesReader(bytes(card))))
        self.assertEqual([f.offset for f in found], sorted(files))
        for f in found:
            self.assertTrue(f.complete)
            self.assertEqual(f.size, len(files[f.offset]))
        self.assertEqual(found[0].camera, "GoPro HERO5 Black")
        self.assertEqual(found[1].camera, "GoPro firmware HD5.02.02.70.00")

    def test_files_only_start_on_sector_boundaries(self):
        card = bytearray(1 << 20)
        # No EXIF thumbnail here: with align=1 that nested JPEG would count too.
        place(card, 4096 + 100, synthetic_jpeg(DEC, thumbnail=False))  # not a FAT file start
        self.assertEqual(list(scanner.scan(BytesReader(bytes(card)))), [])
        self.assertEqual(len(list(scanner.scan(BytesReader(bytes(card)), align=1))), 1)

    def test_signatures_split_across_read_chunks(self):
        with mock.patch.object(scanner, "CHUNK", 8192):
            card = bytearray(1 << 20)
            place(card, 8192 * 3 - 1, synthetic_jpeg(DEC, seed=4, thumbnail=False))  # FF | D8 FF
            place(card, 8192 * 20 - 6, synthetic_mp4(DEC, seed=5))  # .. ft | yp
            found = list(scanner.scan(BytesReader(bytes(card)), align=1))
        self.assertEqual([(f.kind, f.offset) for f in found],
                         [("jpeg", 8192 * 3 - 1), ("mp4", 8192 * 20 - 6)])

    def test_identify_a_recovered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f1234.jpg")
            with open(path, "wb") as fh:
                fh.write(synthetic_jpeg(DEC))
            self.assertEqual(scanner.identify_file(path).taken, DEC.isoformat())


class ReaderTests(unittest.TestCase):
    def test_reads_any_range_of_an_odd_sized_image(self):
        data = random.Random(2).randbytes((3 << 20) + 77)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "card.img")
            with open(path, "wb") as fh:
                fh.write(data)
            with BlockReader(path) as r:
                self.assertEqual(r.size, len(data))
                for off, n in [(0, 10), (511, 2), (1 << 20, 5000), (len(data) - 3, 100), (5, 4 << 20)]:
                    self.assertEqual(r.read(off, n), data[off:off + n])
                self.assertEqual(r.read(len(data), 10), b"")


if __name__ == "__main__":
    unittest.main()
