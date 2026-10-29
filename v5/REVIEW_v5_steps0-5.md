# Review - v5/raw_scan.py, steps 0-5 of the V5 plan

**Reviewed commit:** `802df9f` ("v5: fast scan, six-item menu and pre-scan plan (sections 5, 9, 10)")
`v5/raw_scan.py` blob sha256 `fe028e68...fb051e2f`, 3430 lines; `v5/selftest.py` blob `7bf1dc05...5004d0c`.

**Important:** while this review was running, another session committed step 6 (`8d89645`,
"JSON signature profiles and a validated output root") into the same working tree. All line
numbers below refer to the **802df9f blob**, which I extracted with `git show` and read from
outside the repo so the numbering is stable. Step 6 inserts 114 lines at line 168, so for the
current working file add 114 to any line number between 168 and 1615 and a larger offset after
that. I re-diffed `802df9f..8d89645` for every line in this review: none of the three blockers
was touched by step 6.

**Gate (run before writing anything, and re-run afterwards):**

| Command | Exit |
|---|---|
| `./python/python.exe v5/selftest.py` | **0** (PASS, also 0 after 8d89645) |
| `./python/python.exe v4/selftest.py` | **0** (PASS) |

**Method.** Read the code, then confirmed every claimed defect with a probe that imports the
pinned copy of `raw_scan.py` from a scratch directory (nothing inside the repo was written or
modified). Probes are quoted inline as P1..P6 and "probe2/probe3". Anything I could not execute
is in section 3 and is labelled as unproven.

---

## 1. What the code actually does

Derived from reading, not from the plan. "Touches" = what it reads or writes outside the process.

| Subsystem | Entry points | Touches |
|---|---|---|
| Signature build | `build_patterns`, `expand_text_signature`, `parse_hex_pattern`, `_ext_from_label`, `MAGIC_SIGNATURES`, `TARGET_SIGNATURES`, `SEARCH_UTF16_BE` | Nothing. Returns `(label, bytes, ext, case_insensitive)` tuples; each text signature becomes one tuple per encoding (UTF-8 + UTF-16LE, BE only if the flag is on). |
| Matching core | `build_pat_list`, `match_block`, `_reportable`, `zero_block`, `read_exact`, `read_window` | Reads only. `match_block` = one `bytes.find` loop per pattern over a block+carry; `_reportable` mutates a caller-owned `last_hit` dict to enforce MIN_GAP per label. `zero_block` caches one `b"\x00"*n` per block size. |
| Phase 1, single process | `scan()` lines 1318-1386 | Opens the source once (`open(target,"rb",buffering=0)`), appends every hit to `results_path`, prints buffered hit lines. Zero-blocks are detected with `==` against the cached zero block and matched only over `carry + max_len` bytes. |
| Phase 1, processes | `parallel_phase1`, `_scan_range_worker`, `parallel_worker_count` | N child processes (`spawn`), each opening its own read handle on the same target and pickling `patterns` in the job tuple; parent merges, sorts, applies MIN_GAP, optionally enforces a `deadline`. |
| Phase 2 carving | `carve_phase`, `CachedReader`, `carve_window`, `smart_extend_text`, `classify_window`, `printable_ratio`, `is_all_zeros` | One forward pass over the source through a 4 MB cache; writes `<label>_0x<16hex>.<ext>`, `<...>.triage.txt`, and for text extensions a second `.txt` copy. |
| Reports | `carved_stats`, `extract_candidate_names`, `print_report`, `write_report`, `write_index_html` | Reads the carve dir; writes a text report. `write_index_html` is never called (dead, see N1). |
| Run folders / provenance | `source_label`, `size_tag`, `run_tag`, `make_run_dirs`, `write_provenance`, `read_provenance`, `find_recoveries`, `provenance_payload` | Creates `recovered_<label>_<size>_<stamp>/carved` and `organized_<...>/by_extension`; writes/reads `provenance.json`; walks SCRIPT_DIR, CWD, last-used root and every volume root (depth 1) looking for `recovered_*`. |
| Organized output | `organize_output`, `write_organized_index` | **Moves** every file from `carved/` into `organized/by_extension/<ext>/`, then writes `index.html` (relative links, no external assets) and `summary.txt` (via `write_report`). |
| Progressive + resume | `slice_bounds`, `merge_summaries`, `run_progressive`, `ask_resume`, `resume_flow` | Calls `scan()` once per slice with `start_offset`/`end_offset`, organises after each slice, rewrites `provenance.json` with `resume_offset` and `status`. Resume rebuilds patterns from `signature_labels` and continues in place. |
| Fast scan | `fast_scan_ranges`, `run_fast_scan`, `fast_sample_names`, `fast_scan_projection`, `fast_report_lines`, `run_fast_flow`, `make_fast_scan_dir` | Calls `scan()` twice (head 5 %, tail 5 %) with `carve_dir=None` and a wall-clock `deadline`; re-opens the source to read ~20 sample windows for file names; writes `fastscan_<tag>/search_results.txt`, `fast_scan_report.txt`, `provenance.json`. No carved output. |
| Pre-scan plan / speed memory | `print_plan_summary`, `estimate_scan_seconds`, `load_speed_rates`, `remember_speed` | Writes `scan_speed.json` in SCRIPT_DIR; reads it to print an ETA and says whether the rate was measured. |
| Pickers | `pick_image`, `pick_folder`, `pick_destination`, `_browse_folder`, `list_image_candidates`, `_volume_entries`, `get_volume_fs` | Walks volumes 2 levels deep for image files, enumerates subfolders by number, calls `GetVolumePathNameW`/`GetVolumeInformationW`, enforces free space, writes `last_paths.json`. |
| Imaging | `create_disk_image`, `run_imaging_flow`, `ask_imaging_mode`, `set_sparse_file` | Reads the source once, writes `dest` (+`.gz/.xz/.zst`), computes SHA-256 while copying, renames to `.partial` on interrupt, writes `<dest>.info.txt`. |
| Menu / entry | `main`, `choose_source`, `ask_target`, `ask_resume` | Startup always offers resume first, then the six-item menu. |

