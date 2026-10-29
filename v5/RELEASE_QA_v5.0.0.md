# Release acceptance - fixformat v5.0.0

**RELEASE GATE: FAIL, 96 of 97 checks pass**

One check fails: a progressive scan that the user stops at the `Continue with the next slice?`
prompt is written to `provenance.json` as `status: complete` although only 12.5 % of the source
was read, and the same folder then appears as `12.5%  complete` in the resume list. Details,
exact reproduction and the error text are in **Defects found** below. Everything else in this
gate passes, including all three review blockers on their primary paths.

---

## 1. What was tested

| | |
|---|---|
| Artifact | `v5/raw_scan.py`, sha256 `7f5510a162bb22f15261ff7d37949b8ccc048fd09602ad732c1cac732923be12`, 4,235 lines, reported version `5.0` |
| Blob stability | the same hash before the first check and after the last one - no test ran against a half-written file (another session is editing `raw_scan.py`; its working-tree changes were already in this blob, see Observation O-1) |
| Test scripts | `v5/selftest.py` was re-run after the acceptance work and passes at its current blob `2cfc8729...` (and passed at `6b3dc7b2...` during the first pass); `v5/regress_step7.py` `8b511cd9...` and `v4/selftest.py` `ed37a6f9...` are unchanged and were re-run green at the end |
| Host | Windows 11, `C:` only, no network shares, bundled CPython `3.12.10` (`./python/python.exe`), no administrator rights |
| Fixtures | synthetic images only: 2,003 B (slice-boundary), 4,096 B (edge/slice), 116 B (`.txt` carve), 4 MiB (fast scan), 16 MiB (`big.img`: `%PDF-1.7`@1024, `AC1015`@4096, `\n# Title\n`@10000, `%PDF-1.7`@8 MiB+4096, PNG@15.7 MiB), 300 KiB (imaging source), four JSON profiles |
| Interactive driver | `C:/Users/eyad/AppData/Local/hermes/profiles/eyad/cache/scratch/qa5/qa_driver.py <scenario>` - copies `v5/raw_scan.py` into a temp `app/` dir, imports it, replaces `builtins.input` with a scripted answer list, calls `raw_scan.main()`, captures the whole screen and restores `input` and the module globals afterwards. One process per scenario, so no scenario can leak state into the next |
| Isolation | `raw_scan.SCRIPT_DIR`, `LAST_PATHS_FILE` and `SPEED_FILE` are pointed at a fresh temp dir per scenario, `os.chdir()` goes there, `TARGET`/`OUTPUT_ROOT` are reset, and `list_logical_drives()` is stubbed to `[]` so the pickers never walk `C:` (see Observation O-2 for what that excludes). `git status --porcelain` is unchanged by the whole run: no `recovered_*`, `organized_*`, `fastscan_*`, `last_paths.json` or `scan_speed.json` in the repository |
| Driver result files | transcript + machine-readable facts per scenario in `.../scratch/qa5/out/<scenario>.txt` / `.json`; every quoted line below is copied from them |

### The commands

Repository root for everything: `cd C:/Users/eyad/Documents/Codex/2026-09-27/fixformat`

```sh
# the five regression suites (this is what CI runs)
./python/python.exe v1/selftest.py ; echo exit=$?
./python/python.exe v2/selftest.py ; echo exit=$?
./python/python.exe v3/selftest.py ; echo exit=$?
./python/python.exe v4/selftest.py ; echo exit=$?
./python/python.exe v5/selftest.py ; echo exit=$?
./python/python.exe v5/regress_step7.py ; echo exit=$?

# every functional check in sections A-E, one process each
Q=C:/Users/eyad/AppData/Local/hermes/profiles/eyad/cache/scratch/qa5/qa_driver.py
for s in a1 a2 a3 s1 s2 s5 s6 s7 s8 s11 c1 c2 c3 c4 c5 c6 d1 d2 d3 e1 e2; do
  ./python/python.exe $Q $s ; done

# the one CLI reproduction in "Defects found" uses no driver at all:
#   copy v5/raw_scan.py into a temp dir, feed it an answer file on stdin
```

Driver scenarios are named `<section><item>`: `a1` = blocker 1, `s6` = review item S6,
`c4` = menu option 4, `d2` = broken profile, `e1` = local output root.

---

## 2. A. The three blockers stay fixed - 13 of 14

Command for every row: `./python/python.exe $Q <scenario>`

