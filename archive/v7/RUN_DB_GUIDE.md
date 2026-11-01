# fixformat v6 — Run Database Guide

**Target:** developers and QA, Task 1.
**Companion files:** `v6/run_db.py`, `v6/raw_scan.py` (Task 1 wiring), `v6/db_selftest.py`.
**Status:** implemented; `db_selftest.py` green (229 assertions).

---

## 1. Why a database

v5 leaves an append-only text log, one JSON, a carved folder, an organised folder
and one static page. Nothing in that set can be filtered, ranked, deduplicated or
compared between runs. This database answers, with one query:

- "Have I already recovered this file?" — `sha256` index, one query.
- "Only validated files over 100 KB?" — `WHERE validated = 1 AND length > 102400`.
- "Which MFT-named deleted file was never carved?" — join against the name map.
- "What changed since the last run?" — `run_id` diff.
- "Where is the data on this disk?" — the `region` table is the zero/entropy map.

One SQLite file per run, in the run folder next to `provenance.json`.

---

## 2. Schema (`v6/run_db.py` - `SCHEMA`)

```sql
CREATE TABLE run (
    id INTEGER PRIMARY KEY,
    tool_version TEXT,
    started TEXT,
    source TEXT,
    source_size INTEGER,
    mode TEXT,
    signature_set TEXT,
    workers INTEGER,
    status TEXT,
    resume_offset INTEGER,
    cap_policy TEXT
);

CREATE TABLE region (
    run_id INTEGER,
    start INTEGER,
    length INTEGER,
    zeros INTEGER,
    entropy REAL,
    kind TEXT                  -- data | zero | compressed
);

CREATE TABLE candidate (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    offset INTEGER,
    length INTEGER,
    label TEXT,
    ext TEXT,
    sha256 TEXT,
    validated INTEGER,         -- 0/1
    validator TEXT,
    confidence REAL,
    fragments INTEGER,
    gap_bytes INTEGER,
    name_guess TEXT,
    name_source TEXT,
    mft_record INTEGER,
    carved_path TEXT,
    triage TEXT
);

CREATE TABLE fragment (
    candidate_id INTEGER,
    ordinal INTEGER,
    offset INTEGER,
    length INTEGER
);

CREATE TABLE signature (
    run_id INTEGER,
    label TEXT,
    hex TEXT,
    ext TEXT,
    hits INTEGER
);

CREATE VIEW ranked AS
    SELECT * FROM candidate
    ORDER BY validated DESC, confidence DESC, length DESC, id;
```

Three rules baked into the schema:

- `foreign_keys = ON` and WAL journal mode, so a long-running scan can be stopped
  at any slice without corrupting the index, and a report reader never blocks the
  scan writer.
- `ranked` is a `VIEW`, not a table, so it always reflects the current rows.
- Every value reaches SQLite through a bound parameter (`?`); no caller-supplied
  string is ever concatenated into a statement.

---

## 3. API used by `v6/raw_scan.py`

`raw_scan.py` opens the DB **after** the run folders are created and **before**
the scan starts; it closes it (via `write_report`) at the end.

```python
import run_db

# 1. Open/create the per-run database next to the results
db_conn = run_db.open_run_db(db_path)          # db_path = recovered_dir / "run.db"

# 2. Record the run
run_id = run_db.new_run(db_conn,
                        tool_version="6.0",
                        started=started_at,
                        source=target,
                        source_size=total or 0,
                        mode="full" if not progressive_mode else "progressive")

# 3. Log each found piece
run_db.add_candidate(db_conn, run_id, offset, length, label, ext, sha256,
                     validated=1, validator="jpeg", confidence=0.98,
                     fragments=1, gap_bytes=0, name_guess="IMG_0001.JPG",
                     name_source="filesystem", mft_record=None,
                     carved_path=carved_name, triage="plaintext")
run_db.add_fragment(db_conn, candidate_id, ordinal, offset, length)

# 4. Log the zero/entropy map (one row per region)
run_db.add_region(db_conn, run_id, start, length, zeros, entropy, kind="data")

# 5. The report writer closes the DB and emits report.md / report.csv / report.html
run_db.write_report(db_conn, run_id, out_dir)
```

### Public functions reference

| Function | Purpose |
|---|---|
| `open_run_db(path)` | Open (create) the DB at `path`; returns a connection. Creates the folder if missing. |
| `new_run(conn, tool_version, started, source, source_size, mode, signature_set, workers, status, resume_offset, cap_policy)` | Insert one run row; returns its `id`. |
| `update_run(conn, run_id, **fields)` | Change a run's fields (status, resume_offset, ...). Whitelist-guarded. |
| `add_candidate(conn, run_id, offset, length, label, ext, sha256, validated, validator, confidence, fragments, gap_bytes, name_guess, name_source, mft_record, carved_path, triage)` | Insert one candidate. |
| `add_fragment(conn, candidate_id, ordinal, offset, length)` | Record one piece of a fragmented candidate. |
| `add_region(conn, run_id, start, length, zeros, entropy, kind)` | Record one region of the source map. |
| `add_signature_counts(conn, run_id, counts)` | Record how often each signature fired. |
| `add_destination(conn, run_id, path, free_bytes)` | Record an extractable folder and its free space. |
| `add_plan(conn, run_id, rule, planned_bytes)` | Record the selection rule and the bytes it planned. |
| `ranked(conn, run_id, ext=None, min_length=None, max_length=None, validated_only=False, min_confidence=None, name_like=None, limit=None, offset=None)` | Candidates best-first, with filters; returns `sqlite3.Row` list. |
| `summary(conn, run_id)` | Counts and bytes per extension (with validated counts). |
| `dedupe_by_sha256(conn, run_id)` | Group candidates by `sha256` ("already recovered?"). |
| `write_report(conn, run_id, out_dir)` | Write `report.md`, `report.csv`, `report.html`; finalise the run. |

### `ranked()` filters (all optional)

`ext`, `min_length`, `max_length`, `validated_only`, `min_confidence`,
`name_like`, `limit`, `offset`. The default order is: validated first, then
confidence, then length, then `id`.

---

## 4. `size is None` device case

A raw volume device refuses a seek-to-end, so its size is `None` rather than `0`.
The probe therefore reports `"source size unknown"` and computes the verdict from
the partition/volume evidence (OEM id + entropy), not from size. The run database
records this transparently: the `source_size` row for that run is `0`, and the
`report` states the device caveat so the operator is not misled.