---

## 2. Verified defects

Every item below was either read at the cited line and confirmed by a probe, or is a structural
claim about a call site that I quote. Probes are shown with their real output.

### 2.1 BLOCKERS

**B1 - BLOCKER - `v5/raw_scan.py:1331-1332`, `:1360` vs `:1077` - every bounded scan silently
loses hits that straddle its `end_offset`; progressive scan, resume and the fast scan's head
region all under-report, and the result depends on whether the source is big enough to go parallel.**

```python
1331  to_read = (chunk_size if end_offset is None
1332             else min(chunk_size, end_offset - next_off))     # never reads past end_offset
...
1360  if abs_off < start_offset:                                 # next range drops it
1361      continue
...
1077  read_limit = end + max_len                                 # the worker DOES read past
```

Reasoning: within one `scan()` call the block+carry handles straddles. Across two calls, the range
that *owns* a hit's start never reads far enough to see the pattern completed, and the following
range does see it (it pre-rolls `aligned_start`) but throws it away because `abs_off < start_offset`.
The hit belongs to nobody. The parallel worker does not have the bug precisely because it reads
`end + max_len` — so the sequential and parallel paths disagree on the same input, which is the
"two paths can never drift" claim in `match_block`'s docstring failing one level up.

Probe (probe3), 2003-byte image, single `.md` at offset 1000, split at 1001 (inside the pattern):

```
sequential whole=[1000]  [0,1001)=[]  [1001,end)=[]  union=[]
parallel   whole=[1000]  [0,1001)=[1000]  [1001,end)=[]  union=[1000]
slice boundary: hit at 199, slice [0,200) then [200,end)
   whole            : [199]
   slice 1          : []
   sliCE 2 (resume) : []
```

Consequences, all reachable in the shipped flows:
* `run_progressive` (2015-2018) slices at 5 %: any hit starting in the last `max_len-1` bytes of a
  slice is lost **permanently** — the next slice cannot report it and no later slice re-reads it.
  `max_len` for the default set is 46 (the UTF-16LE form of "Name AGENT_OPERATING_RULES"), so the
  dead band is 45 bytes at every slice boundary, 20 boundaries on a 5 % progressive run.
* `resume_flow` (3124-3147) resumes at exactly such a boundary: same loss.
* `run_fast_scan` (2351-2364) scans `[0, span)` then `[tail_start, total)`: a hit straddling
  `span` is lost, so the fast scan's hit count and its projection are low.
* On a source ≥ `PARALLEL_MIN_SIZE` (64 MB — i.e. every real disk) the same boundary is handled
  correctly by the worker; on a small image it is not. Two fixtures, two answers.

Fix (small, mirrors the worker): when `end_offset` is not None, read
`min(chunk_size, read_limit - next_off)` with
`read_limit = min(total_size, end_offset + max_len) if total_size else end_offset + max_len`,
stop the loop at `next_off >= read_limit`, and add `if abs_off >= end_offset: continue` next to the
existing `abs_off < start_offset` test so the over-read cannot report into the next range. Adjust
`scanned` so the over-read is not counted (see S1).

