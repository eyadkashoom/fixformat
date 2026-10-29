# fixformat - Version 6 Plan (analysis for review, nothing implemented)

Status: **planning only**. No code in `v6/` yet, no change to `v5/`. Release 5.0.0
is tagged and shipped; `main` carries one 5.1 change (the `":` JSON needle).

This document is the answer to five proposals for release 6, plus a study of
Scalpel, Foremost and the fragmented-carving literature. Every proposal is judged
against measurements from the user's own disk image, not against intuition. Where
a number is a projection it says so.

---

## 0. The verdicts, up front

| # | Proposal | Verdict |
|---|---|---|
| 1 | Parallel paths, N blocks on N cores | **already in v5** - 3.0x measured on real media; the remaining work is *adaptive* parallelism, not more threads |
| 2 | Skip everything except cluster boundaries | **measured false on this disk** - 0 of 61 real hits start on a 4096 boundary; a cluster-stride scan would find nothing |
| 3 | File-system metadata triage (MFT, Inodes) | **the highest-value item in the list** - and not what v4 declined. A triage map in seconds, not a full MFT recovery |
| 4 | mmap / Direct I/O | **mmap measured and rejected** (96-98% of the time is the matcher); Direct I/O worth an option, with bounded expectations |
| 5 | Entropy triage | **partly in v5** (zero-skip + window triage); using it to *skip* regions in a recovery tool would throw away the files people want back |

The one thing none of the five proposals addresses is the thing that decides
whether a recovered file is usable: **validation**. A carver that reports bytes
is a byte copier. A carver that proves a candidate *is* a JPEG is a recovery
tool. That is where v6 should spend its effort, and the tools below show why.

---

## 0b. The finding that rewrites the plan (measured 2026-10-29)

The two probes on the user's image changed the shape of this release. Everything
below is verified twice: by the probe agents and independently by the parent.

**The volume that matters is BitLocker-encrypted.**

| Fact | Value | How it was read |
|---|---|---|
| GPT | intact at LBA 1, 6 partitions | MBR stub with one `0xEE` entry, `EFI PART` at LBA 1 |
| partition 2 | 461.09 GiB, LBA 1,288,192 .. 968,261,631, "Basic data partition" | GPT entry |
| its VBR, offset 3 | `-FVE-FS-` (BitLocker), first sector `eb 58 90 2d` | 512-byte read |
| its entropy | 7.99-8.00 bits/byte at +0, +1, +8, +20, +40, +64, +66, +68 GiB | 64 KiB samples |
| its real content | ends near +68 GiB; zeros from +69 GiB to the end | 64 KiB sweep |
| plaintext NTFS volumes | only the three OEM ones: 1.14 GiB WinRE, 12.08 GiB Dell factory, 2.00 GiB vendor rescue | VBR `NTFS    ` |
| files on those volumes | 417 real files, 11.14 GiB, all OEM/recovery payload | MFT walk, cross-checked against each `$I30` |

Three consequences, in order of importance:

1. **This image cannot yield the user's old data.** The MFT, `$Recycle.Bin`,
   `System Volume Information`, `$UsnJrnl` and every file of the system volume
   are inside the ciphertext. No key exists in the image: the ESP holds no
   `.bek`, and no FVE metadata block was found. Recovery of that volume needs the
   48-digit recovery password or a `.bek` file, and then a decrypting layer
   (`manage-bde` on a mounted VHD, or dislocker) - outside a carver's reach.
2. **Section 4's kill criterion fired.** MFT triage stays in the plan, but not as
   this image's headline: it delivers 417 OEM files here and would deliver a real
   user's files *only on a plaintext volume*. Its priority drops below the FVE
   gate and the validators.
3. **The 8,675 "noise hits" of 5.0.0 were ciphertext, not markup.** A two-byte
   needle matches once every 65,536 random bytes, i.e. ~16,384 times per GiB. On
   the first data-dense GiB of this image, 8,675 of 8,899 hits came from the
   removed `":` needle - 53% of the theoretical random rate - and that region is
   0.39% zeros with 7.99-8.00 bits/byte, which is now identified as BitLocker
   ciphertext. The signature was not matching markup; it was matching encryption
   at close to the rate chance predicts. That is the strongest argument yet for
   the validators in Section 8, and it is now evidence instead of intuition.

