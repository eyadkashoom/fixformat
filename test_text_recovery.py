#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_text_recovery.py - Phase 1 (NTFS $MFT parser) tests.

Builds a small synthetic NTFS image in a temp file (boot sector + $MFT with
live and deleted FILE records, resident and non-resident data, a fragmented
file, a zeroed data attribute and an attribute-list case) and checks that
text_recovery.py reads the deleted records back with the exact names, sizes
and bytes.

No real disk is touched: the image is a regular file under tempfile.mkdtemp.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import text_recovery as tr

FAILS = []


def ck(cond, msg):
    if cond:
        print("ok:", msg)
    else:
        print("FAIL:", msg)
        FAILS.append(msg)


# ---------------------------------------------------------------------------
# synthetic NTFS fixture
# ---------------------------------------------------------------------------

def _align8(n):
    return (n + 7) & ~7


def _min_bytes(value, signed=False):
    n = 1
    while True:
        try:
            value.to_bytes(n, "little", signed=signed)
            return n
        except OverflowError:
            n += 1


def encode_runlist(runs):
    out = bytearray()
    prev = 0
    for lcn, count in runs:
        if lcn is None:
            len_size = _min_bytes(count, False)
            out.append(len_size)
            out += count.to_bytes(len_size, "little")
        else:
            delta = lcn - prev
            prev = lcn
            len_size = _min_bytes(count, False)
            off_size = _min_bytes(delta, True)
            out.append((off_size << 4) | len_size)
            out += count.to_bytes(len_size, "little")
            out += delta.to_bytes(off_size, "little", signed=True)
    out.append(0)
    return bytes(out)


def build_attr(atype, value=None, runs=None, name="", attr_flags=0, attr_id=0,
               real_size=0, allocated_size=0, start_vcn=0, last_vcn=0):
    name_bytes = name.encode("utf-16-le") if name else b""
    if value is not None:
        value_off = 0x18 + len(name_bytes)
        length = value_off + len(value)
        hdr = bytearray(0x18)
        hdr[0:4] = atype.to_bytes(4, "little")
        hdr[4:8] = length.to_bytes(4, "little")
        hdr[8] = 0
        hdr[9] = len(name)
        hdr[0x0A:0x0C] = (0x18 if name_bytes else 0).to_bytes(2, "little")
        hdr[0x0C:0x0E] = attr_flags.to_bytes(2, "little")
        hdr[0x0E:0x10] = attr_id.to_bytes(2, "little")
        hdr[0x10:0x14] = len(value).to_bytes(4, "little")
        hdr[0x14:0x16] = value_off.to_bytes(2, "little")
        return bytes(hdr) + name_bytes + value
    runlist = encode_runlist(runs or [])
    runlist_off = 0x40 + len(name_bytes)
    length = runlist_off + len(runlist)
    hdr = bytearray(0x40)
    hdr[0:4] = atype.to_bytes(4, "little")
    hdr[4:8] = length.to_bytes(4, "little")
    hdr[8] = 1
    hdr[9] = len(name)
    hdr[0x0A:0x0C] = (0x40 if name_bytes else 0).to_bytes(2, "little")
    hdr[0x0C:0x0E] = attr_flags.to_bytes(2, "little")
    hdr[0x0E:0x10] = attr_id.to_bytes(2, "little")
    hdr[0x10:0x18] = start_vcn.to_bytes(8, "little")
    hdr[0x18:0x20] = last_vcn.to_bytes(8, "little")
    hdr[0x20:0x22] = runlist_off.to_bytes(2, "little")
    hdr[0x22:0x24] = (0).to_bytes(2, "little")
    hdr[0x28:0x30] = allocated_size.to_bytes(8, "little")
    hdr[0x30:0x38] = real_size.to_bytes(8, "little")
    hdr[0x38:0x40] = real_size.to_bytes(8, "little")
    return bytes(hdr) + name_bytes + runlist


