"""v7 Phase 2 split — core scanning logic.

Re-exports the core scan entry points from raw_scan.
"""
from raw_scan import (  # noqa: F401
    scan, carve_phase, carve_window, build_patterns, match_block,
    build_pat_list, _reportable,
)
