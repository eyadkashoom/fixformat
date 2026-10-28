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

import hashlib
import inspect
import os
import shutil
import sys
import tempfile
import time

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

    # v3: disk imaging round-trip (source -> image -> byte-identical + sha256)
    src_disk = os.path.join(tmpdir, "disk.bin")
    payload = (b"SECTORDATA" * 700) + b"PROJECT_SPEC" + (b"Z" * 4096)
    with open(src_disk, "wb") as fh:
        fh.write(payload)
    img_out = os.path.join(tmpdir, "image.img")
    im = raw_scan.create_disk_image(src_disk, img_out, 1024, show_progress=False)
    with open(img_out, "rb") as fh:
        back = fh.read()
    if back != payload:
        failures.append("imaging round-trip mismatch (%d vs %d bytes)" % (len(back), len(payload)))
    if im["sha256"] != hashlib.sha256(payload).hexdigest():
        failures.append("imaging sha256 mismatch")
    if not im["verified"]:
        failures.append("imaging verification flag failed")
    if im["copied"] != len(payload):
        failures.append("imaging copied count wrong: %d" % im["copied"])
    if not os.path.isfile(img_out + ".info.txt"):
        failures.append("image .info.txt sidecar missing")

    # v5 sections 1-2: provenance-stamped run folders and organized output
    run_base = os.path.join(tmpdir, "runs")
    os.makedirs(run_base)
    recovered_dir, organized_dir, tag = raw_scan.make_run_dirs(
        run_base, os.path.join(tmpdir, "disk9.img"), 5 * 1024 ** 3)
    if not tag.startswith("disk9_5.0GB_"):
        failures.append("run tag = %r" % tag)
    if os.path.basename(recovered_dir) != "recovered_" + tag:
        failures.append("recovered folder = %r" % os.path.basename(recovered_dir))
    if os.path.basename(organized_dir) != "organized_" + tag:
        failures.append("organized folder = %r" % os.path.basename(organized_dir))

    prov = raw_scan.provenance_payload("/dev/disk9.img", 5 * 1024 ** 3, summary,
                                       patterns, True, "2026-10-28 12:00:00")
    raw_scan.write_provenance(recovered_dir, prov)
    back = raw_scan.read_provenance(recovered_dir)
    if back.get("source") != "/dev/disk9.img":
        failures.append("provenance source = %r" % (back.get("source"),))
    if back.get("hits") != len(summary["hits"]):
        failures.append("provenance hits = %r, expected %d"
                        % (back.get("hits"), len(summary["hits"])))
    if back.get("signatures") != len(patterns):
        failures.append("provenance signatures = %r" % (back.get("signatures"),))

    moved, idx_path, summ_path = raw_scan.organize_output(summary, recovered_dir,
                                                          organized_dir)
    by_ext = os.path.join(organized_dir, "by_extension")
    ext_dirs = sorted(d for d in os.listdir(by_ext)
                      if os.path.isdir(os.path.join(by_ext, d)))
    if moved <= 0 or not ext_dirs:
        failures.append("organize moved %d files, ext dirs %s" % (moved, ext_dirs))
    if not idx_path or not os.path.isfile(idx_path):
        failures.append("organized index.html was not written")
    else:
        with open(idx_path, "r", encoding="utf-8") as fh:
            html_text = fh.read()
        if "by_extension/" not in html_text:
            failures.append("organized index.html has no relative links")
    if not summ_path or not os.path.isfile(summ_path):
        failures.append("organized summary.txt was not written")
    runs = raw_scan.find_recoveries(roots=[run_base])
    if not runs or os.path.abspath(runs[0][0]) != os.path.abspath(recovered_dir):
        failures.append("find_recoveries did not return the new run folder")

    # v5: numbered pickers (sections 11 and 12) - discovery, classification,
    # depth limit and remembered paths, all without any interactive input.
    pick_root = os.path.join(tmpdir, "picks")
    pick_deep = os.path.join(pick_root, "a", "b", "c")
    os.makedirs(pick_deep)
    for name in ("disk0.img", "disk1.img.gz", "half.img.partial", "notes.txt"):
        with open(os.path.join(pick_root, name), "wb") as fh:
            fh.write(b"\x00" * 64)
    with open(os.path.join(pick_deep, "toodeep.img"), "wb") as fh:
        fh.write(b"\x00" * 8)

    found = raw_scan.list_image_candidates(roots=[pick_root], depth=2)
    got = sorted(os.path.basename(entry["path"]) for entry in found)
    if got != ["disk0.img", "disk1.img.gz", "half.img.partial"]:
        failures.append("image picker listed %s" % got)
    if not any(entry["partial"] for entry in found if entry["path"].endswith(".partial")):
        failures.append("interrupted image not flagged as partial")
    if raw_scan.classify_image_name("X.IMG") != (".img", False):
        failures.append("classify_image_name rejected an upper-case .IMG")
    if raw_scan.classify_image_name("photo.jpg")[0] is not None:
        failures.append("classify_image_name accepted a non-image")
    deeper = raw_scan.list_image_candidates(roots=[pick_root], depth=3)
    if not any(entry["path"].endswith("toodeep.img") for entry in deeper):
        failures.append("depth=3 did not reach the nested image")
    if any(entry["path"].endswith("toodeep.img") for entry in found):
        failures.append("image picker walked deeper than its depth limit")

    saved_last_paths = raw_scan.LAST_PATHS_FILE
    raw_scan.LAST_PATHS_FILE = os.path.join(tmpdir, "last_paths.json")
    try:
        raw_scan.remember_path("image", os.path.join(pick_root, "disk0.img"))
        raw_scan.remember_path("destination", pick_root)
        remembered = raw_scan.load_last_paths()
        if remembered.get("image") != pick_root:
            failures.append("remember_path(image) stored %r" % (remembered.get("image"),))
        if remembered.get("destination") != pick_root:
            failures.append("remember_path(destination) stored %r"
                            % (remembered.get("destination"),))
        if len(remembered) != 2:
            failures.append("last_paths has %d keys, expected 2" % len(remembered))
    finally:
        raw_scan.LAST_PATHS_FILE = saved_last_paths

    # v5: parallel phase 1 must return exactly the hits of one sequential pass
    saved_workers = raw_scan.PARALLEL_WORKERS
    saved_min_size = raw_scan.PARALLEL_MIN_SIZE
    raw_scan.PARALLEL_WORKERS = 2
    raw_scan.PARALLEL_MIN_SIZE = 0
    try:
        par_results = os.path.join(tmpdir, "search_results_parallel.txt")
        par_summary = raw_scan.scan(img, patterns, CHUNK, par_results, None,
                                    carve_before=64, carve_after=64,
                                    show_progress=False, min_gap=MIN_GAP)
        par_hits = sorted((r["label"], r["offset"]) for r in par_summary["hits"])
        seq_hits = sorted((r["label"], r["offset"]) for r in summary["hits"])
        if par_hits != seq_hits:
            failures.append("parallel phase 1 changed the hits:\n  seq %s\n  par %s"
                            % (seq_hits, par_hits))
        if par_summary["bytes_scanned"] != summary["bytes_scanned"]:
            failures.append("parallel bytes_scanned = %d, expected %d"
                            % (par_summary["bytes_scanned"], summary["bytes_scanned"]))
        if par_summary["counts"] != summary["counts"]:
            failures.append("parallel counts differ: %s vs %s"
                            % (par_summary["counts"], summary["counts"]))
    except Exception as exc:
        failures.append("parallel phase 1 raised: %s" % exc)
    finally:
        raw_scan.PARALLEL_WORKERS = saved_workers
        raw_scan.PARALLEL_MIN_SIZE = saved_min_size

    # v5 sections 5, 9, 10: fast scan (regions, budget, no carving, report,
    # projection), the speed memory behind the time estimate and the six-item
    # main menu.
    for total_size, pct, expect in ((100000, 5, [(0, 5000), (95000, 100000)]),
                                    (100, 50, [(0, 100)]),
                                    (0, 5, [(0, None)])):
        got_ranges = raw_scan.fast_scan_ranges(total_size, pct)
        if got_ranges != expect:
            failures.append("fast_scan_ranges(%r, %r) = %r, expected %r"
                            % (total_size, pct, got_ranges, expect))

    # a deadline that has already passed must stop before reading a single byte
    past = raw_scan.scan(img, patterns, CHUNK,
                         os.path.join(tmpdir, "deadline.txt"), None,
                         show_progress=False, min_gap=0,
                         deadline=time.time() - 1)
    if past["hits"] or not past.get("budget_hit") or past["bytes_scanned"]:
        failures.append("deadline stop: hits=%d budget_hit=%r scanned=%d"
                        % (len(past["hits"]), past.get("budget_hit"),
                           past["bytes_scanned"]))

    # fast scan on a 4 MB image: one signature in the first 5%, one in the
    # unscanned middle, one in the last 5%
    fast_img = os.path.join(tmpdir, "fast.img")
    unit = 4 * 1024 * 1024
    blob = bytearray(b"\x01" * unit)
    for offset, tag in ((1024, b"notes_head_1.md"),
                        (unit // 2, b"notes_middle_2.md"),
                        (unit - 4096, b"notes_tail_3.md")):
        blob[offset:offset + 3] = b".md"
        blob[offset + 8:offset + 8 + len(tag)] = tag
    with open(fast_img, "wb") as fh:
        fh.write(bytes(blob))

    md_only = [("md ext [utf-8]", b".md", "md", False)]
    saved_gap = raw_scan.MIN_GAP
    raw_scan.MIN_GAP = 0
    try:
        fast = raw_scan.run_fast_scan(fast_img, unit, md_only, True, budget=60,
                                      base_dir=os.path.join(tmpdir, "fastruns"))
    finally:
        raw_scan.MIN_GAP = saved_gap

    head_span = unit * 5 // 100
    tail_start = unit - head_span
    offsets = [h["offset"] for h in fast["hits"]]
    if not any(o < head_span for o in offsets):
        failures.append("fast scan missed the head region")
    if not any(o >= tail_start for o in offsets):
        failures.append("fast scan missed the tail region")
    if any(head_span <= o < tail_start for o in offsets):
        failures.append("fast scan reported a hit outside the sampled regions: %r"
                        % [o for o in offsets if head_span <= o < tail_start])
    if not (2 * head_span <= fast["sampled_bytes"] <= 2 * head_span + 3 * 4096):
        failures.append("fast scan sampled_bytes = %d, expected about %d"
                        % (fast["sampled_bytes"], 2 * head_span))
    if fast["carved_files"] or os.path.isdir(os.path.join(fast["fast_dir"], "carved")):
        failures.append("fast scan carved files (it must not)")
    if not fast.get("report_path") or not os.path.isfile(fast["report_path"]):
        failures.append("fast scan report was not written")
    else:
        with open(fast["report_path"], "r", encoding="utf-8") as fh:
            fast_text = fh.read()
        for needle in ("FAST SCAN RESULT", "RECOMMENDATION", "zero regions",
                       "PROJECTION"):
            if needle not in fast_text:
                failures.append("fast scan report is missing %r" % needle)
    if not os.path.isfile(os.path.join(fast["fast_dir"], raw_scan.PROVENANCE_FILE)):
        failures.append("fast scan provenance.json was not written")
    fast_names = set(name for name, _ in fast["names"])
    if "notes_head_1.md" not in fast_names:
        failures.append("fast scan sample names = %s" % sorted(fast_names)[:8])
    if not fast["projection"].get("usable") or fast["projection"]["projected_hits"] <= 0:
        failures.append("fast scan projection = %r" % (fast["projection"],))

    # a budget that is already exhausted (negative = a deadline in the past)
    # must return a partial run with nothing scanned, not scan anyway
    tiny = raw_scan.run_fast_scan(fast_img, unit, md_only, True, budget=-1,
                                  base_dir=os.path.join(tmpdir, "fastruns"))
    if tiny["hits"] or not tiny["budget_hit"] or tiny["sampled_bytes"]:
        failures.append("exhausted budget: hits=%d budget_hit=%r sampled=%d"
                        % (len(tiny["hits"]), tiny["budget_hit"],
                           tiny["sampled_bytes"]))

    # speed memory behind the pre-scan estimate (Section 10)
    saved_speed_file = raw_scan.SPEED_FILE
    raw_scan.SPEED_FILE = os.path.join(tmpdir, "scan_speed.json")
    try:
        if raw_scan.load_speed_rates():
            failures.append("speed memory did not start empty")
        measured_rate = 100 * 1024 * 1024
        raw_scan.remember_speed("image", measured_rate)
        rates = raw_scan.load_speed_rates()
        if abs((rates.get("image") or 0) - measured_rate) > 1:
            failures.append("remember_speed stored %r" % (rates.get("image"),))
        secs, measured = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "x.img"), rates)
        if not measured or abs(secs - 1.0) > 0.01:
            failures.append("measured estimate = (%r, %r)" % (secs, measured))
        secs, measured = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "y.img"), {})
        if measured or abs(secs - measured_rate / float(raw_scan.DEFAULT_SCAN_RATE)) > 1:
            failures.append("default estimate = (%r, %r)" % (secs, measured))
    finally:
        raw_scan.SPEED_FILE = saved_speed_file

    # the six-item main menu (Section 9)
    menu_src = inspect.getsource(raw_scan.main)
    for needle in ("1 - FAST SCAN", "2 - FULL SCAN", "3 - PROGRESSIVE SCAN",
                   "4 - CREATE DISK IMAGE", "5 - ANALYZE EXISTING IMAGE",
                   "6 - RESUME PREVIOUS RECOVERY"):
        if needle not in menu_src:
            failures.append("main menu is missing %r" % needle)

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
    print("candidate file names extracted (%d names), recovery report written," % len(names))
    print("image picker discovery/depth/partial flags and remembered paths verified.")
    print("fast scan verified: 5%+5% regions, budget stop, no carving, report+projection,")
    print("speed memory for the time estimate and the six-item main menu.")
    shutil.rmtree(tmpdir, ignore_errors=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
