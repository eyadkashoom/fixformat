# fixformat v6 — Volume Gate Technical Guide

**Target:** developers and QA, v6 pre-scan integration.  
**Companion files:** `v6/volume_gate.py`, `v6/gate_selftest.py`, `v6/raw_scan.py` (pre-scan hook, now wired).  
**Status:** IMPLEMENTED and wired. `probe_source()` runs as the pre-scan in `v6/raw_scan.py` (Task 1); verdict drives the scan decision. 96 assertions; K4 ANSI block in `v6/selftest.py`; passes with an ANSI env note.

---

## 1. Why a volume gate exists

A raw scan of a **BitLocker-encrypted volume** produces **false positives only**:

- Ciphertext has no headers and no file-system structure, so every "hit" is a
  signature landing on random bytes. The random rate is two bytes per 65,536
  bytes, i.e. roughly **16,384 hits per GiB**.
- One 512-byte read per partition, *before* any scanning, tells the difference
  between "there are files here" and "this is 461 GiB of noise".

The gate is therefore a **read-only pre-scan**: it classifies what is actually
present and stops (or downgrades) the scan for anything that would otherwise
return noise.

---

## 2. The gate decision pipeline

`probe_source()` runs four independent checks and combines their answers:

| Stage | What it reads | Evidence |
|---|---|---|
| 1. Partition table | GPT at LBA 1, MBR fallback, else one synthetic whole-source entry | `EFI PART` header; 4 × 16-byte MBR entries |
| 2. Volume classification | The VBR OEM id at offset 3 of each partition | `NTFS    `, `-FVE-FS-`, `MSDOS5.0`/`MSWIN4.1`/`EXFAT`, eight zero bytes |
| 3. Entropy corroboration | 8 samples of 64 KiB, evenly spread, Shannon entropy | 7.5 bits/byte is a **warning**, never a verdict |
| 4. Content coverage | Backward sweep from the tail (64 KiB chunks) | last non-zero block; truncated/pre-allocated image |

Nothing is decided on a single byte. A BitLocker verdict quotes the OEM id **and**
the entropy samples, so a human can check it. High entropy alone is never called
encryption — a JPEG or a ZIP measures ~7.9 bits/byte too.

---

## 3. Volume classification (the `\\.\C:` fix)

The evidence is the **OEM id at VBR offset 3**:

```text
OEM id            -> kind         meaning
NTFS    (or NTFS) -> ntfs          plaintext NTFS, metadata readable
-FVE-FS-          -> bitlocker     BitLocker-encrypted payload (ciphertext)
MSDOS5.0 etc.     -> fat32         FAT / exFAT, usually the EFI system partition
0x00 × 8          -> unformatted  MSR or unformatted; the GPT type GUID separates them
anything else     -> unknown       neither trusted as plaintext nor called encrypted
```

### The `\\.\C:` bug and how it was fixed

**Symptom (before the fix).** `\\.\C:` (a healthy, plaintext NTFS disk) was being
classified as **empty** and rejected. A scan of a normal disk was being silently
blocked — a false negative that would have hidden recovery results.

**Root cause.** A raw volume device answers no file size, so `_Source._probe_size()`
returned `0` for devices. An empty size made the probe treat the source as "nothing
to scan".

**Fix.** A device that refuses a seek-to-end now reports `size = None` (not 0):

```python
return None if _is_device(self.path) else 0
```

The probe treats `size is None` as "the device refused a seek-to-end", skips the
content-end check, and falls through to its real verdict. A plaintext disk now
classes as **plaintext** (not empty), and a truncated image reports its exact
content end instead of refusing:

```python
if src.size is None:
    probe["warnings"].append(
        "source size unknown (the device refused a seek-to-end): the content-end "
        "check was skipped")
```

---

## 4. Plaintext classification

A partition is **scannable** when all of the following hold:

- It has a readable boot sector (`part["oem_id"]` non-empty).
- Its LBA range is non-empty (`end > start`, `bytes > 0`).
- It starts before the content end (`start < content_end`).
- It is **not** BitLocker (`part["kind"] != "bitlocker"`).
- It is **not** MSR / unformatted (`part["kind"] not in SKIP_KINDS`).

Nothing else is rejected. A partition that reads 7.9 bits/byte but has a
recognisable OEM id stays a scan target — the entropy is reported as a warning,
not a verdict.

### Verdict matrix

