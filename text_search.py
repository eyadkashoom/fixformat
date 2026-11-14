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

Performance (v7, measured on the development host, see perf/README.md)
---------------------------------------------------------------------
The first implementation scored every 512-byte window from scratch: a 15-length
decode sweep per window, over windows that overlap 8x. On a 512 KiB block of
zeros that took ~90 seconds (0.006 MiB/s) and on text 0.006 MiB/s as well, which
is not usable on a disk image.

Three changes removed the structural multipliers rather than tuning constants:

1. a text-run prefilter (bytes.translate mask + bytes.find) so bytes that hold no
   text are never decoded;
2. a full-window fast path so a clear hit does not pay the 15-length trim sweep;
3. prefix-sum scoring per 1 MiB chunk, with a byte->character map for UTF-8 and
   byte//2 for UTF-16LE, so one byte is scored once instead of 120x.

Acceptance targets: runless input >= 10 MiB/s, text-dense input >= 0.5 MiB/s
(one character costs ~1 us in the pure-Python trigram model), 10x input <= 15x
time, and peak memory that does not grow with input size.

Environment: Python standard library only, no network, read-only on the input.
"""

import math
import os
import sys
from array import array
from itertools import accumulate

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

# --- text-run prefilter ------------------------------------------------------
# Before any window is decoded or scored, a C-level pass over the whole buffer
# marks where text can possibly live. Scoring a window whose bytes hold no
# text-shaped run is pure waste, and that waste was the whole cost: a 512-byte
# window of zeros or of random bytes used to pay a full 15-length decode sweep
# plus trigram scoring to discover it was not language.
#
# Implementation note: this uses bytes.translate masks plus bytes.find (memmem)
# rather than a regex. A {128,} regex over random bytes costs ~65 ns/byte
# because the engine retries at every start position; mask+memmem does the same
# job at memory speed. Measured on 512 KiB of random bytes: regex prefilter
# 100 ms, mask prefilter 6 ms.
_ASCII_SET = frozenset(list(range(0x09, 0x0E)) + list(range(0x20, 0x7F)))
_HI_NONNUL_SET = frozenset(list(range(0x01, 0x20)) + list(range(0xAC, 0xD8))
                           + list(range(0xF9, 0xFC)))
_UTF8_LEAD_SET = frozenset(range(0xC2, 0xE0))    # 2-byte UTF-8 leads (Arabic,
                                                  # Cyrillic, Greek, Hebrew...)
_UTF8_CONT_SET = frozenset(range(0x80, 0xC0))


def _table(allowed):
    """256-byte translate table: 0xFF for allowed bytes, 0x00 otherwise."""
    return bytes(0xFF if i in allowed else 0x00 for i in range(256))


_TBL_ASCII = _table(_ASCII_SET)
_ASCII_BYTES = bytes(sorted(_ASCII_SET))
_TBL_HI0 = _table({0x00})
_TBL_HINON = _table(_HI_NONNUL_SET)
_TBL_LEAD = _table(_UTF8_LEAD_SET)
_TBL_CONT = _table(_UTF8_CONT_SET)

# Whitespace as str.isspace() sees it inside a pure-ASCII run (bytes >= 0x80 never
# appear in such a run). _TBL_NONWS is the 0/1 mask used for prefix counting.
_WS_BYTES = b"\t\n\x0b\x0c\r "
_TBL_NONWS = bytes(0 if i in b"\t\n\x0b\x0c\r " else 1 for i in range(256))
_TBL_CONT_START = bytes(0 if 0x80 <= i <= 0xBF else 1 for i in range(256))
_HINON_BYTES = bytes(sorted(_HI_NONNUL_SET | {0x00}))
_WS_DELETE = {ord(c): None for c in "\t\n\x0b\x0c\r \x1c\x1d\x1e\x1f\x85\xa0"}

# Consecutive "text-shaped" bytes needed before a range is worth scoring. A
# 512-byte window needs DECODABLE text, so a shorter run cannot carry a hit on
# its own; 128 bytes is well below the 256-byte region minimum and still far
# above anything random bytes produce.
_MIN_RUN_BYTES = 128
_RUN_GAP = 64               # runs closer than this are treated as one
_SCAN_CHUNK = 4 << 20       # prefilter slice: bounds mask memory to ~8x this, not
                            # to 8x the input (100 MiB in one pass peaked at 840 MiB)


def _mask_or(*masks):
    """Byte-slice OR of equal-length masks (big-int OR, C speed)."""
    n = min(len(m) for m in masks)
    out = 0
    for m in masks:
        out |= int.from_bytes(m[:n], "little")
    return out.to_bytes(n, "little")


def _and_mask(a, b):
    """Byte-slice AND of two equal-length masks (big-int AND, C speed)."""
    n = min(len(a), len(b))
    return (int.from_bytes(a[:n], "little")
            & int.from_bytes(b[:n], "little")).to_bytes(n, "little")


def _byte_mask(data):
    """0xFF where a byte is part of ASCII text or of a 2-byte UTF-8 character."""
    n = len(data)
    ascii_mask = data.translate(_TBL_ASCII)
    leads = int.from_bytes(data.translate(_TBL_LEAD), "little")
    conts = int.from_bytes(data.translate(_TBL_CONT), "little")
    # Byte i starts a 2-byte character when it is a lead and byte i+1 is a
    # continuation. Both bytes of that character are text, so mark both - marking
    # only the lead would leave Arabic text as alternating holes and never form a
    # run. Random bytes almost never satisfy the lead+continuation rule, which is
    # what keeps this mask selective where a raw 0x80-0xBF mask would not be.
    starts = leads & (conts >> 8)
    chars = starts | (starts << 8)
    multi = (chars & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
    return _mask_or(ascii_mask, multi)


def _utf16_mask(data):
    """0xFF per UTF-16LE code unit that looks like text, one byte per unit."""
    n = len(data) - (len(data) % 2)
    hi = data[1:n:2]
    ascii_unit = _and_mask(hi.translate(_TBL_HI0), data[0:n:2].translate(_TBL_ASCII))
    return _mask_or(hi.translate(_TBL_HINON), ascii_unit)


def _runs_from_mask(mask, minimum, scale=1):
    """(start, end) byte ranges where `mask` holds >= `minimum` consecutive 0xFF."""
    needle = b"\xff" * minimum
    runs = []
    pos = mask.find(needle)
    while pos != -1:
        end = mask.find(b"\x00", pos)
        if end == -1:
            end = len(mask)
        runs.append((pos * scale, end * scale))
        pos = mask.find(needle, end)
    return runs


def _text_runs(data):
    """Merged byte ranges that can hold text, best-effort and conservative.

    Returns a sorted list of (start, end) byte ranges. A window that overlaps
    none of them cannot contain a text run and is skipped unread - which is what
    makes the search cheap on disks, where most bytes are not text.

    Conservative on purpose: a window overlapping ANY range is scored exactly as
    before, so the prefilter can only cost a little precision at region edges,
    never the language inside a run.

    The mask pass runs over 4 MiB slices rather than the whole buffer. The masks
    are big-int byte arrays (about 8 bytes of transient memory per input byte), so
    scanning 100 MiB in one pass peaked at 840 MiB RSS. Slices carry a
    _MIN_RUN_BYTES overlap: any run of that length either lies inside one slice or
    fully inside the next one, and the overlapping detections merge.
    """
    n = len(data)
    if n < _MIN_RUN_BYTES:
        return []
    ranges = []
    pos = 0
    while pos < n:
        end = min(pos + _SCAN_CHUNK, n)
        slice_end = min(end + _MIN_RUN_BYTES, n)
        seg = data[pos:slice_end]
        ranges += [(pos + a, pos + b)
                   for a, b in _runs_from_mask(_byte_mask(seg), _MIN_RUN_BYTES)]
        ranges += [(pos + a, pos + b)
                   for a, b in _runs_from_mask(_utf16_mask(seg),
                                               _MIN_RUN_BYTES // 2, scale=2)]
        if slice_end >= n:
            break
        pos = end
    if not ranges:
        return []
    ranges.sort()
    merged = [list(ranges[0])]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + _RUN_GAP:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(int(a), int(b)) for a, b in merged]


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


def score_window(window, model=None, min_chars=MIN_REGION_BYTES, stop_at=None):
    """Score one window: (score, encoding, text) or (None, 'none', '').

    Returns None for the score when the window has no decodable prefix of at
    least `min_chars` non-space characters - which is the common case for random
    and compressed bytes, and the reason the search is fast.

    `stop_at` is the caller's acceptance threshold. When the FULL window already
    scores at or above it, that reading is returned at once instead of sweeping
    the 15-length trim grid. Inside a real text region the full window is the
    right reading, so the sweep was paying 15 decodes and 15 trigram passes to
    confirm what the first one already proved - the single largest cost in the
    module. Boundary windows (a text head followed by non-text) still get the
    full sweep, because that is exactly where a shorter cut wins.
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
    # down. Taking the maximum over every length keeps the genuine reading -
    # except when `stop_at` is given and the full length already clears it.
    while end >= min_chars:
        for enc, text in _decode_candidates(data[:end]):
            nonspace = sum(1 for ch in text if not ch.isspace())
            if nonspace < min_chars:
                continue
            score = text_ngram.trigram_logprob(model, text)
            if best[0] is None or score > best[0]:
                best = (score, enc, text)
                if stop_at is not None and end == len(data) and score >= stop_at:
                    return best
        end -= 32
    return best


