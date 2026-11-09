"""The whole story on a real FAT32 filesystem: fill, quick format, record over."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from cardscan.reader import BlockReader
from cardscan.scanner import scan
from tools.simulate_card import simulate

HAVE_TOOLS = all(shutil.which(t) for t in ("mkfs.fat", "mcopy", "mmd"))


@unittest.skipUnless(HAVE_TOOLS, "needs dosfstools and mtools")
class SimulatedCardTest(unittest.TestCase):
    def test_what_survives_a_quick_format_and_new_footage(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = simulate(tmp, size_mib=64)
            with open(os.path.join(tmp, "manifest.json")) as fh:
                manifest = json.load(fh)
            with BlockReader(image) as r:
                carved = {hashlib.sha256(r.read(f.offset, f.size)).hexdigest() for f in scan(r)}

        def recovered(session):
            files = [m for m in manifest if m["session"] == session]
            return sum(m["sha256"] in carved for m in files), len(files)

        # Footage recorded after the delete is what the card shows now.
        ok, total = recovered("hiking")
        self.assertEqual(ok, total)
        # The camera wrote it from the start of the card, so the oldest files,
        # which also lived at the start, were overwritten...
        ok, total = recovered("summer")
        self.assertEqual(ok, 0)
        # ...while the birthday shoot, further in, comes back byte for byte.
        ok, total = recovered("16th birthday shoot")
        self.assertEqual(ok, total)
        self.assertGreater(total, 10)


if __name__ == "__main__":
    unittest.main()
