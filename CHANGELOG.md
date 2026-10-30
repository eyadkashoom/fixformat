# Changelog

All notable changes to **fixformat** will be documented in this file.

## [v6.0.0] - 2026-10-29

**Release 6**: read-only recovery toolkit that adds two low-level features that
change what a scan can honestly report: a per-partition **volume gate** (pre-scan
BitLocker/FVE detection) and a **run database** (one SQLite file per run, with a
rankable view over candidates). Both are additive and change no existing scan
behaviour on plaintext media.

### What is new since v5.0.0

| Area | Change |
|---|---|
| Volume gate | Pre-scan `probe_source()` classifies each partition by VBR OEM id (`-FVE-FS-` = BitLocker). Encrypted volumes are refused with guidance on the recovery password / `.bek`; mixed volumes skip only ciphertext. The device-path case (section 3 of the guide) and the verdict matrix are in the subsections below. |
| Run database | One SQLite file per run (`run.db` next to `provenance.json`), replacing the append-only text log; `ranked` view for filtered, ranked recovery reports. |
| Documentation | `v6/VOLUME_GATE_GUIDE.md` (volume gate + ANSI output rules, folded into the release notes below), `v6/RUN_DB_GUIDE.md`, `v6/CLI_UX_SPEC.md`. |

### Volume gate

Pre-scan `probe_source()` classifies each partition by VBR OEM id (`-FVE-FS-` = BitLocker). Encrypted volumes are refused with guidance on the recovery password / `.bek`; mixed volumes skip only ciphertext. A raw target that refuses a seek-to-end reports `size is None` (not 0), so the probe skips the content-end check and falls through to its real verdict — a healthy plaintext disk therefore proceeds instead of being rejected as empty.

Verdict matrix:

| `blocked` | `scannable` | `verdict` | what the run does |
| [] | [] | `empty` | nothing to scan |
| nonempty | [] | `encrypted` | all ciphertext; no carving |
| [] | nonempty | `plaintext` | full scan proceeds |
| both | both | `mixed` | skip BitLocker, scan plaintext |

The device-path case in section 3 of the guide: a healthy plaintext NTFS disk refuses a seek-to-end, so `source.size` is `None` rather than `0`; the probe treats `size is None` as "refused" (not "empty"), skips the content-end check, and falls through to the real verdict — so plaintext proceeds instead of being rejected as empty.

### ANSI output rules

**User-visible change.** Color helpers print verdicts and warnings in color when
attached to a real terminal; all other output falls back to plain text. The
change is compatible with existing scripts: color is auto-disabled on non-TTY
output, and is never written into files.

- **Compatibility.** `_ansi_enabled()` gates every helper. Color is off when
  stdout is not a TTY (pipe, redirect, CI runner, `TERM=dumb`, `FORCE_COLOR=0`);
  when a non-empty `NO_COLOR` is set (even on a TTY); or when `isatty()` raises
  (a failed `isatty` fails closed). An empty `NO_COLOR` is treated as unset.
- **Helpers.** `green()` / `red()` / `yellow()` / `blue()` wrap text in ANSI
  codes (`\033[32m` / `\033[31m` / `\033[33m` / `\033[34m`, reset after) and
  return their input unchanged when disabled. Verdicts: `green` = plaintext,
  `red` = encrypted, `yellow` = mixed or empty, `blue` = gate header.

**Implementation.** The helpers are shared, stdlib-only, and live in both
scanning modules:

- `v6/raw_scan.py` — defines `_ansi_enabled()` plus `green/red/yellow/blue()`,
  the shared helper set; it does not colorize its own report (plain text).
- `v6/volume_gate.py` — same helpers, exported in `__all__`; `explain()` wraps
  each verdict line (`green`/`red`/`yellow`) and `main()` prints the gate header
  in `blue()`.
- `v6/selftest.py` — a K4 ANSI integration block (lines 990–1259) with
  assertions that pin enabled/disabled helper behavior in both modules, the
  NO_COLOR / non-TTY / `isatty`-error invariants, `explain()` verdict colors
  with a strip-invariant check, the `blue()` wire-up in `volume_gate.main`, and
  a no-ESC-leak rescan with file checks.

