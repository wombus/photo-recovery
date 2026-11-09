"""The whole story on a real FAT32 filesystem: fill, format in camera, record over."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from cardscan import salvage
from cardscan.reader import BlockReader
from cardscan.scanner import scan
from tools.simulate_card import simulate

HAVE_TOOLS = all(shutil.which(t) for t in ("mkfs.fat", "mcopy", "mmd"))


@unittest.skipUnless(HAVE_TOOLS, "needs dosfstools and mtools")
class SimulatedCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        image = simulate(cls.tmp.name, size_mib=64)
        with open(os.path.join(cls.tmp.name, "manifest.json")) as fh:
            cls.manifest = json.load(fh)
        cls.reader = BlockReader(image)
        cls.found = list(scan(cls.reader, deep=True))
        cls.carved = {hashlib.sha256(cls.reader.read(f.offset, f.size)).hexdigest()
                      for f in cls.found if f.moov_at is None}

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.tmp.cleanup()

    def files(self, session, kind=None, suffix=""):
        return [m for m in self.manifest if m["session"] == session
                and (kind is None or m["kind"] == kind) and m["name"].endswith(suffix)]

    def recovered(self, files):
        return sum(m["sha256"] in self.carved for m in files)

    def test_footage_recorded_after_the_format_is_intact(self):
        hiking = self.files("hiking")
        self.assertEqual(self.recovered(hiking), len(hiking))

    def test_oldest_files_at_the_start_of_the_card_were_overwritten(self):
        self.assertEqual(self.recovered(self.files("summer")), 0)

    def test_birthday_photos_come_back_byte_for_byte(self):
        photos = self.files("16th birthday shoot", "jpeg")
        self.assertGreater(len(photos), 10)
        self.assertEqual(self.recovered(photos), len(photos))

    def test_birthday_video_tail_is_rebuilt_from_its_index(self):
        # The hiking footage ended inside the December video, so the low-res
        # copy recorded after it survived whole...
        self.assertEqual(self.recovered(self.files("16th birthday shoot", suffix=".LRV")), 1)
        # ...and the chapter it ended in lost its beginning, but its index and
        # last frames survived and are rebuilt.
        rebuilt = [f for f in self.found if f.moov_at is not None and f.size > 0]
        self.assertEqual(len(rebuilt), 1)
        self.assertTrue(rebuilt[0].taken.startswith("2025-12-14"))
        p = salvage.plan(self.reader, rebuilt[0].moov_at)
        self.assertGreater(p.frames, 0)
        self.assertLess(p.recovered_s, p.total_s)


if __name__ == "__main__":
    unittest.main()
