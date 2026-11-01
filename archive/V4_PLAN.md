# fixformat - Version 4 Plan (approved scope, not yet implemented)

Decisions recorded from the user on 2026-10-17. Nothing here is implemented
yet; this file is the backlog to execute when version 4 is built. Current
releases: v1.0.0 (scanner), v2.0.0 (extension/case selection), v3.0.0 (disk
imaging + image analysis).

## Decisions recorded (user, 2026-10-17)

| Item | Decision |
|------|----------|
| Imaging compression | **User chooses at imaging time** among five options (see item 1) |
| Never skip unallocated space | **Approved** - design rule, always on |
| Full MFT-based recovery | **Rejected - will not be done** |
| Organized output + browsable index | **Approved** |
| Ship embedded Python as GitHub Release asset | **Approved** |
| Carved-window triage (entropy + type) | **Approved** |
| Smart text carving | **Approved** |

## 1. Imaging compression options (user choice at imaging time)

Add a choice at imaging time (menu option 2):

| Option | Behavior |
|--------|----------|
| `1) Raw` | Default. Uncompressed `.img`, fastest, universal compatibility. |
| `2) NTFS Sparse` | Raw image written as an NTFS sparse file: all-zero sectors consume no disk space on the destination, the file still reads back as a complete image. Requires an NTFS destination - detect the destination file system automatically and warn (or refuse) on exFAT/FAT32. |
| `3) raw + gzip` | Stream-compressed image (stdlib `gzip`, low compression level for speed). |
| `4) raw + xz/lzma` | Stream-compressed image (stdlib `lzma`). Best ratio of the stdlib options, slowest. |
| `5) zstd` | External `zstd.exe` bundled beside the scripts - fastest and best ratio of all, but requires the extra binary. |

Expected real-world saving after a format + fresh Windows install is
modest-to-medium: the unallocated space holds intact deleted data (poorly
compressible), the new install is ~25 GB, and true zero regions compress
very well.

## 2. Never skip unallocated space (design rule - approved)

"Empty" must be distinguished carefully:

- **Unallocated space (file-system free space)** is where deleted file data
  lives after a format. It must NEVER be skipped, trimmed, or excluded from
  imaging or scanning - that is the recovery target itself. The NTFS `$Bitmap`
  must not be used to exclude regions.
- **All-zero sectors** carry no information and may be skipped or compressed
  losslessly (sparse / gzip / xz / zstd). Any zero-skip must be
  bit-transparent: the image must read back identical to the source.

## 3. Full NTFS MFT-based recovery - DROPPED

Rejected by the user for this project (and later, unless reopened). Kept
here for the record only.

## 4. Smart text carving (approved)

For text-format hits (.md/.py/.json/.yaml/.env): extend the carved window
beyond the fixed 64 KB after the hit - keep extending while the data looks
like readable text (printable UTF-8 / UTF-16, line structure), so recovered
text files are near-complete instead of fixed-size fragments.

## 5. Organized output + browsable index (approved)

- Write a clean readable `.txt` copy next to every text carved window.
- Produce an `index.html` or a sorted index listing: candidate file name,
  pattern, byte offset, sector, size - so the recovered set can be browsed
  instead of scrolled as a pile of files.

## 6. GitHub Release with the embedded runtime (approved)

Attach `python-embed.zip` (embeddable Python) to a GitHub Release so users
get the complete bootable-folder kit in one download. Third-party
redistributable - PSF license allows it.

## 7. Carved-window triage (approved - based on real recovered samples)

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

## Recommended execution order

- **Group A** (solves browsability of results): item 7 -> item 4 -> item 5
- **Group B**: item 1 (five-choice imaging compression)
- **Group C**: item 6 (Release asset - independent, cheap)
