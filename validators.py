#!/usr/bin/env python3
"""v7 validators - format validation for JPEG/PNG/PDF/ZIP/SQLite."""
import struct
import zlib


def validate(data, fmt, path_hint=""):
    r = {"validated": False, "confidence": 0.0, "validator": fmt,
         "fragments": 1, "gap_bytes": 0, "error": ""}
    if not data:
        r["error"] = "empty data"
        return r
    try:
        if fmt == "jpeg":
            return _validate_jpeg(data, r)
        elif fmt == "png":
            return _validate_png(data, r)
        elif fmt == "pdf":
            return _validate_pdf(data, r)
        elif fmt == "zip":
            return _validate_zip(data, r)
        elif fmt == "sqlite":
            return _validate_sqlite(data, r)
        else:
            r["error"] = "unknown format: %s" % fmt
    except Exception as exc:
        r["error"] = "validator exception (%s): %s" % (fmt, exc)
    return r


def _validate_jpeg(data, r):
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        r["error"] = "JPEG missing FFD8 SOI marker"
        return r
    if b"\xff\xd9" not in data:
        r["error"] = "JPEG missing FFD9 EOI marker (truncated)"
        return r
    has_app = (b"\xff\xe0" in data) or (b"\xff\xe1" in data)
    has_sof = b"\xff\xc0" in data
    pos = 2
    n = len(data)
    while pos < n - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > n:
            r["error"] = "JPEG truncated (marker length missing)"
            return r
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        if seg_len >= 2 and pos + seg_len <= n:
            pos = pos + seg_len
        else:
            # Malformed/minimal segment: stop walking; EOI presence is the
            # hard requirement (already verified above).
            break
    r["validated"] = True
    if has_app and has_sof:
        r["confidence"] = 0.95
    elif has_app:
        r["confidence"] = 0.92
    else:
        r["confidence"] = 0.88
    return r


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _validate_png(data, r):
    if len(data) < 8 or data[:8] != _PNG_SIG:
        r["error"] = "PNG missing signature/header"
        return r
    if b"IHDR" not in data or b"IEND" not in data:
        r["error"] = "PNG missing IHDR/IEND chunk (corrupt)"
        return r
    pos = 8
    n = len(data)
    crc_checked = False
    crc_ok = True
    while pos < n:
        if pos + 12 > n:
            break
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if len(ctype) != 4 or not all(32 <= b < 127 for b in ctype):
            break
        data_start = pos + 8
        data_end = data_start + length
        crc_pos = data_end
        if crc_pos + 4 > n:
            break
        stored_crc = struct.unpack(">I", data[crc_pos:crc_pos + 4])[0]
        computed = zlib.crc32(ctype + data[data_start:data_end]) & 0xFFFFFFFF
        crc_checked = True
        if stored_crc != computed:
            crc_ok = False
        pos = crc_pos + 4
        if ctype == b"IEND":
            break
    if crc_checked and not crc_ok:
        r["error"] = "PNG chunk CRC mismatch (corrupt)"
        return r
    r["validated"] = True
    r["confidence"] = 0.93
    return r


def _validate_pdf(data, r):
    if b"%PDF" not in data:
        r["error"] = "PDF missing %PDF header"
        return r
    idx = data.find(b"startxref")
    if idx == -1:
        r["error"] = "PDF missing startxref (no xref table)"
        return r
    tail = data[idx + len(b"startxref"):]
    i = 0
    while i < len(tail) and tail[i:i + 1] in (b" ", b"\t", b"\r", b"\n"):
        i += 1
    num_bytes = b""
    while i < len(tail) and tail[i:i + 1].isdigit():
        num_bytes += tail[i:i + 1]
        i += 1
    offset_verified = False
    if num_bytes:
        try:
            off = int(num_bytes)
            if 0 <= off < len(data):
                window = data[off:off + 16]
                if b"xref" in window or b"trailer" in window or b"obj" in window:
                    offset_verified = True
        except ValueError:
            pass
    r["validated"] = True
    r["confidence"] = 0.90 if offset_verified else 0.85
    return r


_EOCD_SIG = b"PK\x05\x06"


def _validate_zip(data, r):
    if len(data) < 4:
        r["error"] = "ZIP too short"
        return r
    search_start = max(0, len(data) - (65536 + 22))
    idx = data.rfind(_EOCD_SIG, search_start)
    if idx == -1:
        r["error"] = "ZIP missing EOCD (empty-eocd corrupt)"
        return r
    if idx + 22 > len(data):
        r["error"] = "ZIP missing EOCD (empty-eocd corrupt)"
        return r
    comment_len = struct.unpack("<H", data[idx + 20:idx + 22])[0]
    if idx + 22 + comment_len != len(data):
        r["error"] = "ZIP missing EOCD (empty-eocd corrupt)"
        return r
    r["validated"] = True
    r["confidence"] = 0.89
    return r


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _validate_sqlite(data, r):
    magic = _SQLITE_MAGIC
    if len(data) < len(magic) or data[:len(magic)] != magic:
        r["error"] = "SQLite missing 'SQLite format 3\0' header"
        return r
    if len(data) >= 20:
        raw_ps = struct.unpack(">H", data[16:18])[0]
        page_size = 65536 if raw_ps == 1 else raw_ps
        if raw_ps != 0:
            if page_size < 512 or page_size > 65536 or (page_size & (page_size - 1)) != 0:
                r["error"] = "SQLite invalid page-size %d" % page_size
                return r
    r["validated"] = True
    r["confidence"] = 0.91
    return r
