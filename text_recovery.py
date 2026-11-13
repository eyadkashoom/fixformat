#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_recovery.py - text recovery pipeline (Phase 1: NTFS $MFT parser).

Independent module: it does not import raw_scan, and it never writes to the
source it reads. Phase 1 reads the NTFS $MFT of an image file or a raw
volume, finds FILE records whose in-use flag is clear (deleted), and returns
their original names, sizes and cluster runs so their bytes can be carved.

Why this is a separate module
-----------------------------
Text files have no magic signature, so signature carving can never find them.
The $MFT is the one place where a deleted text file still has its original
name, its size and (often) the cluster chain that holds its bytes. Phase 1
reads that metadata first; later phases add strings extraction, n-gram
classification and the traditional carving path.

Later phases
------------
* FAT32 directory entries (NTFS is first)
* strings extraction (runs of printable text behind entropy/ratio gates)
* n-gram classification (real text vs random data)
* signature carving (already in raw_scan.py)

Public API
----------
    parse_boot_sector(vbr)          -> boot dict (ValueError when not NTFS)
    mft_layout(source, boot)        -> layout dict (runs, segments, count)
    read_record(source, layout, n)  -> record bytes or None
    iter_mft_records(source, layout, limit=None)
    parse_record(record)            -> record dict or None
    parse_attributes(record)        -> [attribute dict, ...]
    parse_runlist(data)             -> [(lcn_or_None, cluster_count), ...]
    parse_attribute_list(value)     -> [entry dict, ...]
    scan_deleted(source, boot, ...) -> [candidate dict, ...]
    read_candidate_data(source, layout, cand, max_bytes=None)
    carve_candidate(source, layout, cand, out_dir, max_bytes=None)
    recover_mft(source_path, ...)   -> summary dict
    device_path(drive)              -> Windows raw-device path
    main(argv)                      -> exit code

Command line (read-only):
    python text_recovery.py <image | device> [--out DIR] [--limit N] [--no-carve]

