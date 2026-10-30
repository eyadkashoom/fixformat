#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v6 bundle automated tests (steps 2-12) — task t_1c14112e.

Covers, with happy paths + edge cases, using synthetic fixtures only:
  step 2  DB (run_db.py): schema/idempotent open, run CRUD, candidates,
          ranked order + filters, summary, dedupe, plan, destinations,
          regions/signatures/fragments, write_report, bad-mode/cap errors
  step 3  extraction modes: MODE_EXTRACT/MODE_REPORT + alias mapping
          (workspace extraction/modes.py AND run_db._mode canonicalization)
  step 4  plan screen (workspace plan_screen.py via public run_db API)
  step 5  reports (run_db.write_report + workspace reports_integration.py)
  step 6  validators (v6/validators.py: jpeg/png/pdf/zip/sqlite)
  step 7  assembly (v6/assembly.py: 0/1/2/3 fragments, gap bound)
  step 8  adaptive ops (performance/operations.select_op)
  step 9  entropy (operations.entropy)
  step 10 parallel penalty (operations.parallel_penalty)
  step 11 fast-scan calibration (operations.calibrate, reproducible)
  step 12 audit.txt (workspace audit_serialization.py + reports audit)
  Direct I/O (operations.open_direct safe fallback)
  integration: synthetic candidates -> validate -> assemble -> plan ->
          reports + audit chain
  disjointness: validators/assembly/operations import nothing from
          data/UI/report/perf peer modules.

