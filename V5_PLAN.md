# fixformat - Version 5 Plan (draft for review)

Collected proposals for release 5. Nothing here is implemented yet; this file
is the working draft to review with the user before any code is written.
Current releases: v1.0.0 (scanner), v2.0.0 (extension/case selection),
v3.0.0 (disk imaging + image analysis), v4.0.0 (imaging compression, MFT
recovery, smart carving, organized output).

## 1. Provenance-tagged recovered folder

Every recovery run writes into a folder whose name identifies the source
operation, so results from different runs are never mixed or overwritten.

**Naming scheme:**

```
recovered_<disk_label>_<size>_<timestamp>
```

| Part | Source | Example |
|------|--------|---------|
| `disk_label` | Physical drive number or image file base name | `PhysicalDrive1` or `diskimage_20261017` |
| `size` | Human-readable source size | `500GB`, `1TB` |
| `timestamp` | Local time at run start | `20261017_085359` |

**Example:** `recovered_PhysicalDrive1_500GB_20261017_085359`

The folder contains the raw carved data exactly as extracted from the source,
plus a `provenance.json` sidecar recording: source path, source size, SHA-256
of the source (if imaging was used), scan parameters, and the timestamp.

## 2. Organized browsable output folder

A separate folder (sibling to `recovered_...`) holds the organized results:

```
organized_<disk_label>_<size>_<timestamp>/
  by_extension/
    md/
    py/
    json/
    pdf/
    ...
  by_date/           (if MFT timestamps are available)
  index.html         (browsable table: name, type, size, offset, preview)
  summary.txt        (human-readable report)
```

The `index.html` is a self-contained page (no external dependencies) that
lists every recovered item with a clickable preview for text files.

## 3. Resume a previous recovery

At startup the tool asks:

```
A previous recovery folder was found: recovered_PhysicalDrive1_500GB_20261017_085359
Do you want to continue that recovery? [Y/n]
```

- **Yes** → the tool lists all `recovered_*` folders found (in the current
  directory and in a configurable search path) and lets the user pick one.
  The scan resumes from the last recorded offset (stored in
  `provenance.json`).
- **No** → the tool asks: "Is the recovery folder in a different location?"
  - **Yes** → the user types or browses to the folder path.
  - **No** → start a fresh recovery.

## 4. Progressive (chunked) scanning

Instead of scanning the entire disk before producing any output, the scan
proceeds in percentage-based chunks:

```
[1/20] Scanning 5% (0 - 25 GB) ... 142 hits, 87 files carved
        -> organized output written, temporary data cleaned
[2/20] Scanning 5% (25 - 50 GB) ... 98 hits, 54 files carved
        -> organized output written, temporary data cleaned
...
```

**Behavior:**
- After each chunk, the carved files are moved to the organized folder and
  the raw temporary data is deleted (only the organized output is kept).
- The user can stop after any chunk and keep what has been recovered so far.
- The scan resumes from the exact byte offset where the previous chunk
  ended (stored in `provenance.json`).
- A full scan is still available as a single-pass option for smaller disks.

**Rationale:** on a large disk the user sees usable recovered files within
minutes instead of waiting hours for a full pass.

## 5. Fast disk scan (criteria - approved)

The fast scan is a quick triage pass that searches a small, strategic portion
of the disk to estimate recovery potential before committing to a full scan.

### Proposed criteria

| Criterion | Value | Rationale |
|-----------|-------|-----------|
| **Coverage** | First 5% of the disk + last 5% of the disk | The beginning of the disk holds the partition table, boot sector, and often the MFT; the end often holds backup structures and recently deleted data. |
| **Signature set** | All configured signatures (same as full scan) | Keeps the fast scan representative. |
| **Carve window** | Reduced: 2 KB before, 16 KB after (vs. 4 KB / 64 KB) | Smaller windows are faster and sufficient for triage. |
| **Time budget** | Hard stop after 5 minutes | If the scan exceeds 5 minutes it reports partial results and suggests a full scan. |
| **Output** | Summary only: hit count per signature type, sample file names, estimated total recoverable size | No carved files are saved; the goal is a quick assessment. |
| **Decision gate** | If hits > threshold (default: 50), recommend a full scan; if hits = 0, warn that the disk may be wiped/encrypted/SSD-TRIMed. | Gives the user an actionable recommendation. |

