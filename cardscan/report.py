"""Turn a list of findings into something a person can read."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime

from .scanner import Finding

MAP_WIDTH = 60


def human(n: float) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def in_range(f: Finding, lo: datetime | None, hi: datetime | None) -> bool:
    dt = f.taken_dt
    if dt is None or (lo is None and hi is None):
        return False
    return (lo is None or dt >= lo) and (hi is None or dt <= hi)


def _month(f: Finding) -> str:
    return f.taken[:7] if f.taken else "no date"


def summarize(findings: list[Finding], card_size: int | None,
              lo: datetime | None = None, hi: datetime | None = None,
              next_step: str = "Recover them with the `extract` command (see README, step 5).") -> str:
    photos = [f for f in findings if f.kind == "jpeg"]
    videos = [f for f in findings if f.kind == "mp4"]
    out = []
    where = f" on {human(card_size)} of card" if card_size else ""
    out.append(f"Recognised {len(photos)} photos and {len(videos)} videos{where}.")
    broken = sum(not f.complete for f in findings)
    if broken:
        out.append(f"{broken} of them are damaged (marked PARTIAL); the rest are intact.")
    out.append("")

    # --- by month --------------------------------------------------------
    rows = defaultdict(Counter)
    for f in findings:
        rows[_month(f)][f.kind] += 1
        if not f.complete:
            rows[_month(f)]["partial"] += 1
    out.append("By the month the camera says they were taken:")
    out.append("  month      photos  videos  damaged")
    for month in sorted(rows, key=lambda m: (m == "no date", m)):
        c = rows[month]
        mark = ""
        if lo or hi:
            if any(in_range(f, lo, hi) for f in findings if _month(f) == month):
                mark = "   <-- your date range"
        out.append(f"  {month:<9}  {c['jpeg']:>6}  {c['mp4']:>6}  {c['partial']:>7}{mark}")
    out.append("")

    # --- the date range the user cares about ------------------------------
    if lo or hi:
        hits = [f for f in findings if in_range(f, lo, hi)]
        span = f"{lo:%Y-%m-%d}" if lo else "the beginning"
        span += f" to {hi:%Y-%m-%d}" if hi else " onwards"
        if hits:
            hp = [f for f in hits if f.kind == "jpeg"]
            hv = [f for f in hits if f.kind == "mp4"]
            out.append(f"*** Found {len(hp)} photos and {len(hv)} videos from {span}. ***")
            by_day = Counter(f.taken[:10] for f in hits)
            for day in sorted(by_day):
                intact = sum(f.complete for f in hits if f.taken[:10] == day)
                out.append(f"    {day}: {by_day[day]} files ({intact} intact)")
            out.append(next_step)
        else:
            out.append(f"Nothing dated {span} was recognised.")
            out.append("If the GoPro's clock was wrong the dates will be off too: look for a")
            out.append("burst of photos on a single unexpected day, and check 'no date' above.")
        out.append("")

    if card_size and findings:
        out.extend(card_map(findings, card_size, lo, hi))
    return "\n".join(out)


def card_map(findings: list[Finding], card_size: int,
             lo: datetime | None, hi: datetime | None, width: int = MAP_WIDTH) -> list[str]:
    """Show where each day's files physically sit, start of card on the left.

    After a format, a camera usually writes new footage from the start of the
    card onward, so new files cluster on the left and whatever is further right
    is old data that has not been overwritten yet.
    """
    step = card_size / width
    target, other = [0] * width, [0] * width
    for f in findings:
        a, b = f.offset, min(f.offset + f.size, card_size)
        cells = target if in_range(f, lo, hi) else other
        for i in range(int(a // step), min(int((b - 1) // step), width - 1) + 1):
            cells[i] += 1
    bar = "".join("#" if t else ("=" if o else ".") for t, o in zip(target, other))
    out = ["Card map (start of the card on the left):",
           f"  |{bar}|",
           "   # your date range   = other recognised files   . nothing recognised",
           ""]

    # Walk the files in the order they sit on the card and group runs taken on
    # the same day. (Fall back to months if a card holds very many days.)
    for key in (lambda f: f.taken[:10] if f.taken else None, lambda f: f.taken[:7] if f.taken else None):
        runs = _runs(findings, key, lo, hi)
        if len(runs) <= 30:
            break
    out.append("  where on the card            files   taken on")
    pos = 0
    gap = card_size / 100  # only call out empty stretches bigger than 1% of the card
    for label, a, b, count, hits in runs + [(None, card_size, card_size, 0, 0)]:
        if a - pos > gap:
            out.append(f"  {human(pos):>9} - {human(a):<9}      -    (nothing recognised)")
        if count:
            mark = "   <-- your date range" if hits else ""
            out.append(f"  {human(a):>9} - {human(b):<9}  {count:>6}    {label or 'no date'}{mark}")
        pos = max(pos, b)
    return out


def _runs(findings, key, lo, hi) -> list[list]:
    runs: list[list] = []  # [label, start, end, files, files in range]
    for f in sorted(findings, key=lambda f: f.offset):
        k = key(f)
        if k is None and runs:  # undated files (video thumbnails) join their neighbours
            k = runs[-1][0]
        hit = int(in_range(f, lo, hi))
        if runs and runs[-1][0] == k:
            r = runs[-1]
            r[2], r[3], r[4] = max(r[2], f.offset + f.size), r[3] + 1, r[4] + hit
        else:
            runs.append([k, f.offset, f.offset + f.size, 1, hit])
    return runs


def write_csv(findings: list[Finding], path: str) -> None:
    fields = ["offset", "offset_hex", "kind", "status", "taken", "camera",
              "width", "height", "duration_s", "size", "note"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for f in findings:
            w.writerow({
                "offset": f.offset, "offset_hex": hex(f.offset), "kind": f.kind,
                "status": "intact" if f.complete else "PARTIAL", "taken": f.taken or "",
                "camera": f.camera, "width": f.width or "", "height": f.height or "",
                "duration_s": f.duration_s or "", "size": f.size, "note": f.note,
            })
