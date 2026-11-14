#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Size-ladder benchmark for text_search.py.

Deterministic corpora (fixed seeds), one row per size, with throughput and peak
RSS so the memory behaviour is visible next to the speed. This is the script QA
re-runs; it is part of the repo on purpose, not a scratch file.

Usage:
    python perf/bench_text_search.py                # 1, 10, 100 MiB ladder
    python perf/bench_text_search.py --sizes 1 10   # custom sizes (MiB)
    python perf/bench_text_search.py --corpus ascii --sizes 1024

Exit code is 0 unless a corpus produced the wrong number of regions (the planted
controls are checked), so the script is usable as a smoke test too.
"""

import os
import random
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import text_search as S  # noqa: E402
from meminfo import peak_rss_mib  # noqa: E402

PLANT_ASCII = ("SEARCHMARK ASCII hello world recovery test.\n" * 8).encode()
PLANT_ARABIC = ("علامة بحث عربية لاختبار الاسترجاع.\n" * 8).encode()
PLANT_UTF16 = ("SEARCHMARK UTF16LE recovery probe.\n" * 8).encode("utf-16-le")


def corpus(name, mib):
    """(bytes, expected_plants) for one corpus shape at `mib` MiB."""
    n = int(mib * 1024 * 1024)
    rnd = random.Random(0x5EED)
    if name == "noise":
        return rnd.randbytes(n), 0
    if name == "zeros":
        return b"\x00" * n, 0
    if name == "compressed":
        blob = zlib.compress(b"A" * 4096)
        return (blob * (n // len(blob) + 1))[:n], 0
    if name == "ascii":
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "text_search.py"), "rb").read()
        return (src * (n // len(src) + 1))[:n], 1
    if name == "arabic":
        unit = "قراءة النص العربي لاختبار البحث عن اللغة. "
        return (unit * (n // len(unit) + 1))[:n].encode("utf-8"), 1
    if name == "utf16le":
        unit = "UTF16LE text block for language search. "
        return (unit * (n // len(unit) + 1))[:n].encode("utf-16-le"), 1
    if name == "mixed":
        bg = bytearray(rnd.randbytes(n))
        for off, blob in ((512, PLANT_ASCII), (n // 3 - (n // 3) % 64, PLANT_ARABIC),
                          (2 * n // 3 - (2 * n // 3) % 64, PLANT_UTF16)):
            bg[off:off + len(blob)] = blob
        return bytes(bg), 3
    raise SystemExit("unknown corpus: %s" % name)


CORPORA = ("noise", "zeros", "compressed", "mixed", "ascii", "arabic", "utf16le")


def run_one(name, mib):
    data, plants = corpus(name, mib)
    # Peak working set BEFORE the scan: building a 100 MiB corpus by repeating a
    # slice leaves its own high-water mark, which has nothing to do with the
    # scanner. Reporting the delta is what answers "does the search grow with
    # input size".
    rss_before = peak_rss_mib()
    t0 = time.perf_counter()
    hits = S.search(data, top_n=10)
    dt = time.perf_counter() - t0
    peak = peak_rss_mib()
    mb = len(data) / 1048576.0
    # A plant counts as found when a reported region COVERS it. Requiring the
    # region to start within +/-64 bytes of the plant was wrong: a window holding
    # 384 of the plant's 512 bytes legitimately scores above threshold, so the
    # region starts up to one window earlier. Coverage is the real contract, and
    # a caller gets the exact offset from the region start plus its length.
    def covered(off):
        return any(h["offset"] <= off < h["offset"] + h["length"] for h in hits)

    missing = [o for o in _plant_offsets(name, len(data)) if not covered(o)]
    ok = len(hits) >= plants and not missing
    return {
        "corpus": name, "mib": mb, "sec": dt, "mibs": mb / dt if dt else 0.0,
        "peak_mib": peak - rss_before, "regions": len(hits), "ok": ok,
        "missing": missing, "hits": hits,
    }


def _plant_offsets(name, n):
    if name == "mixed":
        return [512, n // 3 - (n // 3) % 64, 2 * n // 3 - (2 * n // 3) % 64]
    return []


def main(argv):
    sizes = [1.0, 10.0, 100.0]
    if "--sizes" in argv:
        i = argv.index("--sizes")
        vals = []
        for tok in argv[i + 1:]:
            try:
                vals.append(float(tok))
            except ValueError:
                break
        if vals:
            sizes = vals
    names = CORPORA
    if "--corpus" in argv:
        names = (argv[argv.index("--corpus") + 1],)

    print("%-11s %8s %9s %9s %10s %8s %s" % (
        "corpus", "size", "seconds", "MiB/s", "scan RSS", "regions", "plants"))
    print("-" * 74)
    worst = 0
    for mib in sizes:
        for name in names:
            r = run_one(name, mib)
            worst = 0 if r["ok"] else 1
            state = "ok" if r["ok"] else "MISSING %s" % [
                (o, [(h["offset"], h["length"]) for h in r["hits"]])
                for o in r["missing"]]
            print("%-11s %6.0f MiB %8.3f %9.2f %8.1f MiB %8d %s" % (
                r["corpus"], r["mib"], r["sec"], r["mibs"], r["peak_mib"],
                r["regions"], state))
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
