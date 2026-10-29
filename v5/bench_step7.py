#!/usr/bin/env python3
"""
bench_step7.py - reproducible throughput benchmark for v5/raw_scan.py.

WHY THIS EXISTS
---------------
V5_PLAN.md section 13.8 says every performance figure the project has quoted so
far came from one 256 MB synthetic fixture, and that no measurement exists on
real media. This harness is the tool that is supposed to produce those numbers,
so it is deliberately explicit about what it measured: every row reports the
byte count, the pattern set, the worker count actually used and whether carving
was on. Nothing in the table is estimated, projected or synthesised - each cell
is read back from the run that produced it (bytes_scanned / sampled_bytes, the
"mode" line raw_scan writes into its own results file, perf_counter wall time).

FIXTURE COMPOSITION (so the MB/s numbers mean something)
--------------------------------------------------------
A real freshly-used disk, as this scanner sees it, is not uniform. The fixture
is built from 1 MiB tiles so that the proportions are exact and repeatable:

  * 49%  STRUCTURED - one repeating text block: fixed-width ASCII directory-
          style records ("%08d  sector  ok  fid=%04X  block  data  pad") that
          repeat for the whole tile and are reused for every structured tile.
          This is the "mostly a repeating structured block" part, and it is
          the realistic worst case for the matcher: lots of text, no
          signatures, every byte scanned by the pattern loop.
          The block is checked at generation time against the default pattern
          set and the run aborts if any signature can match it, so a hit count
          can never be inflated by the filler itself.
  * 30%  ZERO - multi-MiB runs of 0x00 (2-6 tiles long, laid out at random),
          the empty space of a freshly formatted volume. This is what the
          "zero region fast skip" in scan() is for; its effect shows up as the
          difference between this fixture and a fully random file.
  * 21%  HIGH-ENTROPY FILLER - seeded PRNG bytes (random.Random.randbytes),
          standing in for compressed/encrypted payloads: unskippable, every
          byte searched, nothing to find.
  * ~200 SCATTERED MARKERS on top of the non-zero tiles: one signature per
          slot (64 KiB slots at the default 64 MiB size, so ~32 markers per
          tile, closer together only on small fixtures), at a random offset
          inside the slot, so hits are spread evenly rather than clustered.
          The mix is PDF magic (%PDF-), PNG magic (89 50 4E 47 0D 0A 1A 0A), a
          Markdown heading ("\\n# ") with a name marker (DECISIONS), a plain
          text marker (WORK_PLAN) and a Python header (shebang + "import ") -
          i.e. both magic-byte and text signatures, weighted 34/34/14/10/8.
          The mix is magic-heavy on purpose: a text hit that lands in printable
          bytes is extended by smart text carving to ~2 MiB, so the text share
          is what decides how long row (b) takes and how much disk it writes,
          and a 50/50 mix made one 64 MiB run write over a gigabyte.

  Because the filler is generated from a seeded Python PRNG, the fixture is
  byte-identical for a given --size/--hits/--seed, which is what makes the table
  comparable between machines and between commits.

  One consequence worth stating up front: the high-entropy region produces a
  few STATISTICAL FALSE POSITIVES from the default set's 2-3 byte signatures
  (the `":` JSON pattern alone matches ~48 times per 3 MiB of random data, and
  a CRLF heading once or twice). Real compressed/encrypted regions do exactly
  that, so the hit counts below are "marker matches + incidental matches", not
  "200". The marker count is printed next to the fixture so the two can be
  compared.

WHAT EACH ROW IS
----------------
  a.  phase 1 only: scan(..., carve_dir=None), default pattern set (the
      built-in signature table, case sensitive), chunk 8 MiB, min_gap=0
      (scan()'s own default - the interactive CLI passes MIN_GAP=1 MiB, so a
      CLI run reports FEWER hits than the a/c rows here).
  b.  the same scan with carving ON at the module defaults
      (carve_before=4096, carve_after=65536) and carve_limit=0 (unlimited), so
      the carve share of the total is visible as b - a.
  c1. row (a) with PARALLEL_WORKERS=1 - the sequential engine.
  c2. row (a) with PARALLEL_WORKERS=None - the auto worker count
      (min(4, cpu_count)). The speed-up is table[c1] / table[c2]; it is
      measured here, not quoted.
  d.  run_fast_scan(): first+last 5% of the source inside the 5-minute budget,
      no carving. Its byte count is the sampled bytes, not the file size.
  e.  run_progressive() with percent=50, i.e. exactly two slices, carves as it
      goes (ENABLE_CARVING) and uses the module's MIN_GAP=1 MiB and
      CARVE_LIMIT=100 - so its hit count is lower than (a) by design, and its
      seconds include organizing + provenance writes per slice.

Each row runs in its own fresh temporary output directory, which is deleted
right after the row is measured (its carved byte count is recorded first).

USAGE
-----
  ./python/python.exe v5/bench_step7.py                 # 64 MiB, all rows
  ./python/python.exe v5/bench_step7.py --quick         # 16 MiB, rows a + c2
  ./python/python.exe v5/bench_step7.py --size 128 --hits 400 --json out.json
  ./python/python.exe v5/bench_step7.py --repeat 3      # best of 3 per row

Exit code is 0 only when every requested measurement completed; any exception
in a measurement is printed, marks the row FAILED and gives exit code 1.
"""

