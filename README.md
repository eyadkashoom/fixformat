# fixformat - Raw Sector Signature Scanner and Disk Imager

A read-only, fully offline recovery toolkit for Windows (designed for Mini
Windows 10 / DLC Boot on a USB flash drive). After an accidental format or a
Windows reinstall, the old file data usually still exists on the disk. This
toolkit finds it: it images a disk in one fast read, then searches the image
(or the live disk) **sector by sector, at the raw byte level**, for the
signatures of the file types you choose, and hands you the exact locations
and readable copies of everything it finds.

## What the toolkit does

**Disk imaging (release 3, menu option 2).** Copies every byte of a disk into
one `.img` file in a single fast pass. The disk hardware is read only once -
after that the image can be analyzed again and again without stressing the
original hardware. While copying it computes a SHA-256 checksum, verifies the
copied size against the source size, checks the destination free space first,
and writes a `.info.txt` sidecar with all the numbers. On failure or Ctrl+C
the partial file is renamed to `*.partial` so it is never mistaken for a
complete image.

**Signature search and file carving.** Reads a target (disk or image) in
large blocks and searches **byte-by-byte inside the memory** for every
configured signature. The reported **byte offset is exact** - a signature
that starts mid-block or crosses a block boundary is found and reported at
its true first byte. Every hit comes with the pattern name, the exact byte
offset (hex and decimal) and the 512-byte sector number.

**Recovery.** Every hit is carved out as a readable data window (default 4 KB
before, 64 KB after the hit) and logged to `search_results.txt` the moment it
is found. At the end a recovery report shows every recovered item with its
size, the totals (files + bytes), and the **candidate file names** found
inside the recovered data - then asks **where to save** it. Nothing is
assumed about the destination.

The toolkit **never writes to a disk it reads**.

## Repository layout

| Path | Purpose |
|------|---------|
| `v1/` | Release 1 - the scanner: interactive disk selection, multi-encoding search, carving, reporting, save prompt. |
| `v2/` | Release 2 - adds interactive file-extension selection, case-sensitivity choice, `y/n` confirmations, step-by-step English guidance. |
| `v3/` | Release 3 - adds the main menu: (1) search a disk, (2) create a full disk image, (3) analyze a disk image. |
| `README.md` | This document. |
| `python/` | (not in git) the bundled embedded Python 3.12 runtime - see below. |

All releases contain the same file set:

| File | Purpose |
|------|---------|
| `raw_scan.py` | The tool. Configuration is in the block at the bottom of the file. |
| `list_drives.py` | Standalone disk listing with sizes (handy before a scan). |
| `selftest.py` | Self-test on synthetic data - no disk access needed. |
| `run_scan.bat` | Double-click launcher; prefers the bundled Python. |

## Embedded Python - run it without installing anything

The tool is meant to run inside a temporary Mini Windows environment where
installing Python is pointless (the session is gone after reboot). The
solution is the official **embeddable Python package** - a self-contained
folder, no installation, no registry, no traces left behind.

1. Download `python-3.x.x-embed-amd64.zip` from
   `https://www.python.org/ftp/python/` (any 3.8+ embeddable build works).
2. Extract it into a folder named `python/` in the repository root (next to
   `v1/`, `v2/`, `v3/`), so `python/python.exe` exists.
3. `run_scan.bat` finds it automatically. No internet connection is needed
   afterwards - the tool uses only the Python standard library.

## The v3 main menu (recommended)

Run `v3/run_scan.bat`. Every step is explained on screen in English:

- **1 - SEARCH A DISK FOR FILE SIGNATURES.** Read a disk once and report
  every match right away (carving on the live disk).
- **2 - CREATE A FULL IMAGE OF A DISK (recommended first step).** Disk
  imaging: copy every byte of the disk into one `.img` file in a single fast
  pass. Free space is checked before writing (the image is as large as the
  disk), the copied size is verified against the source, a SHA-256 checksum
  is computed while copying, and a `.info.txt` sidecar records everything.
- **3 - ANALYZE AN EXISTING DISK IMAGE.** File carving on an image file
  (created by option 2, or any `.img`): choose the file types to search for,
  get the recovery report, and save the recovered data where you want.

**Best practice: run option 2 first, then option 3 on the created image.**
The hardware is read once; the image can be re-analyzed with different
signature sets, again and again, at full speed and zero risk to the original
disk.

## How a search runs (menu options 1 and 3)

- **STEP 1 - CHOOSE THE TARGET.** All physical disks are listed with their
  exact size and sector count (for option 3: type the image file path).
- **STEP 2 - CHOOSE WHICH FILE TYPES TO SEARCH FOR.** A numbered menu of the
  available extensions (`.md`, `.py`, `.json`, `.pdf`, ...). Type `all`, or
  the numbers you want separated by commas (`1,3,5`) - one extension or
  several.
- **STEP 3 - CHOOSE CASE SENSITIVITY.** `Y` = exact matching (recommended),
  `N` = ignore letter case (finds more, also more false hits).
- **STEP 4 - CONFIRM.** A summary of your choices, then `y/N` (`y`, `yes`,
  `Y`, `YES` all start; anything else stops safely).
- **STEP 5 - SCANNING.** The progress line shows data scanned/total
  (percent), sectors scanned/total, remaining data, ETA and hits found.
  Ctrl+C stops early and keeps everything found so far.
