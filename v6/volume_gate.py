#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
volume_gate.py - read-only pre-scan gate: which volumes can honestly be scanned.

Why this exists
---------------
A raw scan of a BitLocker volume produces false positives only: ciphertext has
no headers and no structure, so every "hit" is a signature landing on random
bytes at the rate chance predicts (two bytes: once per 65,536 random bytes, i.e.
~16,384 times per GiB). One 512-byte read per partition, before any scanning,
tells the difference between "there are files here" and "this is 461 GiB of
noise".

What it does
------------
1. Partition table: GPT at LBA 1 (`EFI PART`, entries at the LBA and with the
   count/size the header names); MBR fallback when the GPT signature is absent;
   one synthetic entry covering the whole source when neither table exists.
2. Volume classification from the VBR OEM id at offset 3 -
   `NTFS    ` plaintext NTFS, `-FVE-FS-` BitLocker, `MSDOS5.0` FAT (usually the
   EFI system partition), eight zero bytes MSR/unformatted.
3. Corroboration, never one byte: a BitLocker verdict quotes the OEM id and the
   entropy samples, so a human can check it. High entropy ALONE is never called
   encryption - a JPEG or a ZIP measures 7.9 bits/byte too.
4. Coverage: the last non-zero 64 KiB block, so a truncated or pre-allocated
   image is reported as "content ends at X of Y bytes", never as "empty".
5. A user-facing message that names what is encrypted, why carving it is
   pointless, and exactly which key is needed.

Rules
-----
* READ-ONLY. Opens the source "rb", seeks and reads; never writes to it.
* Never guesses a key, never attempts decryption. A BitLocker volume is
  reported, not opened.
* Works on image files and on `\\\\.\\PhysicalDriveN`: size comes from a
  seek-to-end and back, and a device that refuses degrades with a warning
  instead of an exception.
* Python standard library only. No subprocess, no ctypes, no new dependency.

Public API
----------
    classify_volume(vbr)                       -> {'kind', 'reason'}
    read_partitions(path, limit_bytes=None)    -> [partition dict, ...]
    entropy_samples(path, first_lba, last_lba, samples=8, block=65536)
                                               -> [bits/byte, ...]
    probe_source(path)                         -> probe dict
    explain(probe)                             -> str

Partition dict keys: index, first_lba, last_lba, bytes, name, oem_id, kind,
reason. Probe keys: size, partitions, verdict ('plaintext' | 'mixed' |
'encrypted' | 'empty'), scannable, blocked, warnings, message (+ table,
content, source for diagnostics).

