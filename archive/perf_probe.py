import sys
sys.path.insert(0, '.')
from v6.performance import operations

# Test select_op
print('select_op zero:', operations.select_op(b''))
print('select_op adaptive_parallel:', operations.select_op(b'\x00'*10))
print('select_op single_worker_skip:', operations.select_op(b'\xff'*10))
print('select_op adaptive_parallel (zeros < 0.3):', operations.select_op(b'\x00'*2 + b'\xff'*8))

# Test entropy
b = b'hello world'
print('entropy hello world:', operations.entropy(b))
print('entropy all zero:', operations.entropy(b'\x00'*100))
print('entropy empty:', operations.entropy(b''))

# Test parallel_penalty
print('parallel_penalty 1 worker zeros 0:', operations.parallel_penalty(1, 0.0))
print('parallel_penalty 4 workers zeros 1.0:', operations.parallel_penalty(4, 1.0))
print('parallel_penalty 4 workers zeros 0.0:', operations.parallel_penalty(4, 0.0))

# Test account_penalty
print('account_penalty:', operations.account_penalty('opA', 4, 0.5))

# Test aggregate_penalties
p = [operations.account_penalty('opA', 4, 0.5), operations.account_penalty('opB', 2, 0.0)]
print('aggregate_penalties:', operations.aggregate_penalties(p))

# Test calibrate
import tempfile, os
with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
    f.write(b'\x00'*1000 + b'\xff'*500)
    path = f.name
try:
    cal = operations.calibrate(path, 4)
    print('calibrate:', cal)
finally:
    os.unlink(path)

# Test Direct I/O
import os
os.environ['FIXFORMAT_DIRECT_IO'] = '0'
print('DIRECT_IO_ENABLED:', operations.DIRECT_IO_ENABLED)
print('open_direct not configured:', operations.open_direct(':test:', 'rb'))
os.unlink(path)
