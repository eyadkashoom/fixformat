# v6 — Documentation Update Spec (pre-flight)

- **Author:** @technical_writer_docs
- **Trigger:** Task 1 (pre-scan `probe_source()` gate + per-run `run_db` wiring) landing in `v6/raw_scan.py`.
- **Status:** PRE-LAND — spec only. Apply to `README.md` + `CHANGELOG.md` once `@core_developer` confirms the hooks are wired.
- **Companions:** `V6_PLAN.md` §0b, `v6/VOLUME_GATE_GUIDE.md`, `v6/run_db.py`, `v6/db_selftest.py`.

## 1. Why the docs change
Before 2026-10-29 the plan treated the image as a plaintext-recovery problem. Measured finding **§0b** (verified 2026-10-29): the volume that matters is **BitLocker-encrypted** — partition 2, 461.09 GiB, VBR OEM id `-FVE-FS-`, entropy 7.99–8.00 bits/byte, content ends near +68 GiB. The 8,675 "noise hits" of v5.0.0 were **ciphertext**, not markup (a two-byte needle matching random bytes at the rate chance predicts, ~16,384/GiB). v6's **first feature is the FVE gate** (detect, never guess a key, never decrypt, never let an encrypted volume produce candidates). The **run database** (`run_db.py`, SQLite per run) records one file per run, with a `ranked` view for filtering.

## 2. `README.md` edits (add one section)
- Add `## v6 — volume gate & run database`:
  - Pre-scan gate: `volume_gate.probe_source()` reads the partition table once, classifies each VBR by OEM id at offset 3 (`NTFS    ` → plaintext, `-FVE-FS-` → BitLocker, `MSDOS5.0`/exFAT → FAT, 8 zero bytes → unformatted, else unknown).
  - Rule: **detect, never guess a key, never attempt decryption**; never let a scan of an encrypted volume produce recoverable-looking candidates.
  - Verdict matrix (plaintext / mixed / encrypted / empty) and what each does.
  - Run database: one SQLite file per run next to `provenance.json`; schema in `v6/RUN_DB_GUIDE.md`; `ranked` view filters by `validated`, `confidence`, `length`.
  - One line on the real-media outcome: this image is BitLocker, so the user's old data on that volume is unrecoverable without the 48-digit recovery password or `.bek` — the v6 gate refuses to carve it.

## 3. `CHANGELOG.md` edits
- Add `## [v6.0.0]` with three bullets:
  1. **Volume gate (FVE) — first feature.** Per-partition `-FVE-FS-` detection before any scan; encrypted partitions refused, plaintext OEM partitions still scanned (417 OEM files, 11.14 GiB on this image).
  2. **Run database.** One SQLite file per run (`v6/run_db.py`), append-only, with `ranked` view for filtered queries; replaces the append-only text log.
  3. **False-positive fix.** Document that v5.0.0's 8,675 "noise hits" were BitLocker ciphertext, and v6 gates encrypted partitions before carving (validators come later).

## 4. `V6_PLAN.md` edits
- §0b already present and accurate; no change needed.
- Add a one-line cross-reference from the README `v5` section to this spec.

## 5. New file: `v6/RUN_DB_GUIDE.md`
- Document the §9b schema (`run`, `region`, `candidate`, `fragment`, `signature`, `ranked` view).
- Document the API used by `raw_scan.py` (public functions in `v6/run_db.py`): `run_db.open_run_db()`, `run_db.new_run()`, `run_db.add_candidate()`, `run_db.add_fragment()`, `run_db.add_region()`, `run_db.add_signature_counts()`, `run_db.add_destination()`, `run_db.add_plan()`, `run_db.update_run()`, `run_db.ranked()`, `run_db.summary()`, `run_db.plan_extraction()`, `run_db.dedupe_by_sha256()`, `run_db.write_report()`.
- Note: open/new/add on a fresh run; one transaction per 5% slice; ranked view used by the report.
- Note the `size is None` device case (seek-to-end refused): reported as "source size unknown", verdict from partition/volume evidence, not from size.

## 6. QA gate
- Docs sync is gated on the Task 1 QA gate going green (integration-missing → integrated). @qa_automated_tester: ping when the gate is green.

## 7. Task 2 — validators (JPEG/PNG/PDF/ZIP/SQLite)
- Spec: V6_PLAN §8 — `validate(bytes, path_hint) -> score` per format.
- Fixtures: 10 synthetic cases (qa_validators_cases.py), 2 per format (valid-min + corruption variant) + 2KB noise rejects. RED gate (EXIT=1) until `validate_jpeg/png/pdf/zip` land in `raw_scan.py`.
- Docs: `v6/VALIDATORS_GUIDE.md` (created) + README/CHANGELOG updates when the validator code lands.
