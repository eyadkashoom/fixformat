#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regress_step7.py - independent regression tests for three fixes to raw_scan.py.

Review findings covered (v5/REVIEW_v5_steps0-5.md):

  T1  S4   Ctrl+C raised inside the parallel phase-1 pool must not escape
           scan(): it returns a summary with interrupted=True.
  T2  S4   Ctrl+C during phase-2 carving must not escape scan(), and the hits
           already found must be kept.
  T3  S6   provenance round-trips the pattern BYTES (label, bytes, ext, ci),
           not just the labels.
  T4  S6   a renamed label cannot swap the pattern (stored bytes win), and a
           provenance whose signatures are all unresolvable produces warnings
           instead of an uncaught ValueError.
  T5  S11  the CARVE_LIMIT budget spans progressive slices, and the combined
           summary's carved_files matches the files on disk.
  T6  S11  scan(carved_counts=...) honours a budget carried in from outside.

Every test builds its own temp dir and prints one PASS/FAIL line. The process
exits 0 only when all six pass. This file never modifies raw_scan.py.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import raw_scan  # noqa: E402
except BaseException:                      # a SyntaxError here is a real result
    raw_scan = None
    IMPORT_ERROR = traceback.format_exc()
else:
    IMPORT_ERROR = ""

CHUNK = 4096


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def case_dir(name):
    return tempfile.mkdtemp(prefix="regress_step7_%s_" % name)


def make_image(path, total, marks, filler=b"A"):
    """`total` bytes of filler with `marks` = [(offset, needle), ...] written in."""
    data = bytearray(filler * total)
    for offset, needle in marks:
        if offset + len(needle) > total:
            raise ValueError("mark at %d does not fit in %d bytes" % (offset, total))
        data[offset:offset + len(needle)] = needle
    with open(path, "wb") as fh:
        fh.write(bytes(data))
    return total


def pattern_marks(offsets, needle=b".md"):
    return [(int(off), needle) for off in offsets]


