#!/usr/bin/env python3
"""Replay "full card -> formatted in the camera -> went hiking" on a real FAT32 image.

    python3 tools/simulate_card.py OUT_DIR [--size-mib 256] [--real-photos] [--real-videos]

Writes OUT_DIR/card.img and OUT_DIR/manifest.json (every file written, with
its SHA-256), so you can scan the image and check exactly what came back.

The scenario is chosen so the hiking footage ends partway through a multi-
chapter December video: one chapter is lost entirely, one loses its beginning
(and can be rebuilt from its surviving index), and the rest are intact.

Needs mkfs.fat (dosfstools) and mtools. mtools writes into a FAT image without
mounting it and, like camera firmware, allocates clusters from the start of
the card. --real-photos uses Pillow and --real-videos uses ffmpeg to make
files you can actually open and watch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.fixtures import synthetic_jpeg, synthetic_mp4  # noqa: E402

PART_OFFSET = 1 << 20  # SD cards put their one partition 1-4 MiB in
CHAPTER = 8 << 20  # GoPro splits long videos into chapters (4 GB on real cards)

# (label, first timestamp, [(what, share of the card), ...]) in recording order
BEFORE_FORMAT = [
    ("summer", datetime(2025, 7, 4, 10, 0), [("photos", 0.30)]),
    ("fall walk", datetime(2025, 10, 12, 16, 0), [("photos", 0.06)]),
    ("16th birthday shoot", datetime(2025, 12, 14, 14, 0), [("video", 0.12), ("photos", 0.12)]),
    ("trip, before the format", datetime(2026, 1, 18, 9, 0), [("photos", 0.18)]),
]
AFTER_FORMAT = [
    ("hiking", datetime(2026, 1, 21, 8, 0), [("photos", 0.08), ("video", 0.32)]),
]


def run(*cmd: str) -> None:
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    subprocess.run(cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def write_mbr(image: str, size: int) -> None:
    """One FAT32 (LBA) partition, as on a real SD card: a 16-byte entry at 446."""
    start, count = PART_OFFSET // 512, (size - PART_OFFSET) // 512
    entry = struct.pack("<B3sB3sII", 0x00, b"\xfe\xff\xff", 0x0C, b"\xfe\xff\xff", start, count)
    with open(image, "r+b") as fh:
        fh.seek(446)
        fh.write(entry + b"\x00" * 48 + b"\x55\xaa")


def quick_format(image: str, size: int) -> None:
    """What the camera's "Format" does: new empty tables, the data left alone."""
    spc = "8" if size >= 600 << 20 else "1"
    run("mkfs.fat", "-F", "32", "-s", spc, "-n", "GOPRO", "--offset", str(PART_OFFSET // 512),
        image, str((size - PART_OFFSET) // 1024))


def make_photo(when: datetime, seed: int, real: bool, label: str) -> bytes:
    if not real:
        return synthetic_jpeg(when, pixels=250_000, seed=seed)
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(seed)
    w, h = 960, 720
    img = Image.effect_noise((w, h), 64).convert("RGB")
    tint = Image.new("RGB", (w, h), tuple(rng.randrange(60, 200) for _ in range(3)))
    img = Image.blend(img, tint, 0.6)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=56)
    d.text((40, 40), label, fill="white", font=font)
    d.text((40, 120), when.strftime("%Y-%m-%d %H:%M:%S"), fill="white", font=font)
    exif = Image.Exif()
    stamp = when.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x010F], exif[0x0110], exif[0x0132] = "GoPro", "HERO5 Black", stamp
    exif.get_ifd(0x8769)[0x9003] = stamp
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "p.jpg")
        img.save(path, "JPEG", quality=92, exif=exif)
        with open(path, "rb") as fh:
            return fh.read()


