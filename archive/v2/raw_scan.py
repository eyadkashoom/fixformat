#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
raw_scan.py - raw sector signature scanner (READ-ONLY, fully offline).

What it does
------------
1. Lists every disk in the machine with its exact size and sector count, then
   ASKS which target to scan (physical disk / volume / image file).
2. Reads the target raw, in large blocks, and searches byte-by-byte inside the
   memory for the configured signatures, reporting the EXACT byte offset and
   the sector number of every hit (signatures crossing block boundaries are
   never missed - an overlap window is carried between blocks).
3. Every hit is printed, appended immediately to search_results.txt, and
   carved out to a data window so the surrounding content can be read back.
4. When the scan ends, a report shows every recovered item (name + size +
   totals) and the candidate file names found inside the carved data, and then
   ASKS where the recovered data should be saved - nothing is assumed about
   the destination.

Signatures
----------
* MAGIC_SIGNATURES  - magic bytes of binary formats (PDF, PNG, ZIP, ...)
* TARGET_SIGNATURES - plain-text keywords and file-extension strings for text
  formats that have no magic bytes (.md, .py, .json, .yaml, .env, ...)

Text signatures are searched in several encodings (UTF-8, UTF-16LE, UTF-16BE),
so non-ASCII content (e.g. Arabic words) is found whatever encoding the file
was saved with. Case sensitivity is controlled by CASE_SENSITIVE.

Environment
-----------
Mini Windows 10 (DLC Boot) from a USB flash drive. The bundled embedded
Python (python\\python.exe) runs this with no installation. Only the Python
standard library is used - no pip, no network, no internet connection needed.

