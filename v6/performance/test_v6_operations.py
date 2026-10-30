"""Automated tests for v6/performance/operations.py.

Covers: adaptive operation selection, entropy calculations, parallel penalty
accounting, fast-scan calibration reproducibility, Direct I/O configured and
fallback behavior. Deterministic documented inputs + edge cases. stdlib only.

Run:  python test_v6_operations.py          (from this dir)
      python -m unittest test_v6_operations -v
Repo under test defaults to the fixformat checkout; override with
FIXFORMAT_REPO env var.
"""

import math
import os
import sys
import tempfile
import unittest

REPO = os.environ.get(
    "FIXFORMAT_REPO", "C:/Users/eyad/Documents/Codex/2026-09-27/fixformat"
)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from v6.performance import operations  # noqa: E402


def make_temp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="v6ops_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class TestSelectOp(unittest.TestCase):
    def test_empty_returns_zero_skip(self):
        self.assertEqual(operations.select_op(b""), "zero_skip")

    def test_all_nonzero_returns_adaptive_parallel(self):
        self.assertEqual(
            operations.select_op(b"\xff\xd8\xff\xe0" * 16), "adaptive_parallel"
        )

    def test_all_zero_returns_single_worker_skip(self):
        self.assertEqual(
            operations.select_op(b"\x00" * 64), "single_worker_skip"
        )

    def test_boundary_below_threshold(self):
        # 2/10 zeros = 0.2 < 0.3 -> adaptive_parallel
        self.assertEqual(
            operations.select_op(b"\x00" * 2 + b"\xff" * 8),
            "adaptive_parallel",
        )

    def test_boundary_at_threshold(self):
        # 3/10 zeros = 0.3, rule is strictly < 0.3 -> single_worker_skip
        self.assertEqual(
            operations.select_op(b"\x00" * 3 + b"\xff" * 7),
            "single_worker_skip",
        )

    def test_boundary_above_threshold(self):
        # 4/10 zeros = 0.4 -> single_worker_skip
        self.assertEqual(
            operations.select_op(b"\x00" * 4 + b"\xff" * 6),
            "single_worker_skip",
        )

    def test_single_zero_byte(self):
        self.assertEqual(operations.select_op(b"\x00"), "single_worker_skip")

    def test_single_nonzero_byte(self):
        self.assertEqual(operations.select_op(b"\x01"), "adaptive_parallel")

    def test_deterministic(self):
        sample = b"\x00\x01\x02" * 100
        self.assertEqual(operations.select_op(sample), operations.select_op(sample))


