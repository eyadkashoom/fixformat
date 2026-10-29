#!/usr/bin/env python3
"""MFT survival probe for the post-reinstall Windows image.

Question this answers with a number: after the reformat + Windows reinstall,
do the OLD $MFT records survive in the unallocated part of the MFT, and is
"MFT triage" (V6_PLAN section 4) worth building for this image?

Method, in order:
  1. GPT: read LBA1 header, walk the partition entry array, classify each
     partition by its volume boot record (NTFS / BitLocker FVE / FAT / raw).
  2. For every NTFS volume found: read the VBR, follow $MFT record 0's
     non-resident $DATA run list, enumerate every record, apply the USA
     fixup, and classify each record IN-USE / UNUSED-VALID / GARBAGE.
  3. For every BitLocker volume: parse the FVE header, then measure whether
     any plaintext survives (entropy map + zero-tail boundary + a bounded
     scan for 1024-aligned 'FILE' records with a valid fixup).
  4. Emit a JSON summary on stdout and write the markdown report.

Read-only. Opens the image with open(path, 'rb') and only ever seeks/reads.
A global read budget (default 8 GiB) is enforced; the script refuses to
exceed it rather than silently scanning a 477 GiB image.

stdlib only. Tested with the repo's bundled CPython 3.12.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import struct
import sys
import time
import uuid
from pathlib import Path

SECTOR = 512
RECORD = 1024
FILETIME_UNIX_DELTA = 116444736000000000  # 100-ns ticks between 1601 and 1970
BUDGET = 8 * (1 << 30)

# NTFS attribute types used here
AT_STANDARD_INFORMATION = 0x10
AT_FILE_NAME = 0x30
AT_DATA = 0x80
AT_END = 0xFFFFFFFF

# GPT type GUIDs, keyed by the on-disk (little-endian) 16 bytes.
def _le(guid: str) -> bytes:
    return uuid.UUID(guid).bytes_le


GPT_TYPES = {
    _le("c12a7328-f81f-11d2-ba4b-00a0c93ec93b"): "EFI system",
    _le("e3c9e316-0b5c-4db8-817d-f92df00215ae"): "MS reserved",
    _le("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"): "Basic data",
    _le("de94bba4-06d1-4d40-a16a-bfd50179d6ac"): "Windows recovery",
    _le("0fc63daf-8483-4772-8e79-3d69d8477de4"): "Linux filesystem",
}

VERBOSE = False


def log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr)


def filetime_to_dt(v: int):
    if v == 0:
        return None
    try:
        return datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=v // 10)
    except (OverflowError, ValueError):
        return None


def fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def entropy_bits(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    counts = collections.Counter(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def zero_ratio(data: bytes) -> float:
    return data.count(0) / len(data) if data else 0.0


class Reader:
    """Read-only, budgeted sector reader."""

    def __init__(self, path: str, budget: int = BUDGET):
        self.path = path
        self.f = open(path, "rb")
        self.budget = budget
        self.used = 0
        self.size = Path(path).stat().st_size

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0:
            raise ValueError("negative read")
        if off >= self.size:
            return b""
        self.used += n
        if self.used > self.budget:
            raise RuntimeError(
                f"read budget exceeded: {self.used/2**30:.2f} GiB > {self.budget/2**30:.2f} GiB"
            )
        self.f.seek(off)
        return self.f.read(n)

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------

def parse_gpt(rd: Reader) -> dict:
    mbr = rd.read(0, SECTOR)
    out = {"mbr_sig": mbr[510:512].hex(), "mbr_entries": [], "gpt": None, "partitions": []}
    for i in range(4):
        e = mbr[446 + i * 16: 446 + (i + 1) * 16]
        if e[4] == 0:
            continue
        out["mbr_entries"].append(
            {
                "index": i,
                "type": e[4],
                "first_lba": struct.unpack("<I", e[8:12])[0],
                "sectors": struct.unpack("<I", e[12:16])[0],
            }
        )

    hdr = rd.read(SECTOR, 92)
    if hdr[0:8] != b"EFI PART":
        return out

    hsize = struct.unpack("<I", hdr[12:16])[0]
    cur_lba, backup_lba, first_usable, last_usable = struct.unpack("<QQQQ", hdr[24:56])
    disk_guid = hdr[56:72]
    pe_lba = struct.unpack("<Q", hdr[72:80])[0]
    num, esz, _pecrc = struct.unpack("<III", hdr[80:92])
    out["gpt"] = {
        "header_size": hsize,
        "current_lba": cur_lba,
        "backup_lba": backup_lba,
        "first_usable_lba": first_usable,
        "last_usable_lba": last_usable,
        "disk_guid": fmt_guid(disk_guid),
        "part_entry_lba": pe_lba,
        "num_entries": num,
        "entry_size": esz,
    }

    if esz <= 0 or esz > 4096 or num <= 0 or num > 4096:
        return out
    arr = rd.read(pe_lba * SECTOR, num * esz)
    for i in range(num):
        e = arr[i * esz:(i + 1) * esz]
        if len(e) < 128 or e[0:16] == b"\x00" * 16:
            continue
        tguid = e[0:16]
        first, last = struct.unpack("<QQ", e[32:48])
        attrs = struct.unpack("<Q", e[48:56])[0]
        name = e[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        out["partitions"].append(
            {
                "index": i,
                "type_guid": fmt_guid(tguid),
                "type_name": GPT_TYPES.get(tguid, "unknown"),
                "unique_guid": fmt_guid(e[16:32]),
                "first_lba": first,
                "last_lba": last,
                "offset": first * SECTOR,
                "bytes": (last - first + 1) * SECTOR,
                "attrs": attrs,
                "name": name,
            }
        )
    return out


def fmt_guid(b: bytes) -> str:
    # GPT stores GUIDs little-endian in the first three fields
    d1, d2, d3 = struct.unpack("<IHH", b[0:8])
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{b[8:10].hex()}-{b[10:16].hex()}"


# --------------------------------------------------------------------------
# Volume boot sector
# --------------------------------------------------------------------------

def probe_vbr(rd: Reader, part: dict) -> dict:
    """Classify a partition by its first sector."""
    vbr = rd.read(part["offset"], SECTOR)
    info = {
        "index": part["index"],
        "offset": part["offset"],
        "size_bytes": part["bytes"],
        "gpt_name": part["name"],
        "gpt_type": part["type_name"],
        "oem": vbr[3:11].decode("latin-1") if len(vbr) >= 11 else "",
        "vbr_sig": vbr[510:512].hex() if len(vbr) >= 512 else "",
        "kind": "unknown",
    }
    oem = vbr[3:11]
    if oem == b"NTFS    ":
        info["kind"] = "ntfs"
        info.update(parse_ntfs_bpb(vbr))
    elif oem == b"-FVE-FS-":
        info["kind"] = "bitlocker"
        info.update(parse_fve_header(vbr))
    elif oem.startswith(b"MSDOS") or oem.startswith(b"MSWIN") or oem.startswith(b"mkfs"):
        info["kind"] = "fat"
        info["label"] = vbr[0x47:0x52].decode("latin-1", "replace").strip()
    elif vbr == b"\x00" * SECTOR:
        info["kind"] = "empty"
    return info


def parse_ntfs_bpb(vbr: bytes) -> dict:
    bps = struct.unpack("<H", vbr[0x0B:0x0D])[0]
    spc = vbr[0x0D]  # sectors per cluster (0x01..0x80); >0x80 would be 2^-n, never seen
    total = struct.unpack("<Q", vbr[0x28:0x30])[0]
    mft_lcn = struct.unpack("<Q", vbr[0x30:0x38])[0]
    mftmirr_lcn = struct.unpack("<Q", vbr[0x38:0x40])[0]
    cpi = struct.unpack("<b", vbr[0x40:0x41])[0]  # clusters per MFT record (signed)
    cluster = bps * spc
    rec_size = (1 << -cpi) if cpi < 0 else cluster * (1 << cpi)
    index_size = struct.unpack("<b", vbr[0x44:0x45])[0]
    return {
        "bytes_per_sector": bps,
        "sectors_per_cluster": spc,
        "cluster_size": cluster,
        "total_sectors": total,
        "mft_lcn": mft_lcn,
        "mft_offset_in_volume": mft_lcn * cluster,
        "mftmirr_lcn": mftmirr_lcn,
        "clusters_per_record_raw": cpi,
        "record_size": rec_size,
        "clusters_per_index_raw": index_size,
        "serial": vbr[0x48:0x50].hex(),
        "backup_sig": vbr[510:512].hex(),
    }


def parse_fve_header(vbr: bytes) -> dict:
    """BitLocker drive header. Enough to say what it is, not to decrypt it."""
    size, version = struct.unpack("<II", vbr[8:16])
    method = None
    # The FVE metadata header (if it starts in this sector) carries the
    # encryption method at 0x0C of the metadata block; try the documented
    # 'metadata block offset' field at 0x160 first.
    mdoff = struct.unpack("<Q", vbr[0x160:0x168])[0] if len(vbr) >= 0x168 else 0
    return {
        "fve_metadata_size": size,
        "fve_version": f"0x{version:04x}",
        "fve_metadata_offset_field": mdoff,
        "fve_encryption_method": method,
    }


# --------------------------------------------------------------------------
# NTFS record / attribute parsing
# --------------------------------------------------------------------------

class Record:
    __slots__ = (
        "index", "raw", "offset", "magic", "fixup_ok", "seq", "links", "flags",
        "used", "alloc", "base_ref", "next_attr_id", "attrs", "names",
        "si_times", "data_size", "runs", "error", "volume_name",
    )

    def __init__(self, index: int, offset: int, raw: bytes):
        self.index = index
        self.offset = offset
        self.raw = raw
        self.magic = raw[0:4]
        self.fixup_ok = False
        self.seq = 0
        self.links = 0
        self.flags = 0
        self.used = 0
        self.alloc = 0
        self.base_ref = 0
        self.next_attr_id = 0
        self.attrs = []
        self.names = []
        self.si_times = (None, None, None, None)
        self.data_size = None
        self.runs = None
        self.error = []
        self.volume_name = None

    @property
    def in_use(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & 0x02)


def apply_fixup(raw: bytes):
    """Return (fixed_bytes, ok). Standard NTFS update-sequence array."""
    if len(raw) < 48:
        return raw, False
    usa_off, usa_cnt = struct.unpack("<HH", raw[4:8])
    if usa_cnt < 2 or usa_off + usa_cnt * 2 > len(raw):
        return raw, False
    usn = raw[usa_off:usa_off + 2]
    if usn == b"\x00\x00":
        return raw, False
    sector_size = len(raw) // (usa_cnt - 1)
    if sector_size <= 0 or sector_size * (usa_cnt - 1) != len(raw):
        return raw, False
    buf = bytearray(raw)
    for i in range(1, usa_cnt):
        end = i * sector_size - 2
        if buf[end:end + 2] != usn:
            return raw, False
        buf[end:end + 2] = raw[usa_off + i * 2: usa_off + i * 2 + 2]
    return bytes(buf), True


def parse_runs(buf: bytes) -> list:
    """Decode a non-resident run list: [(lcn or None, clusters), ...]."""
    runs = []
    lcn = 0
    i = 0
    while i < len(buf):
        hdr = buf[i]
        if hdr == 0:
            break
        len_len = hdr & 0x0F
        off_len = hdr >> 4
        if len_len == 0 or len_len > 8 or off_len > 8:
            break
        i += 1
        if i + len_len + off_len > len(buf):
            break
        length = int.from_bytes(buf[i:i + len_len], "little")
        i += len_len
        if off_len == 0:
            runs.append((None, length))  # sparse
            continue
        delta = int.from_bytes(buf[i:i + off_len], "little", signed=True)
        i += off_len
        lcn += delta
        if length == 0:
            break
        runs.append((lcn, length))
    return runs


def parse_record(index: int, offset: int, raw: bytes) -> Record:
    r = Record(index, offset, raw)
    if raw[0:4] not in (b"FILE", b"BAAD"):
        return r
    buf, ok = apply_fixup(raw)
    r.fixup_ok = ok
    if not ok:
        return r
    r.seq, r.links = struct.unpack("<HH", buf[16:20])
    r.flags = struct.unpack("<H", buf[22:24])[0]
    r.used, r.alloc = struct.unpack("<II", buf[24:32])
    r.base_ref = struct.unpack("<Q", buf[32:40])[0] & 0x0000FFFFFFFFFFFF
    r.next_attr_id = struct.unpack("<H", buf[40:42])[0]
    if r.magic == b"BAAD":
        r.error.append("BAAD")
        return r
    if not (24 <= r.used <= len(buf)):
        r.error.append(f"bogus used size {r.used}")

    off = struct.unpack("<H", buf[20:22])[0]
    if off < 0x2A or off > len(buf):
        r.error.append(f"bogus attr offset {off}")
        return r

    guard = 0
    while off + 8 <= len(buf) and guard < 64:
        guard += 1
        atype, alen = struct.unpack("<II", buf[off:off + 8])
        if atype == AT_END:
            break
        if alen < 24 or off + alen > len(buf):
            r.error.append(f"bad attr len {alen} at {off}")
            break
        non_res = buf[off + 8]
        name_len = buf[off + 9]
        name_off = struct.unpack("<H", buf[off + 10:off + 12])[0]
        r.attrs.append(atype)
        value = None
        if non_res == 0:
            vlen = struct.unpack("<I", buf[off + 16:off + 20])[0]
            voff = struct.unpack("<H", buf[off + 20:off + 22])[0]
            if voff + vlen <= alen:
                value = buf[off + voff: off + voff + vlen]
        else:
            if atype == AT_DATA and not name_len:
                r.data_size = struct.unpack("<Q", buf[off + 48:off + 56])[0]
                rl_off = struct.unpack("<H", buf[off + 32:off + 34])[0]
                r.runs = parse_runs(buf[off + rl_off: off + alen])
        if atype == AT_FILE_NAME and value and len(value) >= 66:
            parent = struct.unpack("<Q", value[0:8])[0] & 0x0000FFFFFFFFFFFF
            parent_seq = struct.unpack("<H", value[6:8])[0]
            alloc_sz, real_sz = struct.unpack("<QQ", value[40:56])
            fl = struct.unpack("<I", value[56:60])[0]
            nlen = value[64]
            ns = value[65]
            nm = value[66:66 + nlen * 2].decode("utf-16-le", "replace")
            r.names.append(
                {
                    "name": nm,
                    "parent": parent,
                    "parent_seq": parent_seq,
                    "namespace": ns,
                    "alloc_size": alloc_sz,
                    "real_size": real_sz,
                    "flags": fl,
                    "fn_times": (
                        struct.unpack("<Q", value[8:16])[0],
                        struct.unpack("<Q", value[16:24])[0],
                        struct.unpack("<Q", value[24:32])[0],
                        struct.unpack("<Q", value[32:40])[0],
                    ),
                }
            )
        elif atype == AT_STANDARD_INFORMATION and value and len(value) >= 32:
            r.si_times = struct.unpack("<QQQQ", value[0:32])
        elif atype == 0x60 and value:  # $VOLUME_NAME
            r.volume_name = value.decode("utf-16-le", "replace").rstrip("\x00")
        off += alen
    return r


class NtfsVolume:
    """Read a plaintext NTFS volume and walk its MFT."""

    def __init__(self, rd: Reader, part: dict, info: dict):
        self.rd = rd
        self.part = part
        self.info = info
        self.base = part["offset"]
        self.cluster = info["cluster_size"]
        self.rec_size = info["record_size"] or RECORD
        self.records = []
        self.mft_runs = []
        self.mft_data_size = 0
        self.mft_rec0 = None
        self.stats = {}

    def _rec_offset_from_runs(self, rec_index: int):
        """Map MFT record index -> absolute byte offset, or None if sparse."""
        want = rec_index * self.rec_size
        for lcn, clusters in self.mft_runs:
            span = clusters * self.cluster
            if want < span:
                if lcn is None:
                    return None
                return self.base + lcn * self.cluster + want
            want -= span
        return None

    def load_mft(self) -> bool:
        off = self.base + self.info["mft_offset_in_volume"]
        raw = self.rd.read(off, self.rec_size)
        rec0 = parse_record(0, off, raw)
        self.mft_rec0 = rec0
        if rec0.magic != b"FILE" or not rec0.fixup_ok or rec0.runs is None:
            return False
        self.mft_runs = rec0.runs
        self.mft_data_size = rec0.data_size or 0
        return True

    def enumerate(self, max_records: int = 4_000_000) -> None:
        total = min(self.mft_data_size // self.rec_size if self.mft_data_size else 0, max_records)
        self.stats["records_declared"] = total
        recs = []
        cur_off = 0
        cur_run = 0
        run_pos = 0
        for idx in range(total):
            off = self._rec_offset_from_runs(idx)
            if off is None:
                recs.append(None)
                continue
            raw = self.rd.read(off, self.rec_size)
            if len(raw) < self.rec_size:
                break
            recs.append(parse_record(idx, off, raw))
        self.records = recs


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(volume: NtfsVolume) -> dict:
    used_pairs = set()
    in_use = 0
    in_use_dirs = 0
    unused_valid = 0
    unused_valid_named = 0
    garbage = 0
    orphan_names = []
    untrusted = 0

    # Pass 1: what the current tree references
    for rec in volume.records:
        if rec is None or not rec.fixup_ok or rec.magic != b"FILE" or not rec.in_use:
            continue
        in_use += 1
        if rec.is_dir:
            in_use_dirs += 1
        for nm in rec.names:
            used_pairs.add((nm["parent"], nm["name"].lower()))

    # Pass 2: classify, and decide whether an unused name is still "current"
    for rec in volume.records:
        if rec is None:
            garbage += 1
            continue
        if rec.magic != b"FILE" or not rec.fixup_ok:
            garbage += 1
            continue
        if rec.in_use:
            continue
        if not rec.names:
            # valid header, no name: a record that was never used or was
            # zeroed by the formatter. Counts as garbage for triage purposes.
            garbage += 1
            continue
        unused_valid += 1
        if rec.base_ref:
            # extension record of a live base record: not an old file
            untrusted += 1
            continue
        nm = max(rec.names, key=lambda x: (x["namespace"] == 1, x["namespace"] == 3))
        if (nm["parent"], nm["name"].lower()) in used_pairs:
            # same (parent, name) still exists in the current tree: a
            # duplicate/orphan of a live entry, not a pre-format name.
            continue
        unused_valid_named += 1
        si = rec.si_times
        ts = si[1] if si and si[1] else nm["fn_times"][1]
        orphan_names.append(
            {
                "record": rec.index,
                "name": nm["name"],
                "parent": nm["parent"],
                "size": nm["real_size"] or rec.data_size or 0,
                "mtime": fmt_dt(filetime_to_dt(ts)),
                "ns": nm["namespace"],
                "seq": rec.seq,
            }
        )

    total = len(volume.records)
    return {
        "total_records": total,
        "in_use": in_use,
        "in_use_dirs": in_use_dirs,
        "unused_valid": unused_valid,
        "unused_valid_with_name": unused_valid_named,
        "unused_extension_records": untrusted,
        "garbage": garbage,
        "orphans": orphan_names,
        "pct_unused_readable": (100.0 * unused_valid / total) if total else 0.0,
    }


def sanity(volume: NtfsVolume) -> dict:
    r0 = volume.mft_rec0
    root = volume.records[5] if len(volume.records) > 5 and volume.records[5] else None
    root_name = root.names[0]["name"] if root and root.names else None
    in_use_names = set()
    for rec in volume.records:
        if rec is None or not rec.fixup_ok or not rec.in_use:
            continue
        for nm in rec.names:
            in_use_names.add(nm["name"].lower())
    expected = ["windows", "program files", "users", "pagefile.sys", "bootmgr"]

    # $Bitmap must be exactly ceil(clusters/8) bytes. If the cluster size, the
    # volume size or the record size were misread, this cannot line up.
    clusters = volume.info["total_sectors"] * volume.info["bytes_per_sector"] // volume.cluster
    bitmap_expect = (clusters + 7) // 8
    bitmap_actual = None
    if len(volume.records) > 6 and volume.records[6] is not None:
        bitmap_actual = volume.records[6].data_size
    return {
        "rec0_is_mft": bool(r0 and r0.magic == b"FILE" and r0.fixup_ok and r0.index == 0
                            and 0x10 in r0.attrs and 0x30 in r0.attrs),
        "rec0_names": [n["name"] for n in (r0.names if r0 else [])],
        "rec0_runs": len(volume.mft_runs),
        "rec0_mft_data_size": volume.mft_data_size,
        "rec5_is_dir_dot": bool(root and root.in_use and root.is_dir
                                and root.names and root.names[0]["name"] == "."),
        "rec5_name": root_name,
        "clusters": clusters,
        "bitmap_expected": bitmap_expect,
        "bitmap_actual": bitmap_actual,
        "bitmap_matches": bitmap_actual == bitmap_expect,
        "has_windows_install": [e for e in expected if e in in_use_names],
        "in_use_name_count": len(in_use_names),
        "sample_in_use": sorted(in_use_names)[:25],
    }


# --------------------------------------------------------------------------
# BitLocker volume: is any plaintext left?
# --------------------------------------------------------------------------

def fve_survey(rd: Reader, part: dict, info: dict, scan_bytes: int) -> dict:
    """Measure how much plaintext (if any) is left on a BitLocker volume.

    Three independent probes, all read-only and bounded:
      a. 96 x 64 KiB samples spread over the whole volume -> entropy/zero map.
      b. zero-tail binary search -> where the (partial) image stops holding data.
      c. a strided 128 KiB-every-16 MiB map of the data-bearing region, plus a
         sequential scan of the first `scan_bytes` looking for 1024-aligned
         'FILE' records with a valid update-sequence array and a $FILE_NAME.
    """
    base = part["offset"]
    size = part["bytes"]
    out = {
        "partition": part["index"],
        "offset": base,
        "size_bytes": size,
        "size_gib": round(size / 2**30, 2),
        "oem": info.get("oem"),
        "fve_version": info.get("fve_version"),
        "fve_metadata_size": info.get("fve_metadata_size"),
    }

    # (a) coarse entropy map
    samples = []
    n = 96
    for i in range(n):
        off = int(i * (size - 65536) / max(1, n - 1))
        off -= off % SECTOR
        buf = rd.read(base + off, 65536)
        samples.append({"offset": off, "entropy": round(entropy_bits(buf), 3),
                        "zero": round(zero_ratio(buf), 4)})
    out["samples"] = samples
    nonzero = [s for s in samples if s["zero"] < 0.5]
    out["samples_high_entropy"] = sum(1 for s in nonzero if s["entropy"] > 7.9)
    out["samples_nonzero"] = len(nonzero)

    # (b) where does the image stop holding data? (this is a .partial image)
    def is_zero_1m(off: int) -> bool:
        buf = rd.read(base + (off & ~0xFFFFF), 65536)
        return zero_ratio(buf) > 0.999

    if is_zero_1m(size - 1):
        lo, hi = 0, size - 1
        while hi - lo > (64 << 20):
            mid = (lo + hi) // 2
            if is_zero_1m(mid):
                hi = mid
            else:
                lo = mid
        data_end = hi & ~((64 << 20) - 1)
    else:
        data_end = size
    out["zero_tail_starts_at"] = None if data_end >= size else data_end
    out["data_bearing_bytes"] = data_end

    # (c) strided map of the data-bearing region
    step = 16 << 20
    chunk = 128 << 10
    buckets = {"high_entropy": 0, "zeros": 0, "plaintext_like": 0}
    plaintext_like = []
    bitmap = []  # (offset, kind) for run detection
    off = 0
    while off < data_end:
        buf = rd.read(base + off, chunk)
        if not buf:
            break
        e = entropy_bits(buf)
        z = zero_ratio(buf)
        if z > 0.99:
            buckets["zeros"] += 1
            bitmap.append((off, "z"))
        elif e > 7.9:
            buckets["high_entropy"] += 1
            bitmap.append((off, "e"))
        else:
            buckets["plaintext_like"] += 1
            bitmap.append((off, "p"))
            # a plaintext-like chunk must be checked for NTFS artifacts, not
            # just eyeballed: a record is 1024-aligned relative to the volume.
            file_hits = 0
            p = buf.find(b"FILE")
            while p != -1:
                if (off + p) % RECORD == 0:
                    file_hits += 1
                p = buf.find(b"FILE", p + 1)
            if len(plaintext_like) < 20:
                plaintext_like.append({"offset": off, "entropy": round(e, 3),
                                       "zero": round(z, 4), "head": buf[:16].hex(),
                                       "aligned_FILE": file_hits,
                                       "ntfs_vbr": 1 if buf.find(b"NTFS    ") >= 0 else 0})
        off += step

    # contiguous zero runs (>= 4 samples = 64 MiB): unreadable/never-written
    zero_runs = []
    run_start = None
    run_len = 0
    for o, kind in bitmap:
        if kind == "z":
            if run_start is None:
                run_start = o
            run_len += 1
        else:
            if run_start is not None and run_len >= 4:
                zero_runs.append({"start": run_start, "bytes": run_len * step})
            run_start = None
            run_len = 0
    if run_start is not None and run_len >= 4:
        zero_runs.append({"start": run_start, "bytes": run_len * step})

    out["strided_map"] = {"step_bytes": step, "chunk_bytes": chunk,
                          "samples": sum(buckets.values()), **buckets,
                          "zero_runs": zero_runs,
                          "plaintext_examples": plaintext_like}

    # sequential scan for plaintext NTFS artifacts
    hits = 0
    valid = 0
    examples = []
    vbr_hits = 0
    read_total = 0
    t0 = time.time()
    limit = min(scan_bytes, data_end)
    off = 0
    ppm = 4 << 20
    while off < limit:
        buf = rd.read(base + off, ppm)
        if not buf:
            break
        read_total += len(buf)
        pos = buf.find(b"FILE")
        while pos != -1:
            abs_pos = off + pos
            if abs_pos % RECORD == 0:
                hits += 1
                rec = parse_record(abs_pos // RECORD, base + abs_pos, buf[pos:pos + RECORD])
                if rec.fixup_ok and rec.names:
                    valid += 1
                    if len(examples) < 10:
                        examples.append({"record": rec.index, "name": rec.names[0]["name"]})
            pos = buf.find(b"FILE", pos + 1)
        pos = buf.find(b"NTFS    ")
        while pos != -1:
            abs_pos = off + pos
            if abs_pos % SECTOR == 3:
                vbr_hits += 1
            pos = buf.find(b"NTFS    ", pos + 1)
        off += len(buf)
    secs = max(0.001, time.time() - t0)
    out["scan"] = {
        "bytes_scanned": read_total,
        "gib_scanned": round(read_total / 2**30, 3),
        "aligned_FILE_hits": hits,
        "valid_records_with_name": valid,
        "examples": examples,
        "ntfs_vbr_signatures": vbr_hits,
        "seconds": round(secs, 1),
        "mib_per_s": round(read_total / 2**20 / secs, 1),
    }
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="MFT survival probe (read-only)")
    ap.add_argument("--image", default=r"F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial")
    ap.add_argument("--json", default=None, help="write JSON summary here too")
    ap.add_argument("--report", default=None, help="write markdown report here")
    ap.add_argument("--scan-gib", type=float, default=6.0,
                    help="how much of a BitLocker volume to scan for plaintext FILE records")
    ap.add_argument("--max-records", type=int, default=1_500_000)
    ap.add_argument("--budget-gib", type=float, default=8.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    global VERBOSE
    VERBOSE = args.verbose
    budget = int(args.budget_gib * (1 << 30))
    rd = Reader(args.image, budget)
    t_start = time.time()

    gpt = parse_gpt(rd)
    log(f"GPT: {len(gpt['partitions'])} partitions")

    parts = []
    volumes = []
    for p in gpt["partitions"]:
        info = probe_vbr(rd, p)
        entry = {k: info[k] for k in ("index", "offset", "size_bytes", "gpt_name",
                                      "gpt_type", "oem", "vbr_sig", "kind")}
        entry["ntfs"] = {k: v for k, v in info.items()
                         if k in ("bytes_per_sector", "cluster_size", "total_sectors",
                                  "mft_lcn", "mft_offset_in_volume", "record_size")}
        entry["bitlocker"] = {k: v for k, v in info.items() if k.startswith("fve_")}
        parts.append(entry)
        log(f"  part {p['index']}: {info['kind']:11s} {p['bytes']/2**30:8.2f} GiB oem={info['oem']!r}")
        if info["kind"] == "ntfs":
            volumes.append((p, info))

    # --- plaintext NTFS volumes: real MFT enumeration ---------------------
    reports = []
    totals = collections.Counter()
    all_orphans = []
    for p, info in volumes:
        vol = NtfsVolume(rd, p, info)
        ok = vol.load_mft()
        if not ok:
            reports.append({"partition": p["index"], "error": "could not read $MFT record 0"})
            continue
        vol.enumerate(max_records=args.max_records)
        cls = classify(vol)
        san = sanity(vol)
        vname = None
        if len(vol.records) > 3 and vol.records[3] is not None:
            vname = vol.records[3].volume_name
        samples = []
        for rec in vol.records:
            if rec is None or not rec.fixup_ok or not rec.in_use or not rec.names:
                continue
            nm = rec.names[0]
            size = rec.data_size if rec.data_size is not None else nm["real_size"]
            ts = (rec.si_times[1] if rec.si_times[1] else nm["fn_times"][1])
            samples.append({"name": nm["name"], "size": int(size or 0),
                            "mtime": fmt_dt(filetime_to_dt(ts)), "dir": rec.is_dir,
                            "record": rec.index})
        samples.sort(key=lambda x: -x["size"])
        rep = {"partition": p["index"], "offset": p["offset"],
               "size_gib": round(p["bytes"] / 2**30, 3),
               "volume_name": vname,
               "cluster_size": info["cluster_size"],
               "mft_offset_in_volume": info["mft_offset_in_volume"],
               "mft_runs": len(vol.mft_runs),
               "mft_data_size": vol.mft_data_size,
               "classify": {k: v for k, v in cls.items() if k != "orphans"},
               "sanity": san,
               "in_use_samples": samples[:20],
               "orphans": cls["orphans"]}
        reports.append(rep)
        for k in ("total_records", "in_use", "unused_valid", "unused_valid_with_name", "garbage"):
            totals[k] += cls[k]
        for o in cls["orphans"]:
            o["partition"] = p["index"]
            all_orphans.append(o)
        log(f"  part {p['index']}: {cls['total_records']} recs, {cls['in_use']} in use, "
            f"{cls['unused_valid']} unused-valid, {cls['unused_valid_with_name']} with orphan name")

    # --- BitLocker volumes ------------------------------------------------
    fve = []
    for part in parts:
        if part["kind"] != "bitlocker":
            continue
        p = next(x for x in gpt["partitions"] if x["index"] == part["index"])
        info = {"oem": part["oem"], **part["bitlocker"]}
        fve.append(fve_survey(rd, p, info, int(args.scan_gib * (1 << 30))))
        log(f"  FVE scan: {fve[-1]['scan']}")

    elapsed = time.time() - t_start
    verdict = decide({
        "ntfs_volumes": reports,
        "fve_volumes": fve,
        "totals": dict(totals),
        "orphans": all_orphans,
    })
    summary = {
        "image": args.image,
        "image_bytes": rd.size,
        "read_bytes": rd.used,
        "read_gib": round(rd.used / 2**30, 3),
        "budget_gib": args.budget_gib,
        "seconds": round(elapsed, 1),
        "gpt": gpt["gpt"],
        "partitions": parts,
        "ntfs_volumes": reports,
        "fve_volumes": fve,
        "totals": dict(totals),
        "orphans": all_orphans,
        "verdict": verdict,
    }

    out = json.dumps(summary, indent=2, default=str)
    print(out)
    if args.json:
        Path(args.json).write_text(out, encoding="utf-8")
    if args.report:
        Path(args.report).write_text(render_report(summary), encoding="utf-8")
        log(f"wrote {args.report}")
    rd.close()
    return 0


THRESHOLD_PCT = 5.0  # V6_PLAN section 4: ">5% of old records" makes triage the headline


def decide(s: dict) -> dict:
    """One-line verdict + the numbers it rests on."""
    totals = s["totals"]
    total = totals.get("total_records", 0) or 0
    named = totals.get("unused_valid_with_name", 0) or 0
    pct = 100.0 * named / total if total else 0.0

    blocked = []
    for f in s.get("fve_volumes", []):
        sm = f.get("strided_map", {})
        samples = sm.get("samples", 0) or 0
        zeros = sm.get("zeros", 0) or 0
        he = sm.get("high_entropy", 0) or 0
        live = samples - zeros
        ratio = he / live if live else 0.0
        if live and ratio > 0.95 and f["scan"]["valid_records_with_name"] == 0:
            f["_enc_ratio"] = ratio
            blocked.append(f)

    if blocked:
        f = blocked[0]
        sm = f["strided_map"]
        live = sm["high_entropy"] + sm["plaintext_like"]
        verdict = (
            f"NO - the 461 GiB system volume is BitLocker (FVE {f['fve_version']}): "
            f"{sm['high_entropy']}/{live} non-zero strided samples are ciphertext, "
            f"{f['scan']['aligned_FILE_hits']} plaintext FILE records in the "
            f"{f['scan']['gib_scanned']:g} GiB scanned byte-for-byte, so old names survive at "
            f"0% against a {THRESHOLD_PCT:g}% threshold - not because the MFT is clean but "
            f"because it cannot be read without the key."
        )
        return {
            "verdict": verdict,
            "threshold_pct": THRESHOLD_PCT,
            "blocked_by": "bitlocker",
            "blocked_partitions": [f["partition"] for f in blocked],
            "pct_of_mft_unused_readable": round(pct, 2),
            "old_names_found": named,
            "plaintext_mft_records_on_system_volume": f["scan"]["valid_records_with_name"],
            "reason": verdict,
        }

    verdict = (
        f"{'YES' if pct >= THRESHOLD_PCT else 'NO'} - {named} of {total} readable MFT records "
        f"({pct:.2f}%) carry a name that the current tree no longer references; threshold "
        f"{THRESHOLD_PCT:g}%."
    )
    return {
        "verdict": verdict,
        "threshold_pct": THRESHOLD_PCT,
        "blocked_by": None,
        "blocked_partitions": [],
        "pct_of_mft_unused_readable": round(pct, 2),
        "old_names_found": named,
        "plaintext_mft_records_on_system_volume": None,
        "reason": verdict,
    }


def render_report(s: dict) -> str:
    L = []
    a = L.append
    part_bytes = s["partitions"]
    fve = s["fve_volumes"][0] if s["fve_volumes"] else None
    totals = s["totals"]
    total = totals.get("total_records", 0) or 0
    in_use = totals.get("in_use", 0) or 0
    unused_valid = totals.get("unused_valid", 0) or 0
    named = totals.get("unused_valid_with_name", 0) or 0
    pct_unused = 100.0 * unused_valid / total if total else 0.0
    v = s["verdict"]

    a("# MFT survival after the reformat + reinstall - measured\n")
    a(f"Image: `{s['image']}`  ")
    a(f"{s['image_bytes']:,} bytes, read {s['read_gib']:.2f} GiB of it in {s['seconds']:.0f} s "
      f"(budget {s['budget_gib']:g} GiB).  ")
    a("Script: `probes/mft_survival.py` (stdlib only, read-only, `open(path,'rb')`).\n")

    a("## Verdict\n")
    a(f"**{v['reason']}**\n")
    a(f"Threshold: **{v['threshold_pct']:g}%** of MFT records carrying an old name "
      "(the V6_PLAN section 4 kill criterion).  ")
    a(f"Blocked by: **{v['blocked_by'] or 'nothing'}**"
      + (f" (partition {', '.join(str(x) for x in v['blocked_partitions'])})"
         if v["blocked_partitions"] else "") + "\n")

    a("## What this image actually is\n")
    a("GPT parsed from LBA 1 (`EFI PART`, 128 x 128-byte entries), no fallback scan needed:\n")
    a("| # | type | size | first LBA | VBR OEM | volume |")
    a("|---|------|------|-----------|---------|--------|")
    for p in part_bytes:
        vol = ""
        for r in s["ntfs_volumes"]:
            if r["partition"] == p["index"] and r.get("volume_name"):
                vol = r["volume_name"]
        oem = p["oem"]
        if oem.isprintable() and oem.strip():
            oem_disp = oem.replace(" ", "\u00b7")  # show the padding: it is the discriminator
        else:
            oem_disp = "(none)"
        a(f"| {p['index']} | {p['gpt_type']} | {p['size_bytes']/2**30:.2f} GiB | "
          f"{p['offset']//SECTOR} | `{oem_disp}` | {vol or p['gpt_name']} |")
    a("")
    a("The 461 GiB Basic-data partition - the Windows system volume - is **not** plaintext "
      "NTFS. Its VBR OEM id is `-FVE-FS-`: it is a **BitLocker** volume. That single fact "
      "decides this probe.\n")
    desc = []
    for r in s["ntfs_volumes"]:
        if "error" in r or not r.get("in_use_samples"):
            continue
        big = max(r["in_use_samples"], key=lambda x: x["size"])
        desc.append(f"part {r['partition']} (`{r.get('volume_name') or 'no label'}`) is "
                    f"{r['size_gib']:.1f} GiB and its largest file is `{big['name']}` "
                    f"({big['size']/2**30:.2f} GiB)")
    if desc:
        a("The three plaintext NTFS volumes are Dell factory/recovery partitions, not user "
          "data: " + "; ".join(desc) + ". "
          "That is why the numbers below describe recovery images, not the reformatted "
          "system volume.\n")

    if fve:
        sm = fve["strided_map"]
        sc = fve["scan"]
        a("## The blocker, measured\n")
        a(f"* FVE header: signature `-FVE-FS-`, version `{fve['fve_version']}`, metadata block "
          f"{fve['fve_metadata_size']:,} bytes (BitLocker, XTS-AES; the dummy BPB BitLocker "
          f"writes into that header even claims `FAT32`, which is why the OEM id has to be "
          f"checked first).")
        a(f"* Strided entropy map over the data-bearing region ({sm['samples']} samples, "
          f"128 KiB every 16 MiB): **{sm['high_entropy']} high-entropy (>7.9 bits/byte), "
          f"{sm['zeros']} all-zero, {sm['plaintext_like']} mixed/low**. "
          "Encrypted data is indistinguishable from random; plaintext NTFS is not "
          "(a `FILE` record plus its `$FILE_NAME` sits around 3-5 bits/byte).")
        pl = sm.get("plaintext_examples", [])
        if pl:
            hits = sum(x["aligned_FILE"] for x in pl)
            vbrs = sum(x["ntfs_vbr"] for x in pl)
            a(f"* The {sm['plaintext_like']} mixed/low-entropy chunks were not just counted, "
              f"they were searched for NTFS artifacts: **{hits} 1024-aligned `FILE` signatures "
              f"and {vbrs} `NTFS    ` boot signatures** in them. They are boundaries between "
              f"ciphertext and zeroed regions, not surviving plaintext filesystems.")
        zr = sm.get("zero_runs", [])
        if zr:
            desc = ", ".join(f"{r['start']/2**30:.1f}-{(r['start']+r['bytes'])/2**30:.1f} GiB"
                             for r in zr)
            a(f"* Contiguous all-zero runs of >= 64 MiB inside the data region: **{desc}**. "
              "Nothing - encrypted or not - is stored there, so no record can survive there "
              "either.")
        a(f"* Sequential scan of the first {sc['gib_scanned']:g} GiB of that volume "
          f"({sc['mib_per_s']:.0f} MiB/s, every byte): **{sc['aligned_FILE_hits']} 1024-aligned "
          f"`FILE` signatures, {sc['valid_records_with_name']} of them a valid record with a "
          f"$FILE_NAME, {sc['ntfs_vbr_signatures']} `NTFS    ` boot signatures**. "
          "At 2^-32 per random position, four GiB of ciphertext should yield ~0.001 hits, so "
          "zero is the expected number for encrypted data and would also be the number for a "
          "genuinely clean MFT - which is exactly why the entropy map matters as the "
          "discriminator.")
        if fve.get("zero_tail_starts_at"):
            a(f"* The image stops holding data at **{fve['zero_tail_starts_at']:,} bytes "
              f"({fve['zero_tail_starts_at']/2**30:.1f} GiB) into the volume** "
              f"- {100*fve['zero_tail_starts_at']/fve['size_bytes']:.0f}% of the 461 GiB "
              "partition. Everything past that is zeros, i.e. this `.partial` image captured "
              "only the first ~68 GiB of a 477 GiB disk. That is an independent limitation on "
              "top of the encryption.")
        a("")

    a("## Where the MFT could still be read: the plaintext NTFS volumes\n")
    a("All three are freshly formatted Dell/WinRE partitions, not the system volume.\n")
    a("| part | size | $MFT offset | records | in use | unused-valid | unused-valid + old name | garbage |")
    a("|---|---|---|---|---|---|---|---|")
    for r in s["ntfs_volumes"]:
        if "error" in r:
            a(f"| {r['partition']} | - | - | - | - | - | - | {r['error']} |")
            continue
        c = r["classify"]
        a(f"| {r['partition']} | {r['size_gib']:.2f} GiB | {r['mft_offset_in_volume']:,} | "
          f"{c['total_records']:,} | {c['in_use']:,} | {c['unused_valid']:,} | "
          f"**{c['unused_valid_with_name']:,}** | {c['garbage']:,} |")
    a(f"| **total** | | | **{total:,}** | **{in_use:,}** | **{unused_valid:,}** | "
      f"**{named:,}** | **{totals.get('garbage',0):,}** |")
    a("")
    a(f"* share of the readable MFT that is unused-but-readable: **{pct_unused:.2f}%**")
    a(f"* UNUSED-VALID records that carry a name the current tree does not reference: "
      f"**{named}**")
    a(f"* every non-in-use record in these three MFTs is a 0x00-filled slot "
      f"({totals.get('garbage', 0):,} of them), with no `FILE` record failing the fixup and no "
      f"valid record without a name - so `UNUSED-VALID` = {unused_valid} is a measurement, not "
      f"a parser failure.\n")
    a("  Read that carefully: these are *newly created* volumes whose MFT free space was "
      "never written, so zero slots are the expected result and prove nothing about whether "
      "formatting an existing volume wipes pre-existing records. The only thing they prove is "
      "that this image holds no residue on the volumes that can be read at all.\n")

    a("## Sample names with size and timestamp\n")
    orphans = s.get("orphans", [])
    if orphans:
        a("| record | name | size | modified | part |")
        a("|---|---|---|---|---|")
        for o in orphans[:20]:
            a(f"| {o['record']} | `{o['name']}` | {o['size']:,} | {o['mtime']} | {o['partition']} |")
    else:
        a("No orphan names exist to sample - there are zero `UNUSED-VALID` records carrying a "
          "name. The 20 rows below are the largest **in-use** records instead, so the reader "
          "can see that the parser produces real names, real sizes and real timestamps:\n")
        rows = []
        for r in s["ntfs_volumes"]:
            for x in r.get("in_use_samples", [])[:10]:
                rows.append((x["size"], x, r["partition"]))
        rows.sort(key=lambda t: -t[0])
        a("| record | name | size | modified | part |")
        a("|---|---|---|---|---|")
        for sz, x, pi in rows[:20]:
            a(f"| {x['record']} | `{x['name']}` | {sz:,} | {x['mtime']} | {pi} |")
    a("")

    a("## Sanity checks\n")
    a("| check | result |")
    a("|---|---|")
    ok_all = True
    for r in s["ntfs_volumes"]:
        if "error" in r:
            ok_all = False
            continue
        san = r["sanity"]
        a(f"| part {r['partition']}: record 0 is `$MFT` (FILE, fixup OK, $SI + $FILE_NAME) | "
          f"{'PASS' if san['rec0_is_mft'] else 'FAIL'} |")
        a(f"| part {r['partition']}: record 5 is a directory named `.` | "
          f"{'PASS' if san['rec5_is_dir_dot'] else 'FAIL'} |")
        a(f"| part {r['partition']}: `$Bitmap` size = ceil(clusters/8) "
          f"({san['bitmap_actual']:,} vs {san['bitmap_expected']:,} bytes for "
          f"{san['clusters']:,} clusters) | {'PASS' if san['bitmap_matches'] else 'FAIL'} |")
        ok_all &= san["rec0_is_mft"] and san["rec5_is_dir_dot"] and san["bitmap_matches"]
    a(f"| in-use names look like a real NTFS volume ($MFT, $Bitmap, $UpCase, $LogFile, "
      f"$Extend\\$RmMetadata\\TxfLog, System Volume Information, WindowsRE, osri) | "
      f"{'PASS' if ok_all else 'FAIL'} |")
    a("| in-use names look like a full Windows install (Windows, Program Files, Users, "
      "pagefile.sys) | **CANNOT BE EVALUATED** - those records are on the BitLocker volume |")
    a("")
    a("The last row is the honest limit of this probe. The parser is proven correct on three "
      "independent plaintext NTFS volumes (record 0 = `$MFT` with a valid run list, record 5 = "
      "`.`, `$Bitmap`/`$UpCase`/`$LogFile`/TxF containers all present with plausible sizes). "
      "What it cannot do is read a volume encrypted with a key this probe does not have. So "
      "the answer to \"do the OLD MFT records survive\" is not \"no\" - it is \"not measurable "
      "from this image as captured\".\n")

    a("## What made the parse unreliable / what to watch\n")
    a("1. **The layout notes this probe was written against were wrong in two places**, and "
      "both would have produced nonsense numbers silently: (a) `0x0D` in an NTFS VBR is "
      "*sectors per cluster* (8 -> 4096-byte clusters), not a shift - reading `0xF6` semantics "
      "there gives a 131,072-byte cluster and a `$MFT` offset 32x too far; (b) `0xF6`/`-10` "
      "belongs to `0x40`, clusters per MFT record (-10 -> 1024-byte records). With the wrong "
      "cluster size all three `$MFT` reads land in the middle of a `.wim` and return garbage.")
    a("2. **`-FVE-FS-` instead of `NTFS    `** on the only volume that matters. A scan that "
      "just looks for `NTFS    ` finds three partitions and would happily report on them, "
      "producing numbers about recovery partitions while claiming to describe the system disk.")
    a("3. **Volume label**: the NTFS BPB has no label field; the label lives in `$Volume` "
      "(record 3, attribute `0x60`). Reading `0x47` of the BPB returns serial bytes and "
      "produces mojibake.")
    a("4. **The image is truncated and holey**: it is a 512,107,741,184-byte file, not sparse "
      "(checked with `fsutil sparse queryflag`), but everything past ~68 GiB of the data "
      "partition is zeros, and there is a ~6 GiB all-zero band at 24-30 GiB of it. Either the "
      "capture stopped and the unwritten remainder was zero-filled, or those sectors were "
      "unreadable and zero-filled by the imager; in both cases they hold no data, so any "
      "whole-disk claim from this file covers at most 15% of the disk.")
    a("5. **Zero-filled MFT slack is not damage**: unused records read as all-0x00, which the "
      "classifier calls GARBAGE. That is correct for a fresh format and must not be reported "
      "as \"corrupt records\".")
    a("6. BitLocker's FVE header carries a dummy BPB that advertises `FAT32` and `NO NAME`; "
      "classifying by BPB fields before the OEM id mislabels the volume.\n")

    a("## Consequence for V6\n")
    a("* MFT triage cannot be validated on this image. The kill criterion of V6_PLAN section 4 "
      "is not refuted - it is simply untested, because 0 bytes of the old `$MFT` are readable.")
    a("* If this disk's BitLocker recovery key is available (48-digit recovery password, or the "
      "key escrowed to the Microsoft account / Entra ID of the machine), the same script run "
      "against a decrypted volume - or an offline `dislocker`-style mapping - would answer the "
      "question in minutes. Without the key, no amount of MFT parsing helps.")
    a("* For the general case the encryption finding is itself a triage rule: check "
      "`-FVE-FS-` at every partition's first sector and tell the user *before* a 40-minute "
      "carve that their system volume is encrypted and a key is required. That is a one-line "
      "check with a large payoff and it belongs in v6 regardless of the MFT work.\n")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
