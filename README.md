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
| `v5/` | Release 5 - fast scan (first 5% + last 5%), progressive 5% slices, resume, provenance, organized output, JSON signature profile. |
| `v6/` | Release 6 - pre-scan volume gate (BitLocker/FVE detection) + per-run SQLite run database. See `v6/docs_plan.md` and `v6/RUN_DB_GUIDE.md`. |
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
installing Python is pointless (the session is gone after reboot). This is not a
guess - v4 was run from a Mini Windows (WinPE) session with no changes and no
installed Python, so portability is a tested property, not an intention. Treat a
new dependency as a release blocker: stdlib only, the bundled `python/` folder as
shipped, no pip, no service, no registry writes.
The solution is the official **embeddable Python package** - a self-contained
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

## Release 5 - the new main menu

`v5/` rebuilds the same tool on top of release 4 and changes the menu. Run
`v5/run_scan.bat` (or the bundled interpreter: `python/python.exe v5/raw_scan.py`):

- **1 - FAST SCAN (quick triage).** Reads only the first 5% and the last 5% of
  the source, inside a hard 5-minute budget, and reports hit counts per
  signature, sample file names, the percentage of all-zero regions (the trace
  of a wiped, encrypted or TRIMmed SSD) and an estimate of what the whole disk
  holds. It saves no files: it decides whether a full scan is worth the hours.
- **2 - FULL SCAN (complete pass).** The whole source, carved.
- **3 - PROGRESSIVE SCAN (chunked, 5% at a time).** The same scan with a
  checkpoint after every 5%, so a long run can be stopped and continued.
- **4 - CREATE DISK IMAGE.** Unchanged from release 3/4.
- **5 - ANALYZE EXISTING IMAGE.** Unchanged, now picked from a numbered list.
- **6 - RESUME PREVIOUS RECOVERY.** Continues a `recovered_*` folder from the
  offset it stopped at, with its own signatures, and refuses one that already
  finished.

Every scan now prints a `PLAN FOR THIS RUN` block first: mode, how much will be
read, signature count and an estimated duration taken from the last run
measured on this machine (`scan_speed.json`), or a clearly-labelled default
when nothing has been measured yet.

**v5.0.0** - Release 5: a **fast scan** first (first and last 5% inside a
five-minute budget, saves no files) so you know whether a full pass is worth the
hours; **progressive 5% slices** with a checkpoint after each; **resume** (menu
option 6) that continues a `recovered_*` folder from its recorded offset. Every
run writes `recovered_<disk>_<size>_<time>/` with a `provenance.json` plus a
browsable `organized_*/` (`by_extension/`, `index.html`). Parallel phase 1
measured **3.0x** on a real 476.9 GiB image and **4.3x** end-to-end on the
synthetic fixture, and a JSON profile can replace the extension menu.

## Release 6 - the v6 bundle (read-only, add-on)

`v6/` is a single, coherent bundle of new features that change what a scan can
honestly report. One new source module backs each feature; every module is
stdlib-only, disjoint from the others, and exercised by self-tests:

| Area | New files | What it does |
|------|-----------|--------------|
| **DB / extraction** | `v6/run_db.py`, `v6/raw_scan.py` (Task 1 wiring) | Per-run SQLite `run.db` plus a pre-scan volume gate (`v6/volume_gate.py`) that classifies each partition as plaintext / mixed / encrypted / empty before any signature search. |
| **Plan screen / reports / audit** | `v6/plan_screen.py`, `v6/reports_integration.py`, `v6/audit_serialization.py` (delivered, standalone) | A recovery-plan screen rendered from the run DB, three report formats (`report.md` / `.csv` / `.html` plus `audit.txt`), all from public run_db APIs only. |
| **Validators / assembly** | `v6/validators.py`, `v6/assembly.py`, `v6/test_v6_steps67.py` | JPEG / PNG / PDF / ZIP / SQLite validation with actionable errors for malformed inputs; sequential + 2-fragment "best-guess continuation" (BGC) assembly returning fragments, gap bytes and confidence. |
| **Adaptive operations** | `v6/performance/operations.py`, `v6/performance/selftest_perf.py` | Adaptive operation selection (adaptive_parallel vs single_worker_skip), Shannon entropy, parallel penalty accounting, fast-scan calibration, and an opt-in Direct I/O path with a safe buffered fallback. |

### Volume gate (pre-scan)

