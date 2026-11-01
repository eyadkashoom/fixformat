#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py - validates raw_scan.py on a synthetic image file (offline).

Checks:
  1. patterns that straddle chunk boundaries are found exactly once,
  2. text patterns are found in UTF-8 and UTF-16LE,
  3. byte offsets are exact,
  4. the MIN_GAP filter collapses near-duplicate hits,
  5. case-sensitive vs case-insensitive matching works,
  6. carved windows contain the pattern bytes,
  7. candidate file names are extracted from carved data.
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import raw_scan  # noqa: E402

CHUNK = 256
MIN_GAP = 150

ARABIC = "\u0627\u0644\u0645\u0634\u0631\u0648\u0639"  # Arabic word


def build_test_image(path):
    ar8 = ARABIC.encode("utf-8")
    ar16 = ARABIC.encode("utf-16-le")
    md8 = ".md".encode("utf-8")

    data = bytearray()
    expected = []

    # 1) ".md" (UTF-8) straddling the first chunk boundary (offset 256)
    data += b"A" * (CHUNK - 2)                       # 254 bytes
    expected.append(("md ext [utf-8]", len(data)))
    data += md8                                     # 254..257 crosses 256

    # 2) Arabic word stored as UTF-8, then "Agent:" for the case test
    data += b"B" * 60
    expected.append(("Arabic word [utf-8]", len(data)))
    data += ar8
    data += b"B" * 50
    expected.append(("Agent ci [utf-8]", len(data)))
    data += b"Agent:"                               # capital A
    data += b"B" * 4

    # 3) same Arabic word stored as UTF-16LE (also straddles boundary 512)
    expected.append(("Arabic word [utf-16-le]", len(data)))
    data += ar16

    # 4) two ".md" hits 5 bytes apart - only the first is reported (MIN_GAP)
    data += b"D" * 300
    expected.append(("md ext [utf-8]", len(data)))
    data += md8 + b"XY" + md8

    # 5) ".md" straddling the chunk boundary near the end (offset 1024)
    pad = (CHUNK * 4) - 2 - len(data)
    data += b"E" * pad
    expected.append(("md ext [utf-8]", len(data)))
    data += md8

    # 6) a readable file name for the candidate-name extraction
    data += b"F" * 40 + b" " + b"notes_2026-10-02.md" + b" " + b"F" * 40

    with open(path, "wb") as out:
        out.write(bytes(data))

    patterns = [
        ("md ext [utf-8]", md8, "md", False),
        ("md ext [utf-16-le]", ".md".encode("utf-16-le"), "md", False),
        ("Arabic word [utf-8]", ar8, "txt", False),
        ("Arabic word [utf-16-le]", ar16, "txt", False),
        ("Agent ci [utf-8]", b"agent:", "yaml", True),     # case-insensitive
        ("Agent cs [utf-8]", b"agent:", "yaml", False),    # case-sensitive
    ]
    pat_by_label = dict((label, raw) for label, raw, _, _ in patterns)
    return expected, patterns, pat_by_label


