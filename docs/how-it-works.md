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

## 2. What "delete all" / "format" really does

A camera's format rewrites **only the index**: a fresh boot sector, an empty
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

### 4c. Damaged files

If a photo's start survived but something overwrote its middle, cardscan
detects it: another photo's `FF D8` turns up where pixels should be, or the
data turns into a blank region. It carves up to that point and appends an
end marker. Most viewers then show the intact top part of the picture with
the rest grey. A partial photo of Tinkerbell is still worth having.

## 5. Limits worth knowing

- **Overwritten means gone.** Once new data is written to a sector, the old
  bytes no longer exist on the card. No software or lab can bring them back.
- **Fragmentation.** Carving assumes a file's clusters are contiguous. Photos
  usually are, since each is written in one go into fresh space. Videos are
  riskier: the GoPro records the full-size `.MP4` and the low-res `.LRV` at
  the same time, and depending on how the firmware allocates space, the two
  can end up interleaved. PhotoRec's "Paranoid: brute force" option can
  reassemble some fragmented JPEGs; fragmented video is much harder.
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

## 7. The experiment behind the claims

`tools/simulate_card.py` runs the whole story on a real FAT32 filesystem
built with `mkfs.fat` and written with `mtools`, whose allocator, like a
camera's, fills from the start:

1. Fill the card in time order: July → October → December birthday shoot →
   January trip, about 80% full.
2. Quick-format it (new empty index, data untouched).
3. Record "hiking" footage on top, about 35% of the card.
4. Save a manifest with the SHA-256 hash of every file ever written.

Then scan and compare hashes:

| session | recovered byte-identical | why |
|---|---|---|
| July | 0 / 204 | first on the card, so first overwritten |
| October | 58 / 102 | straddled the edge of the hiking footage |
| **December shoot** | **85 / 85** | beyond the overwritten region |
| January trip | 122 / 122 | beyond the overwritten region |
| hiking | 65 / 65 | the current files |

PhotoRec on the same image, followed by `cardscan sort`, returned all 82
December photos and both December videos, every one byte-identical. (The 85th
file is a tiny undated video thumbnail that PhotoRec skipped.) `tests/test_simulated_card.py` repeats this check on every
test run.

What this proves: the mechanism works exactly as described, and the tool
recovers files intact. What it can't prove: how *your* camera laid out *your*
card. Only scanning your card's image can show that.