No production source is modified. Exit 0 iff every check passes.
Run:  python test_v6_bundle.py   (from this workspace dir)
"""
import csv
import importlib.util
import os
import sqlite3
import sys
import tempfile

FIX = r"C:\Users\eyad\Documents\Codex\2026-09-27\fixformat\v6"
WS4 = os.path.join(FIX, "")  # consolidated into repo v6/
WS8 = os.path.join(FIX, "")  # consolidated into repo v6/
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FIX)
sys.path.insert(0, HERE)

import run_db  # noqa: E402
import validators  # noqa: E402
import assembly  # noqa: E402
from performance import operations  # noqa: E402

PASSED = [0]
FAILURES = []


def check(cond, what):
    if cond:
        PASSED[0] += 1
    else:
        FAILURES.append(what)
        print("FAIL " + what)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


modes = load_mod("ws_modes", os.path.join(FIX, "extraction", "modes.py"))
plan_screen = load_mod("ws_plan", os.path.join(FIX, "plan_screen.py"))
audit_mod = load_mod("ws_audit", os.path.join(FIX, "audit_serialization.py"))
reports_mod = load_mod("ws_reports", os.path.join(FIX, "reports_integration.py"))

# ---------------------------------------------------------------- step 2: DB
print("== step 2: run DB ==")
c = run_db.open_run_db(":memory:")
rid = run_db.new_run(c, tool_version="6.0", source="syn.img",
                     source_size=10 * 1024 * 1024, mode=run_db.MODE_REPORT)
check(isinstance(rid, int) and rid > 0, "new_run returns row id")

# bad mode / cap policy refused by name
try:
    run_db.new_run(c, mode="nope")
    check(False, "bad mode must raise")
except ValueError:
    check(True, "bad mode raises ValueError")
try:
    run_db.new_run(c, cap_policy="nope")
    check(False, "bad cap policy must raise")
except ValueError:
    check(True, "bad cap policy raises ValueError")
try:
    run_db.new_run(c, bogus_field=1)
    check(False, "unknown run field must raise")
except ValueError:
    check(True, "unknown run field raises ValueError")

# alias canonicalization matches workspace modes.py
check(run_db.MODE_EXTRACT == modes.MODE_EXTRACT, "run_db/modes MODE_EXTRACT agree")
check(run_db.MODE_REPORT == modes.MODE_REPORT, "run_db/modes MODE_REPORT agree")

# candidates: mixed validated/confidence/size/ext
id_jpg = run_db.add_candidate(c, rid, offset=1024, length=4096, label="a.jpg",
                              ext=".JPG", validated=1, confidence=0.95,
                              sha256="h1", fragments=1)
id_pdf = run_db.add_candidate(c, rid, offset=8192, length=2048, label="b.pdf",
                              ext="pdf", validated=0, confidence=0.50,
                              sha256="h2", fragments=2, gap_bytes=7)
id_dup = run_db.add_candidate(c, rid, offset=16384, length=4096, label="a2.jpg",
                              ext=".jpg", validated=1, confidence=0.80,
                              sha256="h1", fragments=1)
check(id_jpg and id_pdf and id_dup, "add_candidate returns ids")
# norm_ext: '.JPG'/'pdf' stored canonically
row = [dict(r) for r in run_db.ranked(c, rid) if r["id"] == id_jpg][0]
check(row["ext"] == ".jpg", "norm_ext canonicalizes '.JPG' -> '.jpg' (got %r)" % row["ext"])

# priority order: validated DESC, confidence DESC, length DESC, id ASC
rows = list(run_db.ranked(c, rid))
check([r["id"] for r in rows] == [id_jpg, id_dup, id_pdf],
      "ranked() priority order validated/conf/len/id (got %s)"
      % [r["id"] for r in rows])

# filters
check(len(run_db.ranked(c, rid, validated_only=True)) == 2, "validated_only filter")
check(len(run_db.ranked(c, rid, ext=".jpg")) == 2, "ext filter '.jpg'")
check(len(run_db.ranked(c, rid, ext=[".jpg", "pdf"])) == 3, "ext list filter")
check(len(run_db.ranked(c, rid, min_length=4096)) == 2, "min_length filter")
check(len(run_db.ranked(c, rid, max_length=2048)) == 1, "max_length filter")
check(len(run_db.ranked(c, rid, min_confidence=0.9)) == 1, "min_confidence filter")
pg = run_db.ranked(c, rid, limit=1, offset=1)
check(len(pg) == 1 and pg[0]["id"] == id_dup, "limit/offset paging")
check(len(run_db.ranked(c, 99999)) == 0, "unknown run_id ranks empty")

# summary
info = run_db.summary(c, rid)
check(info["candidates"] == 3 and info["validated"] == 2, "summary counts")
check(info["bytes"] == 4096 + 2048 + 4096, "summary bytes")
check(info["unvalidated"] == 1, "summary unvalidated")
check(any(e["ext"] == ".jpg" and e["files"] == 2 for e in info["by_extension"]),
      "summary by_extension groups .jpg x2")

# fragments / regions / signatures / destination / plan rows
fid = run_db.add_fragment(c, id_pdf, 0, 8192, 1024)
check(fid > 0, "add_fragment head row")
for kind in ("data", "zero", "compressed"):
    run_db.add_region(c, rid, start=0, length=65536, zeros=10, entropy=1.5, kind=kind)
try:
    run_db.add_region(c, rid, start=0, length=1, kind="bogus")
    check(False, "bad region kind must raise")
except ValueError:
    check(True, "bad region kind raises ValueError")
n = run_db.add_signature_counts(c, rid, {"md ext": 3, "zip": {"hex": "504b", "ext": ".zip", "hits": 2}})
check(n == 2, "add_signature_counts dict + tuple shapes")
try:
    run_db.add_signature_counts(c, rid, [1, 2])
    check(False, "non-dict signature counts must raise")
except ValueError:
    check(True, "non-dict signature counts raises ValueError")
d = run_db.add_destination(c, rid, "SYN", 50 * 1024 ** 3)
check(d > 0, "add_destination row")
p = run_db.add_plan(c, rid, run_db.RULE_EVERYTHING, 1234)
check(p > 0, "add_plan row")

# update_run + unknown field
nch = run_db.update_run(c, rid, status="done", resume_offset=512)
check(nch >= 1, "update_run touches status/resume_offset")
try:
    run_db.update_run(c, rid, bogus=1)
    check(False, "update_run unknown field must raise")
except ValueError:
    check(True, "update_run unknown field raises ValueError")

# dedupe: h1 group keeps priority-first (id_jpg), wastes one copy
groups = run_db.dedupe_by_sha256(c, rid)
check(len(groups) == 1 and groups[0]["sha256"] == "h1", "dedupe finds h1 group")
check(groups[0]["keep_id"] == id_jpg and groups[0]["bytes_wasted"] == 4096,
      "dedupe keeps priority-first, wastes copy (got %s)" % groups[0])

# idempotent file reopen: same tables, same rows, FK on, columns present
tmpd = tempfile.mkdtemp(prefix="v6bundle_")
dbp = os.path.join(tmpd, "run.db")
c2 = run_db.open_run_db(dbp)
r2 = run_db.new_run(c2, source="reopen.img", mode="extract-as-we-go")  # alias ok
run_db.add_candidate(c2, r2, offset=8, length=16, ext="txt")
c2.close()
c3 = run_db.open_run_db(dbp)  # reopen must not fail / duplicate
check(len(run_db.ranked(c3, r2)) == 1, "file DB survives reopen (idempotent schema)")
fk = c3.execute("PRAGMA foreign_keys").fetchone()[0]
check(fk == 1, "foreign_keys ON")
cols = [i[1] for i in c3.execute("PRAGMA table_info(run)").fetchall()]
check("mode" in cols and "cap_policy" in cols, "_ensure_columns present (mode/cap_policy)")
c3.close()

# SQL-injection safety: hostile label cannot alter schema/rows
cm = run_db.open_run_db(":memory:")
rm = run_db.new_run(cm, source="x'); DROP TABLE candidate; --")
run_db.add_candidate(cm, rm, offset=0, length=10, label="a'b\"\\|%_",
                     ext=".txt", name_guess="B;B", carved_path="/p/x")
got = list(run_db.ranked(cm, rm, name_like="b;b"))
check(len(got) == 0 or True, "name_like runs without error")
check(len(run_db.ranked(cm, rm)) == 1, "hostile label stored, table intact")

# ------------------------------------------------------------- step 3: modes
print("== step 3: extraction modes ==")
check(modes.extract_mode("extract-while-scanning") == modes.MODE_EXTRACT, "modes: canonical extract")
check(modes.extract_mode("report-first") == modes.MODE_REPORT, "modes: canonical report")
check(modes.extract_mode("extract-as-we-go") == modes.MODE_EXTRACT, "modes: alias extract-as-we-go")
check(modes.extract_mode("extract") == modes.MODE_EXTRACT, "modes: alias extract")
check(modes.extract_mode("carve") == modes.MODE_EXTRACT, "modes: alias carve")
check(modes.extract_mode("report") == modes.MODE_REPORT, "modes: alias report")
check(modes.extract_mode("  REPORT-FIRST ") == modes.MODE_REPORT, "modes: case/space tolerant")
check(modes.extract_mode("unknown-thing") == "unknown-thing", "modes: unknown passes through")
check(modes.extract_mode(None) == "", "modes: None -> empty (edge)")
check(run_db._mode("extract-as-we-go") == run_db.MODE_EXTRACT, "run_db._mode alias matches modes.py")

# ------------------------------------------------------- steps 4/5/12: UI side
print("== steps 4/5/12: plan screen, reports, audit ==")
cp = run_db.open_run_db(":memory:")
rp = run_db.new_run(cp, source="plan.img", source_size=5 * 1024 * 1024,
                    mode=run_db.MODE_REPORT)
run_db.add_candidate(cp, rp, offset=512, length=1024, ext=".jpg",
                     validated=1, confidence=0.88)
run_db.add_candidate(cp, rp, offset=2048, length=512, ext=".pdf",
                     validated=0, confidence=0.40)

text, res = plan_screen.render_plan_screen(cp, rp)
check("RECOVERY PLAN" in text, "plan screen header")
check("2 found, 1 validated" in text, "plan screen counts line")
check("planned bytes" in text and "fits" in text, "plan screen planned/fits lines")
check("[1] everything" in text and "[4] my own filter" in text, "plan screen 4 options")
check(sorted(res.keys()) == sorted(["run_id", "rule", "selected_bytes", "selected_files",
      "planned_bytes", "planned_files", "fits", "by_extension", "destinations",
      "destinations_used", "skipped_bytes", "skipped_files", "file_limit",
      "destinations_used_bytes"]), "plan_extraction dict keys stable")
# no-destination default: nothing fits yet (matches shipped demo)
check(res["planned_files"] == 0 and res["fits"] is False, "plan w/o destinations plans 0")
# with a roomy destination: everything fits
big = run_db.plan_extraction(cp, rp, run_db.RULE_EVERYTHING,
                             free_bytes_by_destination={"D:/out": 50 * 1024 ** 3})
check(big["fits"] is True and big["planned_files"] == 2, "plan w/ roomy dest fits all")
check(big["planned_bytes"] == 1024 + 512, "plan bytes exact, not estimated")
only_v = run_db.plan_extraction(cp, rp, run_db.RULE_VALIDATED,
                                free_bytes_by_destination={"D:/out": 50 * 1024 ** 3})
check(only_v["planned_files"] == 1 and only_v["planned_bytes"] == 1024, "validated-only rule")
only_d = run_db.plan_extraction(cp, rp, run_db.RULE_DOCUMENTS,
                                free_bytes_by_destination={"D:/out": 50 * 1024 ** 3})
check(only_d["planned_files"] == 1, "documents rule picks the .pdf")
custom = run_db.plan_extraction(cp, rp, {"ext": [".jpg"]},
                                free_bytes_by_destination={"D:/out": 50 * 1024 ** 3})
check(custom["planned_files"] == 1, "dict rule (option 4) passes through to ranked()")
try:
    run_db.plan_extraction(cp, rp, "bogus-rule")
    check(False, "unknown plan rule must raise")
except ValueError:
    check(True, "unknown plan rule raises ValueError")
# too-small destination refuses
small = run_db.plan_extraction(cp, rp, free_bytes_by_destination={"D:/out": 1024})
check(small["planned_files"] == 0 and small["fits"] is False, "tiny dest refuses all")
# empty run edge
ce = run_db.open_run_db(":memory:")
re_ = run_db.new_run(ce, source="empty.img")
te, rese = plan_screen.render_plan_screen(ce, re_)
check("0 found, 0 validated" in te and rese["selected_files"] == 0, "plan screen empty run")

# write_report: md/csv/html, one row per candidate, stable columns
outd = os.path.join(tmpd, "rep")
written = run_db.write_report(cp, rp, outd)
check(set(written.keys()) == {"md", "csv", "html"}, "write_report returns md/csv/html")
for k, path in written.items():
    check(os.path.isfile(path) and os.path.getsize(path) > 0, "report %s non-empty" % k)
with open(written["csv"], encoding="utf-8-sig") as fh:
    rows = list(csv.reader(fh))
check(rows[0] == list(run_db.CSV_COLUMNS), "csv header == CSV_COLUMNS order")
check(len(rows) - 1 == 2, "csv one row per candidate")
with open(written["md"], encoding="utf-8") as fh:
    md = fh.read()
check("recovery report" in md and ".jpg" in md, "report.md content")
with open(written["html"], encoding="utf-8") as fh:
    html = fh.read()
check("<html" in html and ".jpg" in html, "report.html content")
# hostile name escaping: html-escaped, csv still one logical row
run_db.add_candidate(cp, rp, offset=99, length=10, ext=".txt",
                     label="<b>", name_guess="a,b\"c", carved_path="p|x")
written2 = run_db.write_report(cp, rp, outd)
with open(written2["csv"], encoding="utf-8-sig") as fh:
    check(len(list(csv.reader(fh))) - 1 == 3, "csv survives commas/quotes/pipes")
with open(written2["html"], encoding="utf-8") as fh:
    check("&lt;b&gt;" in fh.read(), "html escapes hostile label")

# reports_integration.generate_all (redirect REPORT_DIR into tmp)
reports_mod.REPORT_DIR = os.path.join(tmpd, "rep_all")
gen = reports_mod.generate_all(cp, rp)
check(gen["files_written"] == 4, "generate_all writes 3 reports + audit")
for k in ("report_md", "report_csv", "report_html", "audit_txt"):
    check(os.path.isfile(gen[k]) and os.path.getsize(gen[k]) > 0, "generate_all %s exists" % k)

# audit_serialization.write_audit_txt: stable fields, limit honored
ap = os.path.join(tmpd, "audit.txt")
_, n = audit_mod.write_audit_txt(cp, rp, ap)
check(n == 3, "audit writes one line per candidate")
lines = open(ap, encoding="utf-8").read().splitlines()
check(lines[0].startswith("# audit.txt") and "offset" in lines[1], "audit header fields")
check(all(line.count("|") == 7 for line in lines[2:]), "audit 8 pipe fields per row")
check(any("1024" in line and ".jpg" in line and "yes" in line for line in lines[2:]),
      "audit row carries offset/ext/validated")
_, n1 = audit_mod.write_audit_txt(cp, rp, ap, limit=1)
check(n1 == 1 and len(open(ap, encoding="utf-8").read().splitlines()) == 3,
      "audit limit=1 honored")

# ------------------------------------------------------------- steps 6/7
print("== steps 6/7: validators + assembly ==")
JPG_OK = b"\xff\xd8\xff\xe0" + b"\x00" * 10 + b"\xff\xd9"
PNG_OK = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 8 + b"IEND" + b"\x00" * 4
PDF_OK = b"%PDF-1.4\n1 0 obj\nstartxref\n0\n%%EOF"
# EOCD sig must sit exactly at len-22 (validator's documented simplification:
# zero-length comment). NOTE: the shipped v6/test_v6_steps67.py "zip/eocd"
# fixture appends only 12 trailing bytes, so it does NOT validate — see
# bug note B1 in the completion handoff. This fixture uses the correct 18.
ZIP_OK = b"PK\x03\x04" + b"\x00" * 20 + b"PK\x05\x06" + b"\x00" * 18
SQL_OK = b"SQLite format 3\x00" + b"\x00" * 100
for fmt, blob in [("jpeg", JPG_OK), ("png", PNG_OK), ("pdf", PDF_OK),
                  ("zip", ZIP_OK), ("sqlite", SQL_OK)]:
    r = validators.validate(blob, fmt)
    check(r["validated"] is True and r["confidence"] > 0 and r["error"] == "",
          "validator happy path %s" % fmt)
bad_cases = [
    ("jpeg", b"BAD", "JPEG missing FFD8 SOI"),
    ("jpeg", b"\xff\xd8\x00\x00", "truncated"),
    ("jpeg", b"", "empty data"),
    ("png", b"NOTPNG", "signature"),
    ("png", b"\x89PNG\r\n\x1a\nIHDRonly", "corrupt"),
    ("pdf", b"not pdf", "%PDF"),
    ("pdf", b"%PDF-1.4 no xref here", "startxref"),
    ("zip", b"PK\x03\x04" + b"\x00" * 10, "EOCD"),  # sig not at len-22
    ("zip", b"abc", "too short"),  # len<4 hits the ZIP-too-short branch
    ("sqlite", b"\x00\x00\x00", "SQLite"),
    ("bmp", b"BMxxxx", "unknown format"),
]
for fmt, blob, needle in bad_cases:
    r = validators.validate(blob, fmt)
    check(r["validated"] is False and needle in r["error"],
          "validator edge %s -> %r" % (fmt, needle))
# validator result shape stable
r = validators.validate(JPG_OK, "jpeg")
check(set(r.keys()) == {"validated", "confidence", "validator", "fragments",
                        "gap_bytes", "error"}, "validator dict keys stable")
check(0.0 <= r["confidence"] <= 1.0, "validator confidence in range")

a0 = assembly.assemble([])
check(a0["assembled"] == b"" and "no fragments" in a0["error"], "assembly 0 frags error")
a1 = assembly.assemble([b"ONLY"])
check(a1["assembled"] == b"ONLY" and a1["confidence"] == 0.95
      and a1["fragments"] == 1, "assembly 1 frag passthrough")
a2 = assembly.assemble([b"FRAG1", b"FRAG2"])
check(a2["fragments"] == 2 and a2["gap_bytes"] == 0
      and a2["assembled"] == b"FRAG1FRAG2", "assembly 2 frags minimal gap")
check(0.0 < a2["confidence"] <= 0.85, "assembly 2-frag confidence in range")
a3 = assembly.assemble([b"A", b"B", b"C"])
check(a3["assembled"] == b"ABC" and a3["confidence"] == 0.82, "assembly 3+ sequential")
a4 = assembly.assemble([b"A", b"B", b"C", b"D"])
check(a4["assembled"] == b"ABCD", "assembly 4 frags sequential concat")
ab = assembly.assemble([b"X", b"Y"], gap_search_limit=0)
# degenerate limit: range(0, min(0,256)) is empty, so no gap is tried and
# confidence stays 0.0 while gap_bytes stays 0 (concat with zero gap).
check(ab["assembled"] == b"XY" and ab["gap_bytes"] == 0
      and ab["confidence"] == 0.0,
      "assembly gap limit 0 edge: empty search -> gap 0, conf 0.0")
check(set(a2.keys()) == {"assembled", "fragments", "gap_bytes", "confidence", "error"},
      "assembly dict keys stable")

# ------------------------------------------------- steps 8-11 + Direct I/O
print("== steps 8-11 + Direct I/O: operations ==")
check(operations.select_op(b"") == "zero_skip", "select_op empty -> zero_skip")
check(operations.select_op(b"\xff\xd8\xff\xe0" * 16) == "adaptive_parallel",
      "select_op dense -> adaptive_parallel")
check(operations.select_op(b"\x00" * 100) == "single_worker_skip",
      "select_op all-zero -> single_worker_skip")
check(operations.select_op(b"\x00" * 50 + b"A" * 50) == "single_worker_skip",
      "select_op 50% zero -> single_worker_skip (boundary >= .3)")
check(operations.select_op(b"\x00" * 29 + b"A" * 71) == "adaptive_parallel",
      "select_op 29% zero -> adaptive_parallel (boundary < .3)")
check(operations.select_op(b"\x00" * 30 + b"A" * 70) == "single_worker_skip",
      "select_op 30% zero boundary -> single_worker_skip")
check(operations.select_op(b"\x00" * 29 + b"A" * 71) ==
      operations.select_op(b"\x00" * 29 + b"A" * 71), "select_op deterministic")

check(operations.entropy(b"") == 0.0, "entropy empty == 0.0")
check(operations.entropy(b"A" * 64) == 0.0, "entropy uniform == 0.0")
e2 = operations.entropy(b"AB" * 32)
check(abs(e2 - 1.0) < 1e-9, "entropy 2-symbol == 1.0 (got %r)" % e2)
ee = operations.entropy(bytes(range(256)))
check(abs(ee - 8.0) < 1e-9, "entropy full alphabet == 8.0 (got %r)" % ee)
check(0.0 <= operations.entropy(b"\xff\xd8\xff\xe0hello") <= 8.0, "entropy in 0..8")
check(operations.entropy(b"abc123") == operations.entropy(b"abc123"), "entropy reproducible")

check(operations.parallel_penalty(1, 0.8) == 1.0, "penalty 1 worker == 1.0")
check(operations.parallel_penalty(4, 0.0) == 1.0, "penalty zero-frac == 1.0")
check(operations.parallel_penalty(4, 0.8) > 1.0, "penalty 4-worker/high-zero > 1.0")
check(operations.parallel_penalty(4, 0.9) > operations.parallel_penalty(4, 0.1),
      "penalty grows with zero fraction")
check(operations.parallel_penalty(8, 0.5) > operations.parallel_penalty(4, 0.5),
      "penalty grows with workers")

sample = os.path.join(tmpd, "cal.bin")
with open(sample, "wb") as fh:
    fh.write(b"\xff\xd8\xff\xe0" * 64 + b"\x00" * 32)
cal1 = operations.calibrate(sample, 4)
cal2 = operations.calibrate(sample, 4)
check(cal1 == cal2, "calibrate reproducible")
check(set(cal1.keys()) == {"sample", "bytes", "entropy", "op", "zero_frac",
                           "penalty_factor"}, "calibrate keys stable")
check(cal1["bytes"] == os.path.getsize(sample), "calibrate byte count")
check(cal1["op"] == operations.select_op(open(sample, "rb").read()), "calibrate op agrees")
check(0.0 <= cal1["entropy"] <= 8.0, "calibrate entropy range")
try:
    operations.calibrate(os.path.join(tmpd, "missing.bin"), 4)
    check(False, "calibrate missing file must raise")
except (OSError, IOError):
    check(True, "calibrate missing file raises OSError")

# NOTE: operations.open_direct returns (handle, used_direct, reason).
# With FIXFORMAT_DIRECT_IO unset: (fh, False, "direct_io_not_configured").
fh, used_direct, reason = operations.open_direct(sample, "rb")
data = fh.read()
fh.close()
check(data == open(sample, "rb").read(), "open_direct reads identical bytes")
check(used_direct is False and reason == "direct_io_not_configured",
      "open_direct default: buffered fallback, observable reason")
# opt-in path smoke: FIXFORMAT_DIRECT_IO=1 re-read does not change constants,
# alignment helper rejects non-multiples of 512
check(operations._aligned_buffer(b"A" * 512) == b"A" * 512, "aligned buffer 512 ok")
check(operations._aligned_buffer(b"A" * 100) is None, "unaligned buffer -> None")
check(operations._aligned_buffer(b"") == b"", "empty buffer edge")
# deterministic penalty attribution (sibling worker's new API)
pa1 = operations.account_penalty("opA", 4, 0.5)
pa2 = operations.account_penalty("opA", 4, 0.5)
check(pa1 == pa2, "account_penalty deterministic")
check(pa1["penalty_factor"] == round(operations.parallel_penalty(4, 0.5), 6),
      "account_penalty factor agrees with parallel_penalty")
ag1 = operations.aggregate_penalties([pa1, pa2])
ag2 = operations.aggregate_penalties([pa2, pa1])
check(ag1 == ag2 and ag1["count"] == 2, "aggregate_penalties order-independent")
check(ag1["penalty_total"] == round(pa1["penalty_factor"] * 2, 6),
      "aggregate_penalties total == sum of factors")
ae = operations.aggregate_penalties([])
check(ae == {"penalty_total": 0.0, "count": 0, "attribution": ""},
      "aggregate_penalties empty edge")
try:
    operations.open_direct(os.path.join(tmpd, "missing.bin"), "rb")
    check(False, "open_direct missing file must raise")
except (OSError, IOError):
    check(True, "open_direct missing file raises (no silent fallback)")

# ------------------------------------------------------- integration + disjoint
print("== integration + disjointness ==")
ci = run_db.open_run_db(":memory:")
ri = run_db.new_run(ci, source="e2e.img", source_size=1024 * 1024,
                    mode=run_db.MODE_EXTRACT)
frags = [JPG_OK[:8], JPG_OK[8:]]
vr = validators.validate(b"".join(frags), "jpeg")
check(vr["validated"], "e2e: reassembled jpeg validates")
ar = assembly.assemble(frags)
run_db.add_candidate(ci, ri, offset=0, length=len(ar["assembled"]), ext=".jpg",
                     validated=int(vr["validated"]), confidence=vr["confidence"],
                     fragments=ar["fragments"], gap_bytes=ar["gap_bytes"],
                     sha256="e2e1")
run_db.add_candidate(ci, ri, offset=999, length=100, ext=".pdf",
                     validated=0, confidence=0.2, sha256="e2e2")
pi = run_db.plan_extraction(ci, ri, free_bytes_by_destination={"D:/o": 100 * 1024 ** 3})
check(pi["planned_files"] == 2 and pi["fits"], "e2e: plan fits both")
outi = os.path.join(tmpd, "e2e")
w = run_db.write_report(ci, ri, outi)
_, ni = audit_mod.write_audit_txt(ci, ri, os.path.join(outi, "audit.txt"))
check(all(os.path.getsize(w[k]) > 0 for k in ("md", "csv", "html")) and ni == 2,
      "e2e: reports + audit written")

for label, path in [("validators", os.path.join(FIX, "validators.py")),
                    ("assembly", os.path.join(FIX, "assembly.py")),
                    ("operations", os.path.join(FIX, "performance", "operations.py"))]:
    src = open(path, encoding="utf-8").read()
    for banned in ("run_db", "raw_scan", "volume_gate", "plan_screen",
                   "write_report", "report", "modes"):
        check("import %s" % banned not in src and "from %s" % banned not in src,
              "disjoint: %s imports no %s" % (label, banned))

print("----------------------------------------------------------------------")
if FAILURES:
    print("FAILED %d of %d:" % (len(FAILURES), PASSED[0] + len(FAILURES)))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("PASSED: all %d assertions (steps 2-12 bundle)." % PASSED[0])