| # | Check | Scenario | Result | Evidence |
|---|---|---|---|---|
| A1.1 | whole-file scan of the 2,003 B image finds the `.md` at 1000 | `a1` | PASS | `[1000]` |
| A1.2 | contiguous slices `[0,1001)` + `[1001,2003)` find it exactly once and equal the whole scan | `a1` | PASS | slices `[1000] + []`, union `[1000]`, no duplicate |
| A1.3 | the parallel path gives the same answer (`PARALLEL_MIN_SIZE=0`, `PARALLEL_WORKERS=2`) | `a1` | PASS | `parallel whole/left/right = [[1000], [1000], []]` |
| A1.4 | split 1 byte **inside** another pattern (`@2047`, split at 2048) and a hit 1 byte from EOF (`@4093`) | `a1` | PASS | `[2047] + [4093] == whole [2047, 4093]` |
| A2.1 | an interrupted slice is not recorded as `complete` (monkeypatched `scan()` returning `interrupted=True`, `bytes_scanned//2`) | `a2` | PASS | `status = "interrupted"` |
| A2.2 | the status is exactly `interrupted` | `a2` | PASS | `"status": "interrupted"` |
| A2.3 | `resume_offset` is where the read stopped, not the slice end | `a2` | PASS | `resume_offset = 8388608` of `16777216` (the slice was the whole source) |
| A2.4 | `resume_offset == start + bytes_scanned` | `a2` | PASS | `8388608 == 0 + 8388608` |
| A2.5 | the folder is offered again by `ask_resume()` | `a2` | PASS | returns the folder path with resume offset `8388608` |
| A2.6 | the resume picker does not claim it is complete | `a2` | PASS | `already complete` absent |
| **A2.7** | **end to end: a progressive run the user stops at `Continue with the next slice?` is recorded as `interrupted`** | `a2` | **FAIL** | reads 2 MiB of 16 MiB, provenance says `"status": "complete", "interrupted": false` - see **D-1** |
| A3.1 | an `ext="txt"` carved window is byte-exact | `a3` | PASS | 116 bytes on disk, byte-equal to the source window (`\xff\xfe\x00BADBYTES.txt\x00` intact) |
| A3.2 | the raw bytes survive (no U+FFFD rewriting) | `a3` | PASS | `0xff 0xfe` and `BADBYTES` present |
| A3.3 | the readable transcript is a separate file | `a3` | PASS | `Extension__txt_0x...6F.txt` + `Extension__txt_0x...6F_readable.txt` |

Blocker verdicts:

1. **Boundary hits (B1) - fixed.** A hit straddling a slice boundary is reported by the slice that
   owns its first byte, exactly once, on the sequential *and* the parallel path, and the two paths
   agree. The old dead band (`max_len-1` bytes before every `end_offset`) is gone.
2. **Interrupted slice (B2) - fixed for Ctrl+C inside a slice, broken for the checkpoint prompt.**
   The recorded offset is now `start + bytes_scanned` and the status is `interrupted`, and the folder
   is resumable. But when the *user* stops the run (answers `n` at the slice prompt instead of
   pressing Ctrl+C), the same field is overwritten back to `complete` by the caller - D-1.
3. **`.txt` carve (B3) - fixed.** The raw window keeps its exact bytes and the transcript gets its own
   `_readable.txt` name; the raw file is no longer clobbered.

---

## 3. B. The review's should-fix items - 25 of 25

| # | Check | Scenario | Result | Evidence |
|---|---|---|---|---|
| S1.1 | `sampled_bytes` is exact, not inflated by the 4 KiB pre-roll (sequential) | `s1` | PASS | `419430` == `(209715-0)+(4194304-3984589)` = `419430` |
| S1.2 | the same number in parallel mode (`PARALLEL_WORKERS=2`) | `s1` | PASS | `419430` - identical, so it no longer depends on the worker count |
| S2.1 | a fast scan that read nothing reports `sampled_bytes = 0`, `budget_hit = True` | `s2` | PASS | `{"sampled_bytes": 0, "budget_hit": true}` |
| S2.2 | it does **not** print the wiped/encrypted/TRIMmed verdict | `s2` | PASS | `may be wiped` and `that is the pattern of a` absent |
| S2.3 | it prints the "too little to judge" recommendation instead | `s2` | PASS | `that is too little to judge anything ... This is NOT a wiped-disk verdict.` |
| S2.4 | no projection is printed from an empty sample | `s2` | PASS | `PROJECTION` absent |
| S5.1 | `summary.txt` does not say `recovered files : 0` | `s5` | PASS | `recovered files : 2`, `recovered total : 131072 bytes` |
| S5.2 | and the number equals the files on disk and the hits | `s5` | PASS | summary `2` == files `2` == hits `2` (index.html says `2 files · 128.00 KB`) |
| S6.1 | a progressive run leaves a provenance with a resume point | `s6` | PASS | `resume_offset 1048576`, folder created, exit 0 |
| S6.2 | provenance stores pattern **bytes** for every label | `s6` | PASS | `signature_bytes`: 81 rows for 81 labels |
| S6.3 | the stored hex is the pattern itself | `s6` | PASS | `"PDF document (.pdf)" -> "255044462d"` (= `b"%PDF-1.7"[0:5]`) |
| S6.4 | `patterns_from_provenance()` rebuilds them byte-identically | `s6` | PASS | 81 patterns, no warnings, tuples identical |
| S6.5 | renaming every label cannot swap the bytes | `s6` | PASS | 81 identical byte patterns after renaming all labels |
| S6.6 | the resumed run rebuilds them and says so | `s6` | PASS | `Continuing ... from 1.00 MB with 81 signatures` |
| S6.7 | no "predates stored signature bytes" fallback warning | `s6` | PASS | no `NOTE:` line |
| S7.1 | a stopped run is resumable from where it stopped, with its hits | `s7` | PASS | 2 hits, `resume_offset 2097152` of `16777216` |
| S7.2 | the resumed run finishes the source | `s7` | PASS | `status complete`, `resume_offset 16777216` |
| S7.3 | the totals accumulate instead of resetting to this pass | `s7` | PASS | `hits 2 -> 4` |
| S7.4 | `bytes_scanned` accumulates | `s7` | PASS | `2097152 -> 16777216` (= the source size) |
| S7.5 | `carved_files` accumulates | `s7` | PASS | `2 -> 4` |
| S7.6 | the stored `counts` add up to the stored `hits` | `s7` | PASS | `4 == 4` |
| S8.1 | speed memory is keyed per mode | `s8` | PASS | `{"image:fast": 13107200.0, "image:full": 3670016.0}` |
| S8.2 | a full-scan estimate uses the full-scan rate | `s8` | PASS | 28.6 s (full) vs 8.0 s (fast) for the same 100 MiB |
| S11.1 | two 50 % slices produce hits for both slices | `s11` | PASS | 8 hits: 4 markers in each 8 MiB slice |
| S11.2 | `CARVE_LIMIT` is one budget for the run, not per slice | `s11` | PASS | `CARVE_LIMIT=3`, 8 hits, **3** files carved (6 would be the per-slice answer), all 3 moved to `by_extension` |

