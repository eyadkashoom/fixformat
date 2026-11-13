#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_recovery_pipeline.py - the hybrid pipeline glue for fixformat v7.

Combines the three layers of v7/TextRecoveryGuide.md in the documented
order:

  Layer 1  metadata    text_recovery.recover_mft  (NTFS $MFT)
                       fat32_recovery.recover_fat (FAT32 directory entries)
  Layer 2  strings     text_classify.extract_text_runs
  Layer 3  text check  text_classify.classify     (entropy <= 4 bit/byte AND
                       printable >= 70%; soft markers only raise confidence)

Layer 1 runs first because it recovers original names and sizes. When the
metadata layer is absent (unknown filesystem) or finds nothing (MFT or FAT
entries overwritten), Layer 2+3 runs over the raw bytes as the documented
fallback (الاستنتاج: MFT overwritten -> Strings extraction). The strings
layer honours the guide's noise caps: at most 1000 files and 64 MiB total,
and every run must pass the Layer 3 gate before it is kept -- that is what
rejects base64-of-random and BitLocker-like noise.

This module is the ONLY place the three independent recovery modules meet;
none of them imports another. Like every sibling it is read-only on the
source: bytes are written only under --out.

Public API
----------
    detect_filesystem(source_path, offset=0) -> 'ntfs' | 'fat32' | 'fat1x' |
                                               'unknown'
    scan_strings(source_path, offset=0, length=None, out_dir=None, ...)
    recover(source_path, out_dir=None, fs='auto', offset=0, ...)
    main(argv) -> exit code

CLI
---
    python text_recovery_pipeline.py <image | device> [--out DIR]
        [--fs auto|ntfs|fat32] [--offset BYTES] [--volume-limit BYTES]
        [--limit N] [--strings] [--no-carve]
"""

import os
import sys

try:
    import text_recovery
except ImportError:  # pragma: no cover - embedded interpreter runs from root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import text_recovery
import fat32_recovery
import text_classify
import text_ngram

__all__ = [
    "detect_filesystem", "scan_strings", "recover",
    "DEFAULT_MAX_STRINGS_FILES", "DEFAULT_MAX_STRINGS_BYTES",
    "CHUNK_BYTES", "CLASSIFY_WINDOW", "main",
]

SECTOR_SIZE = 512
CHUNK_BYTES = 8 * 1024 * 1024          # raw scan window
CLASSIFY_WINDOW = 1024 * 1024          # bytes classified for a long run
DEFAULT_MIN_RUN = 256                  # guide: الطبقة 2 minimum run
DEFAULT_MAX_STRINGS_FILES = 1000       # guide cap: أقصى 1000 ملف نصي
DEFAULT_MAX_STRINGS_BYTES = 64 * 1024 * 1024  # guide cap: إجمالي 64MB
DEFAULT_MIN_CONFIDENCE = 0.40          # keep gate passes and flagged borderline runs


def _realigned(data):
    """Trim up to 8 bytes per side until a UTF-8 strict decode succeeds.

    extract_text_runs stitches across <=2-byte gaps, so a run's boundary can
    land inside a multibyte sequence; classify() then falls back to an
    ASCII-only printable count and dilutes real UTF-8 text. Trimming the
    broken edges is alignment recovery, not re-classification.
    """
    for lead in range(9):
        body = data[lead:] if lead else data
        for tail in range(9):
            cand = body[:len(body) - tail] if tail else body
            if not cand:
                break
            try:
                cand.decode("utf-8")
                return cand
            except UnicodeDecodeError:
                continue
    return data


def _ensure_utf8_streams():
    """Never crash on Arabic output under a legacy console codepage."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _read_vbr(source_path, offset=0):
    with open(source_path, "rb") as fh:
        fh.seek(int(offset))
        return fh.read(SECTOR_SIZE)


def detect_filesystem(source_path, offset=0):
    """Classify the volume at `offset` from its boot sector, read-only."""
    vbr = _read_vbr(source_path, offset)
    if len(vbr) < SECTOR_SIZE:
        return "unknown"
    if vbr[3:8] == b"NTFS ":
        return "ntfs"
    if vbr[82:87] == b"FAT32":
        return "fat32"
    if vbr[82:85] in (b"FAT", b"MSW"):
        return "fat1x"
    return "unknown"


# ---------------------------------------------------------------------------
# Layer 2 + 3 fallback: raw strings scan, streaming with a caps ledger
# ---------------------------------------------------------------------------