---

**B2 - BLOCKER - `v5/raw_scan.py:2027-2035`, `:2029` - an interrupted progressive slice is recorded
as `complete` with `resume_offset == total`, so the user cannot resume and the run claims it scanned
everything.**

```python
2027  combined["resume_offset"] = end if end is not None else (...)
2029  finished = end is None or (total and end >= total)      # `interrupted` is never consulted
2035  "status": "complete" if finished else "in progress"
```

`scan()` returns normally after Ctrl+C in the read loop (it catches `KeyboardInterrupt` at 1385 and
sets `interrupted = True`), so `run_progressive`'s own `except KeyboardInterrupt` at 2019 never runs
for that case; the loop goes on to organise, write provenance, and — when the interrupted slice was
the last one — print "The whole source has been scanned." (2043). `ask_resume` then refuses the
folder forever (1969: `status == "complete" and done_at >= done_total`).

Probe P3 (monkeypatched `scan()` returning `interrupted=True`, `bytes_scanned` halved, `percent=100`):

```
   combined interrupted : True
   combined resume_off  : 4096 of 4096
   provenance status    : complete
   provenance resume_off: 4096
   provenance bytes     : 2048
```

2048 of 4096 bytes were read, the provenance says complete, and the console said
"The whole source has been scanned."

Fix:

```python
finished = (not part.get("interrupted")) and (end is None or (total and end >= total))
combined["resume_offset"] = (end if not part.get("interrupted") else
                             start + int(part.get("bytes_scanned") or 0))
status = "interrupted" if part.get("interrupted") else ("complete" if finished else "in progress")
```
and add `break` after an interrupted slice (do not ask "Continue with the next slice?").

---

**B3 - BLOCKER - `v5/raw_scan.py:875` + `:896` - for every signature with extension `txt`, the
"clean readable copy" is written to the *same path* as the raw carved window and destroys it.**

```python
874  fname = "%s_0x%016X.%s" % (safe, abs_off, ext or "bin")     # ext == "txt"
875  path = os.path.join(carve_dir, fname)
...
896  txt_path = os.path.join(carve_dir, "%s_0x%016X.txt" % (safe, abs_off))   # <-- identical when ext=="txt"
897  with open(txt_path, "w", encoding="utf-8", errors="replace") as tf:
```

So the recovered bytes are replaced by `data.decode("utf-8", errors="replace")` re-encoded. Anything
that is not valid UTF-8 becomes U+FFFD and the original byte values are gone — and `<name>.txt` is
the only copy, because no `.bin`/raw duplicate is written.

Probe P1 (23-byte window containing `\xff\xfe` and `\x00`):

```
   carved path      : Extension__txt_0x0000000000000000.txt
   raw window bytes : 23 b'\xff\xfe\x00BADBYTES.txt\x00'
   on-disk bytes    : 7 b'\xef\xbf\xbd\xef\xbf\xbd\x00'
   files in dir     : ['..._0x0000000000000000.txt', '..._0x0000000000000000.txt.triage.txt']
```

Why no test catches it: the selftest's Arabic fixture serialises to UTF-16LE bytes that are all
below 0x80 (`utf-16le bytes: 14 max byte: 72 all < 0x80: True`), so the decode/re-encode round-trip
is byte-identical and the "carved file contains its pattern" check at selftest:144-148 passes.
Real damage is larger than one byte: Windows-1256 Arabic (7 bytes) comes back as 21 bytes of
U+FFFD, and `bytes(range(256))` doubles to 512 bytes.

Affected in the default config: `Extension .txt` and `Hermes marker (.txt)`; the selftest fixture
labels also use `ext="txt"`. Note the `.triage.txt` sidecar is computed from the in-memory window
*after* the file has been overwritten (879-887), so it describes bytes that are no longer on disk.

Fix: name the readable copy differently (`"%s_0x%016X.readable.txt"`), exclude `*.readable.txt`
from `carved_stats`/`write_organized_index`, or simply skip the copy when `ext == "txt"`.

---

### 2.2 SHOULD-FIX

**S1 - should-fix - `:1319-1321`, `:1378`, `:1299-1306`, `:2371` - the fast scan's "sampled" bytes
are inflated by the 4 KiB-aligned pre-roll in sequential mode, contradict the region list printed
right below them, and change with `PARALLEL_WORKERS`.**

