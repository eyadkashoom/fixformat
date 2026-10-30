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
8. the volume-gate pre-scan hook exposes its verdict, warns without
   blocking, and still probes the raw target when the size is unknown.
9. the ANSI/color helpers integrate cleanly: enabled/disabled modes in
   both modules, NO_COLOR + non-TTY invariants, explain() verdict colors,
   and no ESC leaks into any file written during the run.
"""

import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gate_selftest  # noqa: E402
import raw_scan  # noqa: E402
import volume_gate  # noqa: E402

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
    if fast["sampled_bytes"] != 2 * head_span:
        failures.append("fast scan sampled_bytes = %d, expected exactly %d"
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
        if abs((rates.get("image:full") or 0) - measured_rate) > 1:
            failures.append("remember_speed stored %r" % (rates.get("image:full"),))
        secs, measured = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "x.img"), rates)
        if not measured or abs(secs - 1.0) > 0.01:
            failures.append("measured estimate = (%r, %r)" % (secs, measured))
        secs, measured = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "y.img"), {})
        if measured or abs(secs - measured_rate / float(raw_scan.DEFAULT_SCAN_RATE)) > 1:
            failures.append("default estimate = (%r, %r)" % (secs, measured))
        # review S8: a fast scan and a full scan must not share one number
        raw_scan.remember_speed("image", measured_rate * 4, mode="fast")
        rates = raw_scan.load_speed_rates()
        if rates.get("image:fast") == rates.get("image:full"):
            failures.append("fast and full throughput share one key")
        fast_secs, _m = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "z.img"), rates, mode="fast")
        if abs(fast_secs - 0.25) > 0.01:
            failures.append("fast estimate used the wrong rate: %r" % (fast_secs,))
        full_secs, _m = raw_scan.estimate_scan_seconds(
            measured_rate, os.path.join(tmpdir, "z.img"), rates, mode="full")
        if abs(full_secs - 1.0) > 0.01:
            failures.append("full estimate used the wrong rate: %r" % (full_secs,))
    finally:
        raw_scan.SPEED_FILE = saved_speed_file

    # the six-item main menu (Section 9)
    menu_src = inspect.getsource(raw_scan.main)
    for needle in ("1 - FAST SCAN", "2 - FULL SCAN", "3 - PROGRESSIVE SCAN",
                   "4 - CREATE DISK IMAGE", "5 - ANALYZE EXISTING IMAGE",
                   "6 - RESUME PREVIOUS RECOVERY"):
        if needle not in menu_src:
            failures.append("main menu is missing %r" % needle)

    # v5 step 6: signature profiles (Section 7). Hex is spaces-between-bytes,
    # a bare token is TEXT (AutoCAD's header really is the text "AC10").
    prof = raw_scan.parse_signature_profile(
        {"name": "AutoCAD only", "signatures": {"dwg": ["AC10"], ".pdf": ["25 50 44 46 2D"]}})
    if set(prof) != {"dwg", "pdf"}:
        failures.append("profile extensions = %r" % sorted(prof))
    ppats = raw_scan.profile_patterns(prof, True)
    pdf_pats = [entry for entry in ppats if entry[2] == "pdf"]
    dwg_pats = [entry for entry in ppats if entry[2] == "dwg"]
    if not pdf_pats or pdf_pats[0][1] != b"%PDF-":
        failures.append("hex profile value did not decode: %r" % (pdf_pats[:1],))
    if not any(b"AC10" in entry[1] for entry in dwg_pats):
        failures.append("text profile value missing: %r" % (dwg_pats[:2],))
    if len(dwg_pats) < 2:
        failures.append("text profile value searched in %d encoding(s)" % len(dwg_pats))
    for bad_profile in ({}, {"signatures": {}}, {"signatures": {"md": []}},
                        {"signatures": {"m d": ["x"]}}, {"signatures": {"md": [""]}}, []):
        try:
            raw_scan.parse_signature_profile(bad_profile)
            failures.append("profile accepted invalid input: %r" % (bad_profile,))
        except ValueError as exc:
            if not str(exc):
                failures.append("profile error had no message for %r" % (bad_profile,))

    prof_path = os.path.join(tmpdir, "profile.json")
    with open(prof_path, "w", encoding="utf-8") as fh:
        json.dump({"name": "test profile",
                   "signatures": {"md": [".md"], "pdf": ["25 50 44 46 2D"]}}, fh)
    loaded, loaded_name = raw_scan.load_signature_profile(prof_path)
    if set(loaded) != {"md", "pdf"} or loaded_name != "test profile":
        failures.append("load_signature_profile = %r %r" % (loaded, loaded_name))
    bad_path = os.path.join(tmpdir, "bad_profile.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    try:
        raw_scan.load_signature_profile(bad_path)
        failures.append("invalid JSON profile was accepted")
    except ValueError as exc:
        if os.path.basename(bad_path) not in str(exc):
            failures.append("JSON error does not name the file: %s" % exc)

    # a scan driven by a profile reports only the profile's patterns
    prof_scan = raw_scan.scan(img, raw_scan.profile_patterns(loaded, True), CHUNK,
                              os.path.join(tmpdir, "profile_results.txt"), None,
                              show_progress=False, min_gap=0)
    prof_labels = set(entry["label"] for entry in prof_scan["hits"])
    if not prof_labels:
        failures.append("a profile scan found nothing")
    elif any(not label.startswith("Profile .md") for label in prof_labels):
        failures.append("profile scan reported a label outside the profile: %r"
                        % sorted(prof_labels))

    # v5 step 6: output root validation (Section 8)
    good_root = os.path.join(tmpdir, "out_root")
    usable, where = raw_scan.validate_output_root(good_root)
    if not usable or os.path.abspath(where) != os.path.abspath(good_root):
        failures.append("validate_output_root rejected a temp folder: %r" % (where,))
    usable, reason = raw_scan.validate_output_root(os.path.join(img, "sub"))
    if usable or not reason:
        failures.append("validate_output_root accepted a path inside a file")
    if raw_scan.validate_output_root("     ")[0]:
        failures.append("validate_output_root accepted an empty path")

    bs = chr(92)                                    # no escape traps: build paths
    unc = bs + bs + "server" + bs + "share"
    dev_path = bs + bs + "." + bs + "D:"
    if not raw_scan.is_network_path(unc):
        failures.append("is_network_path missed a UNC path")
    if raw_scan.is_network_path("C:" + bs + "local"):
        failures.append("is_network_path called a local path a share")
    if not raw_scan.same_local_volume(dev_path, "D:" + bs + "out"):
        failures.append("same_local_volume missed D: on the same volume")
    if raw_scan.same_local_volume(dev_path, unc):
        failures.append("same_local_volume called a share local")
    if raw_scan.same_local_volume(dev_path, "C:" + bs + "out"):
        failures.append("same_local_volume matched the wrong drive letter")

    # regression (review B1): a hit that STRADDLES a slice boundary must be
    # found by the slice that started it - contiguous slices previously lost
    # max_len-1 bytes at every boundary, depending on parallelism
    edge_img = os.path.join(tmpdir, "edge.img")
    edge = bytearray(b"q" * 2003)
    edge[1000:1003] = b".md"
    with open(edge_img, "wb") as fh:
        fh.write(bytes(edge))
    saved_workers2 = raw_scan.PARALLEL_WORKERS
    raw_scan.PARALLEL_WORKERS = 1                   # the sequential path
    try:
        left = raw_scan.scan(edge_img, md_only, CHUNK, os.path.join(tmpdir, "e1.txt"),
                             None, show_progress=False, min_gap=0,
                             start_offset=0, end_offset=1001)
        right = raw_scan.scan(edge_img, md_only, CHUNK, os.path.join(tmpdir, "e2.txt"),
                              None, show_progress=False, min_gap=0,
                              start_offset=1001, end_offset=2003)
    finally:
        raw_scan.PARALLEL_WORKERS = saved_workers2
    stitched_edge = sorted(h["offset"] for h in left["hits"] + right["hits"])
    if stitched_edge != [1000]:
        failures.append("boundary hit lost across slices: %r (expected [1000])"
                        % stitched_edge)
    if left["bytes_scanned"] + right["bytes_scanned"] != 2003:
        failures.append("slice byte counts = %d + %d, expected 2003"
                        % (left["bytes_scanned"], right["bytes_scanned"]))

    # regression (review B2): a slice stopped by Ctrl+C is 'interrupted' at the
    # offset the read reached - not 'complete' at the end of the source
    prog_dir = os.path.join(tmpdir, "prog_runs")
    prog_org = os.path.join(tmpdir, "prog_org")
    os.makedirs(prog_dir)
    os.makedirs(prog_org)
    real_scan_fn = raw_scan.scan

    def fake_interrupted_scan(target, patterns, chunk, results, carve_dir=None, *a, **kw):
        return {"target": target, "total_size": 4096, "bytes_scanned": 2048,
                "zero_bytes": 0, "elapsed": 0.01, "hits": [],
                "counts": dict((p[0], 0) for p in patterns), "carved_files": 0,
                "interrupted": True, "budget_hit": False,
                "results_path": results, "carve_dir": carve_dir}

    raw_scan.scan = fake_interrupted_scan
    try:
        stopped = raw_scan.run_progressive("stop.img", 4096, md_only, True,
                                           prog_dir, prog_org, "2026-10-29 09:00:00")
    finally:
        raw_scan.scan = real_scan_fn
    stopped_prov = raw_scan.read_provenance(prog_dir)
    if stopped_prov.get("status") != "interrupted":
        failures.append("interrupted slice recorded as %r" % (stopped_prov.get("status"),))
    if stopped_prov.get("resume_offset") != 2048:
        failures.append("interrupted slice resume_offset = %r, expected 2048"
                        % (stopped_prov.get("resume_offset"),))
    if not stopped.get("stopped_early"):
        failures.append("run_progressive did not mark an interrupted slice stopped_early")

    # regression (review B3): the carved window of a .txt hit keeps its exact
    # bytes; the readable transcript is a SEPARATE file
    bin_src = os.path.join(tmpdir, "bin.img")
    raw_window = b"HEAD" + bytes([0xC7, 0xE1, 0xFF]) + b"TAIL" + bytes(32)
    with open(bin_src, "wb") as fh:
        fh.write(raw_window)
    bin_carve = os.path.join(tmpdir, "bin_carved")
    saved_smart = raw_scan.ENABLE_SMART_TEXT
    raw_scan.ENABLE_SMART_TEXT = False
    try:
        bin_summary = raw_scan.scan(bin_src, [("Hermes marker (.txt)", b"HEAD", "txt", False)],
                                    128, os.path.join(tmpdir, "bin_log.txt"), bin_carve,
                                    carve_before=0, carve_after=32,
                                    show_progress=False, min_gap=0)
    finally:
        raw_scan.ENABLE_SMART_TEXT = saved_smart
    if not bin_summary["hits"]:
        failures.append("the .txt regression fixture produced no hit")
    else:
        carved_path = bin_summary["hits"][0]["carved"]
        with open(carved_path, "rb") as fh:
            on_disk = fh.read()
        expected_window = raw_window[:4 + 32]      # plen 4 + carve_after 32
        if on_disk != expected_window:
            failures.append("carved .txt window is not byte-exact: %r != %r"
                            % (on_disk, expected_window))
        readable = carved_path[:-4] + raw_scan.READABLE_SUFFIX
        if os.path.abspath(readable) == os.path.abspath(carved_path):
            failures.append("the readable copy overwrites the carved window")
        if not os.path.isfile(readable):
            failures.append("no separate readable transcript was written")

    # regression (review S4): Ctrl+C inside the parallel pool or while carving
    # returns a partial summary instead of a traceback
    saved_par = raw_scan.parallel_phase1
    saved_minsize = raw_scan.PARALLEL_MIN_SIZE
    saved_workers3 = raw_scan.PARALLEL_WORKERS

    def _raise_interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    raw_scan.parallel_phase1 = _raise_interrupt
    raw_scan.PARALLEL_MIN_SIZE = 0
    raw_scan.PARALLEL_WORKERS = 2
    try:
        intr = raw_scan.scan(img, patterns, CHUNK, os.path.join(tmpdir, "intr.txt"),
                             None, show_progress=False, min_gap=0)
    finally:
        raw_scan.parallel_phase1 = saved_par
        raw_scan.PARALLEL_MIN_SIZE = saved_minsize
        raw_scan.PARALLEL_WORKERS = saved_workers3
    if not intr.get("interrupted"):
        failures.append("Ctrl+C in the parallel pool was not recorded as interrupted")

    saved_carve_phase = raw_scan.carve_phase
    raw_scan.carve_phase = _raise_interrupt
    try:
        intr2 = raw_scan.scan(img, patterns, CHUNK, os.path.join(tmpdir, "intr2.txt"),
                              os.path.join(tmpdir, "intr2_carved"),
                              show_progress=False, min_gap=MIN_GAP)
    finally:
        raw_scan.carve_phase = saved_carve_phase
    if not intr2.get("interrupted") or not intr2["hits"]:
        failures.append("Ctrl+C while carving lost the hits or the interrupted flag")

    # regression (review S6): provenance carries the pattern BYTES, so a resume
    # cannot silently search with a different set after a label rename
    byte_pats = [("Profile .dwg AC10 [utf-8]", b"AC10", "dwg", False),
                 ("PDF document (.pdf)", b"%PDF-", "pdf", False)]
    byte_prov = raw_scan.provenance_payload(
        "x.img", 1024, {"hits": [], "counts": {}, "bytes_scanned": 0,
                        "elapsed": 0.01, "carved_files": 0, "interrupted": False},
        byte_pats, True, "2026-10-29 09:00:00")
    byte_dir = os.path.join(tmpdir, "byte_prov")
    os.makedirs(byte_dir)
    raw_scan.write_provenance(byte_dir, byte_prov)
    rebuilt, rebuilt_warnings = raw_scan.patterns_from_provenance(
        raw_scan.read_provenance(byte_dir))
    if rebuilt != byte_pats:
        failures.append("patterns_from_provenance = %r" % (rebuilt,))
    if rebuilt_warnings:
        failures.append("stored signatures produced warnings: %r" % (rebuilt_warnings,))
    renamed = dict(byte_prov)
    renamed["signature_labels"] = ["a renamed label"] * len(byte_pats)
    renamed_bytes = [entry[1] for entry in raw_scan.patterns_from_provenance(renamed)[0]]
    if renamed_bytes != [entry[1] for entry in byte_pats]:
        failures.append("a label rename changed the stored pattern bytes")
    broken_prov = dict(byte_prov)
    broken_prov["signature_bytes"] = [["x", "zzzz", "md", False]]
    broken_pats, broken_warns = raw_scan.patterns_from_provenance(broken_prov)
    if broken_pats or not broken_warns:
        failures.append("unreadable stored signatures were accepted")

    # regression (review S11): CARVE_LIMIT is one budget for the WHOLE run, not
    # per slice - four slices must not produce four times the files
    span_img = os.path.join(tmpdir, "span.img")
    span_blob = bytearray(b"." * (4 * 1024 * 1024))
    for index in range(4):
        at = index * 1024 * 1024 + 5000
        span_blob[at:at + 3] = b".md"
    with open(span_img, "wb") as fh:
        fh.write(bytes(span_blob))
    span_runs = os.path.join(tmpdir, "span_runs")
    span_org = os.path.join(tmpdir, "span_org")
    os.makedirs(span_runs)
    os.makedirs(span_org)
    saved_limit2 = raw_scan.CARVE_LIMIT
    saved_gap2 = raw_scan.MIN_GAP
    saved_ask2 = raw_scan._ask
    raw_scan.CARVE_LIMIT = 2
    raw_scan.MIN_GAP = 0
    raw_scan._ask = lambda prompt="": ("y", True)
    try:
        span = raw_scan.run_progressive(span_img, os.path.getsize(span_img),
                                        [("Extension .md [utf-8]", b".md", "md", False)],
                                        True, span_runs, span_org,
                                        "2026-10-29 09:00:00", percent=10)
    finally:
        raw_scan.CARVE_LIMIT = saved_limit2
        raw_scan.MIN_GAP = saved_gap2
        raw_scan._ask = saved_ask2
    span_files = 0
    for root in (span_runs, span_org):
        for cur, _dirs, files in os.walk(root):
            span_files += len([name for name in files if name.endswith(".md")
                               and not name.endswith(".triage.txt")
                               and not name.endswith(raw_scan.READABLE_SUFFIX)])
    if span_files > 2:
        failures.append("carve budget leaked across slices: %d files, limit 2"
                        % span_files)
    if span["carved_files"] != span_files:
        failures.append("combined carved_files %d != %d files on disk"
                        % (span["carved_files"], span_files))

    # regression (review S5): summary.txt counts what was carved, not what is
    # left behind after the files were moved into by_extension/
    s5_src = os.path.join(tmpdir, "s5.img")
    with open(s5_src, "wb") as fh:
        fh.write(b"a" * 512 + b".md" + b"b" * 512)
    s5_carve = os.path.join(tmpdir, "s5_carved")
    s5_run = os.path.join(tmpdir, "s5_recovered")
    s5_org = os.path.join(tmpdir, "s5_organized")
    os.makedirs(s5_run)
    os.makedirs(s5_org)
    s5_summary = raw_scan.scan(s5_src, md_only, 128, os.path.join(tmpdir, "s5.txt"),
                               s5_carve, carve_before=16, carve_after=16,
                               show_progress=False, min_gap=0)
    s5_summary["carve_dir"] = s5_carve
    _moved5, _idx5, s5_report = raw_scan.organize_output(s5_summary, s5_run, s5_org)
    if not s5_report or not os.path.isfile(s5_report):
        failures.append("organize_output wrote no summary.txt")
    else:
        with open(s5_report, "r", encoding="utf-8") as fh:
            s5_lines = [line for line in fh.read().splitlines()
                        if line.startswith("recovered files :")]
        if not s5_lines:
            failures.append("summary.txt has no 'recovered files' line")
        elif s5_lines[0].rstrip().endswith(" 0"):
            failures.append("summary.txt reports 0 recovered files after the move: %r"
                            % (s5_lines[0],))

    # regression (review S7): a continued run keeps the folder's totals
    prior_totals = {"hits": 10, "counts": {"a": 10}, "carved_files": 7,
                    "bytes_scanned": 1000, "zero_bytes": 500, "elapsed_seconds": 2.5}
    merged_totals = raw_scan.cumulative_totals(
        prior_totals, {"hits": [1, 2], "counts": {"a": 1, "b": 2},
                       "carved_files": 2, "bytes_scanned": 300,
                       "zero_bytes": 100, "elapsed": 0.5})
    if (merged_totals["hits"] != 12 or merged_totals["counts"] != {"a": 11, "b": 2}
            or merged_totals["carved_files"] != 9
            or merged_totals["bytes_scanned"] != 1300
            or merged_totals["zero_bytes"] != 600
            or abs(merged_totals["elapsed_seconds"] - 3.0) > 0.01):
        failures.append("cumulative_totals = %r" % (merged_totals,))
    overridden = raw_scan.provenance_payload(
        "x.img", 10, {"hits": [1], "counts": {"a": 1}}, byte_pats, True,
        "2026-10-29 09:00:00", totals=merged_totals)
    if overridden.get("hits") != 12 or overridden.get("carved_files") != 9:
        failures.append("provenance totals were not applied: %r"
                        % ({k: overridden.get(k) for k in ("hits", "carved_files")},))

    # regression (review S10): a range that ends at the last byte must not read
    # past the end of the source, and every worker reports its own range
    tail_src = os.path.join(tmpdir, "tail.img")
    with open(tail_src, "wb") as fh:
        fh.write(b"z" * 200 + b".md")
    tail_part = raw_scan._scan_range_worker((tail_src, md_only, 0, 202, 64, None))
    if tuple(tail_part.get("job") or ()) != (0, 202):
        failures.append("worker did not report its range: %r" % (tail_part.get("job"),))
    if [h["offset"] for h in tail_part["hits"]] != [200]:
        failures.append("worker missed the hit in the last bytes: %r"
                        % ([h["offset"] for h in tail_part["hits"]],))

    # step 7 benchmark finding: smart text growth and the readable transcript are
    # both capped by a setting, because a text-heavy 64 MB fixture produced
    # 767 MB of carved data with the 1 MiB cap and a copy per hit
    text_src = os.path.join(tmpdir, "texty.img")
    text_body = bytearray(65 + (i % 26) for i in range(20000))
    text_body[100:103] = b"QQQ"          # ONE hit: a 3-byte needle that the
    with open(text_src, "wb") as fh:     # letter cycle cannot produce again
        fh.write(bytes(text_body))
    text_carve = os.path.join(tmpdir, "texty_carved")
    saved_extra = raw_scan.SMART_TEXT_MAX_EXTRA
    saved_copy = raw_scan.ENABLE_READABLE_COPY
    raw_scan.SMART_TEXT_MAX_EXTRA = 4096
    raw_scan.ENABLE_READABLE_COPY = False
    try:
        texty = raw_scan.scan(text_src, [("Profile .md note", b"QQQ", "md", False)], 4096,
                              os.path.join(tmpdir, "texty.txt"), text_carve,
                              carve_before=1024, carve_after=1024,
                              show_progress=False, min_gap=0)
    finally:
        raw_scan.SMART_TEXT_MAX_EXTRA = saved_extra
        raw_scan.ENABLE_READABLE_COPY = saved_copy
    if not texty["hits"]:
        failures.append("the text-cap fixture produced no hit")
    else:
        window_path = texty["hits"][0]["carved"]
        window_size = os.path.getsize(window_path)
        if window_size > 3 + 1024 + 1024 + 4096 + 4096:
            failures.append("SMART_TEXT_MAX_EXTRA did not cap the window: %d bytes"
                            % window_size)
        transcripts = [name for name in os.listdir(text_carve)
                       if name.endswith(raw_scan.READABLE_SUFFIX)]
        if transcripts:
            failures.append("ENABLE_READABLE_COPY=False still wrote %r" % (transcripts,))

    # v6 Task 1: the volume-gate pre-scan hook (K1) - the verdict is exposed,
    # encrypted/mixed disks warn without blocking, and an unknown size (the
    # shape of open_target on a volume device such as \\.\C:, which refuses
    # a seek-to-end) still probes the raw target. Synthetic fixtures only;
    # raw_scan.py is read-only here, so the hook is driven through
    # run_search_flow with STEP 2 stubbed to stop right after the gate.
    gate_plain = os.path.join(tmpdir, "gate_plain.img")
    gate_selftest.make_gpt_image(gate_plain, [
        dict(type=gate_selftest.BASIC_DATA, name="Basic data partition",
             sectors=256, vbr=gate_selftest.ntfs_vbr(),
             fill=gate_selftest.text_fill),
    ])
    gate_mixed = os.path.join(tmpdir, "gate_mixed.img")
    gate_selftest.make_gpt_image(gate_mixed, [
        dict(type=gate_selftest.BASIC_DATA, name="Basic data partition",
             sectors=256, vbr=gate_selftest.ntfs_vbr(),
             fill=gate_selftest.text_fill),
        dict(type=gate_selftest.BASIC_DATA, name="Basic data partition",
             sectors=256, vbr=gate_selftest.fve_vbr(),
             fill=gate_selftest.random_fill),
    ])
    gate_fve = os.path.join(tmpdir, "gate_fve.img")
    gate_selftest.make_gpt_image(gate_fve, [
        dict(type=gate_selftest.BASIC_DATA, name="Basic data partition",
             sectors=256, vbr=gate_selftest.fve_vbr(),
             fill=gate_selftest.random_fill),
    ])

    # the hook must probe the raw target, print the verdict before STEP 2,
    # and never exit: a warning that aborts the run would be a silent block.
    hook_src = inspect.getsource(raw_scan.run_search_flow)
    probe_at = hook_src.find("probe_source(target)")
    gate_at = hook_src.find("[GATE]")
    step2_at = hook_src.find("= ask_signature_source()")
    if probe_at < 0 or gate_at < 0 or step2_at < 0 \
            or not (probe_at < gate_at < step2_at):
        failures.append("run_search_flow has no gate probe of the raw target "
                        "before STEP 2 (size=None regression)")
        probe_at, step2_at = 0, 0
    hook_block = hook_src[probe_at:step2_at]
    if "sys.exit" in hook_block \
            or re.search(r"(?m)^\s*return\b", hook_block):
        failures.append("the gate hook exits or returns early: it must warn, never block")

    # drive the real pre-scan path: total=0 is the unknown-size shape, and the
    # STEP 2 stub proves the gate let the run through by stopping it there.
    gate_wants = (
        ("plaintext", gate_plain,
         "[GATE] verdict=plaintext | scannable=1 | blocked=0"),
        ("mixed", gate_mixed,
         "[GATE] verdict=mixed | scannable=1 | blocked=1"),
        ("encrypted", gate_fve,
         "[GATE] verdict=encrypted | scannable=0 | blocked=1"),
    )
    real_probe = volume_gate.probe_source
    gate_seen = []
    gate_times = {}

    def spy_probe(target, *args, **kwargs):
        started = time.time()
        try:
            return real_probe(target, *args, **kwargs)
        finally:
            gate_times[target] = time.time() - started
            gate_seen.append(target)

    def stop_after_gate():
        raise SystemExit(77)

    saved_probe = volume_gate.probe_source
    saved_ask_sig = raw_scan.ask_signature_source
    volume_gate.probe_source = spy_probe
    raw_scan.ask_signature_source = stop_after_gate
    try:
        for label, image, want_line in gate_wants:
            del gate_seen[:]
            buf = io.StringIO()
            hook_start = time.time()
            try:
                with contextlib.redirect_stdout(buf):
                    raw_scan.run_search_flow(image, 0)
            except SystemExit as exc:
                hook_elapsed = time.time() - hook_start
                if exc.code != 77:
                    failures.append("run_search_flow(%s) exited with %r: "
                                    "gate warnings must not block" % (label, exc.code))
                    continue
            except Exception as exc:
                failures.append("run_search_flow(%s) raised %r inside the gate"
                                % (label, exc))
                continue
            else:
                failures.append("run_search_flow(%s) never reached STEP 2: "
                                "the gate blocked the scan" % label)
                continue
            text_out = buf.getvalue()
            if gate_seen != [image]:
                failures.append("the gate probed %r instead of the raw target "
                                "for the %s fixture" % (gate_seen, label))
            if "Size            : unknown" not in text_out:
                failures.append("the %s run did not take the unknown-size path"
                                % label)
            if want_line not in text_out:
                failures.append("hook verdict line missing for %s (want %r)"
                                % (label, want_line))
            if label == "plaintext" and "No BitLocker volume found" not in text_out:
                failures.append("the plaintext hook did not confirm a clean disk")
            if label == "mixed" and "is BitLocker-encrypted" not in text_out:
                failures.append("the mixed hook did not name the encrypted partition")
            if label == "encrypted" and "fully BitLocker-encrypted" not in text_out:
                failures.append("the encrypted hook printed no BitLocker warning")
            probe_secs = gate_times.get(image)
            if probe_secs is None or probe_secs > 5.0:
                failures.append("probe_source took %s on the %s fixture "
                                "(budget 5 s)"
                                % ("missing" if probe_secs is None
                                   else "%.2f s" % probe_secs, label))
            if hook_elapsed is None or hook_elapsed > 5.0:
                failures.append("the pre-scan hook took %s on the %s fixture "
                                "(budget 5 s)"
                                % ("missing" if hook_elapsed is None
                                   else "%.2f s" % hook_elapsed, label))
    finally:
        volume_gate.probe_source = saved_probe
        raw_scan.ask_signature_source = saved_ask_sig


    # section 12: a destination on the source's own PHYSICAL disk is refused
    bs12 = chr(92)
    dev_form = bs12 + bs12 + "." + bs12 + "PhysicalDrive0"
    if raw_scan.physical_disk_number(dev_form) != 0:
        failures.append("physical_disk_number did not read the device number")
    unc_path = bs12 + bs12 + "server" + bs12 + "share"
    if raw_scan.physical_disk_number(unc_path) is not None:
        failures.append("physical_disk_number invented a disk for a share")
    if raw_scan.IS_WINDOWS:
        here = os.path.abspath(tmpdir)
        letter = raw_scan.path_drive_letter(here)
        if letter:
            own = raw_scan.physical_disk_number(here)
            if not isinstance(own, int):
                failures.append("physical_disk_number returned %r for a local path" % (own,))
            if raw_scan.same_physical_disk(os.path.join(here, "a.img"),
                                           os.path.join(here, "out")) is not True:
                failures.append("same_physical_disk missed the same local disk")
    # the refusal needs an explicit override, and a refusal falls back.
    # physical_disk_number is faked so the test does not depend on which disk
    # this machine happens to mount where.
    saved_ask12 = raw_scan._ask
    saved_pdn = raw_scan.physical_disk_number
    raw_scan.physical_disk_number = lambda path: (7 if "same" in str(path)
                                                 else (3 if "other" in str(path)
                                                       else None))
    try:
        same_pair = ("same_source.img", "same_dest")
        other_pair = ("same_source.img", "other_dest")
        raw_scan._ask = lambda prompt="": ("no", True)
        if raw_scan.confirm_same_disk(*same_pair):
            failures.append("confirm_same_disk accepted the same disk without an override")
        raw_scan._ask = lambda prompt="": ("YES", True)
        if not raw_scan.confirm_same_disk(*same_pair):
            failures.append("confirm_same_disk ignored the explicit YES override")
        raw_scan._ask = lambda prompt="": ("y", True)
        if not raw_scan.confirm_same_disk(*other_pair):
            failures.append("confirm_same_disk refused a different physical disk")
        if not raw_scan.confirm_same_disk("unknown_src", unc_path):
            failures.append("confirm_same_disk refused a share it cannot resolve")
    finally:
        raw_scan._ask = saved_ask12
        raw_scan.physical_disk_number = saved_pdn

    # section 12 / S9: an unusable folder must raise a clear error, not traceback
    try:
        raw_scan.run_fast_scan(fast_img, unit, md_only, True, budget=-1,
                               base_dir=os.path.join(img, "not_a_folder"))
        failures.append("run_fast_scan accepted an unusable base_dir")
    except ValueError:
        pass

    # v6 K4: ANSI/color helper integration (raw_scan + volume_gate) - enabled
    # and disabled modes, contract invariants, and regression cases.
    # Synthetic fixtures only; monkeypatches are restored in finally blocks.
    ansi_skips = []
    _ANSI_ESC = "\x1b["
    _ANSI_RESET = "\x1b[0m"
    _ANSI_CODES = {"green": "\x1b[32m", "red": "\x1b[31m",
                   "yellow": "\x1b[33m", "blue": "\x1b[34m"}
    try:
        _ansi_live = raw_scan._ansi_enabled()
    except Exception as _ansi_exc:
        ansi_skips.append("live _ansi_enabled() raised %r; enabled-path via stub only"
                          % (_ansi_exc,))
    else:
        if _ansi_live:
            ansi_skips.append("live stdout is a TTY: enabled-path exercised live")
        else:
            ansi_skips.append("live stdout is non-TTY: disabled-path exercised live; "
                              "enabled-path via stub")

    for _mod_label, _mod in (("raw_scan", raw_scan),
                             ("volume_gate", volume_gate)):
        if not callable(getattr(_mod, "_ansi_enabled", None)):
            failures.append("%s._ansi_enabled missing or not callable" % _mod_label)
            continue
        for _fname in ("green", "red", "yellow", "blue"):
            if not callable(getattr(_mod, _fname, None)):
                failures.append("%s.%s helper missing or not callable"
                                % (_mod_label, _fname))
        if failures and any(_mod_label in item and "helper missing" in item
                            for item in failures[-4:]):
            continue
        _orig_flag = _mod._ansi_enabled
        try:
            _mod._ansi_enabled = lambda: False
            for _fname in ("green", "red", "yellow", "blue"):
                _fn = getattr(_mod, _fname)
                if _fn("plain") != "plain":
                    failures.append("%s.%s(disabled) altered text: %r"
                                    % (_mod_label, _fname, _fn("plain")))
                if _ANSI_ESC in _fn("plain"):
                    failures.append("%s.%s(disabled) leaked ESC" % (_mod_label, _fname))
                if _fn("") != "":
                    failures.append("%s.%s(disabled) altered empty string"
                                    % (_mod_label, _fname))
                if _fn(ARABIC) != ARABIC:
                    failures.append("%s.%s(disabled) altered unicode"
                                    % (_mod_label, _fname))
            _mod._ansi_enabled = lambda: True
            for _fname, _code in _ANSI_CODES.items():
                _fn = getattr(_mod, _fname)
                _out = _fn("plain")
                if _code not in _out or _ANSI_RESET not in _out:
                    failures.append("%s.%s(enabled) missing wrap: %r"
                                    % (_mod_label, _fname, _out))
                if "plain" not in _out:
                    failures.append("%s.%s(enabled) dropped input text"
                                    % (_mod_label, _fname))
                if _out != _code + "plain" + _ANSI_RESET:
                    failures.append("%s.%s(enabled) format = %r, want exact wrap"
                                    % (_mod_label, _fname, _out))
        finally:
            _mod._ansi_enabled = _orig_flag

    # contract invariants on the REAL _ansi_enabled: NO_COLOR wins over a TTY,
    # a non-TTY disables, and a TTY without NO_COLOR enables.
    class _AnsiFakeTTY(io.StringIO):
        def isatty(self):
            return True

    class _AnsiFakeNoTTY(io.StringIO):
        def isatty(self):
            return False

    class _AnsiBoomTTY(io.StringIO):
        def isatty(self):
            raise OSError("no tty here")

    _saved_stdout = sys.stdout
    _saved_nocolor = os.environ.get("NO_COLOR")
    try:
        for _mod_label, _mod in (("raw_scan", raw_scan),
                                 ("volume_gate", volume_gate)):
            os.environ["NO_COLOR"] = "1"
            sys.stdout = _AnsiFakeTTY()
            try:
                if _mod._ansi_enabled():
                    failures.append("%s._ansi_enabled ignored NO_COLOR on a TTY"
                                    % _mod_label)
            finally:
                sys.stdout = _saved_stdout
            if "NO_COLOR" in os.environ:
                del os.environ["NO_COLOR"]
            sys.stdout = _AnsiFakeNoTTY()
            try:
                if _mod._ansi_enabled():
                    failures.append("%s._ansi_enabled enabled on non-TTY"
                                    % _mod_label)
            finally:
                sys.stdout = _saved_stdout
            sys.stdout = _AnsiFakeTTY()
            try:
                if not _mod._ansi_enabled():
                    failures.append("%s._ansi_enabled disabled on TTY without NO_COLOR"
                                    % _mod_label)
            finally:
                sys.stdout = _saved_stdout
            sys.stdout = _AnsiBoomTTY()
            try:
                if _mod._ansi_enabled():
                    failures.append("%s._ansi_enabled did not fail closed on isatty error"
                                    % _mod_label)
            finally:
                sys.stdout = _saved_stdout
            os.environ["NO_COLOR"] = ""
            sys.stdout = _AnsiFakeTTY()
            try:
                if not _mod._ansi_enabled():
                    failures.append("%s._ansi_enabled treated empty NO_COLOR as set"
                                    % _mod_label)
            finally:
                sys.stdout = _saved_stdout
    finally:
        sys.stdout = _saved_stdout
        if _saved_nocolor is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = _saved_nocolor

    # explain() verdict -> color mapping, plus strip-invariant regression.
    _scan_part = {"index": 0, "bytes": 131072, "kind": "ntfs",
                  "name": "Basic data partition"}
    _enc_part = {"index": 1, "bytes": 131072, "kind": "bitlocker",
                 "name": "Basic data partition", "entropy": []}
    _plain_probe = {"verdict": "plaintext", "partitions": [_scan_part],
                    "blocked": [], "scannable": [_scan_part], "skipped": [],
                    "content": {}, "size": None}
    _enc_probe = {"verdict": "encrypted", "partitions": [_enc_part],
                  "blocked": [_enc_part], "scannable": [], "skipped": [],
                  "content": {}, "size": None}
    _mixed_probe = {"verdict": "mixed", "partitions": [_scan_part, _enc_part],
                    "blocked": [_enc_part], "scannable": [_scan_part],
                    "skipped": [], "content": {}, "size": None}
    _empty_probe = {"verdict": "empty", "partitions": [], "blocked": [],
                    "scannable": [], "skipped": [], "content": {}, "size": None}
    _err_probe = {"error": "denied", "error_hint": "hint",
                  "partitions": [], "blocked": [], "scannable": [],
                  "skipped": [], "content": {}, "size": None}
    _orig_vg_flag = volume_gate._ansi_enabled
    try:
        volume_gate._ansi_enabled = lambda: True
        _e_plain = volume_gate.explain(_plain_probe)
        _e_enc = volume_gate.explain(_enc_probe)
        _e_mixed = volume_gate.explain(_mixed_probe)
        _e_empty = volume_gate.explain(_empty_probe)
        _e_err = volume_gate.explain(_err_probe)
        if "\x1b[32m" not in _e_plain:
            failures.append("explain(plaintext, enabled) missing green (32m)")
        if "\x1b[31m" not in _e_enc:
            failures.append("explain(encrypted, enabled) missing red (31m)")
        if "\x1b[33m" not in _e_mixed:
            failures.append("explain(mixed, enabled) missing yellow (33m)")
        if "\x1b[33m" not in _e_empty:
            failures.append("explain(empty, enabled) missing yellow (33m)")
        if "\x1b[" in _e_err:
            failures.append("explain(error, enabled) should stay uncolored, got %r"
                            % (_e_err[:80],))
        volume_gate._ansi_enabled = lambda: False
        _p_plain = volume_gate.explain(_plain_probe)
        _p_enc = volume_gate.explain(_enc_probe)
        _p_mixed = volume_gate.explain(_mixed_probe)
        _p_empty = volume_gate.explain(_empty_probe)
        for _lbl, _txt in (("plaintext", _p_plain), ("encrypted", _p_enc),
                           ("mixed", _p_mixed), ("empty", _p_empty)):
            if "\x1b[" in _txt:
                failures.append("explain(%s, disabled) leaked ESC" % _lbl)
        for _lbl, _en, _dis in (("plaintext", _e_plain, _p_plain),
                                ("encrypted", _e_enc, _p_enc),
                                ("mixed", _e_mixed, _p_mixed),
                                ("empty", _e_empty, _p_empty)):
            _stripped = re.sub(r"\x1b\[[0-9;]*m", "", _en)
            if _stripped != _dis:
                failures.append("explain(%s) changes text besides codes" % _lbl)
    finally:
        volume_gate._ansi_enabled = _orig_vg_flag

    # volume_gate.main must wrap the terminal message in blue(); the gate
    # decision logic itself is covered by gate_selftest (untouched here).
    try:
        _main_src = inspect.getsource(volume_gate.main)
    except (IOError, TypeError) as _exc:
        ansi_skips.append("volume_gate.main source unavailable (%s); blue-wire check "
                          "skipped" % (_exc,))
    else:
        if "blue(" not in _main_src:
            failures.append("volume_gate.main does not wrap output in blue()")
    if "green" not in getattr(volume_gate, "__all__", []) \
            or "red" not in getattr(volume_gate, "__all__", []) \
            or "yellow" not in getattr(volume_gate, "__all__", []) \
            or "blue" not in getattr(volume_gate, "__all__", []):
        failures.append("volume_gate.__all__ missing color helpers: %r"
                        % (getattr(volume_gate, "__all__", None),))

    # no-file-leak regression: force colors ON, rescan the synthetic image,
    # and prove the results file carries no ESC and identical hits.
    _orig_rs_flag = raw_scan._ansi_enabled
    _orig_vg_flag2 = volume_gate._ansi_enabled
    raw_scan._ansi_enabled = lambda: True
    volume_gate._ansi_enabled = lambda: True
    try:
        _leak_results = os.path.join(tmpdir, "ansi_leak.txt")
        _leak_summary = raw_scan.scan(img, patterns, CHUNK, _leak_results, None,
                                      carve_before=64, carve_after=64,
                                      show_progress=False, min_gap=MIN_GAP)
    finally:
        raw_scan._ansi_enabled = _orig_rs_flag
        volume_gate._ansi_enabled = _orig_vg_flag2
    with open(_leak_results, "r", encoding="utf-8") as _fh:
        _leak_text = _fh.read()
    if "\x1b[" in _leak_text:
        failures.append("scan results file leaked ANSI with colors forced on")
    _leak_hits = sorted((r["label"], r["offset"]) for r in _leak_summary["hits"])
    if _leak_hits != sorted(expected):
        failures.append("scan hits changed with colors forced on: %r" % (_leak_hits,))
    for _fname in (report_path, os.path.join(tmpdir, "profile_results.txt"),
                   os.path.join(tmpdir, "deadline.txt")):
        if os.path.isfile(_fname):
            with open(_fname, "r", encoding="utf-8", errors="replace") as _fh:
                if "\x1b[" in _fh.read():
                    failures.append("file %s leaked ANSI" % os.path.basename(_fname))
    with open(fast["report_path"], "r", encoding="utf-8") as _fh:
        if "\x1b[" in _fh.read():
            failures.append("fast scan report leaked ANSI")

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
    print("step 6 verified: JSON signature profiles (parse/load/refusals/profile scan),")
    print("output-root validation, UNC detection and the same-volume warning trigger.")
    print("review regressions covered: boundary hits across slices, an interrupted slice")
    print("recorded as interrupted, byte-exact .txt windows with a separate transcript,")
    print("Ctrl+C inside the pool and while carving, pattern bytes in provenance and")
    print("the same-physical-disk refusal with its explicit override, and a clear")
    print("error (not a traceback) for an unusable output folder.")
    print("one carve budget across slices, a real count in summary.txt, the")
    print("folder's totals on resume, per-mode speed memory and the worker's")
    print("end-of-source clamp, and the smart-text cap plus the readable-copy toggle.")
    print("v6 Task 1 gate verified: plaintext allows, encrypted/mixed warn without")
    print("blocking, the hook probes the raw target at unknown size, the verdict is")
    print("exposed from the pre-scan path, and every probe stays under budget.")
    print("v6 K4 ANSI verified: helpers in raw_scan + volume_gate (enabled/disabled),")
    print("NO_COLOR/non-TTY/isatty-error invariants, explain() verdict colors with")
    print("strip-invariant text, blue() terminal wire-up, and no ESC in any file.")
    for _skip in ansi_skips:
        print("ANSI note: %s" % _skip)
    shutil.rmtree(tmpdir, ignore_errors=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
