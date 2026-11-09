# How deleted photos survive, and how cardscan finds them

This explains, layer by layer, why a "deleted" card can still hold your
photos, what decides which ones survive, and how a tool can find files when
the card's own index says it's empty. Each section builds on the one before.

---

## 1. What's actually on an SD card

From the bottom up:

```
flash memory cells           (the controller hides these; you never see them)
└─ sectors                   512-byte blocks numbered 0, 1, 2, ... (what a computer reads)
   └─ partition              one region of the card, usually starting 1-4 MB in
      └─ filesystem          FAT32 on cards ≤ 32 GB, exFAT on 64 GB and up
         ├─ index            "what files exist and where they are"
         └─ data area        divided into clusters (32 KB-128 KB each on camera cards)
```

The **filesystem** is the part that matters here. It has two halves:

- **The index.** In FAT32 that's the *File Allocation Table* (a linked list
  saying "cluster 812's next cluster is 813") plus *directory entries* (32-byte
  records holding a file's name, size, and first cluster). exFAT adds an
  *allocation bitmap*, one bit per cluster: 1 = in use, 0 = free.
- **The data area.** The actual bytes of your photos and videos, cluster after
  cluster.

## 2. What the camera's "Format" really does

A camera's format (like the HERO5's *Format SD Card*) is a *quick format*. It
rewrites **only the index**: a fresh boot sector, an empty
allocation table/bitmap, and an empty root folder. That's a few megabytes,
which is why it takes about a second. Actually overwriting a 64 GB data area
would take many minutes, and cameras don't do it.

So right after the format:

```
index:      [ empty: "no files, every cluster free" ]
data area:  [ Jul photos | Oct photos | Dec birthday shoot | Jan trip | ... ]   <- still all there
```

Every photo is still physically present. The card just no longer has any
record of where they are, and it considers all that space free to reuse.

(Deleting individual files is similar. FAT marks the directory entry as
deleted by changing its first byte to `0xE5` and frees its clusters. The data
stays where it is.)

## 3. What recording new footage does

When the camera saves a new file it asks the filesystem for free clusters.
FAT32 and exFAT implementations keep a "next free cluster" hint, and right
after a format that hint points at the **beginning** of the data area. So new
files are laid down front to back:

```
before the delete:  [ Jul | Oct | Dec shoot | Jan trip |  free  ]
after hiking:       [ HIKING FOOTAGE | ct | Dec shoot | Jan trip |  free  ]
                      ^^^^^^^^^^^^^^^^^^^^
                      overwritten: gone for good
```

Two things follow:

1. **How much was destroyed ≈ how much you recorded after the delete.** The
   hiking files you can see on the card today are roughly the size of the
   overwritten region. On a 64 GB card with 20 GB of hiking footage, about
   44 GB of the old data area was never touched.
2. **Where the December shoot sat decides its fate.** If the card filled up
   over time, newer material sits further from the start. December was among
   the last things written before the card filled, so it's likely in the
   further-in part that survives.

This is the usual behaviour, not a guarantee. Camera firmware chooses its own
allocation strategy, and a card that had files deleted and re-recorded over
the years gets patchy. That's why `cardscan` draws a map of *your* card
instead of assuming: it shows where the post-delete footage actually went and
what survived around it.

## 4. Finding files with no index: file carving

With the index gone, we read the data area raw and recognise files by their
contents. Three ideas make this work.

### 4a. Signatures, at sector boundaries

Most file formats start with fixed "magic" bytes:

| format | first bytes |
|---|---|
| JPEG | `FF D8 FF` |
| MP4 | 4 size bytes, then `ftyp` |

Random data contains `FF D8 FF` about once every 16 MB, so searching
everywhere gives false alarms. But a filesystem always starts a file at the
beginning of a cluster, and clusters are whole multiples of 512-byte sectors.
So a real file can only begin at an offset divisible by 512. Checking only
those offsets throws away 511 of every 512 false alarms and never misses a
real file. On 1.5 GB of random data, cardscan reports zero false positives.

### 4b. Following the file's own structure to find its end

Finding where a file *starts* is the easy part. To recover it you also need
where it *ends*, and the filesystem that knew the size is gone. So we let the
file tell us.

**JPEG** is a chain of segments. Each one is `FF <type> <2-byte length> <data>`:

