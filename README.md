# photo-recovery

Getting back photos and videos from a GoPro SD card after everything on it was
deleted and new footage was recorded on top.

This repo has two things:

1. **This guide**: what to do, in order, starting with what to do *right now*.
2. **`cardscan`**: a small, dependency-free Python tool that scans a copy of
   the card, tells you whether anything from the dates you care about survived
   and where it sits on the card, and pulls those files out. It never writes to
   the card.

It works alongside **PhotoRec**, the free, long-established recovery tool. Use
both: they work differently, so if one misses something the other may catch it.

> **Why there is real hope:** when a camera "deletes all" or formats a card,
> it wipes the card's *table of contents*, not the photos. The photo data stays
> exactly where it was until something new is written over that exact spot. New
> footage is usually written from the start of the card onward, so anything that
> sat further in can survive untouched. The [full explanation](docs/how-it-works.md)
> walks through why.

---

## Step 0: Protect the card (do this now)

Every new write can destroy something that is still recoverable.

- **Don't put the card back in the GoPro**, don't record on it, and don't
  format it. Turning the camera on with the card inserted can write to it.
- **Lock it before plugging it into a computer.** Put the microSD card in a
  full-size SD adapter and slide the adapter's little lock tab to **LOCK**.
  Your computer will then treat the card as read-only.
  - *Why:* computers quietly write small hidden files to cards when they mount
    them (macOS writes `.Spotlight-V100`/`.fseventsd`, Windows can write
    `System Volume Information`). On a FAT/exFAT card those writes land in the
    next free space, which is right next to the newest files. That is exactly
    where old data is most likely to have survived.
- If your computer offers to **"format"** or **"repair"** the card, click
  **Cancel**.
- Keep the hiking footage too. The image you make in Step 2 captures it along
  with everything else.

## Step 1: Check the easy places first (about 15 minutes)

Recovery from the card is a backstop. Copies are often already somewhere else:

- **GoPro cloud**: if you ever had a GoPro subscription (GoPro Plus), the
  HERO5 Black can auto-upload while charging. Sign in at gopro.com → Media
  Library and look at December 2025.
- **GoPro Quik app on your phone**: if you ever previewed or downloaded the
  shoot over Wi-Fi, check the app's media and your phone's photo library
  (often an album named *GoPro*), plus iCloud Photos or Google Photos backups.