def make_video(when: datetime, size: int, seed: int, real: bool, label: str,
               width: int = 1280, height: int = 720) -> bytes:
    if not real:
        return synthetic_mp4(when, frames=size, seed=seed, width=width, height=height)
    seconds = max(3, size // 1_000_000)  # about 8 Mbit/s
    text = f"{label}  %{{pts\\:hms}}"  # e.g. "16th birthday shoot ch2  0:00:04.500"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "v.mp4")
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000", "-t", str(seconds),
            "-vf", f"noise=alls=25:allf=t,drawtext=text='{text}':fontsize={height // 12}:x=30:y=30:"
                   "fontcolor=white:box=1:boxcolor=black@0.6",
            "-c:v", "libx264", "-preset", "veryfast", "-g", "15", "-b:v", str(size * 8 // seconds),
            "-maxrate", str(size * 8 // seconds), "-bufsize", "4M", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-metadata", f"creation_time={when.isoformat()}Z", path], check=True)
        with open(path, "rb") as fh:
            return fh.read()


def session_files(label: str, start: datetime, parts, size: int, counter: list,
                  real_photos: bool, real_videos: bool):
    when = start
    for what, share in parts:
        budget = int(size * share)
        if what == "photos":
            while budget > 0:
                counter[0] += 1
                data = make_photo(when, counter[0], real_photos, f"{label} #{counter[0]}")
                yield f"GOPR{counter[0]:04d}.JPG", data, when, "jpeg"
                budget -= len(data)
                when += timedelta(seconds=37)
            continue
        counter[0] += 1
        n = counter[0]
        chapters = max(1, budget // CHAPTER)
        for c in range(chapters):
            name = f"GOPR{n:04d}" if c == 0 else f"GP{c:02d}{n:04d}"  # GoPro's chapter naming
            yield (name + ".MP4", make_video(when, budget // chapters, n * 100 + c, real_videos,
                                             f"{label} ch{c + 1}"), when, "mp4")
            when += timedelta(minutes=10)
        yield (f"GOPR{n:04d}.LRV", make_video(when, budget // 20, n * 100 + 99, real_videos,
                                              f"{label} low-res", 848, 480), when, "mp4")
        yield (f"GOPR{n:04d}.THM", synthetic_jpeg(with_exif=False, width=160, height=120,
                                                  pixels=6000, seed=n), None, "jpeg")


def simulate(out_dir: str, size_mib: int = 256, real_photos: bool = False,
             real_videos: bool = False) -> str:
    os.makedirs(out_dir, exist_ok=True)
    image = os.path.join(out_dir, "card.img")
    size = size_mib << 20
    with open(image, "wb") as fh:
        fh.truncate(size)
    write_mbr(image, size)
    manifest, counter = [], [0]
    target = f"{image}@@{PART_OFFSET}"
    with tempfile.TemporaryDirectory() as staging:
        for phase, sessions in (("before", BEFORE_FORMAT), ("after", AFTER_FORMAT)):
            quick_format(image, size)
            run("mmd", "-i", target, "::DCIM", "::DCIM/100GOPRO")
            for label, start, parts in sessions:
                paths = []
                for name, data, taken, kind in session_files(label, start, parts, size, counter,
                                                             real_photos, real_videos):
                    path = os.path.join(staging, name)
                    with open(path, "wb") as fh:
                        fh.write(data)
                    paths.append(path)
                    manifest.append({"name": name, "session": label, "phase": phase, "kind": kind,
                                     "taken": taken.isoformat() if taken else None, "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()})
                run("mcopy", "-i", target, *paths, "::DCIM/100GOPRO/")
                for path in paths:
                    os.remove(path)
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    return image


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out_dir")
    p.add_argument("--size-mib", type=int, default=256)
    p.add_argument("--real-photos", action="store_true", help="make viewable photos (needs Pillow)")
    p.add_argument("--real-videos", action="store_true", help="make watchable videos (needs ffmpeg)")
    a = p.parse_args()
    image = simulate(a.out_dir, a.size_mib, a.real_photos, a.real_videos)
    print(f"Wrote {image} and manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
