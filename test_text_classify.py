#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_text_classify.py - Phase 3 tests for text_classify.py
(الطبقة 2 strings extraction + الطبقة 3 n-gram/text classification,
v7/TextRecoveryGuide.md).

Fixtures are built in memory; the only disk use is tempfile.mkdtemp for the
module's __main__ demo path check.

  positives : ASCII prose (2KiB+), Python source, JSON, UTF-8 Arabic,
              UTF-16LE Arabic + UTF-16LE ASCII logs, markdown, shell, HTML,
              CSV, an exact-256-byte run
  negatives : random bytes, ZIP bytes (deflate of a real text file),
              gzip of text, BitLocker-like hmac-sha256 keystream,
              base64/base32 of random, zlib of random, sparse binaries

Also covers: the length gate at the 255/256 boundary, the borderline
(printable>=0.95, 4.0<entropy<=4.5) rule, soft markers never flipping the
gate, run extraction (positions, UTF-16LE, <=2 NUL tolerance, MAX_RUNS cap)
and a precision/recall/F1 corpus harness that must reach F1 == 1.0 on the
documented fixtures.
"""

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import random
import shutil
import sys
import tempfile
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import text_classify as tc

FAILS = []


def ck(cond, msg):
    if cond:
        print("ok:", msg)
    else:
        print("FAIL:", msg)
        FAILS.append(msg)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

AR_SENT = ("\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645 "
           "\u0647\u0630\u0627 \u0646\u0635 \u0639\u0631\u0628\u064a "
           "\u0644\u0644\u0627\u062e\u062a\u0628\u0627\u0631. ")

F_PROSE = ("hello world this file contains plain english words in simple lines\n"
           * 36).encode()
F_PROSE2K = ("the quick brown fox jumps over the lazy dog while the owl watches\n"
             * 34).encode()
F_PY = ("#!/usr/bin/python\nimport os\nimport re\nimport sys\n\n"
        "def run(name, mode):\n"
        "    for i in data:\n"
        "        total = total + i\n"
        "    return total\n"
        "if main():\n    print(run(os, sys))\n" * 6).encode()
F_MD = ("# title\n- item one line\n- item two line\n- item three line\n\n"
        "text body line here and more words follow the same pattern\n" * 8).encode()
F_JSON = json.dumps([{"id": i, "name": "item", "ok": True}
                     for i in range(60)]).encode()
F_CSV = b"name,value,note\n" + b"row,1,text cell\n" * 60
F_SH = ("#!/bin/sh\nset -e\nfor f in *.txt; do\n  echo \"$f\"\n  cat \"$f\"\n"
        "done\nexit 0\n" * 8).encode()
F_HTML = (b"<html>\n<head><title>page</title></head>\n<body>\n"
          b"<p>some paragraph text in this html page here.</p>\n" * 8) \
    + b"</body>\n</html>\n"
F_AR = (AR_SENT * 6).encode("utf-8")            # 390 B > 256
F_AR16 = (AR_SENT * 6).encode("utf-16-le")      # 432 B UTF-16LE Arabic
F_A16 = ("2026-01-01 service started ok done work line\n"
         * 10).encode("utf-16-le")              # UTF-16LE ASCII log
F_256 = (b"aaaa bbbb ccccdddd " * 16)[:256]     # exact min_run boundary

_RNG = random.Random(20261113)
N_RANDOM = _RNG.randbytes(4096)
N_RANDOM2 = _RNG.randbytes(2048)
N_KEYSTREAM = b"".join(hmac.new(b"bitlocker-ish-key", i.to_bytes(8, "big"),
                                hashlib.sha256).digest()
                       for i in range(1024))   # 32 KiB CSPRNG-like noise
N_B64 = base64.b64encode(N_RANDOM)
N_B32 = base64.b32encode(N_RANDOM)
N_ZLIBRAND = zlib.compress(N_RANDOM, 9)

# diverse low-redundancy text so the compressed payloads stay >= 256 bytes
# with near-8 bit/byte entropy (what real compressed data looks like)
_DIV = random.Random(3)
_divwords = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa"]
F_DIV = "".join(_DIV.choice(_divwords) + " " + str(_DIV.randint(0, 99999))
                + "\n" for _ in range(1200)).encode()
_zbuf = io.BytesIO()
with zipfile.ZipFile(_zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("t.txt", F_DIV)
N_ZIP = _zbuf.getvalue()
N_GZIP = gzip.compress(F_DIV, 9)

N_SPARSE = b"".join(b"\x80" + N_RANDOM[i:i + 1] for i in range(2048))
N_MOSTLYNUL = b"".join(b"\x00\x00\x00" + N_RANDOM[i:i + 1]
                       for i in range(2048))

POS = [("ascii-prose", F_PROSE), ("ascii-prose-2k", F_PROSE2K),
       ("python-src", F_PY), ("markdown", F_MD), ("json", F_JSON),
       ("csv", F_CSV), ("shell", F_SH), ("html", F_HTML),
       ("utf8-arabic", F_AR), ("utf16-arabic", F_AR16),
       ("utf16-ascii-log", F_A16), ("ascii-256", F_256)]
NEG = [("random-4k", N_RANDOM), ("random-2k", N_RANDOM2),
       ("keystream-32k", N_KEYSTREAM), ("base64-of-random", N_B64),
       ("base32-of-random", N_B32), ("zlib-of-random", N_ZLIBRAND),
       ("zip-of-text", N_ZIP), ("gzip-of-text", N_GZIP),
       ("sparse-0x80-bin", N_SPARSE), ("mostly-nul-bin", N_MOSTLYNUL)]


# ---------------------------------------------------------------------------
# entropy_bits_per_byte
# ---------------------------------------------------------------------------

ent = tc.entropy_bits_per_byte
ck(ent(b"") == 0.0, "entropy: empty input is 0.0")
ck(ent(b"A" * 1000) == 0.0, "entropy: constant byte is 0.0")
ck(ent(bytes(range(256)) * 4) == 8.0,
   "entropy: uniform 256-bin histogram is exactly 8.0 bits/byte")
ck(7.8 <= ent(N_RANDOM) <= 8.05, "entropy: random 4KiB near 8 bit/byte")
ck(ent(F_PROSE) <= 4.0, "entropy: repetitive ASCII prose <= 4.0")
ck(0.0 <= ent(F_AR) <= 8.0, "entropy: Arabic UTF-8 within [0,8]")
ck(abs(ent(b"ab" * 1000) - 1.0) < 1e-12,
   "entropy: two-symbol alphabet is exactly 1 bit/byte")


# ---------------------------------------------------------------------------
# printable_ratio
# ---------------------------------------------------------------------------

pr = tc.printable_ratio
ck(pr(b"") == 0.0, "printable: empty input is 0.0")
ck(pr(b"hello\tworld\n") == 1.0, "printable: ascii + tab/newline is 1.0")
ck(pr(b"abc\x00\x00def") == 6 / 8, "printable: NUL bytes excluded")
ck(pr(b"caf\xc3\xa9 na\xc3\xafve") == 1.0,
   "printable: valid UTF-8 multibyte counts as printable")
ck(pr(F_AR) == 1.0, "printable: Arabic UTF-8 is 1.0")
ck(pr(b"\xff\xfe\x80abc") == 3 / 6,
   "printable: invalid UTF-8 falls back to ASCII-only counting")
bad = b"".join(bytes([i, 0x41]) for i in range(1, 255))
ck(0.0 <= pr(bad) <= 1.0, "printable: always within [0,1] on garbage")
ck(pr(N_RANDOM) < 0.5, "printable: random 4KiB below 0.5")
ck(0.0 <= pr(N_SPARSE) <= 1.0, "printable: never raises on binary soup")


# ---------------------------------------------------------------------------
# utf16le_printable_ratio
# ---------------------------------------------------------------------------

r16 = tc.utf16le_printable_ratio
ratio, flag = r16(F_A16)
ck(ratio == 1.0 and flag is True, "utf16: ASCII UTF-16LE log ratio 1.0 + flag")
ratio, flag = r16(F_AR16)
ck(ratio >= 0.95 and flag is True, "utf16: Arabic UTF-16LE detected")
ratio, flag = r16(("2026-01-01 x\n" * 2).encode("utf-16-le"))  # 52 B < 64
ck(flag is False, "utf16: good ratio but len < 64 keeps flag False")
ratio, flag = r16(b"A" * 128)  # pairs (0x41,0x41) are not UTF-16LE
ck(flag is False, "utf16: plain repeated ASCII bytes NOT flagged UTF-16LE")
ratio, flag = r16(F_AR)  # UTF-8 Arabic must not pose as UTF-16LE
ck(flag is False, "utf16: UTF-8 Arabic bytes are not flagged UTF-16LE")
ck(r16(b"") == (0.0, False) and r16(b"A") == (0.0, False),
   "utf16: empty/1-byte inputs return (0.0, False)")


# ---------------------------------------------------------------------------
# soft_marker_score
# ---------------------------------------------------------------------------

mk = tc.soft_marker_score
ck(mk(b"") == 0.0, "markers: empty input is 0.0")
ck(mk(b"x" * 4096) == 0.0, "markers: zero on marker-free filler")
ck(mk(N_RANDOM) < 0.6, "markers: bounded on random 4KiB (spurious '{' only)")
dense = (b"#!/bin/sh\ndef f():\nimport os\nfrom x import y\n"
         b"{ \"key\": 1 }\n<html> SELECT a\n# heading\n" * 30)
ck(mk(dense) == 1.0, "markers: dense marker text saturates at 1.0")
ck(0.0 <= mk(F_JSON) <= 1.0, "markers: JSON score within [0,1]")
ck(mk(F_JSON) > 0.3, "markers: quoted keys detected in JSON")
ck(mk(F_PROSE) < 0.05, "markers: plain prose has almost no markers")
ck(mk(F_PY) >= mk(F_PROSE), "markers: python source scores above plain prose")


# ---------------------------------------------------------------------------
# classify — positives
# ---------------------------------------------------------------------------

for name, data in POS:
    res = tc.classify(data)
    ck(res["is_text"] is True, "classify positive: %s is text" % name)

res = tc.classify(F_AR16)
ck(res["utf16"] is True and res["printable"] >= 0.95,
   "classify: UTF-16LE Arabic judged on decoded view (utf16 flag set)")
res = tc.classify(F_JSON)
ck(res["markers"] > 0.0 and res["confidence"] >= 0.6,
   "classify: JSON markers boost confidence past base")
res = tc.classify(F_AR)
ck(res["utf16"] is False and res["printable"] == 1.0,
   "classify: UTF-8 Arabic passes on the raw bytes (no utf16 needed)")


# ---------------------------------------------------------------------------
# classify — negatives
# ---------------------------------------------------------------------------

for name, data in NEG:
    res = tc.classify(data)
    ck(res["is_text"] is False, "classify negative: %s is not text" % name)

b64res = tc.classify(N_B64)
ck(b64res["printable"] >= 0.99 and b64res["entropy"] >= 5.5,
   "classify: base64 passes printable but dies on the entropy gate")
ck("entropy" in b64res["reason"], "classify: base64 reason mentions entropy")
rndres = tc.classify(N_RANDOM)
ck(rndres["entropy"] > 7.5, "classify: random blob entropy > 7.5")
ck(tc.classify(N_SPARSE)["entropy"] > 4.5,
   "classify: sparse binary entropy > 4.5")
ck(tc.classify(N_MOSTLYNUL)["printable"] < 0.70,
   "classify: mostly-NUL binary fails the printable gate")


# ---------------------------------------------------------------------------
# classify — gate details: length, markers-never-gate, borderline
# ---------------------------------------------------------------------------

ck(len(F_256) == 256, "gate: 256-byte fixture is exactly min_run")
ck(not tc.classify(F_256[:255])["is_text"]
   and "short" in tc.classify(F_256[:255])["reason"],
   "gate: 255 bytes rejected (too short)")
ck(tc.classify(F_256[:255], min_run=128)["is_text"] is True,
   "gate: min_run override accepts the same 255 bytes")
ck(tc.classify(F_256)["is_text"] is True, "gate: 256 bytes accepted")

blob = N_RANDOM2 + (
    b"#!/bin/x\ndef f():\nimport os\n{ \"key\": 1 }\n<html># SELECT from\n" * 80)
res = tc.classify(blob)
ck(res["is_text"] is False and res["markers"] > 0.5 and res["entropy"] > 4.5,
   "gate: soft markers CANNOT flip an entropy>4.5 blob to is_text")

_blrng = random.Random(7)
bl = "".join(_blrng.choice("abcdefghijklmnopqrst")
             for _ in range(2048)).encode()
res = tc.classify(bl)
ck(res["is_text"] is True and "borderline" in res["reason"]
   and 4.0 < res["entropy"] <= 4.5 and res["printable"] >= 0.95
   and res["confidence"] <= 0.55,
   "borderline: printable>=0.95 + entropy(4.0,4.5] -> text conf<=0.55")

keys = {"is_text", "confidence", "reason", "entropy", "printable",
        "utf16", "markers"}
res = tc.classify(F_PROSE)
ck(set(res) == keys, "contract: classify returns exactly the documented keys")
ck(isinstance(res["is_text"], bool) and isinstance(res["reason"], str)
   and 0.0 <= res["confidence"] <= 1.0,
   "contract: classify field types and confidence range")
ck(isinstance(res["entropy"], float) and isinstance(res["printable"], float)
   and isinstance(res["utf16"], bool) and isinstance(res["markers"], float),
   "contract: entropy/printable/utf16/markers field types")
ck(tc.classify(b"")["is_text"] is False
   and tc.classify(bytearray(b"A" * 300))["is_text"] is True
   and tc.classify(memoryview(b"A" * 300))["is_text"] is True,
   "contract: empty/bytearray/memoryview inputs never raise")


# ---------------------------------------------------------------------------
# extract_text_runs — الطبقة 2
# ---------------------------------------------------------------------------

filler = b"\x80" * 400
seg = F_PROSE[:600]
runs = tc.extract_text_runs(filler + seg + filler)
ck(runs == [(400, 1000, "ascii")],
   "runs: ASCII text inside noise found at exact positions")

ar_data = filler + F_AR + filler
runs = tc.extract_text_runs(ar_data)
ck(len(runs) == 1 and runs[0][2] == "utf-8"
   and runs[0][0] == 400 and runs[0][1] == 400 + len(F_AR),
   "runs: UTF-8 Arabic labelled utf-8 at exact positions")

u16_data = filler + F_AR16 + filler
runs = tc.extract_text_runs(u16_data)
ck(len(runs) == 1 and runs[0][2] == "utf-16-le"
   and runs[0][0] == 400 and runs[0][1] == 400 + len(F_AR16),
   "runs: UTF-16LE Arabic detected as utf-16-le")
if runs:
    s0, e0, _enc = runs[0]
    ck(u16_data[s0:e0].decode("utf-16-le").startswith(AR_SENT),
       "runs: utf-16-le span decodes back to the original Arabic")

tol = bytearray(F_PROSE[:400])
tol[100] = 0
tol[101] = 0
runs = tc.extract_text_runs(bytes(tol))
ck(runs == [(0, 400, "ascii")], "runs: two embedded NULs tolerated (bridged)")

tol3 = bytearray(F_PROSE[:600])
tol3[300] = tol3[301] = tol3[302] = 0
runs = tc.extract_text_runs(bytes(tol3))
ck(len(runs) == 2 and runs[0][1] == 300 and runs[1][0] == 303,
   "runs: three consecutive NULs break the run (tolerance is <= 2)")

_nsrng = random.Random(99)
noisy = bytearray(_nsrng.randbytes(1200))
pos = 300
noisy[pos:pos + 600] = seg
runs = tc.extract_text_runs(bytes(noisy))
ck(any(s0 <= pos and e0 >= pos + 600 for s0, e0, _ in runs),
   "runs: real text recovered from urandom noise covers the planted span")

many = (b"A" * 256 + b"\x80\x80\x80\x80") * 10001
runs = tc.extract_text_runs(many)
ck(len(runs) == tc.MAX_RUNS, "runs: list capped at MAX_RUNS (%d)" % tc.MAX_RUNS)
ck(all(s0 + 256 == e0 for s0, e0, _ in runs[:5]),
   "runs: capped list keeps well-formed spans")
ck(tc.extract_text_runs(b"\x80" * 1000) == [], "runs: pure noise yields none")


# ---------------------------------------------------------------------------
# precision / recall / F1 corpus harness
# ---------------------------------------------------------------------------

def evaluate(labeled):
    tp = fp = fn = tn = 0
    errors = []
    for name, data, want in labeled:
        res = tc.classify(data)
        if res["is_text"] and want:
            tp += 1
        elif res["is_text"] and not want:
            fp += 1
            errors.append("FP:" + name)
        elif not res["is_text"] and want:
            fn += 1
            errors.append("FN:" + name)
        else:
            tn += 1
    return tp, fp, fn, tn, errors


labeled = ([(n, d, True) for n, d in POS]
           + [(n, d, False) for n, d in NEG])
tp, fp, fn, tn, errors = evaluate(labeled)
precision = tp / (tp + fp) if tp + fp else 0.0
recall = tp / (tp + fn) if tp + fn else 0.0
f1 = (2 * precision * recall / (precision + recall)
      if precision + recall else 0.0)
print("corpus: pos=%d neg=%d TP=%d FP=%d FN=%d TN=%d"
      % (len(POS), len(NEG), tp, fp, fn, tn))
print("precision=%.3f recall=%.3f F1=%.3f" % (precision, recall, f1))
if errors:
    print("corpus errors:", errors)

ck(len(POS) >= 10 and len(NEG) >= 10,
   "corpus: at least 10 positive and 10 negative fixtures")
ck(f1 == 1.0, "corpus: F1 == 1.0 on documented fixtures (got %.3f)" % f1)
ck(fp == 0 and fn == 0, "corpus: zero false positives and zero false negatives")
ck(tp == len(POS) and tn == len(NEG),
   "corpus: every fixture landed in its correct confusion-matrix cell")

bad_pos = None
for name, data in POS:
    if tc.classify(data)["printable"] < 0.70:
        bad_pos = name
        break
ck(bad_pos is None, "sanity: all corpus positives have printable >= 0.70")
bad_neg = None
for name, data in NEG:
    res = tc.classify(data)
    if (res["entropy"] <= 4.5 and res["printable"] >= 0.70
            and len(data) >= tc.MIN_RUN):
        bad_neg = name
        break
ck(bad_neg is None,
   "sanity: every negative fails entropy>4.5 or printable<0.70 (or is short)")


# ---------------------------------------------------------------------------
# __main__ demo (the module's only file I/O), kept in a temp dir
# ---------------------------------------------------------------------------

tmp = tempfile.mkdtemp(prefix="tc_demo_")
try:
    path = os.path.join(tmp, "sample.txt")
    with open(path, "wb") as fh:
        fh.write(F_PROSE)
    ck(tc.main([path]) == 0, "main: demo returns 0 on a text file")
    ck(tc.main([]) == 2, "main: usage path returns exit code 2")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------

ck("classify" in tc.__all__ and "extract_text_runs" in tc.__all__
   and "utf16le_printable_ratio" in tc.__all__
   and "soft_marker_score" in tc.__all__
   and "entropy_bits_per_byte" in tc.__all__
   and "printable_ratio" in tc.__all__,
   "surface: __all__ exports the full public API")
ck("الطبقة 2" in (tc.__doc__ or "") and "الطبقة 3" in (tc.__doc__ or ""),
   "surface: module docstring cites guide الطبقة 2 / الطبقة 3")


# ---------------------------------------------------------------------------

print()
if FAILS:
    print("%d FAILURE(S):" % len(FAILS))
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("ALL TEXT_CLASSIFY PHASE 3 TESTS PASSED")
sys.exit(0)