- **RESULTS.** The recovery report (names, sizes, totals, candidate file
  names), then the save-location prompt.

Release 2 works the same way without the menu (single search flow). Release 1
has no Step 2/Step 3 questions (all configured signatures are searched, with
the `CASE_SENSITIVE` setting from the config block) and its start
confirmation is `Type YES`.

## How text files are found (.md, .py, .json, .yaml, .env ...)

Text formats have **no magic bytes** - nothing in the bytes says "this is a
Markdown file". The tool therefore uses three kinds of text signatures:

1. **Content keywords** - `import `, `API_KEY=*** `agent:` ...
2. **File-extension strings** (`.md`, `.py`, `.codex`, `/codex`, ...). These
   also occur inside **NTFS MFT records**, where the file system stores file
   names - a `.md` hit in the MFT region usually reveals the real file names
   in the carved window, even when the file content itself is overwritten.
3. **File names** - `PROJECT_SPEC`, `DECISIONS`, `WORK_PLAN`, ... (add your
   own in the config block).

Every text signature is searched in **UTF-8, UTF-16LE and UTF-16BE**, so
non-ASCII content (for example Arabic words) is found whatever encoding the
file was saved with. Binary formats use their **magic bytes** (`%PDF-`,
`\x89PNG`, `PK\x03\x04`, ...).

## Output files

`search_results.txt` - one line per hit, appended and flushed immediately:

```
[2026-10-17 10:00:00] HIT | Extension .md [utf-8] | offset=0x00000012AB34CD00 (dec 503316480) | sector=983040 | carved=...
```

`recovery_report.txt` - the full report: per-pattern hit counts, every
recovered item with its size, recovered totals, and the candidate file names.

`recovered/` - the carved data windows (moved to the folder you choose at the
end).

`diskimage_*.img` + `diskimage_*.img.info.txt` - the disk image (option 2)
and its sidecar: source, sizes, sectors, SHA-256 checksum, status.

## Configuration (bottom of `raw_scan.py`)

| Setting | Meaning |
|---------|---------|
| `TARGET` | Empty = interactive selection (recommended). Set to a raw device (`\\.\PhysicalDrive0`, `\\.\D:`) or an image file to skip Step 1. |
| `CHUNK_SIZE` | Read block size for searching (default 8 MiB; must be a multiple of 4096 for raw devices). Large blocks = fast; the search inside them stays byte-exact thanks to the overlap window. |
| `IMAGE_CHUNK_SIZE` | Read block size for imaging (default 8 MiB) - one pass, maximum throughput. |
| `MIN_GAP` | Suppress a new hit of the same pattern closer than this to the previous one (default 1 MiB). `0` reports every occurrence. |
| `CASE_SENSITIVE` | Default for the case question (v2/v3) / the fixed setting (v1). |
| `MAGIC_SIGNATURES` | label -> hex bytes, for binary formats with fixed headers. |
| `TARGET_SIGNATURES` | label -> text or raw bytes. The `( .ext )` part of a label links it to its extension for the menu. |
| `ENABLE_CARVING` / `CARVE_DIR` / `CARVE_BEFORE` / `CARVE_AFTER` / `CARVE_LIMIT` | The carved window around each hit (default 4 KB before, 64 KB after, max 100 carved files per pattern). |

## Safety notes

- The tool is strictly read-only towards every disk it reads. It never writes
  to a scanned disk or to an image being analyzed.
- Do not save anything onto the damaged disk - every write there can overwrite
  the very data you want to recover. Both save prompts warn about this.
- The image file is as large as the disk itself: the imaging step checks the
  destination free space first and refuses to start when there is not enough.
- `MIN_GAP` and `CARVE_LIMIT` keep the flash drive from filling up when very
  generic markers are used.
- On a 510 GB disk a full pass (imaging or scanning) takes roughly 35-60
  minutes at normal spinning-disk speeds.

## Self-test

```
python/python.exe v3/selftest.py
```

Builds synthetic data with patterns crossing block boundaries, ASCII and
Arabic patterns in UTF-8 and UTF-16LE, near-duplicate hits and a readable
file name; verifies exact offsets, the MIN_GAP filter, case sensitivity,
extension filtering, carved content, candidate-name extraction and the
report - and verifies the disk-imaging round-trip (byte-identical copy,
SHA-256 match, `.info.txt` sidecar). No disk access, no admin rights and no
internet needed.

## Version history

- **v1.0.0** - Release 1: interactive disk selection with size/sector
  listing, multi-encoding (UTF-8 / UTF-16LE / UTF-16BE) byte-level search
  with exact offsets, boundary-crossing detection, MIN_GAP filtering, carved
  data windows, immediate logging, recovery report with candidate file names,
  and an interactive save-location prompt. Confirmation word: `YES`.
- **v2.0.0** - Release 2: adds interactive file-extension selection (one or
  many), an interactive case-sensitivity choice (Y/N), flexible `y/yes`
  confirmations in any letter case, and step-by-step English guidance on
  screen for every stage of the run.
- **v3.0.0** - Release 3: adds the main menu with three operations: search a
  disk for file signatures, **create a full disk image** (disk imaging: one
  fast read, free-space pre-check, SHA-256 checksum, size verification,
  `.info.txt` sidecar, `*.partial` safety rename on failure), and **analyze a
  disk image** (file carving on the image). Recommended workflow: image
  first, then analyze the image repeatedly without touching the hardware.
