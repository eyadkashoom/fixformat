#!/usr/bin/env python3
"""CLI entry for K5 report rendering — deterministic, stdlib-only."""
import sys
sys.path.insert(0, ".")

from k5_report import export

FIXTURE = [
    {"offset": 1024, "length": 8192, "label": "JPEG", "ext": ".jpg", "sha256": "a"*64, "validated": 1, "confidence": 0.92, "fragments": 1, "gap_bytes": 0, "name_guess": "img_01"},
    {"offset": 2048, "length": 4096, "label": "PNG", "ext": ".png", "sha256": "b"*64, "validated": 0, "confidence": 0.55, "fragments": 2, "gap_bytes": 512, "name_guess": "unknown"},
]

if __name__ == "__main__":
    for fmt in ("md", "csv", "json"):
        print(f"=== {fmt} ===")
        print(export(FIXTURE, fmt))
