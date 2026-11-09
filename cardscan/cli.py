"""Command line: `python3 -m cardscan scan|extract|sort ...`."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

from . import __version__
from . import salvage
from .reader import BlockReader
from .report import human, in_range, summarize, write_csv
from .scanner import Finding, identify_file, scan

_COPY_BLOCK = 8 << 20
_RECOVERED_EXTS = {".jpg", ".jpeg", ".mp4", ".mov", ".lrv", ".thm"}


def _day(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2025-12-01, got {text!r}")


def _range(args) -> tuple[datetime | None, datetime | None]:
    lo = args.date_from
    hi = args.date_to + timedelta(days=1, seconds=-1) if args.date_to else None
    return lo, hi


def _add_range(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="date_from", type=_day, metavar="YYYY-MM-DD",
                   help="first day you're looking for")
    p.add_argument("--to", dest="date_to", type=_day, metavar="YYYY-MM-DD",
                   help="last day you're looking for (inclusive)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cardscan",
        description="Find photos and videos that survive on a deleted or formatted card. "
                    "Reads only; never writes to the card or image.")
    p.add_argument("--version", action="version", version=f"cardscan {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="scan a card image and report what survived")
    s.add_argument("image", help="card image file (or raw device) to scan")
    s.add_argument("--out", default="scan-results", help="folder for the report (default: %(default)s)")
    _add_range(s)
    s.add_argument("--deep", action="store_true",
                   help="most thorough: check every byte offset, and verify every frame of every video")
    s.add_argument("--align", type=int, default=512,
                   help="only look for files starting at multiples of this many bytes (default: 512)")
    s.add_argument("--size", type=int, help="size of the device in bytes, if it can't be detected")

    e = sub.add_parser("extract", help="copy recovered files out of the image")
    e.add_argument("findings", help="findings.json written by `scan`")
    e.add_argument("--out", default="recovered", help="folder to write files into (default: %(default)s)")
    _add_range(e)
    e.add_argument("--all", action="store_true", help="extract everything, whatever its date")
    e.add_argument("--undated", action="store_true", help="also extract files with no date")
    e.add_argument("--kind", choices=["jpeg", "mp4"], help="only photos or only videos")
    e.add_argument("--image", help="image path, if it moved since the scan")

    t = sub.add_parser("sort", help="pick files by date out of PhotoRec's recup_dir folders")
    t.add_argument("folders", nargs="+", help="folders containing recovered files")
    t.add_argument("--out", help="copy files in the date range into this folder")
    _add_range(t)

    args = p.parse_args(argv)
    return {"scan": _cmd_scan, "extract": _cmd_extract, "sort": _cmd_sort}[args.command](args)


# --- scan ----------------------------------------------------------------

def _cmd_scan(args) -> int:
    lo, hi = _range(args)
    reader = BlockReader(args.image, size=args.size)
    os.makedirs(args.out, exist_ok=True)
    mode = "deep scan" if args.deep else "scan"
    print(f"Starting {mode} of {args.image} ({human(reader.size)}). "
          "This reads only; nothing is written to it.", file=sys.stderr)

    began = time.monotonic()

    live = sys.stderr.isatty()
    last = [-1]

    def progress(done: int, photos: int, videos: int) -> None:
        pct = done * 100 // reader.size
        if not live and pct // 10 == last[0]:
            return  # when output goes to a file, one line per 10% is plenty
        last[0] = pct // 10
        elapsed = time.monotonic() - began
        left = (reader.size - done) * elapsed / done if done else 0
        line = (f"  {pct:3d}%  {human(done)} of {human(reader.size)}  | {photos} photos, "
                f"{videos} videos so far  | about {int(left // 60)} min left")
        print("\r" + line + "   " if live else line, end="" if live else "\n",
              file=sys.stderr, flush=True)

    with reader:
        findings = list(scan(reader, align=args.align, deep=args.deep, progress=progress))
    if live:
        print(file=sys.stderr)

    data = {
        "image": os.path.abspath(args.image),
        "image_size": reader.size,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "deep": args.deep,
        "findings": [f.to_dict() for f in findings],
    }
    with open(os.path.join(args.out, "findings.json"), "w") as fh:
        json.dump(data, fh, indent=1)
    write_csv(findings, os.path.join(args.out, "findings.csv"))
    text = summarize(findings, reader.size, lo, hi)
    with open(os.path.join(args.out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print()
    print(text)
    print(f"\nFull list: {os.path.join(args.out, 'findings.csv')} (opens in any spreadsheet)")
    return 0


# --- extract ---------------------------------------------------------------

def _cmd_extract(args) -> int:
    lo, hi = _range(args)
    if not (args.all or args.undated or lo or hi):
        print("Say what to extract: --from/--to dates, --undated, or --all.", file=sys.stderr)
        return 2
    with open(args.findings) as fh:
        data = json.load(fh)
    image = args.image or data["image"]
    findings = [Finding.from_dict(d) for d in data["findings"]]
    chosen = [f for f in findings
              if (not args.kind or f.kind == args.kind)
              and (args.all or in_range(f, lo, hi) or (args.undated and not f.taken))]
    if not chosen:
        print("No findings match those filters.", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    written = total = 0
    with BlockReader(image) as reader:
        for f in chosen:
            path = os.path.join(args.out, _name(f))
            if f.moov_at is not None:
                if f.size == 0 or not salvage.rebuild(reader, f.moov_at, path):
                    print(f"  (skipped a {f.duration_s or 0:.0f}s video from {f.taken or 'an unknown date'}:"
                          f" only its index survived)")
                    continue
            else:
                _carve(reader, f, path)
            size = os.path.getsize(path)
            written, total = written + 1, total + size
            print(f"  {os.path.basename(path)}  ({human(size)})")
            if f.thumb_at is not None:
                thumb = path.rsplit(".", 1)[0] + "_THUMB.jpg"
                with open(thumb, "wb") as out:
                    out.write(reader.read(f.thumb_at, f.thumb_size))
                print(f"  {os.path.basename(thumb)}  (the whole picture, small)")
    print(f"\nWrote {written} files ({human(total)}) to {args.out}")
    if any(not f.complete for f in chosen):
        print("Files marked _PARTIAL were damaged; most viewers still show the part that survived.\n"
              "_THUMB files are the small preview stored inside a damaged photo: the whole picture.\n"
              "_REBUILT videos had their beginning overwritten; the surviving end was rebuilt.")
    return 0


def _name(f: Finding) -> str:
    when = f.taken_dt.strftime("%Y-%m-%d_%H%M%S") if f.taken else "undated"
    tag = "_REBUILT" if f.moov_at is not None else ("" if f.complete else "_PARTIAL")
    return f"{when}_{f.kind}_at{f.offset:012x}{tag}.{f.ext}"


def _carve(reader: BlockReader, f: Finding, path: str) -> None:
    with open(path, "wb") as out:
        pos, end = f.offset, f.offset + f.size
        while pos < end:
            block = reader.read(pos, min(_COPY_BLOCK, end - pos))
            if not block:
                break
            out.write(block)
            pos += len(block)
        if f.kind == "jpeg" and not f.complete:
            out.write(b"\xff\xd9")  # close the image so viewers render what's there


# --- sort ------------------------------------------------------------------

def _cmd_sort(args) -> int:
    lo, hi = _range(args)
    found = []
    for folder in args.folders:
        for root, _, files in os.walk(folder):
            for name in files:
                if os.path.splitext(name)[1].lower() not in _RECOVERED_EXTS:
                    continue
                path = os.path.join(root, name)
                try:
                    f = identify_file(path)
                except OSError:
                    continue
                if f:
                    found.append((path, f))
    hint = "Add --out FOLDER to copy them somewhere together." if not args.out else ""
    print(summarize([f for _, f in found], None, lo, hi, next_step=hint))
    if args.out and (lo or hi):
        os.makedirs(args.out, exist_ok=True)
        # Skip anything already there, e.g. the same photo recovered by `extract`.
        have = {_sha256(os.path.join(args.out, n)) for n in os.listdir(args.out)
                if os.path.isfile(os.path.join(args.out, n))}
        copied = same = 0
        for path, f in found:
            if not in_range(f, lo, hi):
                continue
            digest = _sha256(path)
            if digest in have:
                same += 1
                continue
            have.add(digest)
            when = f.taken_dt.strftime("%Y-%m-%d_%H%M%S")
            shutil.copy2(path, os.path.join(args.out, f"{when}_{os.path.basename(path)}"))
            copied += 1
        print(f"Copied {copied} files into {args.out}"
              + (f" ({same} were already there, identical)" if same else ""))
    return 0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