---

## 4. C. The user-visible menu and flows - 29 of 29

All six options were driven through `main()` with scripted input. Answers are listed so the run is
reproducible (`all` = every extension).

### C1. Option 1 - fast scan · 4 checks PASS

`qa_driver.py c1`, answers `1 2 1 1 y` (menu → image → picker → built-in signatures → confirm)

```
MAIN MENU - CHOOSE WHAT TO DO
  1 - FAST SCAN (quick triage)
...
  signatures  : 81 patterns
  signature set : built-in signatures
  regions       : 2 (first + last)
  carving       : off - summary only, no files saved
...
FAST SCAN RESULT - QUICK TRIAGE
  source size   : 16.00 MB
  sampled       : 1.60 MB in 2 region(s)
                  0 B - 819.20 KB
                  15.20 MB - 16.00 MB
  time          : 00:00:00
  hits          : 3
  per signature :
      PDF document (.pdf)                          1
      PNG image (.png)                             1
      Markdown heading LF (.md) [utf-8]            1
  zero regions  : 0.0% of the sampled bytes (0 B)

  PROJECTION if the rest of the source has the same density:
    hits on the whole source : about 30
...
RECOMMENDATION: 3 hits is under the fast-scan threshold (50), but
  data is there - run a FULL SCAN (menu option 2) to recover it.
...
Carved files are NOT produced by a fast scan. If the recommendation
says so, choose option 2 (FULL SCAN) or option 3 (PROGRESSIVE SCAN).
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C1.1 | exits 0 | PASS | `exit=0` |
| C1.2 | the triage report and a recommendation are printed | PASS | `FAST SCAN RESULT - QUICK TRIAGE`, `RECOMMENDATION` |
| C1.3 | `sampled` is printed and no carved files are produced | PASS | `sampled       : 1.60 MB in 2 region(s)` |
| C1.4 | one `fastscan_*` folder, no `recovered_*` | PASS | `fastscan_big_16.0MB_20261029_103110` |

### C2. Option 2 - full scan · 6 checks PASS

`qa_driver.py c2`, answers `2 2 1 1 all y 1 y n n`

```
STEP 4 of 5 - CONFIRM YOUR CHOICES
  extensions  : .7z, .bat, .cfg, .codex, ... .zip

WHERE SHOULD THE RECOVERED DATA BE WRITTEN?
  [1] Next to this script (default): C:\...\work\c2
  [2] Another folder or a network share - typed now

PLAN FOR THIS RUN
  mode        : FULL SCAN - one complete pass over the source
  source size : 16.00 MB (16,777,216 bytes)
  will read   : 16.00 MB (100.0% of the source)
  signatures  : 81 patterns
  case        : sensitive (exact match)
  signature set : built-in signatures
  output root   : C:\...\work\c2

Do you want to start the scan now?
  Y = yes, start scanning

STEP 5 of 5 - SCANNING (this can take a long time on big disks)
...
PHASE 2 - CARVING 4 HITS (one forward read of the source)
...
RESULTS - RECOVERY REPORT
hits total    : 4
--- hits per pattern ---
...
recovered folder : C:\...\work\c2\recovered_big_16.0MB_20261029_103120
  provenance     : ...\recovered_big_16.0MB_20261029_103120\provenance.json
