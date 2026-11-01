# fixformat v7.0.1

Raw-sector signature scanner and disk imager (read-only on the source disk).

## What's new in v7.0.1
- **Phase 0 — GUI split:** `gui.py` shim → `gui_web.py` + `gui_web.html` (stdlib HTTP server, port 8787).
- **Phase 1 — validators hardened:** ZIP EOCD search (65 KB comment), PNG CRC32 per chunk, JPEG SOI/EOI + marker sanity, PDF `startxref` offset check, SQLite page-size 512–65536 (offset 16). Assembly 1 GB cap, no zero-padding. Validators wired into carve triage metadata.
- **Phase 2 — module split:** `raw_scan_core.py`, `raw_scan_io.py`, `raw_scan_report.py` (functional re-export split; canonical code remains in `raw_scan.py`).
- **Phase 3 — missing features:** `by_date/YYYY/MM/DD/` organization (`--by-date`), `--no-elevated-check` with Administrator warning.
- **Phase 4 — full web GUI:** all CLI fields (carve_before/after/limit, min_gap, start/end offset, deadline).
- **Phase 5 — desktop:** `run_scan.bat` + `run_gui.bat`.

## Run
- CLI: `python raw_scan.py` (interactive menu) or `python raw_scan.py --no-elevated-check --by-date`
- Web GUI: `python gui_web.py` → http://127.0.0.1:8787/ (or double-click `run_gui.bat`)

## Tests
```
python selftest.py                 # full offline selftest
python test_v6_bundle.py           # 165 assertions
python test_v6_wiring.py           # 21 assertions
python test_v7_validators.py       # 8 assertions
```

## Layout
- `raw_scan.py` — core scanner (4400+ lines)
- `validators.py`, `assembly.py` — format validation & fragmented assembly
- `volume_gate.py`, `run_db.py` — BitLocker gate & per-run SQLite DB
- `display.py`, `progress_indicators.py` — ANSI/display layer
- `gui_web.py` + `gui_web.html` — web GUI

| `archive/` | Older releases (v1–v6, v7, v618_release) and historical planning docs |

## Constraints
- Stdlib only (embedded Python, no pip).
- Read-only on the source disk; never writes to it.
- Designed to run from a USB flash drive in Mini Windows 10 / DLC Boot.