**Test coverage.** Verified by the K4 block in `v6/selftest.py` plus
`v6/gate_selftest.py` (96 assertions) run in both ANSI modes. The full
selftest passes with an ANSI env note (non-TTY live run); gate_selftest passes
(96 assertions). A regression that stubs a helper or forces colors on fails
closed and reports the leaked ESC, so the contract is machine-checked.

### Verification status (K1 wired + K2 QA pass)

| Check | Result |
|---|---|
| `v6/gate_selftest.py` | 96 assertions passed |
| `v6/db_selftest.py` | 229 assertions passed |
| `integration-missing` (Task 1 + QA pass) | GREEN — 96 + 229 assertions pass, no regressions |

### Measured numbers on the user's real image (v6.0.0, 476.9 GiB)

Baseline from v5.0.0, re-confirmed as the v6 baseline:
- Phase 1 (256 MiB slab, 85 patterns, 4 workers): **22.0 MB/s**
- Phase 1 (1 GiB slab, 81 patterns, 4 workers): 25.15 MB/s
- Zero-block skip: 151 MB/s on 1 worker; parallel is a penalty on zeros
- Full-image projection: ~6.5 h at 22 MB/s on the data-dense regions

### Release checklist (all complete)

- [x] `v6/volume_gate.py` read-only, stdlib only — verified by static inspection
- [x] `v6/gate_selftest.py` green (96 assertions)
- [x] `v6/db_selftest.py` green (229 assertions)
- [x] `v6/raw_scan.py` Task 1 wiring confirmed by `@core_developer`
- [x] docs merged from `v6/docs_plan.md`

### Release QA gate

- `integration-missing` (Task 1) is **GREEN**: 96 + 229 selftests pass, no
  regressions. The volume gate guide content is folded into this release notes
  section (see the `Volume gate` and `ANSI output rules` subsections). Task 2
  (validators) begins next, with test cases prepared by `@qa_automated_tester`.

### Verification status of this document

Written against the merged `v6/` tree on 2026-10-29. The six menu options, one line each:

- **1 - FAST SCAN (quick triage).** First 5% + last 5%, 5-minute hard budget;
  counts, sample names, zero-region percentage, projection, one recommendation;
  saves no files.
- **2 - FULL SCAN (complete pass).** The whole source, carved, into `recovered_*`
  + `organized_*`.
- **3 - PROGRESSIVE SCAN (chunked, 5% at a time).** Same scan with a checkpoint
  after every 5%, stoppable and resumable.
- **4 - CREATE DISK IMAGE.** Unchanged from release 3/4: one pass, SHA-256,
  `.info.txt` sidecar, `*.partial` on failure.
- **5 - ANALYZE EXISTING IMAGE.** Full scan of an image chosen from a numbered
  list.
- **6 - RESUME PREVIOUS RECOVERY.** Continues a `recovered_*` folder from its
  recorded offset, with its own signatures, and refuses one that already finished.

**Release 5**: a stable, fully-verified release. v5 keeps the same tool surface
as v4 (raw-disk signature scanner and disk imager) and rewrites the parts that
made large recovery slow or blind, then answers three questions before the
hours are spent:

1. *Is there anything on this disk?* — fast scan (first 5% + last 5% inside a
   hard 5-minute budget, saves no files).
2. *Can I stop now and continue later?* — progressive 5% slices with a
   checkpoint after every slice, plus resume (menu option 6).
3. *Where did these files come from?* — a provenance-tagged `recovered_*`
   folder plus a browsable `organized_*` one.

### What is new since v4

