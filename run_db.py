#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_db.py - the v6 run database: one SQLite file per search.

Why a database instead of the text log
--------------------------------------
v5 leaves an append-only text log, one JSON, a carved folder, an organised
folder and one static page. Nothing in that set can be filtered, ranked,
deduplicated or compared between runs. The questions a run really raises -
"only validated files over 100 KB", "have I already recovered this file",
"which MFT-named deleted file was never carved", "what would fit on the disk I
actually have" - are all queries, not greps. One database per run, in the run
folder next to provenance.json, answers them with one query and one commit.

sqlite3 3.49.1 ships inside the bundled interpreter (python\\python.exe), so
this costs no dependency, needs no pip and works from the DLC Boot stick. The
database lives in the OUTPUT folder, never on the source disk being scanned.

What this module provides
-------------------------
* SCHEMA            - the tables and the `ranked` view, applied idempotently
* open_run_db()     - open/create a run database (foreign keys, WAL, schema)
* new_run()         - record a run and return its id
* add_candidate()   - one hit, with the fields the report and the planner need
* add_fragment()    - one piece of a fragmented candidate
* add_region()      - the zero/entropy map of the source
* add_signature_counts() - how often each signature fired
* add_destination() - a folder the extraction may write to, and its free space
* add_plan()        - record the selection rule and the bytes it planned
* update_run()      - status / resume_offset / cap_policy as the run advances
* ranked()          - the priority order, with filters
* summary()         - counts and bytes per extension, validated counts
* plan_extraction() - what fits where, honouring the output cap
* dedupe_by_sha256()- the "have I already recovered this file" groups
* write_report()    - report.md, report.csv and a self-contained report.html

Rules this module follows
-------------------------
* every value reaches SQLite through a bound parameter ("?") - no value is
  ever formatted into a statement, so a file name containing a quote, a
  backslash or Arabic text cannot break or alter a query;
* every write function commits before it returns, because a run can be
  stopped at any moment (plan 9d) and everything already reported must
  survive the stop;
* nothing here imports raw_scan.py - the database is usable on its own, from
  the interactive pipeline and from a unit test alike.

Environment: Python standard library only, no network.
"""

import csv
import json
import os
import sqlite3
import sys
from datetime import datetime

# ----------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------

MB = 1024 ** 2
GB = 1024 ** 3

# Plan 9d, the two ways a run can work. The stored strings are the ones the
# report and provenance.json quote, so they must not drift.
MODE_EXTRACT = "extract-while-scanning"
MODE_REPORT = "report-first"
MODES = (MODE_EXTRACT, MODE_REPORT)

# "extract-as-we-go" is the wording used in one diagram of the plan; it means
# the same thing, so it is accepted and stored under the canonical name.
MODE_ALIASES = {
    "extract-as-we-go": MODE_EXTRACT,
    "extract": MODE_EXTRACT,
    "carve": MODE_EXTRACT,
    "report": MODE_REPORT,
}

# Plan 9d, the cap question: what happens when the cap is about to be reached.
CAP_STOP_CARVING = "stop-carving"
CAP_VALIDATED_ONLY = "validated-only"
CAP_CHECKPOINT = "checkpoint"
CAP_POLICIES = (CAP_STOP_CARVING, CAP_VALIDATED_ONLY, CAP_CHECKPOINT)

# The planner limits, taken verbatim from plan 9d. They are module constants so
# a test (or a future screen) can read them, and so there is exactly one place
# to change them - never a hidden number buried in a query.
OUTPUT_CAP_PERCENT = 25        # never plan more than this % of a destination
OUTPUT_CAP_GB = 100            # ... and never more than this many GB, whatever free space says
OUTPUT_CAP_FILES = 200000      # the index and the browsing page stop working long before the disk does
FREE_RESERVE_MB = 2048         # leave this much of the destination unused, for the OS and the log

# The region table is the zero/entropy map of the source (plan 9b).
REGION_KINDS = ("data", "zero", "compressed")

# The extraction rules the recovery plan screen offers (plan 9d).
RULE_EVERYTHING = "everything"
RULE_VALIDATED = "validated-only"
RULE_DOCUMENTS = "documents"

# Extensions that count as "documents only" in the plan screen.
DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
                  ".md", ".txt", ".rtf", ".odt", ".ods", ".csv", ".json")

# Fixed column order of report.csv. A later reader (or Excel, or a diff between
# two runs) can rely on the position of every column, so new columns are only
# ever appended at the end.
CSV_COLUMNS = ("id", "offset", "length", "ext", "label", "validated",
               "validator", "confidence", "fragments", "gap_bytes", "sha256",
               "name_guess", "name_source", "mft_record", "carved_path",
               "triage")

# The priority order from plan 9d: "extracts in priority order (validated
# first, then confidence, then size)". id breaks ties so two runs with the
# same data return the same order - a report that shuffles between reads is
# not an audit trail.
PRIORITY_ORDER = "validated DESC, confidence DESC, length DESC, id ASC"


# ----------------------------------------------------------------------
# the schema
# ----------------------------------------------------------------------

# Every statement is IF NOT EXISTS, so open_run_db() can be called on a fresh
# folder, on an existing run, or twice in a row, and the result is the same
# database either way. That matters because the pipeline calls it once per
# stage, and because a resumed run must never be asked to start over.
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS run (
    id            INTEGER PRIMARY KEY,
    tool_version  TEXT,
    started       TEXT,
    source        TEXT,
    source_size   INTEGER,
    mode          TEXT,
    signature_set TEXT,
    workers       INTEGER,
    status        TEXT,
    resume_offset INTEGER,
    cap_policy    TEXT
);

-- the zero/entropy map: where the data is, and where it is not
CREATE TABLE IF NOT EXISTS region (
    run_id  INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    start   INTEGER,
    length  INTEGER,
    zeros   INTEGER,
    entropy REAL,
    kind    TEXT
);

CREATE TABLE IF NOT EXISTS candidate (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    offset      INTEGER,
    length      INTEGER,
    label       TEXT,
    ext         TEXT,
    sha256      TEXT,
    validated   INTEGER,
    validator   TEXT,
    confidence  REAL,
    fragments   INTEGER,
    gap_bytes   INTEGER,
    name_guess  TEXT,
    name_source TEXT,
    mft_record  INTEGER,
    carved_path TEXT,
    triage      TEXT
);

-- one row per piece of a fragmented candidate; ordinal 0 is the head
CREATE TABLE IF NOT EXISTS fragment (
    candidate_id INTEGER NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    ordinal      INTEGER,
    offset       INTEGER,
    length       INTEGER
);

CREATE TABLE IF NOT EXISTS signature (
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    label  TEXT,
    hex    TEXT,
    ext    TEXT,
    hits   INTEGER
);

-- folders the extraction may write to, with the free space seen when the
-- plan was made: the plan is only explainable if the number it used is kept
CREATE TABLE IF NOT EXISTS destination (
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    path          TEXT,
    free_bytes    INTEGER,
    planned_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS plan (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    rule          TEXT,
    planned_bytes INTEGER,
    done_bytes    INTEGER,
    created       TEXT
);

CREATE INDEX IF NOT EXISTS candidate_run_idx      ON candidate(run_id);
CREATE INDEX IF NOT EXISTS candidate_sha_idx      ON candidate(sha256);
CREATE INDEX IF NOT EXISTS candidate_ext_idx      ON candidate(ext);
CREATE INDEX IF NOT EXISTS candidate_length_idx   ON candidate(length);
CREATE INDEX IF NOT EXISTS fragment_candidate_idx ON fragment(candidate_id);
CREATE INDEX IF NOT EXISTS region_run_idx         ON region(run_id);
CREATE INDEX IF NOT EXISTS signature_run_idx      ON signature(run_id);
CREATE INDEX IF NOT EXISTS destination_run_idx    ON destination(run_id);
CREATE INDEX IF NOT EXISTS plan_run_idx           ON plan(run_id);

-- The plan's view, kept for anyone who reads the file with the sqlite3 shell.
-- ranked() in Python adds the filters and the run_id, which a view cannot
-- take as a parameter.
CREATE VIEW IF NOT EXISTS ranked AS
  SELECT * FROM candidate ORDER BY validated DESC, confidence DESC, length DESC;
"""


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------