**Also confirmed: the image is holey.** It is a real 512,107,741,184-byte file
(not sparse), but the data partition is zeros past ~68 GiB. Every "476.94 GiB"
statement in Section 1 must be read as "the first ~68 GiB of the data volume plus
~15 GiB of plaintext OEM volumes". The rates measured there are still valid - they
were measured on real data - but the whole-image projections were extrapolations
over mostly zero.

### What v6 now does first: the FVE gate

One 512-byte read per partition, before any scanning:

```
[FVE] partition 2 (461.09 GiB) is BitLocker-encrypted (-FVE-FS-).
      Carving it would produce false positives only: encrypted data has no
      headers and no structure.
      What you need: the 48-digit recovery password, or a .bek key file, or the
      key in your Microsoft account.
      What this run can still do: the plaintext partitions (1.14 + 12.08 + 2.00 GiB)
      -> 417 files, 11.14 GiB, all OEM payload.
```

Rules for the gate: detect, never guess a key, never attempt decryption, and never
let the scan of an encrypted volume produce candidates that look recoverable.

## 1. Baseline: what v5.0.0 actually does on this disk

Measured on `F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial`
(476.94 GiB, an interrupted image of the user's system disk). Full detail in
`v5/REAL_MEDIA_v5.md`.

| Measurement | Value | Where |
|---|---|---|
| phase 1, 256 MiB slab, 85 patterns, 4 workers | **22.0 MB/s** | this plan, `align_probe.py` |
| phase 1, 1 GiB slab, 81 patterns, 4 workers | 25.15 MB/s | REAL_MEDIA R3 |
| phase 1, same slab, 1 worker | 8.32 MB/s | REAL_MEDIA R4 |
| parallel speed-up, real data | **3.02x** | REAL_MEDIA R4 |
| all-zero 1 GiB slab | 151 MB/s on 1 worker, 112 MB/s on 4 (parallel is a *penalty*) | REAL_MEDIA R6/R7 |
| plain read of the same image | 123-164 MB/s cold | REAL_MEDIA R9-R11 |
| matcher's share of phase-1 wall time | **96-98%** | mmap probe, 8 runs |
| mmap slicing vs `read()` | 300-500 MB/s vs 200-400 MB/s raw - irrelevant at 22 MB/s total | mmap probe |
| carving, 256 MiB slab, conservative settings | 65 windows, 5.1 MB written = **0.019x source**, 19% of wall time | REAL_MEDIA R15 |
| hits that start on a 4096-byte boundary | **0 of 61 (0.0%)** | this plan, `align_probe.py` |
| the old `":` JSON needle | 8,675 of 8,899 hits were noise | REAL_MEDIA R2 |

Two consequences follow, and they run through everything below:

1. **The disk is not the bottleneck; the pattern matcher is.** Reading the image
   raw is 5-7x faster than scanning it. Anything that only improves I/O (mmap,
   Direct I/O) buys at most ~5% here.
2. **The output is only as good as the signatures.** One two-byte needle buried
   8,675 real hits in noise. Validation and signature quality matter more than
   any speed trick in the list.

Time budget for the whole 476.94 GiB image, from the measured rates:

| Case | Rate | Whole image |
|---|---|---|
| data-dense regions only | 22 MB/s | ~6.5 h |
| this image as measured (~80% zeros, blended) | ~69 MB/s | ~2.1 h |
| data-dense, 25-pattern profile (projection) | ~75 MB/s | ~1.9 h |

The blended figure is arithmetic from the two measured rates and the measured
zero fraction, not a run.

---

## 2. Proposal 1 - parallel paths

**Already implemented and measured.** `parallel_phase1()` splits the range into
per-worker spans and runs `_scan_range_worker` in separate *processes* (threads
would not help: `bytes.find` holds the GIL). Measured 3.02x on the real image
(8.32 -> 25.15 MB/s with 4 workers), with byte-identical hit lists.

What is genuinely left:

| Gap | Evidence | Plan for v6 |
|---|---|---|
| worker cap is `min(4, cpu)` | this machine has 8 cores | make it `min(8, cpu)` and **measure**; the matcher is memory-bandwidth hungry, so expect well under 2x from 4 -> 8 |
| parallel *hurts* zero-heavy regions | 9.6 s on 4 workers vs 7.1 s on 1 for an all-zero GiB | adaptive: start a range with 1 worker, sample the first 8 MiB, and only fan out if the sample is not all-zero. Cheap and measured |
| every worker re-opens the file and re-aligns | code | open once in the parent and inherit the handle where the platform allows it, or keep as is if the gain does not justify the fragility |
| no shared knowledge between ranges | - | a per-run zero map (bitmap) so the carve pass and the report can say "this region was empty" without re-reading it |

Kill criterion: if 4 -> 8 workers gives less than 1.3x, keep 4 and say so in the
release notes. Threads are not on the table for phase 1; they may be useful for
phase 2 (carving), which is I/O-blocked, not CPU-blocked.

---

## 3. Proposal 2 - "signatures always start at cluster boundaries"

**This is false, and the measurement on the user's own disk is unambiguous.**

```
256 MiB slab of the real image, current 85-pattern set:
  hits                            61
  start on a 4096-byte boundary    0   (0.0%)
  start on a 512-byte boundary     0   (0.0%)
  start on a 65536-byte boundary   0   (0.0%)
```

With a 4096 stride the scan would have reported **nothing at all**. The reason is
structural, not luck:

* A cluster boundary is a property of the *file system's allocation*, not of the
  data. A JPEG's `FF D8 FF` lands wherever the writer happened to put it inside
  the cluster, and after a format, a partial overwrite or a compressed container
  the original alignment is gone.
* Carving exists precisely to ignore the file system (Scalpel's paper states the
  same premise: the data is recoverable *regardless* of the filesystem, even if
  the metadata was destroyed). A stride of 4096 re-introduces an assumption the
  technique was invented to drop.
* The proposal's own premise ("headers are always at the start of a block") is
  true for one case only: a file that was written on a freshly formatted volume
  and never deleted. That is not the case this tool is for.

What cluster knowledge *is* good for, and what v6 should do with it:

1. **Window alignment, not scan stride.** Carve windows are rounded to cluster
   boundaries so a saved file does not begin mid-cluster (a small correctness win
   for NTFS-aware viewers).
2. **A free/allocated map.** `$Bitmap` tells which clusters the *current* file
   system owns. Carving normally targets unallocated space; using the map to
   *prioritise* (not to exclude) is a real speed path: on this image, most of the
   data-dense regions may be currently allocated, and scanning allocated clusters
   at lower priority would cut hours.
3. **Cluster run lists from MFT records** (see proposal 3) - that is where
   cluster boundaries genuinely matter, because a file's own run list is a list
   of cluster ranges.

Verdict: **reject the stride**, adopt the three uses above.

---

## 4. Proposal 3 - file-system metadata triage (the one to build)

This is the only proposal that can turn hours into seconds, and it is *not* the
feature v4 declined. v4's decision was "no full MFT recovery" (reconstructing
files from MFT records alone, including resident data). What is proposed here is
a **triage map**: read the metadata that survives, and use it to aim the scan and
name its output.

