"""Replace the README v6 section, re-emitting CRLF line endings.

The on-disk README.md uses CRLF; read_file normalizes to LF for display, so a
naive write_file (LF) is refused. This script works on bytes and writes CRLF.
"""
import io

path = "C:/Users/eyad/Documents/Codex/2026-09-27/fixformat/README.md"
import os
print("cwd:", os.getcwd())
print("path repr:", repr(path))
print("abspath:", os.path.abspath(path))
print("exists:", os.path.exists(path))
print("ls style:", os.listdir("."))
raw = io.open(path, "rb").read()
text = raw.decode("utf-8")
lines = text.splitlines(keepends=True)
print("total lines:", len(lines))

# Identify the slice to replace: from the "## Release 6" heading through the
# "Verification:" line, inclusive.
start = None
end = None
for i, ln in enumerate(lines):
    if ln.startswith("## Release 6"):
        start = i
    if ln.startswith("Verification:") and start is not None and end is None:
        end = i

print("start:", start, "end:", end)
if start is None or end is None:
    raise SystemExit("could not locate v6 section slice")

# New v6 bundle section as a list of lines (each will keep its CRLF from the
# surrounding join). We append the section's lines (without their own \r\n) and
# the join step below adds \r\n.
new_section = [
    "## Release 6 - the v6 bundle (read-only, add-on)",
    "",
    "`v6/` is a single, coherent bundle of new features that change what a scan can",
    "honestly report. One new source module backs each feature; every module is",
    "stdlib-only, disjoint from the others, and exercised by self-tests:",
    "",
    "| Area | New files | What it does |",
    "|------|-----------|--------------|",
    "| **DB / extraction** | `v6/run_db.py`, `v6/raw_scan.py` (Task 1 wiring) | Per-run SQLite `run.db` plus a pre-scan volume gate (`v6/volume_gate.py`) that classifies each partition as plaintext / mixed / encrypted / empty before any signature search. |",
    "| **Plan screen / reports / audit** | `v6/plan_screen.py`, `v6/reports_integration.py`, `v6/audit_serialization.py` (delivered, standalone) | A recovery-plan screen rendered from the run DB, three report formats (`report.md` / `.csv` / `.html` plus `audit.txt`), all from public run_db APIs only. |",
    "| **Validators / assembly** | `v6/validators.py`, `v6/assembly.py`, `v6/test_v6_steps67.py` | JPEG / PNG / PDF / ZIP / SQLite validation with actionable errors for malformed inputs; sequential + 2-fragment \"best-guess continuation\" (BGC) assembly returning fragments, gap bytes and confidence. |",
    "| **Adaptive operations** | `v6/performance/operations.py`, `v6/performance/selftest_perf.py` | Adaptive operation selection (adaptive_parallel vs single_worker_skip), Shannon entropy, parallel penalty accounting, fast-scan calibration, and an opt-in Direct I/O path with a safe buffered fallback. |",
    "",
    "### Volume gate (pre-scan)",
    "",
    "Before any signature search, `volume_gate.probe_source()` reads the partition",
    "table once and classifies every partition by its VBR OEM id at offset 3:",
    "`NTFS    ` (plaintext NTFS), `-FVE-FS-` (BitLocker ciphertext), `MSDOS5.0`/`",
    "exFAT (FAT, usually the EFI system partition), eight zero bytes (unformatted/MSR),",
    "or anything else (unknown). `volume_gate.explain()` prints the verdict. Four",
    "outcomes apply: **encrypted** (abort - nothing recoverable without the 48-digit",
    "recovery password or a `.bek` file), **mixed** (skip only the encrypted",
    "partitions), **plaintext** (full scan proceeds), and **empty** (report, no",
    "scan). The gate is read-only, never guesses a key, never attempts decryption,",
    "and never lets an encrypted volume produce candidates that look recoverable -",
    "the 8,675 \"hits\" of v5.0.0 on the user's real image were BitLocker ciphertext,",
    "not markup.",
    "",
    "### Run database (per-run SQLite)",
    "",
    "Each run now writes a per-run SQLite file (`run_db.py`, `run.db` next to",
    "`provenance.json`), replacing the append-only text log. It records the run,",
    "source and size, signature set, region map (zero/entropy), candidates,",
    "fragments and signature counts. A `ranked` view answers \"only validated files",
    "over 100 KB?\", \"have I already recovered this file?\" and \"what changed since",
    "the last run?\" with one query; `summary()` and `dedupe_by_sha256()` cover the",
    "report statistics. See `v6/RUN_DB_GUIDE.md` and the API reference in",
    "`v6/API.md`.",
    "",
    "### Plan screen, reports, audit",
    "",
    "The plan screen renders a recovery plan from the run database",
    "(`run_db.plan_extraction()` / `summary()`); the reports writer emits",
    "`report.md` / `report.csv` / `report.html` (`run_db.write_report()`); and",
    "`audit_serialization.py` writes `audit.txt` - a stable-field serialization of",
    "ranked candidates (offset, length, ext, fragments, confidence, label, validated,",
    "sha256). All three use the run_db public APIs only and stay disjoint from the",
    "scan, validator and performance modules. See `v6/CLI_UX_SPEC.md` for the",
    "display-contract mapping and `v6/RUN_DB_GUIDE.md` for the schema.",
    "",
    "### Validators (JPEG / PNG / PDF / ZIP / SQLite)",
    "",
    "Every recovered candidate is validated before it is carved or reported:",
    "`validate(data, fmt) -> (validated, confidence, validator, fragments, gap_bytes,",
    "error)`. A candidate is \"recovered\" only when a validator accepts its header",
    "structure. Malformed inputs (truncated JPEG, missing PNG IHDR/IEND, PDF without",
    "xref, ZIP without EOCD, SQLite without the magic header) get actionable error",
    "messages. See `v6/VALIDATORS_GUIDE.md`.",
    "",
    "### Fragmented assembly",
    "",
    "`assemble(fragments) -> (assembled, fragments_n, gap_bytes, confidence, error)`",
    "combines validated fragments: single fragment returns it as-is (confidence",
    "0.95); two fragments fall back to \"best-guess continuation\" (BGC) over gaps",
    "0..min(limit,256); three or more concatenate in order (confidence 0.82).",
    "Returns `gap_bytes` and `confidence` so reports can say how much data was",
    "filled and how certain the assembly is. See `v6/VALIDATORS_GUIDE.md`.",
    "",
    "### Adaptive operations (entropy / parallel penalty / calibration / Direct I/O)",
    "",
    "The scanner adapts its operation to the data it samples: an all-zero sample",
    "selects `zero_skip`, otherwise `adaptive_parallel` or `single_worker_skip`",
    "based on the zero fraction; Shannon entropy (0..8 bits/byte) is computed",
    "reproducibly; the parallel penalty factor grows with workers and zero",
    "fraction (measured: all-zero 4-worker ~0.74x single); fast-scan calibration is",
    "deterministic per sample file; and `open_direct()` attempts Direct I/O and",
    "falls back safely to std-buffered file I/O. All functions are deterministic",
    "and reproducible; the selftest passes. See `v6/performance/operations.py`.",
    "",
]

# Replace slice [start .. end] with new_section lines (each line + \\r\\n)
new_lines = [ln + "\r\n" for ln in new_section]
lines[start:end + 1] = new_lines

out = "".join(lines).encode("utf-8")
io.open(path, "wb").write(out)
print("wrote", path, "bytes:", len(out))