### Approved decisions

1. Coverage: 5% + 5% (first and last) - approved.
2. Time budget: 5 minutes hard stop - approved.
3. SSD TRIM detection: yes, report all-zero regions explicitly - approved.
4. Menu placement: standalone menu option (see Section 9) - approved.

## 6. Network streaming / remote target (analysis)

**User proposal:** Add `--target-ip` and `--agent-mode` flags so the tool
streams disk data directly to a remote analysis machine without writing
anything to the formatted disk.

**Analysis:**

| Aspect | Assessment |
|--------|------------|
| **Technical feasibility** | Feasible with Python sockets (TCP). The tool reads sectors and streams them to a remote process. |
| **Performance** | Network (1 Gbps = ~125 MB/s) is slower than local disk (~150-200 MB/s). On a 500 GB disk this adds significant time. |
| **Reliability** | A dropped connection mid-scan loses data. Requires resume logic and retransmission. |
| **Complexity** | High: needs a server component, client component, protocol design, error handling. |
| **Practical value** | Moderate: the same goal (not writing to the source disk) is achieved more simply by imaging to an external USB drive. |

**Recommendation:** Defer to a future version. The simpler alternative --
imaging to an external USB drive -- achieves the same safety goal with far
less complexity and better reliability. If network streaming is added later,
it should be a separate advanced mode, not the default path.

## 7. Custom signature filter (analysis)

**User proposal:** Allow passing a profile file containing only the magic
bytes of the target program, so the scan ignores irrelevant sectors.

**Analysis:**

| Aspect | Assessment |
|--------|------------|
| **Technical feasibility** | Simple: load a JSON/YAML profile with a list of signatures, use only those. |
| **Performance** | Positive impact: fewer signatures = faster scan, fewer false positives. |
| **Complexity** | Low: a config loader and a filter on the signature list. |
| **Practical value** | High: users recovering specific file types (e.g., only `.dwg` or only `.psd`) get cleaner results. |

**Recommendation:** Approve. This is a straightforward, high-value addition.
The profile format should be simple JSON:

```json
{
  "name": "AutoCAD-only recovery",
  "signatures": {
    "dwg": ["AC10", "AC11", "AC12", "AC13"]
  }
}
```

## 8. Save outputs to SMB / network share (analysis)

**User proposal:** Direct recovery outputs to a network share
(`\\YourPC\RecoveredData`) so nothing is written to the formatted disk.

**Analysis:**

| Aspect | Assessment |
|--------|------------|
| **Technical feasibility** | Feasible on Windows via UNC paths or `net use`. Python can write to UNC paths directly if the process has permissions. |
| **Performance** | Network write is slower than local disk, but acceptable for recovered files (typically small). |
| **Reliability** | Network interruptions can corrupt output. Requires write verification and retry logic. |
| **Complexity** | Moderate: needs path validation, permission checks, and error handling. |
| **Practical value** | Moderate: useful when no external drive is available, but an external USB drive is simpler and more reliable. |

**Recommendation:** Approve as an advanced option. The tool should:
1. Validate the network path is reachable before starting.
2. Warn if the path is on the same physical disk as the source.
3. Fall back to a local path if the network write fails.

## 9. Fast scan as a standalone option

**User proposal:** The fast scan should be a separate menu option.

**Analysis:** Clear improvement. The main menu becomes:

```
1 - FAST SCAN (quick triage)
2 - FULL SCAN (complete pass)
3 - PROGRESSIVE SCAN (chunked, 5% at a time)
4 - CREATE DISK IMAGE
5 - ANALYZE EXISTING IMAGE
6 - RESUME PREVIOUS RECOVERY
```

**Recommendation:** Approve.

## 10. Pre-scan confirmation

**User proposal:** Ask the user before starting the full scan.

**Analysis:** Already exists in the tool (Step 4 confirmation). Can be
improved by showing a summary of the chosen mode and estimated time.

**Recommendation:** Already covered; minor UX improvement only.

## 11. Numbered picker for choosing an image / file (no typing)

**User proposal:** when a scan targets an existing `.img`, the tool currently
demands a typed path ("folder or full file path"). Replace (or rather,
supplement) that with a numbered list the user picks from.

**Design:**