What is readable on an NTFS volume, cheapest first:

| Structure | What it gives | Cost |
|---|---|---|
| `$MFT` record 0 and the record array | per file: name, parent, size, 4 timestamps, run list (first cluster + length), allocated/deleted flag | one pass over ~1 KB per file; 400k files = ~400 MB = seconds at 150 MB/s |
| `$Bitmap` | which clusters are currently allocated | 1 bit per cluster: 476 GiB / 4 KiB = 122M clusters = 15 MB |
| `$UsnJrnl:$J` | a change journal: file create/delete/rename records, often *after* the MFT record was reused | tens to hundreds of MB; the single best source of "what was deleted recently" |
| `$LogFile` | transaction log, needs replay logic | expensive, defer |
| `$Secure`, `$Extend` | ACLs, quota, reparse points | not needed for recovery |

The delivery order that makes the user's case work:

1. **Triage pass (minutes, before any carving).** Open the image, find the NTFS
   boot sector (`NTFS    ` at offset 3), read `$MFT`, `$Bitmap`, `$UsnJrnl:$J`.
   Build: `name -> {size, timestamps, run list, allocated}` and a deleted-file
   list from the journal.
2. **Direct recovery where possible.** For every record whose run list is intact
   and whose clusters are *not* overwritten, the file can be copied out with its
   real name and full length - no carving, no guessing, no false positives.
3. **Aim the carve.** Records whose run list is broken but whose name and size
   survive become the expected list: the carver can search for a *specific* file
   size and type instead of every signature everywhere.