organized folder : C:\...\work\c2\organized_big_16.0MB_20261029_103120
  by_extension   : 4 files, 1.26 MB
  index (html)   : ...\organized_big_16.0MB_20261029_103120\index.html
  summary        : ...\organized_big_16.0MB_20261029_103120\summary.txt
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C2.1 | exits 0 | PASS | `exit=0` |
| C2.2 | exactly one `recovered_*` and one `organized_*` folder | PASS | both named `..._big_16.0MB_20261029_103120` |
| C2.3 | `recovered_*` holds `search_results.txt`, `provenance.json`, `carved/` | PASS | all three listed |
| C2.4 | `organized_*` holds `index.html`, `summary.txt`, `by_extension/` | PASS | all three listed |
| C2.5 | the report and the per-pattern table are printed | PASS | `RESULTS - RECOVERY REPORT`, `--- hits per pattern ---`, `hits total : 4` |
| C2.6 | the plan reports the built-in 81 patterns | PASS | `signatures  : 81 patterns` |

### C3. Option 3 - progressive scan · 4 checks PASS

`qa_driver.py c3`, answers `3 2 1 1 all y 1 y` + 15 × `y` (one per checkpoint)

```
STEP 5 of 5 - SCANNING (this can take a long time on big disks)
Slices of 5% with a checkpoint after each one.
...
SLICE 1 - 0 B - 1.00 MB
PHASE 2 - CARVING 2 HITS (one forward read of the source)
Slice done: 2 hits so far, 2 files carved, saved at offset 1.00 MB
Continue with the next slice? [Y/n]:
SLICE 2 - 1.00 MB - 2.00 MB
Slice done: 2 hits so far, 2 files carved, saved at offset 2.00 MB
Continue with the next slice? [Y/n]:
...
SLICE 16 - 15.00 MB - 16.00 MB
Slice done: 4 hits so far, 4 files carved, saved at offset 16.00 MB
The whole source has been scanned.
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C3.1 | exits 0 | PASS | `exit=0` |
| C3.2 | at least two contiguous slices, each with its own checkpoint | PASS | 16 contiguous 1 MiB slices (`5%` of 16 MiB, minimum 1 MiB), first `0 B - 1.00 MB`, last `15.00 MB - 16.00 MB` |
| C3.3 | it says the source was completely scanned | PASS | `The whole source has been scanned.` |
| C3.4 | provenance ends `complete` at the end of the source | PASS | `status complete`, `resume_offset 16777216` of `16777216` |

Note: two slices are what the checkpoint logic was driven with in `s7`/`c6` (stopped after slice 2);
a *complete* 5 % run of a 16 MiB source is 16 slices, not 2, because `slice_bounds()` floors a slice
at 1 MiB (`SECTOR_SIZE * 2048`).

### C4. Option 4 - create an image · 5 checks PASS

`qa_driver.py c4` with `TARGET` = the 300 KiB source file, answers `4 1 0 <folder> y`

```
STEP 5 of 5 - IMAGING (one fast read of the disk)
...
=== IMAGING RESULT ===
  image file   : ...\images\diskimage_C_Users_eyad_AppData_Local_hermes_profil_20261029_103129.img
  copied       : 300.00 KB (307200 bytes, 600 sectors)
  source size  : 300.00 KB (307200 bytes)
  verified     : YES - copied size matches the source
  sha256       : a24f9c5a3ba409b27e14c794776f6a6b3538ef9a741aec71e42d0bd650f90931
  status       : COMPLETE
  info file    : ...\diskimage_...img.info.txt
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C4.1 | exits 0 and writes exactly one `.img` | PASS | `diskimage_..._20261029_103129.img` |
| C4.2 | the image is byte-identical to the source | PASS | 307,200 bytes compared in Python, equal |
| C4.3 | the printed `sha256` is the source's SHA-256 | PASS | independently computed `a24f9c5a...f90931` |
| C4.4 | the `.info.txt` sidecar exists | PASS | `...img.info.txt` |
| C4.5 | the result says `COMPLETE` | PASS | `status       : COMPLETE` |

### C5. Option 5 - analyze an existing image · 3 checks PASS

`qa_driver.py c5`, answers `5 1 1 all y 1 y n n`

```
STEP 1 of 5 - CHOOSE THE DISK IMAGE
Pick the image from the list below, or browse to its folder.
Analyzing an image does not touch the original disk at all.
...
Selected: C:\...\work\c5\big.img
...
RESULTS - RECOVERY REPORT
hits total    : 4
recovered folder : C:\...\work\c5\recovered_big_16.0MB_20261029_103143
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C5.1 | exits 0 | PASS | `exit=0` |
| C5.2 | goes straight to the image picker (no disk question) | PASS | `STEP 1 of 5 - CHOOSE THE DISK IMAGE` |
| C5.3 | produces a full recoverable run | PASS | `RESULTS - RECOVERY REPORT`, `recovered_*` created |

### C6. Option 6 - resume, and the refusal for a completed recovery · 7 checks PASS

`qa_driver.py c6`. Run 1 (`3 2 1 1 all y 1 y y n n`) stops after two slices; run 2 (`n 6 1` + `y` × 15)
resumes through option 6; run 3 (`1`) asks for the now-complete folder.

```
A previous recovery was found - do you want to continue one?
  [1] recovered_big_16.0MB_20261029_103135            12.5%  complete
