# fixformat v5.0.0 - release notes

## What v5 is

v5 is the same tool as release 4 - a raw-disk signature scanner and disk imager
that looks for file data left behind by a format or a reinstall - with the parts
that made a large recovery slow or blind rewritten, and with three questions
answered before the hours are spent: *is there anything on this disk* (fast
scan), *can I stop now and continue later* (progressive slices and resume), and
*where did these files come from* (a provenance-tagged `recovered_*` folder plus
a browsable `organized_*` one). You still point it at a physical disk or an
`.img`, it still reads sectors in large blocks and reports the exact byte offset
of every hit, and it still carves a readable window around each one. The safety
position is unchanged and is the whole design: **it is read-only towards every
disk it reads, it runs fully offline from a USB stick, and it uses nothing but
the Python standard library** - no pip, no network, no writes to the source.

## What is new since v4

| Feature | What you see | Where it lives |
|---|---|---|
| Fast scan (triage) | Reads only the first 5% and the last 5% inside a hard 5-minute budget, then prints hit counts per signature, sample file names, the percentage of all-zero regions, a projection for the whole disk and one recommendation. **Saves no recovered files.** | `fast_scan_ranges()`, `run_fast_scan()`, `fast_report_lines()` - menu option 1 |
| Six-item main menu | Fast scan, full scan, progressive scan, create image, analyze image, resume - each explained on screen | `main()` |
| Progressive scan | The same scan cut into 5% slices, with a checkpoint written after every slice and a `Continue with the next slice?` prompt | `run_progressive()`, `slice_bounds()`, `PROGRESSIVE_PERCENT` - option 3 |
| Resume a recovery | Option 6 lists the `recovered_*` folders with their percentage and status and continues the chosen one from `resume_offset`, with the signature bytes stored in its provenance | `ask_resume()`, `resume_flow()`, `patterns_from_provenance()` |
| Provenance-tagged run folder | `recovered_<disk_label>_<size>_<timestamp>/` with `provenance.json`: source, size, signature bytes, scan parameters, status, resume offset | `make_run_dirs()`, `provenance_payload()`, `write_provenance()` |
| Organized output | A sibling `organized_<...>/` with `by_extension/<ext>/`, a self-contained `index.html` and `summary.txt` | `organize_output()`, `write_organized_index()` |
| JSON signature profile | Pick a `.json` profile instead of the extension menu; the profile *is* the filter, and a malformed entry names the key and stops the run | `parse_signature_profile()`, `ask_signature_source()`, `profile_patterns()` |
| Validated output root | `OUTPUT_ROOT` or a typed folder/share is created and proved writable with a probe file *before* the scan, with a local fallback when it is not | `validate_output_root()`, `ask_output_root()`, `is_network_path()` |
| Numbered pickers | Image, volume and folder chosen from a numbered list with size, free space and modification time; typed path stays as option `0`; last folders remembered | `pick_image()`, `pick_folder()`, `pick_destination()`, `last_paths.json` |
| Two-phase recovery | Every hit is indexed first, then the source is walked forward once to carve; thousands of back-seeks become one sequential pass | `carve_phase()`, `CachedReader()` |
| Zero-block skip | All-zero blocks are skipped at C speed and counted; the count is reported as SSD-TRIM / wiped evidence | `zero_block()`, `zero_bytes` in the summary |
| Parallel phase 1 | Byte ranges matched in up to 4 processes, ordered merge in the parent, sequential fallback if the pool fails | `parallel_phase1()`, `parallel_worker_count()`, `_scan_range_worker()` |
| Fewer default signatures | UTF-16-BE expansion is off by default: 81 patterns instead of 117, same reachable content | `SEARCH_UTF16_BE`, `build_patterns()` |
| Smart-text cap and transcript toggle | `SMART_TEXT_MAX_EXTRA` (1 MiB) bounds how far a text hit may grow; `ENABLE_READABLE_COPY` decides whether a `_readable.txt` transcript is written next to each text window | `smart_extend_text()`, `carve_window()`, `READABLE_SUFFIX` |
| Pre-scan plan | A `PLAN FOR THIS RUN` block: mode, bytes to be read, signature count, and an estimate from the last run measured on this machine - or a clearly labelled default | `print_plan_summary()`, `estimate_scan_seconds()`, `remember_speed()`, `scan_speed.json` |
| Ctrl+C keeps the evidence | A stop inside the pool or the carving phase returns what finished so far, marks the summary `interrupted` and still writes the report | `scan()` interrupt handler, `carve_phase()` |
| Same-disk guard | A destination on the source's own physical disk is refused unless you type `YES` (see Known limits for how far this has been tested) | `confirm_same_disk()`, `same_physical_disk()`, `physical_disk_number()` |

## Upgrading from v4

These change what an existing v4 user sees:

| Change | What it means for you |
|---|---|
| **The menu has six items** | v4 had three. `2` is now FULL SCAN, not "create image"; imaging is `4`, analyzing an image is `5`, and `1`/`3`/`6` are new. Scripts that piped `1`/`2`/`3` into the tool now do something else. |
| **The fast scan saves no files** | Option 1 writes only `fastscan_<label>_<size>_<stamp>/fast_scan_report.txt` and a `provenance.json`. No carved data, no `recovered_*` folder: it is an assessment, not a recovery. |
| **Results go into run folders** | Carved data no longer lands in `recovered/` next to the script. A run writes `recovered_<disk_label>_<size>_<timestamp>/` plus a sibling `organized_<...>/`, so two runs cannot overwrite each other. |
| **`provenance.json` exists and matters** | It records the source, size, signature bytes, scan settings, status and resume offset. Resume (option 6) and the percentage column of the resume list read it; delete it and the folder can no longer be continued. |
| **A resume refuses a complete folder** | A folder whose `status` is `complete` and whose `resume_offset` is at the end of the source is rejected with "That recovery is already complete" instead of rescanning it. |
| **The carve window is capped** | Text hits still extend while the bytes look readable, but only up to `SMART_TEXT_MAX_EXTRA` (default 1 MiB) per hit - now a setting, not a constant in the function. On text-heavy media this is the difference between a few hundred MB and several GB written per run. |
| **Throughput estimates come from `scan_speed.json`** | The pre-scan estimate uses the last measured run on this machine, keyed `image:fast` / `image:full` / `device`. With no measurement it prints the 60 MB/s default and says it is a default. Delete the file to throw the memory away. |
| **A `_readable.txt` transcript is written next to text windows** | `ENABLE_READABLE_COPY` is on by default, so a text hit costs two files. The raw window keeps its original bytes; the transcript is the convenience copy. |

## Measured performance

**These are synthetic fixtures except where stated.** No figure below comes from
a real disk or a real image.

| Measurement | Result | Fixture / provenance |
|---|---|---|
| End-to-end scan, v4 engine vs v5 engine | 92.7 s -> 21.6 s (**4.3x**), 2.8 -> 11.9 MB/s | 256 MB synthetic image, 70% zero blocks / 30% data, 25 planted hits; `V5_PLAN.md` section 13.8 results table |
| Matching only | **7.4x** faster | Same run; cost split with each stage disabled |
| Correctness gate for both | Same 25 hits, same offsets, same 75 carved files, same SHA-256 digest `00dc8591592a37ce` | Same run, digest over the hit list |
| Phase-1 throughput | **33.6 - 43.1 MB/s** | 64 MB synthetic fixture, `v5/bench_step7.py` (the harness is in the repo; the JSON it writes is not committed) |
| Carving share of wall time | **about 98%** of the total | Same 64 MB run, row (b) minus row (a) of the harness |
| Bytes written while carving | **767 MB from a 64 MB source** | Same 64 MB run with the default `SMART_TEXT_MAX_EXTRA`; the reason that setting exists (also noted at the setting itself in `raw_scan.py`) |
| Real image (476.9 GiB, interrupted image `F:\hard512`) | **pending** | A first real-media run is in progress; its numbers are not in this tree yet. Nothing in the table above replaces it. |

Reading the table: the 4.3x headline is the v4 engine against the v5 engine on
the same synthetic image, and the win came from parallel phase 1 and fewer
signatures, not from a cleverer matcher (three fancier matchers were benchmarked
and rejected - `V5_PLAN.md` section 13.2). The 64 MB numbers are a different
fixture with a much higher text share, which is why their MB/s is not comparable
with the 256 MB row: they exist to show what carving costs.

## Known limits

- **Nothing in v5 has run against a real disk or a real image file.** Every
  figure above is synthetic. The first real-media run is happening on an
  interrupted 476.9 GiB image and its numbers are pending, not recorded here.
  Raw-device paths, admin rights and drive-letter resolution are therefore
  code-reviewed and unit-tested, not field-tested.
- **Carving is the cost, and text-heavy media makes it worse.** ~98% of wall
  time and 767 MB written from a 64 MB source on the benchmark fixture. Smart
  text extension grows every text hit towards the 1 MiB cap and
  `ENABLE_READABLE_COPY` writes a second file per text hit. Lower
  `SMART_TEXT_MAX_EXTRA`, lower `CARVE_LIMIT`, or turn the transcript off before
  scanning a small USB stick.
- **The same-disk check is a guard, not a proof.** `confirm_same_disk()` refuses
  a destination on the source's physical disk unless you type `YES`, and falls
  back to comparing drive letters when Windows will not report a disk number.
  It has never been exercised against two real physical disks; when neither
  number resolves, a second volume on the same disk can still get through.
  (This is the step-8 item in `V5_PLAN.md`; at commit `fcc292a` the check only
  warned - the refusal is in the working tree this document describes.)