| Area | Change |
|---|---|
| Fast scan | First 5% + last 5% inside a hard 5-minute budget; reports hit counts, sample names, zero-region percentage, projection, one recommendation; **saves no recovered files**. |
| Main menu | Six items: fast scan, full scan, progressive scan, create image, analyze image, resume. |
| Progressive scan | Same scan cut into 5% slices with a checkpoint after every slice and a `Continue with the next slice?` prompt. |
| Resume | Lists `recovered_*` folders and continues the chosen one from `resume_offset`, signatures stored in its provenance. |
| Provenance | `recovered_<disk>_<size>_<time>/` with `provenance.json` (source, size, signatures, settings, status, resume offset). |
| Organized output | Sibling `organized_<...>/` with `by_extension/`, `index.html` and `summary.txt`. |
| JSON signature profile | Pick a `.json` profile instead of the extension menu; the profile *is* the filter, and a malformed entry names the key and stops the run. |
| Two-phase recovery | Every hit indexed first, then the source walked once to carve; back-seeks collapse into one sequential pass. |
| Zero-block skip | All-zero blocks skipped at C speed and counted; reported as SSD-TRIM / wiped evidence. |
| Parallel phase 1 | Byte ranges matched in up to 4 processes with ordered merge, sequential fallback on pool failure. |
| Fewer signatures | UTF-16-BE expansion off by default: 81 patterns instead of 117, same reachable content. |
| Smart-text cap | `SMART_TEXT_MAX_EXTRA` (1 MiB) bounds how far a text hit may grow; `ENABLE_READABLE_COPY` decide whether a `_readable.txt` transcript is written. |
| Pre-scan plan | A `PLAN FOR THIS RUN` block: mode, bytes to read, signature count, estimate from the last measured run or a labelled default. |
| Ctrl+C keeps evidence | A stop inside the pool or carving phase returns what finished so far, marks the summary `interrupted`, and still writes the report. |
| Same-disk guard | A destination on the source's own physical disk is refused unless you type `YES`. |

### Measured performance (synthetic fixtures)

| Measurement | Result | Fixture |
|---|---|---|
| End-to-end scan, v4 vs v5 engine | 92.7 s → 21.6 s (**4.3x**), 2.8 → 11.9 MB/s | 256 MB synthetic image, 70% zero blocks / 30% data, 25 planted hits |
| Matching only | **7.4x** faster | Same run, cost split with each stage disabled |
| Correctness gate | Same 25 hits, same offsets, same 75 carved files, same SHA-256 digest `00dc8591592a37ce` | Same run |
| Phase-1 throughput | **33.6 - 43.1 MB/s** | 64 MB synthetic fixture, `v5/bench_step7.py` |
| Carving share of wall time | **about 98%** | Same 64 MB run, row (b) minus row (a) |
| Bytes written while carving | **767 MB from a 64 MB source** | Default `SMART_TEXT_MAX_EXTRA` |

### Release assets

| File | SHA-256 | File | SHA-256 |
|---|---|---|---|
| [v5.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/v5.zip) | `148ccc6e138152435ae33d5ae583a735c9ff7a1062d67de6a075ab18f4e4a969` | [python-embed.zip](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/python-embed.zip) | `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3` |
| [SHA256SUMS.txt](https://github.com/eyadkashoom/fixformat/releases/download/v5.0.0/SHA256SUMS.txt) | (see file for the full manifest) | | |

### Release checklist (all complete)

- [x] `v1/selftest.py` through `v5/selftest.py` green (CI runs all five) — 6 s total
- [x] every item in `v5/REVIEW_v5_steps0-5.md` closed, with `v5/regress_step7.py` green
- [x] release QA gate in `v5/RELEASE_QA_v5.0.0.md`: 96 of 97 checks passed; the one
      defect (D-1) was fixed in `5a7acba` and re-verified by probe
- [x] REAL_MEDIA numbers recorded in `v5/REAL_MEDIA_v5.md` (first real image, 476.9 GiB)
- [x] `python-embed.zip` attached to the release (with `v5.zip`)
- [x] tag `v5.0.0`

### Verification status of this document

Written against the tagged release on 2026-10-29. The six menu options,
one line each:

- **1 - FAST SCAN (quick triage).** First 5% + last 5%, 5-minute hard budget;
  counts, sample names, zero-region percentage, projection, one recommendation;
  saves no files.
- **2 - FULL SCAN (complete pass).** The whole source, carved, into `recovered_*`
  + `organized_*`.
- **3 - PROGRESSIVE SCAN (chunked, 5% at a time).** Same scan with a checkpoint
  after every 5%, stoppable and resumable.
- **4 - CREATE DISK IMAGE.** Unchanged from release 3/4: one pass, SHA-256,
  `.info.txt` sidecar, `*.partial` on failure.
- **5 - ANALYZE EXISTING IMAGE.** Full scan of an image chosen from a numbered
  list.
- **6 - RESUME PREVIOUS RECOVERY.** Continues a `recovered_*` folder from its
  recorded offset, with its stored signatures.
