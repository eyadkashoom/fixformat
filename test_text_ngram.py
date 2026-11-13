"""
Regression tests for text_ngram.py - the Layer 4 n-gram filter.

Every fixture is synthetic and deterministic (no os.urandom): the pipeline test
that used os.urandom was the source of the v7.0.1.2 flake, so nothing here may
use it. Noise comes from hashlib.shake_256 with a fixed seed.

Run:  python test_text_ngram.py
"""

import hashlib
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import text_ngram as N  # noqa: E402


def shake(seed, n=2048):
    return hashlib.shake_256(seed).digest(n)


def zip_valid_with_comment(comment=b"C" * 300):
    """A structurally valid ZIP whose comment is one repeated byte.

    This is the exact false positive QA found: entropy 1.08 and printable 0.81
    let it through the hard gate, so only Layer 4 can reject it.
    """
    local = bytes.fromhex(
        "504B03041400000000000000000000000000000000000000000000")
    central = bytes.fromhex(
        "504B01021400140000000000000000000000000000000000000000000000")
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1,
                       len(central), 0, len(comment)) + comment
    return local + central + eocd


class TestDecode(unittest.TestCase):
    def test_ascii_is_utf8(self):
        text, enc, ok = N.decode_view(b"hello world")
        self.assertTrue(ok)
        self.assertEqual(enc, "utf-8")
        self.assertEqual(text, "hello world")

    def test_utf8_arabic(self):
        text, enc, ok = N.decode_view("مرحبا بالعالم".encode("utf-8"))
        self.assertTrue(ok)
        self.assertEqual(enc, "utf-8")
        self.assertIn("مرحبا", text)

    def test_utf16le_flagged(self):
        blob = ("Hello utf16 text line.\n" * 20).encode("utf-16-le")
        ratio, is16 = N.utf16_ratio(blob)
        self.assertTrue(is16, "ratio=%.3f" % ratio)
        text, enc, ok = N.decode_view(blob)
        self.assertTrue(ok)
        self.assertEqual(enc, "utf-16le")
        self.assertIn("Hello", text)

    def test_short_buffer_is_not_utf16(self):
        # "   \n\n" as UTF-16LE must NOT be flagged: 10 bytes is too short.
        _ratio, is16 = N.utf16_ratio("   \n\n".encode("utf-16-le"))
        self.assertFalse(is16)

    def test_random_bytes_are_not_utf8(self):
        _text, enc, ok = N.decode_view(shake(b"random-bytes"))
        self.assertFalse(ok)
        self.assertEqual(enc, "none")


class TestRejects(unittest.TestCase):
    """Every negative must be rejected, with the documented reason."""

    def assertRejected(self, blob, why_contains=None):
        r = N.ngram_filter(blob)
        self.assertFalse(r["is_text"], "accepted as text: %s" % r["reason"])
        if why_contains:
            self.assertIn(why_contains, r["reason"])

    def test_zip_valid_with_long_comment(self):
        # the QA false positive: 379 bytes, entropy 1.08, printable 0.81
        self.assertRejected(zip_valid_with_comment())

    def test_zip_comment_under_runfrac_limit_still_rejected(self):
        # even with a short comment the EOCD structure has no word gaps
        self.assertRejected(zip_valid_with_comment(b"C" * 10))

    def test_zlib_stream(self):
        self.assertRejected(zlib.compress(shake(b"zlib-input")))

    def test_gzip_stream(self):
        import gzip
        self.assertRejected(gzip.compress(shake(b"gzip-input")))

    def test_shake_noise(self):
        self.assertRejected(shake(b"deterministic-noise"))

    def test_sparse_nul_padding(self):
        # 40% NUL bytes -> rejected before any statistics
        self.assertRejected(b"X" * 120 + b"\x00" * 120 + b"Y" * 60)

    def test_empty_utf16le_is_short(self):
        # short, so it fails min_run rather than pretending to be text
        r = N.ngram_filter("   \n\n".encode("utf-16-le"))
        self.assertFalse(r["is_text"])

    def test_all_one_character(self):
        self.assertRejected(b"A" * 4096)

    def test_hex_dump(self):
        blob = b"".join(b"%04x " % i for i in range(400))
        self.assertRejected(blob)


