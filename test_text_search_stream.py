#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_text_search_stream.py - bounded-memory scanning of a byte stream.

`text_search.search(data)` needs the whole input in memory, so a 476 GiB disk
image could not be scanned with it at all: the read alone exceeds system RAM.
`search_stream(fileobj, chunk=...)` reads fixed blocks with `window + step` of
carry-over, so peak memory is set by `chunk` instead of by the input size.

What is verified here:

1. Parity with `search()` - for every committed fixture, and at block sizes
   small enough to force a boundary inside a region, the streamed result covers
   at least every byte the in-memory pass covers, with the same encodings.
2. No cut regions - a text block deliberately placed across a forced block
   boundary comes back as one region that covers the whole block.
3. No seeking - a reader that only offers `read(n)` works, so pipes and device
   objects are usable.
4. Bounded reads - no single `read()` asks for more than `chunk` bytes, and a
   chunk smaller than the carry-over is lifted to a workable size instead of
   producing empty results.

Run: python test_text_search_stream.py
"""

import glob
import io
import os
import tempfile
import unittest

import text_search

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = sorted(glob.glob(os.path.join(ROOT, "fixtures", "*", "*")))

PROSE = (
    "The recovery pass walks the volume in fixed slices and keeps only the "
    "regions whose byte statistics look like language. Every candidate is then "
    "scored against a trigram model, and the survivors are written to the "
    "report with their offsets and their declared encoding. "
) * 6


class ReadOnlyStream(object):
    """A file-like object with read(n) only: no seek, no tell, no size."""

    def __init__(self, data, chunk=None):
        self._data = data
        self._pos = 0
        self.chunk = chunk
        self.max_read = 0

    def read(self, n=-1):
        if self.chunk is not None:
            n = min(n, self.chunk) if n >= 0 else self.chunk
        n = len(self._data) - self._pos if n < 0 else n
        self.max_read = max(self.max_read, n)
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out


def _read(path):
    """Read a fixture with the handle closed - a leaked handle is a defect."""
    with open(path, "rb") as fh:
        return fh.read()


def _covered(hits, encoding=None):
    return sum(hit["length"] for hit in hits
               if encoding is None or hit["encoding"] == encoding)


def _contains(outer, inner, step=text_search.DEFAULT_STEP):
    """True when the [start, end) span of `inner` sits inside `outer`'s span."""
    return (outer["start"] <= inner["start"] + step
            and inner["end"] <= outer["end"] + step)


class TestParityWithInMemorySearch(unittest.TestCase):
    """Streaming must not lose what a single in-memory pass finds."""

    def test_every_fixture_is_covered_at_small_block_sizes(self):
        self.assertTrue(FIXTURES, "no fixtures found")
        for path in FIXTURES:
            data = _read(path)
            memory_hits = text_search.search(data, top_n=64)
            for chunk in (2048, 4096, 8192):
                with self.subTest(fixture=os.path.basename(path), chunk=chunk):
                    stream_hits = text_search.search_stream(
                        io.BytesIO(data), chunk=chunk, top_n=64)
                    for enc in {hit["encoding"] for hit in memory_hits}:
                        self.assertGreaterEqual(
                            _covered(stream_hits, enc),
                            _covered(memory_hits, enc),
                            "%s @%d: coverage lost for %s"
                            % (os.path.basename(path), chunk, enc))
                    spans = [{"start": h["offset"], "end": h["offset"] + h["length"]}
                             for h in stream_hits]
                    for hit in memory_hits:
                        inner = {"start": hit["offset"],
                                 "end": hit["offset"] + hit["length"]}
                        self.assertTrue(
                            any(_contains(outer, inner) for outer in spans),
                            "%s @%d: region %r not covered by stream"
                            % (os.path.basename(path), chunk, inner))

    def test_negative_fixtures_stay_silent(self):
        for path in FIXTURES:
            if os.sep + "negative" + os.sep not in path:
                continue
            with self.subTest(fixture=os.path.basename(path)):
                data = _read(path)
                self.assertEqual(
                    text_search.search_stream(ReadOnlyStream(data), chunk=2048),
                    [])

    def test_rows_never_claim_bytes_past_the_input(self):
        for path in FIXTURES:
            data = _read(path)
            with self.subTest(fixture=os.path.basename(path)):
                for hit in text_search.search_stream(io.BytesIO(data),
                                                     chunk=2048, top_n=64):
                    self.assertGreaterEqual(hit["offset"], 0)
                    self.assertLessEqual(hit["offset"] + hit["length"], len(data))


class TestBoundaries(unittest.TestCase):
    """A block boundary must never cut a region in half."""

    def test_region_across_a_forced_boundary_is_one_region(self):
        start, end = 3800, 3800 + len(PROSE)
        payload = (b"\x00" * start + PROSE.encode("ascii")
                   + b"\x00" * 1500)
        self.assertGreater(end, 4096)      # the plant really crosses the edge
        hits = text_search.search_stream(io.BytesIO(payload), chunk=4096,
                                         top_n=8)
        self.assertTrue(hits, "the straddling region was not found at all")
        covering = [h for h in hits
                    if h["offset"] <= start
                    and h["offset"] + h["length"] >= end - text_search.DEFAULT_STEP]
        self.assertTrue(covering,
                        "no region covers the plant across the boundary: %r"
                        % [(h["offset"], h["length"]) for h in hits])

    def test_tiny_chunk_is_lifted_instead_of_producing_nothing(self):
        data = ("%s" % PROSE).encode("ascii")
        hits = text_search.search_stream(io.BytesIO(data), chunk=64)
        self.assertTrue(hits, "chunk smaller than window+step returned nothing")

    def test_empty_and_short_inputs(self):
        self.assertEqual(text_search.search_stream(io.BytesIO(b"")), [])
        self.assertEqual(text_search.search_stream(io.BytesIO(b"abc")), [])
        self.assertEqual(text_search.search_stream(ReadOnlyStream(b"")), [])


class TestBoundedReads(unittest.TestCase):
    """The whole point: memory and read sizes come from `chunk`, not the input."""

    def test_no_read_exceeds_the_chunk(self):
        data = PROSE.encode("ascii")
        reader = ReadOnlyStream(data)
        text_search.search_stream(reader, chunk=4096)
        self.assertLessEqual(reader.max_read, 4096,
                             "asked for %d bytes with chunk=4096"
                             % reader.max_read)

    def test_many_blocks_come_back_as_few_regions(self):
        data = PROSE.encode("ascii")
        one = text_search.search_stream(io.BytesIO(data), chunk=len(data))
        many = text_search.search_stream(io.BytesIO(data), chunk=2048)
        self.assertEqual(len(one), len(many))
        self.assertAlmostEqual(one[0]["score"], many[0]["score"], places=2)
        self.assertLessEqual(abs(one[0]["length"] - many[0]["length"]), 64)

    def test_file_scan_matches_stream_scan(self):
        data = PROSE.encode("ascii") + b"\x00" * 400
        fd, path = tempfile.mkstemp(suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            from_file = text_search.search_file(path)
            from_memory = text_search.search_stream(io.BytesIO(data),
                                                    chunk=1024)
            self.assertEqual(len(from_file), len(from_memory))
            for a, b in zip(from_file, from_memory):
                self.assertEqual(a["offset"], b["offset"])
                self.assertEqual(a["encoding"], b["encoding"])
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        self.assertEqual(text_search.search_file("no/such/file.bin"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
