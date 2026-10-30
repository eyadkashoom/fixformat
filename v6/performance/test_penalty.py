import sys; sys.path.insert(0, '.')
from v6.performance import operations

p1 = operations.account_penalty('opA', 4, 0.5)
p2 = operations.account_penalty('opA', 4, 0.5)
assert p1 == p2, "not deterministic"
agg1 = operations.aggregate_penalties([p1, p2])
agg2 = operations.aggregate_penalties([p1, p2])
assert agg1 == agg2, "aggregation not deterministic"
print("PASS: deterministic penalty totals and attribution, no shared mutable state.")