Command line (read-only):  python volume_gate.py <image | \\\\.\\PhysicalDriveN>
"""

import math
import os
import sys
import textwrap

# ----------------------------------------------------------------------
# ANSI color contracts (v6): terminal output only; never leaks to files
# ----------------------------------------------------------------------

def _ansi_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


_COLOR_GREEN = "\033[32m"
_COLOR_RED = "\033[31m"
_COLOR_YELLOW = "\033[33m"
_COLOR_BLUE = "\033[34m"
_COLOR_RESET = "\033[0m"


def green(text):
    return ("%s%s%s" % (_COLOR_GREEN, text, _COLOR_RESET)) if _ansi_enabled() else text


def red(text):
    return ("%s%s%s" % (_COLOR_RED, text, _COLOR_RESET)) if _ansi_enabled() else text


def yellow(text):
    return ("%s%s%s" % (_COLOR_YELLOW, text, _COLOR_RESET)) if _ansi_enabled() else text


def blue(text):
    return ("%s%s%s" % (_COLOR_BLUE, text, _COLOR_RESET)) if _ansi_enabled() else text


__all__ = ["classify_volume", "read_partitions", "entropy_samples",
           "probe_source", "explain", "green", "red", "yellow", "blue"]

SECTOR = 512                    # GPT and VBR addressing unit
SAMPLE_BLOCK = 64 * 1024        # entropy sample and content-end block size
SWEEP_CHUNK = 8 * 1024 * 1024   # sequential read size for the content sweep
CONTENT_TAIL_WINDOW = 4 * 1024 ** 3   # default bytes checked at the tail
SAMPLE_SPAN = 8 * 1024 ** 3     # probe entropy samples cover this much of a volume
MATERIAL_GAP = 64 * 1024 ** 2   # a smaller tail than this is reported, not alarmed
HIGH_ENTROPY = 7.5              # bits/byte that warrants a warning (not a verdict)
MAX_GPT_ENTRIES = 256
MAX_GPT_ENTRY_SIZE = 4096

GPT_SIGN = b"EFI PART"
OEM_NTFS = b"NTFS    "
OEM_FVE = b"-FVE-FS-"
FAT_OEMS = (b"MSDOS5.0", b"MSDOS4.0", b"MSDOS3.3", b"MSWIN4.1", b"MSWIN4.0",
            b"EXFAT   ")
FVE_STUB = b"\xeb\x58\x90\x2d"

MSR_GUID = "{e3c9e316-0b5c-4db8-817d-f92df00215ae}"
EFI_GUID = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"
BASIC_DATA_GUID = "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"
WINRE_GUID = "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}"

SCANNABLE_KINDS = ("ntfs", "fat32", "unknown")
SKIP_KINDS = ("msr", "unformatted")
KIND_LABEL = {"ntfs": "NTFS", "fat32": "FAT", "msr": "MSR",
              "unformatted": "unformatted", "unknown": "unknown",
              "bitlocker": "BitLocker"}

_ZERO_CHUNK = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _int(buf, off, size, signed=False):
    """Little-endian integer, 0 when the field is outside the buffer."""
    if off < 0 or size <= 0 or off + size > len(buf):
        return 0
    return int.from_bytes(buf[off:off + size], "little", signed=signed)


def _guid(raw):
    """Canonical GUID text from the 16 bytes as stored on disk."""
    if len(raw) < 16:
        return ""
    return "{%08x-%04x-%04x-%s-%s}" % (
        _int(raw, 0, 4), _int(raw, 4, 2), _int(raw, 6, 2),
        raw[8:10].hex(), raw[10:16].hex())


def _entropy(block):
    """Shannon entropy in bits per byte, 0.0 for an empty block."""
    if not block:
        return 0.0
    counts = [0] * 256
    for byte in block:
        counts[byte] += 1
    total = float(len(block))
    bits = 0.0
    for count in counts:
        if count:
            p = count / total
            bits -= p * math.log2(p)
    return round(bits, 4)


def _zero_chunk():
    global _ZERO_CHUNK
    if _ZERO_CHUNK is None:
        _ZERO_CHUNK = b"\x00" * SWEEP_CHUNK
    return _ZERO_CHUNK


def _fmt_bytes(n):
    """Human size: 512 B, 1.14 GiB, ..."""
    if n is None:
        return "unknown"
    n = int(n)
    if n < 1024:
        return "%d B" % n
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024 or unit == "TiB":
            return "%.2f %s" % (value, unit)


def _is_device(path):
    return path.startswith("\\\\.\\") or path.startswith("\\\\?\\")


def _open_hint(exc):
    """One sentence that says what the user can actually do about a failed open."""
    if isinstance(exc, PermissionError):
        return ("Access denied. On Windows a raw device needs the tool to run as "
                "Administrator, and the disk must not be open in another program.")
    if isinstance(exc, FileNotFoundError):
        return ("That path does not exist. Check the image path, and whether the "
                "drive is still connected.")
    if isinstance(exc, IsADirectoryError):
        return "That path is a directory, not an image file or a device."
    return ("The source could not be opened. Check the path, and on Windows "
            "whether the tool is running elevated.")


def _wrap_text(text, width=78, indent="", first_indent=None):
    if first_indent is None:
        first_indent = indent
    return textwrap.wrap(text, width=width, initial_indent=first_indent,
                         subsequent_indent=indent) or [""]


# ---------------------------------------------------------------------------
# 1. classification from the volume boot record
# ---------------------------------------------------------------------------

def classify_volume(vbr):
    """Classify a volume from its 512-byte boot sector.

    Returns {'kind', 'reason'} with kind one of 'ntfs', 'bitlocker', 'fat32',
    'msr', 'unformatted', 'unknown'. The OEM id at offset 3 is the evidence;
    'msr' cannot be told from 'unformatted' at the VBR, so this function returns
    'unformatted' and read_partitions() promotes it using the GPT type GUID.
    """
    return _classify(vbr, None)


def _classify(vbr, gpt_type=None):
    if vbr is None:
        vbr = b""
    vbr = bytes(vbr)
    if not vbr:
        return {"kind": "unknown",
                "reason": "no boot sector bytes were supplied: nothing can be "
                          "claimed about this volume"}
    if len(vbr) < 11:
        if len(vbr) >= 8 and not vbr.strip(b"\x00"):
            return {"kind": "unformatted",
                    "reason": "only %d byte(s) supplied and all of them are "
                              "0x00, so the OEM id at offset 3 is zero for as "
                              "far as it was read: MSR or unformatted (the GPT "
                              "type GUID is what separates them)" % len(vbr)}
        return {"kind": "unknown",
                "reason": "only %d byte(s) of boot sector available: the OEM id "
                          "at offset 3 needs 11, so nothing can be claimed about "
                          "this volume" % len(vbr)}
    oem = vbr[3:11]
    stub = vbr[0:4] == FVE_STUB
    printable = oem.decode("latin-1")

    if oem == OEM_NTFS or oem.rstrip() == b"NTFS":
        return {"kind": "ntfs",
                "reason": "OEM id 'NTFS    ' at VBR offset 3: plaintext NTFS, "
                          "metadata (MFT, $Recycle.Bin, $UsnJrnl) is readable"}

    if oem == OEM_FVE:
        reason = ("OEM id '-FVE-FS-' at VBR offset 3: BitLocker-encrypted volume")
        reason += (" (first sector begins eb 58 90 2d, the encrypted boot stub)"
                   if stub else
                   " (note: the first sector does not begin eb 58 90 2d)")
        reason += (". Its payload is ciphertext; the FAT32 BPB in the header is "
                   "a dummy, so bytes_per_sector/sectors_per_cluster from it are "
                   "not evidence of anything")
        return {"kind": "bitlocker", "reason": reason}

    if oem in FAT_OEMS or oem.startswith(b"MSDOS") or oem.startswith(b"MSWIN") \
            or oem.startswith(b"FAT"):
        label = "exFAT" if oem == b"EXFAT   " else "FAT"
        return {"kind": "fat32",
                "reason": "OEM id %r at VBR offset 3: %s volume (reported under "
                          "the fat32 kind - same plaintext FAT family); usually "
                          "the EFI system partition" % (printable, label)}

    if oem == b"\x00" * 8:
        if gpt_type == MSR_GUID:
            return {"kind": "msr",
                    "reason": "GPT type %s (Microsoft reserved) with an all-zero "
                              "VBR: reserved space, no file system to read"
                              % MSR_GUID}
        return {"kind": "unformatted",
                "reason": "all eight OEM-id bytes at offset 3 are 0x00: Microsoft "
                          "reserved (MSR) or unformatted - the VBR cannot tell "
                          "them apart, the GPT type GUID can; nothing to read"}

    if stub:
        return {"kind": "unknown",
                "reason": "no recognised OEM id (%r at offset 3), but the first "
                          "sector begins eb 58 90 2d like a BitLocker stub; "
                          "without the -FVE-FS- OEM id this is reported as "
                          "unknown, not called encrypted" % printable}

    return {"kind": "unknown",
            "reason": "no recognised OEM id at offset 3 (%r): neither trusted as "
                      "plaintext nor called encrypted" % printable}


# ---------------------------------------------------------------------------
# 2. read-only source (image file or raw device)
# ---------------------------------------------------------------------------

class _Source(object):
    """Read-only view of an image file or a raw device."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.reads = 0
        self.bytes_read = 0
        self.read_errors = []
        self.size = self._probe_size()

    def _probe_size(self):
        # Seek to the end and back. Never assume getsize(): a raw device has no
        # file size, and an imager's .partial file has a size that is not the
        # size of the disk.
        try:
            self.f.seek(0, os.SEEK_END)
            end = self.f.tell()
            self.f.seek(0)
            if end > 0:
                return end
        except OSError:
            pass
        try:
            st = os.fstat(self.f.fileno())
            if st.st_size > 0:
                return st.st_size
        except OSError:
            pass
        return None if _is_device(self.path) else 0

    def read(self, offset, length):
        if length is None or length <= 0 or offset is None or offset < 0:
            return b""
        offset = int(offset)
        length = int(length)
        if self.size is not None:
            if offset >= self.size:
                return b""
            length = min(length, self.size - offset)
        try:
            self.f.seek(offset)
        except OSError as exc:
            self.read_errors.append((offset, str(exc)))
            return b""
        chunks = []
        got = 0
        while got < length:
            try:
                block = self.f.read(length - got)
            except OSError as exc:
                self.read_errors.append((offset + got, str(exc)))
                break
            if not block:
                break
            chunks.append(block)
            got += len(block)
            if got < length:
                try:
                    self.f.seek(offset + got)
                except OSError as exc:
                    self.read_errors.append((offset + got, str(exc)))
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
# 3. partition tables
# ---------------------------------------------------------------------------

