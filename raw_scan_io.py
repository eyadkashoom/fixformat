"""v7 Phase 2 split — device/file I/O helpers.

Re-exports the I/O helpers from raw_scan so callers can import them from a
dedicated module. The canonical implementation still lives in raw_scan.py to
avoid a risky move of 4000+ lines; new code should prefer this module.
"""
from raw_scan import (  # noqa: F401
    read_exact, read_window, zero_block, get_device_size,
    list_physical_drives, list_logical_drives,
    smart_extend_text, CachedReader,
)