def build_file_name(name, parent_record=0, allocated=0, real=0, namespace=1,
                    flags=0):
    nb = name.encode("utf-16-le")
    value = bytearray(0x42 + len(nb))
    value[0:6] = parent_record.to_bytes(6, "little")
    value[6:8] = (1).to_bytes(2, "little")
    value[0x28:0x30] = allocated.to_bytes(8, "little")
    value[0x30:0x38] = real.to_bytes(8, "little")
    value[0x38:0x3C] = flags.to_bytes(4, "little")
    value[0x40] = len(name)
    value[0x41] = namespace
    value[0x42:] = nb
    return bytes(value)


def build_std_info():
    value = bytearray(0x48)
    value[0x20:0x24] = (0x0002).to_bytes(4, "little")
    return bytes(value)


def build_attribute_list(entries):
    out = bytearray()
    for entry in entries:
        name_bytes = entry.get("name", "").encode("utf-16-le")
        length = 0x1A + len(name_bytes)
        raw = bytearray(length)
        raw[0:4] = entry["type"].to_bytes(4, "little")
        raw[4:6] = length.to_bytes(2, "little")
        raw[6] = len(entry.get("name", ""))
        raw[7] = 0x1A if name_bytes else 0
        raw[8:16] = entry.get("lowest_vcn", 0).to_bytes(8, "little")
        ref = (entry.get("sequence", 1) << 48) | entry["record_number"]
        raw[16:24] = ref.to_bytes(8, "little")
        raw[24:26] = entry.get("id", 0).to_bytes(2, "little")
        raw[26:] = name_bytes
        out += raw
    return bytes(out)


def build_record(record_number, flags, attrs, record_size=1024, usn=0x1234):
    sectors = record_size // 512
    usa_size = sectors + 1
    first_attr = _align8(0x30 + 2 * usa_size)
    body = bytearray(first_attr)
    body[0:4] = b"FILE"
    body[4:6] = (0x30).to_bytes(2, "little")
    body[6:8] = usa_size.to_bytes(2, "little")
    body[0x14:0x16] = first_attr.to_bytes(2, "little")
    body[0x16:0x18] = flags.to_bytes(2, "little")
    body[0x1C:0x20] = record_size.to_bytes(4, "little")
    body[0x2C:0x30] = record_number.to_bytes(4, "little")
    for attr in attrs:
        body += attr
    body[0x18:0x1C] = len(body).to_bytes(4, "little")
    body += bytes(record_size - len(body))
    usa = bytearray()
    usa += usn.to_bytes(2, "little")
    for i in range(sectors):
        pos = (i + 1) * 512 - 2
        usa += body[pos:pos + 2]
    body[0x30:0x30 + len(usa)] = usa
    for i in range(sectors):
        pos = (i + 1) * 512 - 2
        body[pos:pos + 2] = usn.to_bytes(2, "little")
    return bytes(body)


def build_boot(cluster_size=4096, total_clusters=64, mft_lcn=4, mftmirr_lcn=2,
               record_size=1024):
    bps = 512
    spc = cluster_size // bps
    total_sectors = total_clusters * spc
    boot = bytearray(512)
    boot[0:3] = bytes([0xEB, 0x52, 0x90])
    boot[3:11] = b"NTFS    "
    boot[0x0B:0x0D] = bps.to_bytes(2, "little")
    boot[0x0D] = spc
    boot[0x15] = 0xF8
    boot[0x18:0x1A] = (63).to_bytes(2, "little")
    boot[0x1A:0x1C] = (255).to_bytes(2, "little")
    boot[0x28:0x30] = total_sectors.to_bytes(8, "little")
    boot[0x30:0x38] = mft_lcn.to_bytes(8, "little")
    boot[0x38:0x40] = mftmirr_lcn.to_bytes(8, "little")
    shift = 0
    rs = record_size
    while rs > 1:
        rs //= 2
        shift += 1
    boot[0x40] = (256 - shift) & 0xFF
    boot[0x44] = 1
    boot[0x48:0x50] = (0x123456789ABCDEF0).to_bytes(8, "little")
    boot[0x50:0x54] = (0).to_bytes(4, "little")
    boot[510:512] = bytes([0x55, 0xAA])
    return bytes(boot)


