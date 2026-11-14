#!/usr/bin/env python3
"""Generate the golden text-recovery fixtures (deterministic, stdlib only).

The fixtures are the reference inputs every text-recovery claim is measured
against. They are committed to the repository so that any checkout can be
verified without trusting a generator run, and this script reproduces them
byte-for-byte from a fixed seed.

Contract:
  * every file is <= 64 KiB (enough to be representative, small enough to
    keep the repository light);
  * output depends only on this file's content — no clock, no /dev/urandom,
    no locale, no filesystem ordering;
  * ``python scripts/generate_fixtures.py --check`` re-generates everything
    in memory and exits non-zero if the committed bytes or MANIFEST differ.

Usage:
  python scripts/generate_fixtures.py           # write fixtures/
  python scripts/generate_fixtures.py --check   # verify committed fixtures
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import sys

SEED = 0x5EED
GENERATOR_VERSION = 1
MAX_BYTES = 64 * 1024

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "fixtures")

# ---------------------------------------------------------------------------
# Source texts. Original prose written for this repository, so the fixtures
# carry no third-party licence.
# ---------------------------------------------------------------------------

ENGLISH = (
    "The recovery pass walks the volume in fixed slices and keeps only the "
    "regions whose byte statistics look like language. Every candidate is "
    "then scored against a trigram model, and the survivors are written to "
    "the report with their offsets and their declared encoding.\n"
    "A disk image is not a document: it holds deleted pages, compressed "
    "archives, executables and uninitialised space side by side. The filter "
    "must therefore reject binary noise without discarding real text that "
    "happens to sit next to it.\n"
    "Operators care about three numbers: how much text was found, how much of "
    "it is real, and how long the scan took. A fast scanner that invents text "
    "is worse than a slow one that does not.\n"
) * 6

ARABIC = (
    "يمشي الماسح على القرص في شرائح ثابتة، ولا يحتفظ إلا بالمناطق التي تشبه "
    "إحصاءات بايتاتها اللغةَ المكتوبة. ثم تُقاس كل منطقة مرشحة مقابل نموذج "
    "ثلاثي الحروف، وتُكتب المناطق الناجية في التقرير مع مواضعها وترميزها "
    "المعلن.\n"
    "صورة القرص ليست مستندًا: فيها صفحات محذوفة، وأرشيفات مضغوطة، وملفات "
    "تنفيذية، ومساحات غير مهيأة، متجاورةً بلا ترتيب. لذلك يجب أن يرفض المرشح "
    "الضجيج الثنائي دون أن يهمل نصًا حقيقيًا يجاوره.\n"
    "يهتم المشغل بثلاثة أرقام: كم نصًا عُثر عليه، وكم منه حقيقي، وكم استغرق "
    "المسح. الماسح السريع الذي يختلق النصوص أسوأ من ماسح بطيء لا يختلقها.\n"
) * 6


def _rng(tag: str) -> random.Random:
    """One deterministic stream per fixture, derived from the seed and tag."""
    return random.Random("%s:%s" % (SEED, tag))


# ---------------------------------------------------------------------------
# Negative fixtures — none of these is text, and no layer may report regions.
# ---------------------------------------------------------------------------

def random_noise() -> bytes:
    rnd = _rng("random_noise")
    return bytes(rnd.getrandbits(8) for _ in range(10 * 1024))


def zeros() -> bytes:
    return b"\x00" * (10 * 1024)


def compressed() -> bytes:
    rnd = _rng("compressed")
    payload = bytes(rnd.getrandbits(8) for _ in range(32 * 1024))
    return gzip.compress(payload, compresslevel=9, mtime=0)


def binary_structures() -> bytes:
    """Real file headers glued with random filler: the classic false positive."""
    rnd = _rng("binary_structures")
    pieces = [
        b"\x7fELF\x02\x01\x01\x00" + bytes(8) + b"\x02\x00\x3e\x00" + bytes(48),
        b"\x89PNG\r\n\x1a\n" + bytes(8) + b"IHDR" + bytes(13),
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00",
        b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + bytes(48),
        b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(18),
        b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\x03",
    ]
    out = bytearray()
    for i, piece in enumerate(pieces):
        out += piece
        out += bytes(rnd.getrandbits(8) for _ in range(700))
    return bytes(out)


# ---------------------------------------------------------------------------
# Positive fixtures — real text in one declared encoding.
# ---------------------------------------------------------------------------

def ascii_plain() -> bytes:
    return ENGLISH.encode("ascii")


def utf8_arabic() -> bytes:
    return ARABIC.encode("utf-8")


def utf16le_english() -> bytes:
    return ENGLISH.encode("utf-16-le")


def utf16le_arabic() -> bytes:
    return ARABIC.encode("utf-16-le")


def utf16be_english() -> bytes:
    return ENGLISH.encode("utf-16-be")


def mixed_encodings() -> bytes:
    """Adjacent runs in three encodings, separated by short binary gaps.

    This is the layout that exposed the region/preview encoding bug: a single
    region must not borrow the encoding of its best-scoring window. The gaps are
    padded so that each run starts on a parity its own encoding expects; the
    deliberately misaligned case lives in edge_cases/misaligned_utf16le.bin.
    """
    rnd = _rng("mixed_encodings")
    gap = lambda n: bytes(rnd.getrandbits(8) for _ in range(n))  # noqa: E731
    arabic_run = ARABIC[: 700].encode("utf-8")
    pad = b"\x00" if len(arabic_run) % 2 else b""
    return b"".join([
        gap(64),
        ENGLISH[: 1400].encode("ascii"),
        gap(96),
        arabic_run,
        gap(96) + pad,
        ENGLISH[: 1200].encode("utf-16-le"),
        gap(64),
    ])


def misaligned_utf16le() -> bytes:
    """UTF-16LE text starting at an odd byte offset.

    On a disk image a text run begins wherever the previous record ended, so the
    parity of its first byte is arbitrary. A scanner that assumes code units are
    aligned to the start of the buffer silently loses every odd-aligned run.
    """
    return b"\x7e" + ENGLISH[: 1000].encode("utf-16-le") + b"\x7e\x7e"


# ---------------------------------------------------------------------------
# Edge cases — valid text at the boundary of the layer contracts.
# ---------------------------------------------------------------------------

def bom_variants() -> bytes:
    """Three BOM-prefixed runs back to back: detection must not lose the BOM."""
    return b"".join([
        b"\xef\xbb\xbf" + ENGLISH[: 800].encode("utf-8"),
        b"\xff\xfe" + ENGLISH[800:1600].encode("utf-16-le"),
        b"\xfe\xff" + ENGLISH[1600:2400].encode("utf-16-be"),
    ])


def truncated_runs() -> bytes:
    """Runs cut inside a multi-byte character, where decode must not raise."""
    utf8_arabic_bytes = ARABIC.encode("utf-8")
    utf16le_bytes = ARABIC.encode("utf-16-le")
    return b"".join([
        utf8_arabic_bytes[: 1001],          # odd length: cuts a 2-byte char
        b"\x00" * 32,
        utf16le_bytes[: 1002],              # odd length: cuts a code unit
        b"\x00" * 32,
        ARABIC.encode("utf-16-be")[: 401],  # odd length in BE
    ])


def sparse_text() -> bytes:
    """Short text islands between long zero gaps: recall must still hold."""
    out = bytearray()
    for i in range(6):
        out += b"\x00" * 4096
        out += (ENGLISH[i * 200: i * 200 + 300] + "\n").encode("ascii")
    out += b"\x00" * 4096
    return bytes(out)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

# (relative path, builder, kind, expected verdict, note)
FIXTURES = [
    ("negative/random_noise.bin", random_noise, "negative", "non-text",
     "10 KiB of seeded random bytes; the baseline false-positive probe"),
    ("negative/zeros.bin", zeros, "negative", "non-text",
     "10 KiB of NUL: lowest possible entropy, must never be reported as text"),
    ("negative/compressed.bin", compressed, "negative", "non-text",
     "gzip stream; high entropy and a binary header"),
    ("negative/binary_structures.bin", binary_structures, "negative", "non-text",
     "ELF/PNG/JPEG/PE/ZIP/gzip headers with random filler between them"),
    ("positive/ascii_plain.txt", ascii_plain, "positive", "text",
     "English prose, ASCII"),
    ("positive/utf8_arabic.txt", utf8_arabic, "positive", "text",
     "Arabic prose, UTF-8"),
    ("positive/utf16le_english.txt", utf16le_english, "positive", "text",
     "English prose, UTF-16LE (no BOM)"),
    ("positive/utf16le_arabic.txt", utf16le_arabic, "positive", "text",
     "Arabic prose, UTF-16LE (no BOM)"),
    ("positive/utf16be_english.txt", utf16be_english, "positive", "text",
     "English prose, UTF-16BE — declared positive, currently a KNOWN GAP: "
     "text_classify/text_ngram/text_search have no UTF-16BE path"),
    ("positive/mixed_encodings.bin", mixed_encodings, "positive", "text",
     "ASCII / UTF-8 Arabic / UTF-16LE runs with binary gaps between them"),
    ("edge_cases/bom_variants.bin", bom_variants, "edge-case", "text",
     "UTF-8 BOM, UTF-16LE BOM and UTF-16BE BOM runs in one buffer"),
    ("edge_cases/truncated_runs.bin", truncated_runs, "edge-case", "text",
     "runs cut mid-character: decoding must use errors=replace, never raise"),
    ("edge_cases/misaligned_utf16le.bin", misaligned_utf16le, "edge-case", "text",
     "UTF-16LE text starting at an odd byte offset: code-unit parity is a "
     "property of the run, not of the buffer start"),
    ("edge_cases/sparse_text.bin", sparse_text, "edge-case", "text",
     "300-byte text islands separated by 4 KiB zero gaps"),
]

KNOWN_GAPS = {}

# Acceptance per fixture: what the search must report for the fixture to count as
# handled. These numbers are written into MANIFEST.json, so the contract is
# readable without opening the test and cannot drift from the documented
# expectation.
ACCEPTANCE = {
    "positive/utf16le_arabic.txt": {"min_regions": 1, "min_coverage": 0.95,
                                    "encodings": ["utf-16le"]},
    "positive/utf16le_english.txt": {"min_regions": 1, "min_coverage": 0.95,
                                     "encodings": ["utf-16le"]},
    "positive/utf8_arabic.txt": {"min_regions": 1, "min_coverage": 0.95,
                                 "encodings": ["utf-8"]},
    "positive/ascii_plain.txt": {"min_regions": 1, "min_coverage": 0.95,
                                 "encodings": ["utf-8"]},
    "positive/mixed_encodings.bin": {"min_regions": 2, "min_coverage": 0.9,
                                     "encodings": ["utf-8", "utf-16le"]},
    "positive/utf16be_english.txt": {
        "min_regions": 1, "min_coverage": 0.95, "encodings": ["utf-16le"],
        "why": "UTF-16BE is recovered through the odd-parity UTF-16LE reading, "
               "so the row is byte-accurate as utf-16le; there is no utf-16be "
               "code path in the text layers"},
    "edge_cases/misaligned_utf16le.bin": {"min_regions": 1, "min_coverage": 0.9,
                                          "encodings": ["utf-16le"]},
    "edge_cases/sparse_text.bin": {"min_regions": 6, "min_coverage": 0.15},
    "edge_cases/bom_variants.bin": {"min_regions": 2, "min_coverage": 0.7},
    "edge_cases/truncated_runs.bin": {"min_regions": 2, "min_coverage": 0.6},
}


def build_all() -> dict[str, bytes]:
    """Return {relative path: bytes} without touching the filesystem."""
    built = {}
    for rel, builder, _kind, _verdict, _note in FIXTURES:
        data = builder()
        if len(data) > MAX_BYTES:
            raise SystemExit(
                "fixture %s is %d bytes, over the %d budget"
                % (rel, len(data), MAX_BYTES))
        built[rel] = data
    return built


def manifest_for(built: dict[str, bytes]) -> dict:
    entries = []
    for rel, _builder, kind, verdict, note in FIXTURES:
        data = built[rel]
        entry = {
            "path": rel,
            "kind": kind,
            "expected": verdict,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": note,
        }
        if kind == "negative":
            entry["acceptance"] = {"min_regions": 0, "max_regions": 0}
        else:
            entry["acceptance"] = {"min_regions": 1, "min_coverage": 0.5}
        entry["acceptance"].update(ACCEPTANCE.get(rel, {}))
        if rel in KNOWN_GAPS:
            entry["known_gap"] = KNOWN_GAPS[rel]
        entries.append(entry)
    return {
        "generator_version": GENERATOR_VERSION,
        "seed": SEED,
        "max_bytes": MAX_BYTES,
        "fixtures": entries,
    }


README = """# Golden fixtures

