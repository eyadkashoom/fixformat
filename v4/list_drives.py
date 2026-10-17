#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
list_drives.py - list physical disks and logical drives with sizes (read-only).
Use the printed path as TARGET inside raw_scan.py.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import raw_scan  # noqa: E402

GB = 1024.0 ** 3


def main():
    print("Physical disks (copy one of these into TARGET in raw_scan.py):")
    found = 0
    denied = 0
    for i in range(16):
        target = "\\\\.\\PhysicalDrive%d" % i
        try:
            f = open(target, "rb", buffering=0)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        try:
            size = raw_scan.get_device_size(f)
        finally:
            f.close()
        found += 1
        print("  %-22s %10.2f GB  (%d bytes)" % (target, size / GB, size))
    if not found and denied:
        print("  (access denied - run this script as Administrator)")

    print()
    print("Logical drives (TARGET form:  \\\\.\\X: ):")
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        path = letter + ":\\"
        if not os.path.exists(path):
            continue
        try:
            usage = shutil.disk_usage(path)
            print("  %-6s %10.2f GB total, %10.2f GB free"
                  % (path, usage.total / GB, usage.free / GB))
        except OSError as exc:
            print("  %-6s %s" % (path, exc))


if __name__ == "__main__":
    main()