class TestAccepts(unittest.TestCase):
    """Real language must survive the filter."""

    def assertAccepted(self, blob):
        r = N.ngram_filter(blob)
        self.assertTrue(r["is_text"], "rejected as noise: %s" % r["reason"])
        self.assertGreater(r["confidence"], 0.5)
        return r

    def test_english_prose(self):
        text = ("The recovery tool scans the disk image in large blocks and "
                "reports the exact byte offset of every signature it finds. "
                "Because the source is opened read-only, nothing on the damaged "
                "volume can be modified while the analysis runs.\n"
                "When a partition is encrypted, carving its payload produces "
                "false positives only, since ciphertext has no headers.\n")
        self.assertAccepted(text.encode("utf-8"))

    def test_arabic_prose(self):
        text = ("تقوم الأداة بمسح صورة القرص في كتل كبيرة ثم تسجل موضع كل توقيع "
                "بدقة. ولأن المصدر يفتح للقراءة فقط فلا يمكن تعديل أي بيانات على "
                "القرص التالف أثناء التحليل. وعندما يكون القسم مشفرا فإن استخراج "
                "محتواه ينتج نتائج ايجابية كاذبة فقط.\n")
        self.assertAccepted(text.encode("utf-8"))

    def test_python_source(self):
        src = b"def carve(reader, offset):\n    data = reader.read(offset)\n    return data\n"
        self.assertAccepted(src * 20)

    def test_markdown(self):
        src = b"# Title\n\nSome *markdown* text here.\n\n## Section\n\nMore prose follows.\n"
        self.assertAccepted(src * 10)

    def test_utf16le_prose(self):
        text = "Hello utf16 text line with enough words to look like prose.\n" * 5
        r = self.assertAccepted(text.encode("utf-16-le"))
        self.assertEqual(r["decode"], "utf-16le")


class TestNoRegressionOnRealFiles(unittest.TestCase):
    """The rule must hold on real repository sources, not just fixtures.

    This is the calibration set: 8 actual source files, all must pass. If any
    threshold drifts, this fails rather than shipping a filter that eats code.
    """

    FILES = ("text_ngram.py", "text_classify.py", "text_recovery.py",
             "fat32_recovery.py", "text_recovery_pipeline.py", "run_db.py",
             "volume_gate.py", "validators.py")

    def test_repo_sources_are_text(self):
        here = os.path.dirname(os.path.abspath(__file__))
        checked = 0
        for name in self.FILES:
            path = os.path.join(here, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                blob = fh.read()[:8000]
            r = N.ngram_filter(blob)
            self.assertTrue(r["is_text"], "%s rejected: %s" % (name, r["reason"]))
            checked += 1
        self.assertGreaterEqual(checked, 5, "too few source files found")


class TestContract(unittest.TestCase):
    """The filter is Layer 4: it only ever rejects, never promotes."""

    def test_short_input_rejected(self):
        r = N.ngram_filter(b"hi")
        self.assertFalse(r["is_text"])
        self.assertIn("too short", r["reason"])

    def test_non_bytes_input_does_not_raise(self):
        r = N.ngram_filter("a string, not bytes")
        self.assertFalse(r["is_text"])

    def test_empty_input_does_not_raise(self):
        r = N.ngram_filter(b"")
        self.assertFalse(r["is_text"])

    def test_result_keys_always_present(self):
        for blob in (b"", b"hi", shake(b"k"), b"A" * 5000):
            r = N.ngram_filter(blob)
            for key in ("is_text", "confidence", "reason", "decode",
                        "runfrac", "space", "trigram"):
                self.assertIn(key, r)


class TestThresholdsArePinned(unittest.TestCase):
    """Calibration values are committed constants - a change must be deliberate.

    These are the numbers measured in scratch/calib_ngram4.py. If someone tunes
    the filter, this test forces them to update the record too.
    """

    def test_pinned_values(self):
        self.assertEqual(N.NUL_MAX, 0.30)
        self.assertEqual(N.RUNFRAC_MAX, 0.25)
        self.assertEqual(N.SPACE_MIN, 0.05)
        self.assertAlmostEqual(N.TRILP_FLOOR, -4.42, places=2)
        self.assertEqual(N.MODEL_K, 0.1)

    def test_version_is_v7_0_2(self):
        self.assertEqual(N.VERSION, "7.0.2")


if __name__ == "__main__":
    unittest.main(verbosity=2)