```
STEP 1 of 5 - CHOOSE THE DISK IMAGE TO ANALYZE
================================================================================
  [1] D:\backup\disk0_20261020.img              476.94 GB   2026-10-20 14:03
  [2] D:\backup\disk1_20261020.img              114.61 GB   2026-10-20 15:11
  [3] E:\images\old.img.partial                 incomplete - not a full image
  ------------------------------------------------------------------------------
  [0] Type a path manually (network / unusual location)
  [R] Rescan        [F] Change folder        [Q] Quit
Your choice [1]:
```

Rules:

| Rule | Detail |
|------|--------|
| **Search roots** (in order) | script dir → current dir → last-used dir → root of every logical volume. Depth limited (default 2 levels) so a 500 GB disk is not walked. |
| **Extensions** | `.img`, `.raw`, `.dd`, `.001`, `.img.gz`, `.img.xz`, `.img.zst` |
| **Info shown** | size, last-modified time, and the filesystem it sits on |
| **Completeness** | a `*.partial` file is listed but marked incomplete; choosing it asks for explicit confirmation |
| **Sidecar** | if `<image>.info.txt` exists its SHA-256 / mode / source is shown in the same row group |
| **Pagination** | 15 entries per page, `N` next / `P` previous (a disk can hold dozens of images) |
| **Auto-select** | exactly one candidate found → selected automatically with a "press Enter to confirm" prompt |
| **Manual entry** | always kept as option `0` - UNC and network paths can never be enumerated reliably |
| **Remembered** | the last used folder is stored in `last_paths.json` next to the script and offered first next run |

## 12. Numbered picker for the destination (imaging + recovered output)

**User proposal:** when imaging to an external disk the tool also demands a
typed folder. It must offer a numbered list instead.

**Design - step A, choose the volume:**

```
STEP 3 of 5 - CHOOSE WHERE TO SAVE THE IMAGE
================================================================================
  [1] D:\                        261.98 GB total,  122.94 GB free, NTFS
  [2] E:\                        114.60 GB total,   80.86 GB free, exFAT
  [3] F:\                        (external) 931.51 GB total, 842.10 GB free, NTFS
  [4] \\NAS\Recovered            412.30 GB free, NTFS (network)
  ------------------------------------------------------------------------------
  [0] Type a path manually        [R] Rescan volumes        [Q] Quit
```

**Design - step B, walk the folder tree by number:**

```
  Current folder: F:\
  ------------------------------------------------------------------------------
  [1] ..  (up one level)
  [2] images\
  [3] backup_2026\
  [4] recovered\
  ------------------------------------------------------------------------------
  [S] Select this folder        [M] Make a new folder        [Q] Back
Your choice [S]:
```

Rules:

| Rule | Detail |
|------|--------|
| **Free-space gate** | refuse a folder with less than 102% of the source size (already implemented in v4, keep it) |
| **Same-disk warning (new)** | resolve the chosen folder to its physical disk; if it is the same physical disk as the source, refuse by default and allow only an explicit override - writing to the disk being imaged destroys the very data being recovered and halves the throughput |
| **Filesystem check** | NTFS Sparse mode is offered only on NTFS; exFAT/FAT32 gets a warning and falls back to raw/gzip |
| **Empty-folder bias** | folders that already contain an image are marked `(already has an image)` |
| **Network paths** | shown with their free space when reachable; a share that is not reachable is listed greyed out with `(unreachable)` and cannot be selected |
| **Remembered** | last used destination is stored in `last_paths.json` |
| **One shared function** | the same picker serves the image source (Section 11), the imaging destination, the `recovered_*` output root (Section 1), the organized output root (Section 2), and the SMB path (Section 8) - one implementation, no drift |

Nothing that exists today is removed: typing a path stays available everywhere
as option `0`, so a path that cannot be enumerated is never a dead end.

## 13. Making the image scan and the recovery much faster

**User complaint:** analyzing an `.img` is very slow - the scan and the
recovery both. This section is the performance plan, grounded in what the v4
code actually does.

### What is slow today (checked against the v4 source)

