# photo-recovery

Getting back photos and videos from a GoPro SD card that was **formatted in
the camera** and then had new footage recorded on top. The procedure aims to
be as thorough as possible, with speed a secondary concern.

What's here:

1. **This guide**: the steps in order, starting with what to do *right now*.
2. **`cardscan`**: a dependency-free Python tool that reads a copy of the card,
   tells you what survived from the dates you care about and where it sits, and
   recovers it. That includes rebuilding videos whose *beginning* was
   overwritten, which other tools can't do. It never writes to the card.
3. **PhotoRec**, the free, long-established recovery tool, set to its most
   thorough mode. It can stitch together photos that were split into pieces
   across the card, which cardscan can't.

Use **both**. Each recovers things the other misses (see the
[evidence](#what-each-tool-recovered-in-testing)).

> **Why there is real hope:** formatting in a camera is a *quick format*. It
> writes a new, empty table of contents and leaves every photo where it was.
> A photo is only lost where new footage was written over that exact spot, and
> a camera writes new footage from the start of the card onward. Anything
> further in can survive untouched.
> [docs/how-it-works.md](docs/how-it-works.md) explains why, step by step.

---

## Step 0: Protect the card (now)

- **Stop using it.** Don't put it back in the GoPro (turning the camera on can
  write to it), don't record on it, and don't format it again.
- **Lock it before plugging it into a computer.** Put the microSD card in a
  full-size SD adapter and slide the adapter's lock tab to **LOCK**.
  Computers write small hidden files to cards they mount (macOS:
  `.Spotlight-V100`, `.fseventsd`; Windows: `System Volume Information`). Those
  writes land right after the newest files, which is exactly where old data is
  most likely to have survived.
- If the computer offers to **format** or **repair** the card: **Cancel**.
- **Plug it in as few times as possible.** Make one copy (Step 2), then put the
  card away unplugged. Flash memory can tidy itself up in the background while
  powered, which matters only for the long-shot lab option in Step 7.

## Step 1: Check for other copies (about 15 minutes)

These are often where photos turn up, so don't skip this step.

- **GoPro cloud**: with a GoPro subscription, a HERO5 Black can auto-upload
  while charging. Check gopro.com → Media Library around December 2025.
- **GoPro Quik app** on your phone: check its media library and your phone's
  photos (often an album named *GoPro*), plus iCloud Photos or Google Photos
  backups. If you ever previewed the card over Wi-Fi, the app may also have
  cached previews.
- **Your computers**: search for `GOPR*` and `G00*` (GoPro's file names), and
  check Photos/Lightroom/Google Photos libraries for December 2025.
- **Other people and messages**: phones of anyone at the shoot, and photos you
  sent by text, email or social media.

## Step 2: Make a complete copy ("image") of the card

All recovery works from the image, never the card. You need free space at
least the size of the card, e.g. 64 GB for a 64 GB card.

Use **ddrescue**. Unlike a plain copy, it retries unreadable spots, keeps a
log so it can resume, and tells you if anything couldn't be read.

**macOS** (install it first with `brew install ddrescue`):

```sh
diskutil list                         # find the card, e.g. /dev/disk4. CHECK THE SIZE matches the card
diskutil unmountDisk /dev/disk4
sudo ddrescue -r3 /dev/rdisk4 ~/tinkerbell-card.img ~/tinkerbell-card.map
```

**Linux** (`sudo apt install gddrescue`):

```sh
lsblk                                 # find the card, e.g. /dev/sdb (the whole card, not sdb1)
sudo ddrescue -d -r3 /dev/sdb ~/tinkerbell-card.img ~/tinkerbell-card.map
```

**Windows**: install **Win32 Disk Imager**, pick the card's drive letter and a
file such as `C:\recovery\tinkerbell-card.img`, make sure *Read Only Allocated
Partitions* is **unticked**, and click **Read**.

When ddrescue finishes, its summary should say **rescued: 100%** and **bad
areas: 0**. If it doesn't, run the same command again: it resumes and retries
only what's missing. Check that the image is about the card's size, keep a
second copy of it on another drive, and put the card away.

## Step 3: Deep scan with cardscan

Needs Python 3.9+, nothing else.

```sh
git clone https://github.com/wombus/photo-recovery.git
cd photo-recovery
python3 -m cardscan scan ~/tinkerbell-card.img --deep --from 2025-12-01 --to 2025-12-31
```

(Windows: `py -m cardscan scan C:\recovery\tinkerbell-card.img --deep ...`)

`--deep` is the most thorough mode:

- It looks for pictures at **every byte**, not just at file boundaries.
- It finds **video indexes whose video lost its beginning** and works out what
  can be rebuilt.
- It **checks every frame** of every video that looks intact, so a video
  damaged in the middle isn't reported as fine.
- It checks photos' built-in **restart markers**, which catch a photo spliced
  together with other data.

It reads the whole image. Expect minutes to an hour, depending on card size
and disk speed.

Here is the output from a simulated card with the same history as yours
(see [Testing](#testing-it-yourself)). The `◀` notes are mine:

```
Recognised 261 photos and 14 videos on 268.4 MB of card.
1 of them is damaged or only partly recoverable (see the notes below and findings.csv).

By the month the camera says they were taken:
  month      photos  videos  damaged
  2025-12        82       3        1   <-- your date range
  2026-01       177      11        0
  no date         2       0        0

*** Found 82 photos and 3 videos from 2025-12-01 to 2025-12-31. ***
    2025-12-14: 85 files (84 intact)
  Damaged or rebuilt:
    2025-12-14T14:10:00 video: beginning overwritten; the last 3.9s of 10.0s are rebuilt from the surviving index

Card map (start of the card on the left):
  |.=========================############===========...........|
   # your date range   = other recognised files   . nothing recognised

  where on the card            files   taken on
     5.2 MB - 119.7 MB       67    2026-01-21     ◀ recorded after the format
   119.9 MB - 169.6 MB       86    2025-12-14   <-- your date range
   169.6 MB - 218.1 MB      122    2026-01-18
```

- **The table** shows what's there. Everything is recorded in
  `scan-results/findings.csv`, whatever the date filter.
- **"Damaged or rebuilt"** explains each imperfect file in your date range.
- **The card map** shows *why*: the post-format footage sits at the start, and
  whatever is to its right survived.

**Sanity check:** the hiking photos and videos were recorded on your HERO5
after the format, so they are genuine, undamaged files from your camera. They
should all be reported intact. Videos are labelled with GoPro's firmware
version, which starts with `HD5` for a HERO5. If they come back intact, the
tool is handling your camera's files correctly.

**If December doesn't appear**, the GoPro's clock may have been wrong. It
resets if the battery is out for a while, and then the camera stamps files
**2016-01-01**: one of GoPro's own published sample clips carries exactly
that date. Look for a burst of files on one unexpected day (2016 especially),
or under "no date", and scan again with that range.

## Step 4: Recover with cardscan

Write recovered files to your computer, never to the card.

```sh
python3 -m cardscan extract scan-results/findings.json --from 2025-12-01 --to 2025-12-31 --undated --out tinkerbell
python3 -m cardscan extract scan-results/findings.json --all --out everything   # keep everything, in case dates are off
```

Files are named by when they were taken. Suffixes mark the special cases:

| suffix | what it is |
|---|---|
| *(none)* | intact, byte-for-byte as the camera wrote it |
| `_PARTIAL.jpg` | a damaged photo; viewers show the part that survived |
| `_PARTIAL_THUMB.jpg` | the small preview stored inside that damaged photo: the **whole** picture, at low resolution |
| `_REBUILT.mp4` | a video whose beginning was overwritten; its surviving end, rebuilt into a playable file |
| `_PARTIAL.mp4` | a video whose frames survived but whose index didn't; see Step 6 |

## Step 5: Recover with PhotoRec in brute-force mode

Install it from cgsecurity.org ("TestDisk & PhotoRec"): on macOS
`brew install testdisk`, on Linux `sudo apt install testdisk`, on Windows unzip
the download and use `photorec_win.exe`.

Brute-force mode is what reassembles photos that were stored in several
pieces. It isn't on by default, and in testing the default settings recovered
none of the fragmented photos (see [below](#what-each-tool-recovered-in-testing)).
Use the text-mode `photorec`, which has the option (the QPhotoRec window may
not):

```sh
sudo photorec ~/tinkerbell-card.img
```

1. Select the FAT32/exFAT partition.
2. **[Options]**: set **Paranoid** to **Yes (Brute force enabled)** and **Keep
   corrupted files** to **Yes**.
3. **[File Opt]**: leave everything enabled.
4. **[Search]**: filesystem type **[Other]**, then **[Whole]** (scan everything).
5. Choose a destination folder **on your computer** and confirm with `C`.

Or, as one command (the leading `1` is the partition number PhotoRec lists):

```sh
sudo photorec /log /d ~/recovery/photorec /cmd ~/tinkerbell-card.img 1,options,paranoid_bf,keep_corrupted_file,fileopt,everything,enable,wholespace,search
```

Brute force can take hours on a large card. That's expected.

PhotoRec loses the original names: it writes folders of files like
`f12345678.jpg`, named `photorec.1`, `photorec.2`, … with the command above
(`recup_dir.1`, … from the menus). Have cardscan pick out December by the date
stamped inside each file, into the same folder as Step 4. Files you already
have from cardscan are skipped:

```sh
python3 -m cardscan sort ~/recovery/photorec.* --from 2025-12-01 --to 2025-12-31 --out tinkerbell
```

## Step 6: Videos

- **`_REBUILT.mp4`**: watch it. It starts at the first complete keyframe after
  the overwritten part, with sound.
- **`_PARTIAL.mp4`** (frames without an index): try **untrunc**
  (github.com/anthwlock/untrunc). It needs a healthy *reference* video: record
  a few seconds on the same HERO5 with the same resolution and frame rate, **on
  a different card**, then run `untrunc reference.mp4 broken.mp4`.
- **Low-res copies**: GoPro records a small copy of every video (`.LRV`,
  848×480) at the same time. It shows up as its own video and may survive when
  the full-size one didn't.

## Step 7: If something is still missing

Be clear-eyed about what the card map shows:

- **Old months to the right of the hiking footage, but no December**: December
  was in the overwritten area. No software can read data that has been
  overwritten.
- **Only the hiking footage, "nothing recognised" everywhere else**: the old
  area was erased. Same conclusion.

**The long shot: a flash-memory lab.** On flash memory, "overwritten" isn't
always physically immediate. The card's controller writes new data to fresh
memory blocks and erases the old ones later. Specialist labs can remove or
tap the memory chip directly ("chip-off" or "monolith" recovery, e.g. with
PC-3000 Flash) and sometimes find older copies of data the controller hadn't
erased yet. The data most likely to linger is what was overwritten *last*,
i.e. just before the edge of the hiking footage. Be realistic: this is
expensive, it destroys the card, the odds after months of use are low, and no
honest lab will promise a result. It makes sense only if the scan shows
December sat right at the edge of the overwritten area, and only after every
software step. Labs *can* reliably help with a card that is physically
failing (not detected, unreadable).

---

## What each tool recovered in testing

Each scenario was run on a real FAT32 filesystem and checked against SHA-256
hashes of every original file.

**Card formatted in the camera, then hiking footage recorded on top**
(`tools/simulate_card.py --real-photos --real-videos`: 212 MB written over
five months, then 114 MB after the format):

| what | cardscan `--deep` | PhotoRec (brute force) |
|---|---|---|
| July photos (start of card) | 0 / 204, overwritten | same |
| October photos | 0 / 41, overwritten | same |
| **December photos** | **82 / 82** byte-identical, plus the video's thumbnail | 82 / 82 (missed the thumbnail) |
| December video, chapter 1 | lost (entirely overwritten) | lost |
| December video, chapter 2 (beginning overwritten) | **last 3.9 s of 10 s rebuilt**, plays cleanly with sound | not recovered |
| December video, chapter 3 + low-res copy | byte-identical | byte-identical |
| January photos (before the format) | 122 / 122 | 122 / 122 |
| hiking footage | 67 / 67 | 66 / 67 (missed the thumbnail) |

**Photos split into pieces** (card filled to the brim, every other photo
deleted, then 12 larger December photos written into the gaps, each ending up
in 4 pieces):

| | December photos fully recovered |
|---|---|
| cardscan | 0 / 12 complete (all 12 found, first piece only, marked PARTIAL) |
| PhotoRec, default settings | **0 / 12** |
| PhotoRec, Paranoid: brute force | **11 / 12** byte-identical |

That's why the procedure runs both tools, and why PhotoRec's brute-force mode
matters.

**Genuine GoPro files.** GoPro publishes sample clips straight off its
cameras, and ExifTool's test suite includes a real GoPro photo's metadata.
`tests/test_real_gopro.py` downloads them, pinned to exact versions and
checksums:

- All three cameras (HERO5, HERO6, HERO7) write the index **last**
  (`ftyp` → `mdat` → `moov`), which is what makes rebuilding a video's end
  possible.
- Deep scan verifies every frame of each intact clip, with no false alarms.
- With the start of the HERO5 clip overwritten (25%, 50%, 90%), cardscan
  found the original start to the byte. Every rebuilt clip decodes with zero
  errors, and **every recovered frame is pixel-identical to the original**
  (frame checksums from ffmpeg).
- The real GoPro photo's big-endian EXIF parses correctly: camera, model and
  the moment it was taken.

## How cardscan works (short version)

```
cardscan/
  reader.py   read-only, sector-aligned access to an image or raw device
  scanner.py  sweep the card for photo/video signatures and video indexes
  jpeg.py     walk JPEG segments to find where a photo ends; EXIF date, preview,
              restart-marker checks
  mp4.py      walk MP4 boxes to find where a video ends; date, firmware
  salvage.py  rebuild a video whose beginning was overwritten, from its index
  report.py   by-month table and card map
  cli.py      scan / extract / sort
```

It ignores the wiped filesystem and reads raw bytes. It recognises where
files start (JPEG `FF D8 FF`, MP4 `....ftyp`), then follows each file's own
structure to find where it ends. That technique is called *file carving*.
GoPro writes each video's index (`moov`) at the *end* of the file. So when a
video's beginning is overwritten, cardscan uses the surviving index to locate
and verify every remaining frame, and writes them into a new playable file.
[docs/how-it-works.md](docs/how-it-works.md) explains each piece in depth.

## Testing it yourself

```sh
python3 -m unittest discover -s tests -t .       # 41 tests; stdlib only
```

Some tests need `dosfstools` + `mtools` (the simulated card), `ffmpeg` (real
H.264 video), or internet access (the genuine GoPro samples), and skip
themselves if those aren't available. To replay the whole
story and look at the results:

```sh
python3 tools/simulate_card.py /tmp/sim --real-photos --real-videos   # needs Pillow and ffmpeg
python3 -m cardscan scan /tmp/sim/card.img --deep --from 2025-12-01 --to 2025-12-31 --out /tmp/sim/results
python3 -m cardscan extract /tmp/sim/results/findings.json --from 2025-12-01 --to 2025-12-31 --out /tmp/sim/recovered
```