Before any signature search, `volume_gate.probe_source()` reads the partition
table once and classifies every partition by its VBR OEM id at offset 3:
`NTFS    ` (plaintext NTFS), `-FVE-FS-` (BitLocker ciphertext), `MSDOS5.0`/`
exFAT (FAT, usually the EFI system partition), eight zero bytes (unformatted/MSR),
or anything else (unknown). `volume_gate.explain()` prints the verdict. Four
outcomes apply: **encrypted** (abort - nothing recoverable without the 48-digit
recovery password or a `.bek` file), **mixed** (skip only the encrypted
partitions), **plaintext** (full scan proceeds), and **empty** (report, no
scan). The gate is read-only, never guesses a key, never attempts decryption,
and never lets an encrypted volume produce candidates that look recoverable -
the 8,675 "hits" of v5.0.0 on the user's real image were BitLocker ciphertext,
not markup.

### Run database (per-run SQLite)

Each run now writes a per-run SQLite file (`run_db.py`, `run.db` next to
`provenance.json`), replacing the append-only text log. It records the run,
source and size, signature set, region map (zero/entropy), candidates,
fragments and signature counts. A `ranked` view answers "only validated files
over 100 KB?", "have I already recovered this file?" and "what changed since
the last run?" with one query; `summary()` and `dedupe_by_sha256()` cover the
report statistics. See `v6/RUN_DB_GUIDE.md` and the API reference in
`v6/API.md`.

### Plan screen, reports, audit

The plan screen renders a recovery plan from the run database
(`run_db.plan_extraction()` / `summary()`); the reports writer emits
`report.md` / `report.csv` / `report.html` (`run_db.write_report()`); and
`audit_serialization.py` writes `audit.txt` - a stable-field serialization of
ranked candidates (offset, length, ext, fragments, confidence, label, validated,
sha256). All three use the run_db public APIs only and stay disjoint from the
scan, validator and performance modules. See `v6/CLI_UX_SPEC.md` for the
display-contract mapping and `v6/RUN_DB_GUIDE.md` for the schema.

### Validators (JPEG / PNG / PDF / ZIP / SQLite)

Every recovered candidate is validated before it is carved or reported:
`validate(data, fmt) -> (validated, confidence, validator, fragments, gap_bytes,
error)`. A candidate is "recovered" only when a validator accepts its header
structure. Malformed inputs (truncated JPEG, missing PNG IHDR/IEND, PDF without
xref, ZIP without EOCD, SQLite without the magic header) get actionable error
messages. See `v6/VALIDATORS_GUIDE.md`.

### Fragmented assembly

`assemble(fragments) -> (assembled, fragments_n, gap_bytes, confidence, error)`
combines validated fragments: single fragment returns it as-is (confidence
0.95); two fragments fall back to "best-guess continuation" (BGC) over gaps
0..min(limit,256); three or more concatenate in order (confidence 0.82).
Returns `gap_bytes` and `confidence` so reports can say how much data was
filled and how certain the assembly is. See `v6/VALIDATORS_GUIDE.md`.

### Adaptive operations — detail (entropy / parallel penalty / calibration / Direct I/O)

The scanner adapts its operation to the data it samples, replacing a fixed
mode with content-driven selection. All functions live in
`v6/performance/operations.py` and are stdlib-only, deterministic, and
reproducible.

#### Adaptive operation selection — `select_op(sample_bytes) -> str`

`select_op()` picks the scan op per sample byte content:

| op | condition | meaning |
|----|-----------|---------|
| `zero_skip` | `len(sample_bytes) == 0` | nothing to scan |
| `adaptive_parallel` | zero fraction `< 0.3` | dense data → parallel workers |
| `single_worker_skip` | zero fraction `>= 0.3` | mostly-zero sample → one worker with zero-skip |

The threshold is a hard strict `< 0.3`, so exactly 0.30 (3 of 10 zero bytes)
falls into `single_worker_skip`. It is a pure function: the same sample
always yields the same op. Rationale: all-zero regions are cheap to skip and
slow all workers down together, so they are better handled by one worker;
dense data benefits from parallel workers.

#### Entropy — Shannon, bits/byte

`entropy(data: bytes) -> float` returns a reproducible Shannon entropy in
**0..8 bits/byte** (8 = uniform over 256 byte values).

- Empty input → `0.0`.
- It counts byte frequencies with a dict, then computes
  `-sum(p * log2(p))` over the distinct byte values using only `math.log2`
  and exact integer counts.
- Rounding: the caller rounds to a fixed precision; `calibrate()` rounds
  entropy to **4 decimal places**.

#### Parallel penalty accounting

Parallelism is a cost, not a free win, when regions are mostly zero.

- `parallel_penalty(workers: int, zero_frac: float) -> float` — returns a
  penalty multiplier (≥ 1.0): `1.0 + max(0.0, (workers - 1) * zero_frac * 0.25)`.
  Single worker always returns `1.0`; zero fraction `0.0` always returns
  `1.0` (no penalty for dense data). Example: 4 workers, 80% zeros →
  `1.0 + (4-1)*0.8*0.25 = 1.6`.

- `account_penalty(op_name: str, workers: int, zero_frac: float) -> dict` —
  per-operation, deterministic attribution into a **fresh dict** (no shared
  mutable state). Keys: `op` (str), `workers` (int), `zero_frac`
  (rounded 6 d.p.), `penalty_factor` (rounded 6 d.p.), `attribution` (string
  like `opA:w4:zf0.500000`).

- `aggregate_penalties(penalties: list) -> dict` — deterministic sorted
  aggregation over a list of penalty records. Returns: `penalty_total` (sum
  rounded 6 d.p.), `count` (int), `attribution` (`;`-joined sorted
  attribution strings, so input order does not affect the result).

The attribution string format is `op:wN:zf{6d}`; fields are fixed-width so
records sort lexicographically in a deterministic, human-readable order.

#### Fast-scan calibration — `calibrate(sample_path, workers) -> dict`

`calibrate()` reads a sample file once and returns a reproducible
calibration record, rounding **entropy**, **zero_frac**, and **penalty_factor**
to **4 decimal places**:

| key | type | meaning |
|-----|------|---------|
| `sample` | str | path passed in |
| `bytes` | int | file size |
| `entropy` | float | Shannon entropy, 4 d.p. |
| `op` | str | `select_op` verdict |
| `zero_frac` | float | zero fraction, 4 d.p. |
| `penalty_factor` | float | `parallel_penalty(workers, zero_frac)`, 4 d.p. |

Reproducibility guarantee: two calls with the same path and worker count
return **identical dicts** (`cal1 == cal2`), byte-for-byte across runs.

#### Direct I/O — configuration, fallback semantics

Direct I/O is **opt-in** and **512-byte-aligned only**.

- Configuration: `DIRECT_IO_ENABLED = os.environ.get("FIXFORMAT_DIRECT_IO", "0") == "1"`. Set to `1` to enable; any other value (including the default) disables it.
- Alignment: `DIRECT_IO_ALIGN = 512`. The first 512-byte read must be a multiple of 512 bytes; anything else forces a buffered fallback.

`open_direct(path, mode="rb")` returns a 3-tuple `(handle, used_direct, fallback_reason)`:

| `used_direct` | `fallback_reason` | meaning |
|---------------|-------------------|---------|
| `True` | `None` | direct-mapped open succeeded |
| `False` | `buffer_alignment_mismatch` | first 512-byte block not 512-aligned |
| `False` | `direct_io_error:<Type>` | `OSError`/`ValueError`/`AttributeError` on open |
| `False` | `direct_io_not_configured` | `DIRECT_IO_ENABLED` is off |
| `False` | `buffered_fallback` | text mode (`"r"`, no `"b"`) — never direct |

The handle is always a normal Python binary file object opened in `"rb"`
mode (`"b"` must be in `mode`). Direct I/O is read-only: it never writes to
a disk. The new `v6/performance/selftest_perf.py` selftest pins the
calibration determinism, and `v6/performance/test_penalty.py` pins the
penalty accounting determinism.

#### Reproducibility guarantees

- `select_op`, `entropy`, `parallel_penalty`, `account_penalty`,
  `aggregate_penalties` are pure functions: same inputs → same outputs,
  byte-for-byte, across runs.
- `calibrate()` is deterministic: identical dicts across repeated runs.
- `account_penalty()` returns a new `dict` per call; mutating one record
  never affects another (asserted in `test_returns_new_dict_no_shared_state`).
- All rounding is fixed-precision: entropy and `zero_frac` in `calibrate()`
  use 4 decimal places; per-record `penalty_factor` uses 6; aggregation
  rounds the total to 6.

`
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
- **v4.0.0** - Release 4: imaging compression choice (Raw / NTFS Sparse /
  gzip / xz / zstd), never-skip-unallocated-space design rule, smart text
  carving (extend while readable), carved-window triage (entropy + type:
  TEXT/BINARY/COMPRESSED/ZERO), organized output with browsable index.html,
  GitHub Release with embedded Python.