| # | Cause | Where in v4 | Cost |
|---|-------|-------------|------|
| 1 | Every pattern scans the whole block separately | `for label, pat, ... in pat_list` -> `hay.find(needle)` (lines 588-616) | N full passes over 8 MiB per block; 40 patterns = 40 passes |
| 2 | A lowercase copy of every block is built when any case-insensitive pattern exists | `combined.lower()` (line 584) | an extra 8 MiB allocation + copy per block |
| 3 | Carving happens **inside** the read loop and seeks back into the source | `carve_window()` (line 601) | each hit breaks the sequential stream; on a raw device that is a physical back-seek |
| 4 | Every hit is printed to the console and flushed to disk | `print()` + `log()` + `flush()` (lines 614-615) | Windows console I/O is milliseconds per line; thousands of hits stall the read loop |
| 5 | All-zero regions are matched like real data | no zero check | wasted CPU on the majority of a freshly formatted disk |

### Plan

**13.1 Two-phase recovery (index, then carve)** - implemented.
Phase 1 records only `(offset, label, ext, plen)` and never touches the source
again; phase 2 sorts those offsets and walks the source **forward once** in
4 MB blocks (`CachedReader`), serving every window from the block in memory.
Thousands of random back-seeks become one sequential pass. It also lets the
user review the hit index before committing disk space to extracted files.

Measured on the RAM-cached fixture: **no gain by itself** (43.2 s with it vs
40.5 s without - inside the noise), because a page-cached file makes a back-seek
as cheap as a forward read. It is kept for three reasons: it removes the seeks
that cost real time on a physical disk, it is the prerequisite for progressive
scanning and resume (Sections 3 and 4), and it made the triage fix below
possible by separating matching from writing.

**13.2 One C-level pass per block - MEASURED AND REJECTED**
Three candidate matchers were built and benchmarked on one 8 MB block of the
fixture with the 117-pattern default set:

| Matcher | Time for one 8 MB block | Verdict |
|---------|------------------------|---------|
| plain `bytes.find()` per pattern (v4 behaviour) | **1.3 s** | fastest - kept |
| single `re` alternation of all 117 needles | 5.4 s | 4x slower, rejected |
| anchor-byte-class sieve + `startswith` verification | 6.7 s | 5x slower, rejected |

Reason: `bytes.find()` is a C-level search with a skip table, while the
"clever" matchers push one Python-level iteration per candidate byte position
(7.5 million of them on a zero-heavy block, because UTF-16-BE patterns begin
with `0x00`). The lesson is recorded here so no later session re-implements
the slow variants. The real wins on data-dense blocks are 13.5 (parallel
matching) and 13.7 (far fewer signatures), not a fancier matcher.

**13.3 Zero-region fast skip + TRIM reporting**
Compare each block against a cached zero block (C-level `memcmp`); equal ->
skip matching entirely, count it, and report zero runs as SSD-TRIM / wiped
evidence (this is the detection approved in Section 5, implemented once and
reused by every mode).

**13.4 Never carve inside the read path** - implemented.
Phase 2 queues the work instead of doing it mid-read, log lines are buffered and
flushed once per block, and a hit line no longer prints with console I/O inside
the read loop. Carving a text window used to cost ~256 tiny seek+read calls per
hit; through `CachedReader` they are memory slices.

**13.4b Triage in C instead of a Python loop** - implemented, measured 1.2x.
`printable_ratio()` walked a 1 MB window byte by byte in Python (~70 ms per
carved file) and `is_all_zeros()` allocated a 1 MB zero buffer per call. Both
are now single C-level operations (`bytes.translate(None, table)` and
`bytes.count`). Triage cost fell from 11.2 s to 2.9 s on the fixture, with an
identical classification for every window (20 random buffers checked).

**13.5 Parallel matching** - implemented, measured 2.0x on top of everything else.
Phase 1 splits the source into N byte ranges and scans them in **processes**
(bytes `find` holds the GIL). Each worker opens its own handle, skips zero
blocks the same way, and reports only hits that START inside its own range;
the parent merges, applies MIN_GAP over the offset-ordered list (exactly what
one sequential pass did) and hands the result to phase 2. Workers run through
`multiprocessing` with the `spawn` context, and any pool failure falls back to
a single process instead of losing the scan.

The bug the equality test caught (worth keeping in mind for any future ranged
scan): a worker that stops reading exactly at its `end` **loses every pattern
that straddles the boundary**, because only the *next* block exposes it through
the carry. Workers now read `end + max_pattern_len` bytes and still report only
inside `[start, end)`. The selftest asserts parallel hits == sequential hits on
a fixture whose hits sit across the range boundaries.

