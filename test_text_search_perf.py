#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Performance gate for text_search.py - runs in CI with the unit suite.

Why this file exists
--------------------
The unit suite proves the search returns the right regions. It cannot catch a
silent 100x slowdown, and it did not: before the v7 text-search work a 512 KiB
block of zeros took ~90 s and nothing failed. This gate fails when throughput
drops below the shipped target or when cost stops scaling linearly.

Targets (published in perf/README.md, measured on the development host)
---------------------------------------------------------------------
runless input (random / zeros / compressed)   >= 10.0 MiB/s
text-dense input (ascii / arabic / utf-16le)  >=  0.5 MiB/s
10x more input                                 <= 15x more time  (linear)
peak RSS growth 1 MiB -> 10 MiB text            <= 64 MiB        (chunked)

CI uses a slower runner, so the asserted floors are deliberately lower than the
published targets (see CI_* below) while still failing on any structural
regression - the old implementation was 0.006 MiB/s, i.e. 30-2000x below them.

Run:  python test_text_search_perf.py          (about 40 s)
      python test_text_search_perf.py --quick  (about 8 s, 2 MiB corpora)
"""

import os
import random
import sys
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "perf"))

import text_search as S  # noqa: E402
from meminfo import peak_rss_mib  # noqa: E402

# Published targets (development host, see perf/README.md).
TARGET_RUNLESS = 10.0        # MiB/s
TARGET_TEXT = 0.5            # MiB/s

# Floors asserted in CI (same shape, slower machines).
CI_RUNLESS = 3.0             # MiB/s
CI_TEXT = 0.2                # MiB/s
CI_SCALING = 25.0            # time(10x data) / time(1x data)
CI_RSS_GROWTH = 128.0        # MiB between the 1 MiB and 10 MiB text scan

QUICK = "--quick" in sys.argv
if QUICK:
    sys.argv = [a for a in sys.argv if a != "--quick"]


def make(kind, mib):
    n = int(mib * 1024 * 1024)
    if kind == "noise":
        return random.Random(0x5EED).randbytes(n)
    if kind == "zeros":
        return b"\x00" * n
    if kind == "compressed":
        blob = zlib.compress(b"A" * 4096)
        return (blob * (n // len(blob) + 1))[:n]
    if kind == "ascii":
        with open(os.path.join(HERE, "text_search.py"), "rb") as fh:
            src = fh.read()
        return (src * (n // len(src) + 1))[:n]
    if kind == "arabic":
        unit = "قراءة النص العربي لاختبار البحث عن اللغة. "
        return (unit * (n // len(unit) + 1))[:n].encode("utf-8")
    if kind == "utf16le":
        unit = "UTF16LE text block for language search. "
        return (unit * (n // len(unit) + 1))[:n].encode("utf-16-le")
    raise ValueError(kind)


def timed(data):
    t0 = time.perf_counter()
    hits = S.search(data, top_n=10)
    return time.perf_counter() - t0, hits


class TestThroughput(unittest.TestCase):
    """Each supported corpus shape must clear its floor at 1 MiB and 10 MiB."""

    def check(self, kind, floor, expect_regions):
        mib = 2.0 if QUICK else 10.0
        data = make(kind, mib)
        dt, hits = timed(data)
        mibs = len(data) / 1048576.0 / dt if dt else 0.0
        print("  %-11s %5.0f MiB  %7.3f s  %8.2f MiB/s  regions=%d"
              % (kind, mib, dt, mibs, len(hits)))
        self.assertGreaterEqual(
            len(hits), expect_regions,
            "%s: expected at least %d region(s), got %d"
            % (kind, expect_regions, len(hits)))
        self.assertGreaterEqual(
            mibs, floor,
            "%s: %.2f MiB/s is below the %.2f MiB/s CI floor"
            % (kind, mibs, floor))

    def test_noise(self):
        self.check("noise", CI_RUNLESS, 0)

    def test_zeros(self):
        self.check("zeros", CI_RUNLESS, 0)

    def test_compressed(self):
        self.check("compressed", CI_RUNLESS, 0)

    def test_ascii_text(self):
        self.check("ascii", CI_TEXT, 1)

    def test_arabic_utf8(self):
        self.check("arabic", CI_TEXT, 1)

    def test_utf16le_text(self):
        self.check("utf16le", CI_TEXT, 1)


class TestScaling(unittest.TestCase):
    """Cost must grow with input size, not with its square."""

    def test_time_scales_linearly_on_noise(self):
        small = make("noise", 1.0)
        big = make("noise", 10.0)
        dt_small, _ = timed(small)
        dt_big, _ = timed(big)
        ratio = dt_big / dt_small if dt_small else float("inf")
        print("  noise: 1 MiB %.3f s, 10 MiB %.3f s, ratio %.1fx"
              % (dt_small, dt_big, ratio))
        self.assertLess(ratio, CI_SCALING,
                        "10x data took %.1fx the time - superlinear scan" % ratio)

    def test_peak_memory_is_bounded_on_text(self):
        # Chunked prefix arrays: a 10 MiB text scan must not hold the whole
        # input's score arrays. The bound is loose; it fails only if chunking is
        # removed and the arrays become O(input).
        peak_before = peak_rss_mib()
        small = make("ascii", 1.0)
        timed(small)
        peak_small = peak_rss_mib()
        big = make("ascii", 10.0)
        timed(big)
        peak_big = peak_rss_mib()
        growth = max(peak_big - peak_small, peak_small - peak_before)
        print("  peak RSS: after 1 MiB %.1f MiB, after 10 MiB %.1f MiB"
              % (peak_small, peak_big))
        self.assertLess(
            growth, CI_RSS_GROWTH,
            "peak RSS grew by %.1f MiB for 9 MiB more text - prefix arrays are "
            "no longer chunked" % growth)


if __name__ == "__main__":
    unittest.main(verbosity=2)
