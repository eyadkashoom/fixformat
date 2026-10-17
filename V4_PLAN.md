# fixformat - Version 4 Plan (pending proposals)

Collected proposals for release 4. Nothing here is implemented yet; this file
is the backlog to execute when version 4 is started. Current releases:
v1.0.0 (scanner), v2.0.0 (extension/case selection), v3.0.0 (disk imaging +
image analysis).

## 1. Imaging compression options (decision: approved in principle)

Add a choice at imaging time (menu option 2):

| Option | Behavior |
|--------|----------|
| `1) Raw` | Default. Uncompressed `.img`, fastest, universal compatibility. |
| `2) NTFS Sparse` | Recommended. Raw image written as an NTFS sparse file: all-zero sectors consume no disk space on the destination, the file still reads back as a complete image. Requires an NTFS destination - detect the destination file system automatically and warn (or refuse) on exFAT/FAT32. |
| `3) gzip` | Stream-compressed image (stdlib `gzip`, low compression level for speed). Fallback for non-NTFS destinations. Requires a decompression step before analysis. |

Expected real-world saving after a format + fresh Windows install is
modest-to-medium: the unallocated space holds intact deleted data (poorly
compressible), the new install is ~25 GB, and true zero regions compress
very well.

## 2. Never skip unallocated space (design rule)

"Empty" must be distinguished carefully:

- **Unallocated space (file-system free space)** is where deleted file data
  lives after a format. It must NEVER be skipped, trimmed, or excluded from
  imaging or scanning - that is the recovery target itself. The NTFS `$Bitmap`
  must not be used to exclude regions.
- **All-zero sectors** carry no information and may be skipped or compressed
  losslessly (sparse / gzip). Any zero-skip must be bit-transparent: the
  image must read back identical to the source.

## 3. Full NTFS MFT-based recovery (the headline v4 feature)

Turn the tool from a signature detector into a real file recoverer:

- Parse `$MFT` records from the image/disk (both allocated and leftover
  records in unallocated space).
- Extract: real file name, size, timestamps, and the `$DATA` data runs
  (cluster extents).
- Reassemble complete files and save them under their original names.
- Works best when MFT records survive (typical on HDD after a format). On
  SSD with TRIM the free space may already be zeroed - the tool should detect
  and report this condition (scan reveals hits only from the new install).

## 4. Smart text carving

For text-format hits (.md/.py/.json/.yaml/.env): extend the carved window
beyond the fixed 64 KB after the hit - keep extending while the data looks
like readable text (printable UTF-8 / UTF-16, line structure), so recovered
text files are near-complete instead of fixed-size fragments.

## 5. Organized output + browsable index

- Write a clean readable `.txt` copy next to every text carved window.
- Produce an `index.html` or a sorted index listing: candidate file name,
  pattern, byte offset, sector, size - so the recovered set can be browsed
  instead of scrolled as a pile of files.

## 6. GitHub Release with the embedded runtime

Attach `python-embed.zip` (embeddable Python) to a GitHub Release on the
latest tag so users get the complete bootable-folder kit in one download.
(Pending user decision - third-party redistributable, PSF license allows it.)

## 7. Carved-window triage (new - based on real recovered samples)

Recovered samples show that many carved windows are high-entropy binary
noise (measured: entropy ~6.3 bits/byte, 0.3% printable characters, no
magic bytes, no zero runs) - remnants of compressed/encrypted data, not
readable files. Every carved window should be classified automatically:

- `TEXT (readable)` - high printable ratio -> extract and save readable runs
- `BINARY (structured)` - low entropy with structure (FS remnants, runlists)
- `COMPRESSED/ENCRYPTED` - entropy > ~6.5 -> not browsable, likely dead
- `ZERO (empty)` - all zeros -> never written or TRIMed

Report the classification per window and keep only the TEXT ones as
browsable output. This turns thousands of garbage files into a filtered
shortlist.

## Decisions still needed from the user

- Approve the three-way imaging choice (Raw / NTFS Sparse / gzip)?
- Is the affected disk HDD or SSD (sets recovery expectations)?
- Ship the embedded Python as a Release asset (proposal 6)?