`PARALLEL_WORKERS = None` means auto (up to 4); `1` forces one process (set it
for a slow spinning disk if 4 streams measure slower). `PARALLEL_MIN_SIZE`
keeps small sources sequential.

**13.6 Image-specific path** (the exact complaint)
- `mmap` the image instead of `read()`: no copy, and the OS page cache keeps
  it hot, so a second and third scan of the same image are far faster.
- Open with sequential-scan intent and prefetch (`FILE_FLAG_SEQUENTIAL_SCAN`
  on Windows, `MADV_SEQUENTIAL`/`WILLNEED` where available).
- Allow 32-64 MiB blocks for images (the 4 KiB alignment rule only applies to
  raw devices).
- Honest expectation: on USB 3 / SATA an image scans at disk speed
  (~150-250 MB/s, 500 GB is about 35-60 min); on a USB 2.0 stick (~30 MB/s)
  the same scan is about 4.5 hours regardless of code. The tool must print the
  measured source throughput at startup and warn when it is below ~60 MB/s,
  suggesting the image be copied to an internal disk first.

**13.7 Fewer, better signatures**
Bare extension patterns (`.md`, `.py`) match every text region and flood both
the hit list and the carving stage. Rule: extension matching only in
boundary mode (the extension preceded by a valid filename character), text
markers must be at least 8 characters, and the active signature set is
whatever the JSON profile (Section 7) selects. Fewer hits = dramatically less
carving.

First measured step, already implemented: UTF-16-BE expansion is now opt-in
(`SEARCH_UTF16_BE = False`). Windows writes UTF-16-LE, so the BE patterns only
added cost - every one of them begins with `0x00`, the most common byte on a
formatted disk, and they accounted for ~25 ms of every 8 MB block each. The
default set dropped from 117 to 81 patterns with no loss of reachable content.

**13.8 Measured acceptance criteria** (not claims)
Build a 2 GB synthetic image (70% zeros, 30% random, planted hits) and record
MB/s and seconds for: v4 baseline -> after 13.1 -> after 13.2/13.3 -> after
13.5. Target: **at least 3x** wall-clock improvement on the image scan and
**at least 10x** on the recovery/extraction stage, with identical hit counts
and offsets as v4 (correctness gate: the v4 selftest must still pass
unchanged).

### Results so far (256 MB fixture: 70% zero blocks, 30% data blocks, 25 hits)

| Build | Elapsed | Throughput | Carved | Hits / offsets |
|-------|---------|------------|--------|----------------|
| v4 engine as-is (117 patterns) | 92.7 s | 2.8 MB/s | 75 files, 73.4 MB | 25 hits, digest `00dc8591592a37ce` |
| + 13.3 zero-block skip | 80.5 s | 3.3 MB/s | 75 files | 25 hits, same digest |
| + UTF-16-BE opt-out (81 patterns) | 48.6 s | 5.3 MB/s | 75 files | 25 hits, same digest |
| + 13.4b triage in C | 40.5 s | 6.3 MB/s | 75 files | 25 hits, same digest |
| + 13.1/13.4 two-phase carve, one process | 43.2 s | 5.9 MB/s | 75 files | 25 hits, same digest |
| **+ 13.5 parallel phase 1 (auto = 4)** | **21.6 s** | **11.9 MB/s** | 75 files | 25 hits, same digest |

**4.3x end to end, 7.4x in matching, with byte-identical output** (same 25 hits,
same offsets, same 75 carved files, same SHA-256 over the hit list). Cost split
measured with each stage disabled: matching 28.9 s, smart-text extension and
writes 19.6 s, triage 11.2 s (before 13.4b).

What is left on the table: the carved bytes are ~73 MB for 25 hits because
smart-text extension writes up to 1 MB per text hit - worth revisiting once the
output folders (Sections 1 and 2) decide what actually needs to be kept on disk,
and mmap (13.6) only pays on repeated scans of the same image.

## Approved decisions