| `blocked` | `scannable` | `verdict` | what the run does |
|---|---|---|---|
| [] | [] | `empty` | nothing to scan |
| nonempty | [] | `encrypted` | all ciphertext; no carving |
| [] | nonempty | `plaintext` | full scan proceeds |
| both | both | `mixed` | skip BitLocker, scan plaintext |

---

## 5. Public API

```python
classify_volume(vbr)                       # {'kind','reason'}  kind in ntfs/bitlocker/fat32/msr/unformatted/unknown
read_partitions(path, limit_bytes=None)    # [partition dict, ...]
entropy_samples(path, first_lba, last_lba, samples=8, block=65536)  -> [bits/byte]
probe_source(path, limit_bytes=None, tail_window=None) -> probe dict
explain(probe)                             # human-readable verdict + message
```

Probe keys: `size`, `partitions`, `verdict` (`plaintext` \| `mixed` \| `encrypted` \| `empty`),
`scannable`, `blocked`, `warnings`, `message`, `table`, `content`, `skipped`, plus `source`
(`reads`, `bytes_read`, `errors`) and `error`/`error_hint`.

Probe dict (partition) keys: `index`, `first_lba`, `last_lba`, `bytes`, `name`, `oem_id`,
`kind`, `reason`, `entropy`, `scan_bytes`, `sample_bytes`.

### Key constants

| Constant | Value | Role |
|---|---|---|
| `SECTOR` | 512 | GPT / VBR addressing unit |
| `SAMPLE_BLOCK` | 64 KiB | entropy sample and content-end block |
| `SWEEP_CHUNK` | 8 MiB | sequential read for the content sweep |
| `CONTENT_TAIL_WINDOW` | 4 GiB | default tail checked by the sweep |
| `SAMPLE_SPAN` | 8 GiB | probe entropy-samples span |
| `MATERIAL_GAP` | 64 MiB | a tail smaller than this is reported, not alarmed |
| `HIGH_ENTROPY` | 7.5 | bits/byte that warns (never verdicts) |
| `MAX_GPT_ENTRIES` | 256 | cap on GPT entry reads |
| `MAX_GPT_ENTRY_SIZE` | 4096 | cap on GPT entry size |

---

## 6. Connection to `raw_scan.py` (pre-scan, now wired)

`volume_gate.py` is fully unit-tested on its own (96 assertions; K4 ANSI block in `v6/selftest.py`)
and is wired as the pre-scan in `v6/raw_scan.py`. The gate runs before the signature search;
the verdict drives the scan decision:

- `verdict == "encrypted"` (with `blocked` non-empty) -> warning that a full scan would
  return false positives only; nothing useful is recovered without a 48-digit recovery
  password or `.bek` key.
- `verdict == "empty"` (nothing scannable) -> warning "no scannable volumes found;"
  nothing to recover."
- `verdict == "plaintext"` / `"mixed"` -> proceed with the scan; encrypted partitions
  are skipped in a mixed volume.

The `size is None` device case is handled, not a gap: a device that refuses a seek-to-end
reports `size = None` (not 0); the probe skips the content-end check and falls through to
its real verdict (partition/volume evidence), so a healthy plaintext disk is not rejected
as empty. The run database records `source_size = 0` for this case, and the report states
the device caveat.

## 7. ANSI output rules

**Applied to:** every print/render path in `v6/volume_gate.py` and its
callers (`raw_scan.py`, `display.py`).

### 7.1 Color is terminal-only

ANSI color codes (green / red / yellow / blue) are emitted **only to a
terminal**. They never appear in:

- probe dictionaries returned by `probe_source()` (the data is machine
  readable by design),
- the text report, `.csv` exports, `provenance.json`, `run.db`, `index.html`,
  `audit.txt`, or any other artifact written to disk.

`volume_gate.py` defines no color helpers itself; the helpers
`green()` / `red()` / `yellow()` / `blue()` live in `display.py` (also
duplicated in `raw_scan.py`) and are applied only to strings that are being
rendered to a terminal.

### 7.2 non-TTY fallback

When stdout is **not a TTY** (a pipe, a redirect, a CI runner, `TERM=dumb`,
or `FORCE_COLOR=0`), color is disabled:

- `color_enabled(stdout, env)` returns `False`.
- `green()` / `red()` / `yellow()` / `blue()` return their input verbatim,
  with no escape codes.
- Verdicts and warnings are still printed, as plain text.

The read-only pre-scan never prints progress; the verdict reaches the user
through the normal print path, which is subject to the same
`color_enabled()` check as every other terminal line.

### 7.3 NO_COLOR

