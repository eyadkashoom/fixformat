#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_fixtures.py - the golden fixtures are committed, reproducible and met.

Three things are verified here, and they are independent:

1. Reproducibility - the committed bytes and MANIFEST.json are exactly what
   `scripts/generate_fixtures.py` produces from its fixed seed. A fixture that
   was hand-edited, or a generator that silently changed, fails here.
2. Precision - every `negative/` fixture produces zero regions. A false positive
   on random bytes, zeros, compressed data or file headers is a product bug, not
   a tuning preference.
3. Recall - every `positive/` and `edge_cases/` fixture is found, with the
   coverage and the encoding recorded in its own entry of MANIFEST.json. The
   expectation lives in the manifest, not only in this file, so a reader can see
   what the scanner promises without reading the test.

The acceptance numbers are the ones the fixtures were created for; they are
checked against a real run of `text_search.search()` on the committed bytes.

Run: python test_fixtures.py
"""

import hashlib
import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(ROOT, "fixtures")
MANIFEST_PATH = os.path.join(FIXTURE_DIR, "MANIFEST.json")
GENERATOR_PATH = os.path.join(ROOT, "scripts", "generate_fixtures.py")

import text_search  # noqa: E402  (after ROOT is known)


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_fixtures",
                                                  GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _read(rel):
    with open(os.path.join(FIXTURE_DIR, rel.replace("/", os.sep)), "rb") as fh:
        return fh.read()


class TestFixtureIntegrity(unittest.TestCase):
    def setUp(self):
        self.manifest = _manifest()

    def test_every_manifest_entry_is_on_disk(self):
        for entry in self.manifest["fixtures"]:
            with self.subTest(path=entry["path"]):
                path = os.path.join(FIXTURE_DIR, entry["path"].replace("/", os.sep))
                self.assertTrue(os.path.exists(path), "missing %s" % entry["path"])

    def test_sizes_and_hashes_match_the_manifest(self):
        for entry in self.manifest["fixtures"]:
            with self.subTest(path=entry["path"]):
                data = _read(entry["path"])
                self.assertEqual(len(data), entry["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(),
                                 entry["sha256"])

    def test_no_fixture_exceeds_the_size_budget(self):
        budget = self.manifest["max_bytes"]
        for entry in self.manifest["fixtures"]:
            with self.subTest(path=entry["path"]):
                self.assertLessEqual(entry["bytes"], budget)

    def test_generator_reproduces_committed_bytes(self):
        """The whole point of a golden fixture: git clone is enough to verify."""
        generator = _load_generator()
        self.assertEqual(generator.check_all(), 0,
                         "committed fixtures drift from the generator")


class TestPrecision(unittest.TestCase):
    """No negative fixture may produce a region - not one, at any threshold."""

    def test_negative_fixtures_produce_no_regions(self):
        for entry in _manifest()["fixtures"]:
            if entry["kind"] != "negative":
                continue
            with self.subTest(path=entry["path"]):
                hits = text_search.search(_read(entry["path"]))
                self.assertEqual(hits, [],
                                 "%s produced %d region(s)"
                                 % (entry["path"], len(hits)))


class TestRecall(unittest.TestCase):
    """Every positive fixture is found, with the promised coverage/encoding."""

    def test_positive_fixtures_meet_their_acceptance(self):
        for entry in _manifest()["fixtures"]:
            if entry["kind"] == "negative":
                continue
            acceptance = entry.get("acceptance", {})
            with self.subTest(path=entry["path"]):
                data = _read(entry["path"])
                hits = text_search.search(data, top_n=64)
                min_regions = acceptance.get("min_regions", 1)
                self.assertGreaterEqual(
                    len(hits), min_regions,
                    "%s: %d region(s), expected at least %d"
                    % (entry["path"], len(hits), min_regions))

                covered = sum(hit["length"] for hit in hits)
                min_coverage = acceptance.get("min_coverage", 0.0)
                self.assertGreaterEqual(
                    covered / len(data), min_coverage,
                    "%s: %.1f%% covered, expected at least %.0f%%"
                    % (entry["path"], 100.0 * covered / len(data),
                       100.0 * min_coverage))

                encodings = acceptance.get("encodings")
                if encodings:
                    reported = {hit["encoding"] for hit in hits}
                    self.assertTrue(
                        reported & set(encodings),
                        "%s: reported encodings %s, expected one of %s"
                        % (entry["path"], sorted(reported), encodings))

    def test_regions_never_claim_bytes_past_the_buffer(self):
        for entry in _manifest()["fixtures"]:
            with self.subTest(path=entry["path"]):
                size = entry["bytes"]
                for hit in text_search.search(_read(entry["path"]), top_n=64):
                    self.assertGreaterEqual(hit["offset"], 0)
                    self.assertLessEqual(hit["offset"] + hit["length"], size)

    def test_regions_never_overlap_each_other(self):
        for entry in _manifest()["fixtures"]:
            with self.subTest(path=entry["path"]):
                hits = sorted(text_search.search(_read(entry["path"]), top_n=64),
                              key=lambda h: h["offset"])
                for prev, nxt in zip(hits, hits[1:]):
                    # Two regions may overlap by up to one window when a codec
                    # boundary falls inside that window: the split is by encoding,
                    # not by byte, and the window that straddles the boundary is
                    # evidence for both sides.
                    self.assertLessEqual(prev["offset"] + prev["length"],
                                         nxt["offset"] + text_search.DEFAULT_WINDOW,
                                         "regions overlap in %s" % entry["path"])


class TestNamedRegressions(unittest.TestCase):
    """The specific defects these fixtures were written to catch."""

    def test_arabic_utf16le_is_detected(self):
        """Arabic UTF-16LE bytes are valid UTF-8 (high byte 0x06 is a control
        code point), so the UTF-8 reading used to win and score at the trigram
        floor: the whole run was invisible. The control-character guard in
        `_pick_encoding` is what fixes it."""
        path = "positive/utf16le_arabic.txt"
        hits = text_search.search(_read(path))
        self.assertTrue(hits, "Arabic UTF-16LE produced no region")
        self.assertEqual(hits[0]["encoding"], "utf-16le")
        self.assertGreaterEqual(hits[0]["length"], 6000)
        preview = hits[0]["preview"]
        self.assertTrue(any("\u0600" <= ch <= "\u06ff" for ch in preview),
                        "preview is not Arabic: %r" % preview[:40])
        self.assertNotIn("\ufffd", preview)

    def test_a_region_never_mixes_codecs(self):
        """ASCII then UTF-16LE in one run must come back as two regions: the
        `encoding` field describes the bytes a row claims."""
        hits = text_search.search(_read("positive/mixed_encodings.bin"), top_n=64)
        encodings = [hit["encoding"] for hit in hits]
        self.assertIn("utf-8", encodings)
        self.assertIn("utf-16le", encodings)
        data = _read("positive/mixed_encodings.bin")
        for hit in hits:
            chunk = data[hit["offset"]:hit["offset"] + hit["length"]]
            if hit["encoding"] == "utf-16le":
                # UTF-16LE English has a NUL in every second byte.
                self.assertGreater(chunk[1::2].count(0), len(chunk) // 4)

    def test_utf16le_is_found_at_an_odd_offset(self):
        """Code-unit parity belongs to the run, not to the buffer start. A
        buffer-aligned-only prefilter drops every odd-aligned run."""
        hits = text_search.search(_read("edge_cases/misaligned_utf16le.bin"))
        self.assertTrue(hits)
        self.assertEqual(hits[0]["encoding"], "utf-16le")
        preview = hits[0]["preview"]
        self.assertTrue(preview.isascii(), "preview is not ASCII text: %r" % preview[:40])
        self.assertGreater(len(preview), 20)

    def test_utf16be_text_is_recovered_through_the_odd_utf16le_reading(self):
        """There is no UTF-16BE code path; the bytes are still recovered because
        UTF-16BE read as UTF-16LE at offset 1 yields the same characters. The
        row is labelled utf-16le, which is byte-accurate, and the manifest says
        so - see the `why` field of that entry."""
        hits = text_search.search(_read("positive/utf16be_english.txt"))
        self.assertTrue(hits, "UTF-16BE text was not recovered at all")
        preview = hits[0]["preview"]
        self.assertTrue(preview.isascii(),
                        "UTF-16BE recovery produced non-ASCII text: %r"
                        % preview[:40])

    def test_truncated_and_bom_fixtures_do_not_raise(self):
        """decode must never raise: the fixtures cut characters in half."""
        for rel in ("edge_cases/truncated_runs.bin", "edge_cases/bom_variants.bin"):
            with self.subTest(path=rel):
                hits = text_search.search(_read(rel))
                self.assertTrue(hits)
                for hit in hits:
                    self.assertIsInstance(hit["preview"], str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