...
Continuing C:\...\work\c6\big.img from 2.00 MB with 81 signatures
...
Recovery updated: 4 hits in this folder (4 carved), resume offset 16.00 MB
  recovered : C:\...\work\c6\recovered_big_16.0MB_20261029_103135
  organized : C:\...\work\c6\organized_big_16.0MB_20261029_103135
```

and, for the completed folder:

```
  [1] recovered_big_16.0MB_20261029_103135           100.0%  complete
That recovery is already complete: 16.00 MB of 16.00 MB were scanned.
Nothing is left to continue there. Start a new scan (option 2 or 3)
if you want to search the source again, with other signatures.
```

| # | Check | Result | Evidence |
|---|---|---|---|
| C6.1 | a stopped progressive run leaves one `recovered_*` folder | PASS | exit 0, one folder |
| C6.2 | option 6 exits 0 | PASS | `exit=0` |
| C6.3 | it announces what it continues | PASS | `Continuing ...\big.img from 2.00 MB` |
| C6.4 | it resumes to the end of the source | PASS | `resume_offset 16777216`, `bytes_scanned 16777216` |
| C6.5 | the closing line reports the folder's total | PASS | `Recovery updated: 4 hits in this folder (4 carved)` |
| C6.6 | an already-complete recovery is refused (not rescanned) | PASS | `That recovery is already complete: 16.00 MB of 16.00 MB were scanned.` exit 0 |
| C6.7 | it tells the user what to do instead | PASS | `Nothing is left to continue there. Start a new scan (option 2 or 3)` |

The first line of that block is also the visible symptom of **D-1**: a folder that holds 12.5 % of
the source is presented as `complete`.

---

## 5. D. The JSON profile path - 13 of 13

### D1. A good profile scans only its patterns · 6 checks PASS

`qa_driver.py d1`, answers `2 2 1 2 3 y 1 y n n` (the `3` is the number printed for `p_pdf_hex.json`)

```
PROFILE FILES FOUND (4)
  [1] bad_json.json                                            18 B
      ...\work\d1            <- a folder line is printed under every row
  [2] p_dwg_text.json                                         101 B
  [3] p_pdf_hex.json                                           96 B
  [4] short_text.json                                          83 B
  [0] Type the full path of another profile
  [N] Keep the built-in signatures
Profile [N]: 3

Loaded p_pdf_hex.json
  PDF only: 1 signatures over .pdf(1)

The extension menu is skipped: the profile IS the filter.
  PDF only: 1 signatures over .pdf(1)
...
  extensions  : .pdf
  signatures  : 1 patterns
  signature set : PDF only: 1 signatures over .pdf(1)
...
HIT | Profile .pdf 25 50 44 46 2D | offset=0x0000000000000400 (dec 1024) | sector=2
HIT | Profile .pdf 25 50 44 46 2D | offset=0x0000000000801000 (dec 8392704) | sector=16392
hits total    : 2
  Profile .pdf 25 50 44 46 2D              2
```

| # | Check | Result | Evidence |
|---|---|---|---|
| D1.1 | the run completes, exit 0 | PASS | `exit=0` |
| D1.2 | the plan reports only the profile's patterns | PASS | `signatures  : 1 patterns` (not 81) |
| D1.3 | the profile is loaded and the extension menu skipped | PASS | `Loaded p_pdf_hex.json`, `the profile IS the filter.` |
| D1.4 | every provenance label is a `Profile .` label | PASS | `["Profile .pdf 25 50 44 46 2D"]` |
| D1.5 | no built-in label leaks in | PASS | no `PDF document (.pdf)` anywhere |
| D1.6 | only the profile's pattern matched | PASS | 2 hits, both `Profile .pdf`, at 1024 and 8 MiB+4096 |

The 16 MiB fixture also carries `AC1015`, `\n# Title\n` and a PNG header, and the built-in run of
C2 finds 4 hits on it - so the profile really filtered the search, it did not just print itself.

### D2. A broken profile stops the run · 4 checks PASS

`qa_driver.py d2`, answers `2 2 1 2 1` (`bad_json.json` is `{ "signatures": { `)

```
SIGNATURE ERROR: the profile bad_json.json is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 19 (char 18)
Nothing was scanned. Fix the file (or choose the built-in set)
and run the tool again.
```

| # | Check | Result | Evidence |
|---|---|---|---|
| D2.1 | exit code 2 | PASS | `exit=2` |
| D2.2 | `SIGNATURE ERROR` is printed | PASS | line above |
| D2.3 | `Nothing was scanned.` follows | PASS | line above |
| D2.4 | stops before `STEP 3` and creates no folder | PASS | `STEP 3 of 5` absent, no `recovered_*` |