import argparse
import contextlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import raw_scan  # noqa: E402  (must come after sys.path tweak)

MiB = 1024 * 1024
TILE = 1 * MiB            # fixture tile / granularity of the composition
MB = 1024.0 * 1024.0      # "MB/s" below means MiB/s, same base as fmt_bytes

# Marker templates: (name, bytes, weight, labels the default pattern set reports)
# Weights are tuned so a default 64 MiB fixture reports ~250 hits and the carve
# row writes a few hundred MiB, not gigabytes: smart text carving extends every
# TEXT hit that sits in printable bytes by up to ~2 MiB, so the text-marker
# share is the disk/time knob. The mix still covers magic bytes (pdf/png) and
# text signatures (md heading, plain-word marker, python header).
MARKERS = [
    ("pdf", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n", 34),
    ("png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00\x10\x00\x00\x00"
            b"\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa", 34),
    ("md",  b"\n# DECISIONS\n\n- milestone list\n- scratch notes for the run\n", 14),
    ("txt", b"WORK_PLAN\nstep one  index the blocks\n", 10),
    ("py",  b"#!/usr/bin/env python\nimport os\n", 8),
]

MODE_RE = re.compile(r"mode\s+:\s+(?:parallel phase 1 \((\d+) processes\)|single process)")


# ----------------------------------------------------------------------
# fixture
# ----------------------------------------------------------------------

def _structured_tile():
    """One MiB of repeating fixed-width directory-style text records."""
    out = bytearray()
    i = 0
    while len(out) < TILE:
        out += ("%08d  sector  ok  fid=%04X  block  data  pad\n"
                % (i, i & 0xFFFF)).encode("ascii")
        i += 1
    del out[TILE:]
    return bytes(out)


def _check_structured_tile(block, patterns):
    """Abort if the 'no signature' filler can actually match a signature."""
    bad = [(label, pat) for label, pat, _ext, _ci in patterns if pat and pat in block]
    if bad:
        raise RuntimeError("fixture bug: the structured block matches %d pattern(s): %s"
                           % (len(bad), ", ".join(label for label, _ in bad[:4])))


def _plan_roles(n_tiles, rng, zero_share=0.30, rand_share=0.21):
    """Exact proportions: zero runs of 2-6 tiles, rest structured/random."""
    n_zero = int(round(zero_share * n_tiles))
    n_rand = int(round(rand_share * n_tiles))
    if n_zero + n_rand >= n_tiles:
        raise SystemExit("--size %d MiB is too small for the default mix" % n_tiles)
    roles = [None] * n_tiles

    # lay the zero tiles down as runs, at random free positions
    while n_zero > 0:
        free = [i for i, r in enumerate(roles) if r is None]
        if not free:
            break
        run = min(n_zero, rng.randint(2, 6))
        ok_starts = []
        for start in free:
            if start + run <= n_tiles and all(roles[start + k] is None for k in range(run)):
                ok_starts.append(start)
        if not ok_starts:
            run = 1
            ok_starts = free
        start = rng.choice(ok_starts)
        for k in range(run):
            roles[start + k] = "zero"
        n_zero -= run

    free = [i for i, r in enumerate(roles) if r is None]
    rng.shuffle(free)
    for i in free[:n_rand]:
        roles[i] = "random"
    for i in free[n_rand:]:
        roles[i] = "structured"
    return roles


def _plan_markers(roles, count, rng):
    """
    {tile_index: [(offset, marker_bytes), ...]}, spread evenly: every marker
    gets its own slot inside a non-zero tile, so the ~N hits are scattered
    across the whole file instead of clustering. Slot size shrinks for small
    fixtures (16 KiB floor) so `--hits` is honoured at any --size.
    """
    nonzero = [t for t, role in enumerate(roles) if role != "zero"]
    if not nonzero:
        raise SystemExit("the fixture has no non-zero tile to place markers in")
    per_tile = max(1, min(64, -(-count // len(nonzero))))
    slot = TILE // per_tile
    if count > len(nonzero) * per_tile:
        raise SystemExit("--hits %d does not fit: %d non-zero tiles x %d slots"
                         % (count, len(nonzero), per_tile))
    slots = [(t, s) for t in nonzero for s in range(per_tile)]
    plan = {}
    kinds = [m[0] for m in MARKERS]
    weights = [m[2] for m in MARKERS]
    bodies = dict((m[0], m[1]) for m in MARKERS)
    for tile, idx in rng.sample(slots, count):
        kind = rng.choices(kinds, weights=weights)[0]
        body = bodies[kind]
        off = idx * slot + rng.randrange(0, max(1, slot - len(body) - 1))
        plan.setdefault(tile, []).append((off, body))
    return plan


def build_fixture(path, size_mb, hits, seed, patterns, verbose=True):
    """Write the fixture. Returns a dict describing exactly what was written."""
    total = size_mb * MiB
    n_tiles = total // TILE
    rng = random.Random(seed)
    struct_tile = _structured_tile()
    _check_structured_tile(struct_tile, patterns)
    roles = _plan_roles(n_tiles, rng)
    plan = _plan_markers(roles, hits, rng)

    zero_tile = b"\x00" * TILE
    started = time.perf_counter()
    with open(path, "wb") as fh:
        for idx, role in enumerate(roles):
            if role == "zero":
                fh.write(zero_tile)
                continue
            if role == "random":
                buf = bytearray(rng.randbytes(TILE))
            else:
                buf = bytearray(struct_tile)
            for off, body in plan.get(idx, ()):
                buf[off:off + len(body)] = body
            fh.write(buf)
        written = total
        tail = total - n_tiles * TILE
        if tail > 0:                      # only for a non-integer-MiB --size
            fh.write(struct_tile[:tail])
            written += tail

    counts = {"structured": roles.count("structured"),
              "zero": roles.count("zero"),
              "random": roles.count("random")}
    if verbose:
        print("fixture: %s  %.1f MiB  in %.1fs" % (path, written / MB,
                                                  time.perf_counter() - started))
        print("         tiles: %d structured / %d zero / %d random (1 MiB each)"
              % (counts["structured"], counts["zero"], counts["random"]))
        by_kind = {}
        for tile in plan:
            for _off, body in plan[tile]:
                key = next(k for k, b, _w in MARKERS if b is body)
                by_kind[key] = by_kind.get(key, 0) + 1
        print("         markers: %d placed (%s)  seed=%d"
              % (hits, ", ".join("%s x%d" % (k, by_kind[k]) for k in sorted(by_kind)),
                 seed))
        print("         note: the high-entropy region also yields a few statistical "
              "false positives from")
        print("               the 2-3 byte signatures (e.g. the '\\\":' JSON pattern), as real")
        print("               compressed data does; the hit counts below include them.")
    return {"path": path, "bytes": os.path.getsize(path), "tiles": counts,
            "markers_placed": sum(len(v) for v in plan.values()), "seed": seed}


# ----------------------------------------------------------------------
# measurement plumbing
# ----------------------------------------------------------------------

def dir_bytes(path):
    total = 0
    if not path or not os.path.isdir(path):
        return 0
    for cur, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(cur, name))
            except OSError:
                pass
    return total


def log_workers(results_path):
    """The worker count raw_scan itself logged ('mode : ...'), or None."""
    try:
        with open(results_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    matches = list(MODE_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return int(last.group(1)) if last.group(1) else 1


def effective_workers(total):
    """What scan() will really use: auto count, but 1 below PARALLEL_MIN_SIZE."""
    workers = raw_scan.parallel_worker_count()
    if workers > 1 and total and total < raw_scan.PARALLEL_MIN_SIZE:
        return 1
    return workers


@contextlib.contextmanager
def quiet(log_path):
    """Swallow raw_scan's console output into a per-row log file."""
    with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
        with contextlib.redirect_stdout(fh):
            yield


class Bench(object):
    def __init__(self, fixture, total, patterns, base_dir, keep=False):
        self.fixture = fixture
        self.total = total
        self.patterns = patterns
        self.base = base_dir
        self.keep = keep
        self.seq = 0

    def fresh_dir(self):
        self.seq += 1
        path = os.path.join(self.base, "run%02d" % self.seq)
        os.makedirs(path, exist_ok=True)
        return path

    def drop(self, path):
        if self.keep:
            return
        shutil.rmtree(path, ignore_errors=True)

    # -- the six measurements ------------------------------------------

    def scan_row(self, workers, carve):
        work = self.fresh_dir()
        results = os.path.join(work, "search_results.txt")
        carve_dir = os.path.join(work, "carved") if carve else None
        if carve:
            os.makedirs(carve_dir, exist_ok=True)
        raw_scan.PARALLEL_WORKERS = workers
        try:
            t0 = time.perf_counter()
            with quiet(os.path.join(work, "stdout.log")):
                summary = raw_scan.scan(
                    self.fixture, self.patterns, raw_scan.CHUNK_SIZE, results,
                    carve_dir=carve_dir,
                    carve_before=raw_scan.CARVE_BEFORE,
                    carve_after=raw_scan.CARVE_AFTER,
                    show_progress=False, min_gap=0, carve_limit=0)
            secs = time.perf_counter() - t0
        finally:
            raw_scan.PARALLEL_WORKERS = None
        used = log_workers(results)
        carved_bytes = dir_bytes(carve_dir) if carve else 0
        notes = []
        if used is not None and (workers is None) and effective_workers(self.total) > 1 and used == 1:
            notes.append("engine fell back to a single process (see results log)")
        if carve:
            notes.append("carved %d file(s), %.1f MiB written (smart text carving "
                         "extends text hits to ~2 MiB each, which is why b costs "
                         "far more than the source size)"
                         % (summary["carved_files"], carved_bytes / MB))
        row = {"seconds": secs,
               "bytes": summary.get("bytes_scanned") or 0,
               "hits": len(summary["hits"]),
               "workers": used if used is not None else effective_workers(self.total),
               "carve": bool(carve),
               "notes": notes,
               "extra": {"carved_files": summary["carved_files"],
                         "carved_bytes": carved_bytes,
                         "zero_bytes": summary.get("zero_bytes") or 0,
                         "raw_elapsed": round(summary.get("elapsed", 0.0), 4),
                         "results_log": os.path.basename(results)}}
        self.drop(work)
        return row

    def fast_row(self):
        work = self.fresh_dir()
        t0 = time.perf_counter()
        with quiet(os.path.join(work, "stdout.log")):
            summary = raw_scan.run_fast_scan(self.fixture, self.total, self.patterns,
                                             True, budget=None, percent=None,
                                             base_dir=work)
        secs = time.perf_counter() - t0
        sampled = summary.get("sampled_bytes") or summary.get("bytes_scanned") or 0
        ranges = summary.get("ranges") or []
        covered = " + ".join("%.1f-%.1f MiB" % (a / MB, (b if b is not None else self.total) / MB)
                             for a, b in ranges)
        notes = ["sampled %s of the source (%s)" % ("%.1f MiB" % (sampled / MB), covered)]
        if summary.get("budget_hit"):
            notes.append("hit the %gs budget - partial read, bytes above are what it read"
                         % raw_scan.FAST_SCAN_BUDGET)
        row = {"seconds": secs, "bytes": sampled, "hits": len(summary["hits"]),
               "workers": effective_workers(sampled), "carve": False,
               "notes": notes,
               "extra": {"budget_hit": bool(summary.get("budget_hit")),
                         "ranges": [[a, b] for a, b in ranges],
                         "projection": summary.get("projection")}}
        self.drop(work)
        return row

    def progressive_row(self, percent=50):
        work = self.fresh_dir()
        recovered = os.path.join(work, "recovered_bench")
        organized = os.path.join(work, "organized_bench")
        os.makedirs(os.path.join(recovered, "carved"), exist_ok=True)
        os.makedirs(os.path.join(organized, "by_extension"), exist_ok=True)
        expected = -(-100 // percent)      # number of slices
        orig_ask = raw_scan._ask
        raw_scan._ask = lambda prompt="": ("y", True)   # answer "continue" for us
        raw_scan.PARALLEL_WORKERS = None
        try:
            t0 = time.perf_counter()
            with quiet(os.path.join(work, "stdout.log")):
                combined = raw_scan.run_progressive(
                    self.fixture, self.total, self.patterns, True, recovered, organized,
                    time.strftime("%Y-%m-%d %H:%M:%S"), resume_from=0, tag="bench",
                    percent=percent)
            secs = time.perf_counter() - t0
        finally:
            raw_scan._ask = orig_ask
            raw_scan.PARALLEL_WORKERS = None
        notes = ["%d slice(s) of %g%% ; module MIN_GAP=%s and CARVE_LIMIT=%d apply"
                 % (expected, percent, raw_scan.fmt_bytes(raw_scan.MIN_GAP),
                    raw_scan.CARVE_LIMIT),
                 "includes organize_output + provenance write per slice"]
        if combined.get("stopped_early"):
            notes.append("stopped early at %s"
                         % raw_scan.fmt_bytes(combined.get("resume_offset") or 0))
        row = {"seconds": secs,
               "bytes": combined.get("bytes_scanned") or 0,
               "hits": len(combined["hits"]),
               "workers": effective_workers(self.total),
               "carve": True,
               "notes": notes,
               "extra": {"carved_files": combined.get("carved_files"),
                         "carved_bytes": dir_bytes(recovered),
                         "resume_offset": combined.get("resume_offset"),
                         "raw_elapsed": round(combined.get("elapsed", 0.0), 4)}}
        self.drop(work)
        return row


# ----------------------------------------------------------------------
# rows + table
# ----------------------------------------------------------------------

ROWS = [
    ("a", "a. phase1 only (default patterns, no carve)",
     lambda b: b.scan_row(None, False)),
    ("b", "b. phase1 + carve (4 KiB before / 64 KiB after, limit 0)",
     lambda b: b.scan_row(None, True)),
    ("c1", "c1. phase1 PARALLEL_WORKERS=1 (sequential)",
     lambda b: b.scan_row(1, False)),
    ("c2", "c2. phase1 PARALLEL_WORKERS=None (auto)",
     lambda b: b.scan_row(None, False)),
    ("d", "d. fast scan (first+last 5%, 300 s budget)",
     lambda b: b.fast_row()),
    ("e", "e. progressive 2 slices (percent=50, carves)",
     lambda b: b.progressive_row(50)),
]
QUICK_ROWS = ["a", "c2"]

NAME_W, NUM_W = 54, 11


def header_lines(args, fixture, patterns, cpu):
    arch = "%s %s" % (sys.platform, "64-bit" if sys.maxsize > 2 ** 32 else "32-bit")
    lines = []
    lines.append("bench_step7 - v5/raw_scan.py on a synthetic fixture")
    lines.append("python %s (%s) | cpu %d | chunk %s | patterns %d (built-in, case sensitive)"
                 % (sys.version.split()[0], arch, cpu,
                    raw_scan.fmt_bytes(raw_scan.CHUNK_SIZE), len(patterns)))
    lines.append("parallel: PARALLEL_MIN_SIZE=%s, auto workers=min(4,cpu)=%d"
                 % (raw_scan.fmt_bytes(raw_scan.PARALLEL_MIN_SIZE),
                    min(4, cpu or 1)))
    lines.append("fixture : %.1f MiB = %d structured + %d zero + %d random tiles (1 MiB), "
                 "%d markers, seed=%d"
                 % (fixture["bytes"] / MB, fixture["tiles"]["structured"],
                    fixture["tiles"]["zero"], fixture["tiles"]["random"],
                    fixture["markers_placed"], fixture["seed"]))
    lines.append("scan    : min_gap=0 (scan() default), carve_before/after=%d/%d B"
                 % (raw_scan.CARVE_BEFORE, raw_scan.CARVE_AFTER))
    git = git_commit()
    if git:
        lines.append("repo    : %s" % git)
    digest = raw_scan_digest()
    if digest:
        lines.append("subject : raw_scan.py sha256 %s" % digest[:16])
    return lines


def git_commit():
    try:
        out = subprocess.run(["git", "log", "--oneline", "-1"], cwd=SCRIPT_DIR,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10)
        text = out.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0] if text else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def raw_scan_digest():
    """sha256 of the file under test, so a number can be tied to exact source."""
    import hashlib
    try:
        with open(raw_scan.__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def render_table(rows, quick):
    head = ("%-*s %*s %7s %5s %9s %8s %6s"
            % (NAME_W, "measurement", NUM_W, "bytes", "workers", "carve",
               "seconds", "MB/s", "hits"))
    out = [head, "-" * len(head)]
    for row in rows:
        if row.get("error"):
            out.append("%-*s %*s %7s %5s %9s %8s %6s"
                       % (NAME_W, row["name"], NUM_W, "-", "-", "-",
                          "FAILED", "-", "-"))
            continue
        out.append("%-*s %*s %7d %5s %9.3f %8.1f %6d"
                   % (NAME_W, row["name"], NUM_W, "{:,}".format(row["bytes"]),
                      row["workers"], "on" if row["carve"] else "off",
                      row["seconds"], row["mb_s"], row["hits"]))
    return "\n".join(out)


def render_footer(rows, args):
    by = dict((r["key"], r) for r in rows)
    lines = []
    notes = [(r["name"], n) for r in rows for n in (r.get("notes") or [])]
    if notes:
        lines.append("")
        for name, note in notes:
            lines.append("note: %s -> %s" % (name.split(".")[0], note))
    seq, par = by.get("c1"), by.get("c2")
    if seq and par and not seq.get("error") and not par.get("error") and par["seconds"] > 0:
        lines.append("")
        lines.append("derived: measured parallel speed-up c1/c2 = %.2fx "
                     "(%.3fs sequential vs %.3fs with %d workers)"
                     % (seq["seconds"] / par["seconds"], seq["seconds"],
                        par["seconds"], par["workers"]))
    a, b = by.get("a"), by.get("b")
    if a and b and not a.get("error") and not b.get("error"):
        lines.append("derived: carve time b-a = %.3fs (%.0f%% of row b)"
                     % (b["seconds"] - a["seconds"],
                        100.0 * (b["seconds"] - a["seconds"]) / b["seconds"]
                        if b["seconds"] else 0.0))
    lines.append("")
    lines.append("what this does not measure:")
    lines.append("  - real media: the fixture is one file on one SSD/NVMe, read through the")
    lines.append("    OS page cache. After the first row the file is cached, so these are")
    lines.append("    CPU/signature-search numbers, not disk throughput; no seek cost, no")
    lines.append("    HDD head movement, no USB/SD read speed, no bad sectors or timeouts.")
    lines.append("  - NTFS metadata, MFT layout, fragmentation, compression or sparse files:")
    lines.append("    a formatted volume's block layout is not reproduced by a flat file.")
    lines.append("  - raw-device path: no \\\\.\\PhysicalDrive reads, no 4096-byte alignment")
    lines.append("    cost, no admin-rights effects.")
    lines.append("  - the interactive CLI's MIN_GAP=1 MiB de-duplication, its signature/profile")
    lines.append("    menus and the fast scan's 5-minute budget being actually hit (the")
    lines.append("    fixture is far smaller than the media the budget exists for).")
    lines.append("  - carving quality (which bytes are real evidence, triage accuracy); only")
    lines.append("    the time and the number of files written are measured.")
    if args.quick:
        lines.append("  - --quick: rows b, c1, d, e were not run, and a %.0f MiB fixture is"
                     % (args.size,))
        lines.append("    below PARALLEL_MIN_SIZE (%s), so the auto row cannot go parallel."
                     % raw_scan.fmt_bytes(raw_scan.PARALLEL_MIN_SIZE))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Benchmark v5/raw_scan.py on a generated fixture.")
    ap.add_argument("--size", type=int, default=None,
                    help="fixture size in MiB (default 64, or 16 with --quick)")
    ap.add_argument("--hits", type=int, default=200,
                    help="number of scattered signature markers (default 200)")
    ap.add_argument("--seed", type=int, default=1337, help="fixture PRNG seed")
    ap.add_argument("--json", metavar="PATH", default="",
                    help="write the same rows as JSON to PATH")
    ap.add_argument("--quick", action="store_true",
                    help="16 MiB fixture, rows a and c2 only")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each measurement N times, report the fastest")
    ap.add_argument("--keep", action="store_true",
                    help="keep the fixture and the per-row output dirs")
    args = ap.parse_args(argv)
    if args.size is None:
        args.size = 16 if args.quick else 64
    if args.size < 4:
        ap.error("--size must be at least 4 MiB")
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    want = QUICK_ROWS if args.quick else [key for key, _label, _fn in ROWS]

    patterns = raw_scan.build_patterns(True)
    cpu = os.cpu_count() or 0

    base = os.environ.get("TMPDIR") or None
    work_root = os.path.join(base, "bench_step7_%d" % os.getpid())
    os.makedirs(work_root, exist_ok=True)
    fixture_path = os.path.join(work_root, "fixture.bin")

    print("work dir: %s" % work_root)
    try:
        fixture = build_fixture(fixture_path, args.size, args.hits, args.seed, patterns)
        total = fixture["bytes"]
        bench = Bench(fixture_path, total, patterns, work_root, keep=args.keep)
        print()
        print("\n".join(header_lines(args, fixture, patterns, cpu)))
        print()

        rows = []
        failures = 0
        for key, label, fn in ROWS:
            if key not in want:
                continue
            row = {"key": key, "name": label}
            best = None
            try:
                for _ in range(args.repeat):
                    result = fn(bench)
                    if best is None or result["seconds"] < best["seconds"]:
                        best = result
                best["name"] = label
                best["key"] = key
                best["mb_s"] = (best["bytes"] / MB / best["seconds"]
                                if best["seconds"] > 0 else 0.0)
                if args.repeat > 1:
                    best.setdefault("notes", []).append("best of %d runs" % args.repeat)
                row.update(best)
            except Exception as exc:                      # noqa: BLE001 - report, don't hide
                failures += 1
                row["error"] = "%s: %s" % (type(exc).__name__, exc)
                row["traceback"] = traceback.format_exc()
                print("MEASUREMENT FAILED: %s\n%s" % (label, row["traceback"]),
                      file=sys.stderr)
            rows.append(row)

        table = render_table(rows, args.quick)
        footer = render_footer(rows, args)
        print(table)
        print(footer)

        if args.json:
            payload = {
                "meta": {
                    "tool": "v5/bench_step7.py",
                    "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "git_commit": git_commit(),
                    "raw_scan_sha256": raw_scan_digest(),
                    "python": sys.version.split()[0],
                    "cpu_count": cpu,
                    "auto_workers": min(4, cpu or 1),
                    "parallel_min_size": raw_scan.PARALLEL_MIN_SIZE,
                    "chunk_size": raw_scan.CHUNK_SIZE,
                    "patterns": len(patterns),
                    "pattern_labels": [p[0] for p in patterns],
                    "min_gap": 0,
                    "carve_before": raw_scan.CARVE_BEFORE,
                    "carve_after": raw_scan.CARVE_AFTER,
                    "carve_limit": 0,
                    "fixture": dict(fixture, path="(deleted)"),
                    "fixture_mb": total / MB,
                    "quick": bool(args.quick),
                    "repeat": args.repeat,
                },
                "rows": [dict((k, v) for k, v in r.items() if k != "traceback"
                              and not isinstance(v, list))
                         for r in rows],
            }
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            print("\nJSON written: %s" % args.json)

        return 1 if failures else 0
    finally:
        if args.keep:
            print("\nkept: %s" % work_root)
        else:
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
