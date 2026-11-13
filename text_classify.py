#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_classify.py - Layer 2 + Layer 3 of the text recovery pipeline
(الطبقة 2: Strings extraction / الطبقة 3: N-Gram classification).

Per v7/TextRecoveryGuide.md:

  الطبقة 2 — Strings extraction
      Carve maximal printable runs >= 256 bytes (configurable min_run) in
      ASCII, UTF-8 and UTF-16LE. No file name is required — raw bytes only.

  الطبقة 3 — N-Gram / text classification
      Distinguish real text from random data with two hard metrics:
        * entropy  <= 4 bit/byte
        * printable ratio >= 70%
      Soft markers (shebang, '# ', 'def ', 'import ', '{', '"key":', '<html',
      'from ', 'SELECT ') only raise confidence — they NEVER qualify a run on
      their own (guide: الرموو الناعمة soft hits وليست عتبة صارمة).

Negative fixtures from the guide (BitLocker-like noise, ZIP/compressed bytes,
random bytes) must stay non-text; the entropy gate is what rejects
high-printable-but-incompressible payloads such as base64 of random data.

This module is pure functions over bytes: no disk I/O happens at import or
inside any classifier call. The only file open lives under __main__.

Public API
----------
    entropy_bits_per_byte(data)     -> float in [0, 8]
    printable_ratio(data)           -> float in [0, 1]
    utf16le_printable_ratio(data)   -> (ratio, is_utf16)
    soft_marker_score(data)         -> float in [0, 1]
    classify(data, min_run=256)     -> decision dict
    extract_text_runs(data, min_run=256)
                                    -> [(start, end, encoding), ...]
    main(argv)                      -> exit code (CLI demo, reads one file)

Entropy convention: Shannon entropy over the 256-bin byte histogram, base-2
logarithm, i.e. bits per byte:  H = -sum_i p_i * log2(p_i),  p_i = n_i/N.
Uniform random bytes score ~8.0; ASCII text typically 3.0-4.5.

Known limits (documented, intentional):
  * UTF-16LE detection aligns code units at even offsets from the buffer
    start; an odd-shifted UTF-16 stream is treated as raw bytes.
  * Classification of an escaped/encoded high-entropy payload (base64, hex)
    relies solely on the entropy gate — no n-gram language model yet.
