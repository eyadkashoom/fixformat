#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_selftest.py - tests for run_db.py, the v6 run database (offline).

What it proves, in the order the module is used
-----------------------------------------------
  1. the schema: every table and the `ranked` view, foreign keys ON, WAL on,
     and opening the same file twice changes nothing;
  2. a run row round-trips, and a bad mode / cap policy is refused by name;
  3. 50 candidates over 5 extensions (1 KB to 900 MB, some validated, some
     duplicates by sha256, some fragmented) insert with their defaults;
  4. ranked() returns the documented order (validated, confidence, length,
     id) and every filter returns exactly the rows a Python-side filter
     returns - the two answers must agree, or the report lies;
  5. summary() totals per extension and the top-10 list;
  6. plan_extraction() refuses a destination that is too small, fills a big
     one, never plans more than min(25% of free, 100 GB) per destination and
     never more than 200,000 files (checked at the real limit, with 200,010
     rows, not with a patched constant);
  7. dedupe_by_sha256() finds the duplicate groups and keeps the member the
     priority order would extract first;
  8. write_report() writes report.md, report.csv and a self-contained
     report.html, all non-empty, with one row per candidate.

Exit code 0 when every assertion passed, 1 otherwise. Every check prints
either "ok" or "FAIL <what>", so a non-programmer can read the transcript and
see which expectation broke.

Run:  python\\python.exe db_selftest.py
"""

import csv
import hashlib
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_db  # noqa: E402

MB = 1024 ** 2
GB = 1024 ** 3
TB = 1024 ** 4

PASSED = [0]
FAILURES = []


# ----------------------------------------------------------------------
# the tiny test harness
# ----------------------------------------------------------------------

def check(condition, what):
    """Record one assertion; print ok or FAIL so the transcript is readable."""
    if condition:
        PASSED[0] += 1
        print("  ok   %s" % what)
    else:
        FAILURES.append(what)
        print("  FAIL %s" % what)
    return bool(condition)


def section(title):
    print("\n== %s ==" % title)


def ids_of(rows):
    """The ids of a query result, as a plain list."""
    return [row["id"] for row in rows]


# ----------------------------------------------------------------------
# the fixture: 50 candidates, 5 extensions
# ----------------------------------------------------------------------

# (extension, how many, biggest, smallest). The biggest starts at 900 MB and the
# smallest is exactly 1 KB, so the sizes really span the documented range.
EXT_SPREAD = (
    (".jpg", 14, 900 * MB, 20 * 1024),
    (".pdf", 10, 620 * MB, 8 * 1024),
    (".docx", 8, 480 * MB, 12 * 1024),
    (".md", 10, 300 * MB, 1024),
    (".zip", 8, 120 * MB, 5 * 1024),
)

LABELS = {".jpg": "jpeg-magic", ".pdf": "pdf-eof", ".docx": "zip-docx",
          ".md": "markdown-text", ".zip": "zip-eocd"}
VALIDATORS = {".jpg": "jpeg-eoi", ".pdf": "pdf-eof", ".docx": "docx-xml",
              ".md": "utf8-text", ".zip": "zip-central"}

# Which candidate copies whose sha256, so dedupe has real work to do:
# (the copy, the original). Five pairs, five groups of two.
DUPLICATE_PAIRS = ((4, 1), (18, 10), (29, 23), (42, 39), (47, 45))


def geometric(top, bottom, count):
    """count sizes falling from top to bottom, so one list covers 1 KB..900 MB."""
    if count <= 1:
        return [int(top)]
    step = (float(bottom) / float(top)) ** (1.0 / (count - 1))
    return [int(round(top * step ** index)) for index in range(count)]


def build_fixture():
    """
    The 50 candidates, as dicts, before they are written.

    The same list is used to write the database and to compute the expected
    answers in Python, so an error in the SQL shows up as a disagreement.
    """
    rows = []
    index = 0
    for ext, count, top, bottom in EXT_SPREAD:
        for position, size in enumerate(geometric(top, bottom, count)):
            validated = (index % 3 == 0)
            fragmented = (index % 11 == 0)
            name_source = "mft" if index % 4 == 0 else "content"
            rows.append({
                "id": index + 1,
                "offset": 4096 + index * (4 * MB),
                "length": size,
                "label": LABELS[ext],
                "ext": ext,
                # extensions are passed in a deliberately untidy form below, so
                # the normaliser is exercised, not just the happy path
                "ext_input": ext.lstrip(".").upper() if ext == ".jpg" else ext.lstrip("."),
                "sha256": hashlib.sha256(("candidate-%d" % index).encode("ascii")).hexdigest(),
                "validated": 1 if validated else 0,
                "validator": VALIDATORS[ext] if validated else "",
                # plenty of ties on purpose: the length and id tiebreaks must show
                "confidence": round(0.60 + 0.02 * (index % 9), 2),
                "fragments": 2 if fragmented else 1,
                "gap_bytes": 65536 if fragmented else 0,
                "name_guess": ("photo_%02d%s" % (index, ext)) if validated else "",
                "name_source": name_source,
                "mft_record": (1000 + index * 3) if name_source == "mft" else None,
                "carved_path": ("carved/hit_%02d.bin" % index) if validated else "",
                "triage": ("TEXT" if ext == ".md" else "COMPRESSED"),
                "position": position,
            })
            index += 1

    for copy_index, original_index in DUPLICATE_PAIRS:
        rows[copy_index]["sha256"] = rows[original_index]["sha256"]

    # pin the two ends of the documented range, so the claim in the docstring
    # is a fact of the fixture and not an accident of rounding
    rows[0]["length"] = 900 * MB
    rows[[row["ext"] for row in rows].index(".md") + 9]["length"] = 1024
    return rows


def priority(candidate):
    """The documented order: validated, confidence, length, id."""
    return (-candidate["validated"], -candidate["confidence"],
            -candidate["length"], candidate["id"])


def expected_ids(fixture, keep=None):
    """The ids ranked() must return, for an optional Python-side predicate."""
    ordered = sorted(fixture, key=priority)
    if keep is None:
        return [item["id"] for item in ordered]
    return [item["id"] for item in ordered if keep(item)]


def expected_by_ext(fixture):
    totals = {}
    for item in fixture:
        entry = totals.setdefault(item["ext"], {"files": 0, "bytes": 0,
                                                "validated": 0,
                                                "validated_bytes": 0})
        entry["files"] += 1
        entry["bytes"] += item["length"]
        if item["validated"]:
            entry["validated"] += 1
            entry["validated_bytes"] += item["length"]
    return totals


def capacity(free):
    """The planner limit, written out separately from the module's version."""
    return max(0, min(int(free * 25 / 100), 100 * GB, free - 2048 * MB))