### D3. A one-character value is refused · 3 checks PASS

`qa_driver.py d3`, answers `2 2 1 2 4` (`short_text.json` = `{"md": ["#"]}`)

```
SIGNATURE ERROR: profile entry .md has a one-character value '#', which is too short to search for
Nothing was scanned. Fix the file (or choose the built-in set)
and run the tool again.
```

| # | Check | Result | Evidence |
|---|---|---|---|
| D3.1 | exit code 2 | PASS | `exit=2` |
| D3.2 | the reason names the offending entry | PASS | `.md`, `'#'`, `too short to search for` |
| D3.3 | stops before `STEP 3`, nothing scanned | PASS | no `recovered_*` |

---

## 6. E. The output root - 10 of 10

### E1. A local folder is validated and used · 6 checks PASS

`qa_driver.py e1`, answers `2 2 1 1 all y 2 <root> YES y n n`

```
WHERE SHOULD THE RECOVERED DATA BE WRITTEN?
  [1] Next to this script (default): C:\...\work\e1
  [2] Another folder or a network share - typed now

Writing onto the disk being scanned can overwrite the very data you
want back. An external disk or a share (\\PC\RecoveredData) is safest.

Folder or share: C:\...\work\e1\out_local

REFUSED - THAT FOLDER IS ON THE SAME PHYSICAL DISK AS THE SOURCE
  Writing recovered data there can overwrite the very data this scan
  is trying to recover. Use a different physical disk or a share.
Type YES (in capitals) to use it anyway: YES
Overriding the refusal at your explicit request.
Output root     : C:\...\work\e1\out_local
...
  output root   : C:\...\work\e1\out_local
...
SAVED TO: C:\...\work\e1\out_local
```

| # | Check | Result | Evidence |
|---|---|---|---|
| E1.1 | the folder is used: both run folders appear inside it | PASS | 1 `recovered_*` + 1 `organized_*` in `out_local` |
| E1.2 | `Output root     : <path>` is printed after the write proof | PASS | line above |
| E1.3 | the plan's `output root` row shows that path | PASS | `  output root   : C:\...\work\e1\out_local` |
| E1.4 | the probe file `.fixformat_write_test` is gone afterwards | PASS | `os.path.exists(...) == False` |
| E1.5 | nothing is written next to the script when a root was chosen | PASS | no `recovered_*/organized_*/fastscan_*` in `work\e1` |
| E1.6 | the run folders keep the default layout | PASS | `search_results.txt`, `provenance.json`, `carved/` |

`YES` is in the answer list because this build refuses a root on the source's own physical disk
(Observation O-1) - the fixture image and the root are both on `C:`.

### E2. A path that is a file is refused · 4 checks PASS

`qa_driver.py e2`, answers `2 2 1 1 all y 2 <big.img> n y n n`

```
CANNOT USE THAT FOLDER: C:\...\work\e2\big.img exists and is a file, not a folder
Writing next to this script instead: C:\...\work\e2

PLAN FOR THIS RUN
  output root   : C:\...\work\e2
```

| # | Check | Result | Evidence |
|---|---|---|---|
| E2.1 | the reason says it is a file, not a folder | PASS | `exists and is a file, not a folder` |
| E2.2 | the user is asked `Try another folder? [Y/n]:` | PASS | prompt list of the run |
| E2.3 | answering `n` falls back to the script folder and the run completes | PASS | `Writing next to this script instead: ...`, exit 0 |
| E2.4 | nothing is written into or beside the file | PASS | image size unchanged (16,777,216 B), no folder under it |

---

## 7. F. Regression sweep - 6 of 6

Command: from the repository root, one line each.

| # | Command | Result | Evidence |
|---|---|---|---|
| F1 | `./python/python.exe v1/selftest.py` | PASS | `exit=0`, `SELFTEST PASS` |
| F2 | `./python/python.exe v2/selftest.py` | PASS | `exit=0`, `SELFTEST PASS` |
| F3 | `./python/python.exe v3/selftest.py` | PASS | `exit=0`, `SELFTEST PASS` |
| F4 | `./python/python.exe v4/selftest.py` | PASS | `exit=0`, `SELFTEST PASS` |
| F5 | `./python/python.exe v5/selftest.py` | PASS | `exit=0`, `SELFTEST PASS` (89 s warm, 325 s cold - see O-3); re-run green at the current selftest blob after the acceptance work |
| F6 | `./python/python.exe v5/regress_step7.py` | PASS | `exit=0`, `REGRESS STEP7 PASS`, T1-T6 all `PASS` |

v1-v4 were run before the acceptance work, v5/regress and v5/selftest before *and* after it
(`after_v5.out`, `after_regress.out` in the scratch dir): all six exit 0 in both passes, and
`v5/raw_scan.py` had the same sha256 before and after.

---

## 8. Defects found