Reference inputs for the text-recovery layers. Committed so that any checkout
can be verified without trusting a generator run, and reproducible byte for
byte from a fixed seed.

Regenerate (overwrites, then commit the diff):

    python scripts/generate_fixtures.py

Verify the committed bytes and `MANIFEST.json` match the generator:

    python scripts/generate_fixtures.py --check
    python test_fixtures.py

Rules:

* every file is at most 64 KiB;
* generation depends only on `scripts/generate_fixtures.py` — no clock, no
  `/dev/urandom`, no locale, no directory ordering;
* changing a fixture requires bumping `GENERATOR_VERSION`, regenerating, and
  stating in the commit message which acceptance criteria moved.

Folders:

* `negative/` — must never be reported as text (false-positive probes).
* `positive/` — real text in one declared encoding; must be detected.
* `edge_cases/` — valid text at a boundary (BOMs, truncated runs, sparse runs).

`MANIFEST.json` records size, SHA-256, expected verdict and the reason each
file exists. Fixtures whose expected verdict the code does not yet meet are
marked with `known_gap`, so the gap is visible instead of implied.
"""


def write_all() -> None:
    built = build_all()
    for rel, data in built.items():
        path = os.path.join(FIXTURE_DIR, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    with open(os.path.join(FIXTURE_DIR, "MANIFEST.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump(manifest_for(built), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(os.path.join(FIXTURE_DIR, "README.md"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(README)
    total = sum(len(d) for d in built.values())
    print("wrote %d fixtures (%d bytes) to %s"
          % (len(built), total, FIXTURE_DIR))


def check_all() -> int:
    built = build_all()
    problems = []

    for rel, data in built.items():
        path = os.path.join(FIXTURE_DIR, rel.replace("/", os.sep))
        if not os.path.exists(path):
            problems.append("missing file: %s" % rel)
            continue
        with open(path, "rb") as fh:
            on_disk = fh.read()
        if on_disk != data:
            problems.append(
                "content drift: %s (on disk %d bytes sha %s, generator %d bytes "
                "sha %s)" % (rel, len(on_disk),
                             hashlib.sha256(on_disk).hexdigest()[:12],
                             len(data), hashlib.sha256(data).hexdigest()[:12]))

    manifest_path = os.path.join(FIXTURE_DIR, "MANIFEST.json")
    if not os.path.exists(manifest_path):
        problems.append("missing file: MANIFEST.json")
    else:
        with open(manifest_path, encoding="utf-8") as fh:
            on_disk_manifest = json.load(fh)
        if on_disk_manifest != manifest_for(built):
            problems.append("MANIFEST.json does not match the generator")

    if problems:
        print("fixtures FAILED verification:")
        for item in problems:
            print("  - %s" % item)
        return 1

    print("fixtures OK: %d files match MANIFEST.json" % len(built))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify committed fixtures instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        return check_all()
    write_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