def fmt_bytes(n):
    """Render a byte count the way the rest of the tool does: '41.20 GB'."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return "%d B" % int(n) if unit == "B" else "%.2f %s" % (n, unit)
        n /= 1024.0


def now_text():
    """Timestamp stored in every date column: sortable, local, no locale."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_ext(value):
    """
    '.JPG', 'jpg' and ' .jpg ' all become '.jpg'.

    One spelling per extension is what makes the by_extension summary, the
    'documents only' plan rule and a GROUP BY query agree with each other.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        text = "." + text
    return text


def _mode(value):
    """Accept the two mode names (and the plan's alias) and return the stored one."""
    text = str(value or "").strip().lower()
    if text in MODES:
        return text
    if text in MODE_ALIASES:
        return MODE_ALIASES[text]
    raise ValueError(
        "Unknown run mode %r. Use %r (scan and carve in one pass) or %r "
        "(scan only, write a report, extract later)."
        % (value, MODE_EXTRACT, MODE_REPORT))


def _cap_policy(value):
    """'' means 'the cap question was never asked'; otherwise one of the three."""
    text = str(value or "").strip().lower()
    if text == "":
        return ""
    if text in CAP_POLICIES:
        return text
    raise ValueError(
        "Unknown cap policy %r. Use %r, %r or %r (or '' when the cap was "
        "never reached)." % (value, CAP_STOP_CARVING, CAP_VALIDATED_ONLY,
                             CAP_CHECKPOINT))


def _int(value, default=0):
    """An integer column value from anything a caller might pass, never None."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("Expected a whole number, got %r." % (value,))


def _text(value):
    """A text column value; None becomes '' so no query has to test for both."""
    return "" if value is None else str(value)


def _ensure_columns(conn):
    """
    Add columns a slightly older run database is missing.

    The plan introduces mode and cap_policy as ALTER TABLE additions, so a
    database written by an earlier v6 stage (or by a half-finished run) may not
    have them yet. Checking the cursor description avoids putting a table name
    into a statement at all - PRAGMA cannot take a bound parameter, so the only
    safe way to stay free of string-formatted SQL is not to need it.
    """
    additions = (
        ("SELECT * FROM run LIMIT 0", "mode",
         "ALTER TABLE run ADD COLUMN mode TEXT"),
        ("SELECT * FROM run LIMIT 0", "cap_policy",
         "ALTER TABLE run ADD COLUMN cap_policy TEXT"),
        ("SELECT * FROM candidate LIMIT 0", "triage",
         "ALTER TABLE candidate ADD COLUMN triage TEXT"),
        ("SELECT * FROM destination LIMIT 0", "planned_bytes",
         "ALTER TABLE destination ADD COLUMN planned_bytes INTEGER"),
    )
    for probe, column, alter in additions:
        # the statements above are literals written here, not built from input
        names = [item[0] for item in conn.execute(probe).description]
        if column not in names:
            conn.execute(alter)


def open_run_db(path):
    """
    Open (or create) the run database at `path` and return the connection.

    Three settings matter for a scan that runs for hours and can be stopped:

    * foreign keys ON, so a fragment can never point at a candidate that does
      not exist and a run cannot be half-deleted;
    * WAL, so a reader (the report screen) does not block the writer (the scan)
      and a crash leaves a database that still opens;
    * synchronous NORMAL, the WAL-safe middle ground: a power cut can lose the
      last slice, but SQLite still cannot corrupt the file, and fsyncing every
      candidate would cost more than the carving.

    The folder is created if missing, because the run folder is often named
    after the source and does not exist yet on the first call.
    """
    if path != ":memory:":
        folder = os.path.dirname(os.path.abspath(path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row

    # a second process (a report opened while a scan is running) waits instead
    # of failing with "database is locked"
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.Error:
        # a read-only folder keeps the default journal; the database still works
        pass
    conn.execute("PRAGMA synchronous = NORMAL")

    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


# ----------------------------------------------------------------------
# writing a run
# ----------------------------------------------------------------------

# The keyword arguments new_run() understands. Kept in one tuple so the error
# message for a typo can list them, and so update_run() can share the list.
RUN_FIELDS = ("tool_version", "started", "source", "source_size", "mode",
              "signature_set", "workers", "status", "resume_offset",
              "cap_policy")


def new_run(conn, tool_version="6.0", started=None, source="", source_size=0,
            mode=MODE_EXTRACT, signature_set="", workers=1, status="running",
            resume_offset=0, cap_policy="", **extra):
    """
    Insert one run row and return its id.

    A run row is the answer to "what produced this folder": which tool version,
    which source, which mode, which signature set, how many workers, and - when
    the run was stopped or the cap was hit - where it stopped and why. status is
    'running' at the start; the pipeline sets 'done', 'stopped' or 'failed' with
    update_run() as it goes.
    """
    if extra:
        raise ValueError(
            "new_run() does not know the field(s) %s. Accepted fields: %s."
            % (", ".join(sorted(extra)), ", ".join(RUN_FIELDS)))

    cur = conn.execute(
        "INSERT INTO run (tool_version, started, source, source_size, mode,"
        " signature_set, workers, status, resume_offset, cap_policy)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_text(tool_version), started or now_text(), _text(source),
         _int(source_size), _mode(mode), _text(signature_set),
         _int(workers, 1), _text(status) or "running", _int(resume_offset),
         _cap_policy(cap_policy)))
    conn.commit()
    return cur.lastrowid


def update_run(conn, run_id, **fields):
    """
    Change fields of an existing run (status, resume_offset, cap_policy, ...).

    The column names are chosen from the RUN_FIELDS whitelist and the matching
    SQL text is written here, so no caller-supplied string ever becomes part of
    a statement; only the values are bound.
    """
    statements = {
        "tool_version": "UPDATE run SET tool_version = ? WHERE id = ?",
        "started": "UPDATE run SET started = ? WHERE id = ?",
        "source": "UPDATE run SET source = ? WHERE id = ?",
        "source_size": "UPDATE run SET source_size = ? WHERE id = ?",
        "mode": "UPDATE run SET mode = ? WHERE id = ?",
        "signature_set": "UPDATE run SET signature_set = ? WHERE id = ?",
        "workers": "UPDATE run SET workers = ? WHERE id = ?",
        "status": "UPDATE run SET status = ? WHERE id = ?",
        "resume_offset": "UPDATE run SET resume_offset = ? WHERE id = ?",
        "cap_policy": "UPDATE run SET cap_policy = ? WHERE id = ?",
    }
    if not fields:
        return 0
    unknown = [name for name in fields if name not in statements]
    if unknown:
        raise ValueError(
            "update_run() does not know the field(s) %s. Accepted fields: %s."
            % (", ".join(sorted(unknown)), ", ".join(RUN_FIELDS)))

    changed = 0
    for name in fields:
        value = fields[name]
        if name == "mode":
            value = _mode(value)
        elif name == "cap_policy":
            value = _cap_policy(value)
        elif name in ("source_size", "workers", "resume_offset"):
            value = _int(value)
        else:
            value = _text(value)
        changed += conn.execute(statements[name], (value, run_id)).rowcount
    conn.commit()
    return changed


def add_candidate(conn, run_id, offset=0, length=0, label="", ext="", sha256="",
                  validated=0, validator="", confidence=0.0, fragments=1,
                  gap_bytes=0, name_guess="", name_source="", mft_record=None,
                  carved_path="", triage="", **extra):
    """
    Insert one candidate (one hit) and return its id.

    Defaults describe the ordinary case: a single-fragment hit that has not
    been validated, has no name and has not been carved. validated is stored as
    0/1 so `WHERE validated = 1` works without a string comparison;
    confidence is 0.0..1.0 but not clamped here - a validator that reports
    0..100 would show its mistake in the report instead of being silently
    halved.
    """
    if extra:
        raise ValueError(
            "add_candidate() does not know the field(s) %s." % ", ".join(sorted(extra)))

    cur = conn.execute(
        "INSERT INTO candidate (run_id, offset, length, label, ext, sha256,"
        " validated, validator, confidence, fragments, gap_bytes, name_guess,"
        " name_source, mft_record, carved_path, triage)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_int(run_id), _int(offset), _int(length), _text(label), norm_ext(ext),
         _text(sha256), 1 if validated else 0, _text(validator),
         float(confidence or 0.0), _int(fragments, 1), _int(gap_bytes),
         _text(name_guess), _text(name_source),
         None if mft_record is None else _int(mft_record),
         _text(carved_path), _text(triage)))
    conn.commit()
    return cur.lastrowid


def add_fragment(conn, candidate_id, ordinal, offset, length):
    """
    Record one piece of a fragmented candidate (ordinal 0 is the head).

    The pieces are what makes a carved file honest: a report can show that the
    file was assembled from three places with two gaps, instead of presenting a
    spliced guess as a whole file.
    """
    cur = conn.execute(
        "INSERT INTO fragment (candidate_id, ordinal, offset, length)"
        " VALUES (?, ?, ?, ?)",
        (_int(candidate_id), _int(ordinal), _int(offset), _int(length)))
    conn.commit()
    return cur.lastrowid


def add_region(conn, run_id, start, length, zeros=0, entropy=0.0, kind="data"):
    """
    Record one region of the source: how much of it is zeros and how random.

    Fifty rows are enough to draw a whole-disk map - 'the first 40 GB is empty,
    everything from here is compressed' - which is the answer to "where is the
    data on this disk".
    """
    text = str(kind or "data").strip().lower()
    if text not in REGION_KINDS:
        raise ValueError(
            "Unknown region kind %r. Use one of: %s."
            % (kind, ", ".join(REGION_KINDS)))
    cur = conn.execute(
        "INSERT INTO region (run_id, start, length, zeros, entropy, kind)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_int(run_id), _int(start), _int(length), _int(zeros),
         float(entropy or 0.0), text))
    conn.commit()
    return cur.lastrowid


def add_signature_counts(conn, run_id, counts):
    """
    Store how often each signature fired, and return the number of rows written.

    `counts` maps a label to its hit count, which is the shape the scanner
    already builds. Two richer shapes are accepted because the signature table
    also records the pattern itself: a value of (hex, ext, hits) or a dict with
    keys 'hex', 'ext' and 'hits'. Anything else is a plain count.
    """
    if not isinstance(counts, dict):
        raise ValueError(
            "add_signature_counts() expects a dict of label -> hits, got %s."
            % type(counts).__name__)

    rows = []
    for label, value in counts.items():
        hex_value, ext_value, hits = "", "", 0
        if isinstance(value, dict):
            hex_value = _text(value.get("hex"))
            ext_value = norm_ext(value.get("ext"))
            hits = _int(value.get("hits"))
        elif isinstance(value, (tuple, list)) and len(value) == 3:
            hex_value, ext_value, hits = (_text(value[0]),
                                          norm_ext(value[1]), _int(value[2]))
        else:
            hits = _int(value)
        rows.append((_int(run_id), _text(label), hex_value, ext_value, hits))

    conn.executemany(
        "INSERT INTO signature (run_id, label, hex, ext, hits)"
        " VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def add_destination(conn, run_id, path, free_bytes):
    """
    Record a folder the extraction may write to, with the free space seen now.

    planned_bytes starts at 0 and is filled in by plan_extraction(record=True)
    or by the extraction itself, so "the plan said 41 GB and the folder got
    41 GB" can be checked months later.
    """
    cur = conn.execute(
        "INSERT INTO destination (run_id, path, free_bytes, planned_bytes)"
        " VALUES (?, ?, ?, ?)",
        (_int(run_id), _text(path), _int(free_bytes), 0))
    conn.commit()
    return cur.lastrowid


def add_plan(conn, run_id, rule, planned_bytes):
    """
    Record a selection rule and the bytes it planned.

    This is the row that makes a partial extraction explainable: months later,
    "why are only 6,105 of 8,412 candidates here" is answered by the rule text
    and the planned byte count, not by someone's memory.
    """
    cur = conn.execute(
        "INSERT INTO plan (run_id, rule, planned_bytes, done_bytes, created)"
        " VALUES (?, ?, ?, ?, ?)",
        (_int(run_id), _rule_text(rule), _int(planned_bytes), 0, now_text()))
    conn.commit()
    return cur.lastrowid


# ----------------------------------------------------------------------
# reading: the priority order and the filters
# ----------------------------------------------------------------------

def _quote_filter(values):
    """'.jpg', 'JPG' or ['.jpg', 'png'] -> a list of stored extension names."""
    if isinstance(values, (list, tuple, set, frozenset)):
        items = values
    else:
        items = [values]
    cleaned = [norm_ext(item) for item in items]
    return [item for item in cleaned if item]


def ranked(conn, run_id, ext=None, min_length=None, max_length=None,
           validated_only=False, min_confidence=None, name_like=None,
           limit=None, offset=None):
    """
    The candidates of one run, best first, optionally filtered.

    Order is validated, then confidence, then length, then id - the order the
    extraction uses and the order the report shows, so an interrupted run that
    is read again delivers the same files first.

    Filters (all optional, all bound as parameters):
      ext            - one extension or a list ('.jpg', ['pdf', '.docx'])
      min_length     - at least this many bytes
      max_length     - at most this many bytes
      validated_only - only candidates a validator accepted
      min_confidence - confidence >= this value
      name_like      - case-insensitive substring of the guessed or carved name
      limit, offset  - paging for a screen; no limit means every row

    Returns a list of sqlite3.Row, so a caller can use row["length"] and
    dict(row).
    """
    where = ["run_id = ?"]
    params = [_int(run_id)]

    extensions = _quote_filter(ext) if ext is not None else []
    if extensions:
        where.append("ext IN (%s)" % ", ".join("?" for _ in extensions))
        params.extend(extensions)

    if min_length is not None:
        where.append("length >= ?")
        params.append(_int(min_length))
    if max_length is not None:
        where.append("length <= ?")
        params.append(_int(max_length))
    if validated_only:
        where.append("validated = 1")
    if min_confidence is not None:
        where.append("confidence >= ?")
        params.append(float(min_confidence))
    if name_like is not None and str(name_like) != "":
        # a substring search, not a LIKE pattern: a user typing 'photo' means
        # 'photo', so the wildcards are escaped and the match is on lower case
        # of both sides
        needle = "%" + (str(name_like).replace("\\", "\\\\")
                        .replace("%", "\\%").replace("_", "\\_").lower()) + "%"
        where.append("(LOWER(COALESCE(name_guess, '')) LIKE ? ESCAPE '\\'"
                     " OR LOWER(COALESCE(carved_path, '')) LIKE ? ESCAPE '\\')")
        params.extend((needle, needle))

    sql = "SELECT * FROM candidate WHERE " + " AND ".join(where)
    sql += " ORDER BY " + PRIORITY_ORDER
    if limit is not None:
        sql += " LIMIT ?"
        params.append(_int(limit))
        if offset:
            sql += " OFFSET ?"
            params.append(_int(offset))
    return conn.execute(sql, params).fetchall()


def summary(conn, run_id):
    """
    Counts and bytes for one run: the numbers the report and the plan show.

    "by_extension" is the full list ordered by bytes (a scan of a text-heavy
    disk is .md and .txt, not .jpg), "top_extensions" is the same list cut to
    ten rows, and every row carries its validated count as well as its total -
    a type with 3,000 hits and 12 validated files is telling a different story
    from one with 300 hits and 290 validated.
    """
    run_id = _int(run_id)
    totals = conn.execute(
        "SELECT COUNT(*) AS files,"
        " COALESCE(SUM(length), 0) AS bytes,"
        " COALESCE(SUM(CASE WHEN validated = 1 THEN 1 ELSE 0 END), 0) AS validated,"
        " COALESCE(SUM(CASE WHEN validated = 1 THEN length ELSE 0 END), 0)"
        "   AS validated_bytes,"
        " COALESCE(SUM(CASE WHEN COALESCE(carved_path, '') <> '' THEN 1 ELSE 0 END), 0)"
        "   AS carved_files"
        " FROM candidate WHERE run_id = ?", (run_id,)).fetchone()

    rows = conn.execute(
        "SELECT COALESCE(ext, '') AS ext, COUNT(*) AS files,"
        " COALESCE(SUM(length), 0) AS bytes,"
        " COALESCE(SUM(CASE WHEN validated = 1 THEN 1 ELSE 0 END), 0) AS validated,"
        " COALESCE(SUM(CASE WHEN validated = 1 THEN length ELSE 0 END), 0)"
        "   AS validated_bytes"
        " FROM candidate WHERE run_id = ?"
        " GROUP BY COALESCE(ext, '')"
        " ORDER BY bytes DESC, files DESC, ext ASC", (run_id,)).fetchall()

    by_extension = [dict(row) for row in rows]
    run_row = conn.execute(
        "SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()

    return {
        "run_id": run_id,
        "run": dict(run_row) if run_row is not None else {},
        "candidates": int(totals["files"]),
        "validated": int(totals["validated"]),
        "unvalidated": int(totals["files"]) - int(totals["validated"]),
        "bytes": int(totals["bytes"]),
        "validated_bytes": int(totals["validated_bytes"]),
        "carved_files": int(totals["carved_files"]),
        "extension_count": len(by_extension),
        "by_extension": by_extension,
        "top_extensions": by_extension[:10],
    }


def dedupe_by_sha256(conn, run_id):
    """
    Group candidates that hold the same content, biggest saving first.

    This is the "have I already recovered this file" question, and the point of
    the answer is disk space: one 900 MB video found four times is 2.7 GB that
    a second destination does not need to hold.

    Each group keeps the candidate the priority order would extract first
    (validated, then confidence, then size) and reports bytes_wasted as the
    bytes that would not be written if the copies were skipped. Candidates
    without a hash are never grouped - an empty hash is "not computed yet",
    not "identical to every other unhashed file".
    """
    run_id = _int(run_id)
    heads = conn.execute(
        "SELECT sha256, COUNT(*) AS cop, COALESCE(SUM(length), 0) AS bytes"
        " FROM candidate WHERE run_id = ? AND COALESCE(sha256, '') <> ''"
        " GROUP BY sha256 HAVING COUNT(*) > 1"
        " ORDER BY bytes DESC, sha256 ASC", (run_id,)).fetchall()
    if not heads:
        return []

    hashes = [row["sha256"] for row in heads]
    members = conn.execute(
        "SELECT * FROM candidate WHERE run_id = ? AND sha256 IN (%s)"
        " ORDER BY sha256 ASC, %s"
        % (", ".join("?" for _ in hashes), PRIORITY_ORDER),
        [run_id] + hashes).fetchall()

    by_hash = {}
    for row in members:
        by_hash.setdefault(row["sha256"], []).append(dict(row))

    groups = []
    for head in heads:
        rows = by_hash.get(head["sha256"], [])
        keep = rows[0] if rows else {}
        total = int(head["bytes"])
        groups.append({
            "sha256": head["sha256"],
            "count": int(head["cop"]),
            "bytes": total,
            "bytes_wasted": total - int(keep.get("length") or 0),
            "keep_id": keep.get("id"),
            "keep_rank": 0,
            "candidates": rows,
        })
    groups.sort(key=lambda group: (-group["bytes_wasted"], group["sha256"]))
    return groups


# ----------------------------------------------------------------------
# the recovery plan (plan 9d)
# ----------------------------------------------------------------------

def _rule_text(rule):
    """The rule as it is stored and quoted in the report."""
    if isinstance(rule, dict):
        return json.dumps(rule, sort_keys=True)
    return str(rule if rule is not None else RULE_EVERYTHING).strip().lower()


def rule_filters(rule):
    """
    Turn a plan screen answer into ranked() filters.

    'everything'      -> no filter
    'validated-only'  -> validated files only (plan screen option 2)
    'documents'       -> the document extensions (plan screen option 3)
    a dict            -> handed to ranked() unchanged (plan screen option 4,
                         "my own filter")
    """
    text = _rule_text(rule)
    if isinstance(rule, dict):
        return dict(rule)
    if text in ("", RULE_EVERYTHING, "all", "main"):
        return {}
    if text in (RULE_VALIDATED, "validated", "valid"):
        return {"validated_only": True}
    if text in (RULE_DOCUMENTS, "docs", "document"):
        return {"ext": list(DOC_EXTENSIONS)}
    raise ValueError(
        "Unknown recovery plan rule %r. Use %r, %r, %r, or a dict of filters "
        "such as {'ext': ['.pdf'], 'min_length': 102400}."
        % (rule, RULE_EVERYTHING, RULE_VALIDATED, RULE_DOCUMENTS))


def destination_capacity(free_bytes):
    """
    The most this planner will ever write to one destination.

    Three limits, the smallest wins:
      * OUTPUT_CAP_PERCENT of the free space (25%),
      * OUTPUT_CAP_GB (100 GB) however large the disk is,
      * the free space minus FREE_RESERVE_MB (2 GB), so the destination is
        never filled to the last byte - the report, the log and Windows itself
        need somewhere to live, and a full disk fails the run at the end.
    Returns 0 when the destination is too small to be useful.
    """
    free_bytes = _int(free_bytes)
    if free_bytes <= 0:
        return 0
    percent_cap = int(free_bytes * OUTPUT_CAP_PERCENT / 100.0)
    hard_cap = int(OUTPUT_CAP_GB * GB)
    reserve_cap = free_bytes - int(FREE_RESERVE_MB * MB)
    return max(0, min(percent_cap, hard_cap, reserve_cap))


def _destination_map(free_bytes_by_destination):
    """
    Accept {path: free_bytes} or [(path, free_bytes), ...] and return a list of
    (path, free_bytes) pairs in the order given - the order is the fill order,
    so the caller's first destination is the one that fills first.
    """
    if free_bytes_by_destination is None:
        return []
    if isinstance(free_bytes_by_destination, dict):
        items = list(free_bytes_by_destination.items())
    else:
        items = list(free_bytes_by_destination)
    pairs = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((_text(item[0]), _int(item[1])))
        else:
            raise ValueError(
                "A destination must be given as (path, free_bytes); got %r." % (item,))
    return pairs


def plan_extraction(conn, run_id, rule=RULE_EVERYTHING,
                    free_bytes_by_destination=None, record=False):
    """
    Work out what of this run would fit where, and report the exact bytes.

    The plan screen (plan 9d) is built from this one dict: how many candidates
    there are, how big they are, what they are, and what the disks can take.
    The numbers come from the candidate table - the actual lengths of the actual
    rows - never from a hit count multiplied by an average.

    Filling is greedy in priority order across the destinations in the order
    given: the best file goes to the first disk with room, the next to whatever
    has room left. That is also how the extraction itself will run, so the
    number this returns is the number of bytes written, not an estimate.

    Returns a dict with:
      selected_bytes / selected_files - everything the rule matched
      planned_bytes / planned_files   - what actually fits and will be written
      fits                            - True only when everything fits
      by_extension                    - the planned set, bytes first
      destinations                    - per destination: capacity, planned, files
      destinations_used               - just the paths it would write to
      skipped_bytes / skipped_files   - the remainder it will not write
      file_limit                      - OUTPUT_CAP_FILES, so the screen can
                                        say which limit was hit

    record=True also writes the plan row and the destination planned_bytes, so
    the decision is in the database rather than only on the screen.
    """
    run_id = _int(run_id)
    filters = rule_filters(rule)
    rule_text = _rule_text(rule)

    rows = ranked(conn, run_id, **filters)

    destinations = []
    for path, free in _destination_map(free_bytes_by_destination):
        destinations.append({
            "path": path,
            "free_bytes": free,
            "capacity_bytes": destination_capacity(free),
            "planned_bytes": 0,
            "files": 0,
        })

    planned_bytes = 0
    planned_files = 0
    by_ext = {}
    for row in rows:
        if planned_files >= OUTPUT_CAP_FILES:
            break
        size = int(row["length"] or 0)
        placed = False
        for dest in destinations:
            if dest["capacity_bytes"] - dest["planned_bytes"] >= size:
                dest["planned_bytes"] += size
                dest["files"] += 1
                planned_bytes += size
                planned_files += 1
                ext = row["ext"] or ""
                entry = by_ext.setdefault(ext, {"ext": ext, "files": 0, "bytes": 0})
                entry["files"] += 1
                entry["bytes"] += size
                placed = True
                break
        if not placed:
            # nothing has room for this one; later, smaller candidates may
            # still fit, so the loop keeps going rather than stopping here
            continue

    selected_bytes = sum(int(row["length"] or 0) for row in rows)
    selected_files = len(rows)
    breakdown = sorted(by_ext.values(),
                       key=lambda item: (-item["bytes"], item["ext"]))
    used = [dest for dest in destinations if dest["files"] > 0]

    result = {
        "run_id": run_id,
        "rule": rule_text,
        "selected_bytes": selected_bytes,
        "selected_files": selected_files,
        "planned_bytes": planned_bytes,
        "planned_files": planned_files,
        "fits": planned_files == selected_files and planned_bytes == selected_bytes,
        "by_extension": breakdown,
        "destinations": destinations,
        "destinations_used": [dest["path"] for dest in used],
        "skipped_bytes": selected_bytes - planned_bytes,
        "skipped_files": selected_files - planned_files,
        "file_limit": OUTPUT_CAP_FILES,
        "destinations_used_bytes": sum(dest["planned_bytes"] for dest in used),
    }

    if record:
        add_plan(conn, run_id, rule_text, planned_bytes)
        for dest in used:
            conn.execute(
                "UPDATE destination SET planned_bytes = ?"
                " WHERE run_id = ? AND path = ?",
                (dest["planned_bytes"], run_id, dest["path"]))
        conn.commit()

    return result


# ----------------------------------------------------------------------
# the report (plan 9b / 9d)
# ----------------------------------------------------------------------

# What each cap policy means, quoted in the report so the folder explains
# itself: "why are there only 2,000 of 8,000 hits here" has an answer.
CAP_POLICY_TEXT = {
    CAP_STOP_CARVING: "the output cap was reached and carving stopped; every hit is "
                      "still recorded in the database and can be carved later "
                      "without rescanning",
    CAP_VALIDATED_ONLY: "the output cap was reached and only validated files were "
                        "carved from then on",
    CAP_CHECKPOINT: "the output cap was reached and the run was stopped with a "
                    "checkpoint; resume it to continue",
}

MODE_TEXT = {
    MODE_EXTRACT: "scan and carve in one pass (needs free space equal to what it carves)",
    MODE_REPORT: "scan only; this report is the result, extraction is a separate plan",
}


def _md_cell(value):
    """One table cell: no line breaks inside a row, and no broken pipe table."""
    text = _text(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def _report_markdown(conn, run_id, info, rows):
    """The report.md text: what the run found, by type and in full."""
    run = info["run"]
    lines = []
    lines.append("# fixformat run %d - recovery report" % info["run_id"])
    lines.append("")
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append("| tool version | %s |" % _md_cell(run.get("tool_version")))
    lines.append("| started | %s |" % _md_cell(run.get("started")))
    lines.append("| source | %s |" % _md_cell(run.get("source")))
    lines.append("| source size | %s |" % fmt_bytes(run.get("source_size")))
    lines.append("| mode | %s |" % _md_cell(run.get("mode")))
    lines.append("| signature set | %s |" % _md_cell(run.get("signature_set")))
    lines.append("| workers | %s |" % _md_cell(run.get("workers")))
    lines.append("| status | %s |" % _md_cell(run.get("status")))
    lines.append("| resume offset | %s |" % _md_cell(run.get("resume_offset")))
    lines.append("")

    mode = run.get("mode") or ""
    lines.append("## How this run worked")
    lines.append("")
    lines.append("Mode: `%s` - %s." % (mode, MODE_TEXT.get(mode, "unknown mode")))
    policy = run.get("cap_policy") or ""
    if policy:
        lines.append("")
        lines.append("Cap policy: `%s` - %s." % (policy, CAP_POLICY_TEXT.get(policy, "unknown policy")))
    else:
        lines.append("")
        lines.append("Cap policy: none - the output cap was never reached, so every "
                     "candidate below was handled the same way.")
    lines.append("")

    lines.append("## What was found")
    lines.append("")
    lines.append("| measure | value |")
    lines.append("|---|---|")
    lines.append("| candidates | %d |" % info["candidates"])
    lines.append("| validated | %d |" % info["validated"])
    lines.append("| not validated | %d |" % info["unvalidated"])
    lines.append("| total size if everything is extracted | %s |" % fmt_bytes(info["bytes"]))
    lines.append("| validated size | %s |" % fmt_bytes(info["validated_bytes"]))
    lines.append("| already carved | %d |" % info["carved_files"])
    lines.append("| extensions | %d |" % info["extension_count"])
    lines.append("")

    lines.append("## By extension")
    lines.append("")
    lines.append("| ext | files | validated | bytes |")
    lines.append("|---|---|---|---|")
    for item in info["by_extension"]:
        lines.append("| %s | %d | %d | %s |"
                     % (_md_cell(item["ext"] or "(none)"), item["files"],
                        item["validated"], fmt_bytes(item["bytes"])))
    lines.append("")

    lines.append("## Top %d extensions by bytes" % len(info["top_extensions"]))
    lines.append("")
    lines.append("| # | ext | bytes | share |")
    lines.append("|---|---|---|---|")
    total = info["bytes"] or 1
    for index, item in enumerate(info["top_extensions"], 1):
        lines.append("| %d | %s | %s | %.1f%% |"
                     % (index, _md_cell(item["ext"] or "(none)"),
                        fmt_bytes(item["bytes"]), 100.0 * item["bytes"] / total))
    lines.append("")

    lines.append("## Candidates (%d), best first" % len(rows))
    lines.append("")
    lines.append("Order: validated, then confidence, then size. The same order the "
                 "extraction writes in, so an interrupted run still delivered the "
                 "best files first.")
    lines.append("")
    lines.append("| # | id | offset | length | ext | label | validated | conf | frags | name | sha256 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for index, row in enumerate(rows, 1):
        lines.append("| %d | %d | %d | %d | %s | %s | %s | %.2f | %d | %s | %s |"
                     % (index, row["id"], row["offset"], row["length"],
                        _md_cell(row["ext"]), _md_cell(row["label"]),
                        "yes" if row["validated"] else "no",
                        float(row["confidence"] or 0.0),
                        int(row["fragments"] or 0),
                        _md_cell(row["name_guess"]),
                        _md_cell(_text(row["sha256"])[:16])))
    lines.append("")
    return "\n".join(lines)


def _report_html(run_id, info, rows):
    """
    A self-contained page: no fonts, no scripts, no images, no CDN.

    The tool runs on a machine with no internet connection, and the report is
    opened from a USB stick months later, so every byte it needs is inside the
    file. The colours come from CSS variables that follow the reader's own
    light/dark setting (and fall back to a plain readable scheme where that is
    not supported), so the same file is legible on a dark Windows theme and on
    a printer.
    """
    import html as html_escape  # local import: only the HTML writer needs it

    escape = html_escape.escape
    run = info["run"]
    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append("<title>fixformat run %d - recovery report</title>" % run_id)
    parts.append("<style>")
    parts.append(":root{color-scheme:light dark;"
                 "--bg:#ffffff;--fg:#1b1b1b;--line:#c8c8c8;--head:#eeeeee;"
                 "--accent:#1565c0;--ok:#2e7d32;--muted:#5f5f5f}")
    parts.append("@media (prefers-color-scheme: dark){:root{"
                 "--bg:#161616;--fg:#e8e8e8;--line:#3d3d3d;--head:#232323;"
                 "--accent:#7fb3f0;--ok:#7cc47f;--muted:#a8a8a8}}")
    parts.append("body{font-family:Segoe UI,Arial,sans-serif;margin:20px;"
                 "background:var(--bg);color:var(--fg)}")
    parts.append("h1,h2{color:var(--accent)}")
    parts.append("table{border-collapse:collapse;width:100%;margin-bottom:18px}")
    parts.append("th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;"
                 "vertical-align:top;font-size:13px}")
    parts.append("th{background:var(--head)}")
    parts.append("td.num{text-align:right;font-variant-numeric:tabular-nums}")
    parts.append(".ok{color:var(--ok)}.muted{color:var(--muted)}")
    parts.append("code{font-family:Consolas,monospace}")
    parts.append("</style></head><body>")
    parts.append("<h1>fixformat run %d - recovery report</h1>" % run_id)

    parts.append("<table><tr><th>field</th><th>value</th></tr>")
    for label, value in (
            ("tool version", run.get("tool_version")),
            ("started", run.get("started")),
            ("source", run.get("source")),
            ("source size", fmt_bytes(run.get("source_size"))),
            ("mode", run.get("mode")),
            ("signature set", run.get("signature_set")),
            ("workers", run.get("workers")),
            ("status", run.get("status")),
            ("resume offset", run.get("resume_offset"))):
        parts.append("<tr><th>%s</th><td>%s</td></tr>"
                     % (escape(_text(label)), escape(_text(value))))
    parts.append("</table>")

    mode = run.get("mode") or ""
    parts.append("<h2>How this run worked</h2>")
    parts.append("<p>Mode <code>%s</code> - %s.</p>"
                 % (escape(mode), escape(MODE_TEXT.get(mode, "unknown mode"))))
    policy = run.get("cap_policy") or ""
    if policy:
        parts.append("<p>Cap policy <code>%s</code> - %s.</p>"
                     % (escape(policy), escape(CAP_POLICY_TEXT.get(policy, "unknown policy"))))
    else:
        parts.append("<p class='muted'>No cap policy recorded: the output cap was "
                     "never reached.</p>")

    parts.append("<h2>What was found</h2>")
    parts.append("<table><tr><th>measure</th><th>value</th></tr>")
    for label, value in (
            ("candidates", "%d" % info["candidates"]),
            ("validated", "%d" % info["validated"]),
            ("not validated", "%d" % info["unvalidated"]),
            ("total size if everything is extracted", fmt_bytes(info["bytes"])),
            ("validated size", fmt_bytes(info["validated_bytes"])),
            ("already carved", "%d" % info["carved_files"])):
        parts.append("<tr><th>%s</th><td class='num'>%s</td></tr>"
                     % (escape(label), escape(value)))
    parts.append("</table>")

    parts.append("<h2>By extension</h2>")
    parts.append("<table><tr><th>ext</th><th>files</th><th>validated</th>"
                 "<th>bytes</th><th>size</th></tr>")
    for item in info["by_extension"]:
        parts.append("<tr><td>%s</td><td class='num'>%d</td><td class='num ok'>%d</td>"
                     "<td class='num'>%d</td><td>%s</td></tr>"
                     % (escape(item["ext"] or "(none)"), item["files"],
                        item["validated"], item["bytes"], fmt_bytes(item["bytes"])))
    parts.append("</table>")

    parts.append("<h2>Top %d extensions by bytes</h2>" % len(info["top_extensions"]))
    parts.append("<table><tr><th>#</th><th>ext</th><th>bytes</th><th>share</th></tr>")
    total = info["bytes"] or 1
    for index, item in enumerate(info["top_extensions"], 1):
        parts.append("<tr><td class='num'>%d</td><td>%s</td><td class='num'>%s</td>"
                     "<td class='num'>%.1f%%</td></tr>"
                     % (index, escape(item["ext"] or "(none)"),
                        fmt_bytes(item["bytes"]), 100.0 * item["bytes"] / total))
    parts.append("</table>")

    parts.append("<h2>Candidates (%d), best first</h2>" % len(rows))
    parts.append("<table><tr><th>#</th><th>id</th><th>offset</th><th>length</th>"
                 "<th>ext</th><th>label</th><th>validated</th><th>confidence</th>"
                 "<th>fragments</th><th>name</th><th>sha256</th></tr>")
    for index, row in enumerate(rows, 1):
        parts.append(
            "<tr class='cand'><td class='num'>%d</td><td class='num'>%d</td>"
            "<td class='num'>%d</td><td class='num'>%d</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td class='num'>%.2f</td><td class='num'>%d</td>"
            "<td>%s</td><td><code>%s</code></td></tr>"
            % (index, row["id"], row["offset"], row["length"],
               escape(_text(row["ext"])), escape(_text(row["label"])),
               "yes" if row["validated"] else "no",
               float(row["confidence"] or 0.0), int(row["fragments"] or 0),
               escape(_text(row["name_guess"])), escape(_text(row["sha256"]))))
    parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def write_report(conn, run_id, out_dir):
    """
    Write report.md, report.csv and report.html into out_dir.

    One row per candidate in every file, in the same priority order, so the
    three can be cross-checked against each other and against the database.

    The CSV is written with a UTF-8 BOM and quoted fields: Arabic file names
    must open correctly in Excel on the Arabic Windows the tool ships for, and
    a name containing a comma must not shift a column.

    Returns {'md': path, 'csv': path, 'html': path}.
    """
    run_id = _int(run_id)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    info = summary(conn, run_id)
    rows = ranked(conn, run_id)

    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_report_markdown(conn, run_id, info, rows))

    csv_path = os.path.join(out_dir, "report.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([row[name] for name in CSV_COLUMNS])

    html_path = os.path.join(out_dir, "report.html")
    with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_report_html(run_id, info, rows))

    return {"md": md_path, "csv": csv_path, "html": html_path}


# ----------------------------------------------------------------------
# a command line, so the file can be looked at without the pipeline
# ----------------------------------------------------------------------

def main(argv=None):
    """
    `python run_db.py <run.db> report <out_dir>` or `... summary`.

    A stop-and-resume run leaves a database behind and no screen to read it
    with; this small entry point is the manual way in. It is deliberately
    read-only for the report command.
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2 or argv[1] not in ("report", "summary", "runs"):
        print("Usage: python run_db.py <run.db> runs|summary|report [out_dir]")
        print("  runs    - list the runs in this database")
        print("  summary - counts and bytes per extension for run 1 (or --run N)")
        print("  report  - write report.md/.csv/.html into out_dir (or the db folder)")
        return 2

    path, command = argv[0], argv[1]
    rest = argv[2:]
    run_id = 1
    if "--run" in rest:
        index = rest.index("--run")
        if index + 1 < len(rest):
            run_id = int(rest[index + 1])
        del rest[index:index + 2]

    if not os.path.isfile(path):
        print("No run database at %s." % path)
        return 2

    conn = open_run_db(path)
    try:
        if command == "runs":
            for row in conn.execute("SELECT id, started, status, mode, source FROM run"
                                    " ORDER BY id"):
                print("run %d  %s  %s  %s  %s"
                      % (row["id"], row["started"], row["status"], row["mode"],
                         row["source"]))
            return 0
        if command == "summary":
            info = summary(conn, run_id)
            print("run %d: %d candidates, %d validated, %s total"
                  % (info["run_id"], info["candidates"], info["validated"],
                     fmt_bytes(info["bytes"])))
            for item in info["by_extension"]:
                print("  %-8s %7d files %7d validated  %8s"
                      % (item["ext"] or "(none)", item["files"], item["validated"],
                         fmt_bytes(item["bytes"])))
            return 0
        out_dir = rest[0] if rest else os.path.dirname(os.path.abspath(path))
        written = write_report(conn, run_id, out_dir)
        for name, written_path in written.items():
            print("wrote %s" % written_path)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