- **v5.0.0** - Release 5: fast scan first (first 5% + last 5% inside a hard
  5-minute budget, saves no files) so you know whether a full pass is worth
  the hours; progressive 5% slices with a checkpoint after each; resume
  (menu option 6) that continues a `recovered_*` folder from its recorded
  offset. Every run writes `recovered_<disk>_<size>_<time>/` with a
  `provenance.json` plus a browsable `organized_*/` (`by_extension/`,
  `index.html`). Parallel phase 1 measured **3.0x** on a real 476.9 GiB image
  and **4.3x** end-to-end on the synthetic fixture, and a JSON profile can
  replace the extension menu. See `v5/RELEASE_NOTES_v5.0.0.md` and
  `CHANGELOG.md` for details.

## Download the release

The v5.0.0 release assets are attached to the GitHub Release. Verify each file
with the bundled SHA-256 manifest (`SHA256SUMS.txt`):

| File | SHA-256 | Source |
|------|---------|--------|
| [v5.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/v5.zip) | `148ccc6e138152435ae33d5ae583a735c9ff7a1062d67de6a075ab18f4e4a969` | [v5.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/v5.zip) |
| [python-embed.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/python-embed.zip) | `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3` | [python-embed.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/python-embed.zip) |
| [SHA256SUMS.txt](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/SHA256SUMS.txt) | (see file for the full manifest) | [SHA256SUMS.txt](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/SHA256SUMS.txt) |