def _absorb(regions, cur, start, score, enc, nbytes, step):
    """Add one above-threshold window to the region being built.

    Same rule as `_merge_regions` - windows within one step join the current
    region - but applied on the fly so the caller never holds every window.

    `start` is the region's own start and is never moved to the best window:
    `offset` must be where the region begins, or a caller matching a known offset
    would be placed hundreds of bytes late.
    """
    if cur is not None and start <= cur["end"] + step:
        if start + nbytes > cur["end"]:
            cur["end"] = start + nbytes
        if score > cur["best"]:
            cur.update(best=score, enc=enc, win=(start, nbytes))
        return cur
    if cur is not None:
        regions.append(cur)
    return dict(start=start, best=score, enc=enc, win=(start, nbytes),
                end=start + nbytes)


def _merge_regions(scored, step):
    """Collapse overlapping above-threshold windows into one region each.

    `scored` is a list of (start, score, encoding, win, nbytes) sorted by start,
    where `win` is (window_start, window_bytes) of the best-scoring window in it.
    Only that pair is carried, never the decoded text: holding one 512-char
    preview per scored window made a 10 MiB text scan cost 230 MiB of RSS.
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
    cur = dict(start=first[0], best=first[1], enc=first[2], win=first[3],
               end=first[0] + first[4])
    for start, score, enc, win, nbytes in scored[1:]:
        if start <= cur["end"] + step:
            cur["end"] = max(cur["end"], start + nbytes)
            if score > cur["best"]:
                # Track the best-scoring window's text and encoding, but NEVER
                # move `start`: `offset` is the offset of the REGION, which is
                # where its first above-threshold window began. Reporting the
                # best window's offset would place a long region hundreds of
                # bytes late and break any caller matching a known offset.
                cur.update(best=score, enc=enc, win=win)
        else:
            regions.append(cur)
            cur = dict(start=start, best=score, enc=enc, win=win,
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

    # One C-level pass marks where text can live; windows outside every run are
    # skipped without being decoded or scored at all. Runless input (zeros,
    # random bytes, compressed data) therefore costs a single mask scan.
    runs = _text_runs(data)
    if not runs:
        return []

    # Regions are merged as the scan advances, not after it. Holding one entry per
    # above-threshold window made a 100 MiB text file cost 386 MiB of RSS (1.6M
    # tuples); a region list is O(regions), which is what the caller asked for.
    regions = []
    cur = None
    run_idx = 0
    cur_key = None          # only the current chunk is kept alive: windows advance
    cur_scorer = None       # monotonically, so a newer chunk never needs an older one
    stop = max(0, len(data) - min_chars_floor(min_run))
    for start in range(0, stop + 1, step):
        wend = start + window
        while run_idx < len(runs) and runs[run_idx][1] <= start:
            run_idx += 1
        if run_idx >= len(runs):
            break
        run_start, run_end = runs[run_idx]
        if run_start >= wend:
            continue
        # A window wholly inside one chunk of a text run is scored from that
        # chunk's prefix sums: same score as decoding it, O(1) instead of
        # O(window). Chunks are 1 MiB so peak memory stays bounded no matter how
        # large the input or the run is.
        if run_start <= start and wend <= run_end:
            chunk_idx = (start - run_start) // _CHUNK_BYTES
            key = (run_idx, chunk_idx)
            if key != cur_key:
                cs = run_start + chunk_idx * _CHUNK_BYTES
                ce = min(cs + _CHUNK_BYTES, run_end)
                # A chunk boundary can fall inside a multi-byte character. Nudging
                # it to the next character start keeps the chunk decodable; without
                # this the whole chunk failed to decode and every window in it fell
                # back to the 15-length per-window sweep (measured 20x slower).
                while cs < ce and 0x80 <= data[cs] <= 0xBF:
                    cs += 1
                while cs < ce < len(data) and 0x80 <= data[ce] <= 0xBF:
                    ce -= 1
                cur_scorer = _make_scorer(data, cs, ce, model)
                cur_key = key
            scorer = cur_scorer
            if scorer is not None and wend <= scorer.end:
                score = scorer.score(start, wend, MIN_REGION_BYTES)
                if score is not None and score >= threshold:
                    # 'ascii' is an internal fast-path label; the public encoding
                    # of plain ASCII text is 'utf-8' (it is a valid UTF-8 stream).
                    enc = "utf-8" if scorer.mode == "ascii" else scorer.mode
                    cur = _absorb(regions, cur, start, score, enc,
                                  wend - start, step)
                continue
        score, enc, text = score_window(data[start:wend], model,
                                        stop_at=threshold)
        if score is None or score < threshold:
            continue
        # Byte length, not character length: a UTF-16LE region is half as many
        # characters as bytes, so measuring characters would make a real 560-byte
        # plant look like 280 and drop it below a 256-byte minimum.
        # len(...), not wend - start: the last window is truncated at EOF and a
        # region must not claim bytes past the end of the buffer.
        cur = _absorb(regions, cur, start, score, enc, len(data[start:wend]), step)

    if cur is not None:
        regions.append(cur)

    hits = []
    for region in regions:
        length = region["end"] - region["start"]
        if length < min_run:
            continue
        win_start, win_len = region["win"]
        preview = " ".join(
            _preview_text(data, region["enc"], win_start, win_len)[:PREVIEW_CHARS].split())
        hits.append({
            "offset": int(region["start"]),
            "score": round(float(region["best"]), 3),
            "length": int(length),
            "encoding": region["enc"],
            "preview": preview,
        })
    hits.sort(key=lambda h: (-h["score"], h["offset"]))
    return hits[:max(0, int(top_n))]


def _preview_text(data, enc, start, length):
    """Decoded text of one window, for the `preview` field only."""
    chunk = data[start:start + length]
    candidates = (enc,) if enc == "utf-16le" else (enc, "utf-16-le")
    for codec in candidates:
        try:
            return chunk.decode(codec)
        except (UnicodeDecodeError, ValueError):
            continue
    return chunk.decode("utf-8", "replace")


def min_chars_floor(min_run):
    """Smallest window start that can still hold a full `min_run` region."""
    return max(0, min_run - 1)


_CHUNK_BYTES = 1 << 20     # prefix arrays are built per 1 MiB chunk, never for
                           # the whole input: memory stays O(chunk), not O(file).


def _hi_nonnul_ratio(raw):
    """Fraction of bytes at odd offsets that can be a UTF-16LE text high byte."""
    hi = raw[1::2]
    if not hi:
        return 0.0
    return (len(hi) - len(hi.translate(None, _HINON_BYTES))) / len(hi)


def _pick_encoding(raw):
    """'ascii' | 'utf-16le' | 'utf-8' | None for one chunk of text-run bytes.

    Order matters. UTF-16LE ASCII text is also valid UTF-8 (NUL is a legal code
    point), so a NUL-rich chunk is claimed for UTF-16LE first; otherwise the
    chunk is UTF-8 if it decodes strictly. Returning None hands the window back
    to the generic per-window path.
    """
    if not raw.translate(None, _ASCII_BYTES):
        return "ascii"
    if len(raw) >= 8 and len(raw) % 2 == 0 and _hi_nonnul_ratio(raw) >= 0.90 \
            and raw.count(0) * 10 >= len(raw):
        return "utf-16le"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except (UnicodeDecodeError, ValueError):
        pass
    if len(raw) % 2 == 0 and _hi_nonnul_ratio(raw) >= 0.90:
        return "utf-16le"
    return None


class _RunScorer(object):
    """O(1) window scoring inside one chunk of a text run.

    A window score is a prefix-sum difference, not a re-scan. That removes the
    two structural multipliers the old code paid for every window: the 8x overlap
    (step 64 over a 512-byte window scored each byte ~8 times) and the 15-length
    trim sweep.

    Three encodings are handled, because all three are first-class in the search:
      ascii     one byte == one character (source, logs, English prose)
      utf-16le  two bytes == one character (BMP text, NUL-rich)
      utf-8     multi-byte sequences (Arabic, Cyrillic, ...); a byte->character
                prefix array maps a byte window onto a character range
    """

    __slots__ = ("start", "end", "mode", "raw", "text", "pre", "char_at", "nonws")

    def __init__(self, data, start, end, mode, model):
        raw = data[start:end]
        self.start = start
        self.end = end
        self.raw = raw
        self.mode = mode
        if mode == "ascii":
            text = raw.decode("ascii")
        elif mode == "utf-16le":
            text = raw.decode("utf-16-le")
        else:
            text = raw.decode("utf-8")
        self.text = text

        # byte offset -> character index (prefix, length len(raw) + 1)
        if mode == "ascii":
            self.char_at = None                      # identity, no array needed
        elif mode == "utf-16le":
            self.char_at = None                      # k // 2
        else:
            # a byte that is not a UTF-8 continuation byte starts a character
            cont = raw.translate(_TBL_CONT_START)
            self.char_at = array("i", accumulate(cont, initial=0))

        # character index -> count of non-whitespace characters before it
        if mode == "ascii":
            self.nonws = array("i", accumulate(raw.translate(_TBL_NONWS), initial=0))
        else:
            flags = bytes(0 if ch.isspace() else 1 for ch in text)
            self.nonws = array("i", accumulate(flags, initial=0))

        norm = text.translate(_WS_DELETE).lower()
        k = float(model["k"])
        kv = k * model["V"]
        bi = model["bi"]
        tri = model["tri"]
        # pre[t] = sum of log-probabilities of the trigrams starting at 0..t-1.
        # Model keys are CHARACTER TUPLES (Counter of zip(...)), not substrings;
        # zip yields tuples directly, so no key building is needed.
        pre = array("d", [0.0])
        acc = 0.0
        for gram in zip(norm, norm[1:], norm[2:]):
            acc += math.log((tri.get(gram, 0) + k) / (bi.get(gram[:2], 0) + kv))
            pre.append(acc)
        self.pre = pre

    def _char_index(self, offset):
        if self.char_at is None:
            return offset if self.mode == "ascii" else offset // 2
        return self.char_at[offset]

    def score(self, abs_start, abs_end, min_chars):
        """Mean trigram log-probability of data[abs_start:abs_end], or None."""
        s = self._char_index(abs_start - self.start)
        e = self._char_index(abs_end - self.start)
        i = self.nonws[s]
        j = self.nonws[e]
        n = j - i
        if n < min_chars or n < 3:
            return None
        return (self.pre[j - 2] - self.pre[i]) / (n - 2)

    def preview(self, abs_start, abs_end):
        s = self._char_index(abs_start - self.start)
        e = self._char_index(abs_end - self.start)
        return self.text[s:e]


def _make_scorer(data, start, end, model):
    """A _RunScorer for data[start:end], or None when the chunk is not text."""
    if end - start < MIN_REGION_BYTES:
        return None
    mode = _pick_encoding(data[start:end])
    if mode is None:
        return None
    if mode == "utf-16le" and start % 2:
        return None                  # code units would be misaligned
    try:
        return _RunScorer(data, start, end, mode, model)
    except (UnicodeDecodeError, ValueError):
        return None


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