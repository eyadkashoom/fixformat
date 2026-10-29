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


# ----------------------------------------------------------------------
# signature profiles - a JSON list of magic bytes (v5 Section 7)
# ----------------------------------------------------------------------

PROFILE_FILE = "signature_profile.json"
# Path of the profile loaded in this run (empty when the built-in set was used).
# Kept so provenance can say which signature set produced the results.
PROFILE_LAST_PATH = ""
# Hex is written with spaces between bytes: "25 50 44 46 2D". A bare token
# ("AC10", "25504446") is read as TEXT, which is what AutoCAD's "AC10" header
# actually is - the tool must not guess.
HEX_VALUE_RE = re.compile(r"^[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*$")
PROFILE_EXT_RE = re.compile(r"^[a-z0-9_+-]{1,16}$")


def looks_like_hex(value):
    """True when every space-separated token of `value` is one hex byte."""
    text = str(value or "").strip()
    return bool(text) and bool(HEX_VALUE_RE.match(text))


def parse_signature_profile(data):
    """
    Validate the Section 7 shape and return {'<ext>': [value, ...]}:

        {"name": "AutoCAD only", "signatures": {"dwg": ["AC10", "AC11"]}}

    Raises ValueError naming the offending entry: a bad profile must fail before
    a scan starts, with a message that says which key is wrong.
    """
    if not isinstance(data, dict):
        raise ValueError("a profile must be one JSON object")
    signatures = data.get("signatures")
    if not isinstance(signatures, dict) or not signatures:
        raise ValueError("the profile has no 'signatures' object, or it is empty")
    profile = {}
    for raw_ext, values in signatures.items():
        if not isinstance(raw_ext, str) or not raw_ext.strip():
            raise ValueError("a profile extension key is not text: %r" % (raw_ext,))
        ext = raw_ext.strip().lstrip(".").lower()
        if not PROFILE_EXT_RE.match(ext):
            raise ValueError("profile extension %r is not a plain extension "
                             "(write .md, .dwg, .pdf ...)" % (raw_ext,))
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError("profile entry .%s has no signatures" % ext)
        kept = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("profile entry .%s has an empty signature" % ext)
            text = value.strip()
            if looks_like_hex(text):
                try:
                    parse_hex_pattern(text)
                except ValueError as exc:
                    raise ValueError("profile entry .%s: %s" % (ext, exc))
            elif len(text) < 2:
                # one character matches everything: it would fill the report with
                # noise instead of finding a file
                raise ValueError("profile entry .%s has a one-character value %r, "
                                 "which is too short to search for" % (ext, text))
            kept.append(text)
        profile[ext] = kept
    if not profile:
        raise ValueError("the profile is empty")
    return profile