4. **Name the output.** Every carved candidate gets the best matching MFT name
   within ±1 cluster of the predicted location, instead of the heuristic
   `NAME_RE` scan of neighbour bytes that v5 uses today.

**The one thing to prove before building it**: on *this* image, how much of the
old metadata actually survived the reinstall? The image was taken after Windows
was reinstalled and the disk reformatted, so the current `$MFT` is the new one.
Old records may survive as unallocated MFT entries (`FILE` signature, records not
yet reused). That is measurable in an hour: scan the image for `FILE` records
with a valid USA fixup and count how many reference names that no longer exist in
the current MFT.

* If the survival rate is decent (>5% of old records), triage is the headline
  feature of v6.
* If it is near zero, v6 keeps the journal work as an optional extra and the
  headline becomes validation + fragment assembly instead.

Kill criterion is explicit, and it is measured before a line of v6 is written.

---

## 5. Proposal 4 - mmap and Direct I/O

**mmap: measured and rejected for phase 1.** Not an opinion - eight runs on a
96 MB fixture:

```
read() chunks        200-400 MB/s
mmap slicing         300-500 MB/s   (1.2-2.4x the read)
full phase 1           6-11 MB/s     (the matcher)
matcher share          96-98% of wall time
```

Making the read 2x faster when the read is 2-4% of the time changes nothing.
mmap is also impossible on `\\.\PhysicalDriveN`, which is the tool's main target.

**Direct I/O is a different question and worth an option.** `FILE_FLAG_NO_BUFFERING`
avoids filling the OS page cache with 476 GiB of image data that will never be
read again - on a machine that is also running the user's desktop, that matters
for everything *except* the scan's own speed. Expected gain on the scan: small
and bounded by the matcher (see baseline). Expected gain on system
responsiveness during a 2-hour run: real.

If adopted: sector-aligned buffers (4096), `CreateFileW` with
`FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN`, and a fallback to buffered
reads when alignment cannot be met. Measure before/after on the real image; keep
it only if the system stays responsive and the scan does not get slower.

---

## 6. Proposal 5 - entropy triage

Already in v5, in two places: the all-zero block skip in `scan()` (that is why
zero regions run at 151 MB/s) and `classify_window()` in the carve phase
(TEXT / BINARY / COMPRESSED / ZERO, from Shannon entropy + printable ratio).

The proposal extends it to *skip* high-entropy regions during the scan. Two
measurements say do not:

1. **High entropy is where the good files are.** On this image the data resumes
   at 97.0-99.9% with 0.35-0.66% zeros, and JPEG/PNG/ZIP hits came from exactly
   those regions (40 of 61 hits in the alignment probe were JPEG). JPEG and ZIP
   are high-entropy *by nature*. Skipping high entropy skips the photos.
2. **Entropy does not make the matcher cheaper.** `bytes.find` over random bytes
   costs the same as over text; only *zeros* are cheap (a single `memcmp`).
   A skip rule would have to read and classify the block first, so it adds work
   to remove work.

Where entropy *is* worth using in v6:

* **A map, not a gate**: report per-region entropy in the run's provenance so the
  operator can see where the data is, and so the next release can prioritise.
* **A limit on carving effort**: do not smart-extend a text window into
  high-entropy data (v5 already stops at 70% printable, which is the same idea).
* **A signal that a region is encrypted**: an encrypted volume has near-8.0
  entropy everywhere and no headers; saying so in the report saves the user hours
  of hoping.

Verdict: keep entropy as metadata and as a window classifier; never as a stride
over the source.

---

## 7. What Scalpel and Foremost actually are

Both are **header/footer carvers**: a configuration file lists, per file type, a
header (and optionally a footer or a maximum size); the tool scans the image for
headers and copies bytes to a footer. Scalpel is a performance rewrite of
Foremost by Golden G. Richard III and Vassil Roussev (2005); its paper is explicit
that a file's data **must be contiguous** to be carved correctly, and lists
reassembly of fragmented files as *future* work, not a feature. Neither tool
validates what it carves beyond matching the header and footer, and neither
handles fragments.

What v6 should take from them:

* **`audit.txt`**: one machine-readable line per carved file with its offset,
  size and type. v5 writes a results log and a per-run `provenance.json`, but a
  single audit is what makes a recovery auditable in an afternoon.
