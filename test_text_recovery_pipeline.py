#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_text_recovery_pipeline.py - integration tests for the hybrid pipeline
(text_recovery_pipeline.py), the glue that routes Layer 1 by filesystem,
falls back to Layer 2+3 strings, and honours the guide's caps.

Uses the synthetic builders from test_text_recovery.py (NTFS) and
test_fat32_recovery.py (FAT32) plus in-memory noise/text blobs. No real
disks, no network, stdlib only.
"""

import base64
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import text_recovery_pipeline as pipe
import test_text_recovery as ntfs_tests
import test_fat32_recovery as fat_tests

FAILURES = []
COUNT = [0]


def ck(cond, label):
    COUNT[0] += 1
    if cond:
        print("ok:", label)
    else:
        FAILURES.append(label)
        print("FAIL:", label)


def make_blob(text, noise_a=4096, noise_b=4096):
    return os.urandom(noise_a) + text + os.urandom(noise_b)


def main():
    tmp = tempfile.mkdtemp(prefix="pipeline_test_")
    try:
        ntfs_img, _nt_meta = ntfs_tests.build_ntfs_image()
        ntfs_path = os.path.join(tmp, "ntfs.img")
        with open(ntfs_path, "wb") as fh:
            fh.write(ntfs_img)
        fat_img, _fat_meta = fat_tests.build_fat32_image()
        fat_path = os.path.join(tmp, "fat.img")
        with open(fat_path, "wb") as fh:
            fh.write(fat_img)

        # ---- detect_filesystem -------------------------------------------
        ck(pipe.detect_filesystem(ntfs_path) == "ntfs",
           "detect: ntfs image -> ntfs")
        ck(pipe.detect_filesystem(fat_path) == "fat32",
           "detect: fat32 image -> fat32")
        raw_path = os.path.join(tmp, "zeros.bin")
        with open(raw_path, "wb") as fh:
            fh.write(bytes(2048))
        ck(pipe.detect_filesystem(raw_path) == "unknown",
           "detect: zeroed bytes -> unknown")
        ck(pipe.detect_filesystem(ntfs_path, offset=4096) == "unknown",
           "detect: ntfs image at wrong offset -> unknown")

        # ---- Layer 1 routing: NTFS ---------------------------------------
        out1 = os.path.join(tmp, "o1")
        s1 = pipe.recover(ntfs_path, out_dir=out1)
        ck(s1["filesystem"] == "ntfs" and s1["ok"],
           "route: ntfs image runs the MFT layer")
        ck(s1["metadata"]["deleted"] == 5,
           "route: ntfs deleted count = 5 (got %s)"
           % s1["metadata"]["deleted"])
        names = {c["name"] for c in s1["metadata"]["candidates"]}
        ck("deleted.txt" in names, "route: ntfs finds deleted.txt")
        ck(s1["strings"] is None,
           "route: strings fallback NOT run when metadata finds files")
        carved_names = {os.path.basename(c["path"]) for c in
                        s1["metadata"].get("carved") or []}
        ck(any("deleted" in n for n in carved_names),
           "route: ntfs carve wrote files through the pipeline")

        # ---- Layer 1 routing: FAT32 --------------------------------------
        out2 = os.path.join(tmp, "o2")
        s2 = pipe.recover(fat_path, out_dir=out2)
        ck(s2["filesystem"] == "fat32" and s2["ok"],
           "route: fat32 image runs the FAT layer")
        ck(s2["metadata"]["deleted"] == 7,
           "route: fat32 deleted count = 7 (got %s)"
           % s2["metadata"]["deleted"])

        # ---- Layer 2+3 strings fallback -----------------------------------
        text = ("السلام عليكم this is a long realistic text line for "
                "extraction " * 12).encode("utf-8")
        blob_path = os.path.join(tmp, "mix.bin")
        with open(blob_path, "wb") as fh:
            fh.write(make_blob(text))
        out3 = os.path.join(tmp, "o3")
        s3 = pipe.recover(blob_path, out_dir=out3, strings=True)
        str_sum = s3["strings"]
        ck(str_sum is not None and str_sum["ok"],
           "strings: scan ran with strings=True")
        kept = str_sum["candidates"]
        ck(len(kept) == 1,
           "strings: exactly one kept run in noise+text (got %d)" % len(kept))
        ck(kept[0]["size"] >= 300 and kept[0]["confidence"] >= 0.40,
           "strings: kept run has size and confidence")
        ck(kept[0]["encoding"] in ("utf-8", "ascii"),
           "strings: kept run reports its encoding")
        ck(any("borderline" in c["reason"] or "gate" in c["reason"]
               for c in kept),
           "strings: kept run carries its gate/borderline reason")
        if str_sum["carved"]:
            with open(str_sum["carved"][0]["path"], "rb") as fh:
                data = fh.read()
            ck(text[:64] in data,
               "strings: carved file contains the real text prefix")
        else:
            ck(False, "strings: carved file exists")

        # negatives must never qualify (guide's noise fixtures)
        noise_path = os.path.join(tmp, "noise.bin")
        with open(noise_path, "wb") as fh:
            fh.write(os.urandom(64 * 1024))
        s4 = pipe.recover(noise_path, out_dir=None, strings=True)
        ck(s4["strings"]["candidates"] == [],
           "negative: pure random -> no strings candidates")

        b64_path = os.path.join(tmp, "b64.bin")
        with open(b64_path, "wb") as fh:
            fh.write(base64.b64encode(os.urandom(40000)))
        s5 = pipe.recover(b64_path, out_dir=None, strings=True)
        ck(s5["strings"]["candidates"] == [],
           "negative: base64-of-random rejected by entropy gate")

        u16 = ("نص عربي مهما وطويل للتحقق " * 40).encode("utf-16-le")
        u16_path = os.path.join(tmp, "u16.bin")
        with open(u16_path, "wb") as fh:
            fh.write(os.urandom(2048) + u16 + os.urandom(2048))
        s6 = pipe.recover(u16_path, out_dir=None, strings=True)
        ck(any(c["encoding"] == "utf-16-le"
               for c in s6["strings"]["candidates"]),
           "strings: UTF-16LE Arabic run extracted")

        zip_path = os.path.join(tmp, "t.zip")
        import zipfile
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("inner.txt", "real text " * 400)
        s7 = pipe.recover(zip_path, out_dir=None, strings=True)
        ck(s7["filesystem"] == "unknown", "negative: zip is not a volume fs")
        ck(s7["strings"]["candidates"] == [],
           "negative: deflated zip payload stays non-text")

        # ---- caps ----------------------------------------------------------
        s8 = pipe.recover(blob_path, out_dir=None, strings=True, max_files=1)
        ck(len(s8["strings"]["candidates"]) <= 1,
           "cap: max_files respected")

        # ---- merged report --------------------------------------------------
        s9 = pipe.recover(blob_path, out_dir=None, strings=True)
        report = s9["report"]
        ck(len(report) == 1 and report[0]["layer"] == "strings",
           "report: strings candidate appears with its layer tag")
        ck(s9["metadata"] is None or not s9["metadata"].get("ok") or
           s9["metadata"].get("deleted", 0) == 0,
           "report: unknown fs carries no metadata hits")

        # ---- CLI smoke ------------------------------------------------------
        rc = pipe.main(["p", ntfs_path, "--out", os.path.join(tmp, "cli"),
                       "--no-carve"])
        ck(rc == 0, "cli: exit code 0 on ntfs image (--no-carve)")
        ck(pipe.main(["p"]) == 2, "cli: missing source returns 2")
        ck(pipe.main(["p", fat_path, "--fs", "fat32", "--no-carve"]) == 0,
           "cli: --fs fat32 override works")

        print("-" * 60)
        if FAILURES:
            print("PIPELINE TEST FAILURES: %d" % len(FAILURES))
            for f in FAILURES:
                print("  -", f)
            return 1
        print("ALL TEXT RECOVERY PIPELINE TESTS PASSED (%d assertions)"
              % COUNT[0])
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