The source is opened "rb" and only seek/read are used. Output goes only to the
directory named by --out (or the caller's out_dir).
"""

import os
import sys
import time

BS = chr(92)          # one backslash, built without a literal escape
NL = chr(10)
NUL_CHAR = chr(0)

VERSION = "7.0.1"
SECTOR = 512
NTFS_OEM = b"NTFS    "
MFT_MAGIC = b"FILE"
END_OF_ATTRIBUTES = 0xFFFFFFFF

# attribute types (NTFS on-disk values)
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x18
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0

# FILE record flags
RECORD_IN_USE = 0x0001
RECORD_IS_DIRECTORY = 0x0002

# attribute flags
ATTR_COMPRESSED = 0x0001
ATTR_ENCRYPTED = 0x4000
ATTR_SPARSE = 0x8000

# $FILE_NAME namespaces
NS_POSIX = 0
NS_WIN32 = 1
NS_DOS = 2
NS_WIN32_DOS = 3

DEFAULT_MAX_CARVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CARVE_FILES = 1000
MFT_READ_CHUNK = 4 * 1024 * 1024

__all__ = [
    "Source", "parse_boot_sector", "mft_layout", "read_record",
    "iter_mft_records", "parse_record", "parse_attributes", "parse_runlist",
    "parse_attribute_list", "scan_deleted", "read_candidate_data",
    "carve_candidate", "recover_mft", "device_path", "apply_fixup", "main",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _u8(buf, off):
    return buf[off] if 0 <= off < len(buf) else 0


def _u16(buf, off):
    return int.from_bytes(buf[off:off + 2], "little") if 0 <= off and off + 2 <= len(buf) else 0


def _u32(buf, off):
    return int.from_bytes(buf[off:off + 4], "little") if 0 <= off and off + 4 <= len(buf) else 0


def _u64(buf, off):
    return int.from_bytes(buf[off:off + 8], "little") if 0 <= off and off + 8 <= len(buf) else 0


def _s8(value):
    return value - 256 if value >= 128 else value


def _ensure_utf8_streams():
    """Never crash on Arabic output under a legacy console codepage."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def device_path(drive):
    """Build a Windows raw-device path: two backslashes, dot, backslash, drive.

    The path is assembled from chr(92) so the source needs no backslash
    literal and stays readable on every platform.
    """
    drive = str(drive)
    if drive.startswith(BS) or "/" in drive or drive.startswith("\\\\?\\"):
        return drive
    return BS * 2 + "." + BS + drive


def is_device(path):
    return str(path).startswith(BS * 2 + ".") or str(path).startswith(BS * 2 + "?" + BS)


def _fmt_bytes(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024 or unit == "TiB":
            return "%.2f %s" % (value, unit)


def _filetime(value):
    """FILETIME (100 ns since 1601) -> UTC text, or "" when zero/invalid."""
    if not value:
        return ""
    try:
        seconds = value / 10000000.0 - 11644473600
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# read-only source
# ---------------------------------------------------------------------------

class Source(object):
    """Read-only view of an image file or a raw device.

    `offset` shifts every read (a partition inside a whole-disk image); the
    boot sector is then read at `offset`, not at byte 0. `limit` caps how many
    bytes from that offset may be read (the partition length).
    """

    def __init__(self, path, offset=0, limit=None):
        self.path = str(path)
        self.offset = int(offset)
        self.limit = None if limit is None else int(limit)
        self.f = open(self.path, "rb")
        self.size = self._probe_size()
        self.reads = 0
        self.bytes_read = 0
        self.errors = []

    def _probe_size(self):
        try:
            self.f.seek(0, os.SEEK_END)
            end = self.f.tell()
            self.f.seek(0)
            if end > self.offset:
                size = end - self.offset
                if self.limit is not None:
                    size = min(size, self.limit)
                return size
        except OSError:
            pass
        try:
            st = os.fstat(self.f.fileno())
            if st.st_size > self.offset:
                size = st.st_size - self.offset
                if self.limit is not None:
                    size = min(size, self.limit)
                return size
        except OSError:
            pass
        size = None if is_device(self.path) else 0
        if size is not None and self.limit is not None:
            size = min(size, self.limit)
        return size

    def read(self, offset, length):
        if offset is None or length is None:
            return b""
        offset = int(offset)
        length = int(length)
        if offset < 0 or length <= 0:
            return b""
        if self.size is not None:
            if offset >= self.size:
                return b""
            length = min(length, self.size - offset)
        file_offset = self.offset + offset
        try:
            self.f.seek(file_offset)
        except OSError as exc:
            self.errors.append((file_offset, str(exc)))
            return b""
        chunks = []
        got = 0
        while got < length:
            try:
                block = self.f.read(length - got)
            except OSError as exc:
                self.errors.append((offset + got, str(exc)))
                break
            if not block:
                break
            chunks.append(block)
            got += len(block)
            if got < length:
                try:
                    self.f.seek(offset + got)
                except OSError as exc:
                    self.errors.append((offset + got, str(exc)))
                    break
        self.reads += 1
        data = chunks[0] if len(chunks) == 1 else b"".join(chunks)
        self.bytes_read += len(data)
        return data

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


# ---------------------------------------------------------------------------
# boot sector
# ---------------------------------------------------------------------------

def parse_boot_sector(vbr):
    """Parse the 512-byte NTFS volume boot record.

    Raises ValueError when the OEM id is not NTFS or the BPB is implausible.
    Offsets are the on-disk ones: bytes/sector 0x0B, sectors/cluster 0x0D,
    total sectors 0x28, $MFT LCN 0x30, $MFTMirr LCN 0x38, record size 0x40
    (signed: negative means 2^abs bytes), index record size 0x44.
    """
    if vbr is None or len(vbr) < SECTOR:
        raise ValueError("boot sector too short: %d byte(s), need %d"
                         % (0 if not vbr else len(vbr), SECTOR))
    if vbr[3:11] != NTFS_OEM:
        raise ValueError("not an NTFS volume: OEM id is %r, expected %r"
                         % (vbr[3:11], NTFS_OEM))
    bytes_per_sector = _u16(vbr, 0x0B)
    sectors_per_cluster = _u8(vbr, 0x0D)
    if bytes_per_sector < 512 or bytes_per_sector > 4096 or bytes_per_sector % 512:
        raise ValueError("implausible bytes-per-sector %d" % bytes_per_sector)
    if sectors_per_cluster == 0:
        raise ValueError("implausible sectors-per-cluster 0")
    cluster_size = bytes_per_sector * sectors_per_cluster
    raw_record = _u8(vbr, 0x40)
    signed = _s8(raw_record)
    if signed < 0:
        record_size = 1 << (-signed)
    elif signed > 0:
        record_size = signed * cluster_size
    else:
        raise ValueError("implausible MFT record size byte 0")
    if record_size < SECTOR or record_size > 64 * 1024:
        raise ValueError("implausible MFT record size %d" % record_size)
    return {
        "bytes_per_sector": bytes_per_sector,
        "sectors_per_cluster": sectors_per_cluster,
        "cluster_size": cluster_size,
        "total_sectors": _u64(vbr, 0x28),
        "mft_lcn": _u64(vbr, 0x30),
        "mftmirr_lcn": _u64(vbr, 0x38),
        "record_size": record_size,
        "index_record_size_raw": _s8(_u8(vbr, 0x44)),
        "volume_serial": _u64(vbr, 0x48),
        "checksum": _u32(vbr, 0x50),
    }


# ---------------------------------------------------------------------------
# MFT records
# ---------------------------------------------------------------------------

def apply_fixup(record):
    """Restore the USA-protected last two bytes of every 512-byte sector."""
    if record is None or len(record) < 0x30:
        return record
    usa_off = _u16(record, 4)
    usa_size = _u16(record, 6)
    if usa_size < 2 or usa_off + usa_size * 2 > len(record):
        return record
    out = bytearray(record)
    for i in range(1, usa_size):
        pos = i * SECTOR - 2
        if pos + 2 <= len(out):
            out[pos:pos + 2] = record[usa_off + i * 2: usa_off + i * 2 + 2]
    return bytes(out)


def parse_record(record):
    """Parse one FILE record into a dict, or None when it is not a FILE record."""
    if record is None or len(record) < 0x30:
        return None
    if record[0:4] != MFT_MAGIC:
        return None
    fixed = apply_fixup(record)
    first_attr = _u16(fixed, 0x14)
    used = _u32(fixed, 0x18)
    alloc = _u32(fixed, 0x1C) or len(fixed)
    flags = _u16(fixed, 0x16)
    limit = min(alloc, len(fixed))
    attrs = parse_attributes(fixed, first_attr, limit)
    return {
        "record_number": _u32(fixed, 0x2C),
        "sequence": _u16(fixed, 0x10),
        "flags": flags,
        "in_use": bool(flags & RECORD_IN_USE),
        "is_directory": bool(flags & RECORD_IS_DIRECTORY),
        "used_size": used,
        "allocated_size": alloc,
        "base_record": _u64(fixed, 0x20),
        "attributes": attrs,
    }


def parse_attributes(record, first_attr=None, limit=None):
    """Walk the attribute array of a (fixup-applied) FILE record."""
    if record is None:
        return []
    if first_attr is None:
        first_attr = _u16(record, 0x14)
    if limit is None:
        limit = len(record)
    limit = min(limit, len(record))
    attrs = []
    off = first_attr
    while off + 8 <= limit:
        atype = _u32(record, off)
        alen = _u32(record, off + 4)
        if atype == END_OF_ATTRIBUTES:
            break
        if alen < 8 or off + alen > limit:
            break
        non_resident = _u8(record, off + 8)
        name_len = _u8(record, off + 9)
        name_off = _u16(record, off + 0x0A)
        attr_flags = _u16(record, off + 0x0C)
        attr_id = _u16(record, off + 0x0E)
        name = ""
        if name_len and name_off and name_off + name_len * 2 <= off + alen:
            name = record[name_off:name_off + name_len * 2].decode("utf-16-le", "replace")
        attr = {
            "type": atype,
            "length": alen,
            "non_resident": bool(non_resident),
            "name": name,
            "flags": attr_flags,
            "id": attr_id,
            "offset": off,
        }
        if non_resident:
            runlist_off = _u16(record, off + 0x20)
            compression = _u16(record, off + 0x22)
            attr["start_vcn"] = _u64(record, off + 0x10)
            attr["last_vcn"] = _u64(record, off + 0x18)
            attr["compression_unit"] = compression
            attr["allocated_size"] = _u64(record, off + 0x28)
            attr["real_size"] = _u64(record, off + 0x30)
            attr["initialized_size"] = _u64(record, off + 0x38)
            attr["runs"] = parse_runlist(record[off + runlist_off: off + alen])
        else:
            val_len = _u32(record, off + 0x10)
            val_off = _u16(record, off + 0x14)
            if val_off + val_len <= off + alen:
                attr["value"] = record[off + val_off: off + val_off + val_len]
            else:
                attr["value"] = b""
            attr["real_size"] = val_len
        attrs.append(attr)
        off += alen
    return attrs


def parse_runlist(data):
    """Parse an NTFS run list into [(lcn_or_None, cluster_count), ...].

    A None LCN is a sparse run (a hole). The list is terminated by a zero
    header byte; a truncated header stops the walk instead of raising.
    """
    runs = []
    if not data:
        return runs
    pos = 0
    start = 0
    while pos < len(data):
        header = data[pos]
        if header == 0:
            break
        pos += 1
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0 or len_size > 8 or off_size > 8:
            break
        if pos + len_size + off_size > len(data):
            break
        count = int.from_bytes(data[pos:pos + len_size], "little")
        pos += len_size
        if off_size == 0:
            runs.append((None, count))
        else:
            delta = int.from_bytes(data[pos:pos + off_size], "little", signed=True)
            pos += off_size
            start += delta
            if start < 0:
                break
            runs.append((start, count))
    return runs


def parse_attribute_list(value):
    """Parse a resident $ATTRIBUTE_LIST value into entry dicts."""
    entries = []
    if not value:
        return entries
    off = 0
    while off + 0x1A <= len(value):
        atype = _u32(value, off)
        length = _u16(value, off + 4)
        if length < 0x1A or off + length > len(value):
            break
        name_len = _u8(value, off + 6)
        name_off = _u8(value, off + 7)
        ref = _u64(value, off + 0x10)
        name = ""
        if name_len and name_off:
            start = off + name_off
            name = value[start:start + name_len * 2].decode("utf-16-le", "replace")
        entries.append({
            "type": atype,
            "length": length,
            "name": name,
            "lowest_vcn": _u64(value, off + 8),
            "record_number": ref & 0xFFFFFFFFFFFF,
            "sequence": (ref >> 48) & 0xFFFF,
            "id": _u16(value, off + 0x18),
        })
        off += length
    return entries


# ---------------------------------------------------------------------------
# $MFT layout
# ---------------------------------------------------------------------------

def mft_layout(source, boot):
    """Locate the $MFT stream: its runs, byte segments and record count."""
    cluster_size = boot["cluster_size"]
    record_size = boot["record_size"]
    if not boot.get("mft_lcn"):
        raise ValueError("$MFT LCN is 0: the boot sector is not usable")
    raw = source.read(boot["mft_lcn"] * cluster_size, record_size)
    rec = parse_record(raw)
    if rec is None:
        raise ValueError("$MFT record 0 has no FILE magic at cluster %d"
                         % boot["mft_lcn"])
    data = None
    for attr in rec["attributes"]:
        if attr["type"] == ATTR_DATA and attr["name"] == "":
            data = attr
            break
    if data is None:
        raise ValueError("$MFT record 0 has no unnamed $DATA attribute")
    if not data["non_resident"]:
        value = data["value"]
        return {
            "resident": True,
            "data": value,
            "runs": [],
            "segments": [],
            "record_size": record_size,
            "cluster_size": cluster_size,
            "record_count": len(value) // record_size,
            "real_size": len(value),
            "allocated_size": len(value),
        }
    runs = data["runs"]
    segments = []
    pos = 0
    for lcn, count in runs:
        length = count * cluster_size
        segments.append({"lcn": lcn, "start": pos, "length": length})
        pos += length
    real_size = data["real_size"] or pos
    record_count = real_size // record_size
    return {
        "resident": False,
        "data": None,
        "runs": runs,
        "segments": segments,
        "record_size": record_size,
        "cluster_size": cluster_size,
        "record_count": record_count,
        "real_size": real_size,
        "allocated_size": data["allocated_size"],
    }


def read_record(source, layout, index):
    """Read MFT record `index` (0-based) or return None when unavailable."""
    record_size = layout["record_size"]
    if index is None or index < 0:
        return None
    if layout["resident"]:
        start = index * record_size
        if start + record_size > len(layout["data"]):
            return None
        return layout["data"][start:start + record_size]
    offset = index * record_size
    for seg in layout["segments"]:
        if seg["start"] <= offset < seg["start"] + seg["length"]:
            if seg["lcn"] is None:
                return None
            file_offset = seg["lcn"] * layout["cluster_size"] + (offset - seg["start"])
            return source.read(file_offset, record_size)
    return None


def iter_mft_records(source, layout, limit=None):
    """Yield (index, record_bytes) for every record in the $MFT.

    Reads the $MFT sequentially in large chunks instead of one seek per
    record, so a multi-million-record MFT stays fast. Sparse runs are yielded
    as zero-filled records, which parse_record() rejects as bad magic.
    """
    record_size = layout["record_size"]
    count = layout["record_count"]
    if limit is not None:
        count = min(count, max(0, int(limit)))
    if layout["resident"]:
        data = layout["data"]
        for index in range(count):
            start = index * record_size
            yield index, data[start:start + record_size]
        return
    cluster_size = layout["cluster_size"]
    index = 0
    buf = b""
    pos = 0

    def _drain():
        nonlocal index, buf, pos
        end = len(buf)
        while end - pos >= record_size and index < count:
            yield index, buf[pos:pos + record_size]
            pos += record_size
            index += 1
        if pos:
            buf = buf[pos:]
            pos = 0

    for seg in layout["segments"]:
        if index >= count:
            break
        if seg["lcn"] is None:
            remaining = seg["length"]
            while remaining > 0 and index < count:
                want = min(remaining, MFT_READ_CHUNK)
                buf += bytes(want)
                remaining -= want
                for item in _drain():
                    yield item
            continue
        file_offset = seg["lcn"] * cluster_size
        remaining = seg["length"]
        while remaining > 0 and index < count:
            want = min(remaining, MFT_READ_CHUNK)
            chunk = source.read(file_offset, want)
            if not chunk:
                remaining = 0
                break
            file_offset += len(chunk)
            remaining -= len(chunk)
            buf += chunk
            for item in _drain():
                yield item
    for item in _drain():
        yield item


# ---------------------------------------------------------------------------
# deleted-record candidates
# ---------------------------------------------------------------------------

def _pick_name(attrs):
    """Pick the best $FILE_NAME from a record's attributes."""
    best = None
    for attr in attrs:
        if attr["type"] != ATTR_FILE_NAME or attr["non_resident"]:
            continue
        value = attr.get("value") or b""
        if len(value) < 0x42:
            continue
        name_len = _u8(value, 0x40)
        namespace = _u8(value, 0x41)
        if name_len == 0 or 0x42 + name_len * 2 > len(value):
            continue
        name = value[0x42:0x42 + name_len * 2].decode("utf-16-le", "replace")
        name = name.replace(NUL_CHAR, "")
        if not name:
            continue
        rank = {NS_WIN32: 0, NS_WIN32_DOS: 1, NS_POSIX: 2, NS_DOS: 3}.get(namespace, 9)
        key = (rank, -len(name))
        if best is None or key < best[0]:
            best = (key, {
                "name": name,
                "namespace": namespace,
                "allocated_size": _u64(value, 0x28),
                "real_size": _u64(value, 0x30),
                "parent_record": _u64(value, 0x00) & 0xFFFFFFFFFFFF,
                "flags": _u32(value, 0x38),
            })
    return best[1] if best else None


def _find_data(attrs):
    """The unnamed $DATA attribute of a record (the file's content)."""
    for attr in attrs:
        if attr["type"] == ATTR_DATA and attr["name"] == "":
            return attr
    return None


def _resolve_data_from_children(source, layout, rec):
    """Follow a resident $ATTRIBUTE_LIST to a child record holding $DATA."""
    for attr in rec["attributes"]:
        if attr["type"] != ATTR_ATTRIBUTE_LIST or attr["non_resident"]:
            continue
        entries = parse_attribute_list(attr.get("value") or b"")
        for entry in entries:
            if entry["type"] != ATTR_DATA:
                continue
            child = parse_record(read_record(source, layout, entry["record_number"]))
            if child is None:
                continue
            data = _find_data(child["attributes"])
            if data is not None:
                return data, entries
    return None, []


def _std_info(attrs):
    for attr in attrs:
        if attr["type"] == ATTR_STANDARD_INFORMATION and not attr["non_resident"]:
            value = attr.get("value") or b""
            if len(value) >= 0x20:
                return {
                    "created": _filetime(_u64(value, 0)),
                    "modified": _filetime(_u64(value, 8)),
                    "record_modified": _filetime(_u64(value, 16)),
                    "accessed": _filetime(_u64(value, 24)),
                    "flags": _u32(value, 0x20),
                }
    return {}


def _candidate(source, layout, index, rec):
    name_info = _pick_name(rec["attributes"])
    data, attr_entries = _find_data(rec["attributes"]), []
    if data is None:
        data, attr_entries = _resolve_data_from_children(source, layout, rec)
    size = 0
    allocated = 0
    resident = False
    resident_data = b""
    runs = []
    data_offset = None
    carvable = False
    compressed = False
    if data is not None:
        compressed = bool(data.get("compression_unit"))
        if data["non_resident"]:
            runs = data.get("runs") or []
            size = int(data.get("real_size") or 0)
            allocated = int(data.get("allocated_size") or 0)
            for lcn, _count in runs:
                if lcn is not None:
                    data_offset = lcn * layout["cluster_size"]
                    break
            carvable = bool(runs) and not compressed
        else:
            resident = True
            resident_data = data.get("value") or b""
            size = int(data.get("real_size") or len(resident_data))
            allocated = size
            carvable = True
    if name_info is not None:
        if not size and name_info.get("real_size"):
            size = int(name_info["real_size"])
        if not allocated:
            allocated = int(name_info.get("allocated_size") or 0)
    name = name_info["name"] if name_info else ""
    namespace = name_info["namespace"] if name_info else NS_POSIX
    if not name:
        confidence = 0.2
        reason = "deleted record without a file name"
    elif carvable and size > 0:
        confidence = 0.9
        reason = "deleted record with name, size and cluster runs"
    elif carvable:
        confidence = 0.85
        reason = "deleted record with resident data"
    else:
        confidence = 0.5
        reason = "deleted record with a name but no readable data runs"
    return {
        "record_number": int(index),
        "mft_sequence": rec.get("sequence"),
        "name": name,
        "namespace": namespace,
        "deleted": not rec.get("in_use"),
        "in_use": bool(rec.get("in_use")),
        "is_directory": bool(rec.get("is_directory")),
        "size": size,
        "allocated_size": allocated,
        "resident": resident,
        "runs": [[lcn, count] for lcn, count in runs],
        "data_offset": data_offset,
        "resident_data": resident_data,
        "compressed": compressed,
        "attribute_list": bool(attr_entries),
        "parent_record": name_info.get("parent_record") if name_info else None,
        "created": _std_info(rec["attributes"]).get("created", ""),
        "modified": _std_info(rec["attributes"]).get("modified", ""),
        "carvable": carvable,
        "confidence": confidence,
        "reason": reason,
    }


def scan_deleted(source, boot, limit_records=None, include_live=False,
                 stats=None):
    """Return candidate dicts for deleted (or all) FILE records in the $MFT.

    `stats`, when given, is updated with 'records_examined' (how many MFT
    records were actually read) and 'records_deleted' (candidates found).
    """
    layout = mft_layout(source, boot)
    out = []
    examined = 0
    for index, raw in iter_mft_records(source, layout, limit_records):
        examined += 1
        rec = parse_record(raw)
        if rec is None:
            continue
        if rec["in_use"] and not include_live:
            continue
        out.append(_candidate(source, layout, index, rec))
    if stats is not None:
        stats["records_examined"] = examined
        stats["records_deleted"] = len(out)
    return out


# ---------------------------------------------------------------------------
# carving
# ---------------------------------------------------------------------------

_INVALID_NAME_CHARS = set([chr(c) for c in range(32)]) | set(["<", ">", ":", '"', "/", BS, "|", "?", "*"])
_RESERVED_NAMES = set(["CON", "PRN", "AUX", "NUL"] +
                      ["COM%d" % i for i in range(1, 10)] +
                      ["LPT%d" % i for i in range(1, 10)])


def _safe_name(name, record_number):
    cleaned = "".join("_" if ch in _INVALID_NAME_CHARS else ch for ch in str(name))
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "record_%06d.dat" % record_number
    stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned
    if stem.upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def read_candidate_data(source, layout, cand, max_bytes=None):
    """Read the bytes a candidate points at, in run order, capped by max_bytes."""
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_CARVE_BYTES
    max_bytes = int(max_bytes)
    if max_bytes <= 0 or not cand.get("carvable"):
        return b""
    size = int(cand.get("size") or 0)
    if cand.get("resident"):
        data = cand.get("resident_data") or b""
        limit = min(size, max_bytes) if size else max_bytes
        return data[:limit]
    limit = min(size, max_bytes) if size else max_bytes
    out = bytearray()
    cluster_size = layout["cluster_size"]
    for lcn, count in cand.get("runs") or []:
        if len(out) >= limit:
            break
        if lcn is None:
            out += bytes(min(count * cluster_size, limit - len(out)))
        else:
            want = min(count * cluster_size, limit - len(out))
            chunk = source.read(lcn * cluster_size, want)
            out += chunk
            if len(chunk) < want:
                break
    return bytes(out[:limit])


def carve_candidate(source, layout, cand, out_dir, max_bytes=None):
    """Write one candidate's bytes to out_dir. Returns the path or ""."""
    data = read_candidate_data(source, layout, cand, max_bytes)
    if not data:
        return ""
    os.makedirs(out_dir, exist_ok=True)
    name = _safe_name(cand.get("name") or "", cand.get("record_number", 0))
    path = os.path.join(out_dir, name)
    if os.path.exists(path):
        stem, dot, ext = name.rpartition(".")
        suffix = ".rec%06d" % cand.get("record_number", 0)
        path = os.path.join(out_dir, (stem + suffix + dot + ext) if dot else (name + suffix))
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def recover_mft(source_path, out_dir=None, limit_records=None, carve=True,
                max_carve_bytes=DEFAULT_MAX_CARVE_BYTES, offset=0,
                volume_limit=None):
    """Read an NTFS source, list deleted records, and optionally carve them.

    `offset` is the byte offset of the NTFS boot sector inside the source (0
    for a volume device or a volume image; the partition start for a whole-disk
    image). `volume_limit` caps how many bytes from that offset are readable.
    """
    summary = {
        "source": str(source_path),
        "out_dir": out_dir,
        "offset": int(offset),
        "volume_limit": None if volume_limit is None else int(volume_limit),
        "boot": None,
        "layout": None,
        "records_total": 0,
        "records_examined": 0,
        "records_deleted": 0,
        "deleted": 0,
        "candidates": [],
        "carved": [],
        "bytes_written": 0,
        "errors": [],
        "ok": False,
    }
    try:
        source = Source(source_path, offset=offset, limit=volume_limit)
    except OSError as exc:
        summary["errors"].append("cannot open %s: %s" % (source_path, exc))
        return summary
    try:
        boot = parse_boot_sector(source.read(0, SECTOR))
        summary["boot"] = {
            "bytes_per_sector": boot["bytes_per_sector"],
            "sectors_per_cluster": boot["sectors_per_cluster"],
            "cluster_size": boot["cluster_size"],
            "total_sectors": boot["total_sectors"],
            "mft_lcn": boot["mft_lcn"],
            "record_size": boot["record_size"],
            "volume_serial": boot["volume_serial"],
        }
        layout = mft_layout(source, boot)
        summary["layout"] = {
            "record_count": layout["record_count"],
            "record_size": layout["record_size"],
            "cluster_size": layout["cluster_size"],
            "runs": [[lcn, count] for lcn, count in layout["runs"]],
            "real_size": layout["real_size"],
        }
        summary["records_total"] = layout["record_count"]
        candidates = scan_deleted(source, boot, limit_records=limit_records,
                                  stats=summary)
        summary["candidates"] = candidates
        summary["deleted"] = len(candidates)
        if carve and out_dir:
            for cand in candidates:
                if not cand.get("carvable"):
                    continue
                if len(summary["carved"]) >= DEFAULT_MAX_CARVE_FILES:
                    summary["errors"].append("carve file cap reached (%d)"
                                             % DEFAULT_MAX_CARVE_FILES)
                    break
                try:
                    path = carve_candidate(source, layout, cand, out_dir,
                                           max_carve_bytes)
                except OSError as exc:
                    summary["errors"].append("carve %s failed: %s"
                                             % (cand.get("name") or "?", exc))
                    continue
                if path:
                    size = os.path.getsize(path)
                    summary["carved"].append({
                        "name": cand.get("name"),
                        "record_number": cand.get("record_number"),
                        "path": path,
                        "bytes": size,
                    })
                    summary["bytes_written"] += size
        summary["ok"] = True
    except Exception as exc:
        summary["errors"].append("%s: %s" % (type(exc).__name__, exc))
    finally:
        source.close()
    return summary


def _print_summary(summary):
    boot = summary.get("boot") or {}
    layout = summary.get("layout") or {}
    print("source: %s" % summary.get("source"))
    if summary.get("offset"):
        print("offset: %d bytes%s" % (summary["offset"],
              "" if summary.get("volume_limit") is None
              else " (volume limit %d)" % summary["volume_limit"]))
    if boot:
        print("NTFS: bytes/sector=%d sectors/cluster=%d cluster=%d $MFT LCN=%d record=%d"
              % (boot.get("bytes_per_sector", 0), boot.get("sectors_per_cluster", 0),
                 boot.get("cluster_size", 0), boot.get("mft_lcn", 0),
                 boot.get("record_size", 0)))
    if layout:
        print("records: %d in $MFT, %d examined, %d deleted"
              % (summary.get("records_total", 0), summary.get("records_examined", 0),
                 summary.get("deleted", 0)))
    candidates = summary.get("candidates") or []
    if not candidates:
        print("no deleted records found")
    else:
        print("%-8s %-30s %12s %-8s %-9s %-8s %s"
              % ("record", "name", "size", "deleted", "carvable", "conf", "reason"))
        for cand in candidates:
            print("%-8d %-30s %12d %-8s %-9s %-8.2f %s"
                  % (cand.get("record_number", 0), str(cand.get("name") or "")[:30],
                     cand.get("size", 0),
                     "yes" if cand.get("deleted") else "no",
                     "yes" if cand.get("carvable") else "no",
                     cand.get("confidence", 0.0),
                     cand.get("reason", "")))
    if summary.get("carved"):
        print("carved: %d file(s), %s -> %s"
              % (len(summary["carved"]), _fmt_bytes(summary.get("bytes_written", 0)),
                 summary.get("out_dir")))
    for error in summary.get("errors") or []:
        print("error: %s" % error)


def main(argv=None):
    _ensure_utf8_streams()
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]
    if not args or "--help" in args or "-h" in args:
        print(__doc__.strip())
        return 0 if args else 2
    source_path = None
    out_dir = None
    limit = None
    carve = True
    offset = 0
    volume_limit = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--out":
            i += 1
            if i >= len(args):
                print("--out needs a directory")
                return 2
            out_dir = args[i]
        elif arg.startswith("--out="):
            out_dir = arg.split("=", 1)[1]
        elif arg == "--offset":
            i += 1
            if i >= len(args):
                print("--offset needs a byte offset")
                return 2
            try:
                offset = int(args[i], 0)
            except ValueError:
                print("--offset needs a byte offset")
                return 2
        elif arg.startswith("--offset="):
            try:
                offset = int(arg.split("=", 1)[1], 0)
            except ValueError:
                print("--offset needs a byte offset")
                return 2
        elif arg == "--volume-limit":
            i += 1
            if i >= len(args):
                print("--volume-limit needs a byte count")
                return 2
            try:
                volume_limit = int(args[i], 0)
            except ValueError:
                print("--volume-limit needs a byte count")
                return 2
        elif arg.startswith("--volume-limit="):
            try:
                volume_limit = int(arg.split("=", 1)[1], 0)
            except ValueError:
                print("--volume-limit needs a byte count")
                return 2
        elif arg == "--limit":
            i += 1
            if i >= len(args):
                print("--limit needs a number")
                return 2
            try:
                limit = int(args[i])
            except ValueError:
                print("--limit needs a number")
                return 2
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                print("--limit needs a number")
                return 2
        elif arg == "--no-carve":
            carve = False
        elif source_path is None:
            source_path = arg
        else:
            print("unexpected argument: %s" % arg)
            return 2
        i += 1
    if source_path is None:
        print("usage: python text_recovery.py <image | device> [--out DIR] [--limit N] [--no-carve]")
        return 2
    summary = recover_mft(source_path, out_dir=out_dir, limit_records=limit,
                          carve=carve, offset=offset, volume_limit=volume_limit)
    _print_summary(summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
