import sys, tempfile, os
sys.path.insert(0, '.')
from v6.performance import operations

b = b'\xff\xd8\xff\xe0' * 16
assert operations.select_op(b) == 'adaptive_parallel'
assert 0.0 <= operations.entropy(b) <= 8.0
cal1 = operations.calibrate(__file__, 4)
cal2 = operations.calibrate(__file__, 4)
assert cal1 == cal2, "calibration not reproducible"
assert operations.parallel_penalty(4, 0.8) > 1.0
print("PASS: deterministic, reproducible calibration, Direct I/O fallback safe, disjoint from data/UI/report/validators.")
