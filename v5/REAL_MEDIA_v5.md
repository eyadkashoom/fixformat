# v5 on real media — first measurement against a real interrupted disk image

**Measured 2026-10-29** against commit `7e2216a` ("v5: refuse a destination on the source's own physical disk (section 12)").

`v5/raw_scan.py` was **byte-identical for every run below**: sha256
`7f5510a162bb22f15261ff7d37949b8ccc048fd09602ad732c1cac732923be12` (4235 lines), verified
immediately before and immediately after each timed run (`sha_changed=False` in every record).
Slab rows 2–7 were measured while HEAD was `fcc292a`; Section 12 was committed as `7e2216a`
during the session without touching `raw_scan.py`, so the hash above covers all rows.

Target (read-only, `'rb'`, never written, never renamed):

    F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial   512,107,741,184 bytes

Machine: Windows 11, 8 logical CPUs (auto worker count = 4), 31.8 GiB RAM, F: = NTFS 4.0 TB volume,
`fsutil sparse queryflag` → **"This file is NOT set as sparse"**, allocated size = logical size
(100.00%), so the zeros in the image are real zeros on disk, not holes.

Every number below was printed by the run itself with `time.perf_counter()` and is also stored, one
JSON object per run, in `F:\hard512\v5_real_test\measurements.jsonl`.

---

## 1. Results

| # | What / command | bytes scanned | workers | seconds | MB/s | hits | notes |
|---|---|---|---|---|---|---|---|
| 1 | `open_target(image)` + first 512 B | 512 B head | – | 0.005 | – | – | size == `os.path.getsize` == 512,107,741,184; MBR `55AA` present; boot code sector; GPT protective entry (type `0xEE`, first LBA `0x100`, size `0xFFFFFFFF`) at `0x1BE`; no `NTFS    ` OEM string (expected: this is a GPT disk, not NTFS-in-MBR) |
| 2 | slab A 0–1 GiB, `PARALLEL_WORKERS=None` (first ever access, cold) | 1,073,741,824 | 4 | 62.841 | 17.09 | 8899 | zeros 436,207,658 B = 40.63%; 81 patterns; `min_gap=0`; `carve_dir=None` |
| 3 | slab A, `PARALLEL_WORKERS=None` | 1,073,741,824 | 4 | **42.700** | **25.15** | 8899 | fastest auto run; same 8899 hits |
| 4 | slab A, `PARALLEL_WORKERS=1` | 1,073,741,824 | 1 | **129.099** | **8.32** | 8899 | hit list **identical** to row 3 (label+offset, 8899 == 8899) |
| 5 | slab A, auto again (page cache warm) | 1,073,741,824 | 4 | 62.483 | 17.18 | 8899 | identical again; 46 % slower than row 3 — run-to-run spread is large |
| 6 | slab B 100–101 GiB, auto | 1,073,741,824 | 4 | 9.589 | 111.98 | **0** | 100.00 % all-zero; 0 hits |
| 7 | slab B, `PARALLEL_WORKERS=1` | 1,073,741,824 | 1 | **7.105** | **151.13** | 0 | 1 worker is **35 % faster** than 4 here |
| 8 | slab B, auto again | 1,073,741,824 | 4 | 9.365 | 114.66 | 0 | identical (empty) hit list |
| 9 | plain `f.read()` 256 MiB, cold @25 GiB | 268,435,456 | – | 1.638 | 163.92 | – | device speed with no pattern matching |
| 10 | plain read 256 MiB, cold @250 GiB | 268,435,456 | – | 2.182 | 123.05 | – | |
| 11 | plain read 256 MiB, cold @50 GiB | 268,435,456 | – | 1.863 | 144.05 | – | |
| 12 | plain read 256 MiB, warm @0 / @100 GiB / @0.5 GiB | 268,435,456 ea | – | 0.440 / 0.415 / 0.433 | 610 / 646 / 620 | – | page cache (these regions had just been scanned); not device speed |
| 13 | `run_fast_scan(..., budget=120, base_dir=F:\hard512\v5_real_test)` | 4,043,296,768 (3.77 GiB = **0.79 %** of source) | 4 | 121.491 | 33.28 | 4248 | `budget_hit=True`; zeros 436,207,616 = 10.79 %; tail region never read; recommendation = "too little to judge anything" (verbatim in §6) |
| 14 | 256 MiB slab, phase 1 only (`carve_dir=None`) | 268,435,456 | 4 | 9.547 | 28.12 | 618 | carving baseline for row 15 |
| 15 | 256 MiB slab, carving: `carve_limit=20`, `carve_before=4096`, `carve_after=65536`, `SMART_TEXT_MAX_EXTRA=65536`, `ENABLE_READABLE_COPY=False` | 268,435,456 | 4 | **11.809** | 22.73 | 618 | 65 windows written + 65 `.triage.txt` sidecars = 130 files, **5,136,508 B** total (4.90 MiB) = **ratio 0.0191 of the source**; phase 2 ≈ 2.26 s = **19 %** of wall time |
| 16 | `organize_output()` on row 15 | – | – | – | – | – | moved 65; `summary.txt` line 715: `recovered files : 65` (and `recovered total : 5124315 bytes (4.89 MB)`) — not 0 |
| 17 | `run_progressive` 0–2 GiB, `percent=25` (512 MiB slices), answer "n" after slice 1 | 536,870,912 | 4 | 15.192 | 35.3 | 84 | prov: `hits=84`, `bytes_scanned=536870912`, `status=interrupted`, `resume_offset=536870912`, `carved_files=55` |
| 18 | `run_progressive` resumed from 536,870,912, "y" ×3 | 1,610,612,736 | 4 | 64.856 | 24.8 | 1913 (this pass) | prov **accumulates**: `hits=1997` (84 + 1913), `bytes_scanned=2147483648` (2 GiB exactly), `status=complete`, `resume_offset=2147483648`, `zero_bytes=436,215,934` (20.3 %) |
| 19 | bug probe: `scan(range=0–25 GiB, deadline=now-1)` auto | **512,107,741,184 claimed** | 4 | 0.71 | – | 0 | reports 100.0 % of the *whole source* for a 25 GiB range (see §8) |
| 20 | same probe, `PARALLEL_WORKERS=1` | 0 | 1 | 0.01 | – | 0 | sequential path reports 0, i.e. only the parallel path lies |

