#!/usr/bin/env python3
import validators, assembly
# Validators: actionable errors + boundary cases
for fmt, data, desc in [
    ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00", "jpeg/valid-min-like"),
    ("jpeg", b"BAD", "jpeg/no-header"),
    ("jpeg", b"\xff\xd8\x00\x00", "jpeg/truncated"),
    ("png", b"\x89PNG\r\n\x1a\nIHDR\x00\x00\x00\x01", "png/header-only"),
    ("png", b"NOTPNG", "png/no-signature"),
    ("pdf", b"%PDF-1.4\n1 0 obj\nstartxref", "pdf/valid-like"),
    ("pdf", b"not pdf", "pdf/no-header"),
    ("zip", b"PK\x03\x04" + b"\x00"*20 + b"PK\x05\x06" + b"\x00"*18, "zip/eocd"),
    ("sqlite", b"SQLite format 3\x00" + b"\x00"*100, "sqlite/header"),
    ("sqlite", b"\x00\x00\x00", "sqlite/no-magic"),
]:
    r = validators.validate(data, fmt)
    print(f"{desc} fmt={fmt} validated={r['validated']} conf={r['confidence']} error={r['error']!r}")
# Assembly
frags = [b"FRAG1", b"FRAG2"]
r = assembly.assemble(frags)
print(f"2-frags fragments={r['fragments']} gap_bytes={r['gap_bytes']} conf={r['confidence']} len_assembled={len(r['assembled'])}")
r1 = assembly.assemble([b"ONLY"])
print(f"1-frag fragments={r1['fragments']} conf={r1['confidence']}")
r0 = assembly.assemble([])
print(f"0-frags error={r0['error']!r}")
