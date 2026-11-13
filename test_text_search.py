#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for text_search.py - Layer 5 n-gram perplexity search (v7.0.3).

Every fixture is deterministic (random.Random with a fixed seed). No os.urandom:
that is what made the v7.0.1.2 pipeline test flaky.

Acceptance mirrors QA's contract: all three planted regions found within +/-64
bytes, and zero false positives on a pure-random control.

Run:  python test_text_search.py
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import text_search as S  # noqa: E402

PLANT_ASCII = ("SEARCHMARK ASCII hello world recovery test.\n" * 8).encode("utf-8")
PLANT_ARABIC = ("علامة بحث عربية لاختبار الاسترجاع.\n" * 8).encode("utf-8")
PLANT_UTF16 = ("SEARCHMARK UTF16LE recovery probe.\n" * 8).encode("utf-16-le")

OFFSETS = {"ascii": 512, "arabic": 2048, "utf16": 4096}


def build_image():
    """8 KB deterministic image with three plants at known offsets."""
    rnd = random.Random(0x5EED)
    bg = bytearray(rnd.randrange(256) for _ in range(8192))
    bg[512:512 + len(PLANT_ASCII)] = PLANT_ASCII
    bg[2048:2048 + len(PLANT_ARABIC)] = PLANT_ARABIC
    bg[4096:4096 + len(PLANT_UTF16)] = PLANT_UTF16
    return bytes(bg)


class TestScoreWindow(unittest.TestCase):
    def test_ascii_scores_above_threshold(self):
        score, enc, text = S.score_window(PLANT_ASCII)
        self.assertIsNotNone(score)
        self.assertEqual(enc, "utf-8")
        self.assertGreater(float(score), S.DEFAULT_THRESHOLD)

    def test_arabic_scores_above_threshold(self):
        score, enc, _text = S.score_window(PLANT_ARABIC)
        self.assertIsNotNone(score)
        self.assertEqual(enc, "utf-8")
        self.assertGreater(float(score), S.DEFAULT_THRESHOLD)

    def test_utf16le_scores_above_threshold(self):
        score, enc, text = S.score_window(PLANT_UTF16)
        self.assertIsNotNone(score)
        self.assertEqual(enc, "utf-16le")
        self.assertGreater(float(score), S.DEFAULT_THRESHOLD)
        self.assertIn("SEARCHMARK", text)

    def test_random_bytes_score_below_threshold(self):
        # Random bytes can be forced into a short UTF-16LE reading, so a score is
        # not impossible - but it must land BELOW the threshold, which is what
        # keeps them out of the results. Asserting "no score" was wrong; the
        # calibrated property is the score, not the presence of one.
        rnd = bytes(random.Random(0xBAD).randrange(256) for _ in range(512))
        score, _enc, _text = S.score_window(rnd)
        if score is not None:
            self.assertLess(float(score), S.DEFAULT_THRESHOLD,
                            "random bytes scored %s, above threshold" % score)


class TestSearchRecall(unittest.TestCase):
    """The three plants must be found at their offsets (within 64 bytes)."""

    @classmethod
    def setUpClass(cls):
        cls.image = build_image()
        cls.hits = S.search(cls.image, top_n=20)

    def assertFound(self, name, expected):
        match = [h for h in self.hits if abs(h["offset"] - expected) <= 64]
        self.assertTrue(match, "%s not found near %d in %s"
                        % (name, expected,
                           [(h["offset"], h["score"]) for h in self.hits]))

    def test_ascii_found(self):
        self.assertFound("ascii", OFFSETS["ascii"])

    def test_arabic_found(self):
        self.assertFound("arabic", OFFSETS["arabic"])

    def test_utf16le_found(self):
        self.assertFound("utf16", OFFSETS["utf16"])

    def test_scores_are_ordered_best_first(self):
        scores = [h["score"] for h in self.hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_hit_shape(self):
        for hit in self.hits:
            for key in ("offset", "score", "length", "encoding", "preview"):
                self.assertIn(key, hit)
            self.assertIsInstance(hit["offset"], int)
            self.assertIsInstance(hit["length"], int)
            self.assertGreater(hit["length"], 0)


class TestNoFalsePositives(unittest.TestCase):
    def test_pure_random_yields_nothing(self):
        rnd = bytes(random.Random(0xBAD).randrange(256) for _ in range(4096))
        hits = S.search(rnd, top_n=10)
        self.assertEqual(hits, [], "random bytes produced hits: %s" % hits)

    def test_zeros_yield_nothing(self):
        self.assertEqual(S.search(b"\x00" * 4096), [])

    def test_compressed_yields_nothing(self):
        import zlib
        blob = zlib.compress(b"A" * 4096)
        self.assertEqual(S.search(blob), [])


class TestRegionMerge(unittest.TestCase):
    def test_one_region_not_many(self):
        # A single text block slid 8x over must report ONE region, not one row
        # per window.
        image = build_image()
        hits = S.search(image, top_n=50)
        offsets = [h["offset"] for h in hits]
        # three plants -> at most a few regions, never dozens
        self.assertLessEqual(len(hits), 6, "too many regions: %s" % offsets)

    def test_region_offset_is_region_start_not_best_window(self):
        # The reported offset must be where the region begins, not where its
        # highest-scoring window happens to sit.
        image = build_image()
        hits = S.search(image, top_n=50)
        utf16 = [h for h in hits if h["encoding"] == "utf-16le"]
        self.assertTrue(utf16)
        self.assertLessEqual(abs(utf16[0]["offset"] - OFFSETS["utf16"]), 64)


class TestContract(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(S.search(b""), [])

    def test_non_bytes_does_not_raise(self):
        self.assertEqual(S.search("not bytes"), [])

    def test_short_input(self):
        self.assertEqual(S.search(b"hi"), [])

    def test_top_n_respected(self):
        image = build_image()
        self.assertLessEqual(len(S.search(image, top_n=1)), 1)

    def test_zero_top_n(self):
        self.assertEqual(S.search(build_image(), top_n=0), [])


class TestPinnedCalibration(unittest.TestCase):
    """Thresholds are measured constants - a change must be deliberate."""

    def test_threshold_between_floor_and_weakest_plant(self):
        import text_ngram as N
        # must sit above the trigram floor (never language) and below the
        # weakest real plant measured in calibration (-3.88 Arabic)
        self.assertGreater(S.DEFAULT_THRESHOLD, N.TRILP_FLOOR)
        self.assertLess(S.DEFAULT_THRESHOLD, -3.88)

    def test_defaults(self):
        self.assertEqual(S.DEFAULT_WINDOW, 512)
        self.assertEqual(S.DEFAULT_STEP, 64)
        self.assertEqual(S.DEFAULT_MIN_RUN, 256)

    def test_version(self):
        self.assertEqual(S.VERSION, "7.0.3")


if __name__ == "__main__":
    unittest.main(verbosity=2)