def load_signature_profile(path):
    """
    Read + validate a profile file. Returns (profile, name).
    Every failure is a ValueError with a sentence a user can act on.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError("cannot read the profile %s: %s" % (path, exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("the profile %s is not valid JSON: %s"
                         % (os.path.basename(str(path)), exc))
    profile = parse_signature_profile(data)
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    return profile, name.strip()


def _profile_preview(value, width=32):
    flat = " ".join(str(value).split())
    return flat if len(flat) <= width else flat[:width - 3] + "..."


def profile_patterns(profile, case_sensitive=None):
    """{'ext': [values]} -> (label, pattern_bytes, ext, case_insensitive)."""
    if case_sensitive is None:
        case_sensitive = CASE_SENSITIVE
    patterns = []
    for ext in sorted(profile):
        for value in profile[ext]:
            base = "Profile .%s %s" % (ext, _profile_preview(value))
            if looks_like_hex(value):
                patterns.append((base, parse_hex_pattern(value), ext, False))
            else:
                for label, pat, entry_ext in expand_text_signature(base, value, ext):
                    patterns.append((label, pat, entry_ext, not case_sensitive))
    if not patterns:
        raise ValueError("the profile produced no usable patterns")
    return patterns


def profile_summary(profile, name=""):
    """One line for the pre-scan plan: name, extensions, signature count."""
    parts = []
    for ext in sorted(profile):
        parts.append(".%s(%d)" % (ext, len(profile[ext])))
    label = name or "profile"
    return "%s: %d signatures over %s" % (label, sum(len(v) for v in profile.values()),
                                         " ".join(parts))


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


STATE_FILES = ("last_paths.json", "scan_speed.json")


def list_profile_files(roots=None, depth=2):
    """[(path, size)] for the .json files that could be a signature profile."""
    if roots is None:
        remembered = load_last_paths().get("profile")
        roots = [d for d in (SCRIPT_DIR, remembered) if d]
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = os.path.abspath(root).rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            if os.path.abspath(cur).rstrip(os.sep).count(os.sep) - base_depth >= depth:
                del dirs[:]
            for name in sorted(files):
                if not name.lower().endswith(".json"):
                    continue
                if name.lower() in STATE_FILES:
                    continue
                path = os.path.join(cur, name)
                if path.lower() in seen:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                seen.add(path.lower())
                found.append((path, size))
    found.sort(key=lambda item: item[0].lower())
    return found


def ask_profile_file():
    """Numbered picker for a profile. Returns ({ext: [...]}, name) or (None, "")."""
    candidates = list_profile_files()
    print()
    if candidates:
        print("PROFILE FILES FOUND (%d)" % len(candidates))
        for index, (path, size) in enumerate(candidates[:PICKER_PAGE], 1):
            print("  [%d] %-52s %8s" % (index, _shorten(os.path.basename(path), 52),
                                        fmt_bytes(size)))
            print("      %s" % _shorten(os.path.dirname(path), 68))
        print("  [0] Type the full path of another profile")
    else:
        print("No .json profile was found next to this script.")
        print("  [0] Type the full path of a profile")
    print("  [N] Keep the built-in signatures")
    answer, ok = _ask("Profile [N]: ")
    if not ok:
        return None, ""
    answer = answer.strip()
    if not answer or answer.lower() in ("n", "q"):
        return None, ""
    path = None
    if answer == "0" or not answer.isdigit():
        typed, ok_path = _ask("Profile path: ")
        if not ok_path:
            return None, ""
        typed = typed.strip().strip('"').strip("'")
        path = os.path.abspath(os.path.expanduser(typed)) if typed else None
    else:
        index = int(answer) - 1
        if 0 <= index < min(len(candidates), PICKER_PAGE):
            path = candidates[index][0]
    if not path:
        print("No profile chosen - keeping the built-in signatures.")
        return None, ""
    if not os.path.isfile(path):
        raise ValueError("no such profile file: %s" % path)
    profile, name = load_signature_profile(path)   # ValueError explains itself
    remember_path("profile", path)
    global PROFILE_LAST_PATH
    PROFILE_LAST_PATH = path
    print()
    print("Loaded %s" % os.path.basename(path))
    print("  %s" % profile_summary(profile, name))
    return profile, name


def ask_signature_source():
    """
    Section 7: the built-in signatures, or a JSON profile of magic bytes.
    Returns ("builtin", None, "") or ("profile", {ext: [values]}, name).
    """
    print("=" * 78)
    print("STEP 2 of 5 - CHOOSE THE SIGNATURES")
    print("=" * 78)
    print("  [1] BUILT-IN SIGNATURES - every file type this script knows")
    print("  [2] A JSON PROFILE      - only the bytes you list in a file")
    print()
    print("A profile is the fast path: every block is compared against a handful")
    print("of patterns instead of all of them, and only what you asked for is")
    print("reported. Hex bytes are written with spaces (25 50 44 46 2D); anything")
    print("else is searched as text in the configured encodings.")
    print()
    answer, ok = _ask("Your choice [1]: ")
    if not ok or answer.strip() not in ("2",):
        return "builtin", None, ""
    profile, name = ask_profile_file()
    if profile is None:
        return "builtin", None, ""
    return "profile", profile, name


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


def smart_extend_text(f, start, initial_end, total_size, max_extra=None):
    """Extend a carved text window while data looks like readable text.

    Returns the new end offset (exclusive). Stops when a run of non-text bytes
    exceeds a small threshold or max_extra (SMART_TEXT_MAX_EXTRA by default) is
    reached.
    """
    if max_extra is None:
        max_extra = SMART_TEXT_MAX_EXTRA
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
        # Write a readable copy for text windows. It gets its OWN name: the raw
        # window above may itself end in ".txt", and decoding with
        # errors="replace" would destroy every non-UTF-8 byte (Windows-1256
        # Arabic, high-bit binary) - and the raw copy is the only real evidence.
        if ENABLE_READABLE_COPY and ext in ("md", "py", "json", "yaml", "yml", "env",
                                            "txt", "log", "toml", "cfg", "ini", "csv",
                                            "sh", "bat", "ps1", "sql"):
            try:
                text = data.decode("utf-8", errors="replace")
                txt_path = os.path.join(
                    carve_dir, "%s_0x%016X%s" % (safe, abs_off, READABLE_SUFFIX))
                if os.path.abspath(txt_path) == os.path.abspath(path):
                    pass                     # cannot happen, never clobber
                else:
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
    try:
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
    except KeyboardInterrupt:
        # the windows written so far are already on disk - report that number
        print("\nStopped by the user while carving - %d file(s) written so far."
              % carved)
        log("carving interrupted by the user after %d files" % carved)
        return carved
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
    `job[5]` (optional) is an absolute time.time() deadline: the worker stops
    reading when it passes it and reports budget_hit, which is what keeps the
    fast scan inside its time budget (V5 Section 5).
    """
    target, patterns, start, end, chunk_size = job[:5]
    deadline = job[5] if len(job) > 5 else None
    pat_list = build_pat_list(patterns)
    max_len = max(len(p[1]) for p in pat_list)
    carry_len = max_len - 1
    any_ci = any(p[3] for p in pat_list)

    hits = []
    local_hit = {}
    scanned = 0
    zero_bytes = 0
    budget_hit = False
    read_from = max(0, start - carry_len)
    aligned = (read_from // DEVICE_ALIGNMENT) * DEVICE_ALIGNMENT
    # Read a little PAST `end`: a pattern can start just before the range
    # boundary and finish after it, and only the next block's carry exposes it.
    read_limit = end + max_len

    f = open(target, "rb", buffering=0)
    try:
        # the read window runs past `end` by max_len to catch a signature that
        # straddles the boundary - but never past the end of a raw device, which
        # Windows refuses (review S10)
        try:
            device_size = get_device_size(f)
        except Exception:
            device_size = 0
        if device_size:
            read_limit = min(read_limit, device_size)
        f.seek(aligned)
        carry = b""
        next_off = aligned
        while next_off < read_limit:
            if deadline is not None and time.time() > deadline:
                budget_hit = True
                break
            # never read past this range's own window: the fast scan slices are
            # smaller than one block, and reading 8 MB to scan 5 MB wastes the
            # budget it is trying to stay inside
            chunk = read_exact(f, min(chunk_size, read_limit - next_off))
            if not chunk:
                break
            combined = carry + chunk
            base = next_off - len(carry)
            carry_floor = len(carry)
            is_zero = chunk == zero_block(len(chunk))
            if is_zero:
                zero_bytes += len(chunk)
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
            lo = max(start, next_off)
            hi = min(end, next_off + len(chunk))
            if hi > lo:
                scanned += hi - lo
            next_off += len(chunk)
            if len(chunk) < chunk_size:
                break
            carry = combined[-carry_len:] if carry_len else b""
    finally:
        f.close()
    return {"hits": hits, "scanned": scanned, "zero_bytes": zero_bytes,
            "budget_hit": budget_hit, "job": (start, end)}


def parallel_phase1(target, patterns, start_offset, end_offset, chunk_size, workers,
                    min_gap, show_progress=True, deadline=None):
    """
    Split the source range into `workers` byte ranges and scan them in parallel
    processes (bytes.find holds the GIL, so threads would not help). Returns
    {"hits": merged hits with MIN_GAP applied in local order (what one
    sequential pass produced), "scanned": bytes actually read,
    "zero_bytes": all-zero bytes seen, "budget_hit": a worker ran out of time}.
    """
    import multiprocessing

    span = max(0, end_offset - start_offset)
    step = max(chunk_size, ((span + workers - 1) // workers // chunk_size) * chunk_size)
    jobs = []
    start = start_offset
    while start < end_offset:
        end = min(end_offset, start + step)
        jobs.append((target, patterns, start, end, chunk_size, deadline))
        start = end

    parts = []
    scanned = 0
    zero_bytes = 0
    budget_hit = False
    interrupted = False
    completed = set()
    ctx = multiprocessing.get_context("spawn")
    try:
        with ctx.Pool(processes=min(workers, len(jobs))) as pool:
            for done, part in enumerate(pool.imap_unordered(_scan_range_worker, jobs), 1):
                completed.add(tuple(part.get("job") or ()))
                parts.extend(part.get("hits") or [])
                scanned += part.get("scanned") or 0
                zero_bytes += part.get("zero_bytes") or 0
                budget_hit = budget_hit or bool(part.get("budget_hit"))
                if show_progress:
                    sys.stdout.write("\rParallel phase 1: %d / %d ranges done, %d hits   "
                                     % (done, len(jobs), len(parts)))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        # Ctrl+C lands in the parent process while the pool runs. The ranges
        # that already came back are real results: keep them, and never restart
        # the source from byte 0 (that would double-count and waste the read).
        interrupted = True
        print("\nStopped by the user - keeping the ranges that already finished.")
    except Exception as exc:
        # A worker died. The ranges that already finished are real results:
        # rescan ONLY the missing ones here, instead of throwing everything away
        # and re-reading the source from byte 0 (review S10).
        print("\nA parallel worker failed (%s) - rescanning the remaining ranges "
              "in this process." % exc)
        for job in jobs:
            span = (job[2], job[3])
            if span in completed:
                continue
            try:
                part = _scan_range_worker(job)
            except Exception as inner:
                print("  range %s-%s failed as well: %s" % (span[0], span[1], inner))
                continue
            completed.add(span)
            parts.extend(part.get("hits") or [])
            scanned += part.get("scanned") or 0
            zero_bytes += part.get("zero_bytes") or 0
            budget_hit = budget_hit or bool(part.get("budget_hit"))
    if show_progress:
        print()

    merged = sorted(parts, key=lambda h: h["offset"])
    last_hit = {}
    kept = []
    for hit in merged:
        if _reportable(last_hit, hit["label"], hit["offset"], min_gap):
            kept.append(hit)
    kept.sort(key=lambda h: (h["label"], h["offset"]))
    return {"hits": kept, "scanned": scanned, "zero_bytes": zero_bytes,
            "budget_hit": budget_hit, "interrupted": interrupted}


def scan(target, patterns, chunk_size, results_path, carve_dir=None,
         carve_before=4096, carve_after=65536, show_progress=True,
         min_gap=0, carve_limit=0, start_offset=0, end_offset=None,
         deadline=None, carved_counts=None):
    """
    Scan `target` for `patterns` = (label, bytes, ext, case_insensitive) tuples.
    `start_offset`/`end_offset` restrict the scan to one byte range (progressive
    scanning and resume, V5 Sections 3 and 4); hits before `start_offset` are
    dropped so each range owns its own hits. `deadline` (absolute time.time())
    stops the read early and sets summary["budget_hit"] - the fast scan's time
    budget (V5 Section 5).
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
    # a caller (progressive scanning) passes its own dict so CARVE_LIMIT is a
    # budget for the WHOLE run, not per slice - otherwise twenty slices allow
    # twenty times the files (review S11)
    carved_counts = {} if carved_counts is None else carved_counts
    scanned = 0
    zero_bytes = 0
    use_parallel = False
    interrupted = False
    budget_hit = False
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

        # ---- restrict the scan to [start_offset, end_offset) if requested ----
        start_offset = max(0, int(start_offset or 0))
        if end_offset is None or (total_size and end_offset > total_size):
            end_offset = total_size or None
        if end_offset is not None and end_offset <= start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        range_total = (end_offset - start_offset) if (end_offset and start_offset) else (end_offset or 0)

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
                found = parallel_phase1(target, patterns, start_offset, end_offset,
                                        chunk_size, workers, min_gap, show_progress,
                                        deadline=deadline)
                hits.extend(found["hits"])
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
                budget_hit = bool(found.get("budget_hit"))
                if found.get("interrupted"):
                    interrupted = True
                zero_bytes += found.get("zero_bytes") or 0
                # the workers count only the bytes inside their own range, so
                # this is exact for a full pass, a slice and a partial one
                scanned = found.get("scanned") or total_size
                use_parallel = True
            except KeyboardInterrupt:
                # defensive: keep the hits collected so far and never restart
                # the source from byte 0 after the user asked to stop
                interrupted = True
                use_parallel = True
                print("\nStopped by the user during the parallel pass - partial "
                      "results are kept.")
                log("parallel phase interrupted by the user")
            except Exception as exc:      # a broken pool must never lose the scan
                log("parallel phase failed: %s - falling back to one process" % exc)
                print("Parallel scan failed (%s) - falling back to one process." % exc)
                del hits[:]
                use_parallel = False
        log("mode         : %s" % ("parallel phase 1 (%d processes)" % workers
                                   if use_parallel else "single process"))
        if not use_parallel and workers > 1 and total_size and total_size < PARALLEL_MIN_SIZE:
            log("parallel     : off (source smaller than %s)" % fmt_bytes(PARALLEL_MIN_SIZE))

        if not use_parallel:
            aligned_start = (max(0, start_offset - carry_len) // DEVICE_ALIGNMENT) * DEVICE_ALIGNMENT
            f.seek(aligned_start)
            next_off = aligned_start

        # A bounded scan must read PAST end_offset by the longest signature: a
        # hit that starts just before the boundary only completes a few bytes
        # later, and the next range would reject it as "before my start" - that
        # is a dead band of max_len-1 bytes at every slice boundary.
        if end_offset is None:
            read_limit = None
        elif total_size:
            read_limit = min(end_offset + max_len, total_size)
        else:
            read_limit = end_offset + max_len

        try:
            while not use_parallel:
                if read_limit is not None and next_off >= read_limit:
                    break
                if deadline is not None and time.time() > deadline:
                    budget_hit = True
                    interrupted = True
                    break
                to_read = (chunk_size if read_limit is None
                           else min(chunk_size, read_limit - next_off))
                chunk = read_exact(f, to_read)
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
                    # each range owns [start_offset, end_offset): the over-read
                    # above finds boundary hits, and they belong to the NEXT range
                    if abs_off < start_offset:
                        continue
                    if end_offset is not None and abs_off >= end_offset:
                        continue
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

                # count only the bytes inside this range: the pre-roll before
                # start_offset and the over-read after end_offset are not part
                # of what this pass was asked to scan
                lo = max(start_offset, next_off)
                hi = next_off + len(chunk)
                if end_offset is not None:
                    hi = min(hi, end_offset)
                if hi > lo:
                    scanned += hi - lo
                next_off += len(chunk)
                update_progress()

                if len(chunk) < to_read:
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
            try:
                carved_files = carve_phase(f, hits, total_size, carve_dir,
                                           carve_before, carve_after, carve_limit,
                                           carved_counts, log, show_progress)
            except KeyboardInterrupt:
                interrupted = True
                print("\nStopped by the user while carving - the files written so "
                      "far are kept.")
                log("carving interrupted by the user")

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
            "budget_hit": budget_hit,
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
    """
    (file_count, total_bytes, [(filename, size), ...]) for the CARVED DATA only.
    Sidecars (.triage.txt) and readable transcripts (_readable.txt) are notes
    about a window, not recovered data - counting them doubled the totals.
    """
    items = []
    total = 0
    if carve_dir and os.path.isdir(carve_dir):
        for fname in sorted(os.listdir(carve_dir)):
            path = os.path.join(carve_dir, fname)
            if not os.path.isfile(path):
                continue
            if fname.endswith(".triage.txt") or fname.endswith(READABLE_SUFFIX):
                continue
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



def write_report(summary, names, report_path, carved=None):
    """`carved` = (count, bytes) measured BEFORE the files were moved, if known."""
    if carved is not None:
        count, total = carved
        items = []
    else:
        count, total, items = carved_stats(summary.get("carve_dir") or "")
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


PROBE_NAME = ".fixformat_write_test"


def is_network_path(path):
    """True for \\\\server\\share and //server/share."""
    text = str(path or "")
    return text.startswith("\\\\") or text.startswith("//")


def path_drive_letter(path):
    """'D:\\x' -> 'd'; a UNC share or anything else -> ''."""
    m = re.match(r"^([A-Za-z]):[\\/]", str(path or ""))
    return m.group(1).lower() if m else ""


def source_drive_letter(target):
    """The drive behind a scan target: a device path -> 'd'; a plain file path -> 'd'."""
    text = str(target or "")
    bs = chr(92)                      # built from a constant: no escaping traps
    device = bs + bs + "." + bs       # the Windows device namespace
    if text.startswith(device) and len(text) > len(device) + 1:
        letter = text[len(device)]
        if letter.isalpha() and text[len(device) + 1] == ":":
            return letter.lower()   # \\.\D: is a volume; \\.\PhysicalDrive0 is not
    return path_drive_letter(text)


def same_local_volume(target, path):
    """True when `path` is on the same drive letter as a volume target."""
    src = source_drive_letter(target)
    dst = path_drive_letter(path)
    return bool(src and dst and src == dst)


def validate_output_root(path):
    """
    Section 8: can recovered data be written to `path`?
    Returns (ok, message) - the message is the resolved path when ok, otherwise
    the reason. Creates the folder and proves it is writable with a probe file,
    so an unreachable share is refused BEFORE the scan instead of after hours.
    """
    text = str(path or "").strip().strip('"').strip("'")
    if not text:
        return False, "no output folder was given"
    root = os.path.expanduser(text)
    if not is_network_path(root):
        root = os.path.abspath(root)
    if os.path.exists(root) and not os.path.isdir(root):
        return False, "%s exists and is a file, not a folder" % root
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        return False, "cannot create %s: %s" % (root, exc)
    probe = os.path.join(root, PROBE_NAME)
    try:
        with open(probe, "wb") as fh:
            fh.write(b"ok")
        os.remove(probe)
    except OSError as exc:
        return False, "cannot write into %s: %s" % (root, exc)
    return True, root


def ask_output_root(target, default_root):
    """
    Section 8: where should the recovered data be written? Validated before the
    scan starts, so nothing is lost when the share is not there.
    Returns a resolved root (default_root when the user keeps the default).
    """
    if OUTPUT_ROOT.strip():
        usable, message = validate_output_root(OUTPUT_ROOT.strip())
        if usable:
            if same_local_volume(target, message):
                print()
                print("WARNING: OUTPUT_ROOT is on the SAME disk as the source (%s:)."
                      % source_drive_letter(target).upper())
                print("Writing there can destroy the data this scan is looking for.")
            print("Output root     : %s (OUTPUT_ROOT)" % message)
            return message
        print()
        print("WARNING: OUTPUT_ROOT (%s) is not usable: %s"
              % (OUTPUT_ROOT.strip(), message))
        print("Writing next to this script instead: %s" % default_root)
        return default_root
    print()
    print("=" * 78)
    print("WHERE SHOULD THE RECOVERED DATA BE WRITTEN?")
    print("=" * 78)
    print("  [1] Next to this script (default): %s" % default_root)
    print("  [2] Another folder or a network share - typed now")
    print()
    print("Writing onto the disk being scanned can overwrite the very data you")
    print("want back. An external disk or a share (\\\\PC\\RecoveredData) is safest.")
    print()
    answer, ok = _ask("Your choice [1]: ")
    if not ok or answer.strip() not in ("2",):
        return default_root
    while True:
        typed, ok_path = _ask("Folder or share: ")
        if not ok_path:
            return default_root
        typed = typed.strip().strip('"').strip("'")
        if not typed:
            return default_root
        usable, message = validate_output_root(typed)
        if usable:
            if same_local_volume(target, message):
                print()
                print("WARNING: that folder is on the SAME disk as the source (%s:)."
                      % source_drive_letter(target).upper())
                print("Writing there can destroy the data this scan is looking for.")
                confirm, ok_confirm = _ask("Use it anyway? [y/N]: ")
                if not ok_confirm or confirm.strip().lower() not in ("y", "yes"):
                    continue
            print("Output root     : %s" % message)
            remember_path("recovered", message)
            return message
        print()
        print("CANNOT USE THAT FOLDER: %s" % message)
        retry, ok_retry = _ask("Try another folder? [Y/n]: ")
        if not ok_retry or retry.strip().lower() in ("n", "no"):
            print("Writing next to this script instead: %s" % default_root)
            return default_root


def ask_save_location(work_dir, target=""):
    """
    Ask whether the two run folders should be moved to another disk or share.
    The chosen root is validated (Section 8) before anything is moved, and the
    results simply stay where they are when it is not usable.
    Returns the chosen root folder, or "" to keep them where they are.
    """
    print()
    print("Keep the results here, or move them to another disk?")
    print("Choose a folder on a DIFFERENT disk than the scanned one - writing")
    print("onto the damaged disk can overwrite the very data you want to recover.")
    print("A network share works too: \\\\PC\\RecoveredData")
    print()
    answer, ok = _ask("Move the results to another folder now? [y/N]: ")
    if not ok or answer.strip().lower() not in ("y", "yes"):
        print("Results kept in: %s" % work_dir)
        return ""
    root = pick_folder("the recovered data")
    if not root:
        print("Results kept in: %s" % work_dir)
        return ""
    usable, message = validate_output_root(root)
    if not usable:
        print()
        print("CANNOT USE THAT FOLDER: %s" % message)
        print("Results kept in: %s" % work_dir)
        return ""
    root = message
    if os.path.abspath(root) == os.path.abspath(work_dir):
        print("That is already the folder the results are in.")
        return ""
    if same_local_volume(target, root):
        print()
        print("WARNING: that folder is on the SAME disk as the source (%s:)."
              % source_drive_letter(target).upper())
        print("Writing there can destroy the data this scan is looking for. An")
        print("external disk or a share is safe; this move is done as you asked.")
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


def add_counts(base, extra):
    """Sum two {label: count} dicts without losing labels present in one only."""
    out = dict(base or {})
    for label, count in (extra or {}).items():
        out[label] = out.get(label, 0) + int(count or 0)
    return out


def cumulative_totals(prior, summary):
    """
    A resumed run continues a folder, so its provenance must hold the folder's
    TOTAL, not just this pass's numbers (review S7). `prior` is the provenance
    read before the run (empty for a fresh one).
    """
    prior = prior if isinstance(prior, dict) else {}
    return {
        "hits": int(prior.get("hits") or 0) + len(summary.get("hits") or []),
        "counts": add_counts(prior.get("counts") or {}, summary.get("counts") or {}),
        "carved_files": int(prior.get("carved_files") or 0)
                        + int(summary.get("carved_files") or 0),
        "bytes_scanned": int(prior.get("bytes_scanned") or 0)
                         + int(summary.get("bytes_scanned") or 0),
        "zero_bytes": int(prior.get("zero_bytes") or 0)
                      + int(summary.get("zero_bytes") or 0),
        "elapsed_seconds": round(float(prior.get("elapsed_seconds") or 0.0)
                                 + float(summary.get("elapsed") or 0.0), 1),
    }


def patterns_from_provenance(prov):
    """
    Rebuild the exact pattern list a run used (review S6).
    `signature_bytes` is authoritative: it carries the bytes, so renaming a
    label or flipping an encoding option can never change what a resumed half of
    the source is scanned with. Returns (patterns, warnings); an empty pattern
    list means "cannot be continued safely".
    """
    warnings = []
    if not isinstance(prov, dict):
        return [], ["no provenance was read"]
    stored = prov.get("signature_bytes")
    if isinstance(stored, list) and stored:
        patterns = []
        for entry in stored:
            try:
                pattern = bytes.fromhex(str(entry[1]))
                if not pattern:
                    raise ValueError("empty pattern")
                patterns.append((str(entry[0]), pattern, str(entry[2]),
                                 bool(entry[3])))
            except (IndexError, TypeError, ValueError):
                warnings.append("unreadable stored signature: %r" % (entry,))
        if not patterns:
            warnings.append("none of the stored signatures could be rebuilt")
        return patterns, warnings

    # provenance written before this key existed: labels only, so the current
    # config is the best available guess - and the user must be told
    labels = list(prov.get("signature_labels") or [])
    wanted = set(labels)
    patterns = [entry for entry in build_patterns(bool(prov.get("case_sensitive", True)))
                if not wanted or entry[0] in wanted]
    warnings.append("this recovery predates stored signature bytes: its patterns "
                    "were rebuilt from labels (%d rebuilt)" % len(patterns))
    missing = wanted - set(entry[0] for entry in patterns)
    if missing:
        warnings.append("%d label(s) no longer exist in the config: %s"
                        % (len(missing), ", ".join(sorted(missing)[:5])))
    if not patterns:
        warnings.append("no signature could be rebuilt")
    return patterns, warnings


def provenance_payload(target, total, summary, patterns, case_sensitive,
                       started_at, progress=None, profile="", profile_name="",
                       totals=None):
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
        # the bytes themselves (review S6): a resumed run must search with the
        # same patterns even if a label is renamed or the encoding list changes
        "signature_bytes": [[p[0], p[1].hex(), p[2], bool(p[3])] for p in patterns],
        "case_sensitive": bool(case_sensitive),
        "min_gap": MIN_GAP,
        "carve_before": CARVE_BEFORE,
        "carve_after": CARVE_AFTER,
        "chunk_size": CHUNK_SIZE,
        # empty when the built-in signature set was used (Section 7)
        "profile": profile or "",
        "profile_name": profile_name or "",
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
    if totals:
        # a continued run: the folder's total, not this pass alone
        payload.update(totals)
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

    # measure BEFORE moving: after the move carved/ is empty and the report
    # used to claim "0 files / 0 bytes" (review S5)
    carved_count, carved_bytes, _carved_items = carved_stats(carve_dir)

    moved = 0
    if os.path.isdir(carve_dir):
        for name in sorted(os.listdir(carve_dir)):
            src = os.path.join(carve_dir, name)
            if not os.path.isfile(src):
                continue
            is_sidecar = (name.endswith(".triage.txt")
                          or name.endswith(READABLE_SUFFIX))
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
        write_report(summary, names, summary_path, carved=(carved_count, carved_bytes))
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
# V5 Sections 3 and 4: resume a previous recovery, progressive slices
# ----------------------------------------------------------------------

PROGRESSIVE_PERCENT = 5          # slice size as a percentage of the source


def slice_bounds(total, percent, start):
    """(start, end) of the next slice; end == total on the last one."""
    if not total:
        return start, None
    span = max(SECTOR_SIZE * 2048, int(total * percent / 100.0))
    return start, min(total, start + span)


def merge_summaries(base, part):
    """Fold one slice's summary into the running one and return `base`."""
    base["hits"].extend(part.get("hits") or [])
    for label, count in (part.get("counts") or {}).items():
        base["counts"][label] = base["counts"].get(label, 0) + count
    for key in ("carved_files", "bytes_scanned", "zero_bytes"):
        base[key] = base.get(key, 0) + (part.get(key) or 0)
    base["elapsed"] = base.get("elapsed", 0.0) + (part.get("elapsed") or 0.0)
    base["interrupted"] = bool(part.get("interrupted"))
    base["carve_dir"] = part.get("carve_dir") or base.get("carve_dir")
    base["results_path"] = part.get("results_path") or base.get("results_path")
    return base


def ask_resume(force=False):
    """
    Section 3: offer to continue a previous recovery.
    Returns (target, provenance, resume_offset) or None for a fresh run.
    `force` is the explicit menu option: it explains itself when there is
    nothing to continue instead of returning silently.
    """
    found = find_recoveries()
    if not found:
        if force:
            print("No recovered_* folder was found next to this script.")
            print("Nothing to continue - start a scan with option 2 or 3.")
        return None
    print()
    print("-" * 78)
    print("A previous recovery was found - do you want to continue one?")
    print("-" * 78)
    for index, (path, prov) in enumerate(found[:15], 1):
        done = prov.get("resume_offset") or 0
        total = prov.get("source_size") or 0
        pct = ("%.1f%%" % (100.0 * done / total)) if total and done else "?"
        print("  [%d] %-44s %8s  %s" % (index, _shorten(os.path.basename(path), 44),
                                        pct, prov.get("status", "?")))
    print("-" * 78)
    print("  [0] Type the folder path manually")
    print("  [N] Start a new recovery instead")
    answer, ok = _ask("Your choice [N]: ")
    if not ok:
        return None
    answer = answer.strip()
    if not answer or answer.lower() in ("n", "q"):
        return None
    path = None
    if answer == "0":
        typed, ok_path = _ask("Recovered folder path: ")
        if not ok_path:
            return None
        typed = typed.strip().strip('"').strip("'")
        path = os.path.abspath(os.path.expanduser(typed)) if typed else None
    elif answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(found):
            path = found[index][0]
    if not path or not os.path.isdir(path):
        print("No usable folder chosen - starting a new recovery.")
        return None
    prov = read_provenance(path)
    if not prov or not prov.get("source"):
        print("That folder has no provenance.json - starting a new recovery.")
        return None
    done_total = int(prov.get("source_size") or 0)
    done_at = int(prov.get("resume_offset") or 0)
    if prov.get("status") == "complete" and done_at and (not done_total or done_at >= done_total):
        print()
        print("That recovery is already complete: %s of %s were scanned."
              % (fmt_bytes(done_at), fmt_bytes(done_total) if done_total else "the source"))
        print("Nothing is left to continue there. Start a new scan (option 2 or 3)")
        print("if you want to search the source again, with other signatures.")
        return None
    prov["recovered_dir"] = path
    remember_path("recovered", os.path.dirname(path))
    return prov["source"], prov, int(prov.get("resume_offset") or 0)


def run_progressive(target, total, patterns, case_sensitive, recovered_dir,
                    organized_dir, started_at, resume_from=0, tag="",
                    percent=PROGRESSIVE_PERCENT, profile="", profile_name=""):
    """
    Section 4: scan `target` in slices. After every slice the carved data is
    organized, provenance.json is updated with the offset to resume from, and
    the user is asked whether to take the next slice. Returns the combined
    summary (with "resume_offset" and "stopped_early").
    """
    carve_dir = os.path.join(recovered_dir, "carved") if ENABLE_CARVING else None
    results_path = os.path.join(recovered_dir, RESULTS_FILE)
    combined = {"target": target, "total_size": total, "hits": [], "counts": {},
                "carved_files": 0, "bytes_scanned": 0, "zero_bytes": 0,
                "elapsed": 0.0, "interrupted": False,
                "results_path": results_path, "carve_dir": carve_dir,
                "resume_offset": int(resume_from or 0), "stopped_early": False}

    start = max(0, int(resume_from or 0))
    if total and start >= total:
        print("This recovery already reached the end of the source (%s)." % fmt_bytes(total))
        combined["resume_offset"] = total
        return combined

    carved_totals = {}          # shared across slices: one budget per run
    # a resume continues an existing folder: read what it already holds so the
    # provenance keeps the total instead of resetting to this pass (review S7)
    prior_prov = read_provenance(recovered_dir) if resume_from else {}
    slice_no = 0
    while True:
        slice_no += 1
        end = slice_bounds(total, percent, start)[1]
        where = "%s - %s" % (fmt_bytes(start),
                             fmt_bytes(end) if end is not None else "end of media")
        print()
        print("=" * 78)
        print("SLICE %d - %s" % (slice_no, where))
        print("=" * 78)
        try:
            part = scan(target, patterns, CHUNK_SIZE, results_path, carve_dir,
                        CARVE_BEFORE, CARVE_AFTER, show_progress=True,
                        min_gap=MIN_GAP, carve_limit=CARVE_LIMIT,
                        start_offset=start, end_offset=end,
                        carved_counts=carved_totals)
        except KeyboardInterrupt:
            print("\nStopped by the user - everything found so far is kept.")
            combined["stopped_early"] = True
            break
        merge_summaries(combined, part)

        # organize this slice's files, then record where to continue from.
        # scan() catches Ctrl+C itself and returns normally with interrupted
        # set, so this - not an exception - is what "the user stopped" looks
        # like: the slice ends where the read stopped, not at its planned end.
        slice_interrupted = bool(part.get("interrupted"))
        organize_output(part, recovered_dir, organized_dir)
        if slice_interrupted:
            combined["resume_offset"] = start + int(part.get("bytes_scanned") or 0)
        else:
            combined["resume_offset"] = end if end is not None else (
                combined["bytes_scanned"] + start)
        finished = (not slice_interrupted) and (end is None or (total and end >= total))
        slice_totals = cumulative_totals(prior_prov, combined)
        combined["totals"] = slice_totals
        prov = provenance_payload(target, total, combined, patterns,
                                  case_sensitive, started_at,
                                  profile=profile, profile_name=profile_name,
                                  totals=slice_totals)
        prov.update({"run_id": tag, "recovered_dir": recovered_dir,
                     "organized_dir": organized_dir,
                     "resume_offset": combined["resume_offset"],
                     "status": "complete" if finished else "in progress"})
        write_provenance(recovered_dir, prov)

        print()
        print("Slice done: %d hits so far, %d files carved, saved at offset %s"
              % (len(combined["hits"]), combined["carved_files"],
                 fmt_bytes(combined["resume_offset"])))
        if slice_interrupted:
            print("Stopped by the user inside this slice - it is recorded as")
            print("interrupted at %s, so it can be continued from there."
                  % fmt_bytes(combined["resume_offset"]))
            combined["stopped_early"] = True
            break
        if finished:
            print("The whole source has been scanned.")
            break
        answer, ok = _ask("Continue with the next slice? [Y/n]: ")
        if not ok or answer.strip().lower() in ("n", "no", "q"):
            print("Stopped. Run the tool again and choose this recovery to continue.")
            combined["stopped_early"] = True
            break
        start = combined["resume_offset"]

    # final organized view + summary over everything found so far
    organize_output(combined, recovered_dir, organized_dir)
    run_totals = cumulative_totals(prior_prov, combined)
    combined["totals"] = run_totals
    prov = provenance_payload(target, total, combined, patterns, case_sensitive,
                              started_at, profile=profile, profile_name=profile_name,
                              totals=run_totals)
    prov.update({"run_id": tag, "recovered_dir": recovered_dir,
                 "organized_dir": organized_dir,
                 "resume_offset": combined["resume_offset"],
                 "status": ("complete" if not combined["stopped_early"]
                            else "interrupted")})
    write_provenance(recovered_dir, prov)
    return combined


# ----------------------------------------------------------------------
# fast scan (V5 Sections 5 and 9) and the pre-scan summary (Section 10)
# ----------------------------------------------------------------------

FAST_SCAN_PREFIX = "fastscan_"


def source_kind(target):
    """'device' for a raw Windows device, 'image' for a file."""
    return "device" if (IS_WINDOWS and str(target).startswith("\\\\.\\")) else "image"


def fast_scan_ranges(total, percent=None):
    """
    Section 5: the byte ranges a fast scan reads - the first `percent` and the
    last `percent` of the source. A source small enough for the two ranges to
    touch is read once, in full.
    """
    percent = FAST_SCAN_PERCENT if percent is None else float(percent)
    if not total:
        return [(0, None)]
    span = int(total * percent / 100.0)
    span = max(1, min(span, total))
    tail_start = total - span
    if tail_start <= span:
        return [(0, total)]
    return [(0, span), (tail_start, total)]


def _speed_path():
    return os.path.join(SCRIPT_DIR, SPEED_FILE)


def load_speed_rates():
    """{'device': bytes/s, 'image': bytes/s} - throughput measured here."""
    try:
        with open(_speed_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    rates = {}
    for key, value in data.items():
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            rates[key] = value
    return rates


def remember_speed(kind, rate, mode="full"):
    """
    Best-effort: store the throughput of this run (bytes/s).
    Keyed by kind AND mode: a fast scan reads two slices and carves nothing,
    while a full scan's elapsed includes carving, so mixing them made the
    pre-scan estimate up to ten times wrong (review S8).
    """
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return
    if rate <= 0:
        return
    db = load_speed_rates()
    db["%s:%s" % (kind, mode)] = round(rate, 1)
    try:
        with open(_speed_path(), "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2)
    except OSError:
        pass


def estimate_scan_seconds(nbytes, target, rates=None, mode="full"):
    """
    (seconds, measured) for reading `nbytes` of `target` in `mode` ("full" or
    "fast"). `measured` is False when this machine has no timed run yet and
    DEFAULT_SCAN_RATE is used - the pre-scan summary says which one it is
    instead of pretending.
    """
    if not nbytes:
        return 0.0, False
    rates = load_speed_rates() if rates is None else rates
    kind = source_kind(target)
    rate = rates.get("%s:%s" % (kind, mode)) or rates.get(kind)
    if not rate:
        return float(nbytes) / float(DEFAULT_SCAN_RATE), False
    return float(nbytes) / float(rate), True


def print_plan_summary(mode, target, total, patterns, case_sensitive,
                       coverage_bytes=None, budget=None, extra=(),
                       speed_mode="full"):
    """
    Section 10: one screen with the mode, the source, how much will be read and
    how long it should take - the confirmation that used to be a bare Y/N.
    """
    print("=" * 78)
    print("PLAN FOR THIS RUN")
    print("=" * 78)
    print("  mode        : %s" % mode)
    print("  source      : %s" % target)
    if total:
        print("  source size : %s (%s bytes)" % (fmt_bytes(total), fmt_int(total)))
    else:
        print("  source size : unknown (reads until end of media)")
    if coverage_bytes:
        if total:
            print("  will read   : %s (%.1f%% of the source)"
                  % (fmt_bytes(coverage_bytes), 100.0 * coverage_bytes / total))
        else:
            print("  will read   : %s" % fmt_bytes(coverage_bytes))
    print("  signatures  : %d patterns" % len(patterns))
    print("  case        : %s" % ("sensitive (exact match)" if case_sensitive
                                  else "insensitive (ignore case)"))
    print("  read block  : %s" % fmt_bytes(CHUNK_SIZE))
    if coverage_bytes:
        seconds, measured = estimate_scan_seconds(coverage_bytes, target,
                                                  mode=speed_mode)
        print("  est. time   : about %s (%s)"
              % (fmt_hms(seconds), "measured on this machine" if measured
                 else "default %.0f MB/s, nothing measured yet"
                 % (DEFAULT_SCAN_RATE / (1024.0 * 1024.0))))
    if budget:
        print("  time budget : %s hard stop, partial results are kept" % fmt_hms(budget))
    for key, value in extra:
        print("  %-13s : %s" % (key, value))
    print()
    print("Reading is read-only: nothing on the source is modified.")
    print()


def make_fast_scan_dir(base_dir, target, total):
    """fastscan_<label>_<size>_<stamp>/ for the fast-scan log and report."""
    tag = run_tag(target, total)
    path = os.path.join(base_dir, FAST_SCAN_PREFIX + tag)
    os.makedirs(path, exist_ok=True)
    return path, tag


def fast_sample_names(target, hits, count=None, before=None, after=None):
    """
    File-name-like strings from small windows around a sample of the hits: the
    fast scan saves no carved files (Section 5), so names are read on the fly.
    """
    count = FAST_SCAN_SAMPLE_HITS if count is None else max(1, int(count))
    before = FAST_SCAN_WINDOW_BEFORE if before is None else before
    after = FAST_SCAN_WINDOW_AFTER if after is None else after
    if not hits:
        return []
    step = max(1, len(hits) // count)
    found = {}
    try:
        f = open(target, "rb", buffering=0)
    except OSError:
        return []
    try:
        for hit in hits[::step][:count]:
            start = max(0, hit["offset"] - before)
            blob = read_window(f, start, hit["offset"] + after)
            if not blob:
                continue
            for text in (blob.decode("latin-1", "ignore"),
                         blob.decode("utf-16-le", "ignore")):
                for match in NAME_RE.finditer(text):
                    found[match.group(0)] = found.get(match.group(0), 0) + 1
    except OSError:
        pass
    finally:
        f.close()
    return sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[:40]


def fast_scan_projection(summary, total, sampled_bytes):
    """
    Scale the sampled density up to the whole source (Section 5, "estimated
    total recoverable size"). This is a projection, not a measurement, and
    CARVE_LIMIT caps each signature exactly as a full scan would.
    """
    hits = summary.get("hits") or []
    if not sampled_bytes or not total:
        return {"factor": 1.0, "per_label": {}, "projected_hits": len(hits),
                "projected_bytes": 0.0, "usable": False}
    factor = float(total) / float(sampled_bytes)
    per_label = {}
    for label, count in (summary.get("counts") or {}).items():
        if count:
            per_label[label] = count * factor
    window = CARVE_BEFORE + CARVE_AFTER
    projected = 0.0
    for label, estimate in per_label.items():
        capped = min(estimate, CARVE_LIMIT) if CARVE_LIMIT else estimate
        projected += capped * window
    return {"factor": factor, "per_label": per_label,
            "projected_hits": int(round(len(hits) * factor)),
            "projected_bytes": projected, "usable": True}


def fast_report_lines(summary, sampled_bytes, ranges, projection, names):
    """The whole fast-scan output, used both on screen and in the report file."""
    hits = summary.get("hits") or []
    zero_bytes = summary.get("zero_bytes") or 0
    zero_pct = (100.0 * zero_bytes / sampled_bytes) if sampled_bytes else 0.0
    lines = []
    lines.append("=" * 78)
    lines.append("FAST SCAN RESULT - QUICK TRIAGE")
    lines.append("=" * 78)
    lines.append("  source        : %s" % summary.get("target"))
    if summary.get("total_size"):
        lines.append("  source size   : %s" % fmt_bytes(summary["total_size"]))
    lines.append("  sampled       : %s in %d region(s)"
                 % (fmt_bytes(sampled_bytes), len(ranges)))
    for start, end in ranges:
        lines.append("                  %s - %s"
                     % (fmt_bytes(start),
                        fmt_bytes(end) if end is not None else "end of media"))
    lines.append("  time          : %s%s"
                 % (fmt_hms(summary.get("elapsed") or 0.0),
                    "  (BUDGET REACHED - results are partial)"
                    if summary.get("budget_hit") else ""))
    lines.append("  hits          : %d" % len(hits))
    rows = [(label, count) for label, count in (summary.get("counts") or {}).items()
            if count]
    if rows:
        lines.append("  per signature :")
        for label, count in sorted(rows, key=lambda kv: -kv[1]):
            lines.append("      %-44s %d" % (_shorten(label, 44), count))
    else:
        lines.append("  per signature : none")
    lines.append("  zero regions  : %.1f%% of the sampled bytes (%s)"
                 % (zero_pct, fmt_bytes(zero_bytes)))
    if names:
        lines.append("  sample names  : %s"
                     % ", ".join(name for name, _ in names[:10]))
    sample_too_small = bool(summary.get("budget_hit")) and (
        sampled_bytes < max(SECTOR_SIZE, int((summary.get("total_size") or 0) / 100)))
    if projection.get("usable") and not sample_too_small:
        lines.append("")
        lines.append("  PROJECTION if the rest of the source has the same density:")
        lines.append("    hits on the whole source : about %d"
                     % projection["projected_hits"])
        lines.append("    carved-window bytes     : about %s (window %s per hit,"
                     " %s per signature; smart text can grow a window)"
                     % (fmt_bytes(projection["projected_bytes"]),
                        fmt_bytes(CARVE_BEFORE + CARVE_AFTER),
                        CARVE_LIMIT if CARVE_LIMIT else "no limit"))
    lines.append("")
    lines.append("-" * 78)
    if sample_too_small:
        lines.append("RECOMMENDATION: the %s budget ran out after %s of the source -"
                     % (fmt_hms(FAST_SCAN_BUDGET), fmt_bytes(sampled_bytes)))
        lines.append("  that is too little to judge anything. Raise FAST_SCAN_BUDGET or")
        lines.append("  run the full scan (menu option 2). This is NOT a wiped-disk verdict.")
    elif not hits:
        lines.append("RECOMMENDATION: no signatures found in the sampled regions.")
        if zero_pct >= 90.0:
            lines.append("  %.1f%% of the sampled bytes are zeros - that is the pattern of a"
                         % zero_pct)
            lines.append("  wiped, encrypted or TRIMmed (SSD) disk. A full scan is the")
            lines.append("  only way to be sure, but expect little or nothing to recover.")
        else:
            lines.append("  The disk may be wiped, encrypted or TRIMmed (SSD), or it may")
            lines.append("  hold file types whose signatures are not configured here.")
    elif len(hits) > FAST_SCAN_HIT_THRESHOLD:
        lines.append("RECOMMENDATION: %d hits in %s of a %s source - run a FULL SCAN"
                     % (len(hits), fmt_bytes(sampled_bytes),
                        fmt_bytes(summary.get("total_size") or 0)))
        lines.append("  (main menu option 2) to recover everything.")
    else:
        lines.append("RECOMMENDATION: %d hits is under the fast-scan threshold (%d), but"
                     % (len(hits), FAST_SCAN_HIT_THRESHOLD))
        lines.append("  data is there - run a FULL SCAN (menu option 2) to recover it.")
    lines.append("-" * 78)
    return lines


def run_fast_scan(target, total, patterns, case_sensitive, budget=None,
                  percent=None, base_dir=None, profile=None, profile_name=""):
    """
    Section 5: read the first and last `percent` of the source inside a hard
    time budget, then report counts, sample names and a projection. No carved
    files are written - being fast is the point.
    Returns the combined summary plus "sampled_bytes", "ranges", "fast_dir",
    "names", "projection" and "report_path".
    """
    budget = FAST_SCAN_BUDGET if budget is None else budget
    percent = FAST_SCAN_PERCENT if percent is None else percent
    base_dir = base_dir or SCRIPT_DIR
    fast_dir, _tag = make_fast_scan_dir(base_dir, target, total)
    results_path = os.path.join(fast_dir, RESULTS_FILE)
    ranges = fast_scan_ranges(total, percent)
    started = time.time()
    deadline = (started + budget) if budget else None

    summary = {"target": target, "total_size": total, "hits": [],
               "counts": dict((p[0], 0) for p in patterns),
               "carved_files": 0, "bytes_scanned": 0, "zero_bytes": 0,
               "elapsed": 0.0, "interrupted": False, "budget_hit": False,
               "results_path": results_path, "carve_dir": None}

    for index, (start, end) in enumerate(ranges, 1):
        if deadline is not None and time.time() >= deadline:
            summary["budget_hit"] = True
            break
        print()
        print("=" * 78)
        print("FAST SCAN - region %d of %d : %s - %s"
              % (index, len(ranges), fmt_bytes(start),
                 fmt_bytes(end) if end is not None else "end of media"))
        print("=" * 78)
        part = scan(target, patterns, CHUNK_SIZE, results_path, None,
                    FAST_SCAN_WINDOW_BEFORE, FAST_SCAN_WINDOW_AFTER,
                    show_progress=True, min_gap=MIN_GAP, carve_limit=0,
                    start_offset=start, end_offset=end, deadline=deadline)
        merge_summaries(summary, part)
        summary["budget_hit"] = bool(part.get("budget_hit"))
        if summary["budget_hit"]:
            break

    summary["elapsed"] = time.time() - started
    sampled = summary.get("bytes_scanned") or 0
    summary["sampled_bytes"] = sampled
    summary["ranges"] = ranges
    summary["fast_dir"] = fast_dir
    summary["names"] = fast_sample_names(target, summary["hits"])
    summary["projection"] = fast_scan_projection(summary, total, sampled)

    lines = fast_report_lines(summary, sampled, ranges, summary["projection"],
                              summary["names"])
    print()
    print("\n".join(lines))

    report_path = os.path.join(fast_dir, FAST_SCAN_REPORT)
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("FAST SCAN REPORT - %s\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("\n".join(lines) + "\n")
    except OSError:
        report_path = ""
    summary["report_path"] = report_path

    prov = provenance_payload(target, total, summary, patterns, case_sensitive,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              profile=(PROFILE_LAST_PATH if profile is None
                                       else profile) or "",
                              profile_name=profile_name or "")
    prov.update({"mode": "fast scan",
                 "sampled_bytes": sampled,
                 "ranges": [[start, end] for start, end in ranges],
                 "fast_scan_dir": fast_dir,
                 "report": report_path,
                 "budget_seconds": budget,
                 "status": "partial (budget)" if summary["budget_hit"] else "complete"})
    write_provenance(fast_dir, prov)
    return summary


def run_fast_flow(target, total):
    """Menu option 1: the whole fast-scan conversation (Section 9)."""
    print()
    print("Selected target : %s" % target)
    if total:
        print("Size            : %s (%s bytes, %s sectors)"
              % (fmt_bytes(total), fmt_int(total), fmt_int(total // SECTOR_SIZE)))
    else:
        print("Size            : unknown (will read until end of media)")
    print()
    print("A fast scan reads two small strategic slices of the source - the")
    print("beginning (partition table, boot sector, often the MFT) and the end")
    print("(backup structures, recently deleted data) - and tells you in minutes")
    print("whether a full scan is worth the hours. Nothing is written to the")
    print("source, and no recovered files are saved: this is a triage pass.")
    print()
    print()
    try:
        pattern_source, profile, profile_name = ask_signature_source()
    except ValueError as exc:
        print()
        print("SIGNATURE ERROR: %s" % exc)
        print("Nothing was scanned.")
        sys.exit(2)
    if profile is not None:
        try:
            patterns = profile_patterns(profile, CASE_SENSITIVE)
            print()
            print("  %s" % profile_summary(profile, profile_name))
        except ValueError as exc:
            print("SIGNATURE ERROR: %s" % exc)
            sys.exit(2)
    else:
        try:
            patterns = build_patterns(CASE_SENSITIVE)
        except ValueError as exc:
            print("CONFIG ERROR: %s" % exc)
            sys.exit(2)
    if not patterns:
        print("CONFIG ERROR: no signatures are configured - nothing to search for.")
        sys.exit(2)

    ranges = fast_scan_ranges(total, FAST_SCAN_PERCENT)
    coverage = sum((end - start) for start, end in ranges if end is not None)
    if not total:
        coverage = 0
    print_plan_summary(
        "FAST SCAN - quick triage of the first and last %g%%" % FAST_SCAN_PERCENT,
        target, total, patterns, CASE_SENSITIVE,
        coverage_bytes=coverage, budget=FAST_SCAN_BUDGET, speed_mode="fast",
        extra=(("signature set", profile_summary(profile, profile_name)
                if profile is not None else "built-in signatures"),
               ("regions", "%d (%s)" % (len(ranges),
                                        "first + last" if len(ranges) > 1 else "whole source")),
               ("carving", "off - summary only, no files saved")))
    if not confirm_start():
        print("Stopped. Nothing was scanned.")
        sys.exit(0)

    print()
    print("=" * 78)
    print("SCANNING (hard stop after %s)" % fmt_hms(FAST_SCAN_BUDGET))
    print("=" * 78)
    print("Press Ctrl+C to stop early - everything found so far is kept.")
    print("-" * 78)
    try:
        summary = run_fast_scan(
            target, total, patterns, CASE_SENSITIVE,
            profile=(PROFILE_LAST_PATH if profile is not None else ""),
            profile_name=profile_name)
    except PermissionError:
        print("\nERROR: access denied.")
        print("Raw device reads require administrator / SYSTEM rights.")
        print("Re-open the command prompt as Administrator and retry.")
        sys.exit(3)
    except OSError as exc:
        print("\nERROR: %s" % exc)
        sys.exit(1)

    try:
        if summary.get("sampled_bytes") and summary.get("elapsed"):
            remember_speed(source_kind(target),
                           summary["sampled_bytes"] / max(0.001, summary["elapsed"]),
                           mode="fast")
    except Exception:
        pass

    print()
    print("fast scan folder : %s" % summary["fast_dir"])
    if summary.get("report_path"):
        print("  report         : %s" % summary["report_path"])
        print("  hits log       : %s" % summary["results_path"])
        print("  provenance     : %s"
              % os.path.join(summary["fast_dir"], PROVENANCE_FILE))
    print()
    print("Carved files are NOT produced by a fast scan. If the recommendation")
    print("says so, choose option 2 (FULL SCAN) or option 3 (PROGRESSIVE SCAN).")


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


def run_search_flow(target, total, mode="full"):
    """Menu options 2, 3 and 5: choose signatures, confirm, scan, report, save."""
    progressive_mode = (mode == "progressive")
    print()
    print("Selected target : %s" % target)
    if total:
        print("Size            : %s (%d bytes, %s sectors)"
              % (fmt_bytes(total), total, fmt_int(total // SECTOR_SIZE)))
    else:
        print("Size            : unknown (will scan until end of media)")

    # ---------------- STEP 2 (Section 7: built-in set or a JSON profile) ----
    try:
        pattern_source, profile, profile_name = ask_signature_source()
    except ValueError as exc:
        print()
        print("SIGNATURE ERROR: %s" % exc)
        print("Nothing was scanned. Fix the file (or choose the built-in set)")
        print("and run the tool again.")
        sys.exit(2)

    profile_path = PROFILE_LAST_PATH if profile is not None else ""
    if profile is not None:
        print()
        print("The extension menu is skipped: the profile IS the filter.")
        print("  %s" % profile_summary(profile, profile_name))
        exts = set(profile)
    else:
        print()
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
    if profile is not None:
        try:
            patterns = profile_patterns(profile, case_sensitive)
        except ValueError as exc:
            print("PROFILE ERROR: %s" % exc)
            sys.exit(2)
    else:
        patterns = filter_patterns_by_extension(build_patterns(case_sensitive), exts)
    if not patterns:
        print("CONFIG ERROR: those choices leave no signature to search for.")
        sys.exit(2)

    # ---------------- STEP 4 ----------------
    print()
    print("=" * 78)
    print("STEP 4 of 5 - CONFIRM YOUR CHOICES")
    print("=" * 78)
    print("  extensions  : %s" % ", ".join("." + e for e in sorted(exts)))
    print()
    work_root = ask_output_root(target, SCRIPT_DIR)
    print()
    if progressive_mode:
        plan_mode = "PROGRESSIVE SCAN - %d%% slices, a checkpoint after each one" % PROGRESSIVE_PERCENT
    else:
        plan_mode = "FULL SCAN - one complete pass over the source"
    if profile is not None:
        sig_row = profile_summary(profile, profile_name)
    else:
        sig_row = "built-in signatures"
    plan_extra = [("signature set", sig_row),
                  ("file types", ", ".join("." + e for e in sorted(exts))),
                  ("output root", work_root)]
    print_plan_summary(plan_mode, target, total, patterns, case_sensitive,
                       coverage_bytes=total or None, extra=tuple(plan_extra))
    if not confirm_start():
        print("Stopped. Nothing was scanned.")
        sys.exit(0)

    if progressive_mode:
        progressive = True
        print()
        print("This run scans in %d%% slices with a checkpoint after each one."
              % PROGRESSIVE_PERCENT)
    else:
        slice_answer, slice_ok = _ask("Scan in 5%% slices with a checkpoint after each? [y/N]: ")
        progressive = slice_ok and slice_answer.strip().lower() in ("y", "yes")

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
    try:
        recovered_dir, organized_dir, run_id = make_run_dirs(work_root, target, total)
    except OSError as exc:
        print()
        print("ERROR: cannot create the run folders in %s (%s)" % (work_root, exc))
        print("Nothing was scanned. Pick a writable output root and run again.")
        sys.exit(1)
    results_path = os.path.join(recovered_dir, RESULTS_FILE)
    carve_dir = os.path.join(recovered_dir, "carved") if ENABLE_CARVING else None
    print("results folder  : %s" % recovered_dir)

    try:
        if progressive:
            print("Slices of %d%% with a checkpoint after each one." % PROGRESSIVE_PERCENT)
            summary = run_progressive(target, total, patterns, case_sensitive,
                                      recovered_dir, organized_dir, started_at,
                                      tag=run_id,
                                      profile=profile_path or "",
                                      profile_name=profile_name if profile else "")
        else:
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

    # remember the throughput of this run: the pre-scan time estimate for the
    # next scan is built from it (Section 10)
    try:
        if summary.get("bytes_scanned") and summary.get("elapsed"):
            remember_speed(source_kind(target),
                           summary["bytes_scanned"] / max(0.001, summary["elapsed"]),
                           mode="full")
    except Exception:
        pass

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
                                    case_sensitive, started_at,
                                    profile=profile_path or "",
                                    profile_name=profile_name if profile else "")
    provenance["run_id"] = run_id
    provenance["recovered_dir"] = recovered_dir
    provenance["organized_dir"] = organized_dir
    provenance["results_log"] = results_path
    # a finished full scan has no resume point: say where it ended, so the
    # resume list shows 100% and refuses to redo it (Section 3)
    provenance["resume_offset"] = int(summary.get("bytes_scanned") or total or 0)
    write_provenance(recovered_dir, provenance)

    # organized browsable output (Section 2). If it fails (read-only root, disk
    # full, share gone) the carved files are still in recovered_dir - say so
    # instead of dying with a traceback after hours of scanning.
    try:
        moved, index_path, summary_path = organize_output(
            summary, recovered_dir, organized_dir, names)
    except OSError as exc:
        print()
        print("WARNING: could not build the organized folder (%s)." % exc)
        print("The recovered files are safe in: %s" % recovered_dir)
        moved, index_path, summary_path = 0, "", ""
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

    # ask WHERE to keep the two run folders (Section 1/2 keep them siblings).
    # A root chosen in STEP 4 (Section 8) is already the final home.
    final_root = ""
    if os.path.abspath(work_root) == os.path.abspath(SCRIPT_DIR):
        final_root = ask_save_location(SCRIPT_DIR, target)
    else:
        print()
        print("SAVED TO: %s" % work_root)
        print("  %s" % recovered_dir)
        print("  %s" % organized_dir)
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
    """Menu option 4: create a full image of a disk in one fast read."""
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
        print("Next step: run this tool again and choose option 5 to analyze the")
        print("image - the original disk will not be touched again.")


def resume_flow(resumed):
    """Section 3: continue a previous recovery, in place (menu option 6)."""
    resume_target, resume_prov, resume_offset = resumed
    resume_total = int(resume_prov.get("source_size") or 0)
    resume_case = bool(resume_prov.get("case_sensitive", True))
    resume_patterns, resume_warnings = patterns_from_provenance(resume_prov)
    for warning in resume_warnings:
        print("NOTE: %s" % warning)
    if not resume_patterns:
        print()
        print("This recovery cannot be continued safely: its signature set")
        print("cannot be rebuilt. Start a new scan (option 2 or 3) instead -")
        print("the old folder is left untouched.")
        return
    recovered_dir = resume_prov.get("recovered_dir") or ""
    organized_dir = resume_prov.get("organized_dir") or ""
    if not organized_dir or not os.path.isdir(organized_dir):
        organized_dir = os.path.join(
            os.path.dirname(recovered_dir),
            os.path.basename(recovered_dir).replace(RECOVERED_PREFIX,
                                                    ORGANIZED_PREFIX, 1))
        os.makedirs(organized_dir, exist_ok=True)
    print()
    print("Continuing %s from %s with %d signatures"
          % (resume_target, fmt_bytes(resume_offset), len(resume_patterns)))
    combined = run_progressive(resume_target, resume_total, resume_patterns,
                               resume_case, recovered_dir, organized_dir,
                               resume_prov.get("started") or "",
                               resume_from=resume_offset,
                               tag=resume_prov.get("run_id", ""),
                               profile=resume_prov.get("profile") or "",
                               profile_name=resume_prov.get("profile_name") or "")
    totals = combined.get("totals") or {}
    print()
    print("Recovery updated: %d hits in this folder (%d carved), resume offset %s"
          % (totals.get("hits", len(combined["hits"])),
             totals.get("carved_files", combined["carved_files"]),
             fmt_bytes(combined["resume_offset"])))
    print("  recovered : %s" % recovered_dir)
    print("  organized : %s" % organized_dir)


def choose_source(image_only=False):
    """
    Ask whether the scan reads a physical disk or an existing image, then return
    (target, size). Everything is picked from a list - nothing is typed.
    """
    if not image_only:
        print("=" * 78)
        print("STEP 1 of 5 - CHOOSE WHAT TO SCAN")
        print("=" * 78)
        print("  [1] A PHYSICAL DISK      - read raw and read-only (needs Administrator)")
        print("  [2] AN EXISTING IMAGE    - an .img created earlier, or any image file")
        print()
        answer, ok = _ask("Your choice [1]: ")
        if not ok:
            return None, 0
        if answer.strip() == "2":
            image_only = True
        print()
    if image_only:
        print("=" * 78)
        print("STEP 1 of 5 - CHOOSE THE DISK IMAGE")
        print("=" * 78)
        print("Pick the image from the list below, or browse to its folder.")
        print("Analyzing an image does not touch the original disk at all.")
        print()
        return ask_image_file()
    print("=" * 78)
    print("STEP 1 of 5 - CHOOSE THE DISK TO SCAN")
    print("=" * 78)
    if TARGET.strip():
        target = TARGET.strip()
        print("Using the disk fixed in TARGET at the bottom of this script.")
        try:
            return target, open_target(target)
        except PermissionError:
            print("ERROR: access denied opening %s - run as Administrator." % target)
            sys.exit(3)
        except OSError as exc:
            print("ERROR: %s" % exc)
            sys.exit(1)
    print("The machine may contain several disks. Every disk is listed below with")
    print("its exact size and sector count so you can identify the right one.")
    print()
    return ask_target()


def main():
    print("raw_scan.py v%s - raw sector signature scanner and disk imager" % VERSION)
    print("          (read-only for every disk it reads, fully offline)")
    print("script dir : %s" % SCRIPT_DIR)
    print()
    print("This tool never writes to the disk it reads. Every step below is")
    print("explained on screen and asks you what to do.")
    print()
    resumed = ask_resume()
    if resumed:
        resume_flow(resumed)
        return

    print("=" * 78)
    print("MAIN MENU - CHOOSE WHAT TO DO")
    print("=" * 78)
    print("  1 - FAST SCAN (quick triage)")
    print("      Reads only the first 5% and the last 5% of the source, inside a")
    print("      5 minute budget, and tells you whether a full scan is worth it.")
    print("      Saves no files: it is an assessment, not a recovery.")
    print()
    print("  2 - FULL SCAN (complete pass)")
    print("      Reads the whole source once and carves every hit. Slow, but it")
    print("      leaves nothing behind.")
    print()
    print("  3 - PROGRESSIVE SCAN (chunked, 5% at a time)")
    print("      The same scan, stopped after every 5% with a checkpoint, so it")
    print("      can be continued later (option 6) without starting over.")
    print()
    print("  4 - CREATE DISK IMAGE  (recommended before scanning a real disk)")
    print("      Copies every byte of the disk into one .img file. The disk is")
    print("      read once; the image can then be scanned again and again.")
    print()
    print("  5 - ANALYZE EXISTING IMAGE")
    print("      Searches an image file (created by option 4, or any .img) for")
    print("      signatures and recovers data from it.")
    print()
    print("  6 - RESUME PREVIOUS RECOVERY")
    print("      Continues any recovered_* folder from the offset it stopped at,")
    print("      in place, with its own signatures - nothing is rescanned.")
    print()
    print("Best practice: option 4 once on the damaged disk, then option 5 on")
    print("the image, then option 2 on the image if it promises data.")
    print()
    while True:
        try:
            choice = input("Your choice [1-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("No choice made. Nothing was done.")
            sys.exit(0)
        if choice in ("1", "2", "3", "4", "5", "6"):
            break
        print("Please type a number from 1 to 6.")
    print()

    if choice == "4":
        run_imaging_flow()
        return
    if choice == "6":
        resumed = ask_resume(force=True)
        if not resumed:
            return
        resume_flow(resumed)
        return
    if choice == "5":
        target, total = choose_source(image_only=True)
        if not target:
            print("No image selected. Nothing was scanned.")
            sys.exit(0)
        run_search_flow(target, total, mode="full")
        return

    target, total = choose_source()
    if not target:
        print("No target selected. Nothing was scanned.")
        sys.exit(0)
    if choice == "1":
        run_fast_flow(target, total)
    elif choice == "3":
        run_search_flow(target, total, mode="progressive")
    else:
        run_search_flow(target, total, mode="full")


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

# Read block size for imaging (menu option 4). The disk is read once, so big
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
# Smart text carving keeps extending a window while the bytes look readable, up
# to this many extra bytes per hit. On structured text-like media every text hit
# can grow to the cap, so a 64 MB source produced 767 MB of carved data in the
# step-7 benchmark: lower this on a small recovery disk.
SMART_TEXT_MAX_EXTRA = 1024 * 1024
# Write a readable UTF-8 transcript next to each text window. It doubles the
# bytes written for text hits and destroys no evidence either way - turn it off
# when space is short.
ENABLE_READABLE_COPY = True
# Suffix of the readable UTF-8 transcript written next to a text window. It is
# NOT the carved data: the .txt bytes on disk are the recovered truth.
READABLE_SUFFIX = "_readable.txt"
# Phase 2 reads the source forward in blocks of this size and serves every carve
# window from the block in memory (one read per 4 MB instead of a seek per hit).
CARVE_BLOCK_SIZE = 4 * 1024 * 1024
# Phase 1 scans independent byte ranges in separate processes: bytes.find holds
# the GIL, so threads would not help. None = auto (up to 4 processes),
# 1 = always sequential. Small sources stay sequential.
PARALLEL_WORKERS = None
PARALLEL_MIN_SIZE = 64 * 1024 * 1024

# ---- fast scan (V5 Section 5, menu option 1) --------------------------------
# Coverage: the first FAST_SCAN_PERCENT and the last FAST_SCAN_PERCENT of the
# source. FAST_SCAN_BUDGET is a hard wall-clock stop: the scan returns what it
# has, marks the run partial and says so in the report.
FAST_SCAN_PERCENT = 5.0
FAST_SCAN_BUDGET = 5 * 60
# Carve window used by the fast scan. It saves no files, these only bound the
# windows read to pick sample file names (2 KB before / 16 KB after).
FAST_SCAN_WINDOW_BEFORE = 2 * 1024
FAST_SCAN_WINDOW_AFTER = 16 * 1024
# Above this many hits in the sampled regions the report recommends a full scan.
FAST_SCAN_HIT_THRESHOLD = 50
# How many hit windows are read to show sample file names.
FAST_SCAN_SAMPLE_HITS = 20
FAST_SCAN_REPORT = "fast_scan_report.txt"

# Throughput memory for the pre-scan time estimate (Section 10). The last
# measured run on THIS machine is stored here; with no measurement the estimate
# uses DEFAULT_SCAN_RATE and says so on screen instead of pretending.
SPEED_FILE = "scan_speed.json"
DEFAULT_SCAN_RATE = 60 * 1024 * 1024

# Where recovered data is written (V5 Section 8). Empty = asked during the run
# (default: next to this script). Set it to an external disk or a network share
# and it is validated BEFORE the scan starts; when it is not usable the run
# falls back to the script folder instead of failing after hours of scanning.
OUTPUT_ROOT = ""

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
