import io
path = "C:/Users/eyad/Documents/Codex/2026-09-27/fixformat/CHANGELOG.md"
t = io.open(path, encoding="utf-8").read()

anchor = "### Verification status (K1 wired + K2 QA pass)"
assert anchor in t, "anchor not found"

new_sections = """

### Plan screen, reports and audit (steps 4, 5, 12)

The recovery plan screen (`v6/plan_screen.py`) renders the recovery plan from
the run database using `run_db.plan_extraction()` and `summary()`. The reports
writer (`v6/reports_integration.py`) emits `report.md`, `report.csv` and
`report.html` via `run_db.write_report()`. `v6/audit_serialization.py` writes
`audit.txt` - a stable-field serialization of ranked candidates (offset,
length, ext, fragments, confidence, label, validated, sha256). All three stay
disjoint from the scan, validator and performance modules and use public DB
APIs only. Verified with synthetic fixtures.

### Validators and fragmented assembly (steps 6 and 7)

`v6/validators.py` validates each recovered candidate before carving or
reporting: JPEG (FFD8 SOI -> FFD9 EOI), PNG (IHDR + IEND + CRCs), PDF
(%PDF header + startxref xref), ZIP (end-of-central-directory), SQLite
(magic header). Malformed inputs return actionable errors. `v6/assembly.py`
combines validated fragments: single fragment returns as-is (confidence 0.95),
two-fragment falls back to best-guess continuation (BGC) over gaps 0..256,
three+ concatenate in order (confidence 0.82). Both stay disjoint from
data/UI/report/performance. Synthetic fixture verified; real media not written
to.

### Adaptive operations (entropy, parallel penalty, calibration, Direct I/O)

`v6/performance/operations.py` adapts the scanner to the sampled data:
all-zero sample selects `zero_skip`; otherwise `adaptive_parallel` or
`single_worker_skip` by zero fraction; Shannon entropy (0..8 bits/byte)
computed reproducibly; parallel penalty grows with workers and zero fraction
(measured: all-zero 4-worker ~0.74x single); fast-scan calibration is
deterministic per sample file; `open_direct()` attempts Direct I/O and falls
back safely to std-buffered I/O. Deterministic, reproducible; selftest passes.
"""

t = t.replace(anchor, new_sections + anchor, 1)
io.open(path, "w", encoding="utf-8").write(t)
print("CHANGELOG updated")
