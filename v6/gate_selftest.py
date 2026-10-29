#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_selftest.py - synthetic images that prove volume_gate's decisions.

Every case is built in a temporary directory from known bytes, so nothing here
touches a real disk. What is asserted:

1. classify_volume on the five OEM ids and on eight zero bytes, plus an empty
   buffer and a BitLocker stub with no -FVE-FS- OEM id (which must NOT be called
   encrypted).
2. GPT with an NTFS partition and a BitLocker partition -> verdict 'mixed', the
   NTFS one scannable, the FVE one blocked, and the message naming the key. The
   BitLocker verdict must quote entropy samples as corroboration.
3. An all-BitLocker image -> 'encrypted' with an empty scannable list.
4. An MBR-only image -> the MBR fallback path, with the partition classified from
   its VBR.
5. A 4 GiB file whose first 1 MiB is data and the rest zeros -> "content ends at
   1048576 of 4294967296 bytes", not "empty".
6. A volume with high entropy but an NTFS OEM id -> NOT called encrypted.
7. The source is never modified (size, mtime and hash of the small images).

Exit 0 on pass; on the first failure, one clear FAIL line on stderr and exit 1.

Run:  ./python/python.exe v6/gate_selftest.py
"""

import hashlib
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import volume_gate as vg  # noqa: E402

SECTOR = 512
MIB = 1024 * 1024
BIG = 4 * 1024 ** 3

BASIC_DATA = "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"
MSR = "{e3c9e316-0b5c-4db8-817d-f92df00215ae}"


class Failed(Exception):
    pass


class Checks(object):
    def __init__(self):
        self.count = 0

    def ok(self, condition, label, detail=None):
        self.count += 1
        if not condition:
            if detail is None:
                raise Failed(label)
            raise Failed("%s [got %r]" % (label, detail))


# ---------------------------------------------------------------------------
# boot sectors
# ---------------------------------------------------------------------------

def ntfs_vbr():
    vbr = bytearray(512)
    vbr[0:3] = b"\xeb\x52\x90"
    vbr[3:11] = b"NTFS    "
    struct.pack_into("<H", vbr, 0x0B, 512)
    vbr[0x0D] = 8
    struct.pack_into("<Q", vbr, 0x30, 4)
    vbr[0x40] = 0xF6
    vbr[510:512] = b"\x55\xaa"
    return bytes(vbr)


def fve_vbr():
    vbr = bytearray(512)
    vbr[0:4] = b"\xeb\x58\x90\x2d"
    vbr[3:11] = b"-FVE-FS-"
    struct.pack_into("<H", vbr, 0x0B, 512)
    vbr[0x0D] = 8                  # the dummy FAT32 BPB BitLocker writes
    vbr[0x47:0x5A] = b"-FVE-FS-"   # the fake label
    vbr[510:512] = b"\x55\xaa"
    return bytes(vbr)


def fat_vbr(oem=b"MSDOS5.0"):
    vbr = bytearray(512)
    vbr[0:3] = b"\xeb\x3c\x90"
    vbr[3:11] = oem
    vbr[510:512] = b"\x55\xaa"
    return bytes(vbr)


def text_fill(n):
    unit = b"NTFS metadata block; deleted record 0x42; $FILE_NAME. "
    return (unit * (n // len(unit) + 1))[:n]


def random_fill(n):
    return random.Random(0x5EED).randbytes(n)


# ---------------------------------------------------------------------------
# image builders
# ---------------------------------------------------------------------------

def make_gpt_image(path, specs):
    """specs: list of dict(type=GUID, name=str, sectors=int, vbr=bytes, fill=fn).

    Partitions are laid out from LBA 2048 with no gaps, and the file ends exactly
    where the last partition ends, so a well-formed image has no zero tail.
    """
    first = 2048
    entries = []
    for spec in specs:
        last = first + spec["sectors"] - 1
        entries.append((spec, first, last))
        first = last + 1
    total = first
    image = bytearray(total * SECTOR)

    mbr = bytearray(512)
    mbr[446 + 4] = 0xEE                       # protective entry
    struct.pack_into("<I", mbr, 446 + 8, 1)
    struct.pack_into("<I", mbr, 446 + 12, min(total - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    image[0:512] = mbr

    header = bytearray(512)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, total - 1)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, total - 34)
    header[56:72] = uuid.UUID("11111111-2222-3333-4444-555555555555").bytes_le
    struct.pack_into("<Q", header, 72, 2)     # entry array at LBA 2
    struct.pack_into("<I", header, 80, len(entries))
    struct.pack_into("<I", header, 84, 128)
    # The header CRC is left zero on purpose: the gate reads fields, not the CRC.
    image[512:1024] = header

    for index, (spec, first_lba, last_lba) in enumerate(entries):
        entry = bytearray(128)
        entry[0:16] = uuid.UUID(spec["type"]).bytes_le
        entry[16:32] = uuid.UUID("%08d-0000-0000-0000-000000000000"
                                 % index).bytes_le
        struct.pack_into("<Q", entry, 32, first_lba)
        struct.pack_into("<Q", entry, 40, last_lba)
        name = spec["name"].encode("utf-16-le")[:72]
        entry[56:56 + len(name)] = name
        image[1024 + index * 128:1024 + (index + 1) * 128] = entry

        offset = first_lba * SECTOR
        span = (last_lba - first_lba + 1) * SECTOR
        image[offset:offset + 512] = spec["vbr"]
        payload = spec["fill"](span - 512)
        image[offset + 512:offset + 512 + len(payload)] = payload
        image[offset + span - 1] = 0xAA       # content ends at EOF, not before

    with open(path, "wb") as handle:
        handle.write(image)
    return total


def make_mbr_image(path, ptype, first_lba, sectors, vbr, fill):
    total = first_lba + sectors
    image = bytearray(total * SECTOR)
    mbr = bytearray(512)
    mbr[446] = 0x80
    mbr[446 + 4] = ptype
    struct.pack_into("<I", mbr, 446 + 8, first_lba)
    struct.pack_into("<I", mbr, 446 + 12, sectors)
    mbr[510:512] = b"\x55\xaa"
    image[0:512] = mbr
    offset = first_lba * SECTOR
    image[offset:offset + 512] = vbr
    payload = fill(sectors * SECTOR - 512)
    image[offset + 512:offset + 512 + len(payload)] = payload
    image[-1] = 0xAA
    with open(path, "wb") as handle:
        handle.write(image)
    return total


def make_truncated_image(path, size=BIG):
    """First 1 MiB is a plaintext NTFS volume; everything after it is zeros.

    On Windows the file is marked sparse first (FSCTL_SET_SPARSE) so extending it
    to 4 GiB costs neither disk space nor eight seconds of zero-fill; the bytes
    read back are the same zeros. Any failure falls back to a plain truncate.
    """
    with open(path, "wb") as handle:
        sparse = _try_mark_sparse(handle)
        handle.write(ntfs_vbr())
        handle.write(b"X" * (MIB - 512))
        handle.flush()
        if sparse:
            # Extending a sparse file by one byte at the end makes the 3 GiB gap
            # a hole instantly; truncate() would zero-fill it first.
            handle.seek(size - 1)
            handle.write(b"\x00")
        else:
            handle.truncate(size)


def _try_mark_sparse(handle):
    if os.name != "nt":
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        FSCTL_SET_SPARSE = 0x000900C4
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None)
        return bool(ok)
    except Exception:
        return False


def digest(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        left = limit
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n <= 0:
                break
            block = handle.read(n)
            if not block:
                break
            h.update(block)
            if left is not None:
                left -= len(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

def case_classify(c):
    cases = [
        ("NTFS", ntfs_vbr(), "ntfs"),
        ("-FVE-FS-", fve_vbr(), "bitlocker"),
        ("MSDOS5.0", fat_vbr(b"MSDOS5.0"), "fat32"),
        ("EXFAT", fat_vbr(b"EXFAT   "), "fat32"),
        ("MSWIN4.1", fat_vbr(b"MSWIN4.1"), "fat32"),
        ("eight zero bytes", b"\x00" * 8, "unformatted"),
        ("512 zero bytes", b"\x00" * 512, "unformatted"),
        ("empty buffer", b"", "unknown"),
    ]
    for label, vbr, want in cases:
        got = vg.classify_volume(vbr)
        c.ok(got["kind"] == want, "classify_volume(%s) is %s" % (label, want), got)
        c.ok(isinstance(got["reason"], str) and got["reason"],
             "classify_volume(%s) explains itself" % label, got)
        c.ok(set(got) == {"kind", "reason"},
             "classify_volume(%s) returns exactly kind+reason" % label, list(got))

    stub = bytearray(512)
    stub[0:4] = b"\xeb\x58\x90\x2d"
    got = vg.classify_volume(bytes(stub))
    c.ok(got["kind"] == "unknown",
         "a BitLocker stub without the -FVE-FS- OEM id is not called encrypted",
         got)
    c.ok("stub" in got["reason"], "the stub-only reason says why", got["reason"])


def case_mixed_gpt(tmp, c):
    path = os.path.join(tmp, "mixed.img")
    make_gpt_image(path, [
        dict(type=MSR, name="Microsoft reserved", sectors=2048,
             vbr=bytes(512), fill=lambda n: b""),
        dict(type=BASIC_DATA, name="Basic data partition", sectors=8192,
             vbr=ntfs_vbr(), fill=text_fill),
        dict(type=BASIC_DATA, name="Basic data partition", sectors=12288,
             vbr=fve_vbr(), fill=random_fill),
    ])
    before_stat = os.stat(path)
    before_hash = digest(path)

    probe = vg.probe_source(path)
    parts = probe["partitions"]
    message = probe["message"]

    c.ok(probe["verdict"] == "mixed", "GPT+NTFS+BitLocker is 'mixed'",
         probe["verdict"])
    c.ok(len(parts) == 3, "three GPT entries were read", len(parts))
    c.ok([p["kind"] for p in parts] == ["msr", "ntfs", "bitlocker"],
         "MSR promoted by GPT type GUID, then NTFS, then BitLocker",
         [p["kind"] for p in parts])
    c.ok(set(parts[0]) == {"index", "first_lba", "last_lba", "bytes", "name",
                           "oem_id", "kind", "reason"},
         "partition dicts carry exactly the contracted keys", sorted(parts[0]))
    c.ok(parts[2]["oem_id"] == "-FVE-FS-", "the FVE volume reports its OEM id",
         parts[2]["oem_id"])

    c.ok(len(probe["scannable"]) == 1, "exactly one partition is scannable",
         len(probe["scannable"]))
    c.ok(probe["scannable"][0]["index"] == 1
         and probe["scannable"][0]["kind"] == "ntfs",
         "the scannable partition is the NTFS one",
         probe["scannable"][0]["index"])
    c.ok(len(probe["blocked"]) == 1, "exactly one partition is blocked",
         len(probe["blocked"]))
    c.ok(probe["blocked"][0]["index"] == 2
         and probe["blocked"][0]["kind"] == "bitlocker",
         "the blocked partition is the BitLocker one",
         probe["blocked"][0]["index"])

    samples = probe["blocked"][0]["entropy"]
    c.ok(len(samples) == 8, "the BitLocker volume carries 8 entropy samples",
         len(samples))
    c.ok(samples and min(samples) >= 7.5,
         "the BitLocker corroboration is high entropy, not one byte", samples)
    c.ok("bits/byte" in message and "-FVE-FS-" in message,
         "the message quotes both the OEM id and the entropy")

    c.ok("48-digit recovery password" in message, "the message names the password")
    c.ok(".bek" in message, "the message names the .bek key file")
    c.ok("Microsoft account" in message, "the message names the Microsoft account")
    c.ok("partition 2" in message and "is BitLocker-encrypted" in message,
         "the message names the encrypted partition",
         [ln for ln in message.splitlines() if "FVE" in ln])
    c.ok("false positives only" in message,
         "the message says why carving it is pointless")
    c.ok("Scan targets:" in message and "partition 1" in message,
         "the message lists what is still scannable")
    c.ok("Not scanned:" in message and "MSR" in message,
         "the message lists the reserved partition it skips")
    c.ok(vg.explain(probe) == message, "explain(probe) is the stored message")

    after = os.stat(path)
    c.ok(after.st_size == before_stat.st_size
         and after.st_mtime_ns == before_stat.st_mtime_ns,
         "the source file was not written to (size and mtime unchanged)")
    c.ok(digest(path) == before_hash, "the source file hash is unchanged")
    return path


def case_all_fve(tmp, c):
    path = os.path.join(tmp, "all_fve.img")
    make_gpt_image(path, [
        dict(type=BASIC_DATA, name="Basic data partition", sectors=8192,
             vbr=fve_vbr(), fill=random_fill),
    ])
    probe = vg.probe_source(path)
    c.ok(probe["verdict"] == "encrypted", "an all-FVE image is 'encrypted'",
         probe["verdict"])
    c.ok(probe["scannable"] == [], "an all-FVE image has nothing scannable",
         probe["scannable"])
    c.ok(len(probe["blocked"]) == 1, "the one partition is blocked",
         len(probe["blocked"]))
    c.ok("0 of 1" in probe["message"], "the message says 0 of 1 is scannable")
    c.ok("What this run can still do: nothing" in probe["message"],
         "the message promises nothing from an all-ciphertext image")


def case_mbr(tmp, c):
    path = os.path.join(tmp, "mbr.img")
    make_mbr_image(path, 0x07, 2048, 4096, ntfs_vbr(), text_fill)
    probe = vg.probe_source(path)
    c.ok(probe["table"] == "mbr", "the MBR fallback path was used", probe["table"])
    c.ok(len(probe["partitions"]) == 1, "one MBR partition was read",
         len(probe["partitions"]))
    part = probe["partitions"][0]
    c.ok(part["first_lba"] == 2048 and part["last_lba"] == 6143,
         "the MBR LBA range is exact",
         (part["first_lba"], part["last_lba"]))
    c.ok(part["kind"] == "ntfs", "the MBR partition is classified from its VBR",
         part["kind"])
    c.ok(probe["verdict"] == "plaintext", "the MBR image is 'plaintext'",
         probe["verdict"])
    c.ok("No BitLocker volume found" in probe["message"],
         "the message says no BitLocker volume was found")

    parts = vg.read_partitions(path)
    c.ok(len(parts) == 1 and parts[0]["kind"] == "ntfs",
         "read_partitions() alone finds the MBR volume", parts)


def case_truncated(tmp, c):
    path = os.path.join(tmp, "holey.img")
    make_truncated_image(path)
    before = os.stat(path)
    probe = vg.probe_source(path)
    content = probe["content"]

    c.ok(content["end"] == MIB, "content ends at 1 MiB of the 4 GiB file",
         content["end"])
    c.ok(probe["verdict"] != "empty",
         "a truncated image is not reported as 'empty'", probe["verdict"])
    c.ok(any("content ends at" in w for w in probe["warnings"]),
         "the warning says content ends at", probe["warnings"])
    c.ok("content ends at 1048576 of 4294967296 bytes" in probe["message"],
         "the message gives both the byte offsets")
    c.ok("1.00 MiB of 4.00 GiB" in probe["message"],
         "the message gives both the human sizes")
    c.ok("for every rate" in probe["message"],
         "the message says why the number matters")

    after = os.stat(path)
    c.ok(after.st_size == before.st_size
         and after.st_mtime_ns == before.st_mtime_ns,
         "the 4 GiB source was not written to")

    # entropy samples must clamp to the file, not read past it
    last = BIG // SECTOR
    samples = vg.entropy_samples(path, last - 8, last + 10000, samples=3)
    c.ok(len(samples) == 3, "entropy samples clamp inside the file", samples)
    c.ok(all(0.0 <= e <= 8.0 for e in samples),
         "entropy stays in 0..8 bits/byte", samples)
    c.ok(vg.entropy_samples(path, 10, 5) == [],
         "an empty LBA range yields no samples")
    c.ok(vg.entropy_samples(path, 0, last, samples=0) == [],
         "samples=0 yields no samples")


def case_high_entropy_ntfs(tmp, c):
    path = os.path.join(tmp, "compressed_ntfs.img")
    size = 4 * MIB
    image = bytearray(size)
    image[0:512] = ntfs_vbr()
    image[512:] = random.Random(0xC0FFEE).randbytes(size - 512)
    image[-1] = 0xAA
    with open(path, "wb") as handle:
        handle.write(image)

    probe = vg.probe_source(path)
    c.ok(probe["table"] == "none"
         and len(probe["partitions"]) == 1
         and probe["partitions"][0]["name"].startswith("whole source"),
         "an image without a table gets one synthetic whole-source entry",
         probe["table"])
    c.ok(probe["partitions"][0]["kind"] == "ntfs",
         "the high-entropy volume is still NTFS by OEM id",
         probe["partitions"][0]["kind"])
    c.ok(probe["verdict"] == "plaintext",
         "high entropy with an NTFS OEM id is not 'encrypted'", probe["verdict"])
    c.ok(probe["blocked"] == [], "nothing is blocked on a plaintext NTFS volume",
         probe["blocked"])
    samples = probe["scannable"][0]["entropy"]
    c.ok(samples and min(samples) >= 7.5,
         "the volume really is high entropy", samples)
    joined = " ".join(probe["warnings"])
    c.ok("high entropy alone is not encryption" in joined,
         "the warning explains why entropy is not a verdict", joined)
    c.ok("is BitLocker-encrypted" not in probe["message"],
         "the message does not call the plaintext volume encrypted")


def case_degraded(tmp, c, mixed_path):
    """A table that cannot be read must not become invented partitions."""
    cut = os.path.join(tmp, "cut_gpt.img")
    with open(mixed_path, "rb") as source, open(cut, "wb") as target:
        target.write(source.read(900))
    parts = vg.read_partitions(cut)
    c.ok(len(parts) == 1 and parts[0]["name"].startswith("whole source"),
         "a GPT whose entry array is unreadable yields one synthetic entry",
         parts)
    c.ok(parts[0]["kind"] not in ("ntfs", "bitlocker"),
         "no volume kind is invented from an unreadable table", parts[0])
    probe = vg.probe_source(cut)
    c.ok(any("GPT entry array" in w for w in probe["warnings"]),
         "the warning says the GPT entry array could not be read",
         probe["warnings"])

    missing = os.path.join(tmp, "does_not_exist.img")
    probe = vg.probe_source(missing)
    c.ok(probe["verdict"] == "empty" and probe["scannable"] == [],
         "a missing source refuses cleanly instead of raising",
         probe["verdict"])
    c.ok("could not be read" in probe["message"]
         and "does not exist" in probe["message"],
         "the message says the path is missing, not that the disk is empty",
         probe["message"])


def case_limits_and_empty(tmp, c, mixed_path):
    clipped = vg.read_partitions(mixed_path, limit_bytes=4 * MIB)
    c.ok(len(clipped) == 3, "limit_bytes does not drop the GPT entries",
         len(clipped))
    c.ok(clipped[2]["kind"] == "unknown"
         and "past the examined content" in clipped[2]["reason"],
         "a partition beyond limit_bytes is not classified from thin air",
         clipped[2])

    probe = vg.probe_source(mixed_path, limit_bytes=4 * MIB)
    c.ok(any("limit_bytes" in w for w in probe["warnings"]),
         "the probe warns that examination was limited", probe["warnings"])
    c.ok(all(p["index"] != 2 for p in probe["scannable"]),
         "a partition beyond the limit is never a scan target",
         [p["index"] for p in probe["scannable"]])

    empty = os.path.join(tmp, "empty.img")
    open(empty, "wb").close()
    probe = vg.probe_source(empty)
    c.ok(probe["size"] == 0 and probe["partitions"] == [],
         "an empty file has no partitions", (probe["size"],
                                             probe["partitions"]))
    c.ok(probe["verdict"] == "empty", "an empty file is 'empty'",
         probe["verdict"])
    c.ok("Nothing here can be scanned" in probe["message"],
         "the message for an empty file is plain")


# ---------------------------------------------------------------------------

def case_volume_device(tmp, c):
    """A raw volume device: no partition table, and no size it will admit to.

    This is the shape of `C:` seen as a device, and of a dd image of one volume.
    A previous build called it 'empty' and refused it, which would have blocked
    a legitimate scan of a plaintext volume - the exact false negative this case
    locks down.
    """
    path = os.path.join(tmp, "volume_image.bin")
    with open(path, "wb") as fh:
        fh.write(ntfs_vbr())
        fh.write(random_fill(256 * 1024))

    real = vg._Source._probe_size
    try:
        vg._Source._probe_size = lambda self: None      # a device that says nothing
        device = vg.probe_source(path)
    finally:
        vg._Source._probe_size = real

    c.ok(device["verdict"] == "plaintext", "a volume device is plaintext",
         device["verdict"])
    c.ok(len(device["scannable"]) == 1, "a volume device is scannable",
         len(device["scannable"]))
    c.ok(device["scannable"][0]["kind"] == "ntfs", "a volume device is NTFS",
         device["scannable"][0]["kind"])
    c.ok(device["scannable"][0]["bytes"] is None,
         "an unknown size is reported as unknown, not as zero",
         device["scannable"][0]["bytes"])
    c.ok(device["blocked"] == [], "a volume device blocks nothing",
         device["blocked"])

    sized = vg.probe_source(path)     # the same bytes, with a real file size
    c.ok(sized["verdict"] == "plaintext" and len(sized["scannable"]) == 1,
         "a one-volume image with a real size is scannable too", sized["verdict"])

def main():
    started = time.time()
    checks = Checks()
    tmp = tempfile.mkdtemp(prefix="gate_selftest_")
    print("building synthetic images in %s" % tmp)
    try:
        case_classify(checks)
        print("  ok  classify_volume on five OEM ids, zeros, stub, empty")
        mixed = case_mixed_gpt(tmp, checks)
        print("  ok  GPT: NTFS scannable, BitLocker blocked, message names the key")
        case_all_fve(tmp, checks)
        print("  ok  all-BitLocker image is 'encrypted' with nothing scannable")
        case_mbr(tmp, checks)
        print("  ok  MBR fallback path")
        case_truncated(tmp, checks)
        print("  ok  4 GiB holey image reports 'content ends at 1048576 of ...'")
        case_high_entropy_ntfs(tmp, checks)
        print("  ok  high entropy with an NTFS OEM id is not 'encrypted'")
        case_degraded(tmp, checks, mixed)
        print("  ok  unreadable GPT table and missing path degrade without guessing")
        case_limits_and_empty(tmp, checks, mixed)
        print("  ok  limit_bytes clamp and empty file")
        case_volume_device(tmp, checks)
        print("  ok  a raw volume device (no table, unknown size) stays scannable")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.time() - started
    print("%d assertions passed in %.2f s" % (checks.count, elapsed))
    if elapsed > 20.0:
        print("FAIL: selftest took %.2f s, the budget is 20 s" % elapsed,
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failed as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        sys.exit(1)
