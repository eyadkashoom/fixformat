#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v6 wiring regression (entry-point + the mode-bug that crashed runs).

Run:  python v6/test_v6_wiring.py   (from repo root or from v6/)

Covers the exact spots that were NOT covered by selftest.py and that let the
"run_db.new_run(mode='full') raises ValueError" crash through to a real run:

 1. the real entry point `python v6/raw_scan.py` imports cleanly (the embedded
    python._pth omits the script dir, so sibling imports must self-bootstrap);
 2. the wired call site in run_search_flow uses a valid recovery mode, never
    the scan-strategy strings that new_run rejects;
 3. the DB mode contract is honoured (bad mode raises, canonical mode works);
 4. the volume gate classifies a synthetic plaintext NTFS volume as scannable
    (guards the size-is-None false-negative that once made C: look empty);
 5. the full chain validate -> assemble -> record -> plan -> report + audit
    round-trips on a real in-memory DB.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# parent (repo root) so `extraction.modes` resolves when run from repo root
_PARENT = os.path.dirname(HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import raw_scan  # noqa: E402  -- this is the regression under test
import run_db  # noqa: E402
import volume_gate  # noqa: E402
import validators  # noqa: E402
import assembly  # noqa: E402
from extraction import modes  # noqa: E402
import audit_serialization  # noqa: E402

PASS = 0
FAIL = []


def ck(cond, label):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(label)
        print("FAIL " + label)


# 1. entry-point import
ck(raw_scan.VERSION == "6.0", "raw_scan.VERSION == 6.0")
ck(raw_scan.IS_WINDOWS is not None, "raw_scan constants parsed")
ck(hasattr(raw_scan, "run_search_flow"), "run_search_flow present")
ck(hasattr(volume_gate, "probe_source"), "gate self-bootstrap: probe_source importable")
ck(hasattr(run_db, "new_run") and hasattr(run_db, "plan_extraction"),
   "db self-bootstrap: new_run / plan_extraction importable")

# 2. the live call site in run_search_flow must use a valid recovery mode
_src = open(os.path.join(HERE, "raw_scan.py"), encoding="utf-8").read()
_call = _src.split("run_db.new_run(", 1)[1][:500]
ck(('mode="extract-while-scanning"' in _call)
   or ("mode=run_db.MODE_EXTRACT" in _call),
   "run_search_flow wires new_run with a valid recovery mode (not 'full'/'progressive')")

# 3. DB mode contract: bad mode raises, canonical works
c = run_db.open_run_db(":memory:")
try:
    run_db.new_run(c, mode="full")
    ck(False, "mode 'full' must raise ValueError")
except ValueError:
    ck(True, "mode 'full' raises ValueError (recovery mode != scan strategy)")
try:
    run_db.new_run(c, mode="progressive")
    ck(False, "mode 'progressive' must raise ValueError")
except ValueError:
    ck(True, "mode 'progressive' raises ValueError")
rid = run_db.new_run(c, tool_version=raw_scan.VERSION, source="wiring.img",
                     source_size=1, mode="extract-while-scanning")
ck(isinstance(rid, int) and rid > 0, "canonical mode 'extract-while-scanning' opens a run")

# 4. volume gate on a synthetic plaintext NTFS volume
img = os.path.join(tempfile.mkdtemp(prefix="wgate_"), "ntfs.img")
with open(img, "wb") as fh:
    fh.write(b"\xEB\x52\x90" + b"NTFS    " + b"\x00" * (512 - 11))   # 3-byte jump + 8-byte OEM at offset 3
    fh.write(b"DATA" * 64)                    # first non-zero payload
    fh.write(b"\x00" * (2 * 1024 * 1024))    # tail of zeros
probe = volume_gate.probe_source(img)
ck(probe["verdict"] == "plaintext", "synthetic NTFS disk -> plaintext")
ck(len(probe["scannable"]) == 1, "synthetic NTFS -> 1 scannable partition")
ck(probe["scannable"][0]["kind"] == "ntfs", "synthetic NTFS -> kind ntfs")
ck(probe["scannable"][0]["bytes"] is not None and probe["scannable"][0]["bytes"] > 0,
   "synthetic NTFS -> size known (not collapsed to 0)")
ck(probe["blocked"] == [], "synthetic NTFS -> nothing blocked")

# 4b. the regression once hit by \.\C: : a file that refuses seek-to-end
#     must NOT be called 'empty' -- it stays scannable.
real = volume_gate._Source._probe_size if hasattr(volume_gate._Source, "_probe_size") else None
if real:
    _orig = volume_gate._Source._probe_size
    try:
        volume_gate._Source._probe_size = lambda self: None   # device that says nothing
        dev = volume_gate.probe_source(img)
        ck(dev["verdict"] == "plaintext" and len(dev["scannable"]) == 1,
           "unknown-size NTFS stays plaintext+scannable (was 'empty' before fix)")
    finally:
        volume_gate._Source._probe_size = _orig

# 5. integration chain: validate -> assemble -> record -> plan -> report + audit
jpg = (b"\xff\xd8\xff\xe0JFIF\x00\x00\x10" + b"\x00" * 100 + b"\xff\xd9")
vr = validators.validate(jpg, "jpeg")
ck(vr["validated"], "jpeg validator passes a real SOI/EOI jpeg")
ar = assembly.assemble([jpg])
ck(ar["assembled"] == jpg and ar["fragments"] == 1, "1-fragment assemble passthrough")
cid = run_db.add_candidate(c, rid, offset=0, length=len(jpg), ext=".jpg",
                           validated=1, confidence=1.0, fragments=1, sha256="w1")
ck(cid > 0, "add_candidate returns id")
plan = run_db.plan_extraction(c, rid, free_bytes_by_destination={"/o": 10 * 1024 ** 3})
ck(plan.get("fits") and plan["planned_files"] >= 1, "plan_extraction fits the jpeg")
out = os.path.join(tempfile.mkdtemp(prefix="wrepo_"), "rep")
w = run_db.write_report(c, rid, out)
ck(all(os.path.getsize(w[k]) > 0 for k in ("md", "csv", "html")), "reports non-empty")
_, audited = audit_serialization.write_audit_txt(c, rid, os.path.join(out, "audit.txt"))
ck(audited >= 1, "audit.txt wrote at least one row")

print("-" * 60)
print("PASSED %d  FAILED %d" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