```
FF D8                        start of image
FF E1 len "Exif" ...         camera model, date taken, AND a small thumbnail JPEG
FF DB len ...                quantisation tables
FF C0 len ...                width, height
FF C4 len ...                Huffman tables
FF DA len ...                start of scan, then the compressed pixels:
  ... 3F 9A FF 00 12 ...     (a real FF byte in the pixels is written as FF 00)
  ... FF D0 ...              (restart markers D0-D7 may also appear)
FF D9                        end of image
```

The naive approach is "search for the next `FF D9`", and it fails on GoPro
photos. Each one carries a thumbnail JPEG *inside* its EXIF block, with its
own `FF D9`, so the naive search stops a few KB into the file. cardscan
instead hops from segment to segment using the length fields, which jumps
straight over the thumbnail. Inside the compressed pixels it looks for the
first `FF` that isn't followed by `00` or `D0`-`D7`. Thanks to the `FF 00`
"stuffing" rule, only a real marker can look like that.

Along the way it reads the EXIF block for **DateTimeOriginal** (the moment
the shutter fired) and **Model** (`HERO5 Black`). This is how it can tell you
"84 photos from 2025-12-14" without opening any image.

**MP4** is a chain of boxes. Each one is `<4-byte size> <4-char type> <data>`:

```
ftyp   "I'm an MP4"
mdat   the frames (gigabytes)
moov   the index of the frames, the creation date, duration, resolution;
       GoPro also puts its firmware version here (HD5.xx = HERO5 Black)
```

Hopping by size from `ftyp` gives the exact end. The `moov` box is usually
written *last*, after recording stops, so a video can lose its tail and with
it the index. Without the index the frames are just bytes, which is why the
guide points to untrunc and a reference video for repair.

### 4c. Damaged photos

If a photo's start survived but its middle didn't, cardscan notices. Another
photo's `FF D8` may turn up where pixels should be, or the data may become a
blank region. It carves up to that point and appends an end marker, so most
viewers show the intact top of the picture with the rest grey.

Two extra details squeeze more out of a damaged photo:

- **The embedded preview.** The thumbnail inside EXIF lives in the *header*,
  which is the part that survived. It's a small but **complete** copy of the
  whole picture, so cardscan saves it alongside as `_THUMB.jpg`.
- **Restart markers as a splice detector.** Many encoders write a marker every
  N blocks, numbered RST0, RST1, … RST7, then back to RST0. A photo whose
  pixel data was swapped for another file's partway through usually breaks
  that sequence, or ends with the wrong count for its size. cardscan checks
  both. It flags the photo but still carves it in full, so a false alarm never
  costs data. On 100 real JPEGs (every colour layout, odd sizes, many restart
  intervals) it produced no false alarms, and it caught a deliberately spliced
  photo within one marker of the splice.

### 4d. Rebuilding a video whose beginning was overwritten

Section 3 implies something specific: new footage overwrites the card from
the start, so the old file sitting on the *edge* of the overwritten area loses
its **beginning** and keeps its **end**. For a photo, losing the header is
fatal. For a GoPro video it isn't, because the index (`moov`) is written at
the end, and it survives:

```
original:   [ftyp][mdat: frame 1, frame 2, ... frame 600][moov: index]
on card:    [## hiking footage ##][... frame 242 ... 600 ][moov: index]
```

The index lists every frame's size and its offset *from the start of the
original file*. From that, `cardscan/salvage.py`:

1. **Finds where the file started** (even though that spot is now hiking
   footage). The last frame ends where the index begins, so
   `start = index position - end of last frame`.
2. **Checks every frame in place.** An H.264 frame inside an MP4 is a chain of
   pieces (NAL units), each prefixed with its length. The lengths must add up
   *exactly* to the frame size the index records, and each piece's first bit
   must be 0. Overwritten data essentially never passes. Walking back from the
   end, the first frame that fails marks the edge of the overwritten area.
3. **Starts at the next keyframe.** Most frames only describe changes from
   earlier frames; a keyframe is a complete picture. Playback can only begin
   at one.
4. **Writes a new, playable MP4** with those frames, the audio from the same
   moment, and a fresh index (frame sizes, timings, keyframe list) describing
   just what survived. The original creation date and GoPro firmware tag are
   kept.

Tested on real H.264 video with its first 40% overwritten: cardscan found the
exact original start, identified frame 242 as the first intact frame, and
began at keyframe 255. That recovered 11.5 of 20 seconds with sound, and
ffmpeg decoded every frame without a single error. PhotoRec and similar
carvers can't do this, because they need a file's beginning.

