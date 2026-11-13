#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_ngram.py - Layer 4 of the text-recovery pipeline: the n-gram filter.

Why this exists
---------------
The hard gate (entropy <= 4.0 bit/byte, printable ratio >= 0.70, length >= 256)
recovers recall but not precision: a valid ZIP with a long run of identical
comment bytes scores entropy 1.08 and printable 0.81, so it walks straight
through the gate and is carved as "text". QA's `qa_ngram_negatives.py` fixture
reproduces exactly that false positive.

This module adds the precision layer *after* the gate. It never lowers recall by
itself - it only decides whether an already-gated run is *language* or not.

The rule (measured, not guessed)
--------------------------------
Three independent signals, all of which must agree:

1. decode     - strict UTF-8, else a UTF-16LE view when the buffer is flagged
                as UTF-16LE, else reject. Compressed bytes are not valid UTF-8,
                so zlib/gzip/Shake-256 fall out here without any statistics.
2. runfrac    - the longest run of one identical character, as a FRACTION of the
                buffer. A quarter or more of the file being one byte is stuffing
                (ZIP comment padding, zero fill). The fraction, not the count:
                real Python source legitimately contains 75-char comment rules.
3. space      - the fraction of whitespace. Real prose in any script has word
                gaps; ZIP headers and hex dumps effectively do not.
4. trigram    - mean log-probability per character under an add-k trigram model.
                A run whose every trigram is unseen sits at the model floor
                (-4.42 measured) and is not language.

Calibration (scratch/calib_ngram4.py, 15 cases, 0 misclassified)
---------------------------------------------------------------
                 signal    positives (8 real repo files)   negatives
  space          min       0.234                         0.200 (hex dump)
  runfrac        max       0.009                         0.002
  trigram floor  min       -3.92                         -4.42

Thresholds are committed constants below and asserted by test_text_ngram.py, so
a change to any of them fails CI instead of silently shifting behaviour.

What this module deliberately does NOT do
-----------------------------------------
* It does not carve, read devices or write files - it judges bytes only.
* It does not implement full n-gram perplexity ranking (the documented v7 gap):
  the score is a binary pass/fail per run, not a ranked confidence.
* It never raises on bytes-like input; a failure returns a dict with
  is_text=False and the reason.

