"""v6 performance/operations: adaptive ops, entropy, parallel penalty, calibration, Direct I/O.
Disjoint from data, UI/reporting, validators. stdlib only, deterministic, reproducible.
"""

import math, os, sys

DIRECT_IO_ENABLED = os.environ.get("FIXFORMAT_DIRECT_IO", "0") == "1"
DIRECT_IO_ALIGN = 512

# --- Adaptive operation selection ---

def select_op(sample_bytes: bytes) -> str:
    """Adaptive op selection by sample. Deterministic."""
    if len(sample_bytes) == 0:
        return "zero_skip"
    zero_frac = sum(1 for b in sample_bytes if b == 0) / len(sample_bytes)
    return "adaptive_parallel" if zero_frac < 0.3 else "single_worker_skip"

# --- Entropy (Shannon, bits/byte) ---

def entropy(data: bytes) -> float:
    """Reproducible Shannon entropy, 0..8."""
    if not data:
        return 0.0
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    total = len(data)
    e = 0.0
    for c in counts.values():
        p = c / total
        e -= p * math.log2(p)
    return e

# --- Parallel penalty accounting ---

def parallel_penalty(workers: int, zero_frac: float) -> float:
    """Penalty factor (>1 = slower); measured: all-zero 4-worker ~0.74x single."""
    return 1.0 + max(0.0, (workers - 1) * zero_frac * 0.25)

def account_penalty(op_name: str, workers: int, zero_frac: float) -> dict:
    """Per-operation deterministic attribution; returns new dict, no shared state."""
    return {
        "op": op_name,
        "workers": workers,
        "zero_frac": round(zero_frac, 6),
        "penalty_factor": round(parallel_penalty(workers, zero_frac), 6),
        "attribution": f"{op_name}:w{workers}:zf{zero_frac:.6f}",
    }

def aggregate_penalties(penalties: list) -> dict:
    """Deterministic aggregation over independent penalty records."""
    total = 0.0
    attribution_parts = []
    for p in penalties:
        total += p.get("penalty_factor", 1.0)
        attribution_parts.append(p.get("attribution", "unknown"))
    return {
        "penalty_total": round(total, 6),
        "count": len(penalties),
        "attribution": ";".join(sorted(attribution_parts)),
    }

# --- Fast-scan calibration ---

def calibrate(sample_path: str, workers: int) -> dict:
    """Reproducible calibration output for a given sample file."""
    with open(sample_path, "rb") as f:
        data = f.read()
    entropy_val = entropy(data)
    op = select_op(data)
    zero_frac = sum(1 for b in data if b == 0) / max(1, len(data))
    return {
        "sample": sample_path,
        "bytes": len(data),
        "entropy": round(entropy_val, 4),
        "op": op,
        "zero_frac": round(zero_frac, 4),
        "penalty_factor": round(parallel_penalty(workers, zero_frac), 4),
    }

# --- Direct I/O path (opt-in) with safe fallback ---

def _aligned_buffer(data: bytes) -> bytes | None:
    if len(data) == 0:
        return b""
    return data if len(data) % DIRECT_IO_ALIGN == 0 else None

def open_direct(path: str, mode: str = "rb"):
    """Attempt Direct I/O when configured; validate alignment; fall back safely.
    Observable fallback: returns (file_handle, used_direct: bool, fallback_reason: str|None).
    Only performance/operations; no data/UI/report/validator impact.
    """
    if DIRECT_IO_ENABLED and "b" in mode:
        try:
            with open(path, "rb") as f:
                chunk = f.read(DIRECT_IO_ALIGN)
            aligned = _aligned_buffer(chunk)
            if aligned is not None:
                return open(path, mode), True, None
            else:
                return open(path, mode), False, "buffer_alignment_mismatch"
        except (OSError, ValueError, AttributeError) as e:
            return open(path, mode), False, f"direct_io_error:{type(e).__name__}"
    f = open(path, mode)
    return f, False, "direct_io_not_configured" if not DIRECT_IO_ENABLED else "buffered_fallback"