def build_ntfs_image():
    cluster_size = 4096
    total_clusters = 64
    mft_lcn = 4
    mft_clusters = 3
    mft_bytes = mft_clusters * cluster_size

    live_content = b"live file content" + bytes([10]) + bytes([10]) * 7
    del_content = b"deleted text file content" + bytes([10])
    del_content = del_content * 16
    del_res_content = b"deleted resident content" + bytes([10])
    frag1 = ((b"fragmented part one" + bytes([10])) * 410)[:8192]
    frag2 = ((b"fragmented part two" + bytes([10])) * 100)[:1808]
    frag = frag1 + frag2
    al_content = b"attribute list deleted content" + bytes([10])
    al_content = al_content * 4

    img = bytearray(total_clusters * cluster_size)
    img[0:512] = build_boot(cluster_size, total_clusters, mft_lcn, 2, 1024)

    records = []
    # 0: $MFT itself
    records.append(build_record(0, 1, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("$MFT", 0, mft_bytes, mft_bytes)),
        build_attr(0x80, runs=[(mft_lcn, mft_clusters)], real_size=mft_bytes,
                   allocated_size=mft_bytes, start_vcn=0, last_vcn=mft_clusters - 1),
    ]))
    # 1: live.txt (resident)
    records.append(build_record(1, 1, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("live.txt", 0, len(live_content),
                                               len(live_content))),
        build_attr(0x80, value=live_content),
    ]))
    # 2: deleted.txt (non-resident)
    records.append(build_record(2, 0, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("deleted.txt", 0, cluster_size,
                                               len(del_content))),
        build_attr(0x80, runs=[(10, 1)], real_size=len(del_content),
                   allocated_size=cluster_size, start_vcn=0, last_vcn=0),
    ]))
    # 3: deleted_resident.txt
    records.append(build_record(3, 0, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("deleted_resident.txt", 0,
                                               len(del_res_content),
                                               len(del_res_content))),
        build_attr(0x80, value=del_res_content),
    ]))
    # 4: live.bin (non-resident)
    records.append(build_record(4, 1, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("live.bin", 0, cluster_size, 64)),
        build_attr(0x80, runs=[(11, 1)], real_size=64, allocated_size=cluster_size,
                   start_vcn=0, last_vcn=0),
    ]))
    # 5: deleted_frag.txt (two runs)
    records.append(build_record(5, 0, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("deleted_frag.txt", 0,
                                               cluster_size * 3, len(frag))),
        build_attr(0x80, runs=[(12, 2), (14, 1)], real_size=len(frag),
                   allocated_size=cluster_size * 3, start_vcn=0, last_vcn=2),
    ]))
    # 6: deleted_zeroed.txt (data attribute zeroed)
    records.append(build_record(6, 0, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("deleted_zeroed.txt", 0, 0, 0)),
        build_attr(0x80, runs=[], real_size=0, allocated_size=0, start_vcn=0,
                   last_vcn=0),
    ]))
    # 7: unused
    records.append(bytes(1024))
    # 8: extension record holding the $DATA of record 9
    child = bytearray(build_record(8, 1, [
        build_attr(0x80, runs=[(15, 1)], real_size=len(al_content),
                   allocated_size=cluster_size, start_vcn=0, last_vcn=0, attr_id=2),
    ]))
    child[0x20:0x28] = (9 | (1 << 48)).to_bytes(8, "little")
    records.append(bytes(child))
    # 9: deleted_alist.txt (attribute list)
    alist = build_attribute_list([{"type": 0x80, "record_number": 8, "id": 2,
                                   "lowest_vcn": 0, "name": ""}])
    records.append(build_record(9, 0, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("deleted_alist.txt", 0,
                                               cluster_size, len(al_content))),
        build_attr(0x18, value=alist),
    ]))
    # 10: live2.txt (resident)
    records.append(build_record(10, 1, [
        build_attr(0x10, value=build_std_info()),
        build_attr(0x30, value=build_file_name("live2.txt", 0, 5, 5)),
        build_attr(0x80, value=b"live2"),
    ]))
    # 11: unused
    records.append(bytes(1024))

    mft = b"".join(records)
    img[mft_lcn * cluster_size: mft_lcn * cluster_size + len(mft)] = mft
    img[10 * cluster_size: 10 * cluster_size + len(del_content)] = del_content
    img[11 * cluster_size: 11 * cluster_size + 64] = bytes([0x41]) * 64
    img[12 * cluster_size: 12 * cluster_size + len(frag1)] = frag1
    img[14 * cluster_size: 14 * cluster_size + len(frag2)] = frag2
    img[15 * cluster_size: 15 * cluster_size + len(al_content)] = al_content

    meta = {
        "cluster_size": cluster_size,
        "mft_lcn": mft_lcn,
        "live_content": live_content,
        "del_content": del_content,
        "del_res_content": del_res_content,
        "frag": frag,
        "al_content": al_content,
    }
    return bytes(img), meta


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_boot_and_layout(src, boot, layout):
    ck(boot["bytes_per_sector"] == 512, "boot: bytes per sector")
    ck(boot["sectors_per_cluster"] == 8, "boot: sectors per cluster")
    ck(boot["cluster_size"] == 4096, "boot: cluster size")
    ck(boot["mft_lcn"] == 4, "boot: $MFT LCN")
    ck(boot["record_size"] == 1024, "boot: MFT record size")
    ck(layout["record_count"] == 12, "layout: 12 records (got %d)" % layout["record_count"])
    ck(layout["record_size"] == 1024, "layout: record size")