* **Per-type output folders**: v5 has `by_extension/`, which is the same idea and
  is better for browsing; keep it, and add the audit.
* **Config-driven signatures with header *and* footer *and* max size**: our
  profile format (Section 7 of the v5 plan) has the header side only. Adding
  `footer` and `max_size` to a profile entry is a small, high-value extension.

What v6 should not copy: their assumption of contiguity, and their refusal to
validate. Our own real-media run is the argument - 8,675 "hits" that were a
two-byte needle matching noise.

---

## 8. Fragmented carving: the algorithms, and what to adopt

Two canonical families, plus the primitive both need:

**Bifragment Gap Carving (BGC, Pal & Memon).** Assume the file is exactly two
fragments separated by an unknown gap. For gap sizes 1..N, concatenate the first
fragment, skip the gap, append the second, and **validate** the result. The
smallest gap whose concatenation validates is accepted. Cheap and effective for
JPEG/PDF, limited to two fragments.

**SmartCarving (Pal, Sencar & Memon, DFRWS 2008).** Classify every block by
content type, then use **sequential hypothesis testing** to decide whether the
next block belongs to the file being assembled; search the collected blocks for
the next fragment when it does not; greedy search with alpha-beta pruning to keep
the branching factor finite. Reported result: of the fragmented JPEGs in the
DFRWS 2006 and 2007 carving challenges, all but one were recovered. It is the
reference design for this problem.

**Object validation (Garfinkel).** Both of the above stand on it: a candidate is
accepted only if it passes a format-specific validator - JPEG marker walk to
`FF D9` with valid segment lengths, PNG chunk walk to `IEND` with correct CRCs,
PDF `startxref`/`xref` consistency, ZIP central directory entry count vs local
headers, OOXML (`[Content_Types].xml` + `word/document.xml`).

What v6 should adopt, in this order (each step is useful alone):

1. **Validators first.** Per format, a function `validate(bytes, path_hint) ->
   score`. Even with no fragmentation support this removes the noise class that
   produced 8,675 false hits, and it turns "a carved window" into "a file".
2. **Sequential assembly for the formats that matter** (JPEG, PNG, PDF, ZIP,
   OOXML, SQLite): from a validated header, walk forward block by block, and when
   the walk breaks, search the next 64 clusters for a block that continues the
   structure. Bounded, predictable cost.
3. **BGC as the fallback** for the two-fragment case, using the validators from
   step 1 as the acceptance test.
4. **Confidence, not certainty.** Each recovered file carries
   `{validated: yes/no, fragments: n, gap_bytes: n, confidence: 0..1}` in the
   audit, so a user can sort by evidence instead of guessing.

Deliberately out of scope for v6: full SmartCarving with content classification
and alpha-beta pruning. It needs a block classifier trained per type, and it is
a research project in itself. v6 should implement the sequential pass and BGC,
which cover the common cases, and leave the rest for v7 with measurements.

---

## 9. The v6 pipeline

```
0. TRIAGE      (seconds)   NTFS boot sector -> $MFT, $Bitmap, $UsnJrnl:$J
                           -> name map, allocated map, deleted-file shortlist
                           -> direct recovery where the run list is intact
1. INDEX       (hours)     the v5 parallel signature pass, unchanged in principle:
                           adaptive workers, validated hits only, zero map kept
2. ASSEMBLE    (minutes)   sequential continuation per validated hit, bounded
                           window; BGC for the two-fragment case
3. REPORT      (seconds)   audit.txt (offset, size, type, fragments, confidence),
                           by_extension/, index.html, provenance.json
```

Steps 0 and 3 are cheap and independent; step 1 already exists; step 2 is the new
engineering. That ordering means v6 can ship incrementally: triage alone is a
release, validators alone are a release.

---

## 9b. The run database (the user's question: one database per search?)

**Yes - one SQLite file per run**, in the run folder next to `provenance.json`.
`sqlite3` 3.49.1 ships in the bundled interpreter, so this costs no dependency and
works from the DLC Boot stick.

What a run produces today: an append-only text log, one JSON, a carved folder, an
organised folder and one static page. Nothing can be filtered, ranked, deduplicated
or compared between runs.

