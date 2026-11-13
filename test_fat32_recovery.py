#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_fat32_recovery.py - Phase 2 (FAT32 metadata parser) tests.

Builds a small synthetic FAT32 image in a temp file (boot sector + two FAT
copies + a root directory holding live and deleted 8.3 entries, an Arabic
long-name entry, a fragmented chain, a truncated chain, a looping chain, a
zero-cluster entry and an entry whose size lies) and checks that
fat32_recovery.py reads the deleted entries back with the exact names, sizes
and bytes, and leaves live entries alone.

No real disk is touched: the image is a regular file under tempfile.mkdtemp.
"""

import os
import shutil
import sys
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fat32_recovery as fr

FAILS = []

# geometry: bps=512, spc=2, reserved=32, 2 FAT copies of 1 sector each,
# 94 data clusters (clusters 2..95) -> 222 sectors -> 113,664 bytes total
BPS = 512
SPC = 2
RESERVED = 32
FAT_COUNT = 2
SPF = 1
CLUSTERS = 94
TOTAL_SECTORS = RESERVED + FAT_COUNT * SPF + CLUSTERS * SPC
FIRST_DATA_SECTOR = RESERVED + FAT_COUNT * SPF
CLUSTER_BYTES = BPS * SPC

# DOS date 0x58B1 = 2024-05-17, DOS time 0x52D2 = 10:22:36
MOD_DATE = 0x58B1
MOD_TIME = 0x52D2
MOD_TEXT = "2024-05-17 10:22:36"

ARABIC_LONG = "تقرير مبيعات.txt"
LONGNAME = "Long Name Report.TXT"
DEL_CONTENT = b"Hello, deleted FAT world!\n"          # 26 bytes
ARABIC_CONTENT = "مرحبا بالعالم — sales report\n".encode("utf-8")
FRAG_A = bytes(i & 0xFF for i in range(CLUSTER_BYTES))
FRAG_B = bytes((i + 1) & 0xFF for i in range(CLUSTER_BYTES))
FRAG_SIZE = 2000
OVERSIZE_CONTENT = bytes((i * 7) & 0xFF for i in range(CLUSTER_BYTES))


def ck(cond, msg):
    if cond:
        print("ok:", msg)
    else:
        print("FAIL:", msg)
        FAILS.append(msg)


# ---------------------------------------------------------------------------
# entry builders (each returns exactly 32 bytes)
# ---------------------------------------------------------------------------

def short_entry(base, ext, attr, cluster, size, mod_date=MOD_DATE,
                mod_time=MOD_TIME, case_flags=0, deleted=False):
    e = bytearray(32)
    raw_base = base.encode("cp1252", "replace")[:8].ljust(8, b" ")
    raw_ext = ext.encode("cp1252", "replace")[:3].ljust(3, b" ")
    e[0:8] = raw_base
    e[8:11] = raw_ext
    if deleted:
        e[0] = 0xE5
    e[11] = attr
    e[12] = case_flags
    e[14:16] = mod_time.to_bytes(2, "little")          # creation time
    e[16:18] = mod_date.to_bytes(2, "little")          # creation date
    e[18:20] = mod_date.to_bytes(2, "little")          # access date
    e[20:22] = (cluster >> 16).to_bytes(2, "little")   # first cluster hi
    e[22:24] = mod_time.to_bytes(2, "little")          # write time
    e[24:26] = mod_date.to_bytes(2, "little")          # write date
    e[26:28] = (cluster & 0xFFFF).to_bytes(2, "little")
    e[28:32] = size.to_bytes(4, "little")
    return bytes(e)


def lfn_entry(text, checksum, ordinal, total, deleted=False):
    """One 13-character LFN entry: ordinal 1 holds characters 1-13; the
    physically first entry (ordinal == total) carries the 0x40 flag and the
    name tail. Shorter text is terminated by 0x0000 then padded 0xFFFF."""
    e = bytearray(32)
    ord_val = ordinal | (0x40 if ordinal == total else 0)
    e[0] = 0xE5 if deleted else ord_val
    units = [ord(ch) for ch in text[:13]]
    if len(text) < 13:
        units = units + [0x0000] + [0xFFFF] * (12 - len(text))
    idx = 0
    for off, count in ((1, 5), (14, 6), (28, 2)):
        for k in range(count):
            e[off + k * 2: off + k * 2 + 2] = units[idx].to_bytes(2, "little")
            idx += 1
    e[11] = 0x0F
    e[13] = checksum
    return bytes(e)


def checksum_of(raw11):
    """Standard LFN checksum over the 11 raw 8.3 name bytes."""
    chk = 0
    for value in raw11:
        chk = ((((chk >> 1) + ((chk & 1) << 7)) & 0xFF) + value) & 0xFF
    return chk


def lfn_run(long_name, raw11, deleted):
    """The on-disk LFN entry run (reverse logical order) for a long name."""
    chunks = [(long_name[i:i + 13]) for i in range(0, len(long_name), 13)] or [""]
    total = len(chunks)
    chk = checksum_of(raw11)
    return [lfn_entry(chunks[n - 1], chk, n, total, deleted=deleted)
            for n in range(total, 0, -1)]


# ---------------------------------------------------------------------------
# synthetic image
# ---------------------------------------------------------------------------

def build_fat32_image(empty_root=False):
    boot = bytearray(BPS)
    boot[0] = 0xEB
    boot[1] = 0x58
    boot[2] = 0x90
    boot[3:11] = b"MSWIN4.1"
    boot[0x0B:0x0D] = BPS.to_bytes(2, "little")
    boot[0x0D] = SPC
    boot[0x0E:0x10] = RESERVED.to_bytes(2, "little")
    boot[0x10] = FAT_COUNT
    boot[0x11] = 0                               # root entry count (FAT32: 0)
    boot[0x13:0x15] = (0).to_bytes(2, "little")  # total sectors 16 (wide one)
    boot[0x15] = 0xF8                            # media descriptor
    boot[0x16:0x18] = (0).to_bytes(2, "little")  # FAT16 sectors/FAT must be 0
    boot[0x20:0x24] = TOTAL_SECTORS.to_bytes(4, "little")
    boot[0x24:0x28] = SPF.to_bytes(4, "little")
    boot[0x2C:0x30] = (2).to_bytes(4, "little")  # root cluster
    boot[0x30:0x32] = (1).to_bytes(2, "little")  # FSInfo sector
    boot[0x32:0x34] = (6).to_bytes(2, "little")  # backup boot sector
    boot[0x42] = 0x29                            # extended boot signature
    boot[0x43:0x47] = (0x1234ABCD).to_bytes(4, "little")
    boot[0x47:0x52] = b"TESTVOL   "
    boot[0x52:0x5A] = b"FAT32   "
    boot[0x1FE:0x200] = bytes([0x55, 0xAA])

    fat = bytearray(BPS * SPF)
    entries = {0: 0x0FFFFFF8, 1: 0x0FFFFFFF}
    if not empty_root:
        entries.update({
            2: 0x0FFFFFFF,                 # root directory, one cluster
            4: 0x0FFFFFFF,                 # LIVE.TXT
            5: 0x0FFFFFFF,                 # DELETE.TXT
            6: 8, 8: 0x0FFFFFFF,           # FRAG.BIN (gap at cluster 7)
            9: 0x0FFFFFFF,                 # Arabic deleted file
            10: 11, 11: 0x00000000,        # TRUNC.BIN freed mid-chain
            12: 13, 13: 12,                # LOOP.BIN circular chain
            14: 0x0FFFFFFF,                # OVERSIZE.BIN (1 cluster, lies)
            15: 16, 16: 0x0FFFFFFF,        # live 2-cluster LFN file
            20: 0x0FFFFFFF,                # SUBDIR
        })
    for index, value in entries.items():
        fat[index * 4: index * 4 + 4] = int(value).to_bytes(4, "little")
    # dirty high nibbles: parsers must mask the FAT entry to 28 bits
    fat[30 * 4: 30 * 4 + 4] = (0xF1234567).to_bytes(4, "little")

    root_entries = [
        short_entry("TESTVOL", "", 0x08, 0, 0),                        # label
        short_entry("LIVE", "TXT", 0x20, 4, 11),                        # live
    ]
    root_entries += lfn_run(LONGNAME, b"LONGNAMETXT", deleted=False)
    root_entries += [
        short_entry("LONGNAME", "TXT", 0x20, 15, 10, case_flags=0x10),   # live
        short_entry("SUBDIR", "", 0x10, 20, 0),                          # dir
        short_entry("DELETE", "TXT", 0x20, 5, len(DEL_CONTENT),
                    deleted=True),
        short_entry("FRAG", "BIN", 0x20, 6, FRAG_SIZE, deleted=True),
    ]
    root_entries += lfn_run(ARABIC_LONG, b"ARABIC~1TXT", deleted=True)
    root_entries += [
        short_entry("ARABIC~1", "TXT", 0x20, 9, len(ARABIC_CONTENT),
                    deleted=True, case_flags=0x10),
        short_entry("ZERO", "DAT", 0x00, 0, 0, deleted=True),
        short_entry("TRUNC", "BIN", 0x20, 10, 1600, deleted=True),
        short_entry("LOOP", "BIN", 0x20, 12, 1600, deleted=True),
        short_entry("OVERSIZE", "BIN", 0x20, 14, 9000, deleted=True),
    ]
    root_dir = bytearray(CLUSTER_BYTES)
    if not empty_root:
        assert len(root_entries) * 32 <= CLUSTER_BYTES, "root dir overflow"
        for i, entry in enumerate(root_entries):
            root_dir[i * 32: i * 32 + 32] = entry

    image = bytearray(TOTAL_SECTORS * BPS)

    def place_cluster(cluster, payload):
        start = (FIRST_DATA_SECTOR + (cluster - 2) * SPC) * BPS
        image[start:start + len(payload)] = payload[:CLUSTER_BYTES]

    image[0:BPS] = bytes(boot)[:BPS]
    for copy in range(FAT_COUNT):
        start = (RESERVED + copy) * BPS
        image[start:start + BPS] = bytes(fat)[:BPS]
    place_cluster(2, bytes(root_dir))
    if not empty_root:
        place_cluster(4, b"live data!\n")
        place_cluster(5, DEL_CONTENT)
        place_cluster(6, FRAG_A)
        place_cluster(8, FRAG_B)
        place_cluster(9, ARABIC_CONTENT)
        place_cluster(10, bytes([0xAA]) * CLUSTER_BYTES)
        place_cluster(12, bytes([0xBB]) * CLUSTER_BYTES)
        place_cluster(14, OVERSIZE_CONTENT)
        place_cluster(15, b"live long\n")
        place_cluster(16, b"report tail\n")
    meta = {
        "del_content": DEL_CONTENT,
        "arabic_content": ARABIC_CONTENT,
        "frag_content": (FRAG_A + FRAG_B)[:FRAG_SIZE],
        "oversize_content": OVERSIZE_CONTENT,
    }
    return bytes(image), meta


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def raises(exc_type, func, *args):
    try:
        func(*args)
    except exc_type:
        return True
    except Exception:
        return False
    return False


def test_boot(boot):
    ck(boot["bytes_per_sector"] == BPS, "boot: bytes_per_sector 512")
    ck(boot["sectors_per_cluster"] == SPC, "boot: sectors_per_cluster 2")
    ck(boot["cluster_size"] == CLUSTER_BYTES, "boot: cluster size 1024")
    ck(boot["reserved_sector_count"] == RESERVED, "boot: reserved sector count")
    ck(boot["fat_count"] == FAT_COUNT, "boot: fat_count 2")
    ck(boot["sectors_per_fat32"] == SPF, "boot: sectors_per_fat32")
    ck(boot["root_cluster"] == 2, "boot: root cluster read from 0x2C")
    ck(boot["fat_start"] == RESERVED * BPS, "boot: FAT copy 0 at reserved*bps")
    ck(boot["total_sectors"] == TOTAL_SECTORS, "boot: total sectors")
    ck(boot["cluster_count"] == CLUSTERS, "boot: 94 data clusters (FAT32 range)")
    ck(boot["volume_serial"] == 0x1234ABCD, "boot: volume serial")
    ck(boot["volume_label"] == "TESTVOL", "boot: volume label")
    ck(boot["oem_id"] == "MSWIN4.1", "boot: OEM id MSWIN4.1")
    ck(boot["fs_type"] == "FAT32", "boot: FileSysType string")


def test_cluster_address(boot):
    ck(fr.cluster_address(boot, 2) == FIRST_DATA_SECTOR * BPS,
       "addr: cluster 2 sits right after the FAT copies")
    ck(fr.cluster_address(boot, 9) == (FIRST_DATA_SECTOR + 7 * SPC) * BPS,
       "addr: cluster 9 uses the documented formula")
    ck(fr.cluster_address(boot, 5) == (FIRST_DATA_SECTOR + 3 * SPC) * BPS,
       "addr: cluster 5 is volume-relative; Source adds the partition offset")


def test_fat_entry(source, boot):
    cache = {}
    ck(fr.fat_entry(source, boot, 30, cache) == 0x1234567,
       "fat: entry masked to the low 28 bits")
    ck(fr.fat_entry(source, boot, 6, cache) == 8, "fat: entry 6 points to 8")
    value = fr.fat_entry(source, boot, 2, cache)
    ck(value is not None and value >= fr.FAT32_EOC, "fat: entry 2 is EOC")
    ck(fr.fat_entry(source, boot, 11) == fr.FAT32_FREE, "fat: entry 11 is free")


def test_scan(src, boot, meta):
    stats = {}
    cands = fr.scan_deleted(src, boot, stats=stats)
    by_short = {}
    for cand in cands:
        by_short[cand["short_name"]] = cand
    names = set(c["name"] for c in cands)
    ck(len(cands) == 7, "scan: exactly 7 deleted candidates")
    ck("LIVE.TXT" not in names, "scan: live short-name file excluded")
    ck(LONGNAME not in names, "scan: live LFN file excluded")
    ck("SUBDIR" not in names, "scan: live subdirectory not a candidate")
    ck("TESTVOL" not in names, "scan: volume label entry skipped")
    ck(stats.get("subdir_entries") == 1, "scan: one subdir entry counted")
    ck(stats.get("lfn_entries") == 4, "scan: four LFN entries seen (2 live, 2 deleted)")
    del1 = by_short.get("?ELETE.TXT")
    ck(del1 is not None and del1["name"] == "?ELETE.TXT",
       "scan: deleted 8.3 name keeps its tail, '?' marks the lost byte")
    ck(del1 is not None and del1["size_bytes"] == len(meta["del_content"]),
       "scan: DELETE size exact")
    ck(del1 is not None and del1["clusters"] == [5] and del1["carvable"]
       and del1["confidence"] == 0.90, "scan: intact chain gives confidence 0.90")
    ck(del1 is not None and del1["modified"] == MOD_TEXT,
       "scan: DOS date/time decoded to text")
    frag = by_short.get("?RAG.BIN")
    ck(frag is not None and frag["clusters"] == [6, 8],
       "scan: FRAG chain crosses the cluster gap 6->8")
    ck(frag is not None and frag["cluster_count"] == 2 and frag["carvable"]
       and not frag["truncated"], "scan: fragmented but intact")
    arab = by_short.get("?RABIC~1.txt")
    ck(arab is not None, "scan: Arabic 8.3 fallback entry found")
    ck(arab is not None and arab["name"] == ARABIC_LONG,
       "scan: Arabic long name reconstructed exactly")
    ck(arab is not None and arab["long_name"] == ARABIC_LONG,
       "scan: long_name field carries the Arabic text")
    ck(arab is not None and arab["size_bytes"] == len(meta["arabic_content"]),
       "scan: Arabic entry keeps exact size")
    zero = by_short.get("?ERO.DAT")
    ck(zero is not None and zero["confidence"] == 0.35
       and not zero["carvable"] and zero["clusters"] == [],
       "scan: zero-cluster entry -> conf 0.35, not carved")
    trunc = by_short.get("?RUNC.BIN")
    ck(trunc is not None and trunc["truncated"] and not trunc["loop"]
       and trunc["confidence"] == 0.55 and not trunc["carvable"],
       "scan: FAT-freed chain -> truncated, conf 0.55, not carved")
    loop = by_short.get("?OOP.BIN")
    ck(loop is not None and loop["loop"] and loop["truncated"]
       and loop["confidence"] == 0.55 and not loop["carvable"],
       "scan: 12<->13 circular chain detected as loop")
    over = by_short.get("?VERSIZE.BIN")
    ck(over is not None and over["size_bytes"] == 9000
       and over["cluster_count"] == 1,
       "scan: lying size kept, chain still one cluster")
    return by_short


def test_carve(src, boot, by_short, meta, out_dir):
    del1 = by_short["?ELETE.TXT"]
    ck(fr.read_candidate_data(src, boot, del1) == meta["del_content"],
       "carve: deleted ASCII bytes exact")
    frag = by_short["?RAG.BIN"]
    ck(fr.read_candidate_data(src, boot, frag) == meta["frag_content"],
       "carve: fragmented 2000 bytes reassembled across two clusters")
    arab = by_short["?RABIC~1.txt"]
    ck(fr.read_candidate_data(src, boot, arab) == meta["arabic_content"],
       "carve: Arabic UTF-8 bytes exact")
    over = by_short["?VERSIZE.BIN"]
    data = fr.read_candidate_data(src, boot, over)
    ck(len(data) == CLUSTER_BYTES and data == meta["oversize_content"],
       "carve: lying size stops at the chain end, never past the volume")
    ck(fr.read_candidate_data(src, boot, by_short["?ERO.DAT"]) == b"",
       "carve: zero-cluster candidate yields no data")
    ck(fr.read_candidate_data(src, boot, by_short["?OOP.BIN"]) == b"",
       "carve: looping candidate yields no data")
    ck(fr.read_candidate_data(src, boot, by_short["?RUNC.BIN"]) == b"",
       "carve: truncated candidate yields no data")
    capped = fr.read_candidate_data(src, boot, frag, max_bytes=10)
    ck(capped == meta["frag_content"][:10], "carve: max_bytes cap honoured")
    path = fr.carve_candidate(src, boot, arab, out_dir)
    ck(bool(path) and os.path.isfile(path), "carve: Arabic file written to out_dir")
    ck(bool(path) and os.path.basename(path) == ARABIC_LONG,
       "carve: output file keeps the reconstructed long name")
    with open(path, "rb") as fh:
        ck(fh.read() == meta["arabic_content"], "carve: written bytes round-trip")
    ck(fr.carve_candidate(src, boot, by_short["?OOP.BIN"], out_dir) == "",
       "carve: non-carvable candidate writes no file")
    ck(fr.DEFAULT_MAX_CARVE_FILES == 1000
       and fr.DEFAULT_MAX_CARVE_BYTES == 64 * 1024 * 1024,
       "caps: 1000 files / 64 MiB defaults")


def test_lfn_assembler():
    run = lfn_run("ABCDEFGHIJKLMNOP.txt", b"ABCDEFGHTXT", deleted=True)
    name = fr.assemble_lfn([fr.lfn_from_entry(e) for e in run])
    ck(name == "ABCDEFGHIJKLMNOP.txt",
       "lfn: two-entry deleted run reassembled in logical order")
    ck(fr.assemble_lfn([]) == "", "lfn: empty run returns empty text")
    padded = lfn_run("x" * 13 + "y", b"ABCDEFGHTXT", deleted=True)
    ck(fr.assemble_lfn([fr.lfn_from_entry(e) for e in padded]) == "x" * 13 + "y",
       "lfn: 0x0000 terminator and 0xFFFF padding stripped at 14 chars")
    parsed = fr.parse_dir_entry(run[0])
    ck(parsed is not None and parsed["kind"] == "lfn" and parsed["deleted"],
       "lfn: deleted LFN entry parsed with kind and deleted flags")


def test_negative_boot():
    ntfs = bytearray(BPS)
    ntfs[3:11] = b"NTFS    "
    ntfs[0x1FE:0x200] = bytes([0x55, 0xAA])
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(ntfs)),
       "negative: NTFS OEM id rejected")
    fat16 = bytearray(BPS)
    fat16[3:11] = b"MSDOS5.0"
    fat16[0x16:0x18] = (1).to_bytes(2, "little")
    fat16[0x1FE:0x200] = bytes([0x55, 0xAA])
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(fat16)),
       "negative: FAT16 BPB (ReservedSectors16) rejected")
    fat16b = bytearray(BPS)
    fat16b[3:11] = b"MSDOS5.0"
    fat16b[0x1FE:0x200] = bytes([0x55, 0xAA])
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(fat16b)),
       "negative: zero SectorsPerFAT32 rejected")
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(bytearray(BPS - 10))),
       "negative: short boot sector rejected")
    nosig = bytearray(BPS)
    nosig[3:11] = b"MSWIN4.1"
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(nosig)),
       "negative: missing 0x55AA signature rejected")
    unknown = bytearray(BPS)
    unknown[3:11] = b"JOLIET!!"
    unknown[0x1FE:0x200] = bytes([0x55, 0xAA])
    ck(raises(ValueError, fr.parse_fat_boot_sector, bytes(unknown)),
       "negative: unknown OEM id rejected")
    bad_oem = bytearray(BPS)
    bad_oem[3:11] = b"BAD     "
    bad_oem[0x0B:0x0D] = BPS.to_bytes(2, "little")
    bad_oem[0x0D] = SPC
    bad_oem[0x0E:0x10] = RESERVED.to_bytes(2, "little")
    bad_oem[0x10] = FAT_COUNT
    bad_oem[0x20:0x24] = TOTAL_SECTORS.to_bytes(4, "little")
    bad_oem[0x24:0x28] = SPF.to_bytes(4, "little")
    bad_oem[0x2C:0x30] = (2).to_bytes(4, "little")
    bad_oem[0x1FE:0x200] = bytes([0x55, 0xAA])
    parsed = fr.parse_fat_boot_sector(bytes(bad_oem))
    ck(parsed["oem_id"] == "BAD", "negative: 'BAD' OEM prefix accepted as FAT32")


def test_empty_root():
    img, _ = build_fat32_image(empty_root=True)
    tmp = tempfile.mkdtemp(prefix="fat32_empty_")
    try:
        path = os.path.join(tmp, "empty.img")
        with open(path, "wb") as fh:
            fh.write(img)
        src = fr.Source(path)
        try:
            boot = fr.parse_fat_boot_sector(src.read(0, BPS))
            cands = fr.scan_deleted(src, boot)
            ck(cands == [], "empty dir: zero deleted candidates")
            summary = fr.recover_fat(path)
            ck(summary["ok"] and summary["deleted"] == 0 and summary["carved"] == [],
               "empty dir: recover_fat ok with nothing to carve")
        finally:
            src.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli(img_path, meta, out_dir):
    rc = fr.main(["fat32_recovery.py", img_path, "--out", out_dir])
    ck(rc == 0, "cli: exit code 0")
    names = set(os.listdir(out_dir))
    ck("_ELETE.TXT" in names, "cli: deleted ASCII file carved (? -> _ sanitized)")
    ck(ARABIC_LONG in names, "cli: Arabic long name carved")
    ck("_RAG.BIN" in names, "cli: fragmented file carved")
    ck("_VERSIZE.BIN" in names, "cli: oversized file carved")
    ck("_OOP.BIN" not in names and "_RUNC.BIN" not in names
       and "_ERO.DAT" not in names and "LIVE.TXT" not in names,
       "cli: flagged and live entries not carved")
    with open(os.path.join(out_dir, "_ELETE.TXT"), "rb") as fh:
        ck(fh.read() == meta["del_content"], "cli: carved bytes exact")
    rc = fr.main(["fat32_recovery.py", img_path, "--no-carve"])
    ck(rc == 0, "cli: --no-carve exits 0")
    rc = fr.main(["fat32_recovery.py"])
    ck(rc == 2, "cli: missing source returns 2")


def test_offset(img, meta, tmpdir):
    prefix = bytes(4096)
    path = os.path.join(tmpdir, "prefixed.img")
    with open(path, "wb") as fh:
        fh.write(prefix + img)
    src = fr.Source(path, offset=len(prefix), limit=len(img))
    try:
        boot = fr.parse_fat_boot_sector(src.read(0, BPS))
        cands = fr.scan_deleted(src, boot)
        names = set(c["name"] for c in cands)
        ck("?ELETE.TXT" in names, "offset: Source(offset) finds deleted ASCII file")
        ck("LIVE.TXT" not in names, "offset: LIVE.TXT still excluded")
    finally:
        src.close()
    out = os.path.join(tmpdir, "offset_out")
    summary = fr.recover_fat(path, out_dir=out, offset=len(prefix),
                             volume_limit=len(img))
    expected_bytes = (len(meta["del_content"]) + FRAG_SIZE
                      + len(meta["arabic_content"]) + CLUSTER_BYTES)
    ck(summary["ok"] and summary["deleted"] == 7,
       "offset: recover_fat with byte offset lists 7 candidates")
    ck(summary["bytes_written"] == expected_bytes,
       "offset: bytes_written matches the carved total exactly")
    ck(summary["offset"] == 4096 and summary["volume_limit"] == len(img),
       "offset: summary echoes offset and volume limit")


def test_device_path():
    bslash = chr(92)
    ck(fr.device_path("E:") == bslash + bslash + "." + bslash + "E:",
       "device: E: becomes the raw-device path")
    ck(fr.device_path("e:").endswith("E:"),
       "device: lowercase drive letter is upper-cased")
    ck(fr.device_path("image_test.img") == "image_test.img",
       "device: plain file path left alone")
    ck(fr.is_device(bslash + bslash + "." + bslash + "E:"),
       "device: is_device true for the raw-device form")
    ck(not fr.is_device("image_test.img"), "device: is_device false for files")


def main():
    img, meta = build_fat32_image()
    tmpdir = tempfile.mkdtemp(prefix="fat32_recovery_test_")
    try:
        img_path = os.path.join(tmpdir, "fat32.img")
        with open(img_path, "wb") as fh:
            fh.write(img)
        ck(len(img) == TOTAL_SECTORS * BPS and len(img) < 8 * 1024 * 1024,
           "image: %d bytes, under the 8 MiB cap" % len(img))
        src = fr.Source(img_path)
        try:
            boot = fr.parse_fat_boot_sector(src.read(0, BPS))
            test_boot(boot)
            test_cluster_address(boot)
            test_fat_entry(src, boot)
            by_short = test_scan(src, boot, meta)
            out_dir = os.path.join(tmpdir, "out")
            test_carve(src, boot, by_short, meta, out_dir)
        finally:
            src.close()
        test_lfn_assembler()
        test_negative_boot()
        test_empty_root()
        test_cli(img_path, meta, os.path.join(tmpdir, "cli_out"))
        test_offset(img, meta, tmpdir)
        test_device_path()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    if FAILS:
        print("FAILED: %d assertion(s)" % len(FAILS))
        for item in FAILS:
            print("  - " + item)
        return 1
    print("ALL FAT32 RECOVERY PHASE 2 TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