def read_partitions(path, limit_bytes=None):
    """Partitions of a source, read-only.

    GPT first (header at LBA 1), MBR fallback when 'EFI PART' is absent, else one
    synthetic entry covering the whole source. Each dict has index, first_lba,
    last_lba, bytes, name, oem_id, kind, reason.
    """
    with _Source(path) as src:
        return _read_table(src, limit_bytes)["parts"]


def _read_table(src, limit_bytes=None):
    notes = []
    limit = src.size
    if limit_bytes is not None and int(limit_bytes) >= 0:
        limit = int(limit_bytes) if limit is None else min(limit, int(limit_bytes))
        if src.size is not None and limit < src.size:
            notes.append("examination limited to the first %s of the source by "
                         "limit_bytes" % _fmt_bytes(limit))

    if src.size == 0:
        return {"table": "none", "parts": [], "limit": limit, "notes":
                ["the source is 0 bytes: there is no partition table to read"]}

    header = src.read(SECTOR, SECTOR)
    if header[:8] == GPT_SIGN:
        parts, gpt_notes = _gpt_partitions(src, header, limit)
        notes.extend(gpt_notes)
        if parts:
            return {"table": "gpt", "parts": parts, "limit": limit,
                    "notes": notes}
        notes.append("the GPT header at LBA 1 named no usable entry: falling "
                     "back to the MBR table")

    boot = src.read(0, SECTOR)
    parts, mbr_notes = _mbr_partitions(src, boot, limit)
    if parts:
        notes.extend(mbr_notes)
        return {"table": "mbr", "parts": parts, "limit": limit, "notes": notes}
    notes.extend(mbr_notes)

    if not header[:8] == GPT_SIGN:
        notes.append("no GPT header at LBA 1 (signature %r) and no usable MBR "
                     "table: the whole source is treated as one volume"
                     % header[:8])
    part = _partition(src, 0, 0,
                      max(0, (limit or src.size or SECTOR) // SECTOR - 1),
                      "whole source (no partition table)", None, limit, notes)
    if src.size is None:
        # A raw volume device answers no size. It is still a file system, and a
        # scan of it runs to end-of-source, so keep it scannable and report the
        # size as unknown instead of calling the range empty.
        part["bytes"] = None
        notes.append("the source reports no size (a raw volume device): a scan "
                     "runs to the end of the source and the size is unknown")
    return {"table": "none", "parts": [part], "limit": limit, "notes": notes}


def _gpt_partitions(src, header, limit):
    notes = []
    if len(header) < 92:
        return [], ["the GPT header at LBA 1 is only %d byte(s): truncated "
                    "source" % len(header)]
    table_lba = _int(header, 0x48, 8)
    count = _int(header, 0x50, 4)
    entry_size = _int(header, 0x54, 4)
    if count <= 0:
        return [], ["the GPT header declares 0 partition entries"]
    if count > MAX_GPT_ENTRIES:
        notes.append("the GPT header declares %d entries; reading the first %d"
                     % (count, MAX_GPT_ENTRIES))
        count = MAX_GPT_ENTRIES
    if entry_size < 128 or entry_size > MAX_GPT_ENTRY_SIZE or entry_size % 8:
        notes.append("the GPT entry size %d is implausible; assuming 128 bytes"
                     % entry_size)
        entry_size = 128

    raw = src.read(table_lba * SECTOR, count * entry_size)
    if len(raw) < entry_size:
        return [], ["the GPT entry array at LBA %d is not readable (got %d "
                    "byte(s)): the image may end before it" % (table_lba,
                                                               len(raw))]

    parts = []
    for i in range(count):
        entry = raw[i * entry_size:(i + 1) * entry_size]
        if len(entry) < 128:
            break
        if entry[0:16] == b"\x00" * 16:
            continue
        first = _int(entry, 32, 8)
        last = _int(entry, 40, 8)
        if last < first:
            notes.append("GPT entry %d has last_lba < first_lba; ignored" % i)
            continue
        name = entry[56:128].decode("utf-16-le", "replace").rstrip("\x00").strip()
        parts.append(_partition(src, i, first, last,
                                name or "GPT type %s" % _guid(entry[0:16]),
                                _guid(entry[0:16]), limit, notes))
    if not parts:
        notes.append("every GPT entry at LBA %d was empty or invalid" % table_lba)
    return parts, notes


def _mbr_partitions(src, boot, limit):
    notes = []
    if len(boot) < SECTOR or boot[510:512] != b"\x55\xaa":
        return [], []
    candidates = []
    saw_protective = False
    for slot in range(4):
        entry = boot[446 + slot * 16:446 + (slot + 1) * 16]
        ptype = entry[4]
        if ptype == 0:
            continue
        if ptype == 0xEE:
            saw_protective = True
            notes.append("MBR slot %d is the 0xEE protective entry: it points "
                         "at the GPT range, not at a volume" % slot)
            continue
        if ptype in (0x05, 0x0F, 0x85):
            notes.append("MBR extended-partition container 0x%02X at slot %d is "
                         "not walked: logical volumes inside it were not checked"
                         % (ptype, slot))
            continue
        if entry[0] not in (0x00, 0x80):
            notes.append("MBR slot %d has an implausible boot flag 0x%02X; "
                         "ignored" % (slot, entry[0]))
            continue
        first = _int(entry, 8, 4)
        count = _int(entry, 12, 4)
        if first == 0 or count == 0:
            notes.append("MBR slot %d has an empty LBA range; ignored" % slot)
            continue
        if limit is not None and first * SECTOR >= limit:
            notes.append("MBR slot %d starts at LBA %d, past the end of this "
                         "source" % (slot, first))
            continue
        candidates.append((slot, ptype, first, first + count - 1))

    parts = []
    for slot, ptype, first, last in candidates:
        parts.append(_partition(src, slot, first, last, "MBR type 0x%02X" % ptype,
                                None, limit, notes))

    # Corroborate the table: an NTFS boot sector also ends in 55 AA at offset
    # 510, so bytes that parse as an MBR are not proof of one. Require at least
    # one entry to point at a recognisable boot sector before trusting it.
    if parts and not any(p["kind"] in ("ntfs", "fat32", "bitlocker",
                                       "unformatted", "msr") for p in parts):
        notes.append("bytes at offset 446 parse as an MBR but no entry points "
                     "at a recognisable boot sector: the table is not trusted")
        return [], notes
    if parts and saw_protective:
        notes.append("both a protective 0xEE entry and real MBR entries were "
                     "present; the MBR entries were used")
    return parts, notes


def _partition(src, index, first_lba, last_lba, name, gpt_type, limit, notes):
    nbytes = (last_lba - first_lba + 1) * SECTOR if last_lba >= first_lba else 0
    if limit is not None and first_lba * SECTOR >= limit:
        vbr = b""      # outside what the caller asked us to examine
    else:
        vbr = src.read(first_lba * SECTOR, SECTOR)
    if len(vbr) >= 11:
        cls = _classify(vbr, gpt_type)
        kind, reason = cls["kind"], cls["reason"]
        oem = vbr[3:11].decode("latin-1")
    else:
        oem = ""
        kind = "unknown"
        if limit is not None and first_lba * SECTOR >= limit:
            reason = ("no boot sector at LBA %d: the partition starts past the "
                      "examined content (%s)" % (first_lba, _fmt_bytes(limit)))
        else:
            reason = ("no boot sector at LBA %d: it lies beyond the end of this "
                      "source or the device refused the read" % first_lba)
        notes.append("partition %d has no readable boot sector at LBA %d"
                     % (index, first_lba))
    if src.size is not None and nbytes and (last_lba + 1) * SECTOR > src.size:
        notes.append("partition %d ends at %s, past the end of this source (%s)"
                     % (index, _fmt_bytes((last_lba + 1) * SECTOR),
                        _fmt_bytes(src.size)))
    return {"index": index, "first_lba": int(first_lba),
            "last_lba": int(last_lba), "bytes": int(nbytes), "name": name,
            "oem_id": oem, "kind": kind, "reason": reason}


# ---------------------------------------------------------------------------
# 4. entropy samples
# ---------------------------------------------------------------------------

def entropy_samples(path, first_lba, last_lba, samples=8, block=SAMPLE_BLOCK):
    """Entropy (bits/byte) of evenly spread samples inside an LBA range.

    Reads nothing outside the source, and nothing outside the range: offsets are
    clamped to the file length, so a truncated image shortens the last sample
    instead of raising. Returns fewer than `samples` values when the range has
    no readable bytes.
    """
    with _Source(path) as src:
        return _entropy_samples(src, first_lba, last_lba, samples, block)


def _entropy_samples(src, first_lba, last_lba, samples=8, block=SAMPLE_BLOCK):
    if samples is None or int(samples) <= 0 or block is None or int(block) <= 0:
        return []
    samples = int(samples)
    block = int(block)
    start = max(0, int(first_lba)) * SECTOR
    end = (int(last_lba) + 1) * SECTOR
    if src.size is not None:
        end = min(end, src.size)
    if end <= start:
        return []
    span = end - start
    out = []
    for i in range(samples):
        offset = start + (span * i) // samples
        length = min(block, end - offset)
        if length <= 0:
            break
        data = src.read(offset, length)
        if not data:
            continue
        out.append(_entropy(data))
    return out


# ---------------------------------------------------------------------------
# 5. content coverage ("empty" and "never imaged" are different answers)
# ---------------------------------------------------------------------------

def _sweep_result(end, size, start, scanned, complete, window_all_zero, error,
                  block=None):
    return {"end": end, "block": block, "size": size, "window_start": start,
            "scanned": scanned, "complete": complete,
            "window_all_zero": window_all_zero, "read_error": error}


def _content_sweep(src, limit, window):
    """Offset just past the last non-zero byte, read sequentially to the end.

    A backward seek per block is 10x slower than a forward stream on Windows
    (each seek defeats read-ahead), so the sweep streams forward and remembers
    the last non-zero chunk, then walks that chunk backwards in 64 KiB blocks -
    the same answer, at read speed. `window` limits how many tail bytes are
    examined; 0 or None means the whole source.
    """
    size = src.size if limit is None else limit
    if size is None:
        return _sweep_result(None, None, 0, 0, False, False, None)
    size = int(size)
    if size <= 0:
        return _sweep_result(0, size, 0, 0, True, False, None)

    if window is None:
        window = CONTENT_TAIL_WINDOW
    try:
        window = int(window)
    except (TypeError, ValueError, OverflowError):
        window = 0          # uninterpretable: examine the whole source
    start = 0
    if window and 0 < int(window) < size:
        start = size - int(window)
        start -= start % SWEEP_CHUNK

    try:
        src.f.seek(start)
    except OSError as exc:
        return _sweep_result(None, size, start, 0, False, False,
                             "seek failed: %s" % exc)

    zero = _zero_chunk()
    pos = start
    scanned = 0
    last = None
    error = None
    while pos < size:
        want = min(SWEEP_CHUNK, size - pos)
        try:
            block = src.f.read(want)
        except OSError as exc:
            error = "read failed at +%d: %s" % (pos, exc)
            break
        if not block:
            error = "read at +%d returned 0 bytes" % pos
            break
        src.reads += 1
        src.bytes_read += len(block)
        if len(block) == len(zero):
            if block != zero:
                last = (pos, block)
        elif block.count(0) != len(block):
            last = (pos, block)
        pos += len(block)
        scanned += len(block)

    complete = pos >= size
    if last is None:
        if complete and start == 0:
            return _sweep_result(0, size, start, scanned, True, False, error)
        # A tail window that is all zeros proves the tail is hole, not where
        # the content stops; a failed read proves nothing at all.
        return _sweep_result(None, size, start, scanned, complete,
                             bool(complete and start > 0), error)
    if not complete and error:
        return _sweep_result(None, size, start, scanned, False, False, error)

    base, block = last
    end, block_at = _last_nonzero_in(block, base)
    return _sweep_result(end, size, start, scanned, complete, False, error,
                         block_at)


def _last_nonzero_in(block, base):
    """End offset (and block offset) of the last non-zero 64 KiB block."""
    offset = len(block)
    while offset > 0:
        begin = max(0, offset - SAMPLE_BLOCK)
        piece = block[begin:offset]
        if piece.count(0) != len(piece):
            trailing = len(piece) - len(piece.rstrip(b"\x00"))
            return base + begin + len(piece) - trailing, base + begin
        offset = begin
    return base + len(block), base + max(0, len(block) - SAMPLE_BLOCK)


# ---------------------------------------------------------------------------
# 6. the probe
# ---------------------------------------------------------------------------

def probe_source(path, limit_bytes=None, tail_window=None):
    """Probe a source read-only and decide what a scan may touch.

    `limit_bytes` examines only that prefix of the source. `tail_window` caps how
    many tail bytes the content sweep reads (default CONTENT_TAIL_WINDOW = 4 GiB;
    0 sweeps the whole source and gives the exact content end at the cost of a
    full read).

    Returns {'size', 'partitions', 'verdict', 'scannable', 'blocked',
    'warnings', 'message', 'table', 'content', 'source'}. `scannable` and
    `blocked` hold partition dicts plus 'entropy' (and 'scan_bytes' for
    scannable ones).
    """
    warnings = []
    try:
        src = _Source(path)
    except OSError as exc:
        probe = {"path": path, "size": 0, "partitions": [], "verdict": "empty",
                 "scannable": [], "blocked": [],
                 "warnings": ["cannot open %s: %s" % (path, exc)],
                 "message": "", "table": "none", "error": str(exc),
                 "error_hint": _open_hint(exc),
                 "content": _sweep_result(0, 0, 0, 0, True, False, None),
                 "source": {"reads": 0, "bytes_read": 0, "errors": []}}
        probe["message"] = explain(probe)
        return probe

    try:
        table = _read_table(src, limit_bytes)
        parts = table["parts"]
        limit = table["limit"]
        warnings.extend(table["notes"])

        window = CONTENT_TAIL_WINDOW if tail_window is None else tail_window
        content = _content_sweep(src, limit, window)
        if content["read_error"]:
            warnings.append("content sweep incomplete: %s" % content["read_error"])

        size = src.size
        content_end = content["end"]
        if content_end is not None and not content["complete"]:
            content_end = None

        blocked = []
        scannable = []
        skipped = []
        for part in parts:
            start = part["first_lba"] * SECTOR
            declared_end = (part["last_lba"] + 1) * SECTOR
            end = declared_end if limit is None else min(declared_end, limit)
            readable = bool(part["oem_id"])

            if part["kind"] == "bitlocker":
                s_first, s_last = _sample_window(part)
                samples = _entropy_samples(src, s_first, s_last)
                blocked.append(_decorate(part, samples,
                                         sample_bytes=_span_bytes(s_first, s_last)))
                continue

            if part["kind"] in SKIP_KINDS:
                skipped.append(part)
                continue

            if not readable:
                warnings.append(
                    "partition %d (%s) has no readable boot sector and cannot "
                    "be scanned" % (part["index"], _fmt_bytes(part["bytes"])))
                skipped.append(part)
                continue

            if end <= start or (part["bytes"] is not None
                                and part["bytes"] <= 0):
                warnings.append(
                    "partition %d (%s) has an empty LBA range and cannot be "
                    "scanned" % (part["index"], _fmt_bytes(part["bytes"])))
                skipped.append(part)
                continue

            if content_end is not None and start >= content_end:
                warnings.append(
                    "partition %d (%s) starts at +%s, inside the zero tail "
                    "(content ends at +%s): nothing was imaged there"
                    % (part["index"], _fmt_bytes(part["bytes"]),
                       _fmt_bytes(start), _fmt_bytes(content_end)))
                skipped.append(part)
                continue

            s_first, s_last = _sample_window(part)
            samples = _entropy_samples(src, s_first, s_last)
            item = _decorate(part, samples,
                             scan_bytes=None if part["bytes"] is None
                             else end - start,
                             sample_bytes=_span_bytes(s_first, s_last))
            scannable.append(item)
            if samples and min(samples) >= HIGH_ENTROPY:
                warnings.append(
                    "partition %d (%s) reads %.2f-%.2f bits/byte over %d "
                    "sample(s), but its OEM id is %r: high entropy alone is not "
                    "encryption (a JPEG or a ZIP measures the same), so it stays "
                    "a scan target and is not reported as encrypted"
                    % (part["index"], _fmt_bytes(part["bytes"]), min(samples),
                       max(samples), len(samples), part["oem_id"]))

        gap = (size - content_end) if (content_end is not None and size
                                       and content_end < size) else 0
        material = gap >= max(MATERIAL_GAP, (size or 0) // 100)
        content["gap"] = gap
        content["truncated"] = bool(material)

        if content["window_all_zero"]:
            warnings.append(
                "the last %s read as zeros (window from +%s): the tail is hole, "
                "but the exact content end was not located - pass "
                "tail_window=0 for a full sweep"
                % (_fmt_bytes(content["scanned"]),
                   _fmt_bytes(content["window_start"])))
        elif content_end == 0 and size:
            warnings.append("every byte read is zero: content ends at 0 of %d "
                            "bytes (%s)" % (size, _fmt_bytes(size)))
        elif gap and limit is not None and content_end >= limit:
            warnings.append(
                "examination stopped at the %s limit before the source ended; "
                "the content end was not established" % _fmt_bytes(limit))
        elif material:
            warnings.append(
                "content ends at %d of %d bytes (%s of %s): the image is "
                "truncated or pre-allocated, so everything past that point is "
                "zeros and must not be counted"
                % (content_end, size, _fmt_bytes(content_end),
                   _fmt_bytes(size)))

        if not blocked and not scannable:
            verdict = "empty"
        elif blocked and scannable:
            verdict = "mixed"
        elif blocked:
            verdict = "encrypted"
        else:
            verdict = "plaintext"

        probe = {"path": path, "size": size, "partitions": parts,
                 "verdict": verdict, "scannable": scannable,
                 "blocked": blocked, "warnings": warnings, "message": "",
                 "table": table["table"], "content": content,
                 "limit_bytes": limit if (size is not None and limit != size)
                 else None,
                 "skipped": skipped,
                 "source": {"reads": src.reads, "bytes_read": src.bytes_read,
                            "errors": list(src.read_errors)}}
        if src.size is None:
            probe["warnings"].append(
                "source size unknown (the device refused a seek-to-end): the "
                "content-end check was skipped")
        probe["message"] = explain(probe)
        return probe
    finally:
        src.close()


def _sample_window(part):
    """LBA range the probe samples: the first SAMPLE_SPAN of the volume.

    A BitLocker volume is often only partly imaged (ciphertext at the front, a
    zero hole behind it), so sampling the whole declared range would report
    0.00-8.00 bits/byte and prove nothing. The first 8 GiB holds the volume
    header and real ciphertext whenever any was imaged at all.
    """
    first = int(part["first_lba"])
    last = int(part["last_lba"])
    span = max(1, SAMPLE_SPAN // SECTOR)
    if part.get("bytes") is None:
        # Unknown size (a raw volume device): sample the first SAMPLE_SPAN.
        last = first + span - 1
    return first, min(last, first + span - 1)


def _span_bytes(first_lba, last_lba):
    return max(0, (int(last_lba) - int(first_lba) + 1) * SECTOR)


def _decorate(part, entropy, scan_bytes=None, sample_bytes=None):
    item = dict(part)
    item["entropy"] = list(entropy)
    if scan_bytes is not None:
        item["scan_bytes"] = int(scan_bytes)
    if sample_bytes is not None:
        item["sample_bytes"] = int(sample_bytes)
    return item


# ---------------------------------------------------------------------------
# 7. the user-facing message
# ---------------------------------------------------------------------------

def explain(probe):
    """The user-facing block for a probe: what is encrypted, why carving it is
    pointless, what key is needed, and what the plaintext remainder can return.
    """
    probe = probe or {}
    parts = probe.get("partitions") or []
    blocked = probe.get("blocked") or []
    scan = probe.get("scannable") or []
    skipped = probe.get("skipped") or []
    verdict = probe.get("verdict") or "empty"
    content = probe.get("content") or {}
    size = probe.get("size")

    if probe.get("error"):
        hint = probe.get("error_hint") or (
            "The source could not be opened. Check the path, and on Windows "
            "whether the tool is elevated.")
        lines = _wrap_text("Volume gate: the source could not be read (%s)."
                           % probe["error"])
        lines.extend(_wrap_text(hint))
        lines.append("")
        lines.append("Nothing can be scanned until that is fixed.")
        return "\n".join(lines)

    out = []
    add = out.append

    total = len(parts)
    if verdict == "mixed":
        for line in _wrap_text(
                "%d of %d partition%s %s BitLocker-encrypted; %d can still be "
                "scanned." % (len(blocked), total, "" if total == 1 else "s",
                              "is" if len(blocked) == 1 else "are", len(scan))):
            add(yellow(line))
    elif verdict == "encrypted":
        for line in _wrap_text(
                "Every partition in this source is BitLocker-encrypted: 0 of %d "
                "can be scanned." % total):
            add(red(line))
    elif verdict == "plaintext":
        for line in _wrap_text(
                "No BitLocker volume found: all %d partition%s carry a plaintext "
                "OEM id, so their metadata can be read."
                % (total, "" if total == 1 else "s")):
            add(green(line))
    else:
        for line in _wrap_text(
                "Nothing here can be scanned: no partition carries a file system "
                "this tool can read."):
            add(yellow(line))
    add("")

    for part in blocked:
        add(_fve_block(part))

    for part in scan:
        samples = part.get("entropy") or []
        if part.get("kind") == "unknown" and samples \
                and min(samples) >= HIGH_ENTROPY:
            add(_entropy_block(part, samples))

    if size:
        end = content.get("end")
        scanned = content.get("scanned") or 0
        gap = max(0, size - end) if end is not None else 0
        material = gap >= max(MATERIAL_GAP, size // 100)
        if end is not None and 0 <= end < size and material:
            add("[!] content ends at %d of %d bytes (%s of %s): the image"
                % (end, size, _fmt_bytes(end), _fmt_bytes(size)))
            add("    stopped early or was pre-allocated, so every byte past that")
            add("    point is zeros. Use %s, not %s, for every rate and every"
                % (_fmt_bytes(end), _fmt_bytes(size)))
            add("    projection.")
        elif end is not None and 0 <= end < size:
            add("[i] content ends at %d of %d bytes; the last %s of the file are"
                % (end, size, _fmt_bytes(gap)))
            add("    zeros. That is a normal tail, not a missing image.")
        elif end == 0:
            add("[!] content ends at 0 of %d bytes: every byte read is zero, so"
                % size)
            add("    nothing was imaged, or the file was pre-allocated and never")
            add("    written.")
        elif content.get("window_all_zero"):
            add("[!] the last %s read as zeros (checked back to +%s): the tail"
                % (_fmt_bytes(scanned), _fmt_bytes(max(0, size - scanned))))
            add("    is hole, so the image was truncated or pre-allocated. The")
            add("    exact content end is below that point; do not use the file")
            add("    size for rates.")
        if end == 0 or content.get("window_all_zero") or gap:
            add("")

    if scan:
        for line in _wrap_list("Scan targets: ", [
                "partition %d (%s, %s)" % (p["index"], _fmt_bytes(p["bytes"]),
                                           KIND_LABEL.get(p["kind"], p["kind"]))
                for p in scan]):
            add(line)
    skipped_real = [p for p in skipped if p.get("kind") in SKIP_KINDS]
    if skipped_real:
        for line in _wrap_list("Not scanned: ", [
                "partition %d (%s, %s)" % (p["index"], _fmt_bytes(p["bytes"]),
                                           KIND_LABEL.get(p["kind"], p["kind"]))
                for p in skipped_real]):
            add(line)

    if scan and blocked:
        add("What this run can still do: whatever survives in plaintext on the")
        add("partition(s) above. Nothing at all from the encrypted one(s): their")
        add("MFT, $Recycle.Bin, $UsnJrnl and every file are inside the ciphertext.")
    elif scan:
        add("What this run can still do: every file whose bytes survive on those")
        add("partitions - metadata first ($MFT, $Recycle.Bin, $UsnJrnl, VSS),")
        add("then carving for the remains.")
    elif blocked:
        add("What this run can still do: nothing. Every scannable byte in this")
        add("source is ciphertext, and a scan of it would return false positives")
        add("only.")
    else:
        add("What this run can still do: nothing. No partition here holds a file")
        add("system, so there is nothing to read and nothing to carve.")

    return "\n".join(out)


def _wrap_list(prefix, items, width=78, indent="    "):
    """'Prefix: a, b, c' wrapped at `width`, continuation lines indented."""
    lines = []
    line = prefix
    for position, item in enumerate(items):
        token = item if position == 0 else ", " + item
        if position and len(line) + len(token) > width:
            lines.append(line + ",")
            line = indent + item
        else:
            line += token
    lines.append(line)
    return lines


def _fve_block(part):
    samples = part.get("entropy") or []
    evidence = "OEM id '-FVE-FS-' at VBR offset 3"
    if samples:
        span = part.get("sample_bytes")
        where = ""
        if span and part.get("bytes") and span < part["bytes"]:
            where = " in the first %s" % _fmt_bytes(span)
        elif span:
            where = " across the volume"
        evidence += "; entropy %.2f-%.2f bits/byte over %d sample(s)%s" % (
            min(samples), max(samples), len(samples), where)
    name = ', "%s"' % part["name"] if part.get("name") else ""
    lines = ["[FVE] partition %d (%s%s) is BitLocker-encrypted."
             % (part["index"], _fmt_bytes(part["bytes"]), name)]
    lines.extend(_wrap_text("Evidence: %s." % evidence, width=72, indent="      "))
    lines.extend(_wrap_text(
        "Carving it would produce false positives only: ciphertext has no "
        "headers, no file-system structure and no deleted records, so every hit "
        "would be a signature landing on random bytes at the rate chance "
        "predicts.", width=72, indent="      "))
    lines.extend(_wrap_text(
        "What you need: the 48-digit recovery password, or a .bek key file, or "
        "the key in your Microsoft account. This tool never guesses a key and "
        "never attempts decryption.", width=72, indent="      "))
    lines.append("")
    return "\n".join(lines)


def _entropy_block(part, samples):
    lines = _wrap_text(
        "[?] partition %d (%s) has no recognisable OEM id, but reads %.2f-%.2f "
        "bits/byte over %d sample(s)."
        % (part["index"], _fmt_bytes(part["bytes"]), min(samples),
           max(samples), len(samples)), width=78, indent="    ",
        first_indent="")
    lines.extend(_wrap_text(
        "High entropy is not proof of encryption: a JPEG or a ZIP measures the "
        "same. It stays a scan target and is reported as unknown, not called "
        "encrypted.", width=76, indent="    "))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command line (read-only)
# ---------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    probe = probe_source(argv[1])
    print(blue(probe["message"]))
    return 0 if probe["scannable"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