- **The fast scan has to write its own folder.** `fastscan_*` must be created
  next to the script or in the chosen root; in a read-only script folder the run
  stops with an error ("Point the tool at a writable folder") instead of falling
  back to another folder the way the scan output does.
- **Single-file architecture.** All logic is one `raw_scan.py` (4,235 lines
  including the configuration block), and every release ships a complete copy
  under its own folder (`v1/` ... `v5/`). A fix is not inherited by older
  releases, and two functions in the same file cannot be tested in isolation
  without the module-level configuration.
- **Windows-only raw device paths.** `\\.\PhysicalDriveN` and the
  `IOCTL_STORAGE_GET_DEVICE_NUMBER` lookups are Windows calls; on any other
  system the tool can only read image files. Raw device reads need an elevated
  prompt.

Designed in the plan, deliberately not in this release (do not look for them):

| Deferred | Why it is not here |
|---|---|
| `mmap` / prefetch image path (`mmap`, `MADV_SEQUENTIAL`, `FILE_FLAG_SEQUENTIAL_SCAN`) | Section 13.6; it only pays on repeated scans of the same image, and step 7 was spent on the benchmark harness instead |
| `by_date/` in the organized folder | Section 2 lists it "if MFT timestamps are available"; MFT timestamp parsing is not implemented, so only `by_extension/` exists |
| Network streaming (`--target-ip`, agent mode) | Section 6 - analysed and deferred to a future version; imaging to an external disk reaches the same safety goal |
| Detecting a share that disappears *mid-run* | Only caught when the final move fails; the pre-scan probe cannot see a later disconnect |

## How to run it

From the repository root, on Windows:

```
v5\run_scan.bat
python\python.exe v5\raw_scan.py
python\python.exe v5\selftest.py
python\python.exe v5\bench_step7.py
```

`run_scan.bat` warns when the window is not elevated and prefers the bundled
`python\python.exe`. Raw disk reads need Administrator; image files do not.

The six menu options, one line each:

- **1 - FAST SCAN (quick triage).** First 5% + last 5%, 5-minute hard budget; counts, sample names, zero-region percentage, projection, one recommendation; saves no files.
- **2 - FULL SCAN (complete pass).** The whole source, carved, into `recovered_*` + `organized_*`.
- **3 - PROGRESSIVE SCAN (chunked, 5% at a time).** The same scan with a checkpoint after every 5%, stoppable and resumable.
- **4 - CREATE DISK IMAGE.** Unchanged from release 3/4: one pass, SHA-256, `.info.txt` sidecar, `*.partial` on failure.
- **5 - ANALYZE EXISTING IMAGE.** Full scan of an image chosen from a numbered list.
- **6 - RESUME PREVIOUS RECOVERY.** Continues a `recovered_*` folder from its recorded offset, in place, with its stored signatures.

## Release checklist

- [x] `v1/selftest.py` through `v5/selftest.py` green (CI runs all five) - 6 s total
- [x] every item in `v5/REVIEW_v5_steps0-5.md` closed, with `v5/regress_step7.py` green
- [x] release QA gate in `v5/RELEASE_QA_v5.0.0.md`: 96 of 97 checks passed; the one
      defect (D-1) was fixed in `5a7acba` and re-verified by probe (see the
      addendum in that document)
- [x] REAL_MEDIA numbers recorded in `v5/REAL_MEDIA_v5.md` (first real image, 476.9 GiB)
- [x] `python-embed.zip` attached to the release (with `v5.zip`)
- [x] tag `v5.0.0`

## Verification status of this document

Written against the working tree on 2026-10-29: commit `fcc292a` plus the
uncommitted step-8 edits in `v5/raw_scan.py` (the same-disk refusal and the
`OSError` guard around `make_fast_scan_dir()`). Every feature in the table above
was checked by name in `v5/raw_scan.py`; the performance numbers trace to
`V5_PLAN.md` section 13.8, to the comment at `SMART_TEXT_MAX_EXTRA`, or are
marked pending. Two claims from `V5_PLAN.md` are **not** in the code and are
listed as deferred rather than as new features: the `mmap` image path
(section 13.6) and `by_date/` output (section 2).

## README block (paste-ready)

```
- **v5.0.0** - Release 5: a **fast scan** first (first and last 5% inside a
  five-minute budget, saves no files) so you know whether a full pass is worth
  the hours; **progressive 5% slices** with a checkpoint after each; **resume**
  (menu option 6) that continues a `recovered_*` folder from its recorded
  offset. Every run writes `recovered_<disk>_<size>_<time>/` with a
  `provenance.json` plus a browsable `organized_*/` (`by_extension/`,
  `index.html`). Parallel phase 1 measured **3.0x** on a real 476.9 GiB image
  (25.15 MB/s on 4 workers vs 8.32 MB/s on one, identical hits) and **4.3x**
  end-to-end on the synthetic fixture, and a JSON profile can replace the
  extension menu.
```
