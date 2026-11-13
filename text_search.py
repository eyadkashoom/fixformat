#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_search.py - Layer 5: n-gram perplexity search over raw bytes (v7.0.3).

What this adds over text_ngram.py
---------------------------------
`text_ngram.ngram_filter()` answers one question about one buffer: "is this
language?" - a yes/no gate. This module answers the search question: "where in
this binary blob does language live, and how strong is the evidence there?"

That is what a user actually wants when they point the tool at a corrupted or
unknown binary and ask "is there any readable text in here, and where". A gate
cannot answer it; it can only accept or reject a window it was handed.

How it works
------------
1. Slide a window over the bytes at a fixed step.
2. Decode the window (strict UTF-8, else a UTF-16LE view). Bytes that do not
   decode are skipped outright - this is what makes the search cheap AND what
   gives the precision: random bytes essentially never decode to 64+ characters
   of valid UTF-8, so they never reach the scoring stage.
3. Score the decoded text with the shipped trigram model (mean log-probability
   per character, from text_ngram).
4. Keep windows whose score clears DEFAULT_THRESHOLD, merge the overlapping ones
   into regions, and report one row per region: where it starts, how long it
   runs, the best score in it, and a short preview.

Calibration (scratch/calib_search.py, QA fixture image)
------------------------------------------------------
    plants (planted text at known offsets)   -3.88 .. -3.19
    pure random control                      no decodable window at all

So the threshold is set between the trigram floor (-4.42, never language) and
the weakest real plant (-3.88), not guessed from a round number. The decode
step alone removes random bytes; the trigram score separates real prose from
short accidental decodes.

Why decode-first is not optional
--------------------------------
UTF-16LE text is 50% NUL bytes. Scoring raw bytes would score a real UTF-16LE
document as padding. Decoding first, exactly as in text_ngram, is what lets one
search find ASCII, Arabic UTF-8 and UTF-16LE text in the same pass.

