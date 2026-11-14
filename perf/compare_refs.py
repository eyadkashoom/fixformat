#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare the working tree against a fixed git ref, reproducibly.

The point QA made: a before/after table taken from an uncommitted tree cannot be
re-run by anyone. This script takes the baseline from git itself, so the exact
code being compared is identified by a commit SHA, and prints that SHA.

Usage:
    python perf/compare_refs.py                       # baseline = v7.0.3 tag
    python perf/compare_refs.py --baseline e113241
    python perf/compare_refs.py --baseline HEAD --mib 1

Exit code 0 only when every corpus produced the same regions (offset, length,
encoding) in both implementations. Scores may differ by a few hundredths: the
optimised path takes the full-window reading where the old one took the maximum
over trim cuts.
"""

import importlib.util
import os
import random
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import text_search as NEW  # noqa: E402

DEFAULT_BASELINE = "v7.0.3"


def load_baseline(ref):
    """Import text_search.py as of `ref`; returns (module, sha)."""
    sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", ref],
                         capture_output=True, text=True, check=True).stdout.strip()
    src = subprocess.run(["git", "-C", ROOT, "show", "%s:text_search.py" % ref],
                         capture_output=True, text=True, check=True).stdout
    tmp = os.path.join(tempfile.mkdtemp(prefix="ts_base_"), "text_search_base.py")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("text_search_base", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["text_search_base"] = mod
    spec.loader.exec_module(mod)
    return mod, sha


def corpora(mib):
    n = int(mib * 1024 * 1024)
    rnd = random.Random(0x5EED)
    src = open(os.path.join(ROOT, "text_search.py"), "rb").read()
    units = {
        "noise": rnd.randbytes(n),
        "zeros": b"\x00" * n,
        "compressed": (zlib.compress(b"A" * 4096) * (n // 40 + 1))[:n],
        "ascii": (src * (n // len(src) + 1))[:n],
        "arabic": ("قراءة النص العربي لاختبار البحث. " * (n // 60 + 1))[:n].encode("utf-8"),
        "utf16le": ("UTF16LE text block for search. " * (n // 30 + 1))[:n].encode("utf-16-le"),
    }
    bg = bytearray(rnd.randbytes(n))
    for off, blob in ((512, b"SEARCHMARK ASCII hello world recovery.\\n" * 8),
                      (n // 3 - (n // 3) % 64,
                       ("علامة بحث عربية لاختبار الاسترجاع.\\n" * 8).encode("utf-8")),
                      (2 * n // 3 - (2 * n // 3) % 64,
                       b"SEARCHMARK UTF16LE recovery probe.\\n".decode().encode("utf-16-le") * 8)):
        bg[off:off + len(blob)] = blob
    units["mixed + 3 plants"] = bytes(bg)
    return units


def regions(hits):
    return [(h["offset"], h["length"], h["encoding"]) for h in hits]


def main(argv):
    baseline = DEFAULT_BASELINE
    mib = 0.5
    if "--baseline" in argv:
        baseline = argv[argv.index("--baseline") + 1]
    if "--mib" in argv:
        mib = float(argv[argv.index("--mib") + 1])

    old, sha = load_baseline(baseline)
    print("baseline : %s (text_search.py %s)" % (baseline, sha))
    print("candidate: working tree, text_search.py %s\\n" % NEW.VERSION)
    print("%-18s %10s %10s %8s   %s"
          % ("corpus", "base MiB/s", "new MiB/s", "speedup", "regions"))
    print("-" * 72)

    bad = 0
    for name, data in corpora(mib).items():
        t0 = time.perf_counter()
        base_hits = old.search(data, top_n=20)
        dt_old = time.perf_counter() - t0
        t0 = time.perf_counter()
        new_hits = NEW.search(data, top_n=20)
        dt_new = time.perf_counter() - t0
        mb = len(data) / 1048576.0
        same = regions(base_hits) == regions(new_hits)
        if not same:
            bad += 1
        print("%-18s %10.3f %10.3f %7.1fx   %s"
              % (name, mb / dt_old, mb / dt_new, dt_old / dt_new,
                 "same" if same else "MISMATCH"))
        if not same:
            print("    base=%s\\n    new =%s" % (regions(base_hits), regions(new_hits)))

    print("-" * 72)
    print("region mismatches: %d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
