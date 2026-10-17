# fixformat - Raw Sector Signature Scanner

A read-only, fully offline recovery scanner for Windows (designed for Mini
Windows 10 / DLC Boot on a USB flash drive). After an accidental format or a
Windows reinstall, the old file data usually still exists on the disk. This
tool searches the disk **sector by sector, at the raw byte level**, for the
signatures of the file types you choose, and hands you the exact location and
a readable copy of everything it finds.

## What the program does

1. **Lists every disk** in the machine with its exact size and sector count,
   and asks which one to scan (physical disk, volume by letter, or a disk
   image file).
2. **Reads the target raw**, in large blocks (fast on real hardware), and
   searches **byte-by-byte inside the memory** for every configured
   signature. The reported **byte offset is exact** - a signature that starts
   in the middle of a block, or crosses a block boundary, is found and
   reported at its true first byte.
3. **Reports every hit** with the pattern name, the exact byte offset (hex and
   decimal) and the 512-byte sector number - enough to cross-check with any
   other carving tool.
4. **Carves a readable data window** around every hit (default 4 KB before,
   64 KB after), so the surrounding content can be read back as text or data.
5. **Writes everything as it goes**: each hit is appended to
   `search_results.txt` the moment it is found (the screen in a WinPE session
   is temporary; the log is not).
6. **Shows a recovery report** when the scan ends: every recovered item with
   its size, the totals (files + bytes), and the **candidate file names**
   found inside the recovered data.
7. **Asks where to save** the recovered data. Nothing is assumed about the
   destination - you type the folder after you have seen the names and sizes.

The scanner **never writes to the scanned device**.

## Repository layout

| Path | Purpose |
|------|---------|
| `v1/` | Release 1 - the scanner with interactive disk selection, multi-encoding search, carving, reporting and save prompt. |
| `v2/` | Release 2 - adds interactive file-extension selection, a case-sensitivity choice, `y/n` confirmations and step-by-step English guidance on screen. |
| `README.md` | This document. |
| `python/` | (not in git) the bundled embedded Python 3.12 runtime - see below. |

Both releases contain the same file set:

| File | Purpose |
|------|---------|
| `raw_scan.py` | The scanner. Configuration is in the block at the bottom of the file. |
| `list_drives.py` | Standalone disk listing with sizes (handy before a scan). |
| `selftest.py` | Self-test on a synthetic image - no disk access needed. |
| `run_scan.bat` | Double-click launcher; prefers the bundled Python. |

## Embedded Python - run it without installing anything

The tool is meant to run inside a temporary Mini Windows environment where
installing Python is pointless (the session is gone after reboot). The
solution is the official **embeddable Python package** - a self-contained
folder, no installation, no registry, no traces left behind.

1. Download `python-3.x.x-embed-amd64.zip` from
   `https://www.python.org/ftp/python/` (any 3.8+ embeddable build works).
2. Extract it into a folder named `python\` in the repository root (next to
   `v1/` and `v2/`), so `python\python.exe` exists.
3. `run_scan.bat` finds it automatically. No internet connection is needed
   afterwards - the scanner uses only the Python standard library.

## How a run works (v2 - recommended)

Run `v2\run_scan.bat`, or `python\python.exe raw_scan.py` from the `v2/`
folder. Every step is explained on screen in English:

- **STEP 1 - CHOOSE THE DISK.** All physical disks are listed with their exact
  size and sector count. Type a drive number (`0`), a drive letter (`D`), or
  an image file path.
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

Release 1 works the same way but has no Step 2/Step 3 questions (all
configured signatures are searched, with the `CASE_SENSITIVE` setting from
the config block) and its start confirmation is `Type YES`.

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

`recovered\` - the carved data windows (moved to the folder you choose at the
end).

## Configuration (bottom of `raw_scan.py`)

| Setting | Meaning |
|---------|---------|
| `TARGET` | Empty = interactive disk selection (recommended). Set to a raw device (`\\.\PhysicalDrive0`, `\\.\D:`) or an image file to skip Step 1. |
| `CHUNK_SIZE` | Read block size (default 8 MiB; must be a multiple of 4096 for raw devices). Large blocks = fast; the search inside them stays byte-exact thanks to the overlap window. |
| `MIN_GAP` | Suppress a new hit of the same pattern closer than this to the previous one (default 1 MiB). `0` reports every occurrence. |
| `CASE_SENSITIVE` | Default for the case question (v2) / the fixed setting (v1). |
| `MAGIC_SIGNATURES` | label -> hex bytes, for binary formats with fixed headers. |
| `TARGET_SIGNATURES` | label -> text or raw bytes. The `( .ext )` part of a label links it to its extension for the v2 menu. |
| `ENABLE_CARVING` / `CARVE_DIR` / `CARVE_BEFORE` / `CARVE_AFTER` / `CARVE_LIMIT` | The carved window around each hit (default 4 KB before, 64 KB after, max 100 carved files per pattern). |

## Safety notes

- The scanner is strictly read-only. It never writes to the scanned device.
- Do not save anything onto the damaged disk - every write there can overwrite
  the very data you want to recover. The save prompt warns about this.
- `MIN_GAP` and `CARVE_LIMIT` keep the flash drive from filling up when very
  generic markers are used.
- On a 510 GB disk a full pass takes roughly 35-60 minutes at normal
  spinning-disk speeds.

## Self-test

```
python\python.exe v2\selftest.py
```

Builds a synthetic image with patterns crossing block boundaries, ASCII and
Arabic patterns in UTF-8 and UTF-16LE, near-duplicate hits and a readable
file name; verifies exact offsets, the MIN_GAP filter, case sensitivity,
extension filtering, carved content, candidate-name extraction and the
report. No disk access, no admin rights and no internet needed.

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