Environment: Python standard library only, no network, read-only on the input.
"""

import os
import sys

import text_ngram

VERSION = "7.0.3"

__all__ = [
    "VERSION",
    "DEFAULT_THRESHOLD", "DEFAULT_WINDOW", "DEFAULT_STEP", "DEFAULT_MIN_RUN",
    "MIN_REGION_BYTES", "PREVIEW_CHARS",
    "score_window", "search", "search_file", "main",
]

# --- calibrated defaults ----------------------------------------------------
DEFAULT_THRESHOLD = -4.20   # between the trigram floor (-4.42) and the weakest
                            # real plant measured (-3.88)
DEFAULT_WINDOW = 512        # bytes decoded per scoring window
DEFAULT_STEP = 64           # slide step; 8x overlap so a short plant is never
                            # stepped over
DEFAULT_MIN_RUN = 256       # a region shorter than this is not worth reporting
MIN_REGION_BYTES = 64       # shortest decodable text a window may contribute
PREVIEW_CHARS = 120         # characters of decoded text shown per hit


def _decode_candidates(data):
    """Every plausible decoding of `data`: (encoding, text) pairs.

    Both encodings are attempted explicitly rather than trusting a single
    heuristic verdict. This matters at region edges: a window holding 352 bytes
    of ASCII followed by random bytes can be *forced* into a UTF-16LE reading
    that decodes without error but is garbage. Scoring only that reading would
    reject a real ASCII region. Trying both and keeping the better score is
    what makes the search robust at boundaries.
    """
    out = []
    try:
        out.append(("utf-8", data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        pass
    even = len(data) - (len(data) % 2)
    if even >= 2:
        try:
            out.append(("utf-16le", data[:even].decode("utf-16-le")))
        except (UnicodeDecodeError, ValueError):
            pass
    return out


def score_window(window, model=None, min_chars=MIN_REGION_BYTES):
    """Score one window: (score, encoding, text) or (None, 'none', '').

    Returns None for the score when the window has no decodable prefix of at
    least `min_chars` non-space characters - which is the common case for random
    and compressed bytes, and the reason the search is fast.
    """
    model = text_ngram.default_model() if model is None else model
    data = bytes(window)
    best = (None, "none", "")
    end = len(data)
    # Trim from the end until a decodable prefix appears. A window that straddles
    # the end of a text region is normal; its head is still real text.
    #
    # The best score is tracked across ALL lengths, not returned at the first
    # length that decodes. A long window whose tail is random can still decode as
    # UTF-16LE (any even length does) and produce garbage that scores at the
    # floor; stopping there would discard the real ASCII text a little shorter
    # down. Taking the maximum over every length keeps the genuine reading.
    while end >= min_chars:
        for enc, text in _decode_candidates(data[:end]):
            nonspace = sum(1 for ch in text if not ch.isspace())
            if nonspace < min_chars:
                continue
            score = text_ngram.trigram_logprob(model, text)
            if best[0] is None or score > best[0]:
                best = (score, enc, text)
        end -= 32
    return best


def _merge_regions(scored, step):
    """Collapse overlapping above-threshold windows into one region each.

    `scored` is a list of (start, score, encoding, text, nbytes) sorted by start.
    Two windows belong to the same region when they overlap or sit within one
    step of each other, which is what an 8x-overlapping slide produces inside one
    text block. Reporting every window would return hundreds of rows for one file.

    Region length is measured in BYTES (the span the windows cover), never in
    characters: UTF-16LE text has half as many characters as bytes.
    """
    if not scored:
        return []
    regions = []
    first = scored[0]
    cur = dict(start=first[0], best=first[1], enc=first[2], text=first[3],
               end=first[0] + first[4])
    for start, score, enc, text, nbytes in scored[1:]:
        if start <= cur["end"] + step:
            cur["end"] = max(cur["end"], start + nbytes)
            if score > cur["best"]:
                # Track the best-scoring window's text and encoding, but NEVER
                # move `start`: `offset` is the offset of the REGION, which is
                # where its first above-threshold window began. Reporting the
                # best window's offset would place a long region hundreds of
                # bytes late and break any caller matching a known offset.
                cur.update(best=score, enc=enc, text=text)
        else:
            regions.append(cur)
            cur = dict(start=start, best=score, enc=enc, text=text,
                       end=start + nbytes)
    regions.append(cur)
    return regions


def search(data, top_n=10, threshold=None, window=None, step=None,
           min_run=None, model=None):
    """Find language inside `data` and rank it, best first.

    Returns a list of dicts, highest score first:
        offset    int   - byte offset of the region's best window
        score     float - mean trigram log-probability per character
        length    int   - bytes the region spans
        encoding  str   - 'utf-8' | 'utf-16le'
        preview   str   - the start of the decoded text, whitespace-collapsed

    `threshold` defaults to DEFAULT_THRESHOLD, `window` to DEFAULT_WINDOW and
    `step` to DEFAULT_STEP. Never raises on bytes-like input.
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    window = DEFAULT_WINDOW if window is None else int(window)
    step = DEFAULT_STEP if step is None else int(step)
    min_run = DEFAULT_MIN_RUN if min_run is None else int(min_run)
    try:
        data = bytes(data)
    except (TypeError, ValueError):
        return []

    model = text_ngram.default_model() if model is None else model
    if window <= 0 or step <= 0 or len(data) < min_run:
        return []

    scored = []
    for start in range(0, max(0, len(data) - min_chars_floor(min_run)) + 1, step):
        score, enc, text = score_window(data[start:start + window], model)
        if score is None or score < threshold:
            continue
        # Byte length, not character length: a UTF-16LE region is half as many
        # characters as bytes, so measuring characters would make a real 560-byte
        # plant look like 280 and drop it below a 256-byte minimum.
        scored.append((start, score, enc, text, len(data[start:start + window])))

    hits = []
    for region in _merge_regions(scored, step):
        length = region["end"] - region["start"]
        if length < min_run:
            continue
        preview = " ".join(region["text"][:PREVIEW_CHARS].split())
        hits.append({
            "offset": int(region["start"]),
            "score": round(float(region["best"]), 3),
            "length": int(length),
            "encoding": region["enc"],
            "preview": preview,
        })
    hits.sort(key=lambda h: (-h["score"], h["offset"]))
    return hits[:max(0, int(top_n))]


def min_chars_floor(min_run):
    """Smallest window start that can still hold a full `min_run` region."""
    return max(0, min_run - 1)


def search_file(path, top_n=10, **kwargs):
    """Search a file read-only. Returns the same shape as search()."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return search(data, top_n=top_n, **kwargs)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("Usage: python text_search.py <file> [file ...]")
        print("Read-only: ranks where language lives inside each file.")
        return 2
    worst = 0
    for path in argv:
        hits = search_file(path)
        print("=" * 78)
        print("%s - %d region(s)" % (path, len(hits)))
        if not hits:
            print("  (no decodable language found)")
            continue
        for hit in hits:
            print("  @0x%08X  score %-7.3f  %-9s %6d B  %s"
                  % (hit["offset"], hit["score"], hit["encoding"],
                     hit["length"], hit["preview"][:60]))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())