"""

import math
import re
import sys

VERSION = "7.0.1.2"

__all__ = [
    "VERSION",
    "MIN_RUN", "ENTROPY_MAX", "ENTROPY_BORDERLINE_MAX",
    "PRINTABLE_MIN", "PRINTABLE_BORDERLINE_MIN",
    "UTF16_RATIO_MIN", "UTF16_LEN_MIN", "MAX_RUNS", "RUN_GAP_TOLERANCE",
    "MARKER_SATURATION_PER_KIB",
    "entropy_bits_per_byte", "printable_ratio", "utf16le_printable_ratio",
    "soft_marker_score", "classify", "extract_text_runs", "main",
]

# --- guide constants --------------------------------------------------------
MIN_RUN = 256                    # الطبقة 2: carved runs >= 256 bytes
ENTROPY_MAX = 4.0                # الطبقة 3: entropy <= 4 bit/byte
ENTROPY_BORDERLINE_MAX = 4.5     # borderline lift ceiling (high printable only)
PRINTABLE_MIN = 0.70             # الطبقة 3: printable ratio >= 70%
PRINTABLE_BORDERLINE_MIN = 0.95  # borderline needs near-perfect printable
UTF16_RATIO_MIN = 0.90           # utf16le_printable_ratio is_utf16 threshold
UTF16_LEN_MIN = 64               # minimum bytes before trusting utf16 flag
MAX_RUNS = 10000                 # cap on extract_text_runs() output
RUN_GAP_TOLERANCE = 2            # consecutive bad bytes tolerated inside a run
MARKER_SATURATION_PER_KIB = 10.0  # marker hits per KiB that saturate score

_WS = frozenset((0x09, 0x0A, 0x0D))   # \t \n \r also count as printable
_ASCII_LO = 0x20
_ASCII_HI = 0x7E

# Odd (high) bytes that, paired with any even (low) byte, form code units in
# "foreign-script" blocks: 0x00-0x1F covers Latin Extended, IPA, Greek (03),
# Cyrillic (04), Hebrew (05), Arabic (06-07), Indic (09-0D), Thai/Lao (0E-0F),
# Georgian/Tibetan, Khmer, Tai and the phonetic/Latin/Greek extensions;
# 0xAC-0xD7 is Hangul syllables; 0xF9-0xFB is CJK Compatibility. Every value
# here is outside the ASCII printable band except 0x00-0x1F, so plain ASCII
# text (odd bytes = letters/digits/space, 0x20-0x7E) scores ratio 0.0 and can
# never be mis-flagged as UTF-16LE. CJK Unified Ideographs (high 0x4E-0x9F)
# are deliberately EXCLUDED: that band overlaps ASCII 'N'-'Z' and 'a'-'z', so
# accepting it would flag ordinary English prose as UTF-16LE. UTF-16LE CJK is
# therefore a documented limit of this layer.
_UTF16_HI_ALLOWED = frozenset(
    list(range(0x00, 0x20)) + list(range(0xAC, 0xD8)) + list(range(0xF9, 0xFC))
)

# --- soft markers (الرموز الناعمة) — confidence only, never a gate ---------
_MARKER_RES = (
    re.compile(rb"#!/"),
    re.compile(rb"\bdef "),
    re.compile(rb"\bimport "),
    re.compile(rb"(?m)^# "),
    re.compile(rb"\{"),
    re.compile(rb'"[A-Za-z_][A-Za-z0-9_]*"\s*:'),
    re.compile(rb"(?i)<html"),
    re.compile(rb"\bfrom "),
    re.compile(rb"(?i)SELECT "),
)


# ---------------------------------------------------------------------------
# small internal helpers
# ---------------------------------------------------------------------------

def _byte_printable(b):
    return (_ASCII_LO <= b <= _ASCII_HI) or (b in _WS)


def _utf8_seq_len(data, i):
    """Length of a valid UTF-8 sequence starting at data[i], else 0.

    Rejects continuation bytes, overlong leads (0x80-0xC1), invalid leads
    (0xF5-0xFF) and out-of-range continuation bytes.
    """
    n = len(data)
    b = data[i]
    if 0xC2 <= b <= 0xDF:
        need = 1
    elif 0xE0 <= b <= 0xEF:
        need = 2
    elif 0xF0 <= b <= 0xF4:
        need = 3
    else:
        return 0
    if i + need >= n:
        return 0
    for k in range(1, need + 1):
        c = data[i + k]
        if not (0x80 <= c <= 0xBF):
            return 0
    # reject surrogates (ED A0-BF) and out-of-range leads' overlongs
    if b == 0xE0 and data[i + 1] < 0xA0:
        return 0
    if b == 0xED and data[i + 1] >= 0xA0:
        return 0
    if b == 0xF0 and data[i + 1] < 0x90:
        return 0
    if b == 0xF4 and data[i + 1] > 0x8F:
        return 0
    return need + 1


def _utf16_pair_printable(lo, hi):
    """One UTF-16LE code unit: ASCII-with-NUL-high, or a printable code
    point in a foreign-script block (hi byte in _UTF16_HI_ALLOWED \\ {0})."""
    if hi == 0:
        return _byte_printable(lo)
    if hi not in _UTF16_HI_ALLOWED:
        return False
    return chr((hi << 8) | lo).isprintable()


# ---------------------------------------------------------------------------
# 1) entropy
# ---------------------------------------------------------------------------

def entropy_bits_per_byte(data):
    """Shannon entropy of a byte string, base 2 → bits per byte in [0, 8].

    Computed over the full 256-bin byte histogram:
        H = -sum_i (n_i / N) * log2(n_i / N)
    Empty input returns 0.0 (nothing to measure).
    """
    n = len(data)
    if n == 0:
        return 0.0
    hist = [0] * 256
    for b in data:
        hist[b] += 1
    ent = 0.0
    for count in hist:
        if count:
            p = count / n
            ent -= p * math.log2(p)
    return ent


# ---------------------------------------------------------------------------
# 2) printable ratio (ASCII / UTF-8 tolerant)
# ---------------------------------------------------------------------------

def printable_ratio(data):
    """Fraction of bytes considered printable, in [0, 1].

    Printable = 0x20-0x7E plus \\t \\n \\r, plus every byte belonging to a
    *valid* UTF-8 multibyte sequence (so Arabic/CJK UTF-8 text scores 1.0).
    Decode-tolerant: if the buffer fails a strict UTF-8 decode, the function
    falls back to ASCII-only counting. It never raises.
    """
    n = len(data)
    if n == 0:
        return 0.0
    try:
        data.decode("utf-8")
        whole_valid = True
    except (UnicodeDecodeError, ValueError):
        whole_valid = False
    if whole_valid:
        # Every byte is either an ASCII char or part of a valid multibyte
        # sequence. Count non-ASCII sequences as printable wholesale; reject
        # ASCII controls other than \t \n \r.
        good = 0
        i = 0
        while i < n:
            b = data[i]
            if b < 0x80:
                if _byte_printable(b):
                    good += 1
                i += 1
            else:
                step = _utf8_seq_len(data, i)
                if step:
                    good += step
                    i += step
                else:
                    i += 1  # unreachable for valid UTF-8, kept defensive
        return good / n
    good = sum(1 for b in data if _byte_printable(b))
    return good / n


# ---------------------------------------------------------------------------
# 3) UTF-16LE detection
# ---------------------------------------------------------------------------

def utf16le_printable_ratio(data):
    """Return (ratio, is_utf16) for a buffer read as UTF-16LE code units.

    A pair (even byte lo, odd byte hi) counts as printable when hi == 0 and
    lo is printable ASCII/whitespace (the guide's core rule), or when the
    code point lands in a printable foreign-script block whose high byte is
    a control value (Arabic 0x06-0x07, Hebrew, Cyrillic, Indic, Hangul, ...)
    — this is what makes UTF-16LE Arabic/CJK logs detectable while plain
    ASCII prose (high bytes = letters) is not.

    is_utf16 is True when ratio >= UTF16_RATIO_MIN and len(data) >=
    UTF16_LEN_MIN. Trailing odd byte is ignored. Never raises.
    """
    npairs = len(data) // 2
    if npairs == 0:
        return 0.0, False
    good = 0
    for i in range(0, npairs * 2, 2):
        if _utf16_pair_printable(data[i], data[i + 1]):
            good += 1
    ratio = good / npairs
    is_utf16 = ratio >= UTF16_RATIO_MIN and len(data) >= UTF16_LEN_MIN
    return ratio, is_utf16


# ---------------------------------------------------------------------------
# 4) soft markers
# ---------------------------------------------------------------------------

def soft_marker_score(data):
    """Density of soft text markers, saturating at 1.0.

    Counts hits of: '#!/', 'def ', 'import ', '# ' at line start, '{',
    '"key":'-style quoted keys, '<html', 'from ', 'SELECT ' — normalized per
    KiB of input and divided by MARKER_SATURATION_PER_KIB hits. Per the
    guide these are soft hits that raise confidence only; they never act as
    a qualification gate.
    """
    n = len(data)
    if n == 0:
        return 0.0
    hits = 0
    for rx in _MARKER_RES:
        hits += len(rx.findall(data))
    per_kib = hits / (n / 1024.0)
    return min(1.0, per_kib / MARKER_SATURATION_PER_KIB)


# ---------------------------------------------------------------------------
# 5) classify — الطبقة 3 decision
# ---------------------------------------------------------------------------

def classify(data, min_run=MIN_RUN):
    """Classify a byte run as text or non-text per the guide's الطبقة 3.

    Gate (hard): entropy <= 4.0 bit/byte AND printable_ratio >= 0.70 AND
    len(data) >= min_run. Passing the gate gives base confidence 0.6 scaled
    linearly to 0.85 by printable quality (0.70 → 0.6, 1.0 → 0.85), plus a
    soft-marker boost of up to +0.10 (cap 0.95). Soft markers can NEVER
    cross the gate.

    Borderline exception: printable >= 0.95 AND 4.0 < entropy <= 4.5 lifts a
    run to is_text with confidence <= 0.55, flagged with 'borderline' in
    the reason (near-perfect printability at slightly-above-gate entropy).

    UTF-16LE path: when utf16le_printable_ratio flags the buffer, printable,
    entropy and markers are recomputed on the decoded → UTF-8 view, so
    UTF-16LE Arabic/ASCII text is judged on its decoded bytes.

    Returns dict: is_text (bool), confidence (0-1), reason (str),
    entropy, printable, utf16 (bool), markers (float). Never raises on
    bytes-like input.
    """
    data = bytes(data)
    n = len(data)
    ratio16, is16 = utf16le_printable_ratio(data)
    view = data
    if is16:
        even = n - (n % 2)
        try:
            view = data[:even].decode("utf-16-le").encode("utf-8")
        except (UnicodeDecodeError, ValueError):
            view = data[:even].decode("utf-16-le", errors="replace").encode(
                "utf-8", errors="replace")
    entropy = entropy_bits_per_byte(view)
    printable = printable_ratio(view)
    markers = soft_marker_score(view)

    result = {
        "is_text": False,
        "confidence": 0.0,
        "reason": "",
        "entropy": entropy,
        "printable": printable,
        "utf16": is16,
        "markers": markers,
    }

    if n < min_run:
        result["reason"] = (
            "too short: %d bytes < min_run %d" % (n, min_run))
        return result

    if entropy <= ENTROPY_MAX and printable >= PRINTABLE_MIN:
        conf = 0.6 + 0.25 * ((printable - PRINTABLE_MIN)
                             / (1.0 - PRINTABLE_MIN))
        conf += 0.10 * markers
        result["is_text"] = True
        result["confidence"] = min(0.95, conf)
        result["reason"] = (
            "gate passed: entropy %.3f <= %.1f, printable %.3f >= %.2f"
            % (entropy, ENTROPY_MAX, printable, PRINTABLE_MIN))
        if is16:
            result["reason"] += " (utf16le decoded view)"
        return result

    if (printable >= PRINTABLE_BORDERLINE_MIN
            and ENTROPY_MAX < entropy <= ENTROPY_BORDERLINE_MAX):
        result["is_text"] = True
        result["confidence"] = min(0.55, 0.40 + 0.15 * markers)
        result["reason"] = (
            "borderline: printable %.3f >= %.2f lifts entropy %.3f in "
            "(%.1f, %.1f]" % (printable, PRINTABLE_BORDERLINE_MIN,
                              entropy, ENTROPY_MAX,
                              ENTROPY_BORDERLINE_MAX))
        return result

    if entropy > ENTROPY_BORDERLINE_MAX:
        result["reason"] = (
            "entropy too high: %.3f > %.1f (not text-like)"
            % (entropy, ENTROPY_BORDERLINE_MAX))
    elif printable < PRINTABLE_MIN:
        result["reason"] = (
            "printable ratio too low: %.3f < %.2f"
            % (printable, PRINTABLE_MIN))
    else:
        result["reason"] = (
            "gate failed: entropy %.3f, printable %.3f" % (entropy, printable))
    return result


# ---------------------------------------------------------------------------
# 6) extract_text_runs — الطبقة 2 carving
# ---------------------------------------------------------------------------

def _scan_runs(data, min_run, step_fn):
    """Maximal runs where step_fn(data, i) > 0, tolerating at most
    RUN_GAP_TOLERANCE consecutive bad positions inside a run (gaps are
    included in the emitted span; a longer break ends the run before it)."""
    n = len(data)
    runs = []
    start = None
    last_good = -1
    bad = 0
    i = 0
    while i < n:
        step = step_fn(data, i)
        if step:
            if start is None:
                start = i
            last_good = i + step - 1
            bad = 0
            i += step
        else:
            if start is not None:
                bad += 1
                if bad > RUN_GAP_TOLERANCE:
                    end = last_good + 1
                    if end - start >= min_run:
                        runs.append((start, end))
                    start = None
                    last_good = -1
                    bad = 0
            i += 1
    if start is not None:
        end = last_good + 1
        if end - start >= min_run:
            runs.append((start, end))
    return runs


def _ascii_step(data, i):
    b = data[i]
    if _byte_printable(b):
        return 1
    return _utf8_seq_len(data, i)


def _utf16_step(data, i):
    if i + 1 >= len(data):
        return 0
    if _utf16_pair_printable(data[i], data[i + 1]):
        return 2
    return 0


def _covered_fraction(start, end, spans):
    covered = 0
    for s0, s1 in spans:
        lo = max(start, s0)
        hi = min(end, s1)
        if hi > lo:
            covered += hi - lo
    return covered / (end - start)


def extract_text_runs(data, min_run=MIN_RUN):
    """Carve maximal printable runs >= min_run bytes (الطبقة 2).

    Returns [(start, end, encoding), ...] sorted by start, end exclusive,
    capped at MAX_RUNS entries. Encodings: 'ascii', 'utf-8' or 'utf-16-le'.

    * ASCII/UTF-8 pass: printable bytes plus valid UTF-8 sequences; at most
      RUN_GAP_TOLERANCE (2) consecutive control/invalid bytes are tolerated
      inside a run (they are bridged and included in the span).
    * UTF-16LE pass: code-unit pairs (even offset aligned), so Arabic/CJK
      UTF-16LE counts as printable even though its low/high bytes look like
      controls in the ASCII pass. A candidate span is only kept when its own
      pair ratio reaches UTF16_RATIO_MIN — otherwise dense ASCII prose (whose
      odd bytes are random letters) would fake a UTF-16LE run.
    * ASCII runs that overlap a UTF-16LE run by more than half of their
      length are dropped — the UTF-16LE label is the better one (it is also
      what lets an embedded ASCII-in-UTF-16 log be reported as 'utf-16-le').
    """
    data = bytes(data)
    u16 = []
    for s, e in _scan_runs(data, min_run, _utf16_step):
        ratio, _flag = utf16le_printable_ratio(data[s:e])
        if ratio >= UTF16_RATIO_MIN:
            u16.append((s, e))
    ascii_runs = _scan_runs(data, min_run, _ascii_step)
    out = []
    for s, e in ascii_runs:
        if _covered_fraction(s, e, u16) > 0.5:
            continue
        seg = data[s:e]
        enc = "ascii" if all(b < 0x80 for b in seg) else "utf-8"
        out.append((s, e, enc))
    for s, e in u16:
        out.append((s, e, "utf-16-le"))
    out.sort(key=lambda t: (t[0], t[1]))
    return out[:MAX_RUNS]


# ---------------------------------------------------------------------------
# 7) CLI demo — the ONLY file I/O in this module, __main__-guarded
# ---------------------------------------------------------------------------

def _ensure_utf8_streams():
    """Never crash on Arabic output under a legacy console codepage."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _ensure_utf8_streams()
    """Read one file path and print classify() + run stats. Demo only."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python text_classify.py <file>")
        return 2
    with open(argv[0], "rb") as fh:      # file open happens only here
        data = fh.read()
    res = classify(data)
    runs = extract_text_runs(data)
    print("%s: %d bytes" % (argv[0], len(data)))
    for key in ("is_text", "confidence", "entropy", "printable",
                "utf16", "markers"):
        print("  %-11s %s" % (key + ":", res[key]))
    print("  reason:     %s" % res["reason"])
    print("  runs:       %d" % len(runs))
    for s, e, enc in runs[:10]:
        print("    [%d, %d) %s" % (s, e, enc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
