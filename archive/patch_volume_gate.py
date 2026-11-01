"""Focused patch for VOLUME_GATE_GUIDE.md.

The VALIDATORS_GUIDE.md is already updated (see prior patch run).

Labels (check carefully against the file):
- status line
- section 6 header
- section 6 "known open gap" paragraph
"""
import io

g_path = "C:/Users/eyad/Documents/Codex/2026-09-27/fixformat/v6/VOLUME_GATE_GUIDE.md"
g_raw = io.open(g_path, "rb").read()
g_text = g_raw.decode("utf-8")
g_crlf = "\r\n" in g_text

g_repl = [
    (
        "- **Status:** volume gate implemented and tested (96 assertions, 6.3 s); awaiting pre-scan wiring (Task 1).",
        "- **Status:** IMPLEMENTED and wired. `probe_source()` runs as the pre-scan in "
        "`v6/raw_scan.py` (Task 1); verdict drives the scan decision. 96 assertions; K4 ANSI "
        "block in `v6/selftest.py`; passes with an ANSI env note.",
    ),
    (
        "### 6. Connection to `raw_scan.py` (pre-scan hook, Task 1)",
        "### 6. Connection to `raw_scan.py` (pre-scan, now wired)",
    ),
    (
        "The remaining integration is to call `probe_source()` early in the `raw_scan.py`\n"
        "pre-scan and act on the verdict:\n\n"
        "- `verdict == \"encrypted\"` → abort this source, print the `explain()` message.\n"
        "- `verdict == \"mixed\"` → skip only the `blocked` (BitLocker) partitions.\n"
        "- `verdict == \"plaintext\"` → proceed as before.\n"
        "- `verdict == \"empty\"` → report \"nothing was imaged here\" without a scan.\n\n"
        "**Known open gap on the integration side:** a device that refuses a seek-to-end\n"
        "(`size is None`) is reported as \"source size unknown\" rather than \"empty\",\n"
        "so its verdict is computed from the partition/volume evidence, not from size.",
        "The pre-scan is wired in `v6/raw_scan.py`: `probe_source(target)` runs before the "
        "signature search, and the verdict is printed as `[GATE] verdict=... | scannable=... "
        "| blocked=...` plus `volume_gate.explain(probe)`. The run then acts on the verdict:\n\n"
        "- `verdict == \"encrypted\"` (with `blocked` non-empty) → prints a warning that a full "
        "scan would return false positives only; nothing useful is recovered without a 48-digit "
        "recovery password or `.bek` key.\n"
        "- `verdict == \"empty\"` (nothing scannable) → warning \"no scannable volumes found; "
        "nothing to recover.\"\n"
        "- `verdict == \"plaintext\"` / `\"mixed\"` → proceed with the scan; encrypted partitions "
        "are skipped in a mixed volume.\n\n"
        "The `size is None` device case is handled, not a gap: a device that refuses a "
        "seek-to-end reports `size = None` (not 0); the probe skips the content-end check "
        "and falls through to its real verdict (partition/volume evidence), so a healthy "
        "plaintext disk is not rejected as empty. The run database records `source_size = 0` "
        "for this case and the report states the device caveat.",
    ),
]

for old, new in g_repl:
    if old not in g_text:
        print("G NOT FOUND:", old[:70])
    else:
        g_text = g_text.replace(old, new)

# Appended sections 7 & 8
marker = "- **Python standard library only** — no `subprocess`, no `ctypes`, no new dependency."
assert marker in g_text, "section 8 marker not found"

g_append = r"""## 7. Configuration

Configuration for the volume gate lives in `v6/volume_gate.py` and is read-only at
probe time.

| Setting | Meaning |
|---------|---------|
| `probe_source(target, limit_bytes=None, tail_window=None)` | Read-only pre-scan. `limit_bytes` examines only that prefix; `tail_window=None` (default) uses `CONTENT_TAIL_WINDOW` (4 GiB); `tail_window=0` sweeps the whole source for the exact content end. |
| `tail_window=0` | Full content sweep; costs a full read but reports the exact last non-zero block. |
| `HIGH_ENTROPY` (7.5) | bits/byte that warns (never a verdict). High entropy alone is never called encryption. |

## 8. Troubleshooting

- **Healthy plaintext disk classified as empty.** Fixed: a raw device that refuses a
  seek-to-end now reports `size = None` (not 0), so the probe skips the content-end check
  and proceeds on the real verdict. If a plaintext disk is still blocked, check that the
  disk is not actually unformatted/MSR (VBR all zeros) and that the partition table is
  readable (GPT or MBR).
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
- **Platform:** the gate is written for Windows (raw device paths `\\\\.\\PhysicalDriveN`,
  OEM byte ordering, GPT/MBR reading). It is read-only and stdlib-only, for the bundled
  Python 3.12 on the DLC Boot stick, not for a system Python with pip. Raw device reads
  require Administrator / SYSTEM rights.
"""

g_text = g_text.replace(marker, marker + "\n\n" + g_append)

if g_crlf:
    g_text = g_text.replace("\n", "\r\n")
io.open(g_path, "w", encoding="utf-8").write(g_text)

print("VOLUME_GATE_GUIDE updated")