```sql
CREATE TABLE run (id INTEGER PRIMARY KEY, tool_version TEXT, started TEXT,
                  source TEXT, source_size INTEGER, mode TEXT, signature_set TEXT,
                  workers INTEGER, status TEXT, resume_offset INTEGER);
CREATE TABLE region (run_id INTEGER, start INTEGER, length INTEGER, zeros INTEGER,
                     entropy REAL, kind TEXT);          -- data | zero | compressed
CREATE TABLE candidate (id INTEGER PRIMARY KEY, run_id INTEGER, offset INTEGER,
                        length INTEGER, label TEXT, ext TEXT, sha256 TEXT,
                        validated INTEGER, validator TEXT, confidence REAL,
                        fragments INTEGER, gap_bytes INTEGER,
                        name_guess TEXT, name_source TEXT, mft_record INTEGER,
                        carved_path TEXT, triage TEXT);
CREATE TABLE fragment (candidate_id INTEGER, ordinal INTEGER, offset INTEGER, length INTEGER);
CREATE TABLE signature (run_id INTEGER, label TEXT, hex TEXT, ext TEXT, hits INTEGER);
CREATE VIEW ranked AS
  SELECT * FROM candidate ORDER BY validated DESC, confidence DESC, length DESC;
```

| Question the user will ask | Today | With the database |
|---|---|---|
| "Have I already recovered this file?" | compare folders by eye | `sha256` index, one query |
| "Only validated files over 100 KB" | impossible | `WHERE validated=1 AND length>102400` |
| "Which MFT-named deleted file was never carved?" | impossible | join against the name map |
| "What changed since the last run?" | re-read reports | `run_id` diff |
| "Where is the data on this disk?" | nothing | the `region` table is the zero/entropy map |
| "Resume without corrupting the index" | re-parse text | transactions, one commit per slice |

Cost: ~200 bytes per candidate, so 100,000 candidates is 20 MB, against carved data
measured at up to 12x the source on text-heavy media. `index.html` becomes a
generator over queries, and `report.md` comes from the same rows.

---

## 9c. Whole files before carving: shadow copies and the recycle bin

The user asked about "ظل الملف". **When shadow copies exist they are the best
source there is**: whole files, real names, real sizes, no carving and no
confidence score.

| Source | What it gives | Where it lives | Difficulty |
|---|---|---|---|
| **VSS** | point-in-time snapshots: earlier versions of every file, including files deleted since | `System Volume Information`; the catalog and the per-snapshot store files are named `{3808876b-c176-4e48-b7ae-04046e6cc752}`, and a store keeps the original blocks in 16 KiB units | **high** - copy-on-write block map per snapshot |
| **Recycle Bin** | deleted files that went through Explorer: original path, size, deletion time and the content | `$Recycle.Bin\<SID>\$I*` (8-byte version 1 or 2, 8-byte original size, 8-byte FILETIME at 0x10, then the UTF-16LE path; v1 is 544 bytes fixed) and `$R*` holds the content | **low** - a short parser |
| **$UsnJrnl:$J** | create/delete/rename records with names, often surviving after the MFT record was reused | a stream in `$Extend` | medium |
| **$MFT** | names, sizes, timestamps, run lists; straight copy when the run list is intact | record array from record 0 | medium (Section 4) |

Order of operations, because it pays to be lucky first: the MFT walk from
Section 4 finds `System Volume Information` and `$Recycle.Bin` for free, so list
them, recover whole named files, and only then run signatures over what is left.

**Reality check for this image**: it was taken *after* the reformat and the
Windows reinstall, so any shadow copy in it belongs to the new install and cannot
hold pre-format data. VSS still earns its place for the general case, and the
`$I`/`$R` and `$UsnJrnl` work sits on the same MFT reader - but it will not
resurrect the old disk from this image, and the release notes must say so.

Feasibility probe (read-only, minutes): walk the MFT to `System Volume Information`
and to `$Recycle.Bin`, list their children, and report whether any VSS store or
catalog file exists and how many `$I`/`$R` pairs are present.

## 9d. Discovery vs extraction: the two modes, the cap question, and the planner

The cap is **not a constant** and not a hidden rule. The user decides, at the
moment the question is useful, and the choice is recorded in the run database so
a later reader knows why some files exist and others do not.

### The mode question (STEP 1)