`aligned_start` rounds down to `DEVICE_ALIGNMENT`, and `scanned += len(chunk)` counts those bytes,
but the clamp `scanned = min(measured, span)` only exists in the parallel branch (1299-1304).
Probe P4, 4 MiB image, ranges `[(0,209715),(3984589,4194304)]`, union 419430:

```
   sequential sampled: 422707 delta 3277
   parallel   sampled: 419430 delta 0
```

The report then prints `sampled : 412.80 KB` above a region list that adds up to 409.60 KB, and the
progress line for the second region reads `(5.08%)`. The same 4 MiB image gives 412.80 KB with
`PARALLEL_WORKERS=1` and 409.60 KB with 2. `fast_scan_projection.factor = total/sampled` inherits
the error, and `zero_pct` uses the same inflated denominator.
The selftest does not pin this: line 365 accepts `2*head_span <= sampled <= 2*head_span + 3*4096`,
i.e. it encodes the bug as a tolerance (which is why the plan's "sampled_bytes = 10.00 MB exactly"
only holds for the parallel path).

Fix: count only bytes that lie inside `[start_offset, end_offset)`:
`scanned += max(0, min(len(chunk), (end_offset - next_off) if end_offset else len(chunk)))`.
Then delete the `min(measured, span)` special case.

**S2 - should-fix - `:2304-2313` (+ `:2352-2354`) - a fast scan that ran out of budget before
reading anything still tells the user the disk "may be wiped, encrypted or TRIMmed".**

The recommendation branch tests only `not hits` and `zero_pct`; `budget_hit` is used solely inside
the `time` line (2274-2277). Probe P6 (`budget=-1`):

```
  sampled       : 0 B in 2 region(s)
  time          : 00:00:00  (BUDGET REACHED - results are partial)
  hits          : 0
  RECOMMENDATION: no signatures found in the sampled regions.
  The disk may be wiped, encrypted or TRIMmed (SSD), or it may
```

This is the worst failure mode for a triage tool: a false "give up" verdict derived from a sample
that does not exist. Fix: `if summary.get("budget_hit") and sampled_bytes < <minimum interesting>` →
"the time budget expired after N bytes, which is too small for a verdict; raise FAST_SCAN_BUDGET or
scan the image instead", and skip the wiped/TRIM sentence and the projection (S3).

**S3 - should-fix - `:2231-2253`, `:2246-2250` - the projection is labelled "recoverable data" but
is `hits × 68 KiB`, which ignores smart-text extension, ignores that one file yields several
signature hits, ignores overlapping windows, and is still printed for a partial sample.**

```python
2246  window = CARVE_BEFORE + CARVE_AFTER          # 68 KiB, the FULL-scan window
2249  capped = min(estimate, CARVE_LIMIT) if CARVE_LIMIT else estimate
2250  projected += capped * window
```

* `ENABLE_SMART_TEXT` lets a text carve grow to `+1 MiB` (812-848, 866), so for the text signatures
  that dominate the default set the real output is up to 15× the projection. The plan's own
  benchmark recorded "75 files, 73.4 MB" for 25 hits = 2.9 MB per hit, not 68 KiB.
* A file whose name and content match three patterns (`.md` extension + heading + name) is counted
  three times, each time claiming a fresh 68 KiB.
* Adjacent hits share the same bytes; the sum assumes disjoint windows.
* Under `budget_hit`, the density of the head slice is extrapolated to the whole disk with no note.

Observed (selftest fixture): 4 hits in a 4 MiB image that contains exactly three planted strings →
"recoverable data about 2.64 MB".

Fix: rename the quantity to what it is ("carved window bytes if the density holds: about X"), state
that it is `hits × (CARVE_BEFORE+CARVE_AFTER)` and that smart-text hits can be much larger, use the
*fast* 2 KiB/16 KiB window only if the number is meant as a lower bound, and do not print it when
`budget_hit` is set.

**S4 - should-fix - `:1308`, `:1385`, `:1398-1400`, `main()` 3202-3284 - Ctrl+C during phase 1
(parallel) or during phase 2 carving escapes as a traceback and loses the summary, report and
provenance, contradicting the printed promise "Press Ctrl+C to stop early - everything found so
far is kept".**

`except Exception` at 1308 does not catch `KeyboardInterrupt` (it derives from `BaseException`),
and the `except KeyboardInterrupt` at 1385 only wraps the sequential `while` loop. `carve_phase`
(1398) has no handler, and `main()` has none either.
Probe P5 (monkeypatched `parallel_phase1` raising `KeyboardInterrupt`, which is exactly what the
pool does not swallow):

```
   scan() PROPAGATED KeyboardInterrupt (no partial summary)
```