### D-1 (should-fix, user-visible, contradicts the B2 fix) - a progressive run stopped at the checkpoint is recorded as `complete`

**What happens.** A progressive scan that the user stops by answering `n` at
`Continue with the next slice? [Y/n]:` writes `"status": "complete"` and `"interrupted": false`
into `provenance.json`, with `resume_offset` and `bytes_scanned` pointing at 12.5 % of the source.
The resume list then shows the folder as `12.5%  complete`, and any consumer of `provenance.json`
(the release notes' own "Resume a recovery" contract, and `ask_resume()`'s status column) is told
the opposite of the truth.

**Reproduction - no driver, plain CLI** (temp dir, so nothing is written in the repository):

```sh
PY=C:/Users/eyad/Documents/Codex/2026-09-27/fixformat/python/python.exe
D=$TEMP/qa_stop; mkdir -p "$D"; cp v5/raw_scan.py "$D/"; cd "$D"
# 16 MiB source, %PDF-1.7 at offset 1024
"$PY" -c "d=bytearray(b'Z'*16777216); d[1024:1032]=b'%PDF-1.7'; open('big.img','wb').write(bytes(d))"
# menu 3, image 2, picker 1, built-in 1, all extensions, case y, default root 1,
# confirm y, continue slice 1 = y, stop at the checkpoint = n, keep results n
printf '3\n2\n1\n1\nall\ny\n1\ny\ny\nn\nn\n' > a_stop.txt
"$PY" raw_scan.py < a_stop.txt > out.txt ; echo exit=$?
# -> exit=0
#    SLICE 1 - 0 B - 1.00 MB ... SLICE 2 - 1.00 MB - 2.00 MB
#    Continue with the next slice? [Y/n]: Stopped. Run the tool again and choose this recovery to continue.
"$PY" -c "import json,glob;p=json.load(open(glob.glob('recovered_*/provenance.json')[0],encoding='utf-8'));print(p['status'],p['interrupted'],p['resume_offset'],p['bytes_scanned'],p['source_size'])"
# -> complete False 2097152 2097152 16777216        <-- 2 MiB of 16 MiB, reported complete
printf '1\n' | "$PY" raw_scan.py | grep recovered_
# ->   [1] recovered_big_16.0MB_20261029_102256            12.5%  complete
```

**Expected:** `status` = `interrupted` (as `run_progressive()` itself sets), `interrupted` = true,
`resume_offset` = 2,097,152, and the resume list showing `12.5%  interrupted`.

**Observed:** `complete False 2097152 2097152 16777216`, and `12.5%  complete` on screen. The
driver records the same result as check **A2.7** (`FACT stopped_run_provenance`).

**Cause (read at the cited code, not guessed).** `run_progressive()` writes the correct status
(`"complete" if not combined["stopped_early"] else "interrupted"`) and then returns to
`run_search_flow()`, which *unconditionally* writes its own `provenance_payload(...)` over the
same file after the scan:

- `run_progressive()` final write: `prov.update({... "status": ("complete" if not combined["stopped_early"] else "interrupted")})`
- `run_search_flow()` after the call: `provenance_payload(target, total, summary, ...)` +
  `write_provenance(recovered_dir, provenance)`, where `summary["interrupted"]` is the *slice-
  internal* flag (false here, because the stop happened between slices) and the cumulative
  `totals` key from `run_progressive()` is dropped.

So the fix for B2 is correct where it was made and is undone one level up, on the "user stops at
the checkpoint" path. The Ctrl+C-inside-a-slice path (A2.1-A2.6) is unaffected and passes.

**Impact.** No data loss: `resume_offset` is right and `ask_resume()` still offers the folder
(its refusal needs `resume_offset >= source_size`), which is why the resume flow itself works
(C6, S7). The damage is the status a user and any tooling read: it says a 12.5 % folder is finished,
which is the failure mode the review raised as blocker B2 ("a run that claims to be complete").

**Suggested fix (small).** In `run_search_flow()`, when the scan ran progressively, either skip the
second provenance write (the one `run_progressive()` already made is cumulative and correct) or
carry its fields into it: `interrupted=summary.get("interrupted")`, `totals=summary.get("totals")`,
and `status` derived the way `run_progressive()` derives it - `"interrupted"` when
`summary.get("stopped_early")` or `summary.get("interrupted")`, `"complete"` when
`summary.get("resume_offset") >= total`, else `"in progress"`.

---

## 9. Observations (not gate failures, but known and must be decided before shipping)