- **Your computers**: search for `GOPR*` and `G00*` (GoPro's file names), and
  look in the Photos/Lightroom/Google Photos libraries around December 2025.
- **Other people**: anyone at the shoot may have taken phone photos. Also check
  photos you *sent* to someone by text, email, or social media. Those copies
  are smaller, but they are real.

## Step 2: Make a copy (an "image") of the whole card

You scan and recover from the image, never from the card. You need free disk
space at least as big as the card, e.g. 64 GB for a 64 GB card.

*Why an image:* every tool then works on a copy, so nothing can make things
worse. You can also try as many tools as you like, as many times as you like,
and put the card away somewhere safe.

**macOS**

```sh
diskutil list                      # find the card, e.g. /dev/disk4. CHECK THE SIZE matches the card
diskutil unmountDisk /dev/disk4
brew install ddrescue              # best option: copes with flaky cards and can resume
sudo ddrescue /dev/rdisk4 ~/tinkerbell-card.img ~/tinkerbell-card.map
# or, without Homebrew (press Ctrl+T to see progress):
sudo dd if=/dev/rdisk4 of=~/tinkerbell-card.img bs=4m
```

**Linux**

```sh
lsblk                              # find the card, e.g. /dev/sdb (the whole card, not sdb1)
sudo ddrescue /dev/sdb ~/tinkerbell-card.img ~/tinkerbell-card.map
```

**Windows**: install **Win32 Disk Imager**, choose the card's drive letter,
type a file name like `C:\recovery\tinkerbell-card.img`, make sure *Read Only
Allocated Partitions* is **unticked**, and click **Read**. (If imaging is a
hassle on Windows, you can point PhotoRec straight at the card in Step 4. It
only reads.)

Check that the image is about the size of the card (a "64 GB" card gives
roughly 63-64 GB). Then put the card somewhere safe.

## Step 3: Find out what survived, with `cardscan`

You need Python 3.9 or newer. Nothing else is required.

```sh
git clone https://github.com/wombus/photo-recovery.git
cd photo-recovery
python3 -m cardscan scan ~/tinkerbell-card.img --from 2025-12-01 --to 2025-12-31
```

(On Windows use `py -m cardscan scan C:\recovery\tinkerbell-card.img ...`.)

It reads the whole image front to back. Expect a few minutes for 64 GB from
an SSD. It reports every photo and video it can recognise, using the date the
camera stamped inside each file, and a map of where things physically sit.
Here is the output from a simulated card that went through the same history
as yours (see [Testing](#testing-it-yourself)). The `◀` notes are mine, not
part of the output:

```
Recognised 319 photos and 11 videos on 268.4 MB of card.

By the month the camera says they were taken:
  month      photos  videos  damaged
  2025-10        58       0        0
  2025-12        82       2        0   <-- your date range
  2026-01       177       9        0
  no date         2       0        0

*** Found 82 photos and 2 videos from 2025-12-01 to 2025-12-31. ***
    2025-12-14: 84 files (84 intact)

Card map (start of the card on the left):
  |.===========================###########===========..........|
   # your date range   = other recognised files   . nothing recognised

  where on the card            files   taken on
        0 B - 5.2 MB         -    (nothing recognised)
     5.2 MB - 103.1 MB       65    2026-01-21                ◀ recorded after the delete
   103.4 MB - 126.4 MB       58    2025-10-12                ◀ partly overwritten
   126.4 MB - 173.1 MB       85    2025-12-14   <-- your date range
   173.1 MB - 221.6 MB      122    2026-01-18
   221.6 MB - 268.4 MB       -    (nothing recognised)
```

How to read it:

- **The by-month table** tells you what's there. "Damaged" files were partly
  overwritten. They're still worth recovering, because most viewers show the
  surviving part.
- **The card map** tells you *why*. The hiking footage sits at the start of
  the card. Whatever was underneath it is gone, and everything to its right is
  old data that survived. In this simulation the July photos (originally at the
  start) are gone entirely, October was half overwritten, and the December
  shoot came back complete.
- A full list is written to `scan-results/findings.csv`, which you can open in
  any spreadsheet.

**If December doesn't show up**, look at the table before giving up. If the
GoPro's clock was ever reset (for example after the battery was out for a
while), the photos carry the wrong date. Look for a burst of photos on one
unexpected day, or rows under "no date", and re-run with that date range.

## Step 4: Recover the files

**With cardscan** (write the output to your computer, never to the card):

```sh
python3 -m cardscan extract scan-results/findings.json --from 2025-12-01 --to 2025-12-31 --out tinkerbell
```

Files are named by the moment they were taken, e.g.
`2025-12-14_140114_jpeg_at000007947400.jpg`. Add `--undated` to also get
files with no date (video thumbnails), or use `--all` to extract everything.

**With PhotoRec as well** (free, from cgsecurity.org; the "TestDisk & PhotoRec"
download). It recognises hundreds of formats, including the HERO5's `.GPR`
RAW photos if you had RAW turned on, which cardscan doesn't handle.

1. Install: Windows → unzip and run `qphotorec_win.exe` as administrator;
   macOS → `brew install testdisk`; Linux → `sudo apt install testdisk`.
2. Run it on the image: `sudo photorec ~/tinkerbell-card.img` (or pick the
   card in QPhotoRec).
3. Choose the FAT32/exFAT partition. Under **File Opt** you can leave
   everything on, or turn everything off and enable just `jpg` and `mov`
   (`mov` covers GoPro `.mp4`).
4. Filesystem type: **Other**. Then choose **Free**, which scans only the
   unallocated space where deleted files live.
5. Pick a destination folder **on your computer** and start. PhotoRec loses
   the original file names and writes `recup_dir.1/f12345678.jpg` and so on.
   (It also drops a `photorec.log` in the current folder; that's harmless.)
6. Thousands of files with meaningless names are hard to search by eye. Let
   cardscan find the December ones by the date stamped inside each file:

   ```sh
   python3 -m cardscan sort recup_dir.* --from 2025-12-01 --to 2025-12-31 --out tinkerbell-photorec
   ```

On the simulated card, both routes recovered all 84 birthday-shoot files,
byte-for-byte identical to the originals.

## Step 5: If a video won't play

A video has two parts: the frames and an index (`moov`) that says where each
frame is. The camera writes the index *last*, so a video can survive without
it. cardscan marks these `PARTIAL` with the note "moov index did not survive".

Such videos can often be rebuilt with **untrunc**
(github.com/anthwlock/untrunc). It needs a healthy *reference* video: record a
few seconds on the same HERO5, with the same resolution and frame rate, **on a
different card**. Then run `untrunc reference.mp4 broken.mp4`.

GoPro also records a low-resolution copy of every video (`.LRV`, about
848×480). It shows up as a separate smaller video and can survive when the
full-size one didn't.

## If nothing turns up

Be clear-eyed about what the map says:

- **Old months visible, December not**: December was most likely in the area
  the hiking footage overwrote. Overwritten data cannot be recovered by any
  software or lab.
- **Only the hiking footage, and "nothing recognised" everywhere else**: the
  delete may have actually erased the card. (SD cards support an erase
  command, and some formatting tools use it.) That data is gone too.
- **Professional recovery labs** help when a card is *physically* failing
  (unreadable, not detected). They can't bring back overwritten data, so be
  wary of any lab that promises otherwise.

Step 1 matters most in these cases: it's often where the photos turn up.

---

## How the tool works (short version)

```
cardscan/
  reader.py   read-only, sector-aligned access to an image or raw device
  scanner.py  sweep the card for file signatures at sector boundaries
  jpeg.py     walk JPEG segments to find where a photo ends; read EXIF date/camera
  mp4.py      walk MP4 boxes to find where a video ends; read creation date/firmware
  report.py   by-month table and card map
  cli.py      scan / extract / sort
```

It ignores the (wiped) filesystem and looks at raw bytes. Every FAT32/exFAT
file starts on a sector boundary, so at each multiple of 512 bytes it asks:
"does a JPEG (`FF D8 FF`) or MP4 (`....ftyp`) start here?" When one does, it
follows that file's own internal structure (segment lengths for JPEG, box
sizes for MP4) to find where the file ends, and reads the date the camera
stamped inside it. This technique is called *file carving*.
[docs/how-it-works.md](docs/how-it-works.md) explains each layer in depth.

## Testing it yourself

```sh
python3 -m unittest discover -s tests -t .       # 26 tests, stdlib only
```

`tools/simulate_card.py` replays the whole story on a real FAT32 filesystem.
It fills a card from July 2025 to January 2026, quick-formats it like the
camera does, then records "hiking" footage on top. It writes a manifest of
every file with its SHA-256 hash, so you can check exactly what comes back.
It needs `dosfstools` and `mtools`; `--real-photos` needs Pillow and makes
photos you can open:

```sh
python3 tools/simulate_card.py /tmp/sim --real-photos
python3 -m cardscan scan /tmp/sim/card.img --from 2025-12-01 --to 2025-12-31 --out /tmp/sim/results
```

Of 206 MB written before the delete, and 93 MB recorded after:

| session | when | recovered byte-identical |
|---|---|---|
| summer | 2025-07 | 0 / 204 (all overwritten) |
| fall walk | 2025-10 | 58 / 102 |
| **16th birthday shoot** | **2025-12-14** | **85 / 85** |
| trip, before the delete | 2026-01-18 | 122 / 122 |
| hiking, after the delete | 2026-01-21 | 65 / 65 |
