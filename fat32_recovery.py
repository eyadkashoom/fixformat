#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fat32_recovery.py - text recovery pipeline (Phase 2: FAT32 metadata parser).

Independent module: it does not import raw_scan, it does not import
text_recovery, and it never writes to the source it reads. Phase 2 reads the
FAT32 volume boot record and the File Allocation Table of an image file or a
raw volume, walks the root directory's cluster chain, and returns deleted
32-byte directory entries (first byte 0xE5) with their original names (short
8.3 names and reconstructed long names, UTF-16LE, Arabic included), sizes and
cluster chains so their bytes can be carved.

Why this is a separate module
-----------------------------
Text files have no magic signature, so signature carving can never find them.
On FAT32 the deleted directory entry is the one place where a deleted text
file still keeps its name, its size and (often) the cluster chain that holds
its bytes. Layer 1 of the hybrid pipeline in v7/TextRecoveryGuide.md is
"MFT/FAT metadata"; text_recovery.py implements the NTFS half, this module
implements the FAT32 half.

Public API
----------
    parse_fat_boot_sector(vbr)        -> boot dict (ValueError when not FAT32)
    cluster_address(boot, cluster)    -> byte offset of a cluster in the volume
    fat_entry(source, boot, index, cache=None) -> 28-bit FAT value or None
    read_cluster_chain(source, boot, first_cluster, cache=None)
                                      -> chain dict (clusters, truncated, loop)
    iter_directory(source, boot, first_cluster, limit_clusters=None)
                                      -> raw 32-byte directory entries
    parse_dir_entry(entry)            -> entry dict or None
    assemble_lfn(pending)             -> long name text or ""
    scan_deleted(source, boot, root_dir_cluster=2, ...)
                                      -> [candidate dict, ...]
    read_candidate_data(source, boot, cand, max_bytes=None)
    carve_candidate(source, boot, cand, out_dir, max_bytes=None)
    recover_fat(source_path, ...)     -> summary dict
    device_path(drive)                -> Windows raw-device path
    main(argv)                        -> exit code

Command line (read-only):
    python fat32_recovery.py <image | device> [--out DIR] [--offset BYTES]
        [--volume-limit BYTES] [--limit N] [--no-carve]