```
How should this run work?
  [1] EXTRACT AS WE GO
      Scan and carve in the same pass. You get files immediately, and the run
      needs free space equal to what it carves.
  [2] REPORT FIRST, EXTRACT LATER
      Scan only: write the database and a report (report.md / .html / .csv) with
      what is on the disk, by type and size. Nothing is carved, so it needs no
      space at all. Afterwards a RECOVERY PLAN screen lets you choose what to
      extract, to fit the space you actually have.
```

Mode 2 is the answer for a 477 GiB image on a machine with 2 TB free and no
patience: the scan costs hours but no bytes, the report is ~20-100 MB, and the
decision of *what* to recover becomes a separate, cheap, repeatable step.

### The cap question (asked when the first file is about to exceed the cap)

```
The output cap is about to be reached (25 GB of 100 GB, 100,000 files).
What should happen?
  [1] STOP CARVING, KEEP SCANNING
      Every hit is still recorded with its offset and stays in the database, so
      it can be carved later without rescanning. Nothing is lost.
  [2] CARVE ONLY VALIDATED FILES FROM NOW ON
      Keep going, but only files that pass a validator, in priority order.
  [3] STOP THE WHOLE RUN AND SAVE A CHECKPOINT
      Exit cleanly; resume later with option 6.
```

Whichever is chosen is written to `run.cap_policy` and quoted in the report, so
"why are there only 2,000 of 8,000 hits in this folder" has an answer.

### Stop and resume are always available, not a mode

`P` pauses between blocks, `Q` stops and writes the checkpoint, and every stage
(phase 1, carving, assembly, extraction) commits to the database as it goes. A
stopped run resumes in either mode: the scan continues from `resume_offset`, and
the extraction continues from the set of candidates already carved.

### The recovery plan screen (mode 2, and after any cap)

Built from the database, not from the folder:

```
==============================================================================
RECOVERY PLAN - what is on this source, and what will fit
==============================================================================
  candidates        : 8,412 found, 6,105 validated
  total carved size : 41.2 GB if everything is extracted
  by type           : .jpg 3,120 (28.4 GB)   .pdf 410 (6.1 GB)
                      .docx 88 (1.2 GB)      .md 2,004 (410 MB)   other ...
  destinations      : C:  30.0 GB free | F: 2.2 TB free

  Extract which of them?
    [1] everything that fits          -> 41.2 GB, needs F:
    [2] validated only                -> 29.8 GB
    [3] documents only (.pdf/.docx/.md/.txt) -> 7.7 GB
    [4] my own filter (type / size / date / confidence)
  Split the extraction across several disks?[y/N]
```

Rules the planner enforces:

* never plans more than `min(25% of the destination's free space, 100 GB)`
  (`OUTPUT_CAP_PERCENT = 25`, `OUTPUT_CAP_GB = 100`), and never more than the
  free space minus `FREE_RESERVE_MB = 2048`;
* stops at `OUTPUT_CAP_FILES = 200000` files, because the index and the
  browsing page stop being usable long before the disk does;
* shows the *actual* number of bytes it will write, computed from the candidate
  table, not estimated from hit counts;
* writes the selection rule into `plan` and into the run's `provenance.json`, so
  a partial extraction is explainable months later;
* extracts in priority order (validated first, then confidence, then size), so a
  run that is interrupted again still delivered the best files first.

### Schema additions

```sql
ALTER TABLE run ADD COLUMN mode TEXT;          -- extract-as-we-go | report-first
ALTER TABLE run ADD COLUMN cap_policy TEXT;    -- stop-carving | validated-only | checkpoint
CREATE TABLE destination (run_id INTEGER, path TEXT, free_bytes INTEGER, planned_bytes INTEGER);
CREATE TABLE plan (id INTEGER PRIMARY KEY, run_id INTEGER, rule TEXT,
                   planned_bytes INTEGER, done_bytes INTEGER, created TEXT);
```

### Cost

| Mode | Output written | Notes |
|---|---|---|
| extract as we go | up to the cap (default 25% of free space, max 100 GB) | what v5 does, now bounded and explained |
| report first | database + `report.md`/`.html`/`.csv`, 20-100 MB at 100k candidates | the scan itself is the cost in hours |
| extraction (stage 2) | only what the plan selected | can be repeated, can be split across disks |

## 10. Decisions needed before implementation

