#!/usr/bin/env python3
"""Replay "full card -> deleted everything -> went hiking" on a real FAT32 image.

    python3 tools/simulate_card.py OUT_DIR [--size-mib 256] [--real-photos]

Writes OUT_DIR/card.img and OUT_DIR/manifest.json (every file written, with
its SHA-256), so you can scan the image and check exactly what came back.

Needs mkfs.fat (dosfstools) and mtools. mtools writes into a FAT image without
mounting it and, like most camera firmware, allocates clusters from the start
of the card. --real-photos uses Pillow to make photos you can actually open.
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

# (label, first timestamp, share of card for photos, share for video)
BEFORE_DELETE = [
    ("summer", datetime(2025, 7, 4, 10, 0), 0.30, 0.00),
    ("fall walk", datetime(2025, 10, 12, 16, 0), 0.15, 0.00),
    ("16th birthday shoot", datetime(2025, 12, 14, 14, 0), 0.12, 0.05),
    ("trip, before the delete", datetime(2026, 1, 18, 9, 0), 0.18, 0.00),
]
AFTER_DELETE = [
    ("hiking", datetime(2026, 1, 21, 8, 0), 0.08, 0.27),
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
    """What a camera's "format"/"delete all" does: new empty tables, data left alone."""
    spc = "8" if size >= 600 << 20 else "1"
    run("mkfs.fat", "-F", "32", "-s", spc, "-n", "GOPRO", "--offset", str(PART_OFFSET // 512),
        image, str((size - PART_OFFSET) // 1024))


def make_photo(when: datetime, seed: int, target: int, real: bool, label: str) -> bytes:
    if not real:
        return synthetic_jpeg(when, pixels=target, seed=seed)
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
    path = tempfile.mktemp(suffix=".jpg")
    img.save(path, "JPEG", quality=92, exif=exif)
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


def write_session(image: str, staging: str, label: str, start: datetime, photo_share: float,
                  video_share: float, size: int, counter: list, real: bool, manifest: list) -> None:
    files = []
    when = start
    budget = int(size * photo_share)
    while budget > 0:
        counter[0] += 1
        seed = counter[0]
        data = make_photo(when, seed, 250_000, real, f"{label} #{seed}")
        files.append((f"GOPR{seed:04d}.JPG", data, when, "jpeg"))
        budget -= len(data)
        when += timedelta(seconds=37)
    if video_share:
        counter[0] += 1
        n = counter[0]
        total = int(size * video_share)
        clips = max(1, total // (8 << 20))
        for c in range(clips):
            name = f"GOPR{n:04d}" if c == 0 else f"GP{c:02d}{n:04d}"  # GoPro's chapter naming
            frames = total // clips
            files.append((name + ".MP4", synthetic_mp4(when, frames=frames, seed=n * 100 + c), when, "mp4"))
            when += timedelta(minutes=17)
        files.append((f"GOPR{n:04d}.LRV", synthetic_mp4(when, frames=total // 20, width=848, height=480,
                                                         seed=n * 100 + 99), when, "mp4"))
        files.append((f"GOPR{n:04d}.THM", synthetic_jpeg(with_exif=False, width=160, height=120,
                                                         pixels=6000, seed=n), None, "jpeg"))
    paths = []
    for name, data, taken, kind in files:
        path = os.path.join(staging, name)
        with open(path, "wb") as fh:
            fh.write(data)
        paths.append(path)
        manifest.append({"name": name, "session": label, "kind": kind,
                         "taken": taken.isoformat() if taken else None,
                         "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    target = f"{image}@@{PART_OFFSET}"
    run("mcopy", "-i", target, *paths, "::DCIM/100GOPRO/")
    for path in paths:
        os.remove(path)


def simulate(out_dir: str, size_mib: int = 256, real: bool = False) -> str:
    os.makedirs(out_dir, exist_ok=True)
    image = os.path.join(out_dir, "card.img")
    size = size_mib << 20
    with open(image, "wb") as fh:
        fh.truncate(size)
    write_mbr(image, size)
    manifest, counter = [], [0]
    with tempfile.TemporaryDirectory() as staging:
        for phase, sessions in (("before", BEFORE_DELETE), ("after", AFTER_DELETE)):
            quick_format(image, size)
            run("mmd", "-i", f"{image}@@{PART_OFFSET}", "::DCIM", "::DCIM/100GOPRO")
            for label, start, ps, vs in sessions:
                first = len(manifest)
                write_session(image, staging, label, start, ps, vs, size, counter, real, manifest)
                for m in manifest[first:]:
                    m["phase"] = phase
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    return image


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out_dir")
    p.add_argument("--size-mib", type=int, default=256)
    p.add_argument("--real-photos", action="store_true", help="make viewable photos (needs Pillow)")
    a = p.parse_args()
    image = simulate(a.out_dir, a.size_mib, a.real_photos)
    print(f"Wrote {image} and manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