The source is opened "rb" and only seek/read are used. Output goes only to
the directory named by --out (or the caller's out_dir). --offset is the byte
offset of the FAT32 boot sector inside the source (0 for a volume image, the
partition start for a whole-disk image); --volume-limit caps readable bytes.

Not implemented here (documented limits): FSInfo free-cluster accounting,
backup boot sector validation, exFAT, FAT12/FAT16, subdirectory recursion,
LFN checksum verification, and the 8.3 Unicode (NT) alternate extension.
The first character of a deleted 8.3 name is destroyed by the 0xE5 marker
and is reported as '?'; the long-name entries keep every character, so a
deleted file with a long name still returns its exact original name.
Cluster reuse after deletion is reported as-is: the module returns the bytes
the FAT points at and never tries to "correct" them.
"""

import os
import sys

BS = chr(92)          # one backslash, built without a literal escape
NL = chr(10)
NUL_CHAR = chr(0)

VERSION = "7.0.1.2"
SECTOR = 512
BOOT_SIG = bytes([0x55, 0xAA])

# OEM ids written by FAT32 formatters: MSWIN4.1 is the canonical one, "BAD"
# appears on damaged or camera media, mkfs.vfat and mtools write their own.
# Anything else passes only when the FileSysType string at 0x52 says FAT32.
FAT_OEM_PREFIXES = (b"MSWIN", b"MSDOS", b"BAD", b"mkfs", b"mtools", b"WIN98")
FAT32_FS_TYPE = b"FAT32"

# directory entry markers and attribute bits
ENTRY_DELETED = 0xE5
ENTRY_ESCAPED = 0x05        # first byte 0x05 means 'A with ring above'
ENTRY_END = 0x00            # first byte 0x00 ends the directory
ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_LABEL = 0x08
ATTR_SUBDIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LFN = 0x0F             # long file name entry
LFN_LAST_FLAG = 0x40        # set on the ordinal of the physically first entry
CASE_BASE_LOWER = 0x08      # byte 12 case flags (Windows NT extension)
CASE_EXT_LOWER = 0x10

# 32-bit FAT values (only the low 28 bits are meaningful)
FAT32_ENTRY_MASK = 0x0FFFFFFF
FAT32_EOC = 0x0FFFFFF8      # values >= this end a chain
FAT32_BAD = 0x0FFFFFF7
FAT32_FREE = 0x00000000
FAT32_MAX_CLUSTERS = 65525  # fewer data clusters than this is FAT32 territory

DEFAULT_MAX_CARVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CARVE_FILES = 1000
FAT_READ_BLOCK = 4096       # FAT bytes per block read, keeps seeks low

__all__ = [
    "Source", "parse_fat_boot_sector", "cluster_address", "fat_entry",
    "read_cluster_chain", "iter_directory", "parse_dir_entry", "lfn_from_entry",
    "short_name_from_entry", "assemble_lfn", "scan_deleted",
    "read_candidate_data", "carve_candidate", "recover_fat", "device_path",
    "main",
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
    literal and stays readable on every platform. Anything that already
    looks like a device form is passed through; ordinary file paths are too.
    """
    drive = str(drive)
    if drive.startswith(BS * 2 + ".") or drive.startswith(BS * 2 + "?" + BS):
        return drive
    if len(drive) == 2 and drive[1] == ":" and drive[0].isalpha():
        return BS * 2 + "." + BS + drive.upper()
    if len(drive) == 1 and drive.isalpha():
        return BS * 2 + "." + BS + drive.upper() + ":"
    return drive


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


def _dos_time(value):
    """MS-DOS time field -> (h, m, s); None when the field is impossible."""
    if not value:
        return None
    hour = (value >> 11) & 0x1F
    minute = (value >> 5) & 0x3F
    second = (value & 0x1F) * 2
    if hour > 23 or minute > 59 or second > 58:
        return None
    return hour, minute, second


def _dos_date(value):
    """MS-DOS date field -> (y, m, d); None when the field is impossible."""
    if not value:
        return None
    year = ((value >> 9) & 0x7F) + 1980
    month = (value >> 5) & 0x0F
    day = value & 0x1F
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    return year, month, day


def _dos_datetime(date_value, time_value):
    """MS-DOS date + time -> 'YYYY-MM-DD HH:MM:SS' text or ''."""
    date = _dos_date(date_value)
    if date is None:
        return ""
    clock = _dos_time(time_value) or (0, 0, 0)
    return "%04d-%02d-%02d %02d:%02d:%02d" % (date[0], date[1], date[2],
                                              clock[0], clock[1], clock[2])


def _decode_name_bytes(raw):
    """CP-1252 text with the FAT first-byte escapes (0x05/0xE5) applied."""
    out = []
    for i, value in enumerate(raw):
        if i == 0 and value == ENTRY_ESCAPED:
            out.append(chr(0x00C5))       # 'A with ring above'
            continue
        if value == ENTRY_DELETED:
            out.append(chr(0x00E5))       # 'a with ring above'
            continue
        out.append(bytes([value]).decode("cp1252", "replace"))
    return "".join(out)


# ---------------------------------------------------------------------------
# read-only source
# ---------------------------------------------------------------------------

class Source(object):
    """Read-only view of an image file or a raw device.

    `offset` shifts every read (a partition inside a whole-disk image); the
    boot sector is then read at `offset`, not at byte 0. `limit` caps how many
    bytes from that offset may be read (the partition length). Only "rb"
    opens, seek and read are ever used; the source is never written to.
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

def parse_fat_boot_sector(vbr):
    """Parse the 512-byte FAT32 volume boot record.

    Raises ValueError when the boot signature is missing, when the OEM id /
    FileSysType string does not look like FAT32, when the BPB describes
    FAT12/FAT16 (ReservedSectors16 at 0x16 non-zero or SectorsPerFAT32 at
    0x24 zero), or when any geometry field is implausible. Offsets are the
    on-disk ones: bytes/sector 0x0B, sectors/cluster 0x0D, reserved sectors
    0x0E, FAT copies 0x10, total sectors 0x20, sectors per FAT32 0x24, root
    cluster 0x2C, FSInfo 0x30, backup boot 0x32, serial 0x43, label 0x47,
    FS type string 0x52, signature 0x1FE. FAT copy 0 starts at
    reserved_sector_count * bytes_per_sector; entries are 32-bit and only
    their low 28 bits are cluster numbers.
    """
    if vbr is None or len(vbr) < SECTOR:
        raise ValueError("boot sector too short: %d byte(s), need %d"
                         % (0 if not vbr else len(vbr), SECTOR))
    if vbr[0x1FE:0x200] != BOOT_SIG:
        raise ValueError("no boot signature: expected 0x55AA at 0x1FE, found %r"
                         % (vbr[0x1FE:0x200],))
    oem = vbr[0x03:0x0B]
    fs_type = vbr[0x52:0x5A]
    if oem.startswith(b"NTFS") or oem.startswith(b"EXFAT") or oem == b"-FVE-FS-":
        raise ValueError("not a FAT32 volume: OEM id %r is another file system" % oem)
    if not oem.startswith(FAT_OEM_PREFIXES) and not fs_type.startswith(FAT32_FS_TYPE):
        raise ValueError("not a FAT32 volume: OEM id is %r, FS type is %r"
                         % (oem, fs_type))
    bytes_per_sector = _u16(vbr, 0x0B)
    sectors_per_cluster = _u8(vbr, 0x0D)
    if bytes_per_sector < 512 or bytes_per_sector > 4096 or bytes_per_sector % 512:
        raise ValueError("implausible bytes-per-sector %d" % bytes_per_sector)
    if sectors_per_cluster == 0 or sectors_per_cluster & (sectors_per_cluster - 1):
        raise ValueError("implausible sectors-per-cluster %d (must be a power of two)"
                         % sectors_per_cluster)
    reserved = _u16(vbr, 0x0E)
    fat_count = _u8(vbr, 0x10)
    if reserved < 2:
        raise ValueError("implausible reserved sector count %d" % reserved)
    if fat_count < 1 or fat_count > 8:
        raise ValueError("implausible FAT count %d" % fat_count)
    if _u16(vbr, 0x16) != 0:
        raise ValueError("FAT12/FAT16 BPB: ReservedSectors16 at 0x16 is %d, must be 0"
                         % _u16(vbr, 0x16))
    sectors_per_fat32 = _u32(vbr, 0x24)
    if sectors_per_fat32 == 0:
        raise ValueError("not FAT32: sectors-per-FAT32 at 0x24 is zero (FAT12/FAT16 BPB)")
    total_sectors = _u32(vbr, 0x20) or _u16(vbr, 0x13)
    if total_sectors == 0:
        raise ValueError("total sector count is zero")
    data_sectors = total_sectors - reserved - fat_count * sectors_per_fat32
    cluster_count = data_sectors // sectors_per_cluster
    if cluster_count < 1:
        raise ValueError("no data clusters: total=%d reserved=%d fats=%d spf=%d"
                         % (total_sectors, reserved, fat_count, sectors_per_fat32))
    if cluster_count >= FAT32_MAX_CLUSTERS:
        raise ValueError("cluster count %d is in the FAT16 range, not FAT32" % cluster_count)
    root_cluster = _u32(vbr, 0x2C)
    if root_cluster < 2:
        raise ValueError("implausible root cluster %d" % root_cluster)
    if root_cluster - 2 >= cluster_count:
        raise ValueError("root cluster %d beyond the volume (%d clusters)"
                         % (root_cluster, cluster_count))
    label = _decode_name_bytes(vbr[0x47:0x52]).strip(" " + NUL_CHAR)
    return {
        "oem_id": oem.decode("cp1252", "replace").strip(),
        "fs_type": fs_type.decode("cp1252", "replace").strip(),
        "bytes_per_sector": bytes_per_sector,
        "sectors_per_cluster": sectors_per_cluster,
        "cluster_size": bytes_per_sector * sectors_per_cluster,
        "reserved_sector_count": reserved,
        "fat_count": fat_count,
        "sectors_per_fat32": sectors_per_fat32,
        "fat_start": reserved * bytes_per_sector,
        "first_data_sector": reserved + fat_count * sectors_per_fat32,
        "root_cluster": root_cluster,
        "total_sectors": total_sectors,
        "cluster_count": cluster_count,
        "media_descriptor": _u8(vbr, 0x15),
        "fsinfo_sector": _u16(vbr, 0x30),
        "backup_boot_sector": _u16(vbr, 0x32),
        "volume_serial": _u32(vbr, 0x43),
        "volume_label": label,
        "boot_signature": _u8(vbr, 0x42),
    }


def cluster_address(boot, cluster):
    """Byte offset of `cluster` inside the volume (offset 0 = boot sector).

    (reserved_sector_count + fat_count * sectors_per_fat32 +
     (cluster - 2) * sectors_per_cluster) * bytes_per_sector
    """
    sector = (boot["reserved_sector_count"]
              + boot["fat_count"] * boot["sectors_per_fat32"]
              + (cluster - 2) * boot["sectors_per_cluster"])
    return sector * boot["bytes_per_sector"]


def fat_entry(source, boot, index, cache=None):
    """Return the 28-bit value of FAT entry `index`, or None when unreadable.

    Entries are 32-bit little-endian; only the low 28 bits are cluster
    numbers, so the value is masked. `cache`, a plain dict, holds FAT_READ_BLOCK
    sized blocks of FAT copy 0 so a chain walk costs one read per block
    instead of one read per entry.
    """
    abs_off = boot["fat_start"] + int(index) * 4
    key = abs_off // FAT_READ_BLOCK
    if cache is not None and key in cache:
        data = cache[key]
    else:
        data = source.read(key * FAT_READ_BLOCK, FAT_READ_BLOCK)
        if cache is not None:
            cache[key] = data
    if not data:
        return None
    pos = abs_off - key * FAT_READ_BLOCK
    if pos < 0 or pos + 4 > len(data):
        return None
    return _u32(data, pos) & FAT32_ENTRY_MASK


def read_cluster_chain(source, boot, first_cluster, cache=None):
    """Walk the FAT from `first_cluster` until an end-of-chain marker.

    Returns a dict: clusters (list), truncated (bool), loop (bool), bad
    (chain touched 0x0FFFFFF7), out_of_range (a value left the volume),
    eoc (the chain ended normally) and reason (text for the report). Entry
    values are masked to 28 bits; EOC is >= 0x0FFFFFF8, free is 0; a chain
    longer than the volume's cluster count, or a value already seen, is a
    loop and the chain is marked truncated.
    """
    result = {
        "clusters": [],
        "truncated": False,
        "loop": False,
        "bad": False,
        "out_of_range": False,
        "eoc": False,
        "reason": "",
    }
    limit = boot["cluster_count"]
    if first_cluster is None or first_cluster == 0:
        result["reason"] = "entry has no cluster"
        return result
    if first_cluster < 2 or first_cluster - 2 >= boot["cluster_count"]:
        result["truncated"] = True
        result["out_of_range"] = True
        result["reason"] = "first cluster %d is outside the volume" % first_cluster
        return result
    if cache is None:
        cache = {}
    cluster = int(first_cluster)
    seen = set()
    while True:
        result["clusters"].append(cluster)
        seen.add(cluster)
        if len(result["clusters"]) > limit:
            result["truncated"] = True
            result["loop"] = True
            result["reason"] = ("chain longer than the volume (%d clusters)"
                                % boot["cluster_count"])
            return result
        nxt = fat_entry(source, boot, cluster, cache)
        if nxt is None:
            result["truncated"] = True
            result["reason"] = "FAT unreadable at cluster %d" % cluster
            return result
        if nxt >= FAT32_EOC:
            result["eoc"] = True
            return result
        if nxt == FAT32_BAD:
            result["truncated"] = True
            result["bad"] = True
            result["reason"] = "chain hit a bad cluster after %d" % cluster
            return result
        if nxt == FAT32_FREE:
            result["truncated"] = True
            result["reason"] = "cluster %d freed in the FAT" % cluster
            return result
        if nxt < 2 or nxt - 2 >= boot["cluster_count"]:
            result["truncated"] = True
            result["out_of_range"] = True
            result["reason"] = "cluster %d points outside the volume (%d)" % (cluster, nxt)
            return result
        if nxt in seen:
            result["truncated"] = True
            result["loop"] = True
            result["reason"] = "cluster chain loops at %d" % nxt
            return result
        cluster = nxt


# ---------------------------------------------------------------------------
# directory entries
# ---------------------------------------------------------------------------

def lfn_from_entry(entry):
    """Parse one 0x0F long-name entry into a dict, or None when malformed.

    The 13 UTF-16LE code units sit at bytes 1-10 (5), 14-25 (6) and 28-31
    (2). LFN entries precede their short-name entry in REVERSE logical
    order: the physically first entry holds the tail of the name, the entry
    immediately before the short name holds characters 1-13 and carries the
    0x40 flag in its ordinal. A deleted LFN entry has first byte 0xE5:
    Windows overwrites the ordinal byte too, so the sequence information is
    lost and assembly falls back to reversing the on-disk run.
    """
    if entry is None or len(entry) != 32 or entry[11] != ATTR_LFN:
        return None
    ordinal = entry[0]
    units = []
    for off, count in ((1, 10), (14, 12), (28, 4)):
        for pos in range(off, off + count, 2):
            units.append(_u16(entry, pos))
    if ordinal == ENTRY_DELETED:
        return {
            "kind": "lfn",
            "deleted": True,
            "ordinal": ordinal,
            "order": None,
            "last": None,
            "units": units,
            "checksum": entry[13],
        }
    return {
        "kind": "lfn",
        "deleted": False,
        "ordinal": ordinal,
        "order": ordinal & 0x1F,
        "last": bool(ordinal & LFN_LAST_FLAG),
        "units": units,
        "checksum": entry[13],
    }


def short_name_from_entry(entry):
    """8.3 name text from a directory entry, honouring the NT case flags.

    A first byte of 0xE5 is the deletion marker and destroys the original
    first character, so position 0 is reported as '?' (the Windows 0x05
    escape for 'Å' is still decoded for live entries).
    """
    base = _decode_name_bytes(entry[0:8]).rstrip(" " + NUL_CHAR).rstrip(".")
    ext = _decode_name_bytes(entry[8:11]).rstrip(" " + NUL_CHAR)
    if entry[0] == ENTRY_DELETED and base:
        base = "?" + base[1:]
    flags = _u8(entry, 12)
    if flags & CASE_BASE_LOWER:
        base = base.lower()
    if flags & CASE_EXT_LOWER:
        ext = ext.lower()
    return base + ("." + ext if ext else "")


def parse_dir_entry(entry):
    """Parse one 32-byte directory entry.

    Returns a dict with "kind" of "end", "lfn" or "file", or None when the
    entry is not 32 bytes. File dicts carry: deleted (first byte 0xE5), the
    8.3 name, attr, case flags, timestamps, first_cluster (high word at 20,
    low word at 26) and file_size (28). Volume labels (0x08) and
    subdirectories (0x10) are flagged so the scanner can skip them.
    """
    if entry is None or len(entry) != 32:
        return None
    first = entry[0]
    if first == ENTRY_END:
        return {"kind": "end"}
    attr = entry[11]
    if attr == ATTR_LFN:
        return lfn_from_entry(entry)
    return {
        "kind": "file",
        "deleted": first == ENTRY_DELETED,
        "name": short_name_from_entry(entry),
        "attr": attr,
        "case_flags": _u8(entry, 12),
        "is_directory": bool(attr & ATTR_SUBDIRECTORY),
        "is_volume_label": attr == ATTR_VOLUME_LABEL,
        "first_cluster": (_u16(entry, 20) << 16) | _u16(entry, 26),
        "file_size": _u32(entry, 28),
        "created": _dos_datetime(_u16(entry, 16), _u16(entry, 14)),
        "accessed": _dos_datetime(_u16(entry, 18), 0),
        "modified": _dos_datetime(_u16(entry, 24), _u16(entry, 22)),
    }


def assemble_lfn(pending):
    """Join collected LFN dicts, given in on-disk order, into one long name.

    LFN entries sit before their short entry in reverse logical order, so
    the run is reversed first: ordinal 1 (characters 1-13) is the piece
    closest to the short name. A 0x0000 unit terminates a chunk; 0xFFFF
    padding is dropped, and trailing dots and spaces are stripped (they are
    not allowed in long names on Windows). Returns "" when the run is empty.
    """
    if not pending:
        return ""
    chars = []
    for item in reversed(pending):        # logical order: ordinal 1 first
        for value in item.get("units") or []:
            if value == 0x0000:
                break
            if value == 0xFFFF:
                continue
            chars.append(value)
    if not chars:
        return ""
    raw = b"".join(u.to_bytes(2, "little") for u in chars)
    name = raw.decode("utf-16-le", "replace")
    return name.rstrip(" ." + NUL_CHAR)


def iter_directory(source, boot, first_cluster, limit_clusters=None):
    """Yield raw 32-byte entries of a directory's cluster chain.

    Stops at the first 0x00 name byte (end of directory) and after
    `limit_clusters` clusters when given. Directory entries are cluster-
    aligned by the FAT spec, so a cluster tail shorter than 32 bytes is
    dropped; an unreadable cluster ends the walk.
    """
    chain = read_cluster_chain(source, boot, first_cluster)
    cluster_size = boot["cluster_size"]
    cache = {}
    scanned = 0
    for cluster in chain["clusters"]:
        if limit_clusters is not None and scanned >= limit_clusters:
            return
        scanned += 1
        buf = source.read(cluster_address(boot, cluster), cluster_size)
        if not buf:
            return
        pos = 0
        while pos + 32 <= len(buf):
            entry = buf[pos:pos + 32]
            if entry[0] == ENTRY_END:
                return
            yield entry
            pos += 32


# ---------------------------------------------------------------------------
# deleted-entry scan
# ---------------------------------------------------------------------------

def _candidate(parsed, long_name, chain, cand_index, dir_cluster):
    clusters = chain["clusters"]
    size = int(parsed.get("file_size") or 0)
    first = int(parsed.get("first_cluster") or 0)
    if first == 0:
        confidence = 0.35
        reason = "deleted entry with name only (no clusters recorded)"
    elif chain["loop"]:
        confidence = 0.55
        reason = "deleted entry with a looping cluster chain (%s)" % (chain["reason"] or "loop")
    elif chain["out_of_range"]:
        confidence = 0.55
        reason = "deleted entry whose chain leaves the volume (%s)" % chain["reason"]
    elif chain["bad"]:
        confidence = 0.55
        reason = "deleted entry whose chain hits a bad cluster (%s)" % chain["reason"]
    elif chain["truncated"]:
        confidence = 0.55
        reason = "deleted entry with a truncated cluster chain (%s)" % chain["reason"]
    elif size <= 0:
        confidence = 0.90
        reason = "deleted entry with an intact chain but zero size"
    else:
        confidence = 0.90
        reason = "deleted entry with name, size and intact cluster chain"
    return {
        "candidate_index": int(cand_index),
        "directory_cluster": int(dir_cluster),
        "name": long_name or parsed["name"],
        "short_name": parsed["name"],
        "long_name": long_name,
        "deleted": True,
        "attr": parsed["attr"],
        "size_bytes": size,
        "first_cluster": first,
        "clusters": list(clusters),
        "cluster_count": len(clusters),
        "truncated": bool(chain["truncated"]),
        "loop": bool(chain["loop"]),
        "carvable": bool(clusters) and size > 0 and not chain["truncated"],
        "confidence": confidence,
        "reason": reason,
        "created": parsed.get("created", ""),
        "modified": parsed.get("modified", ""),
    }


def scan_deleted(source, boot, root_dir_cluster=2, limit_dirs=None, stats=None):
    """Return candidate dicts for deleted entries in a FAT32 directory.

    `root_dir_cluster` defaults to 2, the value almost every FAT32 BPB stores
    at 0x2C (recover_fat passes the BPB value). `limit_dirs` caps the number
    of directory clusters walked. Volume-label entries (0x08) and
    subdirectory entries (0x10) are skipped; live entries (first byte not
    0xE5) are excluded. A reconstructed long name takes precedence over the
    8.3 name. `stats`, when given, receives entries_examined, lfn_entries
    and subdir_entries.
    """
    out = []
    pending = []          # deleted LFN entries waiting for their short entry
    live_pending = []     # live LFN entries (never attach to a deleted file)
    examined = 0
    lfn_seen = 0
    subdir_entries = 0
    for entry in iter_directory(source, boot, root_dir_cluster, limit_dirs):
        examined += 1
        parsed = parse_dir_entry(entry)
        if parsed is None:
            pending = []
            live_pending = []
            continue
        if parsed["kind"] == "end":
            break
        if parsed["kind"] == "lfn":
            lfn_seen += 1
            if parsed["deleted"]:
                live_pending = []
                pending.append(parsed)
                continue
            pending = []
            if parsed["last"]:
                live_pending = [parsed]
            elif live_pending and live_pending[-1].get("order") == (parsed["order"] + 1):
                live_pending.append(parsed)
            else:
                live_pending = []
            continue
        long_name = ""
        if parsed["deleted"]:
            long_name = assemble_lfn(pending)
        pending = []
        live_pending = []
        if parsed["is_volume_label"]:
            continue
        if parsed["is_directory"]:
            subdir_entries += 1
            continue
        if not parsed["deleted"]:
            continue
        chain = read_cluster_chain(source, boot, parsed["first_cluster"])
        out.append(_candidate(parsed, long_name, chain, len(out), root_dir_cluster))
    if stats is not None:
        stats["entries_examined"] = examined
        stats["lfn_entries"] = lfn_seen
        stats["subdir_entries"] = subdir_entries
    return out


# ---------------------------------------------------------------------------
# carving
# ---------------------------------------------------------------------------

_INVALID_NAME_CHARS = set([chr(c) for c in range(32)]) | set(["<", ">", ":", '"', "/", BS, "|", "?", "*"])
_RESERVED_NAMES = set(["CON", "PRN", "AUX", "NUL"] +
                      ["COM%d" % i for i in range(1, 10)] +
                      ["LPT%d" % i for i in range(1, 10)])


def _safe_name(name, cand_index):
    cleaned = "".join("_" if ch in _INVALID_NAME_CHARS else ch for ch in str(name))
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "entry_%05d.dat" % cand_index
    stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned
    if stem.upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def read_candidate_data(source, boot, cand, max_bytes=None):
    """Read a candidate's cluster chain in order, capped by max_bytes.

    Reads clamp naturally at Source.size / Source.limit, so a chain that
    would run past the volume yields only the bytes that exist; nothing is
    ever carved beyond the volume. Non-carvable candidates (truncated,
    looped, freed, zero-cluster) return b"" so no half-true data is written.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_CARVE_BYTES
    max_bytes = int(max_bytes)
    if max_bytes <= 0 or not cand.get("carvable"):
        return b""
    size = int(cand.get("size_bytes") or 0)
    limit = min(size, max_bytes) if size else max_bytes
    cluster_size = boot["cluster_size"]
    out = bytearray()
    for cluster in cand.get("clusters") or []:
        if len(out) >= limit:
            break
        want = min(cluster_size, limit - len(out))
        chunk = source.read(cluster_address(boot, cluster), want)
        out += chunk
        if len(chunk) < want:
            break
    return bytes(out[:limit])


def carve_candidate(source, boot, cand, out_dir, max_bytes=None):
    """Write one candidate's bytes to out_dir. Returns the path or ""."""
    data = read_candidate_data(source, boot, cand, max_bytes)
    if not data:
        return ""
    os.makedirs(out_dir, exist_ok=True)
    name = _safe_name(cand.get("name") or "", cand.get("candidate_index", 0))
    path = os.path.join(out_dir, name)
    if os.path.exists(path):
        stem, dot, ext = name.rpartition(".")
        suffix = ".rec%05d" % cand.get("candidate_index", 0)
        path = os.path.join(out_dir, (stem + suffix + dot + ext) if dot else (name + suffix))
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def recover_fat(source_path, out_dir=None, offset=0, volume_limit=None,
               limit_dirs=None, carve=True, root_cluster=None,
               max_carve_bytes=DEFAULT_MAX_CARVE_BYTES):
    """Read a FAT32 source, list deleted directory entries, carve them.

    `offset` is the byte offset of the FAT32 boot sector inside the source (0
    for a volume image, the partition start for a whole-disk image);
    `volume_limit` caps how many bytes from that offset are readable.
    `limit_dirs` caps directory clusters walked; `root_cluster` overrides the
    BPB value. Returns a summary dict shaped like recover_mft: source,
    offset, boot, deleted, candidates, carved, bytes_written, errors, ok.
    """
    summary = {
        "source": str(source_path),
        "out_dir": out_dir,
        "offset": int(offset),
        "volume_limit": None if volume_limit is None else int(volume_limit),
        "boot": None,
        "entries_examined": 0,
        "lfn_entries": 0,
        "deleted": 0,
        "candidates": [],
        "carved": [],
        "bytes_written": 0,
        "errors": [],
        "ok": False,
    }
    source = None
    try:
        source = Source(source_path, offset=offset, limit=volume_limit)
        boot = parse_fat_boot_sector(source.read(0, SECTOR))
        if root_cluster is None:
            root_cluster = boot["root_cluster"]
        summary["boot"] = {
            "bytes_per_sector": boot["bytes_per_sector"],
            "sectors_per_cluster": boot["sectors_per_cluster"],
            "cluster_size": boot["cluster_size"],
            "reserved_sector_count": boot["reserved_sector_count"],
            "fat_count": boot["fat_count"],
            "sectors_per_fat32": boot["sectors_per_fat32"],
            "root_cluster": boot["root_cluster"],
            "cluster_count": boot["cluster_count"],
            "total_sectors": boot["total_sectors"],
            "volume_serial": boot["volume_serial"],
            "volume_label": boot["volume_label"],
            "fs_type": boot["fs_type"],
        }
        stats = {}
        candidates = scan_deleted(source, boot, root_dir_cluster=root_cluster,
                                  limit_dirs=limit_dirs, stats=stats)
        summary["entries_examined"] = stats.get("entries_examined", 0)
        summary["lfn_entries"] = stats.get("lfn_entries", 0)
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
                remaining = max_carve_bytes - summary["bytes_written"]
                if remaining <= 0:
                    summary["errors"].append("total carve byte cap reached (%d)"
                                             % max_carve_bytes)
                    break
                try:
                    path = carve_candidate(source, boot, cand, out_dir, remaining)
                except OSError as exc:
                    summary["errors"].append("carve %s failed: %s"
                                             % (cand.get("name") or "?", exc))
                    continue
                if path:
                    size = os.path.getsize(path)
                    summary["carved"].append({
                        "name": cand.get("name"),
                        "candidate_index": cand.get("candidate_index"),
                        "path": path,
                        "bytes": size,
                    })
                    summary["bytes_written"] += size
        summary["ok"] = True
    except Exception as exc:
        summary["errors"].append("%s: %s" % (type(exc).__name__, exc))
    finally:
        if source is not None:
            source.close()
    return summary


def _print_summary(summary):
    boot = summary.get("boot") or {}
    print("source: %s" % summary.get("source"))
    if summary.get("offset"):
        print("offset: %d bytes%s" % (summary["offset"],
              "" if summary.get("volume_limit") is None
              else " (volume limit %d)" % summary["volume_limit"]))
    if boot:
        print("FAT32: bytes/sector=%d sectors/cluster=%d cluster=%d "
              "reserved=%d fats=%d sectors/fat=%d root cluster=%d"
              % (boot.get("bytes_per_sector", 0), boot.get("sectors_per_cluster", 0),
                 boot.get("cluster_size", 0), boot.get("reserved_sector_count", 0),
                 boot.get("fat_count", 0), boot.get("sectors_per_fat32", 0),
                 boot.get("root_cluster", 0)))
        serial = boot.get("volume_serial") or 0
        print("volume: %s, %s%s"
              % (_fmt_bytes(boot.get("cluster_size", 0) * boot.get("cluster_count", 0)),
                 boot.get("volume_label") or "(no label)",
                 "" if not serial
                 else " serial=%04X-%04X" % ((serial >> 16) & 0xFFFF, serial & 0xFFFF)))
    candidates = summary.get("candidates") or []
    if not candidates:
        print("no deleted directory entries found")
    else:
        print("entries examined: %d, deleted candidates: %d"
              % (summary.get("entries_examined", 0), len(candidates)))
        print("%-8s %-30s %12s %-9s %-8s %s"
              % ("entry", "name", "size", "carvable", "conf", "reason"))
        for cand in candidates:
            print("%-8d %-30s %12d %-9s %-8.2f %s"
                  % (cand.get("candidate_index", 0), str(cand.get("name") or "")[:30],
                     cand.get("size_bytes", 0),
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
        print("usage: python fat32_recovery.py <image | device> [--out DIR] "
              "[--offset BYTES] [--volume-limit BYTES] [--limit N] [--no-carve]")
        return 2
    summary = recover_fat(source_path, out_dir=out_dir, offset=offset,
                          volume_limit=volume_limit, limit_dirs=limit,
                          carve=carve)
    _print_summary(summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
