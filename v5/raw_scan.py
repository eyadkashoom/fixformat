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
import hashlib
import json
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
VERSION = "5.0"

# Every text signature is searched in all of these encodings.
# Windows stores text as UTF-16-LE; UTF-16-BE is opt-in, because every BE
# pattern starts with a 0x00 byte - the most common byte on a formatted disk -
# and the benchmark showed those patterns dominating the match time.
SEARCH_UTF16_BE = False
TEXT_ENCODINGS = (("utf-8", "utf-16-le", "utf-16-be") if SEARCH_UTF16_BE
                  else ("utf-8", "utf-16-le"))

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


_ZERO_BLOCKS = {}


def zero_block(n):
    """A cached all-zero bytes object of length n (C-level comparison target)."""
    block = _ZERO_BLOCKS.get(n)
    if block is None:
        block = b"\x00" * n
        _ZERO_BLOCKS[n] = block
    return block


# Measured and rejected (V5_PLAN.md 13.2): a single regex alternation over all
# needles and an anchor-byte-class sieve both lost to plain bytes.find() per
# pattern on zero-heavy blocks (5x and 4x slower). The winning strategy is the
# per-pattern find loop in scan(), combined with the zero-block skip above.


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
# numbered path pickers (sections 11 and 12 of V5_PLAN.md)
# One implementation serves every flow that needs a path: the image
# source, the imaging destination, the recovered-data root, the
# organized output root and the SMB path.
# ----------------------------------------------------------------------

IMAGE_EXTS = (".img", ".raw", ".dd", ".001", ".img.gz", ".img.xz", ".img.zst")
LAST_PATHS_FILE = "last_paths.json"
PICKER_PAGE = 15


def _last_paths_path():
    return os.path.join(SCRIPT_DIR, LAST_PATHS_FILE)