1. Folder naming scheme (Section 1): approved.
2. Organized output structure (Section 2): approved.
3. Resume flow (Section 3): approved.
4. Progressive scanning approach (Section 4): approved.
5. Fast scan criteria (Section 5): approved (5%+5%, 5min, TRIM detection, standalone).
6. Network streaming (Section 6): deferred to future version.
7. Custom signature filter (Section 7): approved (JSON profile).
8. SMB output option (Section 8): approved as advanced option.
9. Standalone fast scan menu option (Section 9): approved.
10. Pre-scan confirmation (Section 10): approved (minor UX improvement).
11. Numbered file picker for choosing an image (Section 11): approved.
12. Numbered volume/folder picker for every destination, with the same-disk
    refusal (Section 12): approved.
13. Performance plan for the image scan and the recovery (Section 13):
    approved - two-phase recovery, anchor sieve, zero-block skip, no carving
    in the read path, parallel matching, mmap image path, tighter signatures,
    measured 3x / 10x targets.
14. Base for the v5 code: `v4/` (the current `v5/` folder is a byte-identical
    copy of `v3/` - `VERSION = "3.0"`, 1342 lines vs v4's 1699 - so it is
    older than v4 and must not be used as the starting point).

## Implementation order (v5, rebuilt from v4)

| Step | Work | Why this order |
|------|------|----------------|
| 0 | `git mv`-style: copy `v4/*` into `v5/`, set `VERSION = "5.0"`, run the v4 selftest in `v5/` | establishes a correct base and a green test before any change |
| 1 | Sections 11 + 12: the shared numbered picker (`pick_path()`) and `last_paths.json` | independent of the engine, immediately removes the typing pain in every flow |
| 2 | Section 13.3 zero-block skip + 13.7 UTF-16-BE opt-out, benchmarked against the v4 engine | measured 1.9x for a few lines of code; 13.2 was benchmarked and rejected here |
| 3 | Section 13.1: two-phase index-then-carve (this is also what takes carving out of the read loop, 13.4) | changes the scan contract - do it before adding progressive/resume, and it is the next 19 s of the 48.6 s |
| 4 | Sections 1, 2, 3, 4: provenance folder, organized output + index.html, resume, progressive 5% chunks | all of them sit on the phase-1 hit index from step 3 |
| 5 | Sections 5, 9, 10: fast scan criteria, six-item main menu, pre-scan summary | menu restructure last so every mode already exists |
| 6 | Sections 7, 8: JSON signature profile loading, SMB output | thin layers over the shared picker and the profile |
| 7 | Section 13.6 mmap for image files and the 13.8 benchmark on real media | **13.5 is done early** (it was the only way to reach the 3x target); mmap only pays on repeated scans |
| 8 | Section 12's same-disk refusal, selftest for v5, commit, tag `v5.0.0`, release with `python-embed.zip`, add v5 to CI | close the release exactly like v1..v4 |

## Progress

| Step | State | Evidence |
|------|-------|----------|
| 0 | **done** | `v5/` is `v4/` + `VERSION = "5.0"`; `v5/selftest.py` PASS, `v4/selftest.py` still PASS |
| 1 | **done** | `pick_image` / `pick_folder` / `pick_destination` + `last_paths.json`; wired into the image source, the imaging destination and the recovered-data root; selftest covers discovery, depth limit, partial flag and remembered paths; driven interactively with scripted answers (menu, selection, partial confirmation, cancel) |
| 2 | **done** | 256 MB fixture: 92.7 s -> 48.6 s (1.9x), identical hits and offsets; 13.2 measured and rejected (see 13.8) |
| 3 | **done** (plus 13.4b and 13.5 pulled forward) | two-phase carve (`CachedReader` + `carve_phase`), triage moved to C, parallel phase 1 with a sequential fallback. 92.7 s -> 21.6 s (**4.3x**), same digest, same 75 carved files. Selftest now asserts parallel hits == sequential hits |
| 4 | partly done | **Sections 1 + 2 done**: `recovered_<label>_<size>_<stamp>/` with `provenance.json`, `organized_<...>/by_extension/<ext>/` + `index.html` + `summary.txt`, one pair per run, verified end-to-end on a real scan. Sections 3 (resume) and 4 (progressive 5%) still to do - they need `scan(start_offset, end_offset)` |

## Next step

Planning is complete: 13 sections, all decisions recorded, and the base is
fixed as `v4/`. The full-disk imaging trial on this machine is **not** a
prerequisite for v5 - the imaging engine was already exercised successfully
(raw and gzip, SHA-256 match) and the feature is confirmed working by the
user. Awaiting go-ahead to start at step 0.