def main():
    tmpdir = tempfile.mkdtemp(prefix="rawscan_test_")
    img = os.path.join(tmpdir, "test.img")
    results = os.path.join(tmpdir, "search_results.txt")
    carved = os.path.join(tmpdir, "carved")

    expected, patterns, pat_by_label = build_test_image(img)

    summary = raw_scan.scan(img, patterns, CHUNK, results, carved,
                            carve_before=64, carve_after=64,
                            show_progress=False, min_gap=MIN_GAP)

    failures = []

    found = sorted((r["label"], r["offset"]) for r in summary["hits"])
    if found != sorted(expected):
        failures.append("hit list mismatch:\n  expected %s\n  found    %s"
                        % (sorted(expected), found))

    size = os.path.getsize(img)
    if summary["bytes_scanned"] != size:
        failures.append("bytes_scanned %d != file size %d"
                        % (summary["bytes_scanned"], size))

    # MIN_GAP: the second ".md" 5 bytes after the first must be suppressed
    if summary["counts"].get("md ext [utf-8]", 0) != 3:
        failures.append("md ext [utf-8] count = %d, expected 3 (MIN_GAP filter)"
                        % summary["counts"].get("md ext [utf-8]", 0))

    # UTF-16LE ".md" never occurs in the image and must not be reported
    if summary["counts"].get("md ext [utf-16-le]", 0) != 0:
        failures.append("false positive for md ext [utf-16-le]")

    # case handling: "Agent:" must match the insensitive pattern only
    if summary["counts"].get("Agent ci [utf-8]", 0) != 1:
        failures.append("case-insensitive count = %d, expected 1"
                        % summary["counts"].get("Agent ci [utf-8]", 0))
    if summary["counts"].get("Agent cs [utf-8]", 0) != 0:
        failures.append("case-sensitive pattern matched 'Agent:' unexpectedly")

    # Arabic found in both encodings
    if summary["counts"].get("Arabic word [utf-8]", 0) != 1:
        failures.append("Arabic UTF-8 count = %d, expected 1"
                        % summary["counts"].get("Arabic word [utf-8]", 0))
    if summary["counts"].get("Arabic word [utf-16-le]", 0) != 1:
        failures.append("Arabic UTF-16LE count = %d, expected 1"
                        % summary["counts"].get("Arabic word [utf-16-le]", 0))

    # every carved file must contain its own pattern bytes (case-insensitively
    # for the case-insensitive pattern)
    for record in summary["hits"]:
        path = record.get("carved") or ""
        if not path or not os.path.isfile(path):
            failures.append("missing carved file for %s @ %d" % (record["label"], record["offset"]))
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        needle = pat_by_label[record["label"]]
        if needle not in blob and needle.lower() not in blob.lower():
            failures.append("carved file %s does not contain its pattern" % path)

    # candidate file names must be recovered from the carved data
    names = raw_scan.extract_candidate_names(carved)
    name_set = set(n for n, _ in names)
    if "notes_2026-10-02.md" not in name_set:
        failures.append("candidate name 'notes_2026-10-02.md' not extracted (got %s)"
                        % sorted(name_set)[:10])

    # report generation must work and mention every hit
    report_path = os.path.join(tmpdir, "recovery_report.txt")
    count, total = raw_scan.write_report(summary, names, report_path)
    if count <= 0 or total <= 0:
        failures.append("report stats wrong: count=%d total=%d" % (count, total))
    with open(report_path, "r", encoding="utf-8") as fh:
        report_text = fh.read()
    if "RECOVERY REPORT" not in report_text or "recovered total" not in report_text:
        failures.append("recovery report content incomplete")

    # v2 helpers: extension filtering and case flags
    allp = raw_scan.build_patterns(True)
    md_only = raw_scan.filter_patterns_by_extension(allp, {"md"})
    if not md_only or any(entry[2] != "md" for entry in md_only):
        failures.append("extension filtering broken")
    ci_p = raw_scan.build_patterns(False)
    if not any(entry[3] for entry in ci_p):
        failures.append("case-insensitive build broken")
    if any(entry[3] for entry in allp):
        failures.append("case-sensitive build marked a pattern insensitive")

    # results log must contain every reported hit
    with open(results, "r", encoding="utf-8") as fh:
        log_text = fh.read()
    for label, offset in expected:
        if "offset=0x%016X" % offset not in log_text:
            failures.append("results log is missing offset 0x%016X" % offset)

    print()
    if failures:
        print("SELFTEST FAILED:")
        for item in failures:
            print("  - " + item)
        shutil.rmtree(tmpdir, ignore_errors=True)
        sys.exit(1)

    print("SELFTEST PASS: %d hits at exact offsets (ASCII + Arabic in UTF-8 and UTF-16LE)," % len(summary["hits"]))
    print("boundary-crossing patterns found exactly once, MIN_GAP filter verified,")
    print("case-sensitive/insensitive matching verified, carved files contain their patterns,")
    print("candidate file names extracted (%d names), recovery report written." % len(names))
    shutil.rmtree(tmpdir, ignore_errors=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