def test_runlist_roundtrip():
    runs = [(4, 2), (None, 3), (9, 1)]
    parsed = tr.parse_runlist(encode_runlist(runs))
    ck(parsed == runs, "runlist: roundtrip %r" % (parsed,))
    ck(tr.parse_runlist(bytes([0x11, 0x01])) == [], "runlist: truncated header stops")
    ck(tr.parse_runlist(b"") == [], "runlist: empty input")


def test_fixup():
    buf = bytearray(1024)
    buf[0:4] = b"FILE"
    buf[4:6] = (48).to_bytes(2, "little")
    buf[6:8] = (3).to_bytes(2, "little")
    usn = 0xABCD
    buf[48:50] = usn.to_bytes(2, "little")
    buf[50:52] = bytes([0x11, 0x22])
    buf[52:54] = bytes([0x33, 0x44])
    buf[510:512] = usn.to_bytes(2, "little")
    buf[1022:1024] = usn.to_bytes(2, "little")
    fixed = tr.apply_fixup(bytes(buf))
    ck(fixed[510:512] == bytes([0x11, 0x22]), "fixup: sector 0 restored")
    ck(fixed[1022:1024] == bytes([0x33, 0x44]), "fixup: sector 1 restored")


def test_record_parse():
    ck(tr.parse_record(bytes(1024)) is None, "record: bad magic rejected")
    ck(tr.parse_record(None) is None, "record: None rejected")


def test_scan(src, boot, meta):
    cands = tr.scan_deleted(src, boot)
    by_name = {}
    for cand in cands:
        by_name[cand["name"]] = cand
    ck(len(cands) == 5, "scan: 5 deleted candidates (got %d)" % len(cands))
    ck("deleted.txt" in by_name, "scan: deleted.txt found")
    ck(by_name.get("deleted.txt", {}).get("size") == len(meta["del_content"]),
       "scan: deleted.txt size")
    ck(by_name.get("deleted.txt", {}).get("deleted") is True, "scan: deleted flag set")
    ck(by_name.get("deleted.txt", {}).get("carvable") is True, "scan: deleted.txt carvable")
    ck(by_name.get("deleted.txt", {}).get("runs") == [[10, 1]], "scan: deleted.txt runs")
    ck("live.txt" not in by_name, "scan: live.txt not reported")
    ck("live.bin" not in by_name, "scan: live.bin not reported")
    ck("live2.txt" not in by_name, "scan: live2.txt not reported")
    ck(by_name.get("deleted_resident.txt", {}).get("resident") is True,
       "scan: resident deleted file flagged")
    ck(by_name.get("deleted_frag.txt", {}).get("runs") == [[12, 2], [14, 1]],
       "scan: fragmented runs")
    ck(by_name.get("deleted_zeroed.txt", {}).get("carvable") is False,
       "scan: zeroed data not carvable")
    ck(by_name.get("deleted_alist.txt", {}).get("carvable") is True,
       "scan: attribute-list data resolved")
    ck(by_name.get("deleted_alist.txt", {}).get("attribute_list") is True,
       "scan: attribute-list flag set")
    return by_name