| # | Decision | Status |
|---|---|---|
| D1 | Build the MFT/`$UsnJrnl` triage pass? | **APPROVED, but demoted**: the probe found the data volume encrypted (0b), so triage pays off only on plaintext volumes. Still worth building - after the FVE gate |
| D2 | Cluster stride instead of byte-exact scanning? | **REJECTED by measurement** - 0 of 61 real hits were aligned |
| D7 | Ask whether the machine is in use, and map it to workers + priority (2.1) | **APPROVED** by the user |
| D8 | One SQLite database per run (9b) | **APPROVED** by the user - `sqlite3` is in the bundled interpreter |
| D9 | Shadow copies and the recycle bin as whole-file sources before carving (9c) | **APPROVED** by the user; the probe sets the VSS scope |
| D10 | Entropy as a map and a sampler, or as a skip gate? | map + sampler, **never a gate** (Section 6) |
| D11 | Cap behaviour: fixed rule or asked at runtime? | **APPROVED: asked at runtime**, three answers (9d), recorded in `run.cap_policy` |
| D12 | One mode or two (`extract as we go` / `report first`)? | **APPROVED: two modes** (9d); report-first needs no output space and always ends at the planner |
| D13 | Stop/resume available in every stage? | **APPROVED: always** - `P` pauses, `Q` checkpoints, both modes resume |
| D3 | Fragmented assembly scope | JPEG, PNG, PDF, ZIP/OOXML, SQLite; sequential + BGC, no SmartCarving |
| D4 | Direct I/O option | yes, as an opt-in, kept only if the real-image measurement shows no slowdown |
| D5 | Output budget per run | **APPROVED by the user**: `OUTPUT_CAP_GB = 100`, `OUTPUT_CAP_PERCENT = 25` of the destination's free space (whichever is smaller), `OUTPUT_CAP_FILES = 200000`, `FREE_RESERVE_MB = 2048`. Behaviour at the cap is asked at runtime, not fixed (D11). |
| D6 | Still one file per version (`v6/raw_scan.py`)? | **yes**, for the DLC Boot USB; split only if it passes ~6,000 lines |

---

## 11. Implementation order (each step ships on its own)

| Step | Work | Why here |
|---|---|---|
| 0 | `v6/` from `v5/`, `VERSION = "6.0"`, v5 selftest green in `v6/` | a green base before any change |
| 1 | `audit.txt` + the profile `footer`/`max_size` fields | small, independent, immediately useful |
| 2 | Validators for JPEG/PNG/PDF/ZIP + a `confidence` field, attached to carve results | kills the noise class; prerequisite for everything in Section 8 |
| 3 | ~~The MFT survival probe~~ **DONE** (0b): the volume is BitLocker, so the answer is 'unreadable', not 'clean' | measurement complete |
| 3b | **The FVE gate**: detect BitLocker per partition, refuse to carve ciphertext, explain the key path | now the first feature of v6 - it protects every later feature from producing confident nonsense |
| 4 | Triage pass: MFT + `$Bitmap` + `$UsnJrnl:$J`, name map, direct recovery | the biggest win if the probe passes |
| 4a | Walk the MFT to `$Recycle.Bin` and `System Volume Information`; parse `$I`/`$R`; probe for VSS stores | cheap, and it hands back whole named files before any carving |
| 4b | If the probe finds stores: a VSS reader (catalog + 16 KiB block map) | high effort, decide on the probe result |
| 4c | The run database (9b), the two modes and the recovery plan (9d), `report.md`/`.csv` | every later feature writes into it, so it lands before assembly |
| 5 | Adaptive parallelism (1 worker on zero-heavy ranges, 8 on data) | measured penalty removed |
| 6 | Sequential assembly, then BGC, behind validators | the fragmented-file feature |
| 7 | Direct I/O option, measured | bounded gain, system responsiveness |
| 8 | Tag `v6.0.0`, release with `python-embed.zip` | same ceremony as v1-v5 |

---

## 12. What would make this plan wrong

Stated in advance so it can be checked, not argued afterwards:

* If the MFT survival probe finds almost no old records, **Section 4 collapses**
  and v6 is "validators + assembly + adaptive parallelism" instead.
* If validators reject more than ~90% of JPEG-header hits on the real image, the
  validator is wrong (JPEG headers are common in data), not the image.
* If 4 -> 8 workers gives under 1.3x, parallel scaling is finished and the
  release notes say so.
* If Direct I/O makes the scan slower on the real image, drop it (Section 5).

Every one of those is a measurement on the user's own image, and every one can be
run before the first line of v6 code.