This tool NEVER writes to the scanned device.
"""

import ctypes
import os
import re
import shutil
import sys
import time
from datetime import datetime

IS_WINDOWS = os.name == "nt"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECTOR_SIZE = 512          # used for the sector counters
DEVICE_ALIGNMENT = 4096    # Windows raw-device reads must be aligned to this
GB = 1024.0 ** 3
VERSION = "2.0"

# Every text signature is searched in all of these encodings.
TEXT_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")

# Candidate file names carved out of recovered data (report section).
NAME_RE = re.compile(
    r"[A-Za-z0-9_\-\.]{2,80}\.(?:md|py|json|ya?ml|env|txt|log|toml|cfg|ini|bat|ps1|sql|csv|sh)\b")


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------

def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return "%d B" % int(n) if unit == "B" else "%.2f %s" % (n, unit)
        n /= 1024.0


def fmt_int(n):
    return format(int(n), ",")


def fmt_hms(seconds):
    seconds = int(max(0, seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def parse_hex_pattern(text):
    """Accept '25 50 44 46', '25504446', '\\x25\\x50', '0x25 0x50' forms."""
    cleaned = (
        text.replace(" ", "").replace("\t", "").replace("\n", "").replace("\r", "")
            .replace(",", "").replace(":", "")
            .replace("0x", "").replace("0X", "")
            .replace("\\x", "").replace("\\X", "")
    )
    if len(cleaned) == 0 or len(cleaned) % 2 != 0:
        raise ValueError("Invalid hex pattern (need an even number of hex digits): %r" % (text,))
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError("Invalid hex pattern: %r" % (text,))


def _ext_from_label(label):
    """'Python Code (.py)' -> 'py';  'Extension .md' -> 'md';  else 'bin'."""
    m = re.search(r"\(\.([A-Za-z0-9]+)\)", label) or re.search(r"\.([A-Za-z0-9]+)$", label)
    return m.group(1).lower() if m else "bin"


def expand_text_signature(label, text, ext):
    """One text signature -> one entry per encoding (label gains [encoding])."""
    if isinstance(text, bytes):
        return [(label, text, ext)]
    if not text:
        raise ValueError("Empty text signature: %r" % (label,))
    entries = []
    seen = set()
    for enc in TEXT_ENCODINGS:
        try:
            raw = text.encode(enc)
        except UnicodeEncodeError:
            continue
        if raw and raw not in seen:
            seen.add(raw)
            entries.append(("%s [%s]" % (label, enc), raw, ext))
    if not entries:
        raise ValueError("Text signature cannot be encoded: %r" % (label,))
    return entries


def build_patterns(case_sensitive=None):
    """Config dicts -> (label, pattern_bytes, ext, case_insensitive) tuples."""
    if case_sensitive is None:
        case_sensitive = CASE_SENSITIVE
    patterns = []
    for label, value in MAGIC_SIGNATURES.items():
        pat = value if isinstance(value, bytes) else parse_hex_pattern(value)
        patterns.append((label, pat, _ext_from_label(label), False))
    for label, value in TARGET_SIGNATURES.items():
        ext = _ext_from_label(label)
        ci = not case_sensitive
        for lab, pat, e in expand_text_signature(label, value, ext):
            patterns.append((lab, pat, e, ci))
    if not patterns:
        raise ValueError("No signatures configured - edit MAGIC_SIGNATURES / TARGET_SIGNATURES.")
    return patterns


# ----------------------------------------------------------------------
# interactive choices (v2)
# ----------------------------------------------------------------------

def available_extensions(patterns):
    """{'md': [labels...], 'py': [...], ...}"""
    exts = {}
    for entry in patterns:
        exts.setdefault(entry[2], []).append(entry[0])
    return exts


def filter_patterns_by_extension(patterns, extensions):
    """Keep only patterns whose extension is in `extensions` (None/empty = keep all)."""
    if not extensions:
        return list(patterns)
    return [entry for entry in patterns if entry[2] in extensions]


def ask_extensions(patterns):
    """Ask which file extensions to search for. Returns a set like {'md','py'}."""
    exts = available_extensions(patterns)
    ordered = sorted(exts.keys())
    print("Available file extensions (with the number of signatures each one uses):")
    for i, e in enumerate(ordered, 1):
        print("  %2d) .%-8s %2d signature(s)" % (i, e, len(exts[e])))
    print()
    print("Type the numbers you want separated by commas (example: 1,3,5),")
    print("or type 'all' to search for every extension listed above.")
    while True:
        try:
            answer = input("Your choice [all]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "all"
        if not answer or answer.lower() in ("all", "a", "*"):
            return set(ordered)
        picked = set()
        bad = False
        for token in answer.replace(" ", "").split(","):
            if not token:
                continue
            if not token.isdigit() or not (1 <= int(token) <= len(ordered)):
                bad = True
                break
            picked.add(ordered[int(token) - 1])
        if bad or not picked:
            print("Invalid input. Use numbers from the list (e.g. 1,3,5) or 'all'.")
            continue
        return picked


def ask_case_sensitive():
    """Ask Y/N whether the search must respect letter case. Returns True/False."""
    print("Case sensitivity decides whether uppercase and lowercase letters must")
    print("match exactly, or are treated as the same letter.")
    print("  Y = case-sensitive, exact match (recommended, fewer false hits)")
    print("      example: API_KEY= matches 'API_KEY=' but not 'api_key='")
    print("  N = case-insensitive (finds more, but also more false hits)")
    print("      example: API_KEY= also matches 'api_key=' and 'Api_Key='")
    while True:
        try:
            answer = input("Your choice [Y]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "y"
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer Y or N.")


def confirm_start():
    """Y/N confirmation before the scan starts. Returns True to start."""
    print("Do you want to start the scan now?")
    print("  Y = yes, start scanning")
    print("  N = no, stop here (nothing is scanned or changed)")
    while True:
        try:
            answer = input("Your choice [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer Y or N.")


# ----------------------------------------------------------------------
# device access
# ----------------------------------------------------------------------

def read_exact(f, length):
    """Read exactly `length` bytes or fewer at EOF (handles partial reads)."""
    buf = bytearray()
    while len(buf) < length:
        piece = f.read(length - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


def read_window(f, start, end):
    """Read bytes [start, end) respecting raw-device alignment rules."""
    if end <= start:
        return b""
    a_start = (start // DEVICE_ALIGNMENT) * DEVICE_ALIGNMENT
    a_end = ((end + DEVICE_ALIGNMENT - 1) // DEVICE_ALIGNMENT) * DEVICE_ALIGNMENT
    saved = f.tell()
    try:
        f.seek(a_start)
        data = read_exact(f, a_end - a_start)
    finally:
        f.seek(saved)
    s = start - a_start
    return data[s:min(s + (end - start), len(data))]


def get_device_size(f):
    """Total size in bytes of a device/file; 0 when unknown."""
    if IS_WINDOWS:
        try:
            import msvcrt
            from ctypes import wintypes

            class GET_LENGTH_INFORMATION(ctypes.Structure):
                _fields_ = [("Length", ctypes.c_longlong)]

            kernel32 = ctypes.windll.kernel32
            handle = msvcrt.get_osfhandle(f.fileno())
            IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
            info = GET_LENGTH_INFORMATION()
            ret = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                wintypes.HANDLE(handle),
                IOCTL_DISK_GET_LENGTH_INFO,
                None, 0,
                ctypes.byref(info), ctypes.sizeof(info),
                ctypes.byref(ret), None,
            )
            if ok:
                return int(info.Length)
        except Exception:
            pass
    try:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        return size
    except OSError:
        return 0


def list_physical_drives(max_index=16):
    """[(index, size_bytes), ...] for every accessible physical disk."""
    drives = []
    for i in range(max_index):
        target = "\\\\.\\PhysicalDrive%d" % i
        try:
            f = open(target, "rb", buffering=0)
        except (PermissionError, OSError):
            continue
        try:
            drives.append((i, get_device_size(f)))
        finally:
            f.close()
    return drives


def list_logical_drives():
    """[(root, total_bytes, free_bytes), ...] for every mounted volume."""
    out = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if not os.path.exists(root):
            continue
        try:
            usage = shutil.disk_usage(root)
            out.append((root, usage.total, usage.free))
        except OSError:
            out.append((root, 0, 0))
    return out


# ----------------------------------------------------------------------
# signature scanning
# ----------------------------------------------------------------------

def _reportable(last_hit, label, abs_off, min_gap):
    """MIN_GAP filter: True = report this hit, False = too close to the previous one."""
    prev = last_hit.get(label)
    if min_gap and prev is not None and (abs_off - prev) < min_gap:
        return False
    last_hit[label] = abs_off
    return True


def carve_window(f, abs_off, plen, total_size, carve_dir, label, ext, before, after):
    """Dump a data window around a hit into carve_dir. Returns the path or ''."""
    try:
        start = max(0, abs_off - before)
        end = abs_off + plen + after
        if total_size:
            end = min(end, total_size)
        data = read_window(f, start, end)
        if not data:
            return ""
        os.makedirs(carve_dir, exist_ok=True)
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in label)
        fname = "%s_0x%016X.%s" % (safe, abs_off, ext or "bin")
        path = os.path.join(carve_dir, fname)
        with open(path, "wb") as out:
            out.write(data)
        return path
    except OSError as exc:
        return "(carve failed: %s)" % exc


def scan(target, patterns, chunk_size, results_path, carve_dir=None,
         carve_before=4096, carve_after=65536, show_progress=True,
         min_gap=0, carve_limit=0):
    """
    Scan `target` for `patterns` = (label, bytes, ext, case_insensitive) tuples.
    Returns a summary dict (see below).
    """
    if not patterns:
        raise ValueError("patterns must not be empty")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    is_device = IS_WINDOWS and target.startswith("\\\\.\\")
    if is_device and chunk_size % DEVICE_ALIGNMENT != 0:
        raise ValueError("For raw devices chunk_size must be a multiple of %d" % DEVICE_ALIGNMENT)

    pat_list = []
    for entry in patterns:
        label, pat, ext = entry[0], entry[1], entry[2]
        ci = entry[3] if len(entry) > 3 else False
        pat_list.append((label, pat, ext, ci, pat.lower() if ci else pat))

    max_len = max(len(p[1]) for p in pat_list)
    carry_len = max_len - 1
    any_ci = any(p[3] for p in pat_list)

    hits = []
    counts = dict((p[0], 0) for p in pat_list)
    carved_counts = {}
    scanned = 0
    interrupted = False
    started = time.time()

    def now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log_file = open(results_path, "a", encoding="utf-8", errors="replace")

    def log(line):
        log_file.write(line + "\n")
        log_file.flush()

    log("=== SCAN START %s ===" % now())
    log("target     : %s" % target)
    log("chunk size : %d bytes" % chunk_size)
    log("patterns   : %d" % len(pat_list))
    log("min gap    : %d bytes" % min_gap)
    log("case       : %s" % ("sensitive" if not any_ci else "insensitive for text signatures"))

    # may raise PermissionError / OSError - handled by main()
    f = open(target, "rb", buffering=0)
    try:
        total_size = get_device_size(f)
        log("target size: %s" % ("unknown" if not total_size else "%d bytes (%s)"
                                 % (total_size, fmt_bytes(total_size))))

        carry = b""
        next_off = 0
        last_hit = {}
        last_progress = [0.0]

        def update_progress(force=False):
            if not show_progress:
                return
            t = time.time()
            if not force and (t - last_progress[0]) < 1.0:
                return
            last_progress[0] = t
            elapsed = t - started
            rate = scanned / elapsed if elapsed > 0 else 0
            if total_size:
                pct = 100.0 * scanned / total_size
                remaining = max(0, total_size - scanned)
                eta = remaining / rate if rate > 0 else 0
                line = ("\rScanned %s / %s (%.2f%%) | sectors %s / %s | remaining %s | ETA %s | hits %d"
                        % (fmt_bytes(scanned), fmt_bytes(total_size), pct,
                           fmt_int(scanned // SECTOR_SIZE), fmt_int(total_size // SECTOR_SIZE),
                           fmt_bytes(remaining), fmt_hms(eta), len(hits)))
            else:
                line = ("\rScanned %s | sectors %s | speed %s/s | hits %d"
                        % (fmt_bytes(scanned), fmt_int(scanned // SECTOR_SIZE),
                           fmt_bytes(rate), len(hits)))
            sys.stdout.write(line)
            sys.stdout.flush()

        try:
            while True:
                chunk = read_exact(f, chunk_size)
                if not chunk:
                    break

                combined = carry + chunk
                combined_lower = combined.lower() if any_ci else None
                base = next_off - len(carry)
                carry_floor = len(carry)

                for label, pat, ext, ci, pat_low in pat_list:
                    needle = pat_low if ci else pat
                    hay = combined_lower if ci else combined
                    plen = len(pat)
                    pos = hay.find(needle)
                    while pos != -1:
                        if pos + plen > carry_floor and _reportable(last_hit, label, base + pos, min_gap):
                            abs_off = base + pos
                            sector = abs_off // SECTOR_SIZE
                            carved_path = ""
                            carved_ok = (carve_dir and
                                         (not carve_limit or carved_counts.get(label, 0) < carve_limit))
                            if carved_ok:
                                carved_path = carve_window(
                                    f, abs_off, plen, total_size, carve_dir,
                                    label, ext, carve_before, carve_after)
                                if carved_path and not carved_path.startswith("(carve failed"):
                                    carved_counts[label] = carved_counts.get(label, 0) + 1
                            elif carve_dir:
                                carved_path = "(carve limit reached)"
                            hits.append({"label": label, "offset": abs_off,
                                         "sector": sector, "carved": carved_path})
                            counts[label] += 1
                            line = ("[%s] HIT | %s | offset=0x%016X (dec %d) | sector=%d | carved=%s"
                                    % (now(), label, abs_off, abs_off, sector, carved_path or "-"))
                            log(line)
                            print("\n" + line)
                        pos = hay.find(needle, pos + 1)

                scanned += len(chunk)
                next_off += len(chunk)
                update_progress()

                if len(chunk) < chunk_size:
                    break
                carry = combined[-carry_len:] if carry_len else b""
        except KeyboardInterrupt:
            interrupted = True

        update_progress(force=True)
        if show_progress:
            print()

        elapsed = time.time() - started
        log("=== SCAN END %s === status=%s" % (now(), "INTERRUPTED" if interrupted else "COMPLETE"))
        log("bytes scanned: %d" % scanned)
        log("sectors      : %d" % (scanned // SECTOR_SIZE))
        log("elapsed      : %s" % fmt_hms(elapsed))
        log("hits total   : %d" % len(hits))
        for entry in pat_list:
            log("  %-40s %d" % (entry[0], counts[entry[0]]))

        return {
            "target": target,
            "total_size": total_size,
            "bytes_scanned": scanned,
            "elapsed": elapsed,
            "hits": hits,
            "counts": counts,
            "interrupted": interrupted,
            "results_path": results_path,
            "carve_dir": carve_dir,
        }
    finally:
        f.close()
        log_file.close()


# ----------------------------------------------------------------------
# report + save-to destination
# ----------------------------------------------------------------------

def extract_candidate_names(carve_dir, limit=400):
    """File-name-like strings found inside the carved windows: [(name, count)]."""
    found = {}
    if not carve_dir or not os.path.isdir(carve_dir):
        return []
    for fname in sorted(os.listdir(carve_dir)):
        path = os.path.join(carve_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read(256 * 1024)
        except OSError:
            continue
        for text in (data.decode("latin-1", errors="ignore"),
                     data.decode("utf-16-le", errors="ignore"),
                     data.decode("utf-16-be", errors="ignore")):
            for m in NAME_RE.finditer(text):
                name = m.group(0)
                found[name] = found.get(name, 0) + 1
        if len(found) > limit * 4:
            break
    return sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def carved_stats(carve_dir):
    """(file_count, total_bytes, [(filename, size), ...])"""
    items = []
    total = 0
    if carve_dir and os.path.isdir(carve_dir):
        for fname in sorted(os.listdir(carve_dir)):
            path = os.path.join(carve_dir, fname)
            if os.path.isfile(path):
                size = os.path.getsize(path)
                total += size
                items.append((fname, size))
    return len(items), total, items


def write_report(summary, names, report_path):
    count, total, items = carved_stats(summary["carve_dir"])
    lines = []
    lines.append("=== RECOVERY REPORT ===")
    lines.append("date          : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("target        : %s" % summary["target"])
    if summary["total_size"]:
        lines.append("target size   : %d bytes (%s)" % (summary["total_size"], fmt_bytes(summary["total_size"])))
    lines.append("bytes scanned : %d" % summary["bytes_scanned"])
    lines.append("sectors       : %d" % (summary["bytes_scanned"] // SECTOR_SIZE))
    lines.append("elapsed       : %s" % fmt_hms(summary["elapsed"]))
    lines.append("status        : %s" % ("INTERRUPTED" if summary["interrupted"] else "COMPLETE"))
    lines.append("hits total    : %d" % len(summary["hits"]))
    lines.append("")
    lines.append("--- hits per pattern ---")
    for label in summary["counts"]:
        lines.append("  %-40s %d" % (label, summary["counts"][label]))
    lines.append("")
    lines.append("--- recovered items (carved data windows) ---")
    lines.append("  %-12s %-40s %-18s %-10s %s" % ("offset", "pattern", "sector", "size", "file"))
    for rec in summary["hits"]:
        path = rec.get("carved") or ""
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        lines.append("  0x%010X %-40s %-18s %-10s %s"
                     % (rec["offset"], rec["label"], fmt_int(rec["sector"]),
                        fmt_int(size), os.path.basename(path) or "-"))
    lines.append("")
    lines.append("recovered files : %d" % count)
    lines.append("recovered total : %d bytes (%s)" % (total, fmt_bytes(total)))
    lines.append("")
    lines.append("--- candidate file names found in recovered data ---")
    if names:
        for name, cnt in names:
            lines.append("  %-60s x%d" % (name, cnt))
    else:
        lines.append("  (none)")
    with open(report_path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    return count, total


def print_report(summary, names):
    count, total, items = carved_stats(summary["carve_dir"])
    print()
    print("=== SCAN SUMMARY ===")
    print("target        : %s" % summary["target"])
    if summary["total_size"]:
        print("target size   : %s (%d bytes, %s sectors)"
              % (fmt_bytes(summary["total_size"]), summary["total_size"],
                 fmt_int(summary["total_size"] // SECTOR_SIZE)))
    print("bytes scanned : %s (%d bytes)" % (fmt_bytes(summary["bytes_scanned"]), summary["bytes_scanned"]))
    print("sectors       : %s" % fmt_int(summary["bytes_scanned"] // SECTOR_SIZE))
    print("elapsed       : %s" % fmt_hms(summary["elapsed"]))
    print("status        : %s" % ("INTERRUPTED (partial results kept)" if summary["interrupted"] else "COMPLETE"))
    print("hits total    : %d" % len(summary["hits"]))
    print("recovered     : %d files, %s (%d bytes)"
          % (count, fmt_bytes(total), total))
    print()
    print("--- hits per pattern ---")
    for label in summary["counts"]:
        print("  %-40s %d" % (label, summary["counts"][label]))
    print()
    print("--- recovered items (first 50, full list in recovery_report.txt) ---")
    for rec in summary["hits"][:50]:
        path = rec.get("carved") or ""
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        print("  0x%016X  sector %-12s %-8s %s"
              % (rec["offset"], fmt_int(rec["sector"]), fmt_bytes(size),
                 os.path.basename(path) or rec["label"]))
    if len(summary["hits"]) > 50:
        print("  ... %d more" % (len(summary["hits"]) - 50))
    print()
    print("--- candidate file names found in recovered data (first 30) ---")
    for name, cnt in names[:30]:
        print("  %-60s x%d" % (name, cnt))
    if len(names) > 30:
        print("  ... %d more (full list in recovery_report.txt)" % (len(names) - 30))
    if not names:
        print("  (none found)")


def ask_save_location(work_dir):
    """Ask where to keep the recovered data and move it there. Returns final dir."""
    print()
    print("Where should the recovered data be saved permanently?")
    print("Type a folder path on a DIFFERENT disk than the scanned one - writing")
    print("onto the damaged disk can overwrite the very data you want to recover.")
    print("Press Enter to keep everything in the current folder.")
    while True:
        try:
            answer = input("Destination folder [keep in %s]: " % work_dir).strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer:
            print("Recovered data kept in: %s" % work_dir)
            return work_dir
        dest_root = os.path.abspath(os.path.expanduser(answer.strip('"')))
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(dest_root, "recovered_" + stamp)
            os.makedirs(dest, exist_ok=True)
            return dest
        except OSError as exc:
            print("Cannot use %s: %s" % (dest_root, exc))


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def open_target(target):
    f = open(target, "rb", buffering=0)   # may raise PermissionError / OSError
    try:
        size = get_device_size(f)
    finally:
        f.close()
    return size


def ask_target():
    print("Physical disks in this machine:")
    drives = list_physical_drives()
    if drives:
        for idx, size in drives:
            print("  [%2d] \\\\.\\PhysicalDrive%-2d  %10.2f GB  %14s sectors  (%d bytes)"
                  % (idx, idx, size / GB, fmt_int(size // SECTOR_SIZE), size))
    else:
        print("  (none accessible - run as Administrator / SYSTEM)")
    print("Logical volumes:")
    for root, total, free in list_logical_drives():
        print("  %-6s %10.2f GB total, %10.2f GB free" % (root, total / GB, free / GB))
    print()
    while True:
        try:
            answer = input("Scan which target? drive number (e.g. 0), letter (e.g. D), or image file path: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, 0
        answer = answer.strip('"').strip("'")
        if not answer:
            continue
        if answer.isdigit():
            target = "\\\\.\\PhysicalDrive%s" % answer
        elif len(answer) == 1 and answer.isalpha():
            target = "\\\\.\\%s:" % answer.upper()
        elif len(answer) == 2 and answer[1] == ":" and answer[0].isalpha():
            target = "\\\\.\\%s:" % answer[0].upper()
        else:
            target = os.path.abspath(os.path.expanduser(answer))
            if not os.path.isfile(target):
                print("File not found: %s" % target)
                continue
        try:
            size = open_target(target)
        except PermissionError:
            print("Access denied opening %s - run as Administrator." % target)
            continue
        except OSError as exc:
            print("Cannot open %s: %s" % (target, exc))
            continue
        return target, size


def main():
    print("raw_scan.py v%s - raw sector signature scanner (read-only, offline)" % VERSION)
    print("script dir : %s" % SCRIPT_DIR)
    print()
    print("This tool searches a disk sector by sector for the byte signatures of")
    print("the file types you choose. It only READS the disk, never writes to it,")
    print("and needs no internet connection. Every step below asks what to do.")
    print()

    # ---------------- STEP 1 ----------------
    print("=" * 78)
    print("STEP 1 of 5 - CHOOSE THE DISK TO SCAN")
    print("=" * 78)
    print("The machine may contain several disks. Every disk is listed below with")
    print("its exact size and sector count so you can identify the right one.")
    print()
    if TARGET.strip():
        target = TARGET.strip()
        try:
            total = open_target(target)
        except PermissionError:
            print("ERROR: access denied opening %s - run as Administrator." % target)
            sys.exit(3)
        except OSError as exc:
            print("ERROR: %s" % exc)
            sys.exit(1)
    else:
        target, total = ask_target()
        if not target:
            print("No target selected. Nothing was scanned.")
            sys.exit(0)

    print()
    print("Selected target : %s" % target)
    if total:
        print("Size            : %s (%d bytes, %s sectors)"
              % (fmt_bytes(total), total, fmt_int(total // SECTOR_SIZE)))
    else:
        print("Size            : unknown (will scan until end of media)")

    # ---------------- STEP 2 ----------------
    print()
    print("=" * 78)
    print("STEP 2 of 5 - CHOOSE WHICH FILE TYPES TO SEARCH FOR")
    print("=" * 78)
    print("Search for one file extension or several. Every extension brings its")
    print("own signatures: magic bytes for binary formats, content keywords and")
    print("extension strings for text formats (.md, .py, .json ...).")
    print()
    try:
        exts = ask_extensions(build_patterns())
    except ValueError as exc:
        print("CONFIG ERROR: %s" % exc)
        sys.exit(2)

    # ---------------- STEP 3 ----------------
    print()
    print("=" * 78)
    print("STEP 3 of 5 - CHOOSE CASE SENSITIVITY")
    print("=" * 78)
    case_sensitive = ask_case_sensitive()
    patterns = filter_patterns_by_extension(build_patterns(case_sensitive), exts)

    # ---------------- STEP 4 ----------------
    print()
    print("=" * 78)
    print("STEP 4 of 5 - CONFIRM YOUR CHOICES")
    print("=" * 78)
    print("  target      : %s (%s)" % (target, fmt_bytes(total) if total else "size unknown"))
    print("  extensions  : %s" % ", ".join("." + e for e in sorted(exts)))
    print("  signatures  : %d patterns will be searched" % len(patterns))
    print("  case        : %s" % ("sensitive (exact match)" if case_sensitive else "insensitive (ignore case)"))
    print("  read block  : %s" % fmt_bytes(CHUNK_SIZE))
    print()
    print("Scanning is read-only: nothing on the disk is modified.")
    print("Recovered data is collected in : %s" % SCRIPT_DIR)
    print("After the scan you will be asked where to save it permanently.")
    print()
    if not confirm_start():
        print("Stopped. Nothing was scanned.")
        sys.exit(0)

    # ---------------- STEP 5 ----------------
    print()
    print("=" * 78)
    print("STEP 5 of 5 - SCANNING (this can take a long time on big disks)")
    print("=" * 78)
    print("The progress line shows: data scanned / total (percent) | sectors")
    print("scanned / total | data remaining | estimated time left | hits found.")
    print("Every hit is saved to search_results.txt the moment it is found.")
    print("Press Ctrl+C to stop early - everything found so far is kept.")
    print("-" * 78)

    results_path = os.path.join(SCRIPT_DIR, RESULTS_FILE)
    carve_dir = os.path.join(SCRIPT_DIR, CARVE_DIR) if ENABLE_CARVING else None

    try:
        summary = scan(target, patterns, CHUNK_SIZE, results_path, carve_dir,
                       CARVE_BEFORE, CARVE_AFTER, min_gap=MIN_GAP,
                       carve_limit=CARVE_LIMIT)
    except PermissionError:
        print("\nERROR: access denied.")
        print("Raw device reads require administrator / SYSTEM rights.")
        print("Re-open the command prompt as Administrator and retry.")
        sys.exit(3)
    except ValueError as exc:
        print("\nCONFIG ERROR: %s" % exc)
        sys.exit(2)
    except OSError as exc:
        print("\nERROR: %s" % exc)
        sys.exit(1)

    # ---------------- RESULTS ----------------
    print()
    print("=" * 78)
    print("RESULTS - RECOVERY REPORT")
    print("=" * 78)
    print("Below is everything that was found: each hit with its exact byte")
    print("offset and sector, the recovered data files and their sizes, the")
    print("totals, and the candidate file names inside the recovered data.")
    print()
    names = extract_candidate_names(carve_dir)
    report_path = os.path.join(SCRIPT_DIR, REPORT_FILE)
    count, total_bytes = write_report(summary, names, report_path)
    print_report(summary, names)
    print()
    print("results log     : %s" % results_path)
    print("recovery report : %s" % report_path)

    # ask WHERE to save, after the user has seen names and sizes
    final_dir = ask_save_location(SCRIPT_DIR)
    if final_dir != SCRIPT_DIR:
        try:
            if carve_dir and os.path.isdir(carve_dir):
                shutil.move(carve_dir, os.path.join(final_dir, "carved"))
            for src in (results_path, report_path):
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(final_dir, os.path.basename(src)))
            count, total_bytes, _ = carved_stats(os.path.join(final_dir, "carved"))
            print()
            print("SAVED TO: %s" % final_dir)
            print("  recovered files : %d" % count)
            print("  recovered total : %s (%d bytes)" % (fmt_bytes(total_bytes), total_bytes))
        except OSError as exc:
            print("Move failed: %s - files remain in %s" % (exc, SCRIPT_DIR))
    if not summary["hits"]:
        print()
        print("No matches found. Review TARGET_SIGNATURES / MAGIC_SIGNATURES")
        print("in raw_scan.py and run the scan again.")


# ======================================================================
# USER CONFIGURATION - edit the values below, then run the script.
# ======================================================================

# Leave TARGET empty to be asked interactively (recommended: the script lists
# every disk with its size and sector count, then asks which one to scan).
# Set it to skip the question, e.g. r"\\.\PhysicalDrive0" or r"\\.\D:".
TARGET = ""

# Read block size. Large blocks keep the scan fast; the byte-level search
# inside them stays exact (signatures crossing block boundaries are caught by
# an overlap window). For raw devices it must be a multiple of 4096
# (512 / 4096 / 8 MiB style sizes are all fine as long as they are).
CHUNK_SIZE = 8 * 1024 * 1024

# Ignore a new hit of the same pattern closer than this to the previous one
# (generic markers like "{" or "# " occur thousands of times in one region and
# one carved window already covers it). Set 0 to report every occurrence.
MIN_GAP = 1024 * 1024

# Case sensitivity for the text signatures in TARGET_SIGNATURES.
# True  = "API_KEY=" matches "API_KEY=" only (max precision)
# False = "API_KEY=" also matches "api_key=" (max recall)
CASE_SENSITIVE = True

# Logs (created next to the script during the scan, moved on request later).
RESULTS_FILE = "search_results.txt"
REPORT_FILE = "recovery_report.txt"

# Carve a readable data window around every hit.
ENABLE_CARVING = True
CARVE_DIR = "recovered"
CARVE_BEFORE = 4096            # bytes kept before the hit
CARVE_AFTER = 64 * 1024        # bytes kept after the hit
CARVE_LIMIT = 100              # max carved files per pattern (0 = unlimited)

# Magic-byte signatures: label -> hex bytes (or bytes). Binary formats with a
# fixed, well-known header. Add more whenever you need them.
MAGIC_SIGNATURES = {
    "PDF document (.pdf)": "25 50 44 46 2D",
    "PNG image (.png)": "89 50 4E 47 0D 0A 1A 0A",
    "JPEG image (.jpg)": "FF D8 FF",
    "GIF image (.gif)": "47 49 46 38",
    "ZIP archive (.zip)": "50 4B 03 04",
    "RAR archive (.rar)": "52 61 72 21 1A 07 00",
    "7z archive (.7z)": "37 7A BC AF 27 1C",
    "GZIP file (.gz)": "1F 8B 08",
    "SQLite database (.db)": "53 51 4C 69 74 65 20 66 6F 72 6D 61 74 20 33 00",
}

# Text signatures: label -> exact text (str, searched in UTF-8 + UTF-16LE +
# UTF-16BE, or raw bytes used as-is). Text formats have no magic bytes, so
# they are found by keywords and file-extension strings; extension strings
# also appear inside NTFS MFT records where file names live, so a ".md" hit
# there usually reveals the real file name in the carved window.
# NOTE: very generic markers ("{", "# ") match huge amounts of unrelated data.
# Longer, more specific markers give better precision.
TARGET_SIGNATURES = {
    # --- plain-text keywords (examples, not exhaustive) ---
    "Python Code (.py)": "import ",
    "Python shebang (.py)": "#!/usr/bin/env python",
    "Python def (.py)": "def ",
    "Environment (.env)": "API_KEY=",
    "YAML Config (.yaml)": "agent:",
    "JSON Data (.json)": '":',
    "Markdown heading LF (.md)": "\n# ",
    "Markdown heading CRLF (.md)": "\r\n# ",
    # --- file extension strings ---
    "Extension .md": ".md",
    "Extension .py": ".py",
    "Extension .json": ".json",
    "Extension .yaml": ".yaml",
    "Extension .yml": ".yml",
    "Extension .env": ".env",
    "Extension .txt": ".txt",
    "Extension .log": ".log",
    "Extension .toml": ".toml",
    "Extension .cfg": ".cfg",
    "Extension .ini": ".ini",
    "Extension .bat": ".bat",
    "Extension .ps1": ".ps1",
    "Extension .sql": ".sql",
    "Extension .csv": ".csv",
    "Extension .codex": ".codex",
    "Path marker /codex (.codex)": "/codex",
    "Folder marker Codex (.codex)": "Codex",
    "Hermes marker (.txt)": "Hermes",
    # --- project file names ---
    "Name PROJECT_SPEC (.md)": "PROJECT_SPEC",
    "Name DECISIONS (.md)": "DECISIONS",
    "Name WORK_PLAN (.md)": "WORK_PLAN",
    "Name SESSION_STATE (.md)": "SESSION_STATE",
    "Name ACTIVITY_LOG (.md)": "ACTIVITY_LOG",
    "Name SALVAGED_TECHNIQUES (.md)": "SALVAGED_TECHNIQUES",
    "Name SOURCE_LAYOUT (.md)": "SOURCE_LAYOUT",
    "Name smart-file-organizer (.md)": "smart-file-organizer",
    "Name AGENT_OPERATING_RULES (.md)": "AGENT_OPERATING_RULES",
}

# ======================================================================

if __name__ == "__main__":
    main()