Environment: Python standard library only, no network, no third-party models.
"""

import math
import os
from collections import Counter

VERSION = "7.0.3"

__all__ = [
    "VERSION",
    "NUL_MAX", "RUNFRAC_MAX", "SPACE_MIN", "TRILP_FLOOR", "FLOOR_MARGIN",
    "MODEL_K", "utf16_ratio", "decode_view", "trigram_logprob",
    "ngram_filter", "build_model", "default_model", "main",
]

# --- calibrated thresholds (measured; asserted by test_text_ngram.py) --------
NUL_MAX = 0.30        # >=30% NUL bytes -> padding/empty UTF-16LE, reject early
RUNFRAC_MAX = 0.25    # longest single-char run >= 25% of buffer -> stuffing
SPACE_MIN = 0.05      # real text always has word gaps
TRILP_FLOOR = -4.42   # measured unseen-trigram floor for MODEL_K / this corpus
FLOOR_MARGIN = 0.02   # a hair above the floor, to absorb float noise
MODEL_K = 0.1         # add-k smoothing constant

# Minimum bytes before a UTF-16LE verdict is trusted (matches text_classify).
UTF16_MIN_BYTES = 64
UTF16_RATIO_MIN = 0.90

# High bytes that, paired with any low byte, form plausible UTF-16LE code units
# for the scripts the tool targets. Mirrors text_classify._UTF16_HI_ALLOWED so
# both layers agree on what "looks like UTF-16LE" means.
_UTF16_HI_ALLOWED = frozenset(
    list(range(0x00, 0x20)) + list(range(0xAC, 0xD8)) + list(range(0xF9, 0xFC))
)


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------

def utf16_ratio(data):
    """(ratio, is_utf16le) for a buffer.

    `ratio` is the fraction of 16-bit code units that are printable under the
    target scripts; `is_utf16le` is True when that fraction clears
    UTF16_RATIO_MIN on at least UTF16_MIN_BYTES of data. Short buffers are never
    flagged, because a 10-byte "   \\n\\n" would otherwise look like UTF-16LE.
    """
    data = bytes(data)
    n = len(data) - (len(data) % 2)
    if n < UTF16_MIN_BYTES:
        return 0.0, False
    pairs = n // 2
    good = 0
    for i in range(0, n, 2):
        lo, hi = data[i], data[i + 1]
        if hi == 0:
            if 0x20 <= lo <= 0x7E or lo in (0x09, 0x0A, 0x0D):
                good += 1
        elif hi in _UTF16_HI_ALLOWED:
            try:
                if chr((hi << 8) | lo).isprintable():
                    good += 1
            except ValueError:
                pass
    ratio = good / pairs if pairs else 0.0
    return ratio, ratio >= UTF16_RATIO_MIN


def decode_view(data):
    """Decode a buffer to a text view.

    Returns (text, encoding, ok). `ok` is False when the buffer is not valid
    UTF-8 and not a plausible UTF-16LE stream - which is the first and cheapest
    rejection for compressed data. Never raises.
    """
    data = bytes(data)
    _ratio, is16 = utf16_ratio(data)
    if is16:
        even = len(data) - (len(data) % 2)
        try:
            return data[:even].decode("utf-16-le"), "utf-16le", True
        except (UnicodeDecodeError, ValueError):
            text = data[:even].decode("utf-16-le", errors="replace")
            return text, "utf-16le", True
    try:
        return data.decode("utf-8"), "utf-8", True
    except (UnicodeDecodeError, ValueError):
        return "", "none", False


# ---------------------------------------------------------------------------
# the trigram model
# ---------------------------------------------------------------------------

def _norm(text):
    """Lowercase, whitespace removed - the unit the model is scored over."""
    return "".join(ch for ch in text.lower() if not ch.isspace())


def build_model(text, k=MODEL_K):
    """Build an add-k trigram model over `text`.

    Returns a dict with the counts and the smoothing constant. Callers may pass
    their own corpus; `default_model()` is the shipped one.
    """
    s = _norm(text)
    return {
        "bi": Counter(zip(s, s[1:])),
        "tri": Counter(zip(s, s[1:], s[2:])),
        "k": float(k),
        "V": max(1, len(set(s))),
        "chars": len(s),
    }


def trigram_logprob(model, text):
    """Mean log-probability per trigram under `model` (natural log).

    Returns TRILP_FLOOR when there is nothing to score. A buffer whose trigrams
    were never seen lands exactly on the floor, which is what makes the floor a
    usable "not language" test rather than an arbitrary cutoff.
    """
    s = _norm(text)
    if len(s) < 3:
        return TRILP_FLOOR
    k = model["k"]
    V = model["V"]
    bi = model["bi"]
    tri = model["tri"]
    total = 0.0
    n = 0
    for i in range(len(s) - 2):
        a, b, c = s[i], s[i + 1], s[i + 2]
        total += math.log((tri[(a, b, c)] + k) / (bi[(a, b)] + k * V))
        n += 1
    return total / n if n else TRILP_FLOOR


_DEFAULT = None


def default_model():
    """The shipped model, built lazily from the corpus file next to this module.

    The corpus is a committed text artifact (English + Arabic prose and code),
    deliberately disjoint from the test fixtures so the calibration is honest.
    """
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "text_ngram_corpus.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            corpus = fh.read()
    except OSError:
        corpus = ""
    _DEFAULT = build_model(corpus)
    return _DEFAULT


# ---------------------------------------------------------------------------
# the filter
# ---------------------------------------------------------------------------

def ngram_filter(data, model=None, min_run=256):
    """Decide whether an already-gated run is language or noise.

    This is Layer 4: it runs AFTER the hard gate and only ever rejects. It never
    promotes a run that the gate refused.

    Returns a dict:
        is_text    bool
        confidence 0.0-1.0, higher when more signals agree
        reason     str, always populated
        decode     'utf-8' | 'utf-16le' | 'none' | 'reject-nul' | 'no-utf8' | 'empty'
        runfrac    longest single-char run as a fraction of the buffer
        space      whitespace fraction
        trigram    mean log-prob per trigram

    Never raises on bytes-like input.
    """
    try:
        data = bytes(data)
    except (TypeError, ValueError):
        return {"is_text": False, "confidence": 0.0,
                "reason": "input is not bytes-like", "decode": "none",
                "runfrac": 0.0, "space": 0.0, "trigram": TRILP_FLOOR}

    result = {"is_text": False, "confidence": 0.0, "reason": "",
              "decode": "none", "runfrac": 0.0, "space": 0.0,
              "trigram": TRILP_FLOOR}
    n = len(data)
    if n < min_run:
        result["reason"] = "too short: %d bytes < min_run %d" % (n, min_run)
        return result

    # 1) decode FIRST. Compressed bytes fail here without any statistics, and a
    #    real UTF-16LE text file is 50% NUL by construction - so the NUL check
    #    below must never run on a buffer that is legitimately UTF-16LE.
    _ratio, is16 = utf16_ratio(data)
    text, enc, ok = decode_view(data)
    if not ok:
        result["decode"] = "no-utf8"
        result["reason"] = ("not valid UTF-8 and not a plausible UTF-16LE "
                            "stream: compressed or binary data")
        return result
    result["decode"] = enc
    if not text:
        result["decode"] = "empty"
        result["reason"] = "decoded to an empty string"
        return result

    # 2) cheap structural reject: padding and empty UTF-16LE. Only for buffers
    #    that are NOT UTF-16LE, where a high NUL share means zero fill.
    null_frac = data.count(0) / float(n)
    if not is16 and null_frac >= NUL_MAX:
        result["decode"] = "reject-nul"
        result["reason"] = ("NUL bytes %.0f%% >= %.0f%%: padding or empty "
                            "UTF-16LE, not text" % (null_frac * 100, NUL_MAX * 100))
        return result

    # 3) run fraction - stuffing, as a fraction rather than an absolute count
    longest = 0
    cur = 0
    prev = ""
    for ch in text:
        cur = cur + 1 if ch == prev else 1
        prev = ch
        if cur > longest:
            longest = cur
    runfrac = longest / float(len(text))
    result["runfrac"] = runfrac

    nonspace = [ch for ch in text if not ch.isspace()]
    if not nonspace:
        result["reason"] = "no non-whitespace characters"
        return result
    space = 1.0 - (len(nonspace) / float(len(text)))
    result["space"] = space

    lp = trigram_logprob(model if model is not None else default_model(), text)
    result["trigram"] = lp

    # 4) the rule, in order: structure first (cheap, decisive), then language
    if runfrac >= RUNFRAC_MAX:
        result["reason"] = ("longest single-character run is %.0f%% of the "
                            "buffer (>= %.0f%%): stuffing, not text"
                            % (runfrac * 100, RUNFRAC_MAX * 100))
        return result
    if space < SPACE_MIN:
        result["reason"] = ("whitespace %.1f%% < %.0f%%: no word gaps, not "
                            "prose" % (space * 100, SPACE_MIN * 100))
        return result
    if lp <= TRILP_FLOOR + FLOOR_MARGIN:
        result["reason"] = ("trigram log-prob %.2f is at the model floor "
                            "(%.2f): no known trigram, not language"
                            % (lp, TRILP_FLOOR))
        return result

    # pass - confidence rises with how far each signal clears its threshold
    c = 0.5
    c += 0.2 * min(1.0, (space - SPACE_MIN) / 0.20)
    c += 0.2 * min(1.0, (lp - (TRILP_FLOOR + FLOOR_MARGIN)) / 1.0)
    c += 0.1 * (1.0 - min(1.0, runfrac / RUNFRAC_MAX))
    result["is_text"] = True
    result["confidence"] = round(min(0.99, c), 3)
    result["reason"] = ("language: space %.1f%%, longest run %.1f%%, trigram "
                        "%.2f above floor %.2f (%s)"
                        % (space * 100, runfrac * 100, lp, TRILP_FLOOR, enc))
    return result


# ---------------------------------------------------------------------------
# command line - judge one file, read-only
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(argv if argv is not None else __import__("sys").argv[1:])
    if not argv:
        print("Usage: python text_ngram.py <file> [file ...]")
        print("Read-only: judges whether each file's bytes look like language.")
        return 2
    model = default_model()
    worst = 0
    for path in argv:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print("%s: cannot read (%s)" % (path, exc))
            worst = 1
            continue
        r = ngram_filter(data, model)
        print("%-40s %-5s conf=%.2f  %s"
              % (os.path.basename(path), "TEXT" if r["is_text"] else "noise",
                 r["confidence"], r["reason"]))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())