Combined with `run_search_flow`, which only catches `PermissionError`/`ValueError`/`OSError`
(2630-2640), the user gets a stack trace on a 500 GB scan at hour three.

Fix: wrap the parallel block and `carve_phase` in `try/except KeyboardInterrupt` → set
`interrupted = True`, keep `hits`, return the summary; and add a `except KeyboardInterrupt:` handler
in `main()` that prints where the partial results are.

**S5 - should-fix - `:1797-1822` + `:1538` - `summary.txt` in the organized folder always reports
"recovered files : 0 / 0 bytes", because `organize_output` moves the files away *before* calling
`write_report`.**

`write_report` sizes `summary["carve_dir"]`, which is empty by then. Probe P2 (one `.md` hit):

```
   summary.txt  | recovered files : 0
   summary.txt  | recovered total : 0 bytes (0 B)
   index.html   | ... 2 files · 638 B ...
```

The same wrong text is written for every slice (2026) and for the final combined summary (2053),
so the human-readable summary contradicts the browsable index in the same folder.
Fix: call `carved_stats(carve_dir)` before the move loop and pass the numbers into `write_report`
(e.g. `write_report(..., stats=(count,total))`), or count from `by_extension` after the move.

**S6 - should-fix - `:3128-3131` - resume rebuilds its signature set from label strings in
provenance; if the config changed the resumed scan silently uses fewer signatures, and if nothing
resolves it raises an uncaught `ValueError`.**

```python
3128  labels = set(resume_prov.get("signature_labels") or [])
3130  resume_patterns = [p for p in build_patterns(resume_case) if not labels or p[0] in labels]
```
`provenance_payload` stores only `signature_labels` (1763), never the pattern bytes. Rename a label,
switch `SEARCH_UTF16_BE`, or run with a profile (step 6) and the resumed half of the disk is scanned
with a different signature set with no warning; if no label matches, `scan()` raises
`ValueError("patterns must not be empty")` (1184-1185) from `run_progressive` inside `resume_flow`,
which `main()` does not guard → traceback.
Fix: persist `[[label, hex, ext, ci], ...]` in provenance and rebuild from that; print a warning
naming each label that could not be rebuilt; refuse with a message if zero patterns remain.

**S7 - should-fix - `:1992-1996`, `:2030-2036` - a resume overwrites the folder's provenance with
*this run's* totals only, so a folder holding hundreds of hits can end up saying `hits: 0`.**

`combined` is created fresh with `"hits": []`, `"counts": {}`, `"carved_files": 0`,
`"bytes_scanned": 0`, and it is what gets written to the same `provenance.json` (2030-2036,
2053-2061). Only the newly scanned tail is described. `resume_flow`'s closing line
"Recovery updated: %d hits" (3149) likewise prints the tail's count.
Fix: seed `combined` from the existing provenance (`hits_total`, `counts`, `carved_files`,
`bytes_scanned`) and keep `slice_hits` separate, or never overwrite cumulative fields.

**S8 - should-fix - `:2118-2132`, `:2465`, `:2646` - the fast scan and the full scan write their
throughput to the same `scan_speed.json` key, so the full-scan ETA is computed from a rate that
included no carving (and vice versa).**

Both call `remember_speed(source_kind(target), bytes/elapsed)`. A fast scan reads page-cached
slices and writes no files; a full scan's `elapsed` covers matching *and* carving (the plan's own
split: 28.9 s matching of 48.6 s total). Mixing them is how the "PLAN FOR THIS RUN" estimate ends
up 10× optimistic or pessimistic depending on which mode ran last.
Fix: key by mode (`"image:fast"`, `"image:full"`) or store both rates, and compute the full-scan
rate from phase-1 time only, adding a stated carve allowance.

**S9 - should-fix - `:2615` and `:2339` - the output folders are created outside the `try` that
turns `OSError` into a message.**