class TestEntropy(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(operations.entropy(b""), 0.0)

    def test_single_repeated_byte_is_zero(self):
        self.assertEqual(operations.entropy(b"\x00" * 64), 0.0)
        self.assertEqual(operations.entropy(b"\xff" * 64), 0.0)

    def test_two_equiprobable_bytes_is_one_bit(self):
        self.assertAlmostEqual(operations.entropy(b"\x00\xff"), 1.0, places=9)

    def test_uniform_256_values_is_eight_bits(self):
        data = bytes(range(256))
        self.assertAlmostEqual(operations.entropy(data), 8.0, places=9)

    def test_documented_sample_in_range(self):
        e = operations.entropy(b"\xff\xd8\xff\xe0" * 16)
        self.assertGreaterEqual(e, 0.0)
        self.assertLessEqual(e, 8.0)
        # two distinct byte values uneven (48 x ff, 16 x d8/e0-ish):
        # deterministic exact value pinned to formula
        counts = {}
        for b in b"\xff\xd8\xff\xe0" * 16:
            counts[b] = counts.get(b, 0) + 1
        total = 64
        expected = -sum((c / total) * math.log2(c / total) for c in counts.values())
        self.assertAlmostEqual(e, expected, places=12)

    def test_deterministic(self):
        data = bytes([i % 7 for i in range(1000)])
        self.assertEqual(operations.entropy(data), operations.entropy(data))

    def test_monotonic_more_uniform_means_higher(self):
        skewed = b"\x00" * 90 + b"\xff" * 10
        uniform = b"\x00" * 50 + b"\xff" * 50
        self.assertLess(operations.entropy(skewed), operations.entropy(uniform))


class TestParallelPenalty(unittest.TestCase):
    def test_single_worker_no_penalty(self):
        self.assertEqual(operations.parallel_penalty(1, 0.0), 1.0)
        self.assertEqual(operations.parallel_penalty(1, 0.8), 1.0)
        self.assertEqual(operations.parallel_penalty(1, 1.0), 1.0)

    def test_zero_fraction_no_penalty(self):
        self.assertEqual(operations.parallel_penalty(4, 0.0), 1.0)
        self.assertEqual(operations.parallel_penalty(8, 0.0), 1.0)

    def test_documented_value(self):
        # 1 + (4-1)*0.8*0.25 = 1.6
        self.assertAlmostEqual(
            operations.parallel_penalty(4, 0.8), 1.6, places=9
        )
        self.assertGreater(operations.parallel_penalty(4, 0.8), 1.0)

    def test_zero_workers_clamped(self):
        self.assertEqual(operations.parallel_penalty(0, 0.8), 1.0)

    def test_negative_fraction_clamped(self):
        self.assertEqual(operations.parallel_penalty(4, -0.5), 1.0)

    def test_monotonic_in_workers_and_fraction(self):
        self.assertLess(
            operations.parallel_penalty(2, 0.5), operations.parallel_penalty(4, 0.5)
        )
        self.assertLess(
            operations.parallel_penalty(4, 0.2), operations.parallel_penalty(4, 0.9)
        )

    def test_deterministic(self):
        self.assertEqual(
            operations.parallel_penalty(4, 0.5), operations.parallel_penalty(4, 0.5)
        )


class TestAccountPenalty(unittest.TestCase):
    def test_deterministic_equal_inputs(self):
        p1 = operations.account_penalty("opA", 4, 0.5)
        p2 = operations.account_penalty("opA", 4, 0.5)
        self.assertEqual(p1, p2)

    def test_returns_new_dict_no_shared_state(self):
        p1 = operations.account_penalty("opA", 4, 0.5)
        p1["penalty_factor"] = -999.0
        p1["attribution"] = "mutated"
        p2 = operations.account_penalty("opA", 4, 0.5)
        self.assertNotEqual(p1, p2)
        self.assertIsNot(p1, p2)
        self.assertEqual(
            p2["attribution"], "opA:w4:zf0.500000"
        )

    def test_schema_and_rounding(self):
        p = operations.account_penalty("myop", 4, 0.123456789)
        self.assertEqual(p["op"], "myop")
        self.assertEqual(p["workers"], 4)
        self.assertEqual(p["zero_frac"], round(0.123456789, 6))
        self.assertEqual(
            p["penalty_factor"],
            round(operations.parallel_penalty(4, 0.123456789), 6),
        )
        self.assertEqual(p["attribution"], "myop:w4:zf0.123457")

    def test_distinct_inputs_distinct_attribution(self):
        pa = operations.account_penalty("opA", 4, 0.5)
        pb = operations.account_penalty("opB", 4, 0.5)
        pc = operations.account_penalty("opA", 2, 0.5)
        self.assertNotEqual(pa["attribution"], pb["attribution"])
        self.assertNotEqual(pa["attribution"], pc["attribution"])


class TestAggregatePenalties(unittest.TestCase):
    def test_empty_list(self):
        agg = operations.aggregate_penalties([])
        self.assertEqual(agg, {"penalty_total": 0.0, "count": 0, "attribution": ""})

    def test_deterministic(self):
        ps = [
            operations.account_penalty("opA", 4, 0.5),
            operations.account_penalty("opB", 2, 0.25),
        ]
        self.assertEqual(
            operations.aggregate_penalties(ps), operations.aggregate_penalties(ps)
        )

    def test_order_invariant_attribution_sorted(self):
        pa = operations.account_penalty("opB", 4, 0.5)
        pb = operations.account_penalty("opA", 4, 0.5)
        agg1 = operations.aggregate_penalties([pa, pb])
        agg2 = operations.aggregate_penalties([pb, pa])
        self.assertEqual(agg1, agg2)
        parts = agg1["attribution"].split(";")
        self.assertEqual(parts, sorted(parts))
        self.assertEqual(agg1["count"], 2)

    def test_total_is_rounded_sum(self):
        ps = [
            operations.account_penalty("opA", 4, 0.5),
            operations.account_penalty("opB", 2, 0.25),
        ]
        agg = operations.aggregate_penalties(ps)
        expected = round(sum(p["penalty_factor"] for p in ps), 6)
        self.assertAlmostEqual(agg["penalty_total"], expected, places=9)

    def test_missing_keys_defaults(self):
        agg = operations.aggregate_penalties([{}])
        self.assertEqual(agg["penalty_total"], 1.0)
        self.assertEqual(agg["count"], 1)
        self.assertEqual(agg["attribution"], "unknown")


class TestCalibrate(unittest.TestCase):
    def test_reproducible_byte_for_byte(self):
        path = make_temp(b"\xff\xd8\xff\xe0" * 64)
        try:
            cal1 = operations.calibrate(path, 4)
            cal2 = operations.calibrate(path, 4)
            self.assertEqual(cal1, cal2)
            self.assertEqual(repr(cal1), repr(cal2))
        finally:
            os.unlink(path)

    def test_all_zero_fixture(self):
        path = make_temp(b"\x00" * 1024)
        try:
            cal = operations.calibrate(path, 4)
            self.assertEqual(cal["bytes"], 1024)
            self.assertEqual(cal["entropy"], 0.0)
            self.assertEqual(cal["op"], "single_worker_skip")
            self.assertEqual(cal["zero_frac"], 1.0)
            self.assertEqual(
                cal["penalty_factor"],
                round(operations.parallel_penalty(4, 1.0), 4),
            )
        finally:
            os.unlink(path)

    def test_all_nonzero_fixture(self):
        path = make_temp(b"\xff" * 1024)
        try:
            cal = operations.calibrate(path, 4)
            self.assertEqual(cal["entropy"], 0.0)
            self.assertEqual(cal["op"], "adaptive_parallel")
            self.assertEqual(cal["zero_frac"], 0.0)
            self.assertEqual(cal["penalty_factor"], 1.0)
        finally:
            os.unlink(path)

    def test_empty_file(self):
        path = make_temp(b"")
        try:
            cal = operations.calibrate(path, 4)
            self.assertEqual(cal["bytes"], 0)
            self.assertEqual(cal["entropy"], 0.0)
            self.assertEqual(cal["op"], "zero_skip")
            self.assertEqual(cal["zero_frac"], 0.0)
        finally:
            os.unlink(path)

    def test_rounding_precision_four_decimals(self):
        # zero_frac with repeating decimal -> rounded to 4
        data = b"\x00" * 1 + b"\xff" * 6  # 1/7 = 0.142857...
        path = make_temp(data)
        try:
            cal = operations.calibrate(path, 4)
            self.assertEqual(cal["zero_frac"], round(1 / 7, 4))
            self.assertEqual(
                cal["entropy"], round(operations.entropy(data), 4)
            )
        finally:
            os.unlink(path)

    def test_consistent_with_primitives(self):
        data = b"\x00\x01\x02\x03" * 256
        path = make_temp(data)
        try:
            cal = operations.calibrate(path, 4)
            self.assertEqual(cal["op"], operations.select_op(data))
            self.assertEqual(
                cal["entropy"], round(operations.entropy(data), 4)
            )
        finally:
            os.unlink(path)

    def test_workers_affect_penalty_only(self):
        path = make_temp(b"\x00" * 512 + b"\xff" * 512)
        try:
            c1 = operations.calibrate(path, 1)
            c4 = operations.calibrate(path, 4)
            self.assertEqual(c1["op"], c4["op"])
            self.assertEqual(c1["entropy"], c4["entropy"])
            self.assertEqual(c1["zero_frac"], c4["zero_frac"])
            self.assertLessEqual(c1["penalty_factor"], c4["penalty_factor"])
        finally:
            os.unlink(path)


class TestAlignedBuffer(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(operations._aligned_buffer(b""), b"")

    def test_exact_multiples_pass(self):
        for n in (512, 1024, 512 * 10):
            data = b"x" * n
            self.assertEqual(operations._aligned_buffer(data), data)

    def test_misaligned_returns_none(self):
        for n in (1, 511, 513, 100, 1000):
            self.assertIsNone(operations._aligned_buffer(b"x" * n))

    def test_align_constant_is_512(self):
        self.assertEqual(operations.DIRECT_IO_ALIGN, 512)


class TestOpenDirectFallback(unittest.TestCase):
    """Fallback path when Direct I/O is not configured (default env)."""

    def test_not_configured_reason(self):
        if operations.DIRECT_IO_ENABLED:
            self.skipTest("DIRECT_IO_ENABLED is set; fallback test N/A")
        path = make_temp(b"hello-direct-io" * 100)
        try:
            handle, used_direct, reason = operations.open_direct(path, "rb")
            try:
                self.assertFalse(used_direct)
                self.assertEqual(reason, "direct_io_not_configured")
                self.assertEqual(handle.read(len(b"hello-direct-io" * 100)),
                                 b"hello-direct-io" * 100)
            finally:
                handle.close()
        finally:
            os.unlink(path)

    def test_tuple_shape(self):
        if operations.DIRECT_IO_ENABLED:
            self.skipTest("DIRECT_IO_ENABLED is set; fallback test N/A")
        path = make_temp(b"abc" * 200)
        try:
            result = operations.open_direct(path, "rb")
            self.assertEqual(len(result), 3)
            handle, used_direct, reason = result
            try:
                self.assertIsInstance(used_direct, bool)
                self.assertTrue(hasattr(handle, "read"))
            finally:
                handle.close()
        finally:
            os.unlink(path)

    def test_missing_file_raises_not_silent(self):
        missing = os.path.join(
            tempfile.gettempdir(), "v6ops_definitely_missing_12345.bin"
        )
        if os.path.exists(missing):
            os.unlink(missing)
        with self.assertRaises((FileNotFoundError, OSError)):
            operations.open_direct(missing, "rb")


class TestOpenDirectConfigured(unittest.TestCase):
    """Configured path via monkeypatched DIRECT_IO_ENABLED (no prod change)."""

    def setUp(self):
        self._orig = operations.DIRECT_IO_ENABLED
        operations.DIRECT_IO_ENABLED = True

    def tearDown(self):
        operations.DIRECT_IO_ENABLED = orig = self._orig
        operations.DIRECT_IO_ENABLED = orig

    def test_large_aligned_file_uses_direct(self):
        path = make_temp(b"A" * 2048)
        try:
            handle, used_direct, reason = operations.open_direct(path, "rb")
            try:
                self.assertTrue(used_direct)
                self.assertIsNone(reason)
                self.assertEqual(handle.read(4), b"AAAA")
            finally:
                handle.close()
        finally:
            os.unlink(path)

    def test_small_file_falls_back_alignment_mismatch(self):
        path = make_temp(b"tiny")
        try:
            handle, used_direct, reason = operations.open_direct(path, "rb")
            try:
                self.assertFalse(used_direct)
                self.assertEqual(reason, "buffer_alignment_mismatch")
            finally:
                handle.close()
        finally:
            os.unlink(path)

    def test_text_mode_never_direct(self):
        path = make_temp(b"B" * 2048)
        try:
            handle, used_direct, reason = operations.open_direct(path, "r")
            try:
                self.assertFalse(used_direct)
                self.assertEqual(reason, "buffered_fallback")
            finally:
                handle.close()
        finally:
            os.unlink(path)


class TestIntegration(unittest.TestCase):
    def test_calibrate_penalty_pipeline_deterministic(self):
        data = b"\x00" * 300 + b"\xff" * 700
        path = make_temp(data)
        try:
            cal = operations.calibrate(path, 4)
            rec = operations.account_penalty(cal["op"], 4, cal["zero_frac"])
            agg = operations.aggregate_penalties([rec, rec])
            rec2 = operations.account_penalty(cal["op"], 4, cal["zero_frac"])
            agg2 = operations.aggregate_penalties([rec2, rec2])
            self.assertEqual(agg, agg2)
            self.assertEqual(agg["count"], 2)
        finally:
            os.unlink(path)

    def test_open_direct_content_matches_calibrate(self):
        data = b"\xff\xd8\xff\xe0" * 128  # 512 bytes, aligned
        path = make_temp(data)
        orig = operations.DIRECT_IO_ENABLED
        operations.DIRECT_IO_ENABLED = True
        try:
            handle, used_direct, reason = operations.open_direct(path, "rb")
            try:
                content = handle.read()
            finally:
                handle.close()
            self.assertEqual(content, data)
            self.assertEqual(operations.select_op(content),
                             operations.calibrate(path, 4)["op"])
        finally:
            operations.DIRECT_IO_ENABLED = orig
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