# ----------------------------------------------------------------------
# 1 + 2: schema, pragmas, the run row
# ----------------------------------------------------------------------

def test_schema_and_run(conn, path):
    section("1. schema, foreign keys, WAL and idempotent open")

    source = open(run_db.__file__, encoding="utf-8").read()
    check(isinstance(run_db.SCHEMA, str) and "CREATE TABLE" in run_db.SCHEMA,
          "SCHEMA is a string containing CREATE TABLE statements")
    check("import raw_scan" not in source and "from raw_scan" not in source,
          "run_db.py imports nothing from raw_scan (it must run standalone)")

    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name in ("run", "region", "candidate", "fragment", "signature",
                 "destination", "plan"):
        check(name in tables, "table %s exists" % name)

    views = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view'")}
    check("ranked" in views, "view ranked exists (the plan's read-only order)")

    check(conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
          "PRAGMA foreign_keys is ON")
    check(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal",
          "journal_mode is WAL")

    run_columns = [item[0] for item in conn.execute(
        "SELECT * FROM run LIMIT 0").description]
    for column in ("tool_version", "started", "source", "source_size", "mode",
                   "signature_set", "workers", "status", "resume_offset",
                   "cap_policy"):
        check(column in run_columns, "run.%s exists (plan 9b + 9d)" % column)

    candidate_columns = [item[0] for item in conn.execute(
        "SELECT * FROM candidate LIMIT 0").description]
    for column in ("offset", "length", "label", "ext", "sha256", "validated",
                   "validator", "confidence", "fragments", "gap_bytes",
                   "name_guess", "name_source", "mft_record", "carved_path",
                   "triage"):
        check(column in candidate_columns, "candidate.%s exists" % column)

    # opening the same file again must be a no-op, not an error and not a
    # second set of tables
    second = run_db.open_run_db(path)
    try:
        again = {row["name"] for row in second.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        check(again == tables, "opening the same database twice changes nothing")
        check(second.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
              "the second connection also has foreign keys ON")
    finally:
        second.close()

    memory = run_db.open_run_db(":memory:")
    try:
        check(isinstance(memory, sqlite3.Connection),
              "open_run_db(':memory:') works (WAL is simply not applied there)")
    finally:
        memory.close()

    section("2. the run row")
    run_id = run_db.new_run(
        conn, tool_version="6.0-selftest", started="2026-10-29 10:00:00",
        source="F:\\image_test.img", source_size=477 * GB,
        mode="report-first", signature_set="v5-default (61 signatures)",
        workers=6, status="running", resume_offset=0, cap_policy="")
    check(run_id == 1, "new_run() returns the first run id (1)")

    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    for field, expected in (
            ("tool_version", "6.0-selftest"),
            ("started", "2026-10-29 10:00:00"),
            ("source", "F:\\image_test.img"),
            ("source_size", 477 * GB),
            ("mode", "report-first"),
            ("signature_set", "v5-default (61 signatures)"),
            ("workers", 6),
            ("status", "running"),
            ("resume_offset", 0),
            ("cap_policy", "")):
        check(row[field] == expected,
              "run.%s round-trips as %r" % (field, expected))

    for bad_mode, label in (("carve-now", "an unknown mode"),
                            ("extract-as-we-go", "the plan's alias mode")):
        try:
            stored = run_db.new_run(conn, mode=bad_mode)
        except ValueError:
            check(label == "an unknown mode",
                  "new_run() refuses %s with a readable ValueError" % label)
        else:
            check(label == "the plan's alias mode"
                  and conn.execute("SELECT mode FROM run WHERE id = ?",
                                   (stored,)).fetchone()[0] == "extract-while-scanning",
                  "new_run() accepts 'extract-as-we-go' and stores "
                  "'extract-while-scanning'")
            conn.execute("DELETE FROM run WHERE id = ?", (stored,))
            conn.commit()

    try:
        run_db.new_run(conn, cap_policy="keep-most")
    except ValueError:
        check(True, "new_run() refuses an unknown cap policy")
    else:
        check(False, "new_run() refuses an unknown cap policy")

    try:
        run_db.new_run(conn, colour="blue")
    except ValueError as error:
        check("colour" in str(error),
              "new_run() names the field it does not know (a typo cannot vanish)")
    else:
        check(False, "new_run() rejects an unknown keyword argument")

    changed = run_db.update_run(conn, run_id, status="stopped",
                                resume_offset=123456789,
                                cap_policy="stop-carving")
    check(changed == 3, "update_run() reports the three fields it changed")
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    check(row["status"] == "stopped" and row["resume_offset"] == 123456789
          and row["cap_policy"] == "stop-carving",
          "update_run() stores status, resume_offset and cap_policy (plan 9d)")

    try:
        run_db.update_run(conn, run_id, nonsense=1)
    except ValueError:
        check(True, "update_run() refuses an unknown field")
    else:
        check(False, "update_run() refuses an unknown field")

    # foreign keys must actually bite: a candidate for a run that does not
    # exist is a bug in the pipeline, not a row to keep
    try:
        conn.execute(
            "INSERT INTO candidate (run_id, offset, length) VALUES (?, ?, ?)",
            (9999, 0, 10))
        conn.commit()
        check(False, "a candidate for a missing run is refused (foreign key)")
    except sqlite3.IntegrityError:
        check(True, "a candidate for a missing run is refused (foreign key)")
    conn.rollback()
    return run_id


# ----------------------------------------------------------------------
# 3: the 50 candidates
# ----------------------------------------------------------------------

def test_candidates(conn, run_id, fixture):
    section("3. 50 candidates across 5 extensions")
    inserted = []
    for item in fixture:
        inserted.append(run_db.add_candidate(
            conn, run_id, offset=item["offset"], length=item["length"],
            label=item["label"], ext=item["ext_input"], sha256=item["sha256"],
            validated=bool(item["validated"]), validator=item["validator"],
            confidence=item["confidence"], fragments=item["fragments"],
            gap_bytes=item["gap_bytes"], name_guess=item["name_guess"],
            name_source=item["name_source"], mft_record=item["mft_record"],
            carved_path=item["carved_path"], triage=item["triage"]))

    check(inserted == list(range(1, 51)),
          "add_candidate() returns ids 1..50 in order")
    check(len(set(inserted)) == 50, "every candidate got a distinct id")

    extensions = [row[0] for row in conn.execute(
        "SELECT DISTINCT ext FROM candidate WHERE run_id = ? ORDER BY ext",
        (run_id,))]
    check(extensions == [".docx", ".jpg", ".md", ".pdf", ".zip"],
          "extensions are normalised to lower case with a leading dot "
          "(inputs were 'JPG', 'pdf', '.docx', ...)")

    row = conn.execute("SELECT * FROM candidate WHERE id = ?",
                       (inserted[0],)).fetchone()
    check(row["validated"] == 1 and isinstance(row["validated"], int),
          "validated=True is stored as the integer 1 (WHERE validated = 1 works)")
    row = conn.execute("SELECT * FROM candidate WHERE id = ?",
                       (inserted[1],)).fetchone()
    check(row["validated"] == 0, "validated=False is stored as 0")
    check(row["name_guess"] == "" and row["carved_path"] == "",
          "an unvalidated candidate defaults to no name and no carved path")

    defaults = conn.execute("SELECT * FROM candidate WHERE id = 50").fetchone()
    check(defaults["fragments"] >= 1, "fragments defaults to at least 1")

    # fragment rows for the fragmented candidates
    fragments = 0
    for item in fixture:
        if item["fragments"] == 2:
            run_db.add_fragment(conn, item["id"], 0, item["offset"], 4096)
            run_db.add_fragment(conn, item["id"], 1, item["offset"] + 2 * MB,
                                item["length"] - 4096)
            fragments += 2
    stored = conn.execute("SELECT COUNT(*) FROM fragment").fetchone()[0]
    check(stored == fragments,
          "add_fragment() stored %d rows for the fragmented candidates" % fragments)

    run_db.add_region(conn, run_id, 0, 20 * GB, zeros=20 * GB, entropy=0.0,
                      kind="zero")
    run_db.add_region(conn, run_id, 20 * GB, 100 * GB, zeros=0,
                      entropy=7.92, kind="compressed")
    run_db.add_region(conn, run_id, 120 * GB, 1 * GB, zeros=2048, entropy=4.1)
    check(conn.execute("SELECT COUNT(*) FROM region").fetchone()[0] == 3,
          "add_region() stored the zero / compressed / data regions")
    try:
        run_db.add_region(conn, run_id, 0, 1, kind="wobbly")
    except ValueError:
        check(True, "add_region() refuses an unknown region kind")
    else:
        check(False, "add_region() refuses an unknown region kind")

    written = run_db.add_signature_counts(conn, run_id, {
        "%PDF-": 410, "\\x89PNG": 3120, ".md": 2004,
        "PK\\x03\\x04": {"hex": "504b0304", "ext": "docx", "hits": 88},
        "RIFF": ("52494646", "wav", 17),
    })
    check(written == 5, "add_signature_counts() wrote one row per signature")
    hits = conn.execute("SELECT SUM(hits) FROM signature WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    check(hits == 410 + 3120 + 2004 + 88 + 17,
          "add_signature_counts() stored the counts (plain, dict and tuple forms)")
    rich = conn.execute("SELECT * FROM signature WHERE label = ?",
                        ("PK\\x03\\x04",)).fetchone()
    check(rich["hex"] == "504b0304" and rich["ext"] == ".docx" and rich["hits"] == 88,
          "the dict form of a signature keeps its hex, ext and hits")

    try:
        run_db.add_signature_counts(conn, run_id, ["not", "a", "dict"])
    except ValueError:
        check(True, "add_signature_counts() refuses something that is not a dict")
    else:
        check(False, "add_signature_counts() refuses something that is not a dict")

    try:
        run_db.add_candidate(conn, run_id, colour="blue")
    except ValueError:
        check(True, "add_candidate() rejects an unknown keyword argument")
    else:
        check(False, "add_candidate() rejects an unknown keyword argument")


# ----------------------------------------------------------------------
# 4: ranked() and every filter
# ----------------------------------------------------------------------

def test_ranked(conn, run_id, fixture):
    section("4. ranked(): the order, and each filter against its own Python answer")

    rows = run_db.ranked(conn, run_id)
    check(len(rows) == 50, "ranked() returns all 50 candidates")
    check(ids_of(rows) == expected_ids(fixture),
          "ranked() order is validated DESC, confidence DESC, length DESC, id ASC")

    keys = [(-row["validated"], -row["confidence"], -row["length"], row["id"])
            for row in rows]
    check(keys == sorted(keys), "the returned order is already sorted by that key")

    top = rows[0]
    best = sorted(fixture, key=priority)[0]
    check(top["id"] == best["id"] and top["validated"] == 1,
          "the first row is a validated candidate")
    check(all(rows[i]["validated"] >= rows[i + 1]["validated"]
              for i in range(len(rows) - 1)),
          "no validated candidate ever sits below an unvalidated one")
    check(isinstance(rows[0], sqlite3.Row) and "length" in rows[0].keys(),
          "rows are sqlite3.Row, so row['length'] and dict(row) both work")

    cases = (
        ("ext as one string", dict(ext=".pdf"),
         lambda c: c["ext"] == ".pdf"),
        ("ext as a list", dict(ext=["jpg", ".docx"]),
         lambda c: c["ext"] in (".jpg", ".docx")),
        ("min_length 100 MB", dict(min_length=100 * MB),
         lambda c: c["length"] >= 100 * MB),
        ("max_length 200 KB", dict(max_length=200 * 1024),
         lambda c: c["length"] <= 200 * 1024),
        ("validated_only", dict(validated_only=True),
         lambda c: c["validated"] == 1),
        ("min_confidence 0.70", dict(min_confidence=0.70),
         lambda c: c["confidence"] >= 0.70),
        ("name_like 'photo_0' (substring)", dict(name_like="photo_0"),
         lambda c: "photo_0" in (c["name_guess"] + c["carved_path"]).lower()),
        ("name_like 'PHOTO_03' (case folded)", dict(name_like="PHOTO_03"),
         lambda c: "photo_03" in (c["name_guess"] + c["carved_path"]).lower()),
        ("name_like with a real '_' stays literal", dict(name_like="photo_1"),
         lambda c: "photo_1" in (c["name_guess"] + c["carved_path"]).lower()),
        ("min_length + max_length together",
         dict(min_length=1024 * 1024, max_length=50 * MB),
         lambda c: 1024 * 1024 <= c["length"] <= 50 * MB),
        ("ext + validated_only + min_length",
         dict(ext=[".jpg", ".pdf"], validated_only=True, min_length=10 * MB),
         lambda c: c["ext"] in (".jpg", ".pdf") and c["validated"] == 1
         and c["length"] >= 10 * MB),
        ("a filter nothing matches", dict(min_length=2 * TB),
         lambda c: c["length"] >= 2 * TB),
    )
    for label, filters, predicate in cases:
        got = ids_of(run_db.ranked(conn, run_id, **filters))
        want = expected_ids(fixture, predicate)
        check(got == want,
              "filter %s -> %d rows, identical to the Python answer"
              % (label, len(want)))

    # a '_' in the search text must be a literal underscore, not SQL's "any one
    # character": the two rules differ on this exact pair of names
    names_db = run_db.open_run_db(":memory:")
    try:
        names_run = run_db.new_run(names_db, source="names.img")
        run_db.add_candidate(names_db, names_run, offset=0, length=10, ext="md",
                             name_guess="my_file.md")
        run_db.add_candidate(names_db, names_run, offset=10, length=10, ext="md",
                             name_guess="myXfile.md")
        check(ids_of(run_db.ranked(names_db, names_run, name_like="my_file")) == [1],
              "name_like 'my_file' matches only the literal 'my_file' row")
        check(run_db.ranked(names_db, names_run, name_like="myfile") == [],
              "name_like 'myfile' does not match through the '_' (wildcards are escaped)")
        check(ids_of(run_db.ranked(names_db, names_run, name_like="my_fil")) == [1],
              "name_like is a substring, not a prefix")
    finally:
        names_db.close()

    page = run_db.ranked(conn, run_id, limit=10)
    page2 = run_db.ranked(conn, run_id, limit=10, offset=10)
    check(len(page) == 10 and len(page2) == 10, "limit returns exactly that many rows")
    check(ids_of(page)[0] == expected_ids(fixture)[0]
          and ids_of(page2)[0] == expected_ids(fixture)[10],
          "limit and offset page through the same order")

    check(run_db.ranked(conn, run_id + 100) == [],
          "a run with no candidates returns an empty list, not an error")


# ----------------------------------------------------------------------
# 5: summary()
# ----------------------------------------------------------------------

def test_summary(conn, run_id, fixture):
    section("5. summary(): totals per extension and the top 10")
    info = run_db.summary(conn, run_id)
    expected = expected_by_ext(fixture)

    check(info["candidates"] == 50, "summary counts 50 candidates")
    check(info["validated"] == sum(1 for c in fixture if c["validated"]),
          "summary counts %d validated" % sum(1 for c in fixture if c["validated"]))
    check(info["unvalidated"] == info["candidates"] - info["validated"],
          "unvalidated is the remainder")
    check(info["bytes"] == sum(c["length"] for c in fixture),
          "summary total bytes = %s" % run_db.fmt_bytes(sum(c["length"] for c in fixture)))
    check(info["validated_bytes"] == sum(c["length"] for c in fixture if c["validated"]),
          "summary validated bytes are the validated rows only")
    check(info["carved_files"] == sum(1 for c in fixture if c["carved_path"]),
          "summary counts the carved candidates")
    check(info["extension_count"] == 5, "summary sees 5 extensions")
    check(len(info["by_extension"]) == 5, "by_extension has one row per extension")

    for item in info["by_extension"]:
        want = expected[item["ext"]]
        check(item["files"] == want["files"] and item["bytes"] == want["bytes"]
              and item["validated"] == want["validated"]
              and item["validated_bytes"] == want["validated_bytes"],
              "by_extension %s: %d files, %d validated, %s"
              % (item["ext"], want["files"], want["validated"],
                 run_db.fmt_bytes(want["bytes"])))

    sizes = [item["bytes"] for item in info["by_extension"]]
    check(sizes == sorted(sizes, reverse=True), "by_extension is ordered by bytes, biggest first")
    check(len(info["top_extensions"]) == 5, "top_extensions is the whole list when there are 5")
    check(info["top_extensions"][0]["ext"] == ".jpg",
          "the top extension by bytes is .jpg (900 MB candidate)")
    check(info["top_extensions"] == info["by_extension"][:10],
          "top_extensions is by_extension cut at ten rows")
    check(info["run"]["mode"] == "report-first" and info["run"]["status"] == "done",
          "summary carries the run row (mode, status) for the report header")

    empty = run_db.summary(conn, run_id + 100)
    check(empty["candidates"] == 0 and empty["bytes"] == 0
          and empty["by_extension"] == [] and empty["top_extensions"] == [],
          "summary of an empty run is zeroes, not None")


# ----------------------------------------------------------------------
# 6: plan_extraction()
# ----------------------------------------------------------------------

def test_plan(conn, run_id, fixture):
    section("6. plan_extraction(): the caps, the fits, the destinations")

    check(run_db.OUTPUT_CAP_PERCENT == 25, "OUTPUT_CAP_PERCENT is 25")
    check(run_db.OUTPUT_CAP_GB == 100, "OUTPUT_CAP_GB is 100")
    check(run_db.OUTPUT_CAP_FILES == 200000, "OUTPUT_CAP_FILES is 200000")
    check(run_db.FREE_RESERVE_MB == 2048, "FREE_RESERVE_MB is 2048")

    total_bytes = sum(c["length"] for c in fixture)
    total_validated = sum(c["length"] for c in fixture if c["validated"])
    validated_count = sum(1 for c in fixture if c["validated"])

    check(run_db.destination_capacity(0) == 0, "a full destination has capacity 0")
    for free in (0, 2 * GB, 9 * GB, 40 * GB, 500 * GB, 2 * TB):
        check(run_db.destination_capacity(free) == capacity(free),
              "capacity(%s) = min(25%% of free, 100 GB, free - 2 GB) = %s"
              % (run_db.fmt_bytes(free), run_db.fmt_bytes(capacity(free))))

    small = run_db.plan_extraction(conn, run_id, "everything",
                                   {"C:/small": 3 * GB})
    check(small["fits"] is False,
          "a 3 GB destination cannot hold %s: fits False" % run_db.fmt_bytes(total_bytes))
    check(small["selected_bytes"] == total_bytes,
          "the plan measured the actual candidate bytes, not a hit count")
    check(small["planned_bytes"] <= capacity(3 * GB),
          "planned %s never exceeds the capacity of the small disk (%s)"
          % (run_db.fmt_bytes(small["planned_bytes"]),
             run_db.fmt_bytes(capacity(3 * GB))))
    check(small["planned_bytes"] > 0, "it still plans what does fit on the small disk")
    check(small["skipped_bytes"] == total_bytes - small["planned_bytes"],
          "skipped_bytes is the remainder the small disk cannot take")
    check(small["destinations_used"] == ["C:/small"],
          "destinations_used lists the one folder it would write to")

    big = run_db.plan_extraction(conn, run_id, "everything",
                                 {"F:/big": 900 * GB})
    check(big["fits"] is True,
          "a 900 GB destination holds everything: fits True")
    check(big["planned_bytes"] == total_bytes and big["planned_files"] == 50,
          "everything means every candidate and the exact byte total")
    check(big["skipped_bytes"] == 0 and big["skipped_files"] == 0,
          "nothing is skipped when everything fits")
    check(capacity(900 * GB) == 100 * GB,
          "the 100 GB ceiling binds on a 900 GB disk (25% would be 225 GB)")

    tight = run_db.plan_extraction(conn, run_id, "everything",
                                   {"G:/tight": 12 * GB})
    check(tight["fits"] is False and tight["skipped_files"] > 0,
          "a 12 GB destination only partly takes the data (fits False)")
    check(tight["planned_bytes"] <= capacity(12 * GB),
          "and no more than 25% of it is planned")

    split = run_db.plan_extraction(conn, run_id, "everything",
                                   [("C:/small", 3 * GB), ("F:/big", 900 * GB)])
    check(split["fits"] is True and split["planned_bytes"] == total_bytes,
          "split across two disks everything fits")
    check(split["destinations_used"] == ["C:/small", "F:/big"],
          "both destinations are used, in the order they were given")
    check(sum(d["planned_bytes"] for d in split["destinations"]) == split["planned_bytes"],
          "the per-destination bytes add up to the plan total")
    check(all(d["planned_bytes"] <= d["capacity_bytes"] for d in split["destinations"]),
          "no destination is planned beyond its own capacity")

    validated = run_db.plan_extraction(conn, run_id, "validated-only",
                                       {"F:/big": 900 * GB})
    check(validated["planned_bytes"] == total_validated,
          "validated-only plans exactly the validated bytes (%s)"
          % run_db.fmt_bytes(total_validated))
    check(validated["planned_files"] == validated_count,
          "validated-only plans %d files" % validated_count)

    documents = run_db.plan_extraction(conn, run_id, "documents",
                                       {"F:/big": 900 * GB})
    doc_bytes = sum(c["length"] for c in fixture
                    if c["ext"] in run_db.DOC_EXTENSIONS)
    check(documents["planned_bytes"] == doc_bytes,
          "documents plans .pdf/.docx/.md only (%s)" % run_db.fmt_bytes(doc_bytes))
    check(all(item["ext"] in run_db.DOC_EXTENSIONS
              for item in documents["by_extension"]),
          "the documents breakdown contains document extensions only")

    custom = run_db.plan_extraction(conn, run_id,
                                    {"ext": [".jpg"], "min_length": 100 * MB},
                                    {"F:/big": 900 * GB})
    jpg_big = sum(c["length"] for c in fixture
                  if c["ext"] == ".jpg" and c["length"] >= 100 * MB)
    check(custom["planned_bytes"] == jpg_big,
          "a dict rule (my own filter) plans exactly those bytes")
    check(all(item["ext"] == ".jpg" for item in custom["by_extension"]),
          "a dict rule reaches the breakdown as given")

    check(sorted(item["ext"] for item in big["by_extension"]) ==
          sorted(set(c["ext"] for c in fixture)),
          "the per-extension breakdown covers all five extensions")
    check(sum(item["bytes"] for item in big["by_extension"]) == big["planned_bytes"],
          "the per-extension bytes add up to the plan total")
    check(big["by_extension"] == sorted(big["by_extension"],
                                       key=lambda item: (-item["bytes"], item["ext"])),
          "the breakdown is ordered by bytes, biggest first")

    for label, free in (("1 TB", 1 * TB), ("500 GB", 500 * GB),
                        ("40 GB", 40 * GB), ("9 GB", 9 * GB)):
        plan = run_db.plan_extraction(conn, run_id, "everything", {"X:/d": free})
        check(all(d["planned_bytes"] <= min(free * 25 / 100, 100 * GB)
                  for d in plan["destinations"]),
              "on a %s disk no destination is planned beyond min(25%%, 100 GB)" % label)

    check(run_db.plan_extraction(conn, run_id, "everything", {})["planned_bytes"] == 0,
          "with no destination nothing is planned (and nothing crashes)")

    try:
        run_db.plan_extraction(conn, run_id, "only-the-good-ones", {"F:/big": 900 * GB})
    except ValueError as error:
        check("only-the-good-ones" in str(error),
              "an unknown rule is refused and quoted back")
    else:
        check(False, "an unknown rule is refused and quoted back")

    # recording: the plan row and the destination bytes, so a partial
    # extraction stays explainable (plan 9d)
    run_db.add_destination(conn, run_id, "F:/big", 900 * GB)
    run_db.add_destination(conn, run_id, "C:/small", 3 * GB)
    recorded = run_db.plan_extraction(conn, run_id, "validated-only",
                                      {"F:/big": 900 * GB}, record=True)
    row = conn.execute("SELECT * FROM plan WHERE run_id = ? ORDER BY id DESC",
                       (run_id,)).fetchone()
    check(row is not None and row["rule"] == "validated-only"
          and row["planned_bytes"] == recorded["planned_bytes"],
          "record=True writes the rule and the planned bytes into plan")
    destination = conn.execute(
        "SELECT * FROM destination WHERE run_id = ? AND path = ?",
        (run_id, "F:/big")).fetchone()
    check(destination["planned_bytes"] == recorded["planned_bytes"],
          "record=True stores planned_bytes on the destination it used")
    check(run_db.add_plan(conn, run_id, "documents", 123456) > 0,
          "add_plan() inserts a plan row on its own")


def test_file_cap(work_dir):
    """
    The 200,000-file limit, tested at the real limit.

    The limit is a constant, so a test that patches it to 5 would prove only
    that the constant is read. 200,010 rows cost a second to insert and prove
    the planner stops at exactly 200,000 files and says so.
    """
    section("6b. the 200,000-file ceiling, at the real number")
    path = os.path.join(work_dir, "cap.db")
    conn = run_db.open_run_db(path)
    try:
        run_id = run_db.new_run(conn, source="many-files.img", mode="report-first")
        rows = [(run_id, index, 1024, ".md", 0) for index in range(200010)]
        started = time.time()
        conn.executemany(
            "INSERT INTO candidate (run_id, offset, length, ext, validated)"
            " VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
        elapsed = time.time() - started
        check(conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 200010,
              "inserted 200,010 candidates in %.1fs" % elapsed)

        plan = run_db.plan_extraction(conn, run_id, "everything", {"Z:/big": 10 * TB})
        check(plan["planned_files"] == 200000,
              "the planner stops at exactly 200,000 files of 200,010")
        check(plan["planned_bytes"] == 200000 * 1024,
              "and plans 200,000 x 1 KB, not 200,010 x 1 KB")
        check(plan["skipped_files"] == 10, "the ten it did not plan are reported")
        check(plan["fits"] is False, "fits is False when the file cap is what stopped it")
        check(plan["planned_bytes"] <= 100 * GB,
              "the bytes stay under the 100 GB ceiling as well")
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 7: dedupe
# ----------------------------------------------------------------------

def test_dedupe(conn, run_id, fixture):
    section("7. dedupe_by_sha256(): the duplicate groups")
    groups = run_db.dedupe_by_sha256(conn, run_id)
    check(len(groups) == len(DUPLICATE_PAIRS),
          "found %d duplicate groups (one per duplicated sha256)" % len(DUPLICATE_PAIRS))
    check(sum(g["count"] - 1 for g in groups) == len(DUPLICATE_PAIRS),
          "the groups account for every duplicate row")
    check(all(g["count"] >= 2 for g in groups), "no group has a single member")
    check(all(g["candidates"] and len(g["candidates"]) == g["count"] for g in groups),
          "each group carries its members")

    ordered = [g["bytes_wasted"] for g in groups]
    check(ordered == sorted(ordered, reverse=True),
          "groups are ordered by bytes_wasted, biggest saving first")

    for group in groups:
        members = sorted(fixture, key=priority)
        same = [c for c in members if c["sha256"] == group["sha256"]]
        check(group["count"] == len(same),
              "group %s has %d members" % (group["sha256"][:8], len(same)))
        check(group["keep_id"] == same[0]["id"],
              "group %s keeps the member the priority order extracts first"
              % group["sha256"][:8])
        check(group["bytes"] == sum(c["length"] for c in same),
              "group %s total bytes are the sum of its members" % group["sha256"][:8])
        check(group["bytes_wasted"] == group["bytes"] - same[0]["length"],
              "group %s bytes_wasted excludes the copy it would keep" % group["sha256"][:8])

    empty = run_db.open_run_db(":memory:")
    try:
        other = run_db.new_run(empty, source="no-hashes.img")
        run_db.add_candidate(empty, other, offset=0, length=10, ext="md")
        run_db.add_candidate(empty, other, offset=10, length=10, ext="md")
        check(run_db.dedupe_by_sha256(empty, other) == [],
              "two candidates with no sha256 are not called duplicates")
    finally:
        empty.close()


# ----------------------------------------------------------------------
# 8: the report
# ----------------------------------------------------------------------

def test_report(conn, run_id, fixture, work_dir):
    section("8. write_report(): report.md, report.csv, report.html")
    out_dir = os.path.join(work_dir, "report")
    written = run_db.write_report(conn, run_id, out_dir)

    for name in ("md", "csv", "html"):
        path = written[name]
        check(os.path.isfile(path) and os.path.getsize(path) > 0,
              "%s exists and is not empty (%s)"
              % (os.path.basename(path), run_db.fmt_bytes(os.path.getsize(path))))
    check(written["md"].endswith("report.md") and written["csv"].endswith("report.csv")
          and written["html"].endswith("report.html"),
          "the three files have the documented names")

    with open(written["md"], encoding="utf-8") as handle:
        markdown = handle.read()
    check(markdown.startswith("# "), "report.md starts with a level-1 heading")
    check("fixformat run %d" % run_id in markdown, "report.md names the run")
    check("report-first" in markdown, "report.md quotes the mode")
    check("Cap policy" in markdown, "report.md explains the cap policy (plan 9d)")
    check(run_db.fmt_bytes(sum(c["length"] for c in fixture)) in markdown,
          "report.md shows the total size")
    for row in fixture:
        if row["validated"] and row["name_guess"]:
            break
    check(row["name_guess"] in markdown, "report.md lists the guessed names")

    section_lines = markdown.split("## Candidates")[-1].splitlines()
    table_rows = [line for line in section_lines if line.startswith("| ")]
    check(len(table_rows) == 50 + 1,
          "report.md has one table row per candidate plus the header (%d)"
          % len(table_rows))
    check("|---|---|---|---|---|---|---|---|---|---|---|" in section_lines,
          "report.md has a valid markdown table header rule")
    check(markdown.count("\n") > 50, "report.md has real content, not a stub")

    with open(written["csv"], encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.reader(handle))
    check(csv_rows[0] == list(run_db.CSV_COLUMNS),
          "report.csv header is the module's fixed column order")
    check(len(csv_rows) == 51, "report.csv has one row per candidate (51 with the header)")
    check(len(set(row[0] for row in csv_rows[1:])) == 50,
          "report.csv has 50 distinct candidate ids")
    ranked_ids = [str(row["id"]) for row in run_db.ranked(conn, run_id)]
    check([row[0] for row in csv_rows[1:]] == ranked_ids,
          "report.csv is in the same priority order as ranked()")
    check([row[4] for row in csv_rows[1:]][0] == sorted(fixture, key=priority)[0]["label"],
          "the first CSV data row is the top-ranked candidate")
    width = len(csv_rows[0])
    check(all(len(row) == width for row in csv_rows),
          "every CSV row has the same number of fields (no comma broke a column)")

    with open(written["html"], encoding="utf-8") as handle:
        page = handle.read()
    check(page.lstrip().startswith("<!DOCTYPE html>"), "report.html is a complete page")
    check("<style>" in page and "</html>" in page, "report.html carries its own CSS")
    check("http://" not in page and "https://" not in page and "//cdn" not in page,
          "report.html loads nothing from the network (self-contained)")
    check("<script" not in page and "<img" not in page,
          "report.html has no scripts and no images")
    check("prefers-color-scheme" in page,
          "report.html is theme-neutral (reads on a light and a dark Windows theme)")
    check(page.count("class='cand'") == 50,
          "report.html has one table row per candidate")
    check(run_db.fmt_bytes(sum(c["length"] for c in fixture)) in page,
          "report.html shows the total size")
    check("report-first" in page, "report.html quotes the mode")

    # a name with a quote must not be able to break the page
    tricky = run_db.add_candidate(conn, run_id, offset=1, length=2, ext="txt",
                                  name_guess="a <b>&'\" name.txt",
                                  sha256="deadbeef")
    page2 = run_db.write_report(conn, run_id, out_dir)
    with open(page2["html"], encoding="utf-8") as handle:
        page = handle.read()
    check("<b>" not in page.split("</style>")[-1] or "&lt;b&gt;" in page,
          "a file name containing HTML is escaped in report.html")
    check("&lt;b&gt;" in page, "the escaped name appears as &lt;b&gt;")
    csv_again = open(page2["csv"], encoding="utf-8-sig", newline="")
    try:
        rows_again = list(csv.reader(csv_again))
        check(len(rows_again) == 52, "the report follows the table on a re-write")
    finally:
        csv_again.close()
    conn.execute("DELETE FROM candidate WHERE id = ?", (tricky,))
    conn.commit()


def test_cascade(work_dir):
    """Deleting a run must take its candidates with it (foreign keys work)."""
    section("9. deleting a run deletes its rows")
    path = os.path.join(work_dir, "cascade.db")
    conn = run_db.open_run_db(path)
    try:
        run_id = run_db.new_run(conn, source="throwaway.img")
        candidate = run_db.add_candidate(conn, run_id, offset=0, length=1024)
        run_db.add_fragment(conn, candidate, 0, 0, 1024)
        run_db.add_region(conn, run_id, 0, 1024)
        conn.execute("DELETE FROM run WHERE id = ?", (run_id,))
        conn.commit()
        remaining = sum(conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
                        for name in ("candidate", "fragment", "region"))
        check(remaining == 0, "candidates, fragments and regions are cascaded away")
    finally:
        conn.close()


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    fixture = build_fixture()
    check(len(fixture) == 50, "the fixture has 50 candidates")
    check(sorted(set(c["ext"] for c in fixture)) == [".docx", ".jpg", ".md", ".pdf", ".zip"],
          "the fixture spans 5 extensions")
    check(min(c["length"] for c in fixture) == 1024,
          "the smallest candidate is 1 KB")
    check(max(c["length"] for c in fixture) == 900 * MB,
          "the largest candidate is 900 MB")

    work_dir = tempfile.mkdtemp(prefix="fixformat_db_selftest_")
    path = os.path.join(work_dir, "run", "run.db")
    conn = run_db.open_run_db(path)
    try:
        run_id = test_schema_and_run(conn, path)
        run_db.update_run(conn, run_id, status="done")
        test_candidates(conn, run_id, fixture)
        test_ranked(conn, run_id, fixture)
        test_summary(conn, run_id, fixture)
        test_plan(conn, run_id, fixture)
        test_dedupe(conn, run_id, fixture)
        test_report(conn, run_id, fixture, work_dir)
    finally:
        conn.close()

    test_file_cap(work_dir)
    test_cascade(work_dir)

    total = PASSED[0] + len(FAILURES)
    print("")
    print("-" * 70)
    if FAILURES:
        print("FAILED: %d of %d assertions" % (len(FAILURES), total))
        for what in FAILURES:
            print("  - %s" % what)
        print("the temporary files are in %s" % work_dir)
        return 1
    print("PASSED: all %d assertions, in %s" % (total, work_dir))
    print("(run_db.py loaded from %s)" % run_db.__file__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