def load_last_paths():
    """{'image': dir, 'destination': dir, ...} - empty dict when unreadable."""
    try:
        with open(_last_paths_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_path(key, path):
    """Best-effort: remember the folder behind `path` under `key`."""
    if not path:
        return
    folder = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
    db = load_last_paths()
    db[key] = folder
    try:
        with open(_last_paths_path(), "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2)
    except OSError:
        pass


def classify_image_name(name):
    """(extension, is_partial) for an image-like name, else (None, False)."""
    low = name.lower()
    partial = low.endswith(".partial")
    if partial:
        low = low[:-len(".partial")]
    for ext in IMAGE_EXTS:
        if low.endswith(ext):
            return ext, partial
    return None, False


def _fmt_mtime(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "?"


def _shorten(text, width):
    if len(text) <= width:
        return text
    return "..." + text[-(width - 3):]


def _ask(prompt):
    """input() that survives EOF and Ctrl+C. Returns (answer, ok)."""
    try:
        return input(prompt), True
    except (EOFError, KeyboardInterrupt):
        print()
        return "", False


def _ask_manual_file(prompt):
    """Ask for an existing file path. Returns an absolute path or None."""
    while True:
        answer, ok = _ask(prompt)
        if not ok:
            return None
        answer = answer.strip().strip('"').strip("'")
        if not answer:
            return None
        path = os.path.abspath(os.path.expanduser(answer))
        if os.path.isfile(path):
            return path
        print("File not found: %s" % path)


def list_image_candidates(roots=None, depth=2):
    """
    [{path, size, mtime, partial}, ...] for every disk image found under `roots`.
    Default roots: script dir, current dir, last used dir, every volume root.
    Depth stays shallow on purpose so a 500 GB volume is never walked whole.
    """
    if roots is None:
        roots = []
        for candidate in (SCRIPT_DIR, os.getcwd(), load_last_paths().get("image")):
            if candidate and os.path.isdir(candidate):
                roots.append(candidate)
        roots.extend([root for root, _total, _free in list_logical_drives()])
    skip_dirs = ("windows", "system volume information", "recycler",
                 "$recycle.bin", "program files", "program files (x86)",
                 "programdata", "appdata")
    found = []
    seen = set()
    for root in roots:
        try:
            root = os.path.abspath(root)
        except OSError:
            continue
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for cur, dirs, files in os.walk(root, onerror=lambda _e: None):
            if cur.rstrip("\\/").count(os.sep) - base_depth >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs
                       if not d.startswith("$") and d.lower() not in skip_dirs]
            for name in files:
                ext, partial = classify_image_name(name)
                if not ext:
                    continue
                path = os.path.join(cur, name)
                key = path.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                found.append({"path": path, "size": st.st_size,
                              "mtime": st.st_mtime, "partial": partial})
    found.sort(key=lambda e: (e["partial"], -e["mtime"]))
    return found


def _volume_entries():
    """[(root, total, free, fs), ...] for every mounted volume."""
    out = []
    for root, total, free in list_logical_drives():
        out.append((root, total, free, get_volume_fs(root)))
    return out


def _browse_folder(start, need=0):
    """
    Walk folders by number: [n] enter a subfolder, [1] up, [S] select the
    current folder, [M] make a folder, [Q] back. Returns a path or None.
    """
    cur = os.path.abspath(start)
    while True:
        try:
            subdirs = sorted(d for d in os.listdir(cur)
                             if not d.startswith("$")
                             and os.path.isdir(os.path.join(cur, d)))
        except OSError as exc:
            print("Cannot list %s: %s" % (cur, exc))
            return None
        try:
            free = shutil.disk_usage(cur).free
            free_txt = fmt_bytes(free)
        except OSError:
            free = 0
            free_txt = "?"
        print()
        print("  Current folder: %s   (%s free)" % (cur, free_txt))
        print("  " + "-" * 74)
        entries = []
        parent = os.path.dirname(cur.rstrip("\\/"))
        if parent and parent != cur.rstrip("\\/"):
            entries.append(("..", parent))
        for d in subdirs:
            entries.append((d, os.path.join(cur, d)))
        for i, (name, _full) in enumerate(entries, 1):
            print("  [%d] %s" % (i, name))
        if not entries:
            print("  (no subfolders)")
        print("  " + "-" * 74)
        print("  [S] Select this folder   [M] Make a new folder   [Q] Back")
        answer, ok = _ask("Your choice [s]: ")
        if not ok:
            return None
        answer = answer.strip()
        low = answer.lower()
        if low in ("q", "b"):
            return None
        if not answer or low == "s":
            if need and free and free < need:
                print("NOT ENOUGH FREE SPACE: need about %s, this folder has %s."
                      % (fmt_bytes(need), free_txt))
                continue
            return cur
        if low == "m":
            name, ok_new = _ask("New folder name: ")
            if not ok_new:
                continue
            name = name.strip().strip('"').strip("'")
            if not name:
                continue
            target = os.path.join(cur, name)
            try:
                os.makedirs(target, exist_ok=True)
                cur = target
            except OSError as exc:
                print("Cannot create %s: %s" % (target, exc))
            continue
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(entries):
                cur = entries[index][1]
                continue
        print("Please type one of the numbers shown.")


def pick_image():
    """Numbered picker for an existing image file. Returns (path, size) or (None, 0)."""
    print("Searching for disk images: script folder, current folder, last used")
    print("folder, and every volume root (2 levels deep) ...")
    candidates = list_image_candidates()
    page = 0
    while True:
        start = 0
        if candidates:
            pages = (len(candidates) + PICKER_PAGE - 1) // PICKER_PAGE
            page = max(0, min(page, pages - 1))
            start = page * PICKER_PAGE
            print()
            print("-" * 78)
            if pages > 1:
                print("Disk images found   (page %d of %d)" % (page + 1, pages))
            else:
                print("Disk images found")
            print("-" * 78)
            for i, ent in enumerate(candidates[start:start + PICKER_PAGE], 1):
                note = "   incomplete - not a full image" if ent["partial"] else ""
                print("  [%d] %-52s %10s  %s%s"
                      % (i, _shorten(ent["path"], 52), fmt_bytes(ent["size"]),
                         _fmt_mtime(ent["mtime"]), note))
            print("-" * 78)
        else:
            print()
            print("No image file found in the searched folders.")
        print("  [0] Type a path manually")
        nav = "  [R] Rescan"
        if len(candidates) > PICKER_PAGE:
            nav += "   [N] Next page   [P] Previous page"
        print(nav + "   [Q] Quit")
        answer, ok = _ask("Your choice [1]: ")
        if not ok:
            return None, 0
        answer = answer.strip()
        if not answer:
            answer = "1" if candidates else "q"
        low = answer.lower()
        if low == "q":
            return None, 0
        if low == "r":
            candidates = list_image_candidates()
            page = 0
            continue
        if low == "n":
            page += 1
            continue
        if low == "p":
            page -= 1
            continue
        if answer == "0":
            path = _ask_manual_file("Path of the disk image file to analyze (.img): ")
            if path:
                remember_path("image", path)
                return path, os.path.getsize(path)
            continue
        if answer.isdigit() and candidates:
            index = start + int(answer) - 1
            if 0 <= index < len(candidates):
                entry = candidates[index]
                if entry["partial"]:
                    print("WARNING: %s is marked incomplete (an interrupted copy)."
                          % entry["path"])
                    confirm, ok_partial = _ask("Use it anyway? [y/N]: ")
                    if not ok_partial or confirm.strip().lower() not in ("y", "yes"):
                        continue
                remember_path("image", entry["path"])
                print("Selected: %s" % entry["path"])
                return entry["path"], entry["size"]
        print("Please type one of the numbers shown.")


def pick_folder(purpose, need=0):
    """Choose an existing folder by number. Returns an absolute path or None."""
    while True:
        volumes = _volume_entries()
        remembered = load_last_paths().get("destination") or ""
        print()
        print("-" * 78)
        print("Where should %s be saved?" % purpose)
        print("-" * 78)
        for i, (root, total, free, fs) in enumerate(volumes, 1):
            note = ""
            if need and free and free < need:
                note = "   too small (needs %s)" % fmt_bytes(need)
            print("  [%d] %-12s %10s free of %10s  %-6s%s"
                  % (i, root, fmt_bytes(free), fmt_bytes(total), fs or "-", note))
        if remembered and os.path.isdir(remembered):
            print("  [L] %s   (last used)" % _shorten(remembered, 52))
        print("-" * 78)
        print("  [U] Network path (\\\\server\\share)")
        print("  [0] Type a path manually   [R] Rescan volumes   [Q] Cancel")
        answer, ok = _ask("Your choice [1]: ")
        if not ok:
            return None
        answer = answer.strip()
        if not answer:
            answer = "1"
        low = answer.lower()
        if low == "q":
            return None
        if low == "r":
            continue
        if low == "l" and remembered and os.path.isdir(remembered):
            chosen = _browse_folder(remembered, need)
            if chosen:
                remember_path("destination", chosen)
                return chosen
            continue
        if low == "u":
            path, ok_net = _ask("Network path (\\\\server\\share): ")
            if not ok_net:
                return None
            path = path.strip().strip('"').strip("'")
            if not path:
                continue
            if os.path.isdir(path):
                chosen = _browse_folder(path, need)
                if chosen:
                    remember_path("destination", chosen)
                    return chosen
            else:
                print("Not reachable: %s" % path)
            continue
        if answer == "0":
            path, ok_manual = _ask("Folder path: ")
            if not ok_manual:
                return None
            path = path.strip().strip('"').strip("'")
            if not path:
                continue
            path = os.path.abspath(os.path.expanduser(path))
            if os.path.isdir(path):
                remember_path("destination", path)
                return path
            print("Folder does not exist: %s" % path)
            continue
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(volumes):
                chosen = _browse_folder(volumes[index][0], need)
                if chosen:
                    remember_path("destination", chosen)
                    return chosen
                continue
        print("Please type one of the numbers shown.")


def pick_destination(source_size, suggested_name, purpose="the disk image"):
    """
    Full destination for a new image: pick a folder by number, append
    `suggested_name`, and enforce the free-space rule (102% of the source).
    Returns a full file path or None.
    """
    need = int(source_size * 1.02) if source_size else 0
    folder = pick_folder(purpose, need)
    if not folder:
        return None
    path = os.path.join(folder, suggested_name)
    if os.path.exists(path):
        answer, ok = _ask("%s already exists. Overwrite? [y/N]: " % path)
        if not ok or answer.strip().lower() not in ("y", "yes"):
            return None
    return path


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



# Bytes that are NOT printable text: deleted in one C-level translate() call to
# count printable bytes without a Python loop (see printable_ratio).
_NON_PRINTABLE_BYTES = bytes(b for b in range(256)
                             if not ((32 <= b < 127) or b in (9, 10, 13)))


def shannon_entropy(data):
    """Shannon entropy in bits/byte for `data` (0..8)."""
    if not data:
        return 0.0
    from collections import Counter
    counts = Counter(data)
    n = len(data)
    ent = 0.0
    for c in counts.values():
        p = c / n
        if p > 0:
            import math
            ent -= p * math.log2(p)
    return ent


def printable_ratio(data):
    """Fraction of bytes that are printable ASCII or common whitespace."""
    if not data:
        return 0.0
    # C-level count: a Python loop over a 1 MB window cost ~70 ms per file.
    return len(data.translate(None, _NON_PRINTABLE_BYTES)) / len(data)


def classify_window(data):
    """Return (label, entropy) for a carved window.

    TEXT (readable)        - high printable ratio
    BINARY (structured)    - low entropy with structure
    COMPRESSED/ENCRYPTED   - entropy > ~6.5
    ZERO (empty)           - all zeros
    """
    if not data:
        return ("ZERO (empty)", 0.0)
    if is_all_zeros(data):
        return ("ZERO (empty)", 0.0)
    ent = shannon_entropy(data)
    pr = printable_ratio(data)
    if ent > 6.5:
        return ("COMPRESSED/ENCRYPTED", ent)
    if pr >= 0.60:
        return ("TEXT (readable)", ent)
    return ("BINARY (structured)", ent)


def is_text_like_byte(b):
    """True if byte is plausible in a text file."""
    return (32 <= b < 127) or b in (9, 10, 13) or b >= 0x80


def smart_extend_text(f, start, initial_end, total_size, max_extra=1024*1024):
    """Extend a carved text window while data looks like readable text.

    Returns the new end offset (exclusive). Stops when a run of non-text bytes
    exceeds a small threshold or max_extra is reached.
    """
    end = initial_end
    if total_size:
        hard_limit = min(total_size, initial_end + max_extra)
    else:
        hard_limit = initial_end + max_extra
    # read in 4KB steps
    step = 4096
    bad_run = 0
    while end < hard_limit:
        to_read = min(step, hard_limit - end)
        if to_read <= 0:
            break
        try:
            chunk = read_window(f, end, end + to_read)
        except Exception:
            break
        if not chunk:
            break
        # count text-like bytes in this chunk
        textish = sum(1 for b in chunk if is_text_like_byte(b))
        if textish / len(chunk) < 0.70:
            # too much binary; stop, but allow a short tail
            bad_run += 1
            if bad_run >= 2:
                break
        else:
            bad_run = 0
        end += len(chunk)
        if len(chunk) < to_read:
            break
    return end


def carve_window(f, abs_off, plen, total_size, carve_dir, label, ext, before, after,
                 smart_text=False, triage=True):
    """Dump a data window around a hit into carve_dir. Returns the path or ''.

    v4: optional smart text extension + carved-window triage classification.
    Writes a .txt copy next to text windows and a .triage.txt sidecar.
    """
    try:
        start = max(0, abs_off - before)
        end = abs_off + plen + after
        if total_size:
            end = min(end, total_size)
        # smart text carving: extend while data looks like readable text
        if smart_text and ext in ("md", "py", "json", "yaml", "yml", "env", "txt", "log",
                                  "toml", "cfg", "ini", "csv", "sh", "bat", "ps1", "sql"):
            end = smart_extend_text(f, start, end, total_size)
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
        # triage classification
        if triage:
            cls, ent = classify_window(data)
            try:
                with open(path + ".triage.txt", "w", encoding="utf-8") as tf:
                    tf.write("classification : %s\n" % cls)
                    tf.write("entropy        : %.3f bits/byte\n" % ent)
                    tf.write("printable      : %.1f%%\n" % (printable_ratio(data) * 100))
                    tf.write("size           : %d bytes\n" % len(data))
                    tf.write("offset         : 0x%016X\n" % abs_off)
                    tf.write("sector         : %d\n" % (abs_off // SECTOR_SIZE))
            except OSError:
                pass
        # write a clean readable .txt copy for text windows
        if ext in ("md", "py", "json", "yaml", "yml", "env", "txt", "log",
                   "toml", "cfg", "ini", "csv", "sh", "bat", "ps1", "sql"):
            try:
                text = data.decode("utf-8", errors="replace")
                txt_path = os.path.join(carve_dir, "%s_0x%016X.txt" % (safe, abs_off))
                with open(txt_path, "w", encoding="utf-8", errors="replace") as tf:
                    tf.write(text)
            except OSError:
                pass
        return path
    except OSError as exc:
        return "(carve failed: %s)" % exc


class CachedReader(object):
    """
    A file-like view that serves read()/seek()/tell() from one forward block.

    carve_window() and smart_extend_text() ask for a few kilobytes at a time;
    without a cache each of those small reads is a seek plus a syscall (one hit
    of a text window cost ~256 of them). In phase 2 the hits arrive sorted by
    offset, so almost every request is served from the block already in memory.
    """

    def __init__(self, f, block_size=None):
        self._f = f
        self._block = block_size or CARVE_BLOCK_SIZE
        self._start = 0
        self._data = b""
        self._pos = 0

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = int(offset)
        elif whence == 1:
            self._pos += int(offset)
        else:
            raise ValueError("CachedReader does not support seek from the end")
        return self._pos

    def _read_range(self, start, end):
        if end <= start:
            return b""
        cached = self._data
        if cached and self._start <= start and end <= self._start + len(cached):
            offset = start - self._start
            return cached[offset:offset + (end - start)]
        aligned = (start // self._block) * self._block
        length = max(self._block, end - aligned)
        self._f.seek(aligned)
        self._data = read_exact(self._f, length)
        self._start = aligned
        offset = start - aligned
        return self._data[offset:offset + (end - start)]

    def read(self, size):
        if size is None or size < 0:
            raise ValueError("CachedReader needs an explicit read size")
        data = self._read_range(self._pos, self._pos + size)
        self._pos += len(data)
        return data


def carve_phase(f, hits, total_size, carve_dir, before, after, carve_limit,
                carved_counts, log, show_progress=True):
    """
    Phase 2 of the two-phase recovery (V5_PLAN.md 13.1).

    The hits were recorded by phase 1 without touching the source again. Here
    they are sorted by offset and their windows are carved through one
    forward-moving cache, so a thousand windows cost one sequential read per
    CARVE_BLOCK_SIZE instead of a back-seek each. Fills hit["carved"] and
    returns the number of files written.
    """
    if not hits:
        return 0
    reader = CachedReader(f)
    ordered = sorted(hits, key=lambda h: h["offset"])
    total = len(ordered)
    carved = 0
    last = [0.0]
    for done, hit in enumerate(ordered, 1):
        label = hit["label"]
        if carve_limit and carved_counts.get(label, 0) >= carve_limit:
            hit["carved"] = "(carve limit reached)"
            continue
        path = carve_window(reader, hit["offset"], hit.get("plen", 1), total_size,
                            carve_dir, label, hit.get("ext", ""), before, after,
                            smart_text=ENABLE_SMART_TEXT, triage=ENABLE_TRIAGE)
        hit["carved"] = path
        if path and not path.startswith("(carve failed"):
            carved_counts[label] = carved_counts.get(label, 0) + 1
            carved += 1
            log("CARVED | %s | offset=0x%016X | %s" % (label, hit["offset"], path))
        elif path:
            log("CARVE PROBLEM | %s | offset=0x%016X | %s"
                % (label, hit["offset"], path))
        if show_progress:
            t = time.time()
            if t - last[0] >= 0.5 or done == total:
                last[0] = t
                sys.stdout.write("\rCarving %d / %d hits (%d files)   "
                                 % (done, total, carved))
                sys.stdout.flush()
    if show_progress:
        print()
    return carved


def build_pat_list(patterns):
    """[(label, pattern, ext, case_insensitive, matched_bytes), ...]"""
    out = []
    for entry in patterns:
        label, pat, ext = entry[0], entry[1], entry[2]
        ci = entry[3] if len(entry) > 3 else False
        out.append((label, pat, ext, ci, pat.lower() if ci else pat))
    return out


def match_block(hay, hay_low, pat_list, base, carry_floor, last_hit, min_gap):
    """
    Every reportable hit inside one block, as (label, ext, plen, abs_offset).

    One bytes.find() pass per pattern is deliberate and measured: it beat a
    single regex alternation and an anchor-byte sieve by 4-5x (see
    V5_PLAN.md 13.2). Shared by the sequential and the parallel phase 1 so the
    two paths can never drift.
    """
    out = []
    for label, pat, ext, ci, pat_low in pat_list:
        needle = pat_low if ci else pat
        hay_use = hay_low if ci else hay
        plen = len(pat)
        pos = hay_use.find(needle)
        while pos != -1:
            if pos + plen > carry_floor and _reportable(last_hit, label, base + pos, min_gap):
                out.append((label, ext, plen, base + pos))
            pos = hay_use.find(needle, pos + 1)
    return out


def parallel_worker_count():
    """How many phase-1 processes to use (1 = sequential)."""
    if PARALLEL_WORKERS is None:
        try:
            return max(1, min(4, os.cpu_count() or 1))
        except Exception:
            return 1
    try:
        return max(1, int(PARALLEL_WORKERS))
    except (TypeError, ValueError):
        return 1


def _scan_range_worker(job):
    """
    Phase 1 for one byte range, executed in a child process.

    Each worker opens its own handle, reads forward, and reports only the hits
    that START inside its own [start, end) so adjacent ranges never report the
    same hit twice. MIN_GAP is NOT applied here - the parent applies it over the
    merged, offset-ordered hits, which is exactly what one sequential pass did.
    """
    target, patterns, start, end, chunk_size = job
    pat_list = build_pat_list(patterns)
    max_len = max(len(p[1]) for p in pat_list)
    carry_len = max_len - 1
    any_ci = any(p[3] for p in pat_list)

    hits = []
    local_hit = {}
    read_from = max(0, start - carry_len)
    aligned = (read_from // DEVICE_ALIGNMENT) * DEVICE_ALIGNMENT
    # Read a little PAST `end`: a pattern can start just before the range
    # boundary and finish after it, and only the next block's carry exposes it.
    read_limit = end + max_len

    f = open(target, "rb", buffering=0)
    try:
        f.seek(aligned)
        carry = b""
        next_off = aligned
        while next_off < read_limit:
            chunk = read_exact(f, chunk_size)
            if not chunk:
                break
            combined = carry + chunk
            base = next_off - len(carry)
            carry_floor = len(carry)
            if chunk == zero_block(len(chunk)):
                hay = combined[:min(len(combined), carry_floor + max_len)]
            else:
                hay = combined
            hay_low = hay.lower() if any_ci else None
            for label, ext, plen, abs_off in match_block(hay, hay_low, pat_list,
                                                         base, carry_floor,
                                                         local_hit, 0):
                if start <= abs_off < end:
                    hits.append({"label": label, "offset": abs_off,
                                 "sector": abs_off // SECTOR_SIZE,
                                 "carved": "", "ext": ext, "plen": plen})
            next_off += len(chunk)
            if len(chunk) < chunk_size:
                break
            carry = combined[-carry_len:] if carry_len else b""
    finally:
        f.close()
    return hits


def parallel_phase1(target, patterns, total_size, chunk_size, workers, min_gap,
                    show_progress=True):
    """
    Split the source into `workers` byte ranges and scan them in parallel
    processes (bytes.find holds the GIL, so threads would not help). Returns the
    merged hits with MIN_GAP applied in local order, which is what one
    sequential pass produced.
    """
    import multiprocessing

    step = max(chunk_size, ((total_size + workers - 1) // workers // chunk_size) * chunk_size)
    jobs = []
    start = 0
    while start < total_size:
        end = min(total_size, start + step)
        jobs.append((target, patterns, start, end, chunk_size))
        start = end

    parts = []
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=min(workers, len(jobs))) as pool:
        for done, part in enumerate(pool.imap_unordered(_scan_range_worker, jobs), 1):
            parts.extend(part)
            if show_progress:
                sys.stdout.write("\rParallel phase 1: %d / %d ranges done, %d hits   "
                                 % (done, len(jobs), len(parts)))
                sys.stdout.flush()
    if show_progress:
        print()

    merged = sorted(parts, key=lambda h: h["offset"])
    last_hit = {}
    kept = []
    for hit in merged:
        if _reportable(last_hit, hit["label"], hit["offset"], min_gap):
            kept.append(hit)
    kept.sort(key=lambda h: (h["label"], h["offset"]))
    return kept


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

    pat_list = build_pat_list(patterns)

    max_len = max(len(p[1]) for p in pat_list)
    carry_len = max_len - 1
    any_ci = any(p[3] for p in pat_list)

    hits = []
    counts = dict((p[0], 0) for p in pat_list)
    carved_counts = {}
    scanned = 0
    zero_bytes = 0
    use_parallel = False
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
        pending = []

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

        # ---- phase 1: parallel byte ranges when the source is big enough ----
        workers = parallel_worker_count()
        if workers > 1 and total_size and total_size >= PARALLEL_MIN_SIZE:
            try:
                print("Scanning %s in %d parallel ranges ..." % (fmt_bytes(total_size), workers))
                found = parallel_phase1(target, patterns, total_size, chunk_size,
                                        workers, min_gap, show_progress)
                hits.extend(found)
                counts = dict((p[0], 0) for p in pat_list)
                for hit in hits:
                    counts[hit["label"]] = counts.get(hit["label"], 0) + 1
                    line = ("[%s] HIT | %s | offset=0x%016X (dec %d) | sector=%d"
                            % (now(), hit["label"], hit["offset"], hit["offset"],
                               hit["sector"]))
                    log(line)
                    pending.append(line)
                if pending:
                    print()
                    print("\n".join(pending))
                    del pending[:]
                scanned = total_size
                use_parallel = True
            except Exception as exc:      # a broken pool must never lose the scan
                log("parallel phase failed: %s - falling back to one process" % exc)
                print("Parallel scan failed (%s) - falling back to one process." % exc)
                del hits[:]
                use_parallel = False
        log("mode         : %s" % ("parallel phase 1 (%d processes)" % workers
                                   if use_parallel else "single process"))
        if not use_parallel and workers > 1 and total_size and total_size < PARALLEL_MIN_SIZE:
            log("parallel     : off (source smaller than %s)" % fmt_bytes(PARALLEL_MIN_SIZE))

        try:
            while not use_parallel:
                chunk = read_exact(f, chunk_size)
                if not chunk:
                    break

                combined = carry + chunk
                base = next_off - len(carry)
                carry_floor = len(carry)

                # 13.3 zero-region fast skip: an all-zero block can only hold a
                # signature that started inside the carried tail of the previous
                # block, so the match runs over a tiny boundary window instead of
                # the whole block. On a freshly formatted disk this is most of
                # the volume, skipped at C-level memcmp speed.
                if chunk == zero_block(len(chunk)):
                    zero_bytes += len(chunk)
                    hay = combined[:min(len(combined), carry_floor + max_len)]
                else:
                    hay = combined
                hay_low = hay.lower() if any_ci else None

                # 13.2 measured: one bytes.find() pass per pattern is the fastest
                # strategy here (a single regex alternation of 117 needles was
                # 4x slower, and an anchor-class sieve 5x slower on zero-heavy
                # blocks - see V5_PLAN.md 13.8).
                for label, ext, plen, abs_off in match_block(hay, hay_low, pat_list,
                                                             base, carry_floor,
                                                             last_hit, min_gap):
                    sector = abs_off // SECTOR_SIZE
                    hits.append({"label": label, "offset": abs_off,
                                 "sector": sector, "carved": "",
                                 "ext": ext, "plen": plen})
                    counts[label] += 1
                    line = ("[%s] HIT | %s | offset=0x%016X (dec %d) | sector=%d"
                            % (now(), label, abs_off, abs_off, sector))
                    log(line)
                    pending.append(line)

                if pending:
                    print()
                    print("\n".join(pending))
                    del pending[:]
                log_file.flush()

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

        # ---- phase 2: carve every recorded hit in ONE forward pass ----------
        carved_files = 0
        if carve_dir and hits:
            print("-" * 78)
            print("PHASE 2 - CARVING %d HITS (one forward read of the source)" % len(hits))
            print("-" * 78)
            carved_files = carve_phase(f, hits, total_size, carve_dir,
                                       carve_before, carve_after, carve_limit,
                                       carved_counts, log, show_progress)

        elapsed = time.time() - started
        log("=== SCAN END %s === status=%s" % (now(), "INTERRUPTED" if interrupted else "COMPLETE"))
        log("bytes scanned: %d" % scanned)
        log("sectors      : %d" % (scanned // SECTOR_SIZE))
        if scanned:
            log("zero regions : %d bytes (%.1f%%)" % (zero_bytes, 100.0 * zero_bytes / scanned))
        log("elapsed      : %s" % fmt_hms(elapsed))
        log("hits total   : %d" % len(hits))
        for entry in pat_list:
            log("  %-40s %d" % (entry[0], counts[entry[0]]))

        return {
            "target": target,
            "total_size": total_size,
            "bytes_scanned": scanned,
            "zero_bytes": zero_bytes,
            "elapsed": elapsed,
            "hits": hits,
            "counts": counts,
            "carved_files": carved_files,
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




def write_index_html(summary, names, report_dir, index_path):
    """Write a browsable index.html listing recovered items."""
    carve_dir = summary.get("carve_dir") or ""
    items = []
    if carve_dir and os.path.isdir(carve_dir):
        for fname in sorted(os.listdir(carve_dir)):
            path = os.path.join(carve_dir, fname)
            if not os.path.isfile(path):
                continue
            if fname.endswith(".triage.txt"):
                continue
            if fname.endswith(".txt"):
                continue
            size = os.path.getsize(path)
            triage = ""
            tp = path + ".triage.txt"
            if os.path.isfile(tp):
                try:
                    with open(tp, "r", encoding="utf-8", errors="replace") as fh:
                        triage = fh.read()
                except OSError:
                    triage = ""
            cls = ""
            ent = ""
            for line in triage.splitlines():
                if line.startswith("classification"):
                    cls = line.split(":", 1)[-1].strip()
                if line.startswith("entropy"):
                    ent = line.split(":", 1)[-1].strip()
            items.append((fname, size, cls, ent))
    rows = []
    for fname, size, cls, ent in items:
        color = "#2e7d32" if str(cls).startswith("TEXT") else ("#c62828" if str(cls).startswith("COMPRESSED") else ("#616161" if str(cls).startswith("ZERO") else "#1565c0"))
        rows.append("<tr><td>%s</td><td>%s</td><td style='color:%s'>%s</td><td>%s</td></tr>" % (fname, fmt_bytes(size), color, cls or "-", ent or "-"))
    html = []
    html.append("<!DOCTYPE html>")
    html.append("<html><head><meta charset='utf-8'><title>fixformat recovery index</title>")
    html.append("<style>body{font-family:Segoe UI,Arial,sans-serif;margin:20px;background:#fafafa;color:#222}")
    html.append("table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}")
    html.append("th{background:#eee} h1{color:#1565c0}</style></head><body>")
    html.append("<h1>fixformat v%s - recovery index</h1>" % VERSION)
    html.append("<p>target: %s<br>bytes scanned: %s<br>hits: %d<br>recovered files: %d</p>" % (summary.get("target", ""), fmt_bytes(summary.get("bytes_scanned", 0)), len(summary.get("hits", [])), len(items)))
    html.append("<table><tr><th>file</th><th>size</th><th>classification</th><th>entropy</th></tr>")
    html.append("".join(rows))
    html.append("</table>")
    if names:
        html.append("<h2>Candidate file names</h2><ul>")
        for name, cnt in names:
            html.append("<li>%s (x%d)</li>" % (name, cnt))
        html.append("</ul>")
    html.append("</body></html>")
    try:
        with open(index_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(html))
        return True
    except OSError:
        return False



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
    """
    Ask whether the two run folders should be moved to another disk.
    Returns the chosen root folder, or "" to keep them where they are.
    """
    print()
    print("Keep the results here, or move them to another disk?")
    print("Choose a folder on a DIFFERENT disk than the scanned one - writing")
    print("onto the damaged disk can overwrite the very data you want to recover.")
    print()
    answer, ok = _ask("Move the results to another folder now? [y/N]: ")
    if not ok or answer.strip().lower() not in ("y", "yes"):
        print("Results kept in: %s" % work_dir)
        return ""
    root = pick_folder("the recovered data")
    if not root:
        print("Results kept in: %s" % work_dir)
        return ""
    if os.path.abspath(root) == os.path.abspath(work_dir):
        print("Results kept in: %s" % work_dir)
        return ""
    remember_path("recovered", root)
    return root


# ----------------------------------------------------------------------
# run folders: provenance (V5 Section 1) and organized output (Section 2)
# ----------------------------------------------------------------------

RECOVERED_PREFIX = "recovered_"
ORGANIZED_PREFIX = "organized_"
PROVENANCE_FILE = "provenance.json"


def source_label(target):
    """'PhysicalDrive0' for a device, else the image base name (sanitized)."""
    if target.startswith("\\\\"):
        tail = target.replace("\\\\", "\\").split("\\")[-1]
        clean = "".join(ch if ch.isalnum() else "_" for ch in tail).strip("_")
        return clean or "device"
    base = os.path.basename(target.rstrip("\\/")) or "image"
    for suffix in (".img.gz", ".img.xz", ".img.zst", ".img", ".raw", ".dd"):
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)]
            break
    clean = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in base)
    return clean[:40] or "image"


def size_tag(total):
    """Folder-safe size tag: '476.9GB', '114.6GB', '900MB', 'unknown'."""
    if not total:
        return "unknown"
    for unit, div in (("TB", 1024.0 ** 4), ("GB", 1024.0 ** 3),
                      ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if total >= div:
            return "%.1f%s" % (total / div, unit)
    return "%dB" % total


def run_tag(target, total):
    """<label>_<size>_<timestamp> - the provenance stamp shared by both folders."""
    return "%s_%s_%s" % (source_label(target), size_tag(total),
                         datetime.now().strftime("%Y%m%d_%H%M%S"))


def make_run_dirs(base_dir, target, total, tag=None):
    """
    Create recovered_<tag>/carved and organized_<tag>/by_extension under
    base_dir. Returns (recovered_dir, organized_dir, tag).
    """
    tag = tag or run_tag(target, total)
    recovered = os.path.join(base_dir, RECOVERED_PREFIX + tag)
    organized = os.path.join(base_dir, ORGANIZED_PREFIX + tag)
    os.makedirs(os.path.join(recovered, "carved"), exist_ok=True)
    os.makedirs(os.path.join(organized, "by_extension"), exist_ok=True)
    return recovered, organized, tag


def write_provenance(run_dir, data):
    """Write provenance.json into the recovered folder. Returns the path or ''."""
    path = os.path.join(run_dir, PROVENANCE_FILE)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return path
    except OSError:
        return ""


def read_provenance(run_dir):
    """The provenance dict of a recovered folder, {} when unreadable."""
    try:
        with open(os.path.join(run_dir, PROVENANCE_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_recoveries(roots=None, depth=1):
    """[(folder, provenance), ...] for every recovered_* folder that has one."""
    if roots is None:
        roots = [SCRIPT_DIR, os.getcwd()]
        remembered = load_last_paths().get("recovered")
        if remembered:
            roots.append(remembered)
        roots.extend(root for root, _t, _f in list_logical_drives())
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        base_depth = root.rstrip("\\/").count(os.sep)
        for cur, dirs, _files in os.walk(root, onerror=lambda _e: None):
            if cur.rstrip("\\/").count(os.sep) - base_depth >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d.startswith(RECOVERED_PREFIX)]
            for name in list(dirs):
                path = os.path.join(cur, name)
                key = path.lower()
                if key in seen:
                    continue
                seen.add(key)
                prov = read_provenance(path)
                if prov:
                    found.append((path, prov))
    found.sort(key=lambda item: str(item[1].get("started", "")), reverse=True)
    return found


def provenance_payload(target, total, summary, patterns, case_sensitive,
                       started_at, progress=None):
    """The provenance.json body for one run (Section 1)."""
    payload = {
        "tool": "fixformat raw_scan.py",
        "version": VERSION,
        "started": started_at,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": target,
        "source_label": source_label(target),
        "source_size": total,
        "source_size_text": fmt_bytes(total) if total else "unknown",
        "signatures": len(patterns),
        "signature_labels": [p[0] for p in patterns],
        "case_sensitive": bool(case_sensitive),
        "min_gap": MIN_GAP,
        "carve_before": CARVE_BEFORE,
        "carve_after": CARVE_AFTER,
        "chunk_size": CHUNK_SIZE,
    }
    if summary:
        payload.update({
            "bytes_scanned": summary.get("bytes_scanned", 0),
            "zero_bytes": summary.get("zero_bytes", 0),
            "hits": len(summary.get("hits") or []),
            "carved_files": summary.get("carved_files", 0),
            "counts": summary.get("counts", {}),
            "interrupted": bool(summary.get("interrupted")),
            "elapsed_seconds": round(summary.get("elapsed", 0.0), 1),
            "status": "interrupted" if summary.get("interrupted") else "complete",
        })
    if progress:
        payload.update(progress)
    return payload


def organize_output(summary, run_dir, organized_dir, names=None):
    """
    Section 2: move every carved file into organized/by_extension/<ext>/,
    then write index.html (relative links) and summary.txt. Returns
    (moved, index_path, summary_path).
    """
    carve_dir = summary.get("carve_dir") or os.path.join(run_dir, "carved")
    by_ext = os.path.join(organized_dir, "by_extension")
    os.makedirs(by_ext, exist_ok=True)

    moved = 0
    if os.path.isdir(carve_dir):
        for name in sorted(os.listdir(carve_dir)):
            src = os.path.join(carve_dir, name)
            if not os.path.isfile(src):
                continue
            is_sidecar = name.endswith(".triage.txt")
            if is_sidecar:
                continue
            ext = os.path.splitext(name)[1].lstrip(".").lower() or "bin"
            dest_dir = os.path.join(by_ext, ext)
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.move(src, os.path.join(dest_dir, name))
                sidecar = src + ".triage.txt"
                if os.path.isfile(sidecar):
                    shutil.move(sidecar, os.path.join(dest_dir, name + ".triage.txt"))
                moved += 1
            except OSError:
                continue

    index_path = write_organized_index(organized_dir, summary)
    if names is None:
        names = extract_candidate_names(by_ext)
    summary_path = os.path.join(organized_dir, "summary.txt")
    try:
        write_report(summary, names, summary_path)
    except OSError:
        summary_path = ""
    return moved, index_path, summary_path


def write_organized_index(organized_dir, summary):
    """Self-contained index.html over by_extension, with clickable previews."""
    by_ext = os.path.join(organized_dir, "by_extension")
    rows = []
    total_files = 0
    total_bytes = 0
    if os.path.isdir(by_ext):
        for ext in sorted(os.listdir(by_ext)):
            folder = os.path.join(by_ext, ext)
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if name.endswith(".triage.txt"):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                total_files += 1
                total_bytes += size
                cls = ""
                triage = path + ".triage.txt"
                if os.path.isfile(triage):
                    try:
                        with open(triage, "r", encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                if line.startswith("classification"):
                                    cls = line.split(":", 1)[-1].strip()
                    except OSError:
                        cls = ""
                color = ("#2e7d32" if cls.startswith("TEXT")
                         else "#c62828" if cls.startswith("COMPRESSED")
                         else "#616161" if cls.startswith("ZERO") else "#1565c0")
                rows.append(
                    "<tr><td>%s</td><td><a href='by_extension/%s/%s'>%s</a></td>"
                    "<td>%s</td><td style='color:%s'>%s</td></tr>"
                    % (ext, ext, name, name, fmt_bytes(size), color, cls or "-"))
    header = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'><title>fixformat organized recovery</title>",
        "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:20px;background:#fafafa;color:#222}",
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}",
        "th{background:#eee} h1{color:#1565c0} .meta{color:#555}</style></head><body>",
        "<h1>fixformat - organized recovery</h1>",
        "<p class='meta'>%s &middot; %d files &middot; %s &middot; generated %s</p>"
        % (os.path.basename(organized_dir), total_files, fmt_bytes(total_bytes),
           datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "<table><tr><th>type</th><th>file</th><th>size</th><th>triage</th></tr>",
    ]
    html = header + (rows or ["<tr><td colspan='4'>no recovered file</td></tr>"])
    html.append("</table></body></html>")
    index_path = os.path.join(organized_dir, "index.html")
    try:
        with open(index_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(html))
        return index_path
    except OSError:
        return ""


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def open_target(target):
    """Open a target read-only and return its size in bytes (0 when unknown)."""
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


def run_search_flow(target, total):
    """Menu options 1 and 3: choose signatures, confirm, scan, report, save."""
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

    # ---- run folders (Section 1 + 2): one pair per run, provenance-stamped ----
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recovered_dir, organized_dir, run_id = make_run_dirs(SCRIPT_DIR, target, total)
    results_path = os.path.join(recovered_dir, RESULTS_FILE)
    carve_dir = os.path.join(recovered_dir, "carved") if ENABLE_CARVING else None
    print("results folder  : %s" % recovered_dir)

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
    print_report(summary, names)

    # provenance (Section 1): one JSON next to the extracted data
    provenance = provenance_payload(target, total, summary, patterns,
                                    case_sensitive, started_at)
    provenance["run_id"] = run_id
    provenance["recovered_dir"] = recovered_dir
    provenance["organized_dir"] = organized_dir
    provenance["results_log"] = results_path
    write_provenance(recovered_dir, provenance)

    # organized browsable output (Section 2)
    moved, index_path, summary_path = organize_output(
        summary, recovered_dir, organized_dir, names)
    carved_count = carved_bytes = 0
    for cur_dir, _dirs, files in os.walk(os.path.join(organized_dir, "by_extension")):
        for name in files:
            if name.endswith(".triage.txt"):
                continue
            try:
                carved_bytes += os.path.getsize(os.path.join(cur_dir, name))
                carved_count += 1
            except OSError:
                pass

    print()
    print("recovered folder : %s" % recovered_dir)
    print("  results log    : %s" % results_path)
    print("  provenance     : %s" % os.path.join(recovered_dir, PROVENANCE_FILE))
    print("organized folder : %s" % organized_dir)
    print("  by_extension   : %d files, %s" % (carved_count, fmt_bytes(carved_bytes)))
    if index_path:
        print("  index (html)   : %s" % index_path)
    if summary_path:
        print("  summary        : %s" % summary_path)

    # ask WHERE to keep the two run folders (Section 1/2 keep them siblings)
    final_root = ask_save_location(SCRIPT_DIR)
    if final_root:
        try:
            new_recovered = shutil.move(recovered_dir, final_root)
            new_organized = shutil.move(organized_dir, final_root)
            print()
            print("SAVED TO: %s" % final_root)
            print("  %s" % new_recovered)
            print("  %s" % new_organized)
        except OSError as exc:
            print("Move failed: %s - results remain in %s" % (exc, SCRIPT_DIR))
    if not summary["hits"]:
        print()
        print("No matches found. Add the file types you need in this run's")
        print("extension step, or extend the signatures in raw_scan.py.")


# ----------------------------------------------------------------------
# disk imaging (v3)
# ----------------------------------------------------------------------

def ask_image_file():
    """Numbered picker for an existing disk image. Returns (path, size) or (None, 0)."""
    return pick_image()


def get_volume_fs(path):
    """Return the file-system name (e.g. 'NTFS', 'exFAT', 'FAT32') for path."""
    if not IS_WINDOWS:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        target = os.path.abspath(path if os.path.isdir(path) else (os.path.dirname(path) or "."))
        buf = ctypes.create_unicode_buffer(1024)
        ok = kernel32.GetVolumePathNameW(target, buf, 1024)
        root = buf.value if ok else ""
        if not root:
            drive = os.path.splitdrive(target)[0]
            root = (drive + "\\") if drive else ""
        fs = ctypes.create_unicode_buffer(260)
        ok2 = kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 260)
        return fs.value if ok2 else ""
    except Exception:
        return ""


def set_sparse_file(f):
    """Mark an open file handle as a sparse file (Windows NTFS). Returns True on success."""
    if not IS_WINDOWS:
        return False
    try:
        from ctypes import wintypes
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        h = msvcrt.get_osfhandle(f.fileno())
        ret = wintypes.DWORD(0)
        FSCTL_SET_SPARSE = 0x000900C4
        ok = kernel32.DeviceIoControl(
            wintypes.HANDLE(h), FSCTL_SET_SPARSE,
            None, 0, None, 0, ctypes.byref(ret), None)
        return bool(ok)
    except Exception:
        return False


def is_all_zeros(chunk):
    """True when chunk consists entirely of zero bytes (no buffer allocated)."""
    if not chunk:
        return True
    return chunk.count(b"\x00") == len(chunk)


def ask_imaging_mode():
    """Ask which image format to use. Returns one of: raw, sparse, gzip, xz, zstd."""
    print("Choose the image format:")
    print("  1) Raw (.img)            - default, uncompressed, universal compatibility.")
    print("  2) NTFS Sparse (.img)    - recommended on NTFS: all-zero sectors take no disk space, file still reads back complete.")
    print("  3) gzip (.img.gz)        - stream-compressed (stdlib gzip, low level for speed). Fallback for non-NTFS.")
    print("  4) xz (.img.xz)          - stream-compressed (stdlib lzma).")
    print("  5) zstd (.img.zst)       - requires an external 'zstd' binary on PATH.")
    print()
    while True:
        try:
            answer = input("Your choice [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "raw"
        if answer in ("", "1"):
            return "raw"
        if answer == "2":
            return "sparse"
        if answer == "3":
            return "gzip"
        if answer == "4":
            return "xz"
        if answer == "5":
            return "zstd"
        print("Please type 1, 2, 3, 4 or 5.")

def ask_image_destination(source_size, suggested_name):
    """Numbered picker for where to save a new image (free space enforced)."""
    print("The image file will be as large as the disk itself.")
    print("Pick the volume, walk into a folder, then press S to select it.")
    print("Never select the disk you are imaging.")
    print()
    return pick_destination(source_size, suggested_name, "the disk image")


def create_disk_image(source, dest, chunk_size, show_progress=True, mode="raw"):
    """
    Copy every byte of `source` (raw device or file) into `dest`, computing a
    SHA-256 checksum while copying. Returns a summary dict:
      source, dest, source_size, copied, sha256, elapsed, interrupted, verified.
    On failure or interruption the partial file is renamed to `<dest>.partial`
    so it is never mistaken for a complete image. A `<dest>.info.txt` sidecar
    with the checksum and sizes is written next to the image.

    mode: raw | sparse | gzip | xz | zstd
    """
    is_device = IS_WINDOWS and source.startswith("\\\\.\\")
    if is_device and chunk_size % DEVICE_ALIGNMENT != 0:
        raise ValueError("For raw devices chunk_size must be a multiple of %d" % DEVICE_ALIGNMENT)

    # normalize destination extension per mode
    ext_map = {"raw": "", "sparse": "", "gzip": ".gz", "xz": ".xz", "zstd": ".zst"}
    suffix = ext_map.get(mode, "")
    if suffix and not dest.lower().endswith(suffix):
        dest = dest + suffix

    if mode == "zstd":
        import shutil as _sh
        if not _sh.which("zstd"):
            raise ValueError("zstd mode requires the 'zstd' executable on PATH (not found)")

    started = time.time()
    hasher = hashlib.sha256()
    copied = 0
    interrupted = False
    total_size_ref = [0]
    last_progress = [0.0]

    def update_progress(force=False):
        if not show_progress:
            return
        t = time.time()
        if not force and (t - last_progress[0]) < 1.0:
            return
        last_progress[0] = t
        elapsed = t - started
        rate = copied / elapsed if elapsed > 0 else 0
        if total_size_ref[0]:
            pct = 100.0 * copied / total_size_ref[0]
            remaining = max(0, total_size_ref[0] - copied)
            eta = remaining / rate if rate > 0 else 0
            line = ("\\rImaging: %s / %s (%.2f%%) | sectors %s / %s | speed %s/s | ETA %s"
                    % (fmt_bytes(copied), fmt_bytes(total_size_ref[0]), pct,
                       fmt_int(copied // SECTOR_SIZE), fmt_int(total_size_ref[0] // SECTOR_SIZE),
                       fmt_bytes(rate), fmt_hms(eta)))
        else:
            line = ("\\rImaging: %s copied | sectors %s | speed %s/s"
                    % (fmt_bytes(copied), fmt_int(copied // SECTOR_SIZE), fmt_bytes(rate)))
        sys.stdout.write(line)
        sys.stdout.flush()

    f = open(source, "rb", buffering=0)   # may raise PermissionError / OSError
    try:
        total_size_ref[0] = get_device_size(f)
        out = None
        proc = None
        try:
            if mode in ("raw", "sparse"):
                out = open(dest, "wb")
                if mode == "sparse":
                    if get_volume_fs(dest) != "NTFS":
                        raise ValueError("NTFS Sparse requires an NTFS destination (detected: %s)"
                                         % (get_volume_fs(dest) or "unknown"))
                    if not set_sparse_file(out):
                        raise ValueError("Failed to mark destination as a sparse file (NTFS required)")
            elif mode == "gzip":
                import gzip as _gz
                out = _gz.open(dest, "wb", compresslevel=3)
            elif mode == "xz":
                import lzma as _lz
                out = _lz.open(dest, "wb", format=_lz.FORMAT_XZ, check=_lz.CHECK_CRC64, preset=1)
            elif mode == "zstd":
                import subprocess as _sp
                import shutil as _sh
                zbin = _sh.which("zstd")
                proc = _sp.Popen([zbin, "-q", "-o", dest, "-"], stdin=_sp.PIPE)
                out = proc.stdin
            else:
                raise ValueError("unknown imaging mode: %s" % mode)

            while True:
                chunk = read_exact(f, chunk_size)
                if not chunk:
                    break
                if mode == "sparse" and is_all_zeros(chunk):
                    # create a hole (no disk space consumed) instead of writing zeros
                    try:
                        out.seek(len(chunk), os.SEEK_CUR)
                    except (OSError, IOError):
                        out.write(chunk)
                else:
                    out.write(chunk)
                hasher.update(chunk)
                copied += len(chunk)
                update_progress()
                if len(chunk) < chunk_size:
                    break
            if out is not None:
                try:
                    out.flush()
                except Exception:
                    pass
            if proc is not None:
                try:
                    out.close()
                except Exception:
                    pass
                out = None
                rc = proc.wait()
                if rc != 0:
                    raise RuntimeError("zstd exited with code %d" % rc)
            if mode in ("raw", "sparse", "gzip", "xz") and out is not None:
                try:
                    out.close()
                except Exception:
                    pass
                out = None
            if mode in ("raw", "sparse"):
                try:
                    with open(dest, "r+b") as _fh:
                        _fh.seek(0, os.SEEK_END)
                        if _fh.tell() < copied:
                            _fh.seek(copied - 1)
                            _fh.write(b"\x00")
                            _fh.flush()
                        os.fsync(_fh.fileno())
                except OSError:
                    pass
        except KeyboardInterrupt:
            interrupted = True
        except BaseException:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            try:
                os.replace(dest, dest + ".partial")
            except OSError:
                pass
            raise
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

        update_progress(force=True)
        if show_progress:
            print()
        if interrupted:
            partial = dest + ".partial"
            try:
                os.replace(dest, partial)
                dest = partial
            except OSError:
                pass

        elapsed = time.time() - started
        verified = (not total_size_ref[0]) or (copied == total_size_ref[0])
        summary = {
            "source": source, "dest": dest,
            "source_size": total_size_ref[0], "copied": copied,
            "sha256": hasher.hexdigest(), "elapsed": elapsed,
            "interrupted": interrupted, "verified": verified,
            "mode": mode,
        }
        try:
            with open(dest + ".info.txt", "w", encoding="utf-8") as fh:
                fh.write("disk image created by raw_scan.py v%s\n" % VERSION)
                fh.write("date        : %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("source      : %s\n" % source)
                fh.write("mode        : %s\n" % mode)
                fh.write("source size : %d bytes (%s sectors)\n"
                         % (total_size_ref[0], fmt_int(total_size_ref[0] // SECTOR_SIZE)))
                fh.write("copied      : %d bytes (%s sectors)\n"
                         % (copied, fmt_int(copied // SECTOR_SIZE)))
                fh.write("verified    : %s\n" % ("yes" if verified else "NO"))
                fh.write("sha256      : %s\n" % summary["sha256"])
                fh.write("elapsed     : %s\n" % fmt_hms(elapsed))
                fh.write("status      : %s\n" % ("INTERRUPTED" if interrupted else "COMPLETE"))
        except OSError:
            pass
        return summary
    finally:
        f.close()

def run_imaging_flow():
    """Menu option 2: create a full image of a disk in one fast read."""
    print("=" * 78)
    print("STEP 1 of 5 - CHOOSE THE DISK TO IMAGE")
    print("=" * 78)
    print("The disk is read ONE time, at maximum speed, and copied into a single")
    print("image file. Nothing on the disk is modified.")
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
            print("No target selected. Nothing was done.")
            sys.exit(0)

    print()
    print("Selected target : %s" % target)
    if total:
        print("Size            : %s (%d bytes, %s sectors)"
              % (fmt_bytes(total), total, fmt_int(total // SECTOR_SIZE)))
    else:
        print("Size            : unknown (will copy until end of media)")

    print()
    print("=" * 78)
    print("STEP 2 of 5 - CHOOSE THE IMAGE FORMAT")
    print("=" * 78)
    mode = ask_imaging_mode()
    print("Selected format : %s" % mode)
    print()

    print("=" * 78)
    print("STEP 3 of 5 - CHOOSE WHERE TO SAVE THE IMAGE")
    print("=" * 78)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", target.replace("\\\\.\\", ""))[:40]
    suggested = "diskimage_%s_%s.img" % (safe, datetime.now().strftime("%Y%m%d_%H%M%S"))
    dest = ask_image_destination(total, suggested)
    if not dest:
        print("No destination selected. Nothing was done.")
        sys.exit(0)

    print()
    print("=" * 78)
    print("STEP 4 of 5 - CONFIRM YOUR CHOICES")
    print("=" * 78)
    print("  source       : %s" % target)
    print("  source size  : %s" % (fmt_bytes(total) if total else "unknown"))
    print("  format       : %s" % mode)
    print("  image file   : %s" % dest)
    print("  read block   : %s" % fmt_bytes(IMAGE_CHUNK_SIZE))
    print()
    print("The image is written directly to the destination above (no second")
    print("copy). A SHA-256 checksum is computed while copying and saved next to")
    print("the image in a .info.txt file.")
    print()
    if not confirm_start():
        print("Stopped. Nothing was done.")
        sys.exit(0)

    print()
    print("=" * 78)
    print("STEP 5 of 5 - IMAGING (one fast read of the disk)")
    print("=" * 78)
    print("The progress line shows: copied / total (percent) | sectors copied /")
    print("total | speed | estimated time left. Press Ctrl+C to stop early - the")
    print("partial file is renamed to *.partial so it is never mistaken for a")
    print("complete image.")
    print("-" * 78)

    try:
        summary = create_disk_image(target, dest, IMAGE_CHUNK_SIZE, mode=mode)
    except PermissionError:
        print("\nERROR: access denied.")
        print("Raw device reads require administrator / SYSTEM rights.")
        sys.exit(3)
    except ValueError as exc:
        print("\nCONFIG ERROR: %s" % exc)
        sys.exit(2)
    except OSError as exc:
        print("\nERROR: %s" % exc)
        print("If the destination disk is full, free some space or choose another")
        print("destination and run again.")
        sys.exit(1)

    print()
    print("=== IMAGING RESULT ===")
    print("  image file   : %s" % summary["dest"])
    print("  copied       : %s (%d bytes, %s sectors)"
          % (fmt_bytes(summary["copied"]), summary["copied"],
             fmt_int(summary["copied"] // SECTOR_SIZE)))
    if summary["source_size"]:
        print("  source size  : %s (%d bytes)"
              % (fmt_bytes(summary["source_size"]), summary["source_size"]))
        print("  verified     : %s"
              % ("YES - copied size matches the source"
                 if summary["verified"] else "NO - sizes differ, run the imaging again"))
    print("  sha256       : %s" % summary["sha256"])
    print("  elapsed      : %s" % fmt_hms(summary["elapsed"]))
    print("  status       : %s"
          % ("INTERRUPTED - file kept as %s" % summary["dest"] if summary["interrupted"]
             else "COMPLETE"))
    print("  info file    : %s" % (summary["dest"] + ".info.txt"))
    if not summary["interrupted"]:
        print()
        print("Next step: run this tool again and choose option 3 to analyze the")
        print("image - the original disk will not be touched again.")


def main():
    print("raw_scan.py v%s - raw sector signature scanner and disk imager" % VERSION)
    print("          (read-only for every disk it reads, fully offline)")
    print("script dir : %s" % SCRIPT_DIR)
    print()
    print("This tool never writes to the disk it reads. Every step below is")
    print("explained on screen and asks you what to do.")
    print()
    print("=" * 78)
    print("MAIN MENU - CHOOSE WHAT TO DO")
    print("=" * 78)
    print("  1 - SEARCH A DISK FOR FILE SIGNATURES")
    print("      Reads a disk once and reports every match right away.")
    print()
    print("  2 - CREATE A FULL IMAGE OF A DISK  (recommended first step)")
    print("      Copies every byte of the disk into one .img file in a single")
    print("      fast pass. The disk hardware is read only once; the image can")
    print("      then be analyzed again and again without stressing the disk.")
    print()
    print("  3 - ANALYZE AN EXISTING DISK IMAGE")
    print("      Searches an image file (created by option 2, or any .img)")
    print("      for file signatures and recovers data from it.")
    print()
    print("Best practice: run option 2 first, then option 3 on the created image.")
    print()
    while True:
        try:
            choice = input("Your choice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("No choice made. Nothing was done.")
            sys.exit(0)
        if choice in ("1", "2", "3"):
            break
        print("Please type 1, 2 or 3.")
    print()
    if choice == "2":
        run_imaging_flow()
        return
    if choice == "3":
        print("=" * 78)
        print("STEP 1 of 5 - CHOOSE THE DISK IMAGE TO ANALYZE")
        print("=" * 78)
        print("Type the path of the image file created by option 2 (a .img file).")
        print("Analyzing an image does not touch the original disk at all.")
        print()
        target, total = ask_image_file()
        if not target:
            print("No image selected. Nothing was scanned.")
            sys.exit(0)
    else:
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
    run_search_flow(target, total)


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

# Read block size for imaging (menu option 2). The disk is read once, so big
# blocks maximize sustained throughput. Must be a multiple of 4096 for raw
# devices.
IMAGE_CHUNK_SIZE = 8 * 1024 * 1024

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
# v4: extend carved text windows while data looks like readable text.
ENABLE_SMART_TEXT = True
# v4: classify each carved window (TEXT/BINARY/COMPRESSED/ZERO) via entropy.
ENABLE_TRIAGE = True
# v4: write an index.html (browsable) next to the report.
ENABLE_INDEX_HTML = True
CARVE_DIR = "recovered"
CARVE_BEFORE = 4096            # bytes kept before the hit
CARVE_AFTER = 64 * 1024        # bytes kept after the hit
CARVE_LIMIT = 100              # max carved files per pattern (0 = unlimited)
# Phase 2 reads the source forward in blocks of this size and serves every carve
# window from the block in memory (one read per 4 MB instead of a seek per hit).
CARVE_BLOCK_SIZE = 4 * 1024 * 1024
# Phase 1 scans independent byte ranges in separate processes: bytes.find holds
# the GIL, so threads would not help. None = auto (up to 4 processes),
# 1 = always sequential. Small sources stay sequential.
PARALLEL_WORKERS = None
PARALLEL_MIN_SIZE = 64 * 1024 * 1024

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
