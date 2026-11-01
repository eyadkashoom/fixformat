#!/usr/bin/env python3
"""v7 validator improvements (Phase 1.10) - new test cases."""
import os, sys, struct, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validators


def ck(cond, msg):
    if not cond:
        print("FAIL:", msg)
        raise SystemExit(1)
    print("ok:", msg)


# 1. ZIP with comment (EOCD search from end)
comment = b"hello comment"
eocd = b"PK\x05\x06" + b"\x00" * 16 + struct.pack("<H", len(comment))
zip_comment = b"PK\x03\x04" + b"\x00" * 20 + eocd + comment
r = validators.validate(zip_comment, "zip")
ck(r["validated"] is True, "ZIP with comment validates")

# ZIP missing EOCD
r = validators.validate(b"PK\x03\x04" + b"\x00" * 10, "zip")
ck(r["validated"] is False, "ZIP missing EOCD fails")

# 2. PNG CRC corrupt
sig = b"\x89PNG\r\n\x1a\n"
# Build a minimal valid IHDR chunk
ihdr_data = b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
# IEND chunk
iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
# Corrupt the CRC of IHDR
bad_ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc ^ 0xFFFFFFFF)
png_ok = sig + ihdr + iend
png_bad = sig + bad_ihdr + iend
ck(validators.validate(png_ok, "png")["validated"] is True, "PNG valid CRC passes")
ck(validators.validate(png_bad, "png")["validated"] is False, "PNG bad CRC fails")
ck("CRC" in validators.validate(png_bad, "png")["error"], "PNG bad CRC error mentions CRC")

# 3. SQLite page-size validation
magic = b"SQLite format 3\x00"
# valid page size 4096 at offset 18
hdr = bytearray(magic + b"\x00" * (20 - len(magic)))
struct.pack_into(">H", hdr, 16, 4096)
sql_ok = bytes(hdr) + b"\x00" * 100
r = validators.validate(sql_ok, "sqlite")
ck(r["validated"] is True, "SQLite valid page-size passes")
# invalid page size 3
hdr2 = bytearray(magic + b"\x00" * (20 - len(magic)))
struct.pack_into(">H", hdr2, 16, 3)
sql_bad = bytes(hdr2) + b"\x00" * 100
r = validators.validate(sql_bad, "sqlite")
ck(r["validated"] is False, "SQLite invalid page-size fails")

# 4. PDF xref offset
pdf_good = b"%PDF-1.4\nxref\n0 1\ntrailer\nstartxref\n5\n%%EOF"
r = validators.validate(pdf_good, "pdf")
ck(r["validated"] is True, "PDF valid xref passes")
print("ALL v7 validator tests PASSED")
