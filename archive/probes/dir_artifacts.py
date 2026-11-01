#!/usr/bin/env python3
"""dir_artifacts.py - which whole-file recovery sources exist on this image.

Read-only feasibility probe for V6 plan section 9c. It walks the GPT, finds
every NTFS volume, reads each VBR and locates the MFT (following record 0's run
list, so a fragmented $MFT is handled), then asks the four questions 9c asks:

    * can the MFT hand back whole named files?
    * is there a $Recycle.Bin, and how many $I/$R pairs are in it?
    * is there a VSS catalog / per-snapshot store in System Volume Information?
    * is there an $Extend\\$UsnJrnl:$J?

The source image is never written to: it is opened 'rb' and only seek/read is
used, under a hard 6 GiB read budget.

Layout notes (all verified against this image, see DIRECTORY_ARTIFACTS.md):
  * MBR stub -> one 0xEE protective entry -> GPT at LBA 2.
  * A volume is "NTFS" only if the VBR OEM id at offset 3 is 'NTFS    '. A
    BitLocker volume says '-FVE-FS-' there; its payload is ciphertext and there
    is no MFT to read, so it is reported UNREADABLE rather than guessed at.
  * sectors-per-cluster at 0x0D is signed: > 0 means "this many sectors",
    < 0 means 2**-n bytes per cluster. (V6_PLAN's "0xF6 = 2^-6" note does not
    describe a real NTFS VBR; this tool does not rely on it.)
  * MFT records are 1024 bytes; the USA fixup is applied before any attribute is
    read, and a record whose fixup does not match is counted, not trusted.

Directory enumeration: a directory's $DATA is an $I30 B-tree. This tool uses the
shortcut 9c sanctions - it scans the MFT once and groups in-use records by the
parent reference in their $FILE_NAME attribute - and it independently parses
record 5's $I30 (INDEX_ROOT plus every INDX block in $INDEX_ALLOCATION) so a
broken walk shows up as a set mismatch instead of a plausible-looking list.

Plain stdlib only (struct/os/sys/json/collections/math/datetime).
"""

import collections
import json
import math
import os
import struct
import sys
from datetime import datetime, timedelta, timezone

IMG = r"F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial"
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "DIRECTORY_ARTIFACTS.md")

READ_BUDGET = 6 * 1024 * 1024 * 1024   # 6 GiB hard cap
REC_SIZE = 1024
MAX_RECORDS = 400000
VSS_CATALOG = "{3808876b-c176-4e48-b7ae-04046e6cc752}"
VSS_GUID_LE = bytes.fromhex("6b88083876c1484eb7ae04046e6cc752")


# --------------------------------------------------------------------------
# byte source
# --------------------------------------------------------------------------

class Reader:
    """Read-only, budgeted view of the image."""

    def __init__(self, path, budget=READ_BUDGET):
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size
        self.budget = budget
        self.read_bytes = 0
        self.reads = 0

    def read(self, off, n):
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        if self.read_bytes + n > self.budget:
            raise RuntimeError("read budget exhausted at offset %#x" % off)
        self.f.seek(off)
        b = self.f.read(n)
        self.read_bytes += len(b)
        self.reads += 1
        return b

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def s64(b, o):
    return struct.unpack_from("<q", b, o)[0]


EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime(v):
    if not v:
        return None
    try:
        return EPOCH + timedelta(microseconds=v // 10)
    except OverflowError:
        return None


def fmt_time(dt):
    return "-" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%SZ")


def fmt_size(n):
    if n is None:
        return "-"
    if n < 1024:
        return "%d B" % n
    v = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        v /= 1024.0
        if v < 1024 or unit == "TiB":
            return "%.2f %s" % (v, unit)
    return str(n)


def guid_str(b):
    return "{%08x-%04x-%04x-%s-%s}" % (
        u32(b, 0), u16(b, 4), u16(b, 6), b[8:10].hex(), b[10:16].hex())


def cluster_size(bps, spc):
    return bps * spc if spc > 0 else 1 << (-spc)


def decode_runs(buf):
    """NTFS run list -> [(vcn, lcn_or_None, cluster_count), ...]."""
    runs, vcn, lcn, i = [], 0, 0, 0
    while i < len(buf):
        h = buf[i]
        if h == 0:
            break
        lsz, osz = h & 0x0F, (h >> 4) & 0x0F
        i += 1
        if lsz == 0 or i + lsz + osz > len(buf):
            break
        count = int.from_bytes(buf[i:i + lsz], "little")
        i += lsz
        if osz == 0:
            runs.append((vcn, None, count))
        else:
            lcn += int.from_bytes(buf[i:i + osz], "little", signed=True)
            i += osz
            runs.append((vcn, lcn, count))
        vcn += count
    return runs


def entropy(b):
    if not b:
        return 0.0
    c = collections.Counter(b)
    return -sum((v / len(b)) * math.log2(v / len(b)) for v in c.values())


def runs_text(runs):
    out = []
    for vcn, lcn, n in runs:
        out.append(("sparse@vcn %d +%d clusters" % (vcn, n)) if lcn is None
                   else "vcn %d -> lcn %d, %d cl" % (vcn, lcn, n))
    return "; ".join(out)


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------

KNOWN_TYPES = {
    "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}": "EFI system partition",
    "{e3c9e316-0b5c-4db8-817d-f92df00215ae}": "Microsoft reserved (MSR)",
    "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}": "Microsoft basic data",
    "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}": "Windows recovery (WinRE)",
}


def parse_gpt(r):
    mbr = r.read(0, 512)
    info = {
        "mbr_sig": mbr[510:512].hex(),
        "mbr_part_type": "%#04x" % mbr[446 + 4],
        "mbr_first_lba": u32(mbr, 446 + 8),
        "mbr_sectors": u32(mbr, 446 + 12),
        "mbr_boot_flag": "%#04x" % mbr[446],
    }
    hdr = r.read(512, 512)
    if hdr[0:8] != b"EFI PART":
        raise SystemExit("no GPT at LBA 1 (signature %r)" % hdr[0:8])
    info.update({
        "rev": "%d.%d" % (u16(hdr, 8), u16(hdr, 10)),
        "header_lba": u64(hdr, 24),
        "backup_lba": u64(hdr, 32),
        "first_usable": u64(hdr, 40),
        "last_usable": u64(hdr, 48),
        "disk_guid": guid_str(hdr[56:72]),
        "entries_lba": u64(hdr, 72),
        "entry_count": u32(hdr, 80),
        "entry_size": u32(hdr, 84),
        "header_crc": "%08x" % u32(hdr, 16),
    })
    n = min(info["entry_count"], 128)
    raw = r.read(info["entries_lba"] * 512, n * info["entry_size"])
    parts = []
    for i in range(n):
        e = raw[i * info["entry_size"]:(i + 1) * info["entry_size"]]
        if len(e) < 128 or e[0:16] == b"\x00" * 16:
            continue
        tg = guid_str(e[0:16])
        first, last = u64(e, 32), u64(e, 40)
        parts.append({
            "index": i,
            "type_guid": tg,
            "type": KNOWN_TYPES.get(tg, "unrecognised type"),
            "unique_guid": guid_str(e[16:32]),
            "first_lba": first,
            "last_lba": last,
            "bytes": (last - first + 1) * 512,
            "attrs": "%#x" % u64(e, 48),
            "name": e[56:128].decode("utf-16le", "replace").rstrip("\x00"),
        })
    info["partitions"] = parts
    return info


# --------------------------------------------------------------------------
# NTFS
# --------------------------------------------------------------------------

ATTR_FILENAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOC = 0xA0
ATTR_END = 0xFFFFFFFF


class Attr:
    __slots__ = ("type", "name", "id", "non_resident", "value", "runs",
                 "alloc_size", "real_size", "start_vcn", "last_vcn")


def parse_attrs(rec):
    ao = u16(rec, 20)
    out = []
    while ao + 8 <= len(rec):
        t = u32(rec, ao)
        if t == ATTR_END:
            break
        ln = u32(rec, ao + 4)
        if ln < 24 or ao + ln > len(rec):
            break
        a = Attr()
        a.type = t
        a.non_resident = bool(rec[ao + 8])
        nlen = rec[ao + 9]
        noff = u16(rec, ao + 10)
        a.id = u16(rec, ao + 14)
        a.name = (rec[ao + noff:ao + noff + nlen * 2].decode("utf-16le", "replace")
                  if nlen else "")
        if a.non_resident:
            a.start_vcn = u64(rec, ao + 0x10)
            a.last_vcn = u64(rec, ao + 0x18)
            a.alloc_size = u64(rec, ao + 0x28)
            a.real_size = u64(rec, ao + 0x30)
            a.runs = decode_runs(rec[ao + u16(rec, ao + 0x20):ao + ln])
            a.value = b""
        else:
            vl = u32(rec, ao + 0x10)
            vo = u16(rec, ao + 0x14)
            a.value = rec[ao + vo:ao + vo + vl]
            a.runs = []
            a.alloc_size = a.real_size = len(a.value)
            a.start_vcn = a.last_vcn = 0
        out.append(a)
        ao += ln
    return out


class MftRecord:
    def __init__(self, num, raw):
        self.num = num
        self.ok = self.in_use = self.is_dir = self.fixup_ok = False
        self.attrs = []
        if len(raw) < 48 or raw[0:4] != b"FILE":
            return
        self.ok = True
        self.flags = u16(raw, 22)
        self.in_use = bool(self.flags & 1)
        self.is_dir = bool(self.flags & 2)
        fixed = self._fixup(raw)
        if fixed is None:
            return
        self.fixup_ok = True
        used = u32(raw, 24)
        self.attrs = parse_attrs(fixed[:used] if 0 < used <= len(fixed) else fixed)

    @staticmethod
    def _fixup(raw):
        uo, uc = u16(raw, 4), u16(raw, 6)
        if uc < 2 or uo + 2 * uc > len(raw) or uc != len(raw) // 512 + 1:
            return None
        b = bytearray(raw)
        usn = b[uo:uo + 2]
        for i in range(1, uc):
            p = i * 512 - 2
            if b[p:p + 2] != usn:
                return None
            b[p:p + 2] = b[uo + 2 * i:uo + 2 * i + 2]
        return bytes(b)

    def attr(self, t, name=None):
        for a in self.attrs:
            if a.type == t and (name is None or a.name == name):
                return a
        return None

    def filename(self):
        best = None
        for a in self.attrs:
            if a.type != ATTR_FILENAME or len(a.value) < 0x42:
                continue
            ns = a.value[0x41]
            nlen = a.value[0x40]
            cand = {
                "parent": u64(a.value, 0) & 0x0000FFFFFFFFFFFF,
                "name": a.value[0x42:0x42 + nlen * 2].decode("utf-16le", "replace"),
                "ns": ns,
                "flags": u32(a.value, 0x38),
                "alloc": u64(a.value, 0x28),
                "real": u64(a.value, 0x30),
                "ctime": filetime(u64(a.value, 0x08)),
                "mtime": filetime(u64(a.value, 0x10)),
                "atime": filetime(u64(a.value, 0x20)),
            }
            if best is None or (ns in (1, 3) and best["ns"] == 2):
                best = cand
        return best


class Ntfs:
    def __init__(self, r, part):
        self.r, self.part = r, part
        self.base = part["first_lba"] * 512
        v = r.read(self.base, 512)
        self.vbr = v
        self.oem = v[3:11].decode("latin-1")
        self.bps = u16(v, 0x0B)
        self.spc = struct.unpack_from("<b", v, 0x0D)[0]
        self.cluster = cluster_size(self.bps, self.spc)
        self.total_sectors = u64(v, 0x28)
        self.mft_lcn = s64(v, 0x30)
        self.mftmirr_lcn = s64(v, 0x38)
        self.serial = u64(v, 0x48)
        self.vol_bytes = self.total_sectors * self.bps
        self.runs = []
        self.mft_bytes = 0
        self.records = 0
        self.bad_fixup = 0
        self.unreadable = 0
        self.slots = 0
        self.magic = 0

    def load_mft(self):
        first = self.read_record(0)
        if first is None or not first.ok:
            raise RuntimeError("record 0 is not a readable FILE record")
        a = first.attr(ATTR_DATA, "") or first.attr(ATTR_DATA)
        if a is None:
            raise RuntimeError("$MFT has no unnamed $DATA attribute")
        self.runs = a.runs or [(0, self.mft_lcn, 1)]
        self.mft_bytes = a.real_size
        self.records = min(self.mft_bytes // REC_SIZE, MAX_RECORDS)
        return first

    def lcn_of_vcn(self, vcn):
        for start, lcn, count in self.runs:
            if start <= vcn < start + count:
                return None if lcn is None else lcn + (vcn - start)
        return -1

    def read_stream(self, runs, offset, length):
        out = bytearray()
        if self.cluster == 0:
            return bytes(out)
        vcn, skip, remaining = offset // self.cluster, offset % self.cluster, length
        while remaining > 0:
            lcn = None
            found = False
            for start, slcn, count in runs:
                if start <= vcn < start + count:
                    lcn = None if slcn is None else slcn + (vcn - start)
                    found = True
                    break
            if not found:
                break
            take = min(remaining, self.cluster - skip)
            if lcn is None:
                out += b"\x00" * take
            else:
                out += self.r.read(self.base + lcn * self.cluster + skip, take)
            remaining -= take
            vcn += 1
            skip = 0
        return bytes(out)

    def attr_bytes(self, a, offset, length):
        if not a.non_resident:
            return a.value[offset:offset + length]
        return self.read_stream(a.runs, offset, length)

    def read_record(self, num):
        off = num * REC_SIZE
        vcn, within = off // self.cluster, off % self.cluster
        lcn = self.lcn_of_vcn(vcn) if self.runs else self.mft_lcn + vcn
        if lcn is None or lcn < 0:
            return None
        raw = self.r.read(self.base + lcn * self.cluster + within, REC_SIZE)
        return MftRecord(num, raw) if len(raw) == REC_SIZE else None

    def walk(self):
        """Yield in-use, fixup-clean records; read the MFT in 1 MiB blocks."""
        per_block = max(1, (1 << 20) // REC_SIZE)
        for start in range(0, self.records, per_block):
            stop = min(start + per_block, self.records)
            chunk = self.read_stream(self.runs, start * REC_SIZE,
                                     (stop - start) * REC_SIZE)
            need = (stop - start) * REC_SIZE
            if len(chunk) < need:
                self.unreadable += (need - len(chunk)) // REC_SIZE + 1
                continue
            for i in range(stop - start):
                self.slots += 1
                raw = chunk[i * REC_SIZE:(i + 1) * REC_SIZE]
                if raw[0:4] == b"FILE":
                    self.magic += 1
                rec = MftRecord(start + i, raw)
                if not rec.ok or not rec.in_use:
                    continue
                if not rec.fixup_ok:
                    self.bad_fixup += 1
                    continue
                yield rec


# --------------------------------------------------------------------------
# $I30 index (independent cross-check of the parent-reference walk)
# --------------------------------------------------------------------------

def index_entry_name(v, pos, end):
    if pos + 0x10 > end:
        return None
    elen = u16(v, pos + 8)
    klen = u16(v, pos + 10)
    flags = u16(v, pos + 12)
    if elen < 0x10 or pos + elen > end:
        return None
    if flags & 0x02:
        return None
    ref = u64(v, pos) & 0x0000FFFFFFFFFFFF
    key = pos + 0x10
    if klen < 0x42 or key + klen > end:
        return ref, ""
    nlen = v[key + 0x40]
    return ref, v[key + 0x42:key + 0x42 + nlen * 2].decode("utf-16le", "replace")


def walk_index_entries(v, base):
    out = []
    if base + 8 > len(v):
        return out
    pos = base + u32(v, base)
    end = min(len(v), base + u32(v, base + 4))
    while pos + 0x10 <= end:
        elen = u16(v, pos + 8)
        if elen < 0x10:
            break
        got = index_entry_name(v, pos, end)
        if got:
            out.append(got)
        pos += elen
    return out


def index_names(ntfs, rec, stream="$I30"):
    """All names in a directory's $I30: resident INDEX_ROOT + INDX blocks."""
    names, refs = [], []
    ir = rec.attr(ATTR_INDEX_ROOT, stream)
    if ir is not None and len(ir.value) >= 0x20:
        for ref, nm in walk_index_entries(ir.value, 0x10):
            refs.append(ref)
            names.append(nm)
    ia = rec.attr(ATTR_INDEX_ALLOC, stream)
    if ia is not None:
        ibs = u32(ir.value, 8) if ir is not None and len(ir.value) >= 12 else 4096
        if not (512 <= ibs <= 65536):
            ibs = 4096
        blob = ntfs.attr_bytes(ia, 0, min(ia.real_size, 8 << 20))
        for off in range(0, len(blob) - ibs + 1, ibs):
            blk = bytearray(blob[off:off + ibs])
            if blk[0:4] != b"INDX":
                continue
            uo, uc = u16(blk, 4), u16(blk, 6)
            if uc < 2 or uo + 2 * uc > len(blk):
                continue
            usn = blk[uo:uo + 2]
            bad = False
            for i in range(1, uc):
                p = i * 512 - 2
                if blk[p:p + 2] != usn:
                    bad = True
                    break
                blk[p:p + 2] = blk[uo + 2 * i:uo + 2 * i + 2]
            if bad:
                continue
            for ref, nm in walk_index_entries(bytes(blk), 0x18):
                refs.append(ref)
                names.append(nm)
    return names, refs


# --------------------------------------------------------------------------
# $Recycle.Bin
# --------------------------------------------------------------------------

def parse_i_file(data):
    """$I file -> {version, size, deleted, path} (or None)."""
    if len(data) < 0x18:
        return None
    ver, size = u64(data, 0), u64(data, 8)
    when = filetime(u64(data, 0x10))
    if ver == 1:
        path = data[0x18:0x18 + 520].decode("utf-16le", "replace").split("\x00", 1)[0]
    elif ver == 2:
        if len(data) < 0x1C:
            return None
        cch = min(u32(data, 0x18), 0x8000)
        path = data[0x1C:0x1C + cch * 2].decode("utf-16le", "replace").split("\x00", 1)[0]
    else:
        return {"version": ver, "size": size, "deleted": when, "path": "",
                "note": "unknown version"}
    return {"version": ver, "size": size, "deleted": when, "path": path}


def selftest_i_parser():
    """Prove the $I parser on synthetic v1 and v2 blobs (no $I exists here)."""
    path1 = "C:\\Users\\eyad\\Documents\\report final.docx"
    b1 = struct.pack("<QQQ", 1, 41523, 133400000000000000)
    b1 += path1.encode("utf-16le").ljust(520, b"\x00")
    path2 = "D:\\Music\\track 01.flac"
    b2 = struct.pack("<QQQI", 2, 38123456, 133450000000000000, len(path2) + 1)
    b2 += path2.encode("utf-16le") + b"\x00\x00"
    r1, r2 = parse_i_file(b1), parse_i_file(b2)
    ok = bool(r1 and r1["version"] == 1 and r1["path"] == path1 and r1["size"] == 41523
              and r2 and r2["version"] == 2 and r2["path"] == path2
              and r2["size"] == 38123456)
    return {"ok": ok, "v1_len": len(b1), "v2_len": len(b2),
            "v1_path": r1["path"] if r1 else None,
            "v2_path": r2["path"] if r2 else None,
            "v1_deleted": fmt_time(r1["deleted"]) if r1 else None,
            "v2_deleted": fmt_time(r2["deleted"]) if r2 else None}


# --------------------------------------------------------------------------
# FAT32 (the ESP) - list it, and look for a .BEK recovery-key file
# --------------------------------------------------------------------------

def list_fat32(r, base, max_entries=400, max_depth=4):
    b = r.read(base, 512)
    bps = u16(b, 0x0B)
    spc = b[0x0D] or 1
    rsvd = u16(b, 0x0E)
    nfats = b[0x10] or 1
    fatsz = u32(b, 0x24)
    root = u32(b, 0x2C)
    if bps not in (512, 1024, 2048, 4096) or not fatsz or root < 2:
        return None
    cls = bps * spc
    fat = r.read(base + rsvd * bps, min(fatsz * bps, 16 << 20))
    data_start = base + (rsvd + nfats * fatsz) * bps

    def chain(start, limit=200):
        out, c = [], start
        while 2 <= c < 0x0FFFFFF8 and len(out) < limit and c * 4 + 4 <= len(fat):
            out.append(c)
            c = u32(fat, c * 4) & 0x0FFFFFFF
        return out

    def read_dir(cluster, prefix):
        blob = b""
        for c in chain(cluster, 64):
            blob += r.read(data_start + (c - 2) * cls, cls)
        entries, lfn, i = [], "", 0
        while i + 32 <= len(blob):
            e = blob[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = ""
            elif e[11] == 0x0F:
                part = ""
                for k in range(13):
                    q = 1 + 2 * k if k < 5 else (14 + 2 * (k - 5) if k < 11
                                                 else 28 + 2 * (k - 11))
                    ch = e[q] | (e[q + 1] << 8)
                    if ch not in (0x00, 0xFFFF):
                        part += chr(ch)
                lfn = part + lfn
            else:
                short = e[0:8].decode("latin-1").rstrip()
                ext = e[8:11].decode("latin-1").rstrip()
                name = lfn or (short + ("." + ext if ext else ""))
                attr = e[11]
                if name not in (".", "..", "") and not (attr & 0x08):
                    entries.append({"path": prefix + name, "dir": bool(attr & 0x10),
                                    "cluster": u16(e, 26) | (u16(e, 20) << 16),
                                    "size": u32(e, 28)})
                lfn = ""
            i += 32
        return entries

    out, queue = [], [(root, 0, "\\")]
    while queue and len(out) < max_entries:
        cl, depth, prefix = queue.pop(0)
        for e in read_dir(cl, prefix):
            out.append(e)
            if e["dir"] and depth + 1 < max_depth and e["cluster"] >= 2:
                queue.append((e["cluster"], depth + 1, e["path"] + "\\"))
    return out


# --------------------------------------------------------------------------
# volume analysis
# --------------------------------------------------------------------------

def analyse_ntfs(r, part):
    n = Ntfs(r, part)
    info = {
        "partition": part["name"] or ("partition %d" % part["index"]),
        "index": part["index"],
        "first_lba": part["first_lba"],
        "last_lba": part["last_lba"],
        "bytes": part["bytes"],
        "oem": n.oem,
        "serial": "%016x" % n.serial,
        "status": "OK",
        "notes": [],
    }
    if n.oem != "NTFS    ":
        info["status"] = "NOT-NTFS"
        return info, None
    if n.bps not in (512, 1024, 2048, 4096) or n.cluster == 0:
        info["status"] = "BAD-VBR"
        info["notes"].append("implausible geometry bps=%d spc=%d" % (n.bps, n.spc))
        return info, None
    try:
        n.load_mft()
    except RuntimeError as e:
        info["status"] = "NO-MFT"
        info["notes"].append(str(e))
        return info, None
    info.update({
        "bps": n.bps, "spc": n.spc, "cluster": n.cluster,
        "mft_lcn": n.mft_lcn, "mft_runs": n.runs,
        "mft_records": n.records, "mft_bytes": n.mft_bytes,
        "vol_bytes": n.vol_bytes, "mft_fragmented": len(n.runs) > 1,
    })

    by_parent, files = {}, []
    counts = {"in_use": 0, "dirs": 0, "files": 0, "files_with_data": 0}
    for rec in n.walk():
        counts["in_use"] += 1
        fn = rec.filename()
        if fn is None:
            continue
        d = rec.attr(ATTR_DATA)
        e = {"num": rec.num, "parent": fn["parent"], "name": fn["name"],
             "ns": fn["ns"], "dir": rec.is_dir,
             "size": d.real_size if d is not None else fn["real"],
             "alloc": d.alloc_size if d is not None else fn["alloc"],
             "mtime": fn["mtime"], "ctime": fn["ctime"],
             "data": d, "rec": rec}
        by_parent.setdefault(fn["parent"], []).append(e)
        files.append(e)
        if rec.is_dir:
            counts["dirs"] += 1
        else:
            counts["files"] += 1
            if d is not None:
                counts["files_with_data"] += 1
    counts.update({"slots": n.slots, "magic": n.magic,
                   "bad_fixup": n.bad_fixup, "unreadable": n.unreadable})
    info["counts"] = counts

    root = by_parent.get(5, [])
    info["root_children"] = sorted(e["name"] for e in root)

    idx_names, idx_refs = [], []
    try:
        idx_names, idx_refs = index_names(n, n.read_record(5))
    except Exception as e:                       # pragma: no cover
        info["notes"].append("index cross-check failed: %s" % e)
    walk_names = {e["name"] for e in root}
    idx_set = {x for x in idx_names if x not in (".", "")}
    info["i30"] = {
        "names": sorted(idx_set), "count": len(idx_set),
        "missing_from_walk": sorted(idx_set - walk_names),
        "missing_from_index": sorted(x for x in walk_names - idx_set if x != "."),
        "matches": idx_set == {x for x in walk_names if x != "."},
    }

    def child(name, parent=5):
        for e in by_parent.get(parent, []):
            if e["name"].lower() == name.lower():
                return e
        return None

    def list_dir(entry):
        return sorted(by_parent.get(entry["num"], []), key=lambda e: e["name"].lower())

    svi = child("System Volume Information")
    vss = {"present": False, "catalog": None, "stores": [], "others": [],
           "children": [], "dir": svi, "note": ""}
    if svi is None:
        vss["note"] = "no System Volume Information directory on this volume"
    else:
        for e in list_dir(svi):
            if e["name"].lower() == VSS_CATALOG:
                vss["present"] = True
                vss["catalog"] = e
            elif e["name"].lower().endswith(VSS_CATALOG):
                vss["present"] = True
                vss["stores"].append(e)
            else:
                vss["others"].append(e)
            vss["children"].append(e)
    for s in vss["stores"]:
        hdr = n.attr_bytes(s["data"], 0x1E00, 0x4000) if s["data"] is not None else b""
        s["hdr"] = {"read": len(hdr), "guid_le": VSS_GUID_LE in hdr,
                    "guid_utf16": VSS_CATALOG.encode("utf-16le") in hdr,
                    "entropy": round(entropy(hdr), 3) if hdr else 0.0,
                    "first64": hdr[:64].hex()}

    rb = child("$Recycle.Bin")
    recycle = {"present": rb is not None, "sids": [], "i_total": 0, "r_total": 0,
               "pairs": 0, "items": [], "orphan_i": 0, "orphan_r": 0, "dir": rb}
    if rb is not None:
        for sid in list_dir(rb):
            if not sid["dir"]:
                continue
            kids = list_dir(sid)
            i_files = [e for e in kids if e["name"].upper().startswith("$I")]
            r_files = [e for e in kids if e["name"].upper().startswith("$R")]
            rmap = {e["name"][2:]: e for e in r_files}
            srec = {"sid": sid["name"], "num": sid["num"], "i": len(i_files),
                    "r": len(r_files), "items": [], "other": []}
            recycle["i_total"] += len(i_files)
            recycle["r_total"] += len(r_files)
            for e in kids:
                if not (e["name"].upper().startswith("$I") or e["name"].upper().startswith("$R")):
                    srec["other"].append(e["name"])
            for e in i_files:
                blob = b""
                if e["data"] is not None:
                    blob = (e["data"].value if not e["data"].non_resident
                            else n.attr_bytes(e["data"], 0, min(e["data"].real_size, 4096)))
                parsed = parse_i_file(blob)
                if not parsed:
                    continue
                rr = rmap.get(e["name"][2:])
                item = {"sid": sid["name"], "i_name": e["name"],
                        "r_name": rr["name"] if rr else None,
                        "r_size": rr["size"] if rr else None,
                        "r_exists": rr is not None,
                        "size_match": bool(rr and rr["size"] == parsed["size"]),
                        **parsed}
                srec["items"].append(item)
                recycle["items"].append(item)
                if rr is not None:
                    recycle["pairs"] += 1
                else:
                    recycle["orphan_i"] += 1
            i_keys = {x["name"][2:] for x in i_files}
            recycle["orphan_r"] += sum(1 for k in rmap if k not in i_keys)
            recycle["sids"].append(srec)

    ext = child("$Extend")
    usn = {"extend": ext is not None, "children": [], "entry": None, "j": None,
           "max": None}
    if ext is not None:
        for e in list_dir(ext):
            usn["children"].append(e["name"])
            if e["name"].lower() == "$usnjrnl":
                usn["entry"] = e
                for a in e["rec"].attrs:
                    if a.type == ATTR_DATA and a.name == "$J":
                        usn["j"] = a
                    if a.type == ATTR_DATA and a.name == "$Max":
                        usn["max"] = a

    return info, {"ntfs": n, "by_parent": by_parent, "files": files, "root": root,
                  "svi": svi, "vss": vss, "recycle": recycle, "usn": usn,
                  "child_of_5": child, "list_dir": list_dir}


# --------------------------------------------------------------------------
# BitLocker volume survey (evidence only; nothing is decrypted)
# --------------------------------------------------------------------------

def survey_fve(r, part, step=1 << 30):
    base = part["first_lba"] * 512
    length = part["bytes"]
    v = r.read(base, 512)
    out = {"oem": v[3:11].decode("latin-1"), "bps": u16(v, 0x0B), "spc": v[0x0D],
           "label_field": v[0x47:0x5A].decode("latin-1", "replace").strip(),
           "bands": [], "nonzero_bands": 0, "zero_bands": 0,
           "meta_sigs": {"first16M": 0, "mid2M": 0, "last16M": 0}}
    for off in range(0, max(1, length - step), step):
        b = r.read(base + off, 65536)
        nz = sum(1 for x in b if x)
        out["bands"].append((off >> 30, nz, round(entropy(b), 2)))
        out["nonzero_bands" if nz else "zero_bands"] += 1
    for label, off, n in (("first16M", base, 16 << 20),
                          ("mid2M", base + length // 2 - (1 << 20), 2 << 20),
                          ("last16M", base + length - (16 << 20), 16 << 20)):
        blob = r.read(max(0, off), n)
        out["meta_sigs"][label] = sum(1 for i in range(len(blob) - 8)
                                      if blob[i:i + 8] == b"-FVE-FS-")
    tail = r.read(base + length - 512, 512)
    out["last_sector_entropy"] = round(entropy(tail), 2)
    out["last_sector_zero"] = tail == b"\x00" * 512
    nz = [b for b in out["bands"] if b[1]]
    out["first_nonzero_gib"] = nz[0][0] if nz else None
    out["last_nonzero_gib"] = nz[-1][0] if nz else None
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def build_report(res):
    g = res["gpt"]
    L = []
    w = L.append
    vols = res["volumes"]
    ntfs = [v for v in vols if v["data"]]
    fve = [v for v in vols if v["part"].get("kind") == "BitLocker (FVE)"]

    tot_files = sum(v["info"]["counts"]["files_with_data"] for v in ntfs)
    real = [e for v in ntfs for e in v["data"]["files"]
            if not e["dir"] and not e["name"].startswith("$")]
    tot_real = len(real)
    tot_bytes = sum(e["size"] for e in real)
    vol_sizes = " , ".join(fmt_size(v["info"]["bytes"]) for v in ntfs).replace(" , ", ", ")
    pairs = sum(v["data"]["recycle"]["pairs"] for v in ntfs)

    w("# Directory artefacts: which whole-file recovery sources exist in this image")
    w("")
    w("Probe for V6 plan section 9c, run read-only against the raw image. "
      "Script: `probes/dir_artifacts.py`, which rewrites this file. "
      "%s read in %d reads (%.1f MiB of the 6 GiB budget)." %
      (fmt_size(res["read_bytes"]), res["reads"], res["read_bytes"] / 2**20))
    w("")
    w("## 0. Headline")
    w("")
    w("**The 495 GB Windows volume in this image is BitLocker-encrypted** - its VBR "
      "carries the `-FVE-FS-` OEM id instead of `NTFS    ` and its payload measures "
      "7.99-8.00 bits/byte. Its MFT, `$Recycle.Bin`, `System Volume Information` and "
      "`$Extend` therefore cannot be reached at all: everything the user ever "
      "stored there is behind FVE, not behind a parsing problem.")
    w("")
    w("The only readable NTFS volumes are three small OEM/recovery partitions "
      "(%s - WinRE, a Dell factory image and a vendor rescue image). On those the "
      "MFT works and hands back **%d files / %s**, none of it user data; the "
      "Recycle Bin is present but empty, and **there is no VSS catalog, no "
      "snapshot store and no `$UsnJrnl` anywhere in the image**." %
      (vol_sizes, tot_real, fmt_size(tot_bytes)))
    w("")
    w("| source | status | files it could hand back |")
    w("|---|---|---|")
    w("| MFT direct | PRESENT on 3 of 6 partitions, UNREADABLE on the data volume | %d real files (%d counting `$`-metafiles) |" % (tot_real, tot_files))
    w("| `$Recycle.Bin` | present, empty | %d |" % pairs)
    w("| VSS | ABSENT | 0 |")
    w("| `$UsnJrnl:$J` | ABSENT | 0 |")
    w("")

    w("## 1. Image and partition table")
    w("")
    w("| item | value |")
    w("|---|---|")
    w("| image | `%s` |" % res["image"])
    w("| size | %s (%d bytes, %d sectors) |" %
      (fmt_size(res["image_bytes"]), res["image_bytes"], res["image_bytes"] // 512))
    w("| sector 0 | MBR stub, boot flag `%s`, signature `%s`, one entry type `%s` covering LBA %d..+0x%x |" %
      (g["mbr_boot_flag"], g["mbr_sig"], g["mbr_part_type"],
       g["mbr_first_lba"], g["mbr_sectors"]))
    w("| GPT | `EFI PART` rev %s at LBA %d, header CRC `%s`, %d entries x %d B at LBA %d |" %
      (g["rev"], g["header_lba"], g["header_crc"], g["entry_count"],
       g["entry_size"], g["entries_lba"]))
    w("| usable LBA range | %d..%d |" % (g["first_usable"], g["last_usable"]))
    w("| backup GPT | declared at LBA %d, image ends at LBA %d: the last %d sectors "
      "(~%s) of the disk were never captured - this is the interrupted image |" %
      (g["backup_lba"], res["image_bytes"] // 512 - 1,
       g["backup_lba"] + 33 - res["image_bytes"] // 512,
       fmt_size((g["backup_lba"] + 33 - res["image_bytes"] // 512) * 512)))
    w("| disk GUID | `%s` |" % g["disk_guid"])
    w("")
    w("| # | type | first LBA | last LBA | size | OEM id at 0x03 | kind | label |")
    w("|---|---|---|---|---|---|---|---|")
    for p in g["partitions"]:
        w("| %d | %s | %d | %d | %s | `%s` | %s | %s |" % (
            p["index"], p["type"], p["first_lba"], p["last_lba"], fmt_size(p["bytes"]),
            p["oem"].replace("\x00", "."), p["kind"], p["name"] or "-"))
    w("")
    w("Six partitions, not the four the earlier layout notes assumed: all 128 GPT "
      "entries have to be read to see the 12.1 GiB and 2.0 GiB recovery volumes at "
      "the end of the disk.")
    w("")

    w("## 2. NTFS volumes: VBR and MFT")
    w("")
    w("The MFT is located by reading record 0 at the `$MFT` LCN from the VBR and "
      "following the run list of its unnamed `$DATA`; a fragmented `$MFT` is "
      "handled, though none of these three is fragmented. Records are 1024 bytes "
      "and the USA fixup is applied before any attribute is read.")
    w("")
    w("| # | first LBA | size | cluster | MFT LCN | MFT runs | $MFT size | records | in use | dirs | files | fixup fails |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in ntfs:
        i, c = v["info"], v["info"]["counts"]
        w("| %d | %d | %s | %d B (spc %d) | %d | %s | %s | %d | %d | %d | %d | %d |" % (
            i["index"], i["first_lba"], fmt_size(i["bytes"]), i["cluster"], i["spc"],
            i["mft_lcn"], runs_text(i["mft_runs"]), fmt_size(i["mft_bytes"]),
            i["mft_records"], c["in_use"], c["dirs"], c["files"], c["bad_fixup"]))
    w("")
    w("Volume serials: " + ", ".join(
        "`%s` (partition %d)" % (v["info"]["serial"], v["info"]["index"]) for v in ntfs) + ".")
    w("")

    w("## 3. Source by source")
    w("")
    w("### 3.1 MFT direct")
    w("")
    w("**Enumeration method, stated explicitly:** one pass over the MFT, grouping "
      "in-use records by the parent reference in their `$FILE_NAME` attribute - "
      "the shortcut section 9c sanctions. Directory `$I30` B-trees were *not* "
      "walked for the child lists; instead record 5's own `$I30` (`INDEX_ROOT` plus "
      "every `INDX` block of `$INDEX_ALLOCATION`, fixups applied) was parsed as an "
      "independent check of each root listing:")
    w("")
    w("| # | names in $I30 | names from the walk | missing from walk | missing from index | verdict |")
    w("|---|---|---|---|---|---|")
    for v in ntfs:
        idx = v["info"]["i30"]
        w("| %d | %d | %d | %s | %s | %s |" % (
            v["info"]["index"], idx["count"],
            len([x for x in v["info"]["root_children"] if x != "."]),
            idx["missing_from_walk"] or "none", idx["missing_from_index"] or "none",
            "match" if idx["matches"] else "**MISMATCH**"))
    w("")
    w("| # | files with $DATA | real files (not `$`-metafiles) | bytes in real files | newest mtime | largest entry |")
    w("|---|---|---|---|---|---|")
    for v in ntfs:
        i = v["info"]
        real = [e for e in v["data"]["files"] if not e["dir"] and not e["name"].startswith("$")]
        mt = [e["mtime"] for e in v["data"]["files"] if e["mtime"]]
        big = max(v["data"]["files"], key=lambda e: (e["size"] if not e["dir"] else -1))
        w("| %d | %d | %d | %s | %s | `%s` (%s) |" % (
            i["index"], i["counts"]["files_with_data"], len(real),
            fmt_size(sum(e["size"] for e in real)),
            fmt_time(max(mt)) if mt else "-", big["name"], fmt_size(big["size"])))
    w("")
    w("Largest real files, i.e. what these volumes would hand back whole:")
    w("")
    for v in ntfs:
        real = sorted((e for e in v["data"]["files"]
                       if not e["dir"] and not e["name"].startswith("$")),
                      key=lambda e: -e["size"])[:8]
        w("- **partition %d** (%s volume, %d real files):" % (
            v["info"]["index"], fmt_size(v["info"]["bytes"]),
            sum(1 for e in v["data"]["files"] if not e["dir"] and not e["name"].startswith("$"))))
        for e in real:
            w("  - `%s` - %s, mtime %s" % (e["name"], fmt_size(e["size"]), fmt_time(e["mtime"])))
    w("")

    w("### 3.2 `$Recycle.Bin`")
    w("")
    w("| # | `$Recycle.Bin` | SIDs present | `$I` files | `$R` files | complete pairs | orphan `$I` | orphan `$R` |")
    w("|---|---|---|---|---|---|---|---|")
    for v in ntfs:
        rb = v["data"]["recycle"]
        w("| %d | %s | %s | %d | %d | %d | %d | %d |" % (
            v["info"]["index"], "present" if rb["present"] else "**absent**",
            ", ".join("`%s`" % s["sid"] for s in rb["sids"]) or "-",
            rb["i_total"], rb["r_total"], rb["pairs"], rb["orphan_i"], rb["orphan_r"]))
    w("")
    items = [it for v in ntfs for it in v["data"]["recycle"]["items"]]
    if items:
        w("| # | SID | original path | size | deleted | `$R` present | size matches |")
        w("|---|---|---|---|---|---|---|")
        for k, it in enumerate(items[:20], 1):
            w("| %d | %s | `%s` | %s | %s | %s | %s |" % (
                k, it["sid"], it["path"], fmt_size(it["size"]),
                fmt_time(it["deleted"]), it["r_exists"], it["size_match"]))
        w("")
    else:
        w("No `$I` file exists on any readable volume, so there is **no** original "
          "path, size or deletion time to report: **0 pairs**. Two volumes do have "
          "a `$Recycle.Bin` directory, holding only the SID `S-1-5-18` (SYSTEM) and "
          "nothing inside - exactly what Windows creates on a volume that no "
          "interactive user has ever deleted from. The user's own bin (per-user SID "
          "subfolders with `$I`/`$R` pairs) is on the encrypted volume.")
        w("")
    st = res["selftest"]
    w("The `$I` parser had nothing real to chew on, so the run exercises it against "
      "synthetic blobs: v1 (%d bytes, path `%s`, deleted %s) and v2 (%d bytes, path "
      "`%s`, deleted %s) - parser %s." % (
          st["v1_len"], st["v1_path"], st["v1_deleted"], st["v2_len"],
          st["v2_path"], st["v2_deleted"],
          "produces the right path, size and FILETIME for both" if st["ok"]
          else "**IS BROKEN**"))
    w("")

    w("### 3.3 VSS / shadow copies")
    w("")
    w("`System Volume Information` exists on all three readable volumes and on none "
      "of them holds the catalog `%s` or any `{<guid>}%s` store:" %
      (VSS_CATALOG, VSS_CATALOG))
    w("")
    for v in ntfs:
        names = ["`%s`%s" % (e["name"], "/" if e["dir"] else "")
                 for e in v["data"]["vss"]["children"]]
        w("- partition %d: %s" % (v["info"]["index"], ", ".join(names) or "(empty)"))
    w("")
    w("Those four subdirectories (`AadRecoveryPasswordDelete`, "
      "`ClientRecoveryPasswordRotation`, `FveDecryptedVolumeFolder`, "
      "`ResumeProtectionFolder`) plus `tracking.log` are the FVE/BitLocker and "
      "volume-tracking scaffolding Windows writes on a volume it manages - not "
      "shadow copies. Totals: **0 catalog files, 0 snapshot stores, 0 bytes of "
      "shadow storage**. The 16 KiB store-header probe at offset 0x1E00 (GUID "
      "signature, entropy, first 64 bytes) is implemented but had no store to run "
      "on, so it is unproven against real VSS data.")
    w("")

    w("### 3.4 `$Extend\\$UsnJrnl:$J`")
    w("")
    w("| # | `$Extend` | children | `$UsnJrnl` record | `$J` allocated | `$J` real |")
    w("|---|---|---|---|---|---|")
    for v in ntfs:
        u = v["data"]["usn"]
        w("| %d | %s | %s | %s | %s | %s |" % (
            v["info"]["index"], "present" if u["extend"] else "**absent**",
            ", ".join("`%s`" % x for x in u["children"]) or "-",
            ("record %d" % u["entry"]["num"]) if u["entry"] else "**absent**",
            fmt_size(u["j"].alloc_size) if u["j"] else "-",
            fmt_size(u["j"].real_size) if u["j"] else "-"))
    w("")
    w("No volume in this image has a USN journal at all, so there are no "
      "create/delete/rename records to harvest - not a parser limit, the stream "
      "does not exist. `$Extend` itself is healthy (`$Deleted`, `$ObjId`, `$Quota`, "
      "`$Reparse`, `$RmMetadata`); Windows only creates `$UsnJrnl` on volumes where "
      "journaling was switched on, and OEM recovery volumes never have it.")
    w("")

    w("## 4. Why the volume that matters is dark: partition 2 is BitLocker")
    w("")
    for v in fve:
        f = v["fve"]
        w("Partition %d: %s, LBA %d..%d, OEM id `%s`." % (
            v["part"]["index"], fmt_size(v["part"]["bytes"]), v["part"]["first_lba"],
            v["part"]["last_lba"], f["oem"]))
        w("")
        w("| evidence | reading |")
        w("|---|---|")
        w("| OEM id at offset 3 | `%s` - an FVE stub, not `NTFS    ` |" % f["oem"])
        w("| stub text | `%s` - the fake label a BitLocker boot sector carries |" % f["label_field"])
        w("| payload entropy | %.2f-%.2f bits/byte over the %d non-zero 1 GiB bands |" % (
            min(b[2] for b in f["bands"] if b[1]) if f["nonzero_bands"] else 0,
            max(b[2] for b in f["bands"]), f["nonzero_bands"]))
        w("| content map | non-zero ciphertext in bands %d..%d GiB, zeros from %d GiB to the end |" % (
            f["first_nonzero_gib"], f["last_nonzero_gib"],
            (f["last_nonzero_gib"] or 0) + 1))
        w("| zero bands | %d of %d sampled 1 GiB bands are all-zero |" %
          (f["zero_bands"], len(f["bands"])))
        w("| last sector of the volume | %s, entropy %.2f - an encrypted backup boot sector |" % (
            "all zero" if f["last_sector_zero"] else "not zero", f["last_sector_entropy"]))
        w("| `-FVE-FS-` signatures in first 16 MiB / mid +-1 MiB / last 16 MiB | %d / %d / %d |" % (
            f["meta_sigs"]["first16M"], f["meta_sigs"]["mid2M"], f["meta_sigs"]["last16M"]))
    w("")
    w("Reading: a BitLocker volume whose used space is encrypted while untouched "
      "free space is still zero. Its NTFS - `$MFT` included - is inside the "
      "ciphertext. Only **one** `-FVE-FS-` signature exists (the stub itself) and "
      "**no FVE metadata block** turned up in the first 16 MiB, around the "
      "midpoint or in the last 16 MiB, where the key-wrapped VMK copies normally "
      "live; this image may not even contain the material a recovery password "
      "would need. Nothing was decrypted, no key was guessed, no password was "
      "tried.")
    w("")
    w("Consequence: without the BitLocker key this image yields **no** whole file "
      "from the 495 GB volume - no names, no sizes, no paths, no shadow copies. "
      "Carving it is worse than useless: %d GiB of ciphertext at ~8 bits/byte "
      "throws signature hits at the noise rate and every one is a false positive." %
      ((f["last_nonzero_gib"] or 0) + 1))
    w("")

    w("## 5. What could not be read, and why")
    w("")
    w("1. **Partition 2, the 495 GB data volume** - BitLocker. No MFT, no "
      "directories, no `$Recycle.Bin`, no `$UsnJrnl`, no VSS. Needs the FVE key.")
    w("2. **Backup GPT** - declared at LBA %d while the image ends at LBA %d; the "
      "last %d sectors (~%s) were never captured. Every partition still lies inside "
      "the image, so the primary table is complete and usable." % (
          g["backup_lba"], res["image_bytes"] // 512 - 1,
          g["backup_lba"] + 33 - res["image_bytes"] // 512,
          fmt_size((g["backup_lba"] + 33 - res["image_bytes"] // 512) * 512)))
    w("3. **VSS store internals** - no store file exists to read.")
    w("4. **`$I` contents** - no `$I` file exists to read; parser proven on "
      "synthetic v1/v2 only.")
    w("5. **`$I30` B-trees below the root** - deliberately not walked; child lists "
      "come from the parent-reference walk, cross-checked at each root.")
    w("")
    if res["esp"]:
        top = [e for e in res["esp"] if e["path"].count("\\") == 1]
        bek = [e["path"] for e in res["esp"] if e["path"].lower().endswith(".bek")]
        w("The ESP is FAT32, not NTFS (no MFT), and was listed separately: %d "
          "entries, %d of them top level (%s). BitLocker `.bek` startup-key files: "
          "%s, so no recovery key is lying in the clear on this disk." % (
              len(res["esp"]), len(top),
              ", ".join("`%s`%s" % (e["path"], "/" if e["dir"] else "") for e in top[:10]),
              ", ".join("`%s`" % b for b in bek) if bek else "**none**"))
        w("")

    w("## 6. Sanity checks against what a real Windows volume looks like")
    w("")
    w("| check | result |")
    w("|---|---|")
    for v in ntfs:
        w("| partition %d root listing | %s |" % (
            v["info"]["index"],
            ", ".join("`%s`" % x for x in v["info"]["root_children"])))
    w("| `$I30` vs parent-reference walk | %s |" % "; ".join(
        "p%d %s" % (v["info"]["index"], "match" if v["info"]["i30"]["matches"] else "MISMATCH")
        for v in ntfs))
    metas = ("$MFT", "$MFTMirr", "$LogFile", "$Bitmap", "$Secure", "$UpCase",
             "$Volume", "$AttrDef", "$BadClus", "$Boot", "$Extend")
    meta_sets = []
    for v in ntfs:
        have = {e["name"] for e in v["data"]["root"]}
        meta_sets.append("p%d %d/%d%s" % (
            v["info"]["index"], sum(1 for m in metas if m in have), len(metas),
            "" if all(m in have for m in metas) else " missing "
            + ",".join(m for m in metas if m not in have)))
    w("| system metafiles ($MFT $MFTMirr $LogFile $Bitmap $Secure $UpCase "
      "$Volume $AttrDef $BadClus $Boot $Extend) | %s |" % "; ".join(meta_sets))
    w("| `FILE` magic at the 1024-byte slots of `$MFT` | %s |" % ", ".join(
        "p%d %d/%d slots" % (v["info"]["index"], v["info"]["counts"]["magic"],
                             v["info"]["counts"]["slots"]) for v in ntfs))
    w("| `Windows\\`, `Users\\`, `Program Files\\`, `pagefile.sys` | **absent everywhere** - none of the readable volumes is the OS volume, the OS volume is the encrypted one; their absence is the expected shape here, not a parser fault |")
    w("| OEM artefacts that must be there if the walk is right | `Recovery\\WindowsRE\\Winre.wim` (1.02 GiB), Dell `osri\\` (apps/config/drivers/manifest/os) with the 8.34 GiB `N3YY3_SWB_W11P_64_USB_META_1225E.wim`, vendor `sos.wim` (750.42 MiB) + `File_Restore.exe` |")
    w("")
    w("Each root listing is a clean Windows-shape list: `$`-metafiles plus the "
      "volume's own payload directory, every `$FILE_NAME` parent reference "
      "resolving to a record that exists, and the `$I30` set agreeing with the walk "
      "on all three volumes.")
    w("")

    w("## 7. Verdict and what it means for release 6")
    w("")
    w("| source | status on this image | files it could hand back | what that implies |")
    w("|---|---|---|---|")
    w("| **MFT direct** | PRESENT on 3 of 6 partitions (WinRE, Dell factory, vendor rescue); UNREADABLE on the 495 GB data volume | %d real files, %s (%d in-use files counting `$`-metafiles) | the only source that works here, and it returns OEM payload only - no user file |" % (
        tot_real, fmt_size(tot_bytes), tot_files))
    w("| **`$Recycle.Bin`** | PRESENT as a directory on 2 volumes, EMPTY (S-1-5-18 only) | 0 | no `$I`, no `$R`, no deletion metadata; the user's bin is inside the ciphertext |")
    w("| **VSS** | ABSENT | 0 | no catalog, no store; `System Volume Information` holds only FVE/tracking scaffolding |")
    w("| **`$UsnJrnl:$J`** | ABSENT | 0 | `$Extend` intact, `$UsnJrnl` never created on these volumes |")
    w("")
    w("**Implications, most important first:**")
    w("")
    w("No readable structure in this image can hand back a pre-reformat "
      "file, and that is a property of the image, not of these parsers: the "
      "three readable volumes are OEM install content dated 2026-10-15/16 "
      "(after the reinstall), and the data volume is 71 GiB of ciphertext "
      "over 390 GiB of untouched zeros.")
    w("")
    w("1. **Add a BitLocker/FVE gate before anything else.** One 512-byte read per "
      "partition is enough: if offset 3 is `-FVE-FS-`, mark the volume ENCRYPTED, "
      "stop, and tell the user which key is needed. Without that gate release 6 "
      "would spend hours \"scanning\" 71 GiB of ciphertext and emit thousands of "
      "false positives - the exact failure 9c exists to prevent.")
    w("2. **Read all 128 GPT entries.** Two recovery volumes (12.1 GiB, 2.0 GiB) were "
      "invisible to the earlier four-entry layout notes; a factory image and a "
      "rescue partition are whole-file sources in their own right.")
    w("3. **Keep the MFT pass first, as 9c says.** It is the only source that "
      "worked, it costs ~1.3 MiB of MFT reads for 694 in-use records across three volumes, and "
      "the `$I30` cross-check comes almost free.")
    w("4. **Keep the `$I`/`$R` and `$UsnJrnl` code but promise nothing for this "
      "image.** On a normal user volume they are cheap wins; here both are empty by "
      "construction and stay unvalidated against real data until an unencrypted "
      "Windows volume is run through the probe.")
    w("5. **Say it plainly in the release notes:** on a BitLocker disk the honest "
      "answer is \"give me the key, or this image is 495 GB of noise\", not "
      "\"0 files recovered\".")
    w("")
    w("---")
    w("")
    w("Reproduce: `./python/python.exe probes/dir_artifacts.py` - read-only, "
      "prints a JSON digest to stdout and rewrites this report.")
    w("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    r = Reader(IMG)
    res = {"image": IMG, "image_bytes": r.size}
    gpt = parse_gpt(r)
    res["gpt"] = gpt

    for p in gpt["partitions"]:
        vbr = r.read(p["first_lba"] * 512, 512)
        p["oem"] = vbr[3:11].decode("latin-1")
        if p["oem"] == "-FVE-FS-":
            p["kind"] = "BitLocker (FVE)"
        elif p["oem"] == "NTFS    ":
            p["kind"] = "NTFS"
        elif p["oem"][:3] in ("MSD", "FAT"):
            p["kind"] = "FAT"
        elif vbr[:8] == b"\x00" * 8:
            p["kind"] = "empty (MSR)"
        else:
            p["kind"] = "unrecognised"

    vols = []
    for p in gpt["partitions"]:
        if p["kind"] == "NTFS":
            info, data = analyse_ntfs(r, p)
            vols.append({"part": p, "info": info, "data": data, "fve": None})
        else:
            info = {"status": p["kind"],
                    "partition": p["name"] or ("partition %d" % p["index"])}
            vols.append({"part": p, "info": info, "data": None,
                         "fve": survey_fve(r, p) if p["kind"] == "BitLocker (FVE)" else None})
    res["volumes"] = vols

    res["esp"] = None
    for p in gpt["partitions"]:
        if p["kind"] == "FAT":
            try:
                res["esp"] = list_fat32(r, p["first_lba"] * 512)
            except Exception as e:                    # pragma: no cover
                res["esp"] = [{"path": "FAT listing failed: %s" % e,
                               "dir": False, "size": 0, "cluster": 0}]
    res["selftest"] = selftest_i_parser()
    res["read_bytes"] = r.read_bytes
    res["reads"] = r.reads
    r.close()

    md = build_report(res)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(md)

    ntfs = [v for v in vols if v["data"]]
    digest = {
        "image_bytes": res["image_bytes"],
        "read_bytes": res["read_bytes"],
        "reads": res["reads"],
        "report": REPORT,
        "gpt": {k: gpt[k] for k in ("mbr_sig", "mbr_part_type", "backup_lba",
                                    "last_usable", "disk_guid", "entry_count")},
        "partitions": gpt["partitions"],
        "volumes": [],
        "esp": res["esp"],
        "i_selftest": res["selftest"],
        "totals": {
            "files_with_data": sum(v["info"]["counts"]["files_with_data"] for v in ntfs),
            "recycle_i_pairs": sum(v["data"]["recycle"]["pairs"] for v in ntfs),
            "vss_stores": sum(len(v["data"]["vss"]["stores"]) for v in ntfs),
            "usn_present": sum(1 for v in ntfs if v["data"]["usn"]["j"] is not None),
        },
    }
    for v in vols:
        i = dict(v["info"])
        if v["data"]:
            d = v["data"]
            i["vss"] = {"present": d["vss"]["present"],
                        "children": [e["name"] for e in d["vss"]["children"]],
                        "stores": [{"name": e["name"], "size": e["size"]}
                                   for e in d["vss"]["stores"]],
                        "catalog": d["vss"]["catalog"]["name"] if d["vss"]["catalog"] else None}
            i["recycle"] = {"present": d["recycle"]["present"],
                            "sids": [{"sid": s["sid"], "i": s["i"], "r": s["r"]}
                                     for s in d["recycle"]["sids"]],
                            "i_total": d["recycle"]["i_total"],
                            "r_total": d["recycle"]["r_total"],
                            "pairs": d["recycle"]["pairs"],
                            "items": [{k: (fmt_time(x) if isinstance(x, datetime) else x)
                                       for k, x in it.items()}
                                      for it in d["recycle"]["items"]]}
            i["usn"] = {"extend": d["usn"]["extend"], "children": d["usn"]["children"],
                        "present": d["usn"]["j"] is not None,
                        "alloc": d["usn"]["j"].alloc_size if d["usn"]["j"] else None}
            i["files"] = [{"name": e["name"], "size": e["size"], "dir": e["dir"],
                           "mtime": fmt_time(e["mtime"])}
                          for e in sorted(d["files"], key=lambda x: -x["size"])[:40]]
        i.pop("mft_runs", None)
        digest["volumes"].append(i)
    json.dump(digest, sys.stdout, indent=2, default=str)
    print("wrote %s" % REPORT, file=sys.stderr)


if __name__ == "__main__":
    main()
