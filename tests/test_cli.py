import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime

from cardscan.cli import main
from tests.fixtures import synthetic_jpeg, synthetic_mp4

DEC = datetime(2025, 12, 14, 14, 0)
FEB = datetime(2026, 2, 20, 9, 0)


def quiet(*argv) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(argv))
    assert code == 0, out.getvalue()
    return out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.old = synthetic_jpeg(DEC, seed=1)
        self.video = synthetic_mp4(DEC, seed=2)
        self.new = synthetic_jpeg(FEB, seed=3)
        self.damaged = synthetic_jpeg(DEC, seed=4, pixels=200_000)
        card = bytearray(2 << 20)
        card[0:len(self.new)] = self.new
        card[1 << 19:(1 << 19) + len(self.old)] = self.old
        card[600 * 1024:600 * 1024 + len(self.video)] = self.video
        card[1 << 20:(1 << 20) + 65536] = self.damaged[:65536]  # rest was erased
        self.image = os.path.join(self.dir, "card.img")
        with open(self.image, "wb") as fh:
            fh.write(card)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_then_extract_a_date_range(self):
        results = os.path.join(self.dir, "results")
        text = quiet("scan", self.image, "--out", results, "--from", "2025-12-01", "--to", "2025-12-31")
        self.assertIn("Found 2 photos and 1 videos from 2025-12-01 to 2025-12-31", text)
        self.assertTrue(os.path.exists(os.path.join(results, "findings.csv")))

        out = os.path.join(self.dir, "recovered")
        quiet("extract", os.path.join(results, "findings.json"), "--from", "2025-12-01",
              "--to", "2025-12-31", "--out", out)
        files = {}
        for name in os.listdir(out):
            with open(os.path.join(out, name), "rb") as fh:
                files[name] = fh.read()
        self.assertEqual(len(files), 4)
        self.assertIn(self.old, files.values())
        # The damaged photo's embedded preview comes out whole, as its own file.
        thumb = [d for n, d in files.items() if n.endswith("_THUMB.jpg")]
        self.assertEqual(thumb, [synthetic_jpeg(pixels=300, seed=5, width=160, height=120, with_exif=False)])
        self.assertIn(self.video, files.values())
        partial = [d for n, d in files.items() if n.endswith("_PARTIAL.jpg")]
        self.assertEqual(len(partial), 1)
        self.assertTrue(partial[0].startswith(self.damaged[:60000]))
        self.assertTrue(partial[0].endswith(b"\xff\xd9"))  # closed so viewers can open it
        self.assertNotIn(self.new, files.values())  # February is outside the range

    def test_extract_needs_a_filter(self):
        results = os.path.join(self.dir, "results")
        quiet("scan", self.image, "--out", results)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["extract", os.path.join(results, "findings.json")]), 2)

    def test_sort_photorec_output(self):
        recup = os.path.join(self.dir, "recup_dir.1")
        os.makedirs(recup)
        for name, data in (("f0001.jpg", self.old), ("f0002.jpg", self.new), ("f0003.mp4", self.video)):
            with open(os.path.join(recup, name), "wb") as fh:
                fh.write(data)
        picked = os.path.join(self.dir, "tinkerbell")
        text = quiet("sort", recup, "--from", "2025-12-14", "--to", "2025-12-14", "--out", picked)
        self.assertIn("Found 1 photos and 1 videos", text)
        self.assertEqual(sorted(os.listdir(picked)),
                         ["2025-12-14_140000_f0001.jpg", "2025-12-14_140000_f0003.mp4"])
        # Sorting a second tool's output into the same folder skips duplicates.
        again = quiet("sort", recup, "--from", "2025-12-14", "--to", "2025-12-14", "--out", picked)
        self.assertIn("Copied 0 files", again)
        self.assertIn("2 were already there", again)


if __name__ == "__main__":
    unittest.main()