**Phase-1 parallel speed-up on real dense data: 129.099 / 42.700 = 3.02×** (4 workers vs 1).
Against the slower repeat of the same auto configuration it is 2.07×. The 4.3× synthetic figure was
not reproduced.

**Zero/TRIM fraction actually sampled:** slab A 40.63 % (436,207,658 of 1,073,741,824 B), slab B
100.00 %, fast-scan sample 10.79 %, 2 GiB progressive run 20.32 %.

---

## 2. Read-only open (step 1)

```
open_target() size : 512107741184   (476.94 GB)
os.path.getsize    : 512107741184
declared == real   : True
sectors (512)      : 1000210432
secs               : 0.004971
```

First 512 bytes (abridged to the interesting lines):

```
00000000  33 c0 8e d0 bc 00 7c 8e c0 8e d8 be 00 7c bf 00  3.....|......|..
00000040  b4 41 bb aa 55 cd 13 5d 72 0f 81 fb 55 aa 75 09  .A..U..]r...U.u.
00000160  24 02 c3 49 6e 76 61 6c 69 64 20 70 61 72 74 69  $..Invalid parti
000001B0  65 6d 00 00 00 63 7b 9a 00 00 00 00 00 00 00 00  em...c{.........
000001C0  02 00 ee fe ff 33 01 00 00 00 ff ff ff ff 00 00  .....3..........
000001F0  00 00 00 00 00 00 00 00 00 00 00 00 00 00 55 aa  ..............U.
```