Any non-empty `NO_COLOR` environment variable forces color off, even on a
TTY (convention: no-color.org). `color_enabled()` checks `env` first, so
`NO_COLOR` wins over `FORCE_COLOR` and over a TTY being present.

### 7.4 No leak

- The gate is strictly read-only: it opens the source `"rb"`, seeks and
  reads; never writes to it.
- Every file handle is closed in a `with` block; no handle escapes the
  probe.
- `probe_source()` mutates nothing, caches nothing between probes, and
  writes no files - a second probe on the same path is independent of the
  first.
- No temporary files, no `tempfile`, no shelling out to `ffmpeg` or any
  external program.

### 7.5 Selftest

The gate is exercised on synthetic fixtures in **both ANSI modes**
`color_enabled()` on and off, in `v6/gate_selftest.py` (96 assertions, run
with the bundled interpreter):

```bat
python/python.exe v6/gate_selftest.py
```

Coverage includes: the `[GATE] verdict=X | scannable=Y | blocked=Z` line
under TTY and non-TTY, a NO_COLOR collapse, probe dictionaries containing
zero ANSI bytes, and a no-leak check that a probe leaves no open handles.
The suite must pass before any change to the verdict text or the
`explain()` message.

## 8. Rules

- **READ-ONLY.** Opens the source `"rb"`, seeks and reads; never writes to it.
- **Never guesses a key and never attempts decryption.** A BitLocker volume is
  reported, not opened.
- **Works on images and raw devices.** Size comes from a seek-to-end-and-back;
  a device that refuses degrades with a warning instead of an exception.
- **Python standard library only** — no `subprocess`, no `ctypes`, no new dependency.

## 9. Configuration

Configuration for the volume gate lives in `v6/volume_gate.py` and is read-only at probe time.

| Setting | Meaning |
|---------|---------|
| `probe_source(target, limit_bytes=None, tail_window=None)` | Read-only pre-scan. `limit_bytes` examines only that prefix; `tail_window=None` (default) uses `CONTENT_TAIL_WINDOW` (4 GiB); `tail_window=0` sweeps the whole source for the exact content end. |
| `tail_window=0` | Full content sweep; costs a full read but reports the exact last non-zero block. |
| `HIGH_ENTROPY` (7.5) | bits/byte that warns (never a verdict). High entropy alone is never called encryption. |

---

## 10. Troubleshooting

- **Healthy plaintext disk classified as empty.** Fixed: a raw device that refuses a seek-to-end
  now reports `size = None` (not 0), so the probe skips the content-end check and proceeds on
  the real verdict. If a plaintext disk is still blocked, check that the disk is not actually
  unformatted/MSR (VBR all zeros) and that the partition table is readable (GPT or MBR).
- **Verify the gate runs:** use the bundled interpreter:
  `python/python.exe v6/volume_gate.py <image | .PhysicalDriveN>`
  `python/python.exe v6/gate_selftest.py`
  The self-test assertions pin the verdict, the `size is None` case, ANSI enabled/disabled,
  NO_COLOR, and the no-ESC-leak invariant.
- **STOP (do not scan):** if `probe_source()` returns `verdict=encrypted` (or the screen
  prints `[GATE] verdict=encrypted`):
  - Every partition is BitLocker ciphertext. No carving can recover files from it; the old
    v5.0.0 "noise hits" were ciphertext.
  - You need the 48-digit recovery password or a `.bek` key file, or the key in your
    Microsoft account. This tool never guesses a key and never attempts decryption.
  - Use a plaintext volume (or the plaintext partitions of a mixed volume) for a scan.
- **Continue (with warning):** if `verdict=mixed`:
  - BitLocker partitions are skipped; plaintext partitions are scanned normally.
  - The report lists which partitions were skipped.
- **Continue (plain, no scan):** if `verdict=empty`:
  - No partition carries a readable file system (unformatted/MSR/all-zero image). Nothing
    to recover; check the source path or `tail_window=0` for a full content sweep.
- **Platform:** the gate is written for Windows (raw device paths `\\.\\PhysicalDriveN`,
  OEM byte ordering, GPT/MBR reading). It is read-only and stdlib-only, for the bundled
  Python 3.12 on the DLC Boot stick, not for a system Python with pip. Raw device reads
  require Administrator / SYSTEM rights.

- **Prefer the unit tests** (`v6/gate_selftest.py`, 96 assertions) over manual probing.
  ```bat
  python/python.exe v6/volume_gate.py <image | \\.\\PhysicalDriveN>
  python/python.exe v6/gate_selftest.py
  ```



