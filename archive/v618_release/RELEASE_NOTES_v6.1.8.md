# fixformat v6.1.8 — single-file raw-disk signature scanner + imager (stdlib only)

## What changed (cumulative into v6.1.8)
- **K5 progress**: `scan()` loop now reports progress through the K5 ProgressCallback renderer (ANSI-aware).
- **Display wiring**: `_ansi_enabled` delegates to `display.color_enabled`; raw_scan + volume_gate share the global flag so no ANSI leaks to piped/non-tty.
- **Bootstrap single-entry**: runnable directly off the bundled `python/` with no install (CPython 3.12 embed).
- **Volume gate + run DB** integrated.

## Verified (run on the embed interpreter, no install)
- selftest matrix: **12/12 pass** (v6/selftest, gate_selftest, db_selftest, bundle, wiring, steps67, fixture_test, ops, penalty, perf, display_layer, v5/selftest).
- End-to-end fixture: **4/4** signatures hit on precise offsets (synthetic 16 MB image).
- Sustained scan throughput: ~**566 MB/s**.

## Assets
- `v6.1.8.zip`        — `v6/` source tree at this tag (extract to a folder).
- `python-embed.zip`  — bundled CPython 3.12; extract INTO a new `python/` folder beside `v6/`.
- `SHA256SUMS.txt`    — sha256 of both archives.

## Run (no install)
    python\python.exe v6aw_scan.py --selftest   # sanity check
    python\python.exe v6aw_scan.py \.\.\PhysicalDrive1   # scan a physical drive