def test_carve(src, layout, by_name, meta, out_dir):
    path = tr.carve_candidate(src, layout, by_name["deleted.txt"], out_dir)
    ck(os.path.exists(path), "carve: deleted.txt written")
    with open(path, "rb") as fh:
        ck(fh.read() == meta["del_content"], "carve: deleted.txt content exact")
    path = tr.carve_candidate(src, layout, by_name["deleted_resident.txt"], out_dir)
    with open(path, "rb") as fh:
        ck(fh.read() == meta["del_res_content"], "carve: resident content exact")
    path = tr.carve_candidate(src, layout, by_name["deleted_frag.txt"], out_dir)
    with open(path, "rb") as fh:
        ck(fh.read() == meta["frag"], "carve: fragmented content exact")
    path = tr.carve_candidate(src, layout, by_name["deleted_alist.txt"], out_dir)
    with open(path, "rb") as fh:
        ck(fh.read() == meta["al_content"], "carve: attribute-list content exact")


def test_negative():
    try:
        tr.parse_boot_sector(b"MSDOS5.0" + bytes(504))
        ck(False, "negative: non-NTFS boot rejected")
    except ValueError:
        ck(True, "negative: non-NTFS boot rejected")
    try:
        tr.parse_boot_sector(bytes(512))
        ck(False, "negative: zero boot rejected")
    except ValueError:
        ck(True, "negative: zero boot rejected")


def test_cli(img_path, meta, out_dir):
    rc = tr.main(["text_recovery.py", img_path, "--out", out_dir])
    ck(rc == 0, "cli: exit code 0 (got %d)" % rc)
    names = set(os.listdir(out_dir))
    ck("deleted.txt" in names, "cli: deleted.txt carved")
    ck("deleted_frag.txt" in names, "cli: deleted_frag.txt carved")
    with open(os.path.join(out_dir, "deleted.txt"), "rb") as fh:
        ck(fh.read() == meta["del_content"], "cli: deleted.txt content exact")


def test_offset(img, meta, tmpdir):
    prefix = bytes(4096)
    path = os.path.join(tmpdir, "prefixed.img")
    with open(path, "wb") as fh:
        fh.write(prefix + img)
    src = tr.Source(path, offset=len(prefix), limit=len(img))
    try:
        boot = tr.parse_boot_sector(src.read(0, 512))
        layout = tr.mft_layout(src, boot)
        cands = tr.scan_deleted(src, boot)
        names = set(c["name"] for c in cands)
        ck("deleted.txt" in names, "offset: partition scan finds deleted.txt")
        ck("live.txt" not in names, "offset: live.txt excluded")
    finally:
        src.close()


def main():
    img, meta = build_ntfs_image()
    tmpdir = tempfile.mkdtemp(prefix="text_recovery_test_")
    try:
        img_path = os.path.join(tmpdir, "ntfs.img")
        with open(img_path, "wb") as fh:
            fh.write(img)
        src = tr.Source(img_path)
        try:
            boot = tr.parse_boot_sector(src.read(0, 512))
            layout = tr.mft_layout(src, boot)
            test_boot_and_layout(src, boot, layout)
            test_runlist_roundtrip()
            test_fixup()
            test_record_parse()
            by_name = test_scan(src, boot, meta)
            out_dir = os.path.join(tmpdir, "out")
            test_carve(src, layout, by_name, meta, out_dir)
            test_negative()
            cli_dir = os.path.join(tmpdir, "cli_out")
            test_cli(img_path, meta, cli_dir)
            test_offset(img, meta, tmpdir)
        finally:
            src.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    if FAILS:
        print("FAILED: %d assertion(s)" % len(FAILS))
        for item in FAILS:
            print("  - " + item)
        return 1
    print("ALL TEXT_RECOVERY PHASE 1 TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
