#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Peak resident memory of this process, stdlib only, all platforms.

Used by the performance gate and the size-ladder benchmark. tracemalloc is
deliberately not used for timing runs: it instruments every allocation and made
the same 1 MiB scan take 12 s instead of 1 s, which would have been reported as
the tool's throughput.
"""

import ctypes
import sys


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_psapi = None
_kernel32 = None


def _init_windows():
    global _psapi, _kernel32
    if _psapi is not None:
        return
    _kernel32 = ctypes.WinDLL("kernel32")
    _psapi = ctypes.WinDLL("psapi")
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC),
                                            ctypes.c_ulong]
    _psapi.GetProcessMemoryInfo.restype = ctypes.c_int


def peak_rss_mib():
    """Peak resident set of this process in MiB (0.0 if unavailable)."""
    if sys.platform == "win32":
        _init_windows()
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(),
                                       ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1048576.0
        return 0.0
    try:
        import resource
    except ImportError:
        return 0.0
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss / (1048576.0 if sys.platform == "darwin" else 1024.0)