**O-1. The same-disk rule is now a refusal, for image files too.** With the source `big.img` on
`C:` and the output root on `C:`, this build prints
`REFUSED - THAT FOLDER IS ON THE SAME PHYSICAL DISK AS THE SOURCE` and requires the literal `YES`
(`confirm_same_disk()`), and the run only proceeds after the override - E1 above answers `YES` for
that reason. `v5/STEP6_ACCEPTANCE.md` section F3 requires the image-file case to be a *warning*
that names the volume and lets the run continue with no question ("Section 12's refuse by default
belongs to step 8"). `v5/RELEASE_NOTES_v5.0.0.md` documents the refusal as an accepted step-8
change. Both cannot be true: either STEP6-F3 is updated, or the image-source case goes back to a
warning. Nothing in this gate breaks either way - the point is that the release document and the
acceptance document disagree.

**O-2. Volume enumeration was stubbed for the gate.** `list_logical_drives()` returned `[]` so the
pickers and `find_recoveries()` could not walk the machine's real volumes. Consequences: the
network-share paths (`\\localhost\...`, mapped drives), the volume list inside `pick_folder()` and
`find_recoveries()`' drive-root sweep are **not** covered here (see "Not covered").

**O-3. `v5/selftest.py` is slow on a cold machine.** 325 s the first time, 87-89 s once the file cache
and the on-access scanner had seen it. The cost is the text-cap fixture inside `v5/selftest.py`
(the 200,000-byte `texty.img` block at the end of `main()`): `min_gap=0`, a 3-byte pattern
(`b"BCD"`), 7,693 hits and one carve window per hit - ~15,000 files written. My first attempt with a
200 s timeout was killed, so any CI job needs a >6 min allowance.

**O-4. STEP6 wording deviations in the profile errors (cosmetic, still actionable).**
`STEP6_ACCEPTANCE.md` B8 requires the one-character refusal to say `produced no searchable pattern`
plus the profile's name or file; the message is `profile entry .md has a one-character value '#',
which is too short to search for` - it names the extension and the value but neither the phrase nor
the file. STEP6 B9 requires a *mixed* profile (`{"md": ["#", "\n# "]}`) to drop the bad value with
one note line and continue; this build refuses the entire profile
(`profile entry .md has a one-character value '#'...`). STEP6 B6 requires `41 43 1` (a byte list
with a partial byte) to be refused; `parse_signature_profile()` accepts it and searches it as text.

**O-5. `create_disk_image()` prints a literal `\r`.** The imaging progress line is built with
`"\\rImaging: ..."` instead of `"\rImaging: ..."`, so the console shows the two characters `\` `r`
at the start of the line and the line does not overwrite itself:

```
\rImaging: 300.00 KB / 300.00 KB (100.00%) | sectors 600 / 600 | speed 19.37 MB/s | ETA 00:00:00
```

Cosmetic (the `\rScanned ...` line in `scan()` is correct), one-character fix.

---

## 10. Not covered by this gate

Deliberately not tested here - do not read the PASS above as evidence for any of these:

1. **Real disks and real images.** No `\\.\PhysicalDriveN`, no `\\.\C:`: every scan read a synthetic
   file. `F:\hard512` was left untouched on purpose (another session owns that run).
2. **Administrator rights**, ACL-protected volumes, raw-device sector alignment, >2 TiB offsets.
3. **Long runs.** Nothing near 500 GB, no multi-hour timing, no Ctrl+C on real hardware (Ctrl+C is
   covered by `v5/regress_step7.py` T1/T2 in-process, not by a real interrupt).
4. **Network and SMB.** No UNC root, no mapped drive, no share that disappears mid-run, no
   `pick_folder()` `[U]`/`[L]` entries (O-2). The loopback share recipe in STEP6 was not used.
5. **A read-only output folder** (`icacls /inheritance:r`) and the mid-run loss of the root.
6. **The same-disk warning against a second physical disk**, and the `subst`-letter negative case.
7. **NTFS sparse imaging** (`IMAGE_CHUNK_SIZE` 8 MiB, mode 2) and the gzip/xz/zstd image modes;
   only the default raw mode was exercised.
8. **MAX_PATH / long-path behaviour**, and Arabic or other non-ASCII paths through the run folders.
9. **`v5/bench_step7.py`** and any performance claim - this gate checks behaviour, not throughput.
10. **`v4` and older scanner behaviour** beyond their selftests.

---

## 11. Verdict

- 96 of 97 checks pass; the three blockers hold on their primary paths (boundary hits, interrupted
  slice, `.txt` byte-exactness) and every should-fix item from the review is closed and verified.
- One check fails, it is reproducible without the driver, and it is a real user-visible falsehood
  (`complete` for a 12.5 % folder) rather than a documentation mismatch. Per the gate rule it is
  not averaged away.
- **RELEASE GATE: FAIL** until D-1 is fixed (one small change in `run_search_flow()`'s provenance
  write) and checks A2.7 plus C6/S7 are re-run. O-1 needs a decision, O-3 an allowance in CI, O-4
  and O-5 a cleanup.

*Tested blob: `v5/raw_scan.py` sha256 `7f5510a1...23be12`. Everything above describes that file and
nothing else; a later edit needs a re-run of `qa_driver.py a1 a2 a3 s1 s2 s5 s6 s7 s8 s11 c1 c2 c3
c4 c5 c6 d1 d2 d3 e1 e2` plus the six regression commands.*