def scan_strings(source_path, offset=0, length=None, out_dir=None,
                 min_run=DEFAULT_MIN_RUN,
                 max_files=DEFAULT_MAX_STRINGS_FILES,
                 max_bytes=DEFAULT_MAX_STRINGS_BYTES,
                 min_confidence=DEFAULT_MIN_CONFIDENCE,
                 chunk=CHUNK_BYTES, carve=True):
    """Extract text runs (Layer 2) and gate them (Layer 3) over raw bytes.

    Reads `length` bytes from `offset` in large chunks, stitches runs that
    cross chunk boundaries, and keeps only runs that pass
    text_classify.classify (entropy <= 4.0, printable >= 70%). Nothing is
    ever written back to the source; carved copies go to `out_dir` named
    strings_NNNNNN.txt. Returns a summary dict.
    """
    summary = {
        "source": str(source_path),
        "offset": int(offset),
        "length": None if length is None else int(length),
        "min_run": int(min_run),
        "candidates": [],
        "carved": [],
        "bytes_written": 0,
        "rejected": 0,
        "bytes_scanned": 0,
        "errors": [],
        "ok": False,
    }
    pending = None  # dict: start, encoding, data (bytearray)

    def finalize(frag, end_global):
        data = bytes(frag["data"])
        if len(data) < min_run:
            return
        aligned = _realigned(data)
        start = frag["start"]
        payload = aligned
        decision = text_classify.classify(aligned[:CLASSIFY_WINDOW],
                                          min_run=min_run)
        if (not decision["is_text"]
                and decision["printable"] >= 0.55
                and decision["entropy"] <= 4.60):
            # extract_text_runs stitches valid-UTF-8 random bytes into the
            # edges of a real run (random sequences that decode cleanly are
            # printable by design). Progressive trim (<=64B/side, step 8)
            # re-gates on the interior without touching the classifier.
            for head in range(0, 65, 8):
                found = False
                for tail in range(0, 65, 8):
                    if head + tail >= len(aligned) - min_run:
                        break
                    trial_data = aligned[head:len(aligned) - tail] if tail \
                        else aligned[head:]
                    trial = text_classify.classify(
                        trial_data[:CLASSIFY_WINDOW], min_run=min_run)
                    if trial["is_text"]:
                        payload, decision, start = trial_data, trial, \
                            frag["start"] + head
                        found = True
                        break
                if found:
                    break
        if not decision["is_text"] or decision["confidence"] < min_confidence:
            summary["rejected"] += 1
            return
        # Layer 4: n-gram filter. The hard gate above is recall-oriented, so a
        # valid ZIP whose comment is a long run of one byte clears it (entropy
        # 1.08, printable 0.81). This filter rejects it as language without
        # touching the gate's own numbers.
        #
        # `payload`, not `data`: the progressive trim above may have cut random
        # bytes that valid-UTF-8 noise stitched onto the edges of a real run.
        # Filtering the untrimmed buffer would reject the very runs the trim
        # just rescued.
        ng = text_ngram.ngram_filter(payload)
        if not ng["is_text"]:
            summary["rejected"] += 1
            summary["ngram_rejected"] = summary.get("ngram_rejected", 0) + 1
            return
        decision = dict(decision)
        decision["confidence"] = round(min(0.99, decision["confidence"]
                                           * 0.5 + ng["confidence"] * 0.5), 3)
        decision["reason"] = "%s; layer4: %s" % (decision["reason"],
                                                 ng["reason"])
        cand = {
            "kind": "strings",
            "start": frag["start"],
            "end": end_global,
            "size": len(data),
            "encoding": frag["encoding"],
            "confidence": decision["confidence"],
            "entropy": round(decision["entropy"], 3),
            "printable": round(decision["printable"], 3),
            "markers": round(decision["markers"], 3),
            "ngram": round(ng["trigram"], 3),
            "reason": decision["reason"],
            "carvable": True,
            "name": None,
        }
        summary["candidates"].append(cand)
        if carve and out_dir:
            if summary["bytes_written"] + len(data) > max_bytes:
                summary["errors"].append(
                    "strings byte cap reached (%d)" % max_bytes)
                cand["carvable"] = False
                return
            try:
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(
                    out_dir, "strings_%06d.txt" % len(summary["carved"]))
                with open(path, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                summary["errors"].append("strings write failed: %s" % exc)
                return
            summary["carved"].append({"path": path, "bytes": len(data),
                                      "start": cand["start"]})
            summary["bytes_written"] += len(data)

    try:
        with open(source_path, "rb") as fh:
            fh.seek(int(offset))
            file_pos = int(offset)
            scanned = 0
            stop = False
            while not stop:
                want = chunk if length is None else min(chunk, length - scanned)
                if want <= 0:
                    break
                buf = fh.read(want)
                if not buf:
                    break
                scanned += len(buf)
                base = file_pos
                file_pos += len(buf)
                runs = text_classify.extract_text_runs(buf, min_run=min_run)
                if not runs and pending is not None:
                    finalize(pending, base)
                    pending = None
                for (st, en, enc) in runs:
                    if pending is not None:
                        if st == 0 and enc == pending["encoding"]:
                            pending["data"] += buf[:en]
                            if en < len(buf):
                                finalize(pending, base + en)
                                pending = None
                            continue
                        finalize(pending, base)
                        pending = None
                    if en - st < min_run and en == len(buf) and st == 0:
                        continue
                    frag = {"start": base + st, "encoding": enc,
                            "data": bytearray(buf[st:en])}
                    if en == len(buf):
                        pending = frag
                    else:
                        finalize(frag, base + en)
                    if len(summary["candidates"]) >= max_files:
                        summary["errors"].append(
                            "strings file cap reached (%d)" % max_files)
                        stop = True
                        if pending is not None:
                            finalize(pending, file_pos)
                            pending = None
                        break
            if pending is not None:
                finalize(pending, file_pos)
        summary["bytes_scanned"] = scanned
        summary["ok"] = True
    except OSError as exc:
        summary["errors"].append("cannot scan %s: %s" % (source_path, exc))
    return summary


# ---------------------------------------------------------------------------
# top level: route Layer 1 by filesystem, fall back to Layer 2+3
# ---------------------------------------------------------------------------

def recover(source_path, out_dir=None, fs="auto", offset=0, volume_limit=None,
            limit=None, carve=True, strings=False,
            max_carve_bytes=text_recovery.DEFAULT_MAX_CARVE_BYTES,
            strings_offset=None, min_run=DEFAULT_MIN_RUN,
            max_files=DEFAULT_MAX_STRINGS_FILES,
            max_bytes=DEFAULT_MAX_STRINGS_BYTES,
            min_confidence=DEFAULT_MIN_CONFIDENCE):
    """Run the three-layer pipeline over one volume and merge the report.

    `fs` selects the Layer 1 parser ('auto' detects from the boot sector).
    The strings layer runs when `strings=True` or automatically when Layer 1
    produced no candidates (MFT/FAT entries overwritten -> fallback).
    `strings_offset`/scan caps apply to the raw pass only.
    """
    summary = {
        "source": str(source_path),
        "out_dir": out_dir,
        "offset": int(offset),
        "volume_limit": None if volume_limit is None else int(volume_limit),
        "filesystem": fs,
        "metadata": None,
        "strings": None,
        "report": [],
        "errors": [],
        "ok": False,
    }
    detected = fs
    if fs == "auto":
        try:
            detected = detect_filesystem(source_path, offset)
        except OSError as exc:
            summary["errors"].append("cannot read boot sector: %s" % exc)
            return summary
        summary["filesystem"] = detected
    meta = None
    if detected == "ntfs":
        meta = text_recovery.recover_mft(
            source_path, out_dir=out_dir, limit_records=limit, carve=carve,
            max_carve_bytes=max_carve_bytes, offset=offset,
            volume_limit=volume_limit)
    elif detected == "fat32":
        meta = fat32_recovery.recover_fat(
            source_path, out_dir=out_dir, offset=offset,
            volume_limit=volume_limit, limit_dirs=limit, carve=carve,
            max_carve_bytes=max_carve_bytes)
    else:
        summary["errors"].append(
            "no metadata layer for filesystem '%s' (fat1x/exFAT/raw)"
            % detected)
    summary["metadata"] = meta
    meta_candidates = list((meta or {}).get("candidates") or [])

    run_strings = strings or (meta is not None and meta.get("ok")
                              and not meta_candidates)
    str_summary = None
    if run_strings:
        str_summary = scan_strings(
            source_path,
            offset=(offset if strings_offset is None else int(strings_offset)),
            length=volume_limit, out_dir=out_dir, min_run=min_run,
            max_files=max_files, max_bytes=max_bytes,
            min_confidence=min_confidence, carve=carve)
    summary["strings"] = str_summary

    for cand in meta_candidates:
        summary["report"].append({
            "layer": "metadata",
            "name": cand.get("name"),
            "size": cand.get("size", cand.get("size_bytes", 0)),
            "confidence": cand.get("confidence", 0.0),
            "carvable": bool(cand.get("carvable")),
            "reason": cand.get("reason", ""),
        })
    if str_summary:
        for cand in str_summary.get("candidates") or []:
            summary["report"].append({
                "layer": "strings",
                "name": os.path.basename(
                    (cand.get("name") or "strings@%d" % cand.get("start", 0))),
                "size": cand.get("size", 0),
                "confidence": cand.get("confidence", 0.0),
                "carvable": bool(cand.get("carvable")),
                "reason": cand.get("reason", ""),
                "encoding": cand.get("encoding"),
                "start": cand.get("start"),
            })
    summary["report"].sort(key=lambda item: -float(item["confidence"]))
    meta_ok = bool(meta and meta.get("ok"))
    strings_ok = bool(str_summary and str_summary.get("ok"))
    summary["ok"] = meta_ok or strings_ok
    for err in (meta or {}).get("errors") or []:
        summary["errors"].append("metadata: %s" % err)
    for err in (str_summary or {}).get("errors") or []:
        summary["errors"].append("strings: %s" % err)
    return summary


def _print_summary(summary):
    print("source: %s  filesystem: %s" % (summary.get("source"),
                                          summary.get("filesystem")))
    if summary.get("offset"):
        print("offset: %d bytes" % summary["offset"])
    meta = summary.get("metadata")
    if meta is not None:
        deleted = meta.get("deleted", 0)
        print("layer 1 metadata: ok=%s deleted=%d" % (meta.get("ok"), deleted))
    str_sum = summary.get("strings")
    if str_sum is not None:
        print("layer 2+3 strings: scanned=%s kept=%d rejected=%d carved=%d"
              % (text_recovery._fmt_bytes(str_sum.get("bytes_scanned", 0)),
                 len(str_sum.get("candidates") or []),
                 str_sum.get("rejected", 0),
                 len(str_sum.get("carved") or [])))
    report = summary.get("report") or []
    if not report:
        print("no candidates from any layer")
    else:
        print("%-9s %-30s %12s %-8s %s" % ("layer", "name", "size",
                                           "conf", "reason"))
        for item in report:
            print("%-9s %-30s %12d %-8.2f %s"
                  % (item["layer"], str(item["name"] or "")[:30],
                     item["size"], item["confidence"],
                     (item["reason"] or "")[:60]))
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
    fs = "auto"
    offset = 0
    volume_limit = None
    limit = None
    carve = True
    strings = False
    i = 0
    while i < len(args):
        arg = args[i]
        def value(flag):
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
            return None
        if arg in ("--out", "--fs", "--offset", "--volume-limit", "--limit"):
            i += 1
            if i >= len(args):
                print("%s needs a value" % arg)
                return 2
            v = args[i]
            if arg == "--out":
                out_dir = v
            elif arg == "--fs":
                fs = v
            elif arg == "--offset":
                offset = int(v, 0)
            elif arg == "--volume-limit":
                volume_limit = int(v, 0)
            else:
                limit = int(v)
        elif arg.startswith("--out="):
            out_dir = value("--out")
        elif arg.startswith("--fs="):
            fs = value("--fs")
        elif arg.startswith("--offset="):
            offset = int(value("--offset"), 0)
        elif arg.startswith("--volume-limit="):
            volume_limit = int(value("--volume-limit"), 0)
        elif arg.startswith("--limit="):
            limit = int(value("--limit"))
        elif arg == "--strings":
            strings = True
        elif arg == "--no-carve":
            carve = False
        elif source_path is None:
            source_path = arg
        else:
            print("unexpected argument: %s" % arg)
            return 2
        i += 1
    if source_path is None:
        print("usage: python text_recovery_pipeline.py <image | device> "
              "[--out DIR] [--fs auto|ntfs|fat32] [--offset N] "
              "[--volume-limit N] [--limit N] [--strings] [--no-carve]")
        return 2
    summary = recover(source_path, out_dir=out_dir, fs=fs, offset=offset,
                      volume_limit=volume_limit, limit=limit, carve=carve,
                      strings=strings)
    _print_summary(summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
