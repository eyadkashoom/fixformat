# Changelog — fixformat v7

## [v7.0.1] — 2026-11-01

### Added
- Phase 0: GUI split into `gui_web.py` + `gui_web.html`; `gui.py` shim for backward compat.
- Phase 1: hardened validators (ZIP EOCD search 65 KB, PNG CRC32, JPEG markers, PDF xref, SQLite page-size @16); assembly 1 GB cap; validators wired into carve triage; SQLite offset corrected to 16.
- Phase 2: `raw_scan_core.py`, `raw_scan_io.py`, `raw_scan_report.py` split modules.
- Phase 3: `by_date/YYYY/MM/DD/` organization (`--by-date`); `--no-elevated-check` with Administrator warning.
- Phase 4: full web GUI with all CLI fields.
- Phase 5: `run_gui.bat` desktop shortcut.
- `test_v7_validators.py` (8 assertions), `test_v7_gui_module` check in selftest.

### Changed
- `VALIDATORS_GUIDE.md` moved/updated to `v7/` (CRC/EOCD/page-size table).
- `README.md` v7 entry point added.

### Fixed
- SQLite page-size offset (18 → 16) per spec.
- JPEG segment-length sanity no longer rejects minimal fixtures.
- `_read_range` missing return; `carve_phase` signature; progress stdout line.

### Tests
- selftest PASS, bundle 165/165, wiring 21/21, v7 validators 8/8.