## 5. Limits worth knowing

- **Overwritten means gone, as far as software can see.** Once new data is
  written to a sector, reading that sector returns the new data. There is one
  physical nuance. Flash memory can't overwrite in place, so the card's
  controller writes new data to fresh blocks and erases the old ones later,
  during "garbage collection". For a while, old copies can physically linger
  where only a lab that reads the memory chip directly could see them. The
  controller reclaims that space continuously as more is written, so after a
  trip's worth of footage little is likely to remain. What remains is most
  likely the data overwritten *last*, just before the edge of the new footage.
  That's why the guide calls this a long shot.
- **Fragmentation (files split into pieces).** Carving follows a file from its
  start, assuming its clusters are contiguous. Experiments with a real FAT32
  allocator show when that fails. The filesystem keeps a "next free cluster"
  pointer that only moves forward, so deleting files leaves holes that are
  *not* reused, and new files still go in one piece after the old ones. Only
  once the card fills to the end does the pointer wrap around and pour new
  files into the holes, splitting them. A card that was used until full, with
  some files deleted in the camera along the way, is exactly that case.

  On a card built that way (12 December photos, each split into 4 pieces),
  cardscan found all 12 but only their first piece, and PhotoRec's default
  settings recovered none. **PhotoRec with "Paranoid: brute force" recovered 11
  of 12 byte-for-byte.** It tries candidate pieces and checks whether the JPEG
  still decodes correctly across each join. That's why the guide runs both
  tools.
- **Videos and fragmentation.** The GoPro records the full-size `.MP4` and the
  low-res `.LRV` at the same time; depending on how the firmware allocates
  space, the two can end up interleaved on the card. `--deep` checks every
  frame of every intact-looking video and reports any that are damaged.
- **Erase commands.** SD cards support an ERASE command that makes the
  controller discard data. Ordinary camera formats don't appear to use it,
  but if one did, the old area reads back as blank. cardscan's map would show
  "nothing recognised" everywhere except the new files.
- **Camera clock.** Dates come from the camera's clock. If it was ever reset,
  the photos carry the wrong date, so look for a burst of photos on an
  unexpected day.

## 6. Why the safety steps matter

- **The lock switch.** Mounting a card lets the computer write small hidden
  files. On a FAT/exFAT card they go into the next free clusters, which is
  right after the newest files and exactly where surviving old data begins.
- **Image first.** Every tool then reads a copy. You can retry freely, try
  multiple tools, and a failing card only has to be read once.
- **Read-only code.** `cardscan` opens its input with `open(path, "rb")` and
  has no code path that writes to it. Its output always goes to a separate
  folder you choose.

## 7. The experiments behind the claims

`tools/simulate_card.py` runs the whole story on a real FAT32 filesystem
built with `mkfs.fat` and written with `mtools`, whose allocator, like a
camera's, fills from the start. With `--real-photos --real-videos` it uses
Pillow and ffmpeg so every file can be opened and watched.

1. Fill the card in time order: July → October → the December birthday shoot
   (a 3-chapter video, its low-res copy, and photos) → January trip, about 80% full.
2. Format it (new empty index, data untouched).
3. Record "hiking" footage on top, about 40% of the card. It ends partway
   through the December video.
4. Save a manifest with the SHA-256 hash of every file ever written.

Then scan with `--deep` and compare hashes:

| what | result | why |
|---|---|---|
| July, October photos | 0 recovered | under the new footage |
| December video, chapter 1 | lost | entirely under the new footage |
| December video, chapter 2 | **last 3.9 s of 10 s rebuilt**, plays cleanly | its beginning was under the edge; its index survived |
| December video, chapter 3 + low-res copy | byte-identical | beyond the overwritten area |
| **December photos** | **82 / 82 byte-identical** | beyond the overwritten area |
| January photos | 122 / 122 | beyond the overwritten area |
| hiking footage | 67 / 67 | the current files |

PhotoRec in brute-force mode, followed by `cardscan sort`, recovered the same
December photos and the two intact video files, but not the rebuilt chapter.
The fragmentation experiment is described in section 5.

`tests/test_simulated_card.py` and `tests/test_salvage.py` repeat these checks
on every test run, including the real-H.264 rebuild when ffmpeg is installed.

What this proves: the mechanisms work as described and the tools recover
files intact. What it can't prove: how *your* camera laid out *your* card.
Only scanning your card's image can show that.