`make_run_dirs(SCRIPT_DIR, ...)` at 2615 runs before the `try:` at 2620 (and `make_fast_scan_dir`
at 2339 before the `try:` at 2452). A read-only script directory — the normal state of a script run
from a mounted DLC Boot ISO — or a full destination therefore produces a traceback instead of
"cannot write here". (Step 6's `work_root` validation covers only the full-scan path for now.)
Fix: move both calls inside the guarded block, or wrap them in `try/except OSError` with an
actionable message and a fallback root.

**S10 - should-fix - `:1077`, `:1308-1312` - one worker error discards the whole parallel result
and re-reads the range sequentially from byte 0.**

```python
1308  except Exception as exc:      # a broken pool must never lose the scan
1310      print("Parallel scan failed (%s) - falling back to one process." % exc)
1311      del hits[:]                  # everything found so far is dropped
```
The worker also over-reads `end + max_len` (1077), which on the last range of a raw device asks for
bytes past the end of the device — a read Windows can refuse (see section 3). The result would be:
a 5-hour 4-process scan hits an error at the end, throws away all hits, and starts again in one
process. Fix: clamp `read_limit` to the device size, and on failure keep completed ranges and
rescan only the failed one (or fall back without `del hits[:]`).

**S11 - should-fix - `:1201`, `:978-980`, `:2015-2018` - `CARVE_LIMIT` is created fresh per
`scan()` call, so progressive scanning multiplies it by the number of slices.**

`carved_counts = {}` per call; `run_progressive` calls `scan()` 20 times → up to 2000 files per
signature instead of 100, each text window up to ~1 MB. On a 500 GB disk that is a multi-GB
surprise the plan never mentions (Section 13.8 already flagged carved size as the open cost).
Fix: keep `carved_counts` in `combined` and pass it into `scan()`.

---

### 2.3 NITS

| # | Line | Problem | Fix |
|---|---|---|---|
| N1 | 1244 | `range_total` is computed and never used. | Delete. |
| N2 | 1477-1533 | `write_index_html` (57 lines) is never called; it duplicates `write_organized_index` and would additionally hide every `.txt` carve from the index. | Delete. |
| N3 | 3328, 3319, 3329 | `ENABLE_INDEX_HTML`, `REPORT_FILE`, `CARVE_DIR` are config knobs nothing reads — in particular `ENABLE_INDEX_HTML = False` does not stop `index.html` from being written. | Delete, or wire them up. |
| N4 | 1550-1586 | `carved_stats` counts the `.triage.txt` sidecar and the readable `.txt` copy as recovered files: one 319-byte window is reported as "3 files / 816 bytes" (probe2 F). | Skip `*.triage.txt` and `*.readable.txt`; report "windows" and "bytes" separately. |
| N5 | 1266-1268 | In the fast scan, the progress percentage is of the whole source while region 2 is scanned, so a 5 % slice prints `5.08%`. | Print the slice-relative percentage, or label it "position". |
| N6 | 1736 | `dirs[:] = [d for d in dirs if d.startswith(RECOVERED_PREFIX)]` prunes the walk to only `recovered_*` folders, so a recovery nested one level inside any other folder (`D:\case1\recovered_x`) can never be found, although the docstring promises a depth-1 search. | Prune by depth only; filter names when appending. |
| N7 | 2393-2394 | The fast scan's provenance `started` is `datetime.now()` *after* the scan, i.e. the end time (it also overwrites the value `run_tag` used). | Capture the start timestamp before the loop. |
| N8 | 1654 vs 2074 | `source_label` treats any UNC path as a device; `source_kind` treats only `\\.\` as a device. A UNC image gets a device-style label in provenance. | Use one predicate. |
| N9 | 630 | `os.path.getsize(path)` after `_ask_manual_file` is unguarded; a file removed in between gives a traceback. | Wrap in `try/except OSError` and `continue`. |
| N10 | 1560 | The report formats offsets with `0x%010X`; everything else uses 16 digits, so a >4 GiB offset widens the column. | Use `0x%016X`. |
| N11 | 1911 | `merge_summaries` *overwrites* `interrupted` with the last slice's value, and never merges `budget_hit`. | OR both flags. |
| N12 | 1234, 1161, 2365 | `last_hit` (MIN_GAP state) is reset at every slice/region boundary, so a progressive scan can report a near-duplicate pair that a full scan suppresses: hit sets differ between modes by design. | Seed `last_hit` from the previous slice's last accepted offsets, or document that MIN_GAP is per-range. |

### 2.4 Verified correct (checked, not defects)

* Block/carry matching inside one `scan()`: a pattern straddling a 256-byte chunk boundary is found
  exactly once (reproduced by the selftest and by probe2 case C at an exact `end_offset`).
* The parallel worker's `[start, end)` ownership test plus its `end + max_len` over-read is right
  *within one parallel scan*: the straddle at 1001 was reported by the range that owns the start.
* MIN_GAP pre-roll consistency: two hits 100 bytes apart straddling a split, `MIN_GAP=150` → the
  whole file and the two halves all report exactly one hit (probe2 case B). The suppressed hit is
  not resurrected, and `last_hit` is advanced by pre-roll hits exactly as a full scan would.
* Empty source: `scan()` on a 0-byte file returns `hits=[] bytes_scanned=0 total=0` without raising;
  `fast_scan_ranges(0)` → `[(0, None)]`; `run_fast_scan` returns `sampled=0, usable=False`.
* `scan()`'s zero-block skip still finds a pattern that starts in the carried tail of the previous
  block and ends inside the zero block (`hay = combined[:carry_floor + max_len]`).
* Fast scan really reads nothing outside the two ranges (selftest asserts it), really writes no
  carved files (`carve_dir=None`, `carved_files == 0`), and really returns partial hits when the
  budget expires (`budget_hit`, partial `hits`).
* `provenance_payload` → `write_provenance` → `read_provenance` round-trips the fields the selftest
  checks (`source`, `hits`, `signatures`) with `ensure_ascii=False`, so Arabic paths survive.
* `organize_output` moves raw files and their sidecars into `by_extension/<ext>/` and the generated
  `index.html` uses relative links only (no external assets).
* `create_disk_image` is byte-identical and SHA-256-correct on a 31 KB fixture, writes `.info.txt`,
  and renames to `.partial` on interrupt.

---

## 3. Suspicious but unproven (needs a real disk or a bigger fixture)

1. **Over-read past the end of a raw device** (`:1077`). `read_limit = end + max_len` on the last
   range asks the device for bytes beyond its length. Windows usually answers
   `ERROR_INVALID_PARAMETER`/short read. If it raises, S10's fallback discards a finished scan.
   Needs `\\.\PhysicalDriveN`.
2. **`read_window` rounds `a_end` up to 4096** (`:262-263`). For a device whose size is not a
   multiple of 4096, a carve window in the last 4095 bytes reads past the end → `OSError` →
   `"(carve failed: ...)"` instead of a recovered file. Files are unaffected (short read at EOF).
3. **MAX_PATH.** `recovered_<label>_<size>_<stamp>\carved\<label>_0x<16hex>.<ext>` plus
   `organized_...\by_extension\<ext>\<same name>` reaches ~180 characters before the base path; on a
   deep USB folder or with long Arabic labels it can exceed 260. `organize_output` swallows the
   `OSError` (1814-1815) and leaves the file behind, so the index silently misses it. Needs a
   long-path fixture with long-path support disabled.
4. **Zero-block truncation vs all-zero signatures** (`:1100`, `:1348`). A signature consisting only
   of `0x00` bytes can never match inside a zero block. Unreachable with the default set; a
   user-supplied profile (step 6) can create one.
5. **`spawn` start-up cost inside the 5-minute budget.** Four child interpreters each import a
   3400-line module before reading a byte. On a USB 2.0 stick this is a noticeable slice of the
   budget, and the fast-scan ETA does not include it. Needs timing on the target hardware.
6. **Ctrl+C against `ctx.Pool.__exit__` on Windows** — whether the four children are reliably
   terminated, or can keep device handles open after the parent dies. Needs a manual run.
7. **>`2 TiB` offsets on a raw device** with this embedded interpreter: `seek`/`tell` on a device
   handle depends on `SetFilePointerEx`; unverified here.
8. **TRIM reporting on a real SSD.** Only synthetic zero data was used; the `zero_pct >= 90`
   threshold and "of the sampled bytes" denominator have never been compared with a real
   TRIMmed volume.
9. **Free-space / same-disk safety.** `ask_save_location` → `pick_folder` offers every volume,
   including the disk being scanned; the same-disk refusal approved in Section 12 is step 8 and does
   not exist yet. Not a steps 0-5 defect, but the flow already invites the mistake.

---

## 4. Coverage gaps in selftest.py

Each item is written so it can be implemented directly. (1)-(3) fail against 802df9f.

1. **Bounded-range equivalence.** Build a 4 KiB image with `.md` at `split-2`, at `split`, and at
   `split+2`; assert `scan(0,split)["hits"] + scan(split,end)["hits"]` equals `scan(0,end)["hits"]`
   (offsets *and* labels, sorted). Then repeat with `PARALLEL_MIN_SIZE=0, PARALLEL_WORKERS=2` and
   assert the split result is unchanged. This is the test that would have caught B1; the current
   suite only tests `fast_scan_ranges()` as a pure function.
2. **Interrupted slice is not "complete".** Monkeypatch `raw_scan.scan` to return the real summary
   with `interrupted=True` and `bytes_scanned // 2`; run `run_progressive(..., percent=100)`; assert
   `read_provenance(dir)["status"] != "complete"`, `resume_offset < total`, and that
   `ask_resume` (with the folder in `roots`) offers it again.
3. **`ext="txt"` carve is byte-exact.** Carve a window containing `b"\xff\xfe\x00"` and assert
   `open(path,"rb").read() == raw_window` (currently 7 bytes instead of 23).
4. **Report and index agree.** After `organize_output`, assert the file count in `summary.txt`
   equals the file count in `index.html` and equals the number of hits (currently 0 vs 2 vs 1).
5. **Ctrl+C in phase 1.** Monkeypatch `parallel_phase1` to raise `KeyboardInterrupt`; assert
   `scan()` returns a summary with `interrupted=True` instead of raising.
6. **No verdict from an empty sample.** `run_fast_scan(..., budget=-1)`; assert `sampled_bytes == 0`
   and that the report text does **not** contain `"wiped"`.
7. **Exact `sampled_bytes` in both modes.** For a 4 MiB image assert `sampled_bytes ==
   sum(e - s for s, e in ranges)` with `PARALLEL_WORKERS=1` **and** `=2` (tighten the current
   `+ 3*4096` tolerance at line 365).
8. **Projection is not multiplied by signature count.** Fixture with one file matching three
   signatures; assert `projection["projected_bytes"] <= 1.5 * file_size` (today one 400-byte file
   yields 3 × 68 KiB). Also assert the report omits `PROJECTION` when `budget_hit`.
9. **Resume stitches to the full scan.** Full scan of a 3.9 MiB image, then
   `run_progressive(..., resume_from=<provenance resume_offset>)`; assert the union of hit offsets
   equals the full scan's and that provenance keeps the earlier totals (`hits >= <full count>`).
10. **MIN_GAP across a boundary** (locks in the behaviour I verified): two hits 100 bytes apart
    straddling a slice split, `MIN_GAP=150`; assert split == whole.
11. **Error paths.** (a) empty file → no exception, `sampled_bytes == 0`; (b) unknown size
    (`total=0`) → the plan summary and the report say "unknown", no division by zero; (c) results
    directory not writable → a message, not a traceback (assert via a read-only temp dir on POSIX or
    a path under a nonexistent drive on Windows); (d) carve dir cannot be created → hits are
    reported as "carve failed", the scan still completes.
12. **`find_recoveries` depth.** A `recovered_x` inside `tmp/case1/` must be found when
    `tmp/case1` is a root (today `dirs[:]` filtering hides it).
13. **Fast scan in parallel mode.** Run the 4 MiB fast scan with `PARALLEL_MIN_SIZE=0`; assert the
    same hits/offsets, `sampled_bytes`, and `projection` as the sequential run — this exposes B1 at
    `span` and S1 at once.
14. **Windows path forms.** `source_label("\\\\.\\PhysicalDrive0") == "PhysicalDrive0"` and
    `source_kind("\\\\NAS\\share\\x.img") == "image"` (today the label path and the kind path
    disagree, N8); `classify_image_name("X.IMG.PARTIAL") == (".img", True)`.

---

## 5. If I had one hour

Ordered by risk removed per minute. Items 1-3 are the blockers; the rest are cheap.

| # | Minutes | Change | Risk removed |
|---|---|---|---|
| 1 | 10 | B1: over-read `end_offset + max_len` in `scan()`, add `abs_off >= end_offset` filter, add selftest case 1. | Silent, permanent hit loss in every progressive/resume/fast-scan run. Highest damage, smallest diff. |
| 2 | 5 | B3: rename the readable copy to `*.readable.txt` (and skip it in stats/index). | Destroys recovered `.txt` bytes; one-line change. |
| 3 | 10 | B2: `finished` consults `interrupted`; `resume_offset = start + bytes_scanned`; status "interrupted"; break the loop. | A run that claims to be complete and refuses to resume. |
| 4 | 5 | S2: one `if budget_hit` branch in `fast_report_lines`. | Removes the false "disk is wiped" verdict from an empty sample. |
| 5 | 10 | S4: `try/except KeyboardInterrupt` around the parallel block, `carve_phase`, and `main()`. | Ctrl+C on a 500 GB disk stops being a traceback; the promise on screen becomes true. |
| 6 | 5 | S5: compute `carved_stats` before the move and pass it to `write_report`. | The human summary stops contradicting the index. |
| 7 | 10 | S1: count only in-range bytes; drop the `min(measured, span)` special case; tighten selftest 365. | The fast scan's headline number stops depending on `PARALLEL_WORKERS`. |
| 8 | 5 | N1-N3: delete `write_index_html`, `range_total`, and the dead config flags. | Removes a 57-line decoy and a flag that lies about what it controls. |