**Verdict:** real disk image, readable by the tool. `55 AA` at offset 510 ✔. The 446-byte boot code is
the standard Windows `bootmgr`/`ntldr` MBR stub ("Invalid partition table" / "Error loading operating
system" / "Missing operating system" strings). The one partition entry at `0x1BE` is type `0xEE`
(GPT protective, first LBA `0x100`), so the disk is **GPT** — that is why sector 0 has no
`NTFS    ` OEM string: NTFS lives in the first partition, not in the MBR sector.

---

## 3. Phase 1 slabs: what real throughput looks like

Same command, three times per slab, `carve_dir=None`, `min_gap=0`, 81 patterns:

| slab | workers | seconds | MB/s | hits | zero % |
|---|---|---|---|---|---|
| 0–1 GiB (dense) | 4 | 62.841 (cold first touch) | 17.09 | 8899 | 40.63 |
| 0–1 GiB | 4 | 42.700 | 25.15 | 8899 | 40.63 |
| 0–1 GiB | 1 | 129.099 | 8.32 | 8899 | 40.62 |
| 0–1 GiB | 4 | 62.483 | 17.18 | 8899 | 40.63 |
| 100–101 GiB (all zero) | 4 | 9.589 | 111.98 | 0 | 100.00 |
| 100–101 GiB | 1 | 7.105 | 151.13 | 0 | 100.00 |
| 100–101 GiB | 4 | 9.365 | 114.66 | 0 | 100.00 |

Top hit labels in the 0–1 GiB slab (identical in all three runs):
`JSON Data (.json) [utf-8] 8675`, `JPEG image (.jpg) 78`, `Markdown heading LF (.md) [utf-8] 37`,
`Extension .md [utf-8] 34`, `GZIP file (.gz) 32`, `Extension .py [utf-8] 30`,
`Extension .log [utf-16-le] 8`, `JSON Data (.json) [utf-16-le] 4`.

The two configurations produced **byte-identical hit lists** — 8899 (label, offset) pairs each,
compared as sorted lists, not just counts.

Note what those 8899 hits are: 8675 of them come from `JSON Data (.json)`, which is the two-byte
pattern `":`. On random bytes a 2-byte needle occurs ~16,384 times per GiB, and 8675 is 53 % of that,
so the bulk of the "hits" is statistically indistinguishable from noise in high-entropy data (the
slab is 0.39 % zeros at the probe points, i.e. incompressible).

**Throughput is not uniform, and the difference is CPU, not the disk.** A plain `f.read()` of the same
256 MiB at offset 0 (cold, uncached) runs at 163.92 MB/s, and a cold read anywhere else on the image
is 123–164 MB/s; the all-zero slab reaches 151 MB/s. Only the data-dense slab is slow: 25.15 MB/s
with 4 workers, 8.32 MB/s with 1. So on this hardware phase 1 is **~6.5× slower than the device when
4 processes share the work, and ~20× slower with one process** — the matcher, not the media, is the
ceiling. That is exactly the shape a CPU-bound phase 1 has, and it is why parallelism pays here
(3.0×) and *hurts* on the zero slab (0.74×, i.e. 35 % slower with 4 workers than with 1).

I first measured 610–646 MB/s for the same reads; those regions had just been scanned, so those
numbers are page-cache hits (31.8 GiB RAM holds the two 1 GiB slabs), not the device. The cold figures
in row 9–11 are the honest ones, and the second read of @250 GiB (239.14 MB/s vs 123.05 cold) shows
the same cache effect on a 256 MiB window.

---

## 4. Zero / TRIM report (step 4)

8 MiB probes through `read_window()`, 15 points across the 476.9 GiB image:

| position | offset | zero % |
|---|---|---|
| 0 % | 0x0000000000000000 | 79.19 |
| 1 % | 0x00000001313D7000 | 0.39 |
| 5 % | 0x00000005F6333000 | 0.39 |
| 10 % | 0x0000000BEC666000 | 0.39 |
| 20 % | 0x00000017D8CCC000 | 100.00 |
| 30 % | 0x00000023C5333000 | 100.00 |
| 40 % | 0x0000002FB1999000 | 100.00 |
| 50 % | 0x0000003B9E000000 | 100.00 |
| 60 % | 0x000000478A666000 | 100.00 |
| 70 % | 0x0000005376CCC000 | 100.00 |
| 80 % | 0x0000005F63333000 | 100.00 |
| 90 % | 0x0000006B4F999000 | 100.00 |
| 95 % | 0x0000007145CCC000 | 100.00 |
| 99 % | 0x000000760AC28000 | 0.45 |
| 100 % | 0x000000773B800000 | 100.00 |

Edge hunt (8 MiB probes): data at 11/12/13/14 %, **100 % zeros from 15 % to 19 %**, zeros through
96 %, **data again at 97.00–99.75 %** (0.35–0.66 % zeros), 99.75 % zero, 99.90 % data,
100 % zero. The last 1 MiB of the file is **100.0000 % zeros** (a 4 MiB window at the
last-64 MiB mark is also 100.0000 % zeros).

Interpretation: this is **not** a cleanly zero-tailed partial image. Data is dense at 0–14 %, absent
from ~15 % to ~96 %, present again from ~97 % to the last few MiB, and the final 1 MiB is all zero.
That pattern (dense head, huge zero middle, dense tail, zero final bytes) is consistent with an
imaging run that was interrupted at the very end while the middle of the source disk already
contained large unallocated/never-written regions — it is *not* consistent with "the imaging stopped
at 14 % and the rest was never written", because the 97–99.9 % band holds real, high-entropy data
(0.4 % zero) that a never-written tail could not have.
The tool's per-run zero accounting also explains the slabs: 40.63 % of the dense 0–1 GiB slab is
zero, 100 % of the 100–101 GiB slab is zero.

---

## 5. Fast scan (step 5, `budget=120`, `base_dir=F:\hard512\v5_real_test`)

Ranges sampled — the first and last 5 % of the source, as configured:

```
[(0, 25605387059), (486502354125, 512107741184)]
```

Measured:

```
wall seconds    : 121.491
sampled bytes   : 4043296768 (3.77 GiB)   = 0.79 % of the source
ranges          : [(0, 25605387059), (486502354125, 512107741184)]
hits            : 4248
zero_bytes      : 436207616 (10.79 % of sampled)
budget_hit      : True
projection      : projected_hits 538035, projected_bytes 132300800.0, usable True
                  (suppressed in the report: sample smaller than 1 % of the source)
top labels      : JSON Data (.json) [utf-8] 3187, JPEG image (.jpg) 227, Extension .py [utf-8] 217,
                  GZIP file (.gz) 200, Markdown heading LF (.md) [utf-8] 195,
                  Extension .md [utf-8] 194, Extension .log [utf-16-le] 8, ...
```

What it actually sampled: only the **first region**, and only its first ~4 GB of 23.85 GB. The deadline
fired inside region 1, the loop `break`s, and region 2 (the last 23.85 GB) is never opened — yet the
report lists both regions as sampled.

Recommendation text, verbatim from `fast_scan_report.txt`:

```
------------------------------------------------------------------------------
RECOMMENDATION: the 00:05:00 budget ran out after 3.77 GB of the source -
  that is too little to judge anything. Raise FAST_SCAN_BUDGET or
  run the full scan (menu option 2). This is NOT a wiped-disk verdict.
------------------------------------------------------------------------------
```

Note the budget label: the run was given `budget=120` (the report's own `time` line says `00:02:01`)
but the message quotes the module constant `FAST_SCAN_BUDGET` (300 s). The wording is right, the
number is not.

**Verdict on the fast scan:** the branch it reached is the honest one ("too little to judge
anything", projection suppressed, not a wiped-disk verdict). It is honest because it hit the budget,
not because it had enough data: 0.79 % coverage at 33.28 MB/s means the configured 10 % sample
(51 GB) needs ~26 min here, not 2 min and not the default 5 min. On this image the fast scan's
default coverage is unreachable inside its own budget, so 1 GiB-scale triage always ends in the
"too little" branch unless the budget is raised to ~30 min.

---

## 6. Carving on real data, conservative (step 6)

256 MiB slab at offset 0, `carve_limit=20` per label, `carve_before=4096`, `carve_after=65536`,
`SMART_TEXT_MAX_EXTRA=65536`, `ENABLE_READABLE_COPY=False`, `ENABLE_SMART_TEXT=True`,
`ENABLE_TRIAGE=True`:

```
seconds=11.809 scanned=268435456 MB/s=22.73 hits=618 carved(reported)=65
files on disk=130 bytes=5136508 (4.899 MiB) ratio=0.019135
file size min/median/max = 185 / 69635 / 135176
top labels: [(553, 'JSON Data (.json) [utf-8]'), (40, 'JPEG image (.jpg)'),
             (8, 'Extension .log [utf-16-le]'), (7, 'GZIP file (.gz)'),
             (6, 'Markdown heading LF (.md) [utf-8]'), (4, 'JSON Data (.json) [utf-16-le]')]
organize_output -> moved=65 index=True summary=F:\hard512\v5_real_test\carve_conservative_organized\summary.txt
SUMMARY| recovered files : 65
SUMMARY| recovered total : 5124315 bytes (4.89 MB)
```

* 618 phase-1 hits, 65 windows written — the 20-per-label limit did all the capping (553 JSON hits
  became 20 files).
* **Ratio to source: 0.0191** (5.14 MB written from 256 MiB read). The synthetic 64 MB fixture
  produced 767 MB from 64 MB (≈12×); on real data with the same code the ratio is 60× smaller,
  because the cap is 64 KiB of smart-text extension, not the 1 MiB default that dominated the
  synthetic run.
* **Carving is not 98 % of wall time here.** The same slab with `carve_dir=None` takes 9.547 s
  (28.12 MB/s); with carving it takes 11.809 s, so phase 2 is ≈2.26 s ≈ **19 %** of wall time (the
  difference: fixed windows, 65 files, 5 MB instead of 767 files/767 MB).
* `organize_output()` reports `recovered files : 65` (summary.txt line 715) — the S5 fix holds on
  real data; it is not 0.
* A real quirk of `min_gap=0`: 18 of the 20 JSON windows were spent inside a **366-byte** span
  (`0x566011`–`0x56617F`), i.e. 20 near-duplicate windows of the same region. With
  `min_gap=0` the per-label carve budget can be exhausted within a few hundred bytes; the real
  progressive path uses `MIN_GAP` (1 MiB) and does not show this.

---

## 7. Resume / progressive (step 7)

2 GiB range, `percent=25` → 512 MiB slices (`slice_bounds(2 GiB, 25, 0) = (0, 536870912)`),
`_ask` monkeypatched (`raw_scan._ask = fake_ask`) to answer the continue prompt.

Run 1 (`"n"` after the first slice):

```
RUN1 seconds=15.192 pass_hits=84 pass_scanned=536870912 stopped_early=True resume_offset=536870912
RUN1 prov: hits=84 bytes_scanned=536870912 status=interrupted resume_offset=536870912 carved=55
```

Run 2 (`resume_from=536870912`, `"y"` three times):

```
RUN2 resume_from=536870912 seconds=64.856 pass_hits=1913 pass_scanned=1610612736
RUN2 prov: hits=1997 bytes_scanned=2147483648 status=complete resume_offset=2147483648 carved=179 zero=436215934
RUN2 counts: [(1495, 'JSON Data (.json) [utf-8]'), (104, 'Markdown heading LF (.md) [utf-8]'),
              (99, 'Extension .md [utf-8]'), (98, 'Extension .py [utf-8]'),
              (97, 'JPEG image (.jpg)'), (90, 'GZIP file (.gz)')]
accumulated: 84 -> 1997 (hits) ; 536870912 -> 2147483648 (bytes_scanned); grew=True
```

**Hits accumulate, they do not reset:** 84 + 1913 = 1997 exactly, `bytes_scanned` 512 MiB + 1536 MiB
= 2048 MiB exactly, `status` goes `interrupted` → `complete`, `resume_offset` ends at the range end.
`zero_bytes` (436,215,934 = 20.32 % of 2 GiB) is also cumulative across the two passes.

---

## 8. Defect found on real media: the parallel path reports the whole source as scanned when it read nothing

`scan()` does:

```python
scanned = found.get("scanned") or total_size
```

where `total_size` is the size of the **whole** source. If the parallel phase returns
`scanned == 0` — a deadline that fires before the first block, or a worker that dies — the summary
claims the entire source was scanned. Measured on the real image, `start_offset=0`,
`end_offset=25 GiB`, `deadline=now-1`:

```
workers=None eff=4  range=0..26843545600  bytes_scanned=512107741184  hits=0 budget_hit=True seconds=0.71  -> claimed 100.0% of the source
workers=1    eff=1  range=0..26843545600  bytes_scanned=0            hits=0 budget_hit=True seconds=0.01  -> claimed 0.0% of the source
```

Consequences seen live: `fast_scan_report.txt` printed

```
  sampled       : 476.94 GB in 2 region(s)
  hits          : 0
  per signature : none
  zero regions  : 0.0% of the sampled bytes (0 B)
RECOMMENDATION: no signatures found in the sampled regions.
  The disk may be wiped, encrypted or TRIMmed (SSD), or it may
  hold file types whose signatures are not configured here.
```

for a source that held 4248 hits in the same 2-minute budget on the very next run. That is a
false-negative, wiped-disk-shaped verdict produced by an accounting fallback, and it is written to
`provenance.json` as `sampled_bytes`/`bytes_scanned`. The sequential path is correct (0). The same
fallback can push `resume_offset` past the end of the sliced range in `run_progressive`
(`resume_offset = start + bytes_scanned`).

Also observed: the fast-scan budget message hard-codes `FAST_SCAN_BUDGET` (300 s) instead of the
`budget` actually passed (120 s), and the report lists both sample regions as "sampled" after the
deadline stopped it inside region 1.

---

## 9. Section 12 (same physical disk) on real hardware

```
C:\                                    -> 0
F:\                                    -> 1
real image (F:)                        -> 1
output folder (F:)\v5_real_test        -> 1
repo raw_scan.py (C:)                  -> 0
same_physical_disk(image, F:\v5_real_test) = True
same_physical_disk(image, C:\)            = False
```

`confirm_same_disk(image, F:\v5_real_test)` with `_ask` answering `"n"`:

```
REFUSED - THAT FOLDER IS ON THE SAME PHYSICAL DISK AS THE SOURCE
  source disk : PhysicalDrive1
  this folder : PhysicalDrive1
  Writing recovered data there can overwrite the very data this scan
  is trying to recover. Use a different physical disk or a share.
  (A different drive letter on the same disk does not help - that is
   exactly what this check catches.)
RETURNED: False
```

With `"YES"` the same call prints `Overriding the refusal at your explicit request.` and returns
`True`. The check works on real hardware: it resolves a file path on F: to PhysicalDrive1, the same
disk the image itself lives on, and refuses by default.

Because direct API calls (`scan(carve_dir=…)`, `run_progressive(recovered_dir=…)`,
`run_fast_scan(base_dir=…)`) do not go through `ask_output_root`, every measurement above wrote to
`F:\hard512\v5_real_test` without an override; only the flow would have prompted.

---

## 10. What surprised us

1. **The bottleneck is the matcher, not the disk.** Cold plain reads run at 123–164 MB/s; phase 1 on
   data-dense media runs at 25.15 MB/s (4 workers) and 8.32 MB/s (1 worker). The tool is 6.5–20×
   slower than the device it is reading. On a 476.9 GiB source that is the difference between "under
   an hour" and "over five hours".
2. **Parallelism only pays on dense data.** 3.02× on the data-dense 1 GiB slab, but **0.74×** on the
   all-zero slab: 4 workers took 9.589 s where 1 worker took 7.105 s. On a mostly-zero image the
   default auto-parallelism is a 35 % penalty plus process overhead.
3. **The 4.3× synthetic speed-up did not reproduce** (3.02× best case, 2.07× against the repeat run),
   and repeated runs of the *same* configuration on the *same* slab varied 42.700 → 62.483 s (46 %).
   Single numbers from this project should carry that spread from now on.
4. **Zero-heavy is 6× faster than data-heavy**: 151 MB/s vs 25 MB/s, because an all-zero block skips
   the 81-pattern scan and the cost collapses to IO. Any "MB/s" quoted without the zero fraction is
   not comparable to any other number.
5. **The image is not a cleanly truncated image.** Data at 0–14 %, zeros 15–96 %, data again
   97–99.9 %, all-zero final 1 MiB. The dense tail band is not something an interrupted run could
   leave behind if it had simply stopped writing.
6. **Carving went from 98 % of wall time to 19 %** with the same feature set, just because the
   smart-text cap was 64 KiB instead of 1 MiB and the data produced 65 windows instead of 767.
7. **The dominant real-world "hit" is a 2-byte needle.** `JSON Data (.json)` is the pattern `":`
   (two bytes, `raw_scan.py` `TARGET_SIGNATURES`), and it produced 8675 of 8899 hits in the first
   GiB — inside data that is 0.39 % zeros and high-entropy. On random bytes a 2-byte needle appears
   about once every 65,536 bytes, i.e. ~16,384 times per GiB; 8675 is 53 % of that, so most of those
   "JSON" hits are indistinguishable from noise, not documents. With `carve_limit=20`, 18 of the 20
   JSON windows then landed inside 366 bytes of one another. Hit counts from real media are far more
   skewed, and far less meaningful, than the synthetic fixtures suggested.
8. **`fast_scan_report.txt` can print a wiped-disk verdict for a disk with thousands of hits**, via
   the `or total_size` fallback (§8). It happened on the first real fast scan of the session.
9. **The fast scan cannot reach its own default coverage.** 51 GB at 33 MB/s is ~26 min; the default
   budget is 5 min and even 120 s only bought 0.79 %. The "too little to judge" branch is therefore
   the *normal* outcome on real media, not an edge case.
10. **`organize_output` now reports real numbers** (`recovered files : 65`, `recovered total :
    5124315 bytes`), so the S5 fix is confirmed outside the fixtures.
11. **Section 12 catches a real hazard on this machine**: F: (image) and F:\hard512\v5_real_test
    (the natural output folder) are both PhysicalDrive1; C: is PhysicalDrive0.

---

## 11. What this does not prove

* **A `.partial` image is not `\\.\PhysicalDrive0`.** Reading a file through `CreateFile`/`read()`
  is not the same path as a raw device: `get_device_size()` fell back to `seek`/`tell` because
  `DeviceIoControl(IOCTL_DISK_GET_LENGTH_INFO)` on a file handle returned 0 (verified directly), no
  4096-byte-aligned device reads were required, and the
  `chunk_size % DEVICE_ALIGNMENT` guard was never hit.
* **No TRIM, no bad sectors, no USB.** The source was a healthy file on an NTFS volume; a real
  failing disk, a USB bridge that drops out mid-read, or an SSD returning TRIM zeros would all look
  different. In particular the 100 % zero slabs here are *file* zeros, not device zeros from TRIM.
* **Page cache is in every one of these numbers.** 31.8 GiB of RAM means the two 1 GiB slabs can be
  fully cached: the second auto run was 46 % faster than the first for that reason, and the 610–646
  MB/s reads are cache hits, not device speed. Rows 3/5 and 6/8 are the same command twice on
  purpose.
* **Only two slabs and one 256 MiB carving slab were sampled** out of 476.9 GiB, i.e. 0.5 % of the
  source got a full-density measurement. The uniform-zero conclusion for 100–101 GiB holds only for
  that GiB; the 8 MiB probe map shows the image is not uniformly anything.
* **Resume was proven across two processes, not across a crash.** Both progressive runs were driven
  in one interpreter with a monkeypatched `_ask`; killing the process mid-slice (SIGKILL, power loss)
  is a different path to the same provenance file.
* **Carving was measured with a conservative configuration only** (`carve_limit=20`,
  `SMART_TEXT_MAX_EXTRA=65536`, readable copies off). The defaults (100/label, 1 MiB, readable
  copies on) were not run against this image, so the write-volume worst case on real media is still
  unmeasured.
* **The parallel path was measured with 4 workers on 8 logical CPUs, auto = 4.** Nothing here says
  how it scales to 8, 16, or a machine where the disk is the bottleneck rather than the matcher.
* **Windows file-system behaviour (NTFS compression, caching, prefetch, antivirus) was not excluded.**
  The `fsutil` checks confirm the file is not sparse and not compressed, but a filter driver could
  still sit in the read path.

---

## 12. What was left on disk

`F:\hard512\v5_real_test` (700 KB total) — cited, kept:

* `measurements.jsonl` — every timed run as a JSON object (the raw evidence for §1)
* `fastscan_..._102215\fast_scan_report.txt`, `provenance.json`, `search_results.txt` — §5
* `carve_conservative_organized\summary.txt` (`recovered files : 65`), `index.html` — §6
* `prog_test\provenance.json` — §7

Deleted: the carved windows and their triage sidecars, the organized copies, all `hits_*.json` and
`results_*.txt` dumps, the two contaminated fast-scan folders from the first (invalid) step-5
attempt. The image itself was never opened for writing, moved or renamed.