def safe_label(label):
    """The file-name form carve_window() gives a label."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in label)


def primary_carved_files(roots, label):
    """Carved windows (not .triage.txt / .readable.txt sidecars) for `label`."""
    rx = re.compile(r"^%s_0x[0-9A-Fa-f]{16}\.[A-Za-z0-9]+$" % re.escape(safe_label(label)))
    found = []
    for root in roots:
        for cur, _dirs, files in os.walk(root):
            for name in files:
                if rx.match(name):
                    found.append(os.path.join(cur, name))
    return found


def build_prov(patterns, case_sensitive=False, target="fixture.img", total=4096):
    summary = {"hits": [], "counts": {}, "bytes_scanned": 0, "carved_files": 0,
               "interrupted": False, "elapsed": 0.0}
    return raw_scan.provenance_payload(target, total, summary, patterns,
                                       case_sensitive, "2026-10-29 09:00:00")


def stored_pattern_list(prov):
    """The bytes-carrying list a provenance stores (key name tolerated)."""
    for key in ("signature_bytes", "patterns"):
        value = prov.get(key)
        if isinstance(value, list) and value:
            return value
    return None


def same_pattern_rows(left, right):
    """True when two (label, bytes, ext, ci) lists are identical, bytes included."""
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if len(a) != 4 or len(b) != 4:
            return False
        if str(a[0]) != str(b[0]) or a[2] != b[2] or bool(a[3]) != bool(b[3]):
            return False
        if not isinstance(a[1], (bytes, bytearray)) or bits(a[1]) != bits(b[1]):
            return False
    return True


def bits(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


# ----------------------------------------------------------------------
# T1 - Ctrl+C inside the parallel pool
# ----------------------------------------------------------------------

def t1_ctrl_c_in_parallel_pool(tmpdir):
    failures = []
    img = os.path.join(tmpdir, "slice.img")
    results = os.path.join(tmpdir, "search_results.txt")
    make_image(img, 64 * 1024, pattern_marks([1024, 40000]))
    patterns = [("md ext [utf-8]", b".md", "md", False)]

    saved_workers = raw_scan.PARALLEL_WORKERS
    saved_min = raw_scan.PARALLEL_MIN_SIZE
    saved_phase1 = raw_scan.parallel_phase1
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise KeyboardInterrupt("simulated Ctrl+C inside the worker pool")

    try:
        raw_scan.PARALLEL_WORKERS = 2
        raw_scan.PARALLEL_MIN_SIZE = 0
        raw_scan.parallel_phase1 = boom
        try:
            summary = raw_scan.scan(img, patterns, CHUNK, results,
                                    carve_dir=None, show_progress=False,
                                    min_gap=0)
        except BaseException as exc:
            failures.append("scan() let %s escape instead of returning a summary: %s"
                            % (type(exc).__name__, exc))
            summary = None
    finally:
        raw_scan.PARALLEL_WORKERS = saved_workers
        raw_scan.PARALLEL_MIN_SIZE = saved_min
        raw_scan.parallel_phase1 = saved_phase1

    if not calls:
        failures.append("the parallel branch was never entered (monkeypatch unused): "
                        "PARALLEL_WORKERS/PARALLEL_MIN_SIZE did not force it")
    if summary is not None:
        if not isinstance(summary, dict):
            failures.append("scan() returned %r, not a summary dict" % (summary,))
        else:
            if not summary.get("interrupted"):
                failures.append("summary['interrupted'] is %r, expected True"
                                % (summary.get("interrupted"),))
            for key in ("hits", "counts", "bytes_scanned", "results_path"):
                if key not in summary:
                    failures.append("summary is missing the %r field" % key)
    if not os.path.isfile(results):
        failures.append("the results log %s does not exist after the interrupt"
                        % results)
    return failures


# ----------------------------------------------------------------------
# T2 - Ctrl+C during phase 2 carving
# ----------------------------------------------------------------------

def t2_ctrl_c_during_carving(tmpdir):
    failures = []
    img = os.path.join(tmpdir, "carve.img")
    results = os.path.join(tmpdir, "search_results.txt")
    carve_dir = os.path.join(tmpdir, "carved")
    offsets = [512, 9000, 20000]
    make_image(img, 32 * 1024, pattern_marks(offsets))
    patterns = [("md ext [utf-8]", b".md", "md", False)]

    saved_carve = raw_scan.carve_phase
    seen = {"hits": 0}

    def fake_carve(f, hits, total_size, carve_dir_arg, before, after, carve_limit,
                   carved_counts, log, show_progress=True):
        # write one partial file, exactly like a real interrupted carve would
        os.makedirs(carve_dir_arg, exist_ok=True)
        seen["hits"] = len(hits)
        with open(os.path.join(carve_dir_arg, "partial_dummy.md"), "wb") as fh:
            fh.write(b"half-written window")
        raise KeyboardInterrupt("simulated Ctrl+C during phase 2")

    try:
        raw_scan.carve_phase = fake_carve
        try:
            summary = raw_scan.scan(img, patterns, CHUNK, results, carve_dir,
                                    carve_before=64, carve_after=64,
                                    show_progress=False, min_gap=0)
        except BaseException as exc:
            failures.append("scan() let %s escape instead of returning a summary: %s"
                            % (type(exc).__name__, exc))
            summary = None
    finally:
        raw_scan.carve_phase = saved_carve

    if seen["hits"] == 0:
        failures.append("phase 2 was never reached (no hits handed to carve_phase)")
    if summary is not None:
        if not isinstance(summary, dict):
            failures.append("scan() returned %r, not a summary dict" % (summary,))
        else:
            if not summary.get("interrupted"):
                failures.append("summary['interrupted'] is %r, expected True"
                                % (summary.get("interrupted"),))
            hits = summary.get("hits") or []
            got = sorted(int(h.get("offset", -1)) for h in hits)
            if got != sorted(offsets):
                failures.append("the hit list was not preserved: got offsets %s, "
                                "expected %s" % (got, sorted(offsets)))
            if (summary.get("counts") or {}).get("md ext [utf-8]") != len(offsets):
                failures.append("summary['counts'] lost the hits: %r"
                                % (summary.get("counts"),))
            if not os.path.isfile(summary.get("results_path") or ""):
                failures.append("the results log is gone after the interrupt")
            if not os.path.isfile(os.path.join(carve_dir, "partial_dummy.md")):
                failures.append("the partial carve file disappeared")
    return failures


# ----------------------------------------------------------------------
# T3 - provenance round-trip of the pattern BYTES
# ----------------------------------------------------------------------

PATTERNS_T3 = [("Profile .dwg AC10 [utf-8]", b"AC10", "dwg", False),
               ("PDF", b"%PDF-", "pdf", False)]


def t3_provenance_round_trips_bytes(tmpdir):
    failures = []
    prov = build_prov(PATTERNS_T3)
    path = raw_scan.write_provenance(tmpdir, prov)
    if not path or not os.path.isfile(path):
        return ["write_provenance() wrote nothing into %s" % tmpdir]

    back = raw_scan.read_provenance(tmpdir)
    if not back:
        return ["read_provenance() returned nothing for %s" % tmpdir]

    try:
        patterns, warnings = raw_scan.patterns_from_provenance(back)
    except BaseException as exc:
        return ["patterns_from_provenance() raised %s: %s"
                % (type(exc).__name__, exc)]

    if not same_pattern_rows(patterns, PATTERNS_T3):
        failures.append("round-trip changed the signatures: expected %r, got %r"
                        % ([(p[0], p[1], p[2], p[3]) for p in PATTERNS_T3],
                           [(p[0], p[1], p[2], p[3]) for p in (patterns or [])]))
    if warnings:
        failures.append("a clean round-trip raised warnings: %r" % (warnings,))
    # the bytes themselves must have survived, not been re-derived from labels
    by_label = {}
    for entry in (patterns or []):
        by_label[entry[0]] = bits(entry[1])
    if by_label.get("Profile .dwg AC10 [utf-8]") != b"AC10":
        failures.append("the .dwg pattern bytes did not survive: %r"
                        % (by_label.get("Profile .dwg AC10 [utf-8]"),))
    if by_label.get("PDF") != b"%PDF-":
        failures.append("the PDF pattern bytes did not survive: %r"
                        % (by_label.get("PDF"),))
    return failures


# ----------------------------------------------------------------------
# T4 - a rename cannot swap the pattern; unresolvable provenance warns
# ----------------------------------------------------------------------

def t4_renamed_label_keeps_stored_bytes(tmpdir):
    failures = []
    renamed_label = "Renamed .dwg signature [utf-8]"

    # --- part 1: rename the label inside the stored form ------------------
    prov = raw_scan.read_provenance(tmpdir) if os.path.isfile(
        os.path.join(tmpdir, raw_scan.PROVENANCE_FILE)) else None
    if not prov:
        raw_scan.write_provenance(tmpdir, build_prov(PATTERNS_T3))
        prov = raw_scan.read_provenance(tmpdir)
    if not prov:
        return ["could not stage a provenance file in %s" % tmpdir]

    stored = stored_pattern_list(prov)
    if stored is None:
        return ["the provenance stores no signature bytes at all "
                "(no 'signature_bytes'/'patterns' key): a resume can only "
                "rebuild from labels"]

    mutated = []
    renamed = 0
    target_hex = b"AC10".hex().lower()
    for entry in stored:
        row = list(entry) if isinstance(entry, (list, tuple)) else None
        if row is None or len(row) < 2:
            mutated.append(entry)
            continue
        if str(row[1]).strip().lower() == target_hex:
            row[0] = renamed_label
            renamed += 1
        mutated.append(row)
    if renamed != 1:
        return ["could not find the stored AC10 entry to rename (hex %r) in %r"
                % (target_hex, stored)]
    prov = dict(prov)
    for key in ("signature_bytes", "patterns"):
        if key in prov:
            prov[key] = mutated

    try:
        patterns, warnings = raw_scan.patterns_from_provenance(prov)
    except BaseException as exc:
        failures.append("a renamed label made patterns_from_provenance() raise "
                        "%s: %s" % (type(exc).__name__, exc))
        patterns, warnings = None, []

    if patterns is not None:
        matches = [p for p in patterns if bits(p[1]) == b"AC10"]
        if len(matches) != 1:
            failures.append("after the label rename the stored AC10 bytes are "
                            "gone: %r" % ([(p[0], p[1], p[2]) for p in patterns],))
        else:
            if matches[0][2] != "dwg":
                failures.append("the renamed entry lost its extension: %r"
                                % (matches[0][2],))
            if str(matches[0][0]) != renamed_label:
                failures.append("the returned label is %r, expected the stored %r"
                                % (matches[0][0], renamed_label))
        if [w for w in warnings if renamed_label in w]:
            failures.append("a warning was raised for a resolvable entry: %r"
                            % (warnings,))
        if warnings:
            failures.append("renaming a label produced warnings: %r" % (warnings,))

    # --- part 2: nothing resolves -> warnings, empty patterns, no ValueError
    bad = {"signature_bytes": [["Ghost signature", "zz-not-hex", "dwg", False],
                               ["Ghost signature 2", "", "pdf", False]],
           "signature_labels": ["Ghost signature", "Ghost signature 2"],
           "case_sensitive": False}
    try:
        bad_patterns, bad_warnings = raw_scan.patterns_from_provenance(bad)
    except BaseException as exc:
        failures.append("an unresolvable provenance raised %s: %s (expected "
                        "warnings + an empty pattern list)"
                        % (type(exc).__name__, exc))
    else:
        if not bad_patterns:
            pass
        else:
            failures.append("unresolvable signatures still produced patterns: %r"
                            % (bad_patterns,))
        if not bad_warnings:
            failures.append("no warning was produced for unresolvable signatures")
    return failures


# ----------------------------------------------------------------------
# T5 - the carve budget spans slices
# ----------------------------------------------------------------------

def t5_carve_budget_spans_slices(tmpdir):
    failures = []
    label = "md ext [utf-8]"
    patterns = [(label, b".md", "md", False)]
    total = 9 * 1024 * 1024
    hit_offsets = [512 * 1024, 2 * 1024 * 1024, 3 * 1024 * 1024 + 512 * 1024,
                   5 * 1024 * 1024, 6 * 1024 * 1024 + 512 * 1024, 8 * 1024 * 1024]
    img = os.path.join(tmpdir, "spread.img")
    make_image(img, total, pattern_marks(hit_offsets))
    recovered = os.path.join(tmpdir, "recovered_step7")
    organized = os.path.join(tmpdir, "organized_step7")
    os.makedirs(recovered, exist_ok=True)
    os.makedirs(organized, exist_ok=True)

    saved_limit = raw_scan.CARVE_LIMIT
    saved_ask = raw_scan._ask
    try:
        raw_scan.CARVE_LIMIT = 2
        raw_scan._ask = lambda prompt: ("", True)    # auto-continue the slices
        combined = raw_scan.run_progressive(img, total, patterns, False,
                                           recovered, organized,
                                           "2026-10-29 09:00:00",
                                           resume_from=0, tag="step7",
                                           percent=34)
    except BaseException as exc:
        failures.append("run_progressive() raised %s: %s"
                        % (type(exc).__name__, exc))
        combined = None
    finally:
        raw_scan.CARVE_LIMIT = saved_limit
        raw_scan._ask = saved_ask

    carved = primary_carved_files([recovered, organized], label)
    if len(carved) > 2:
        failures.append("CARVE_LIMIT=2 was multiplied by the slices: %d carved "
                        "files on disk (%s)"
                        % (len(carved), ", ".join(os.path.basename(p) for p in carved[:8])))
    if not carved:
        failures.append("no carved file at all - the fixture never reached phase 2")
    if combined is not None:
        if not combined.get("hits"):
            failures.append("the progressive run reported no hits")
        reported = int(combined.get("carved_files") or 0)
        if reported != len(carved):
            failures.append("summary['carved_files'] = %d but %d carved files are "
                            "on disk" % (reported, len(carved)))
    return failures


# ----------------------------------------------------------------------
# T6 - scan(carved_counts=...) is honoured
# ----------------------------------------------------------------------

def t6_scan_honours_carved_counts(tmpdir):
    failures = []
    label = "md ext [utf-8]"
    patterns = [(label, b".md", "md", False)]
    offsets = [512, 6000, 12000]
    img = os.path.join(tmpdir, "budget.img")
    results = os.path.join(tmpdir, "search_results.txt")
    carve_dir = os.path.join(tmpdir, "carved")
    make_image(img, 32 * 1024, pattern_marks(offsets))

    limit = raw_scan.CARVE_LIMIT
    if not limit:
        return ["raw_scan.CARVE_LIMIT is %r - the budget cannot be tested" % (limit,)]

    counts = {label: limit}
    try:
        summary = raw_scan.scan(img, patterns, CHUNK, results, carve_dir,
                                carve_before=64, carve_after=64,
                                show_progress=False, min_gap=0,
                                carve_limit=limit, carved_counts=counts)
    except BaseException as exc:
        return ["scan(carved_counts=...) raised %s: %s"
                % (type(exc).__name__, exc)]

    if len(summary.get("hits") or []) != len(offsets):
        failures.append("the hits were lost when the budget was already spent: "
                        "%d of %d" % (len(summary.get("hits") or []), len(offsets)))
    if int(summary.get("carved_files") or 0) != 0:
        failures.append("scan carved %r new files although the budget was spent"
                        % (summary.get("carved_files"),))
    fresh = primary_carved_files([carve_dir], label)
    if fresh:
        failures.append("scan wrote %d carved file(s) for a spent budget: %s"
                        % (len(fresh), ", ".join(os.path.basename(p) for p in fresh[:5])))
    if counts.get(label) != limit:
        failures.append("the caller's carved_counts dict was changed: %r"
                        % (counts,))
    if (summary.get("counts") or {}).get(label) != len(offsets):
        failures.append("summary['counts'] does not report the hits: %r"
                        % (summary.get("counts"),))
    return failures


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

TESTS = [
    ("T1_ctrl_c_in_parallel_pool", t1_ctrl_c_in_parallel_pool,
     "Ctrl+C inside the parallel pool returns a summary"),
    ("T2_ctrl_c_during_carving", t2_ctrl_c_during_carving,
     "Ctrl+C during phase 2 carving keeps the hits"),
    ("T3_provenance_round_trips_bytes", t3_provenance_round_trips_bytes,
     "provenance round-trips the pattern bytes"),
    ("T4_renamed_label_keeps_stored_bytes", t4_renamed_label_keeps_stored_bytes,
     "a renamed label cannot swap the pattern"),
    ("T5_carve_budget_spans_slices", t5_carve_budget_spans_slices,
     "CARVE_LIMIT is a budget for the whole progressive run"),
    ("T6_scan_honours_carved_counts", t6_scan_honours_carved_counts,
     "scan(carved_counts=...) honours an outside budget"),
]


def main():
    if raw_scan is None:
        print("FAIL  import raw_scan: %s" % IMPORT_ERROR.strip().splitlines()[-1])
        print()
        print("REGRESS STEP7 FAILED: raw_scan.py could not be imported")
        print(IMPORT_ERROR)
        print("failing: %s" % ", ".join(name for name, _fn, _d in TESTS))
        return 1

    passing, failing, details = [], [], []
    for name, fn, description in TESTS:
        tmpdir = case_dir(name.split("_")[0].lower())
        try:
            problems = fn(tmpdir) or []
        except BaseException as exc:
            problems = ["unexpected %s: %s" % (type(exc).__name__, exc)]
            details.append("%s traceback:\n%s" % (name, traceback.format_exc()))
        if problems:
            failing.append(name)
            print("FAIL  %-38s %s" % (name, description))
            for item in problems:
                print("        %s" % item)
            print("        temp dir kept: %s" % tmpdir)
        else:
            passing.append(name)
            print("PASS  %-38s %s" % (name, description))
            shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    for block in details:
        print(block)
    print("=" * 78)
    print("regress_step7: %d passed, %d failed (of %d)"
          % (len(passing), len(failing), len(TESTS)))
    if failing:
        print("REGRESS STEP7 FAILED:")
        for name in failing:
            print("  - " + name)
        return 1
    print("REGRESS STEP